#include "LineRenderer.h"
#include "ComponentFactory.h"
#include "Transform.h"
#include <algorithm>
#include <cmath>
#include <core/log/InxLog.h>
#include <nlohmann/json.hpp>
#include <stdexcept>

using json = nlohmann::json;

namespace infernux
{

namespace
{
constexpr uint32_t LINE_VERTEX_MARKER = 0x4C494E45u;
constexpr float DIRECTION_EPSILON = 1.0e-6f;
constexpr float METRIC_EPSILON = 1.0e-5f;
constexpr uint32_t MAX_ROUNDING_VERTICES = 1024u;

bool ApproximatelyEqual(const glm::mat3 &left, const glm::mat3 &right)
{
    for (uint32_t column = 0; column < 3; ++column) {
        for (uint32_t row = 0; row < 3; ++row) {
            const float scale = std::max({1.0f, std::abs(left[column][row]), std::abs(right[column][row])});
            if (std::abs(left[column][row] - right[column][row]) > METRIC_EPSILON * scale)
                return false;
        }
    }
    return true;
}

void RequireFinite(const glm::vec3 &value, const char *name)
{
    if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
        throw std::invalid_argument(std::string(name) + " must contain finite coordinates");
}

void RequireFinite(const glm::vec4 &value, const char *name)
{
    if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z) || !std::isfinite(value.w))
        throw std::invalid_argument(std::string(name) + " must contain finite channels");
}

void RequireFinite(const glm::vec2 &value, const char *name)
{
    if (!std::isfinite(value.x) || !std::isfinite(value.y))
        throw std::invalid_argument(std::string(name) + " must contain finite coordinates");
}

float RequireNonNegative(float value, const char *name)
{
    if (!std::isfinite(value) || value < 0.0f)
        throw std::invalid_argument(std::string(name) + " must be finite and non-negative");
    return value;
}

void ValidateWidthCurve(const std::vector<LineWidthKey> &keys)
{
    if (keys.empty())
        throw std::invalid_argument("LineRenderer width curve requires at least one key");
    for (size_t index = 0; index < keys.size(); ++index) {
        const auto &key = keys[index];
        if (!std::isfinite(key.time) || !std::isfinite(key.value) || !std::isfinite(key.inTangent) ||
            !std::isfinite(key.outTangent))
            throw std::invalid_argument("LineRenderer width curve keys must be finite");
        if (key.value < 0.0f)
            throw std::invalid_argument("LineRenderer width curve values must be non-negative");
        if (index > 0 && keys[index - 1].time >= key.time)
            throw std::invalid_argument("LineRenderer width curve key times must be strictly increasing");
    }
}

void ValidateColorGradient(const std::vector<LineColorKey> &keys)
{
    if (keys.empty())
        throw std::invalid_argument("LineRenderer color gradient requires at least one key");
    for (size_t index = 0; index < keys.size(); ++index) {
        const auto &key = keys[index];
        if (!std::isfinite(key.time) || key.time < 0.0f || key.time > 1.0f)
            throw std::invalid_argument("LineRenderer color gradient key times must be between zero and one");
        RequireFinite(key.color, "LineRenderer color gradient key");
        if (index > 0 && keys[index - 1].time >= key.time)
            throw std::invalid_argument("LineRenderer color gradient key times must be strictly increasing");
    }
}

float WrapCurveTime(float time, float first, float last, LineCurveWrapMode mode)
{
    const float duration = last - first;
    if (duration <= DIRECTION_EPSILON || mode == LineCurveWrapMode::Clamp)
        return std::clamp(time, first, last);
    const float cycles = (time - first) / duration;
    float fraction = cycles - std::floor(cycles);
    if (mode == LineCurveWrapMode::PingPong && (static_cast<int64_t>(std::floor(cycles)) & 1LL) != 0)
        fraction = 1.0f - fraction;
    return first + fraction * duration;
}

float EvaluateWidthCurve(const std::vector<LineWidthKey> &keys, float time, LineCurveWrapMode preWrap,
                         LineCurveWrapMode postWrap)
{
    if (keys.size() == 1)
        return keys.front().value;
    if (time < keys.front().time)
        time = WrapCurveTime(time, keys.front().time, keys.back().time, preWrap);
    else if (time > keys.back().time)
        time = WrapCurveTime(time, keys.front().time, keys.back().time, postWrap);
    if (time <= keys.front().time)
        return keys.front().value;
    if (time >= keys.back().time)
        return keys.back().value;
    const auto right = std::upper_bound(keys.begin(), keys.end(), time, [](float sampleTime, const LineWidthKey &key) {
        return sampleTime < key.time;
    });
    const auto &rightKey = *right;
    const auto &leftKey = *(right - 1);
    const float duration = rightKey.time - leftKey.time;
    const float t = (time - leftKey.time) / duration;
    const float t2 = t * t;
    const float t3 = t2 * t;
    return (2.0f * t3 - 3.0f * t2 + 1.0f) * leftKey.value + (t3 - 2.0f * t2 + t) * duration * leftKey.outTangent +
           (-2.0f * t3 + 3.0f * t2) * rightKey.value + (t3 - t2) * duration * rightKey.inTangent;
}

glm::vec3 LinearRgbToOklab(const glm::vec3 &color)
{
    const float l = std::cbrt(0.4122214708f * color.r + 0.5363325363f * color.g + 0.0514459929f * color.b);
    const float m = std::cbrt(0.2119034982f * color.r + 0.6806995451f * color.g + 0.1073969566f * color.b);
    const float s = std::cbrt(0.0883024619f * color.r + 0.2817188376f * color.g + 0.6299787005f * color.b);
    return {0.2104542553f * l + 0.7936177850f * m - 0.0040720468f * s,
            1.9779984951f * l - 2.4285922050f * m + 0.4505937099f * s,
            0.0259040371f * l + 0.7827717662f * m - 0.8086757660f * s};
}

glm::vec3 OklabToLinearRgb(const glm::vec3 &color)
{
    const float lRoot = color.x + 0.3963377774f * color.y + 0.2158037573f * color.z;
    const float mRoot = color.x - 0.1055613458f * color.y - 0.0638541728f * color.z;
    const float sRoot = color.x - 0.0894841775f * color.y - 1.2914855480f * color.z;
    const float l = lRoot * lRoot * lRoot;
    const float m = mRoot * mRoot * mRoot;
    const float s = sRoot * sRoot * sRoot;
    return {4.0767416621f * l - 3.3077115913f * m + 0.2309699292f * s,
            -1.2684380046f * l + 2.6097574011f * m - 0.3413193965f * s,
            -0.0041960863f * l - 0.7034186147f * m + 1.7076147010f * s};
}

glm::vec4 EvaluateColorGradient(const std::vector<LineColorKey> &keys, float time, LineGradientMode mode)
{
    if (keys.size() == 1 || time <= keys.front().time)
        return keys.front().color;
    if (time >= keys.back().time)
        return keys.back().color;
    const auto right = std::upper_bound(keys.begin(), keys.end(), time, [](float sampleTime, const LineColorKey &key) {
        return sampleTime < key.time;
    });
    const auto &rightKey = *right;
    const auto &leftKey = *(right - 1);
    if (mode == LineGradientMode::Fixed)
        return leftKey.color;
    const float t = (time - leftKey.time) / (rightKey.time - leftKey.time);
    if (mode == LineGradientMode::PerceptualBlend) {
        const glm::vec3 color = OklabToLinearRgb(
            glm::mix(LinearRgbToOklab(glm::vec3(leftKey.color)), LinearRgbToOklab(glm::vec3(rightKey.color)), t));
        return glm::vec4(color, glm::mix(leftKey.color.a, rightKey.color.a, t));
    }
    return glm::mix(leftKey.color, rightKey.color, t);
}

float PointSegmentDistanceSquared(const glm::vec3 &point, const glm::vec3 &start, const glm::vec3 &end)
{
    const glm::vec3 segment = end - start;
    const float lengthSquared = glm::dot(segment, segment);
    if (lengthSquared <= DIRECTION_EPSILON * DIRECTION_EPSILON)
        return glm::dot(point - start, point - start);
    const float t = std::clamp(glm::dot(point - start, segment) / lengthSquared, 0.0f, 1.0f);
    const glm::vec3 delta = point - (start + segment * t);
    return glm::dot(delta, delta);
}

void SimplifyRange(const std::vector<glm::vec3> &points, size_t first, size_t last, float toleranceSquared,
                   std::vector<bool> &keep)
{
    if (last <= first + 1)
        return;
    float maximumDistance = -1.0f;
    size_t furthest = first;
    for (size_t index = first + 1; index < last; ++index) {
        const float distance = PointSegmentDistanceSquared(points[index], points[first], points[last]);
        if (distance > maximumDistance) {
            maximumDistance = distance;
            furthest = index;
        }
    }
    if (maximumDistance <= toleranceSquared)
        return;
    keep[furthest] = true;
    SimplifyRange(points, first, furthest, toleranceSquared, keep);
    SimplifyRange(points, furthest, last, toleranceSquared, keep);
}
} // namespace

INFERNUX_REGISTER_VALIDATED_COMPONENT("LineRenderer", LineRenderer)

LineRenderer::LineRenderer()
{
    SetCastShadows(false);
    SetInlineMeshName("Line");
    UpdateMaximumWidth();
    RebuildMesh();
}

void LineRenderer::SetPositionCount(size_t count)
{
    if (m_positions.size() == count)
        return;
    m_positions.resize(count, glm::vec3(0.0f));
    RebuildMesh();
}

glm::vec3 LineRenderer::GetPosition(size_t index) const
{
    if (index >= m_positions.size())
        throw std::out_of_range("LineRenderer position index is out of range");
    return m_positions[index];
}

void LineRenderer::SetPosition(size_t index, const glm::vec3 &position)
{
    if (index >= m_positions.size())
        throw std::out_of_range("LineRenderer position index is out of range");
    RequireFinite(position, "LineRenderer position");
    if (m_positions[index] == position)
        return;
    m_positions[index] = position;
    RebuildMesh();
}

void LineRenderer::SetPositions(const std::vector<glm::vec3> &positions)
{
    for (const auto &position : positions)
        RequireFinite(position, "LineRenderer position");
    if (m_positions == positions)
        return;
    m_positions = positions;
    RebuildMesh();
}

float LineRenderer::GetStartWidth() const
{
    return EvaluateWidthCurve(m_widthCurve, 0.0f, m_widthCurvePreWrap, m_widthCurvePostWrap);
}

void LineRenderer::SetStartWidth(float width)
{
    width = RequireNonNegative(width, "LineRenderer start width");
    if (std::abs(GetStartWidth() - width) <= DIRECTION_EPSILON)
        return;
    const auto key = std::lower_bound(m_widthCurve.begin(), m_widthCurve.end(), 0.0f,
                                      [](const LineWidthKey &entry, float time) { return entry.time < time; });
    if (key != m_widthCurve.end() && std::abs(key->time) <= DIRECTION_EPSILON)
        key->value = width;
    else
        m_widthCurve.insert(key, LineWidthKey{0.0f, width, 0.0f, 0.0f});
    UpdateMaximumWidth();
    RebuildMesh();
}

float LineRenderer::GetEndWidth() const
{
    return EvaluateWidthCurve(m_widthCurve, 1.0f, m_widthCurvePreWrap, m_widthCurvePostWrap);
}

void LineRenderer::SetEndWidth(float width)
{
    width = RequireNonNegative(width, "LineRenderer end width");
    if (std::abs(GetEndWidth() - width) <= DIRECTION_EPSILON)
        return;
    const auto key = std::lower_bound(m_widthCurve.begin(), m_widthCurve.end(), 1.0f,
                                      [](const LineWidthKey &entry, float time) { return entry.time < time; });
    if (key != m_widthCurve.end() && std::abs(key->time - 1.0f) <= DIRECTION_EPSILON)
        key->value = width;
    else
        m_widthCurve.insert(key, LineWidthKey{1.0f, width, 0.0f, 0.0f});
    UpdateMaximumWidth();
    RebuildMesh();
}

void LineRenderer::SetWidthMultiplier(float multiplier)
{
    multiplier = RequireNonNegative(multiplier, "LineRenderer width multiplier");
    if (m_widthMultiplier == multiplier)
        return;
    m_widthMultiplier = multiplier;
    UpdateMaximumWidth();
    RebuildMesh();
}

void LineRenderer::SetWidthCurve(const std::vector<LineWidthKey> &keys)
{
    ValidateWidthCurve(keys);
    m_widthCurve = keys;
    UpdateMaximumWidth();
    RebuildMesh();
}

void LineRenderer::SetWidthCurvePreWrap(LineCurveWrapMode mode)
{
    if (m_widthCurvePreWrap == mode)
        return;
    m_widthCurvePreWrap = mode;
    UpdateMaximumWidth();
    RebuildMesh();
}

void LineRenderer::SetWidthCurvePostWrap(LineCurveWrapMode mode)
{
    if (m_widthCurvePostWrap == mode)
        return;
    m_widthCurvePostWrap = mode;
    UpdateMaximumWidth();
    RebuildMesh();
}

glm::vec4 LineRenderer::GetStartColor() const
{
    return EvaluateColorGradient(m_colorGradient, 0.0f, m_colorGradientMode);
}

void LineRenderer::SetStartColor(const glm::vec4 &color)
{
    RequireFinite(color, "LineRenderer start color");
    if (GetStartColor() == color)
        return;
    if (std::abs(m_colorGradient.front().time) <= DIRECTION_EPSILON)
        m_colorGradient.front().color = color;
    else
        m_colorGradient.insert(m_colorGradient.begin(), LineColorKey{0.0f, color});
    RebuildMesh();
}

glm::vec4 LineRenderer::GetEndColor() const
{
    return EvaluateColorGradient(m_colorGradient, 1.0f, m_colorGradientMode);
}

void LineRenderer::SetEndColor(const glm::vec4 &color)
{
    RequireFinite(color, "LineRenderer end color");
    if (GetEndColor() == color)
        return;
    if (std::abs(m_colorGradient.back().time - 1.0f) <= DIRECTION_EPSILON)
        m_colorGradient.back().color = color;
    else
        m_colorGradient.push_back(LineColorKey{1.0f, color});
    RebuildMesh();
}

void LineRenderer::SetColorGradient(const std::vector<LineColorKey> &keys)
{
    ValidateColorGradient(keys);
    m_colorGradient = keys;
    RebuildMesh();
}

void LineRenderer::SetColorGradientMode(LineGradientMode mode)
{
    if (m_colorGradientMode == mode)
        return;
    m_colorGradientMode = mode;
    RebuildMesh();
}

void LineRenderer::SetLoop(bool loop)
{
    if (m_loop == loop)
        return;
    m_loop = loop;
    RebuildMesh();
}

void LineRenderer::SetUseWorldSpace(bool useWorldSpace)
{
    if (m_useWorldSpace == useWorldSpace)
        return;
    m_useWorldSpace = useWorldSpace;
    RebuildMesh();
}

void LineRenderer::SetAlignment(LineAlignment alignment)
{
    if (m_alignment == alignment)
        return;
    m_alignment = alignment;
    RebuildMesh();
}

void LineRenderer::SetTextureMode(LineTextureMode mode)
{
    if (m_textureMode == mode)
        return;
    m_textureMode = mode;
    RebuildMesh();
}

void LineRenderer::SetTextureScale(const glm::vec2 &scale)
{
    RequireFinite(scale, "LineRenderer texture scale");
    if (m_textureScale == scale)
        return;
    m_textureScale = scale;
    RebuildMesh();
}

void LineRenderer::SetNumCornerVertices(uint32_t count)
{
    if (count > MAX_ROUNDING_VERTICES)
        throw std::invalid_argument("LineRenderer corner vertex count exceeds the supported limit");
    if (m_numCornerVertices == count)
        return;
    m_numCornerVertices = count;
    RebuildMesh();
}

void LineRenderer::SetNumCapVertices(uint32_t count)
{
    if (count > MAX_ROUNDING_VERTICES)
        throw std::invalid_argument("LineRenderer cap vertex count exceeds the supported limit");
    if (m_numCapVertices == count)
        return;
    m_numCapVertices = count;
    RebuildMesh();
}

void LineRenderer::SetShadowBias(float bias)
{
    bias = RequireNonNegative(bias, "LineRenderer shadow bias");
    if (m_shadowBias == bias)
        return;
    m_shadowBias = bias;
    RebuildMesh();
}

void LineRenderer::SetGenerateLightingData(bool generate)
{
    if (m_generateLightingData == generate)
        return;
    m_generateLightingData = generate;
    RebuildMesh();
}

void LineRenderer::BakeMesh(MeshRenderer &target, const glm::vec3 &cameraPosition, bool useTransform) const
{
    RequireFinite(cameraPosition, "LineRenderer bake camera position");
    const std::vector<Vertex> sourceVertices = GetInlineVertices();
    const std::vector<uint32_t> sourceIndices = GetInlineIndices();
    std::vector<Vertex> bakedVertices;
    bakedVertices.reserve(sourceVertices.size());

    glm::mat4 objectWorld(1.0f);
    if (!m_useWorldSpace) {
        if (const Transform *transform = GetTransform())
            objectWorld = transform->GetWorldMatrix();
    }
    const glm::mat4 inverseWorld = glm::inverse(objectWorld);
    const glm::mat3 normalMatrix = glm::transpose(glm::inverse(glm::mat3(objectWorld)));
    const bool outputWorldSpace = m_useWorldSpace || useTransform;

    for (const Vertex &source : sourceVertices) {
        Vertex baked = source;
        const glm::vec3 centerWorld = glm::vec3(objectWorld * glm::vec4(source.pos, 1.0f));
        glm::vec3 tangentWorld = glm::mat3(objectWorld) * glm::vec3(source.tangent);
        if (glm::dot(tangentWorld, tangentWorld) <= DIRECTION_EPSILON * DIRECTION_EPSILON)
            tangentWorld = glm::vec3(1.0f, 0.0f, 0.0f);
        else
            tangentWorld = glm::normalize(tangentWorld);
        glm::vec3 facing =
            m_alignment == LineAlignment::View ? cameraPosition - centerWorld : normalMatrix * source.normal;
        if (glm::dot(facing, facing) <= DIRECTION_EPSILON * DIRECTION_EPSILON)
            facing = glm::vec3(0.0f, 0.0f, 1.0f);
        else
            facing = glm::normalize(facing);
        glm::vec3 side = glm::cross(facing, tangentWorld);
        if (glm::dot(side, side) <= DIRECTION_EPSILON * DIRECTION_EPSILON)
            side = glm::cross(glm::vec3(0.0f, 1.0f, 0.0f), tangentWorld);
        if (glm::dot(side, side) <= DIRECTION_EPSILON * DIRECTION_EPSILON)
            side = glm::vec3(1.0f, 0.0f, 0.0f);
        else
            side = glm::normalize(side);
        const glm::vec3 expandedWorld = centerWorld + side * source.boneWeights.x;

        if (outputWorldSpace) {
            baked.pos = expandedWorld;
            baked.normal = facing;
            baked.tangent = glm::vec4(tangentWorld, 1.0f);
        } else {
            baked.pos = glm::vec3(inverseWorld * glm::vec4(expandedWorld, 1.0f));
            glm::vec3 localNormal = glm::transpose(glm::mat3(objectWorld)) * facing;
            if (glm::dot(localNormal, localNormal) > DIRECTION_EPSILON * DIRECTION_EPSILON)
                localNormal = glm::normalize(localNormal);
            baked.normal = localNormal;
            baked.tangent = source.tangent;
        }
        baked.boneIndices = glm::uvec4(0u);
        baked.boneWeights = glm::vec4(0.0f);
        bakedVertices.push_back(baked);
    }

    target.SetMesh(std::move(bakedVertices), sourceIndices);
    target.SetInlineMeshName("Baked Line");
}

void LineRenderer::Simplify(float tolerance)
{
    tolerance = RequireNonNegative(tolerance, "LineRenderer simplify tolerance");
    if (m_positions.size() <= 2 || tolerance == 0.0f)
        return;

    std::vector<bool> keep(m_positions.size(), false);
    keep.front() = true;
    keep.back() = true;
    SimplifyRange(m_positions, 0, m_positions.size() - 1, tolerance * tolerance, keep);

    std::vector<glm::vec3> simplified;
    simplified.reserve(m_positions.size());
    for (size_t index = 0; index < m_positions.size(); ++index) {
        if (keep[index])
            simplified.push_back(m_positions[index]);
    }
    m_positions = std::move(simplified);
    RebuildMesh();
}

glm::mat4 LineRenderer::ResolveRenderWorldMatrix(const glm::mat4 &objectWorldMatrix) const
{
    return m_useWorldSpace ? glm::mat4(1.0f) : objectWorldMatrix;
}

void LineRenderer::RefreshProceduralGeometry(const glm::mat4 &objectWorldMatrix)
{
    if (m_useWorldSpace || m_textureMode != LineTextureMode::Tile)
        return;
    const glm::mat3 linearTransform(objectWorldMatrix);
    const glm::mat3 metric = glm::transpose(linearTransform) * linearTransform;
    if (ApproximatelyEqual(m_geometryMetric, metric))
        return;
    m_geometryMetric = metric;
    RebuildMesh();
}

void LineRenderer::ComputeWorldBounds(const glm::mat4 &worldMatrix, glm::vec3 &outMin, glm::vec3 &outMax) const
{
    if (m_positions.empty()) {
        outMin = outMax = glm::vec3(ResolveRenderWorldMatrix(worldMatrix)[3]);
        return;
    }
    MeshRenderer::ComputeWorldBounds(ResolveRenderWorldMatrix(worldMatrix), outMin, outMax);
    const float radius = 0.5f * m_maximumWidth;
    outMin -= glm::vec3(radius);
    outMax += glm::vec3(radius);
}

void LineRenderer::UpdateMaximumWidth()
{
    float maximumWidth = 0.0f;
    for (uint32_t sample = 0; sample <= 64; ++sample) {
        const float t = static_cast<float>(sample) / 64.0f;
        maximumWidth =
            std::max(maximumWidth, EvaluateWidthCurve(m_widthCurve, t, m_widthCurvePreWrap, m_widthCurvePostWrap));
    }
    m_maximumWidth = maximumWidth * m_widthMultiplier;
}

void LineRenderer::RebuildMesh()
{
    std::vector<Vertex> vertices;
    std::vector<uint32_t> indices;

    // Consecutive duplicate samples are common in runtime trails while an
    // object is stationary. Keeping them creates degenerate triangles and
    // arbitrary fallback tangents, which twist or flicker when movement resumes.
    // Preserve the authored point list, but build only meaningful segments.
    std::vector<glm::vec3> geometryPositions;
    geometryPositions.reserve(m_positions.size());
    for (const glm::vec3 &position : m_positions) {
        if (geometryPositions.empty() ||
            glm::dot(position - geometryPositions.back(), position - geometryPositions.back()) >
                DIRECTION_EPSILON * DIRECTION_EPSILON)
            geometryPositions.push_back(position);
    }
    if (geometryPositions.size() > 2 && m_loop &&
        glm::dot(geometryPositions.back() - geometryPositions.front(),
                 geometryPositions.back() - geometryPositions.front()) <= DIRECTION_EPSILON * DIRECTION_EPSILON)
        geometryPositions.pop_back();

    if (geometryPositions.size() < 2) {
        SetProceduralMesh(std::move(vertices), std::move(indices));
        return;
    }

    struct RibbonSample
    {
        glm::vec3 position{0.0f};
        glm::vec3 tangent{1.0f, 0.0f, 0.0f};
        float distance = 0.0f;
        float normalizedDistance = 0.0f;
        float distributedCoordinate = 0.0f;
        float segmentCoordinate = 0.0f;
        float widthScale = 1.0f;
    };

    const size_t authoredCount = geometryPositions.size();
    const size_t segmentCount = m_loop && authoredCount > 2 ? authoredCount : authoredCount - 1;
    std::vector<float> distances(authoredCount, 0.0f);
    const auto metricLength = [&](const glm::vec3 &delta) {
        if (!m_useWorldSpace && m_textureMode == LineTextureMode::Tile) {
            const float squaredLength = glm::dot(delta, m_geometryMetric * delta);
            return std::sqrt(std::max(0.0f, squaredLength));
        }
        return glm::length(delta);
    };
    for (size_t index = 1; index < authoredCount; ++index)
        distances[index] = distances[index - 1] + metricLength(geometryPositions[index] - geometryPositions[index - 1]);
    float totalLength = distances.back();
    if (m_loop && authoredCount > 2)
        totalLength += metricLength(geometryPositions.front() - geometryPositions.back());

    const auto safeDirection = [&](const glm::vec3 &from, const glm::vec3 &to) {
        const glm::vec3 direction = to - from;
        return glm::dot(direction, direction) > DIRECTION_EPSILON * DIRECTION_EPSILON ? glm::normalize(direction)
                                                                                      : glm::vec3(1.0f, 0.0f, 0.0f);
    };
    const auto normalizedDistance = [&](float distance, size_t index) {
        return totalLength > DIRECTION_EPSILON ? distance / totalLength
                                               : static_cast<float>(index) / static_cast<float>(authoredCount - 1);
    };

    std::vector<RibbonSample> samples;
    samples.reserve(authoredCount * (static_cast<size_t>(m_numCornerVertices) + 1u) +
                    static_cast<size_t>(m_numCapVertices) * 2u + 1u);
    for (size_t index = 0; index < authoredCount; ++index) {
        const bool hasIncoming = index > 0 || (m_loop && authoredCount > 2);
        const bool hasOutgoing = index + 1 < authoredCount || (m_loop && authoredCount > 2);
        const size_t previous = index > 0 ? index - 1 : authoredCount - 1;
        const size_t next = index + 1 < authoredCount ? index + 1 : 0;
        const glm::vec3 incoming = hasIncoming ? safeDirection(geometryPositions[previous], geometryPositions[index])
                                               : safeDirection(geometryPositions[index], geometryPositions[next]);
        const glm::vec3 outgoing =
            hasOutgoing ? safeDirection(geometryPositions[index], geometryPositions[next]) : incoming;
        const float distance = distances[index];
        const float t = normalizedDistance(distance, index);
        const float distributed = static_cast<float>(index) / static_cast<float>(segmentCount);
        const float segment = static_cast<float>(index);

        if (hasIncoming && hasOutgoing && m_numCornerVertices > 0) {
            for (uint32_t corner = 0; corner <= m_numCornerVertices; ++corner) {
                const float blend = static_cast<float>(corner) / static_cast<float>(m_numCornerVertices);
                glm::vec3 tangent = glm::mix(incoming, outgoing, blend);
                if (glm::dot(tangent, tangent) <= DIRECTION_EPSILON * DIRECTION_EPSILON)
                    tangent = corner * 2u < m_numCornerVertices ? incoming : outgoing;
                else
                    tangent = glm::normalize(tangent);
                samples.push_back({geometryPositions[index], tangent, distance, t, distributed, segment, 1.0f});
            }
        } else {
            glm::vec3 tangent = glm::normalize(incoming + outgoing);
            if (!std::isfinite(tangent.x) || !std::isfinite(tangent.y) || !std::isfinite(tangent.z))
                tangent = outgoing;
            samples.push_back({geometryPositions[index], tangent, distance, t, distributed, segment, 1.0f});
        }
    }

    if (m_loop && authoredCount > 2) {
        RibbonSample closing = samples.front();
        closing.distance = totalLength;
        closing.normalizedDistance = 1.0f;
        closing.distributedCoordinate = 1.0f;
        closing.segmentCoordinate = static_cast<float>(segmentCount);
        samples.push_back(closing);
    } else if (m_numCapVertices > 0) {
        const RibbonSample first = samples.front();
        const RibbonSample last = samples.back();
        const float startRadius =
            0.5f * std::max(0.0f, EvaluateWidthCurve(m_widthCurve, 0.0f, m_widthCurvePreWrap, m_widthCurvePostWrap)) *
            m_widthMultiplier;
        const float endRadius =
            0.5f * std::max(0.0f, EvaluateWidthCurve(m_widthCurve, 1.0f, m_widthCurvePreWrap, m_widthCurvePostWrap)) *
            m_widthMultiplier;
        std::vector<RibbonSample> capped;
        capped.reserve(samples.size() + static_cast<size_t>(m_numCapVertices) * 2u);
        for (uint32_t cap = 0; cap < m_numCapVertices; ++cap) {
            const float axial = -1.0f + static_cast<float>(cap) / static_cast<float>(m_numCapVertices);
            RibbonSample sample = first;
            sample.position += first.tangent * (axial * startRadius);
            sample.widthScale = std::sqrt(std::max(0.0f, 1.0f - axial * axial));
            capped.push_back(sample);
        }
        capped.insert(capped.end(), samples.begin(), samples.end());
        for (uint32_t cap = 1; cap <= m_numCapVertices; ++cap) {
            const float axial = static_cast<float>(cap) / static_cast<float>(m_numCapVertices);
            RibbonSample sample = last;
            sample.position += last.tangent * (axial * endRadius);
            sample.widthScale = std::sqrt(std::max(0.0f, 1.0f - axial * axial));
            capped.push_back(sample);
        }
        samples = std::move(capped);
    }

    vertices.reserve(samples.size() * 2u);
    indices.reserve((samples.size() - 1u) * 6u);
    for (const auto &sample : samples) {
        const float t = sample.normalizedDistance;
        const float halfWidth =
            0.5f * std::max(0.0f, EvaluateWidthCurve(m_widthCurve, t, m_widthCurvePreWrap, m_widthCurvePostWrap)) *
            m_widthMultiplier * sample.widthScale;
        const glm::vec4 color = EvaluateColorGradient(m_colorGradient, t, m_colorGradientMode);
        float u = t;
        switch (m_textureMode) {
        case LineTextureMode::Tile:
            u = sample.distance;
            break;
        case LineTextureMode::DistributePerSegment:
            u = sample.distributedCoordinate;
            break;
        case LineTextureMode::RepeatPerSegment:
            u = sample.segmentCoordinate;
            break;
        case LineTextureMode::Stretch:
        case LineTextureMode::Static:
            break;
        }
        u *= m_textureScale.x;

        for (uint32_t side = 0; side < 2; ++side) {
            Vertex vertex;
            vertex.pos = sample.position;
            vertex.normal = glm::vec3(0.0f, 0.0f, 1.0f);
            vertex.tangent = glm::vec4(sample.tangent, 1.0f);
            vertex.color = glm::vec3(color);
            vertex.texCoord = glm::vec2(u, static_cast<float>(side) * m_textureScale.y);
            vertex.boneIndices = glm::uvec4(0u, m_generateLightingData ? 1u : 0u, static_cast<uint32_t>(m_alignment),
                                            LINE_VERTEX_MARKER);
            vertex.boneWeights = glm::vec4(side == 0 ? -halfWidth : halfWidth, color.a, m_shadowBias, 0.0f);
            vertices.push_back(vertex);
        }
    }

    for (uint32_t segment = 0; segment + 1 < samples.size(); ++segment) {
        const uint32_t base = segment * 2;
        indices.insert(indices.end(), {base, base + 2, base + 1, base + 1, base + 2, base + 3});
    }
    SetProceduralMesh(std::move(vertices), std::move(indices));
}

nlohmann::json LineRenderer::SerializeDocument() const
{
    json document = MeshRenderer::SerializeDocument();
    document["type"] = "LineRenderer";
    document["meshId"] = 0u;
    document["useInlineMesh"] = false;
    document.erase("meshAssetGuid");
    document.erase("inlineMeshName");
    document.erase("inlineMeshBuiltin");
    document.erase("inlineVertices");
    document.erase("inlineIndices");

    document["positions"] = json::array();
    for (const auto &position : m_positions)
        document["positions"].push_back({position.x, position.y, position.z});
    document["widthMultiplier"] = m_widthMultiplier;
    document["widthCurve"] = {{"keys", json::array()},
                              {"preWrap", static_cast<int>(m_widthCurvePreWrap)},
                              {"postWrap", static_cast<int>(m_widthCurvePostWrap)}};
    for (const auto &key : m_widthCurve) {
        document["widthCurve"]["keys"].push_back(
            {{"time", key.time}, {"value", key.value}, {"inTangent", key.inTangent}, {"outTangent", key.outTangent}});
    }
    document["colorGradient"] = {{"keys", json::array()}, {"mode", static_cast<int>(m_colorGradientMode)}};
    for (const auto &key : m_colorGradient)
        document["colorGradient"]["keys"].push_back(
            {{"time", key.time}, {"color", {key.color.r, key.color.g, key.color.b, key.color.a}}});
    document["loop"] = m_loop;
    document["useWorldSpace"] = m_useWorldSpace;
    document["alignment"] = static_cast<int>(m_alignment);
    document["textureMode"] = static_cast<int>(m_textureMode);
    document["textureScale"] = {m_textureScale.x, m_textureScale.y};
    document["numCornerVertices"] = m_numCornerVertices;
    document["numCapVertices"] = m_numCapVertices;
    document["shadowBias"] = m_shadowBias;
    document["generateLightingData"] = m_generateLightingData;
    return document;
}

void LineRenderer::ValidateSerializedDocument(const nlohmann::json &document)
{
    ValidateSerializedDocumentForType(document, "LineRenderer");
}

bool LineRenderer::DeserializeDocument(const nlohmann::json &document)
{
    if (!MeshRenderer::DeserializeDocument(document))
        return false;
    try {
        std::vector<glm::vec3> positions;
        positions.reserve(document["positions"].size());
        for (const auto &entry : document["positions"])
            positions.emplace_back(entry[0].get<float>(), entry[1].get<float>(), entry[2].get<float>());

        m_positions = std::move(positions);
        m_widthMultiplier = document["widthMultiplier"].get<float>();
        std::vector<LineWidthKey> widthCurve;
        for (const auto &entry : document["widthCurve"]["keys"]) {
            widthCurve.push_back({entry["time"].get<float>(), entry["value"].get<float>(),
                                  entry["inTangent"].get<float>(), entry["outTangent"].get<float>()});
        }
        std::vector<LineColorKey> colorGradient;
        for (const auto &entry : document["colorGradient"]["keys"]) {
            const auto &color = entry["color"];
            colorGradient.push_back(
                {entry["time"].get<float>(), glm::vec4(color[0].get<float>(), color[1].get<float>(),
                                                       color[2].get<float>(), color[3].get<float>())});
        }
        m_widthCurve = std::move(widthCurve);
        m_widthCurvePreWrap = static_cast<LineCurveWrapMode>(document["widthCurve"]["preWrap"].get<int>());
        m_widthCurvePostWrap = static_cast<LineCurveWrapMode>(document["widthCurve"]["postWrap"].get<int>());
        m_colorGradient = std::move(colorGradient);
        m_colorGradientMode = static_cast<LineGradientMode>(document["colorGradient"]["mode"].get<int>());
        m_loop = document["loop"].get<bool>();
        m_useWorldSpace = document["useWorldSpace"].get<bool>();
        m_alignment = static_cast<LineAlignment>(document["alignment"].get<int>());
        m_textureMode = static_cast<LineTextureMode>(document["textureMode"].get<int>());
        m_textureScale = glm::vec2(document["textureScale"][0].get<float>(), document["textureScale"][1].get<float>());
        m_numCornerVertices = document["numCornerVertices"].get<uint32_t>();
        m_numCapVertices = document["numCapVertices"].get<uint32_t>();
        m_shadowBias = document["shadowBias"].get<float>();
        m_generateLightingData = document["generateLightingData"].get<bool>();
        SetInlineMeshName("Line");
        UpdateMaximumWidth();
        RebuildMesh();
        return true;
    } catch (const std::exception &error) {
        INXLOG_ERROR("LineRenderer::Deserialize failed: ", error.what());
        return false;
    }
}

std::unique_ptr<Component> LineRenderer::Clone() const
{
    auto clone = std::make_unique<LineRenderer>();
    clone->DeserializeDocument(SerializeDocument());
    return clone;
}

} // namespace infernux
