#include "LineRenderer.h"
#include "ComponentFactory.h"
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

float RequireNonNegative(float value, const char *name)
{
    if (!std::isfinite(value) || value < 0.0f)
        throw std::invalid_argument(std::string(name) + " must be finite and non-negative");
    return value;
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
    RebuildMesh();
}

void LineRenderer::SetPositionCount(size_t count)
{
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
    m_positions[index] = position;
    RebuildMesh();
}

void LineRenderer::SetPositions(const std::vector<glm::vec3> &positions)
{
    for (const auto &position : positions)
        RequireFinite(position, "LineRenderer position");
    m_positions = positions;
    RebuildMesh();
}

void LineRenderer::SetStartWidth(float width)
{
    m_startWidth = RequireNonNegative(width, "LineRenderer start width");
    RebuildMesh();
}

void LineRenderer::SetEndWidth(float width)
{
    m_endWidth = RequireNonNegative(width, "LineRenderer end width");
    RebuildMesh();
}

void LineRenderer::SetWidthMultiplier(float multiplier)
{
    m_widthMultiplier = RequireNonNegative(multiplier, "LineRenderer width multiplier");
    RebuildMesh();
}

void LineRenderer::SetStartColor(const glm::vec4 &color)
{
    RequireFinite(color, "LineRenderer start color");
    m_startColor = color;
    RebuildMesh();
}

void LineRenderer::SetEndColor(const glm::vec4 &color)
{
    RequireFinite(color, "LineRenderer end color");
    m_endColor = color;
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
    m_alignment = alignment;
    RebuildMesh();
}

void LineRenderer::SetTextureMode(LineTextureMode mode)
{
    m_textureMode = mode;
    RebuildMesh();
}

void LineRenderer::SetTextureScale(float scale)
{
    m_textureScale = RequireNonNegative(scale, "LineRenderer texture scale");
    RebuildMesh();
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

void LineRenderer::ComputeWorldBounds(const glm::mat4 &worldMatrix, glm::vec3 &outMin, glm::vec3 &outMax) const
{
    if (m_positions.empty()) {
        outMin = outMax = glm::vec3(ResolveRenderWorldMatrix(worldMatrix)[3]);
        return;
    }
    MeshRenderer::ComputeWorldBounds(ResolveRenderWorldMatrix(worldMatrix), outMin, outMax);
    const float radius = 0.5f * std::max(m_startWidth, m_endWidth) * m_widthMultiplier;
    outMin -= glm::vec3(radius);
    outMax += glm::vec3(radius);
}

void LineRenderer::RebuildMesh()
{
    std::vector<glm::vec3> samples = m_positions;
    if (m_loop && samples.size() > 2)
        samples.push_back(samples.front());

    std::vector<Vertex> vertices;
    std::vector<uint32_t> indices;
    if (samples.size() < 2) {
        SetProceduralMesh(std::move(vertices), std::move(indices));
        return;
    }

    std::vector<float> distances(samples.size(), 0.0f);
    for (size_t index = 1; index < samples.size(); ++index)
        distances[index] = distances[index - 1] + glm::length(samples[index] - samples[index - 1]);
    const float totalLength = distances.back();

    vertices.reserve(samples.size() * 2);
    indices.reserve((samples.size() - 1) * 6);
    for (size_t index = 0; index < samples.size(); ++index) {
        glm::vec3 tangent;
        if (index == 0)
            tangent = samples[1] - samples[0];
        else if (index + 1 == samples.size())
            tangent = samples[index] - samples[index - 1];
        else
            tangent = samples[index + 1] - samples[index - 1];
        if (glm::dot(tangent, tangent) <= DIRECTION_EPSILON * DIRECTION_EPSILON)
            tangent = glm::vec3(1.0f, 0.0f, 0.0f);
        else
            tangent = glm::normalize(tangent);

        const float t = totalLength > DIRECTION_EPSILON ? distances[index] / totalLength
                                                        : static_cast<float>(index) / (samples.size() - 1);
        const float halfWidth = 0.5f * glm::mix(m_startWidth, m_endWidth, t) * m_widthMultiplier;
        const glm::vec4 color = glm::mix(m_startColor, m_endColor, t);
        const float u = m_textureMode == LineTextureMode::Tile ? distances[index] * m_textureScale : t * m_textureScale;

        for (uint32_t side = 0; side < 2; ++side) {
            Vertex vertex;
            vertex.pos = samples[index];
            vertex.normal = glm::vec3(0.0f, 0.0f, 1.0f);
            vertex.tangent = glm::vec4(tangent, 1.0f);
            vertex.color = glm::vec3(color);
            vertex.texCoord = glm::vec2(u, static_cast<float>(side));
            vertex.boneIndices = glm::uvec4(0u, 0u, static_cast<uint32_t>(m_alignment), LINE_VERTEX_MARKER);
            vertex.boneWeights = glm::vec4(side == 0 ? -halfWidth : halfWidth, color.a, 0.0f, 0.0f);
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
    document["meshId"] = 0;
    document["useInlineMesh"] = false;
    document.erase("meshAssetGuid");
    document.erase("inlineMeshName");
    document.erase("inlineMeshBuiltin");
    document.erase("inlineVertices");
    document.erase("inlineIndices");

    document["positions"] = json::array();
    for (const auto &position : m_positions)
        document["positions"].push_back({position.x, position.y, position.z});
    document["startWidth"] = m_startWidth;
    document["endWidth"] = m_endWidth;
    document["widthMultiplier"] = m_widthMultiplier;
    document["startColor"] = {m_startColor.r, m_startColor.g, m_startColor.b, m_startColor.a};
    document["endColor"] = {m_endColor.r, m_endColor.g, m_endColor.b, m_endColor.a};
    document["loop"] = m_loop;
    document["useWorldSpace"] = m_useWorldSpace;
    document["alignment"] = static_cast<int>(m_alignment);
    document["textureMode"] = static_cast<int>(m_textureMode);
    document["textureScale"] = m_textureScale;
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
        m_startWidth = document["startWidth"].get<float>();
        m_endWidth = document["endWidth"].get<float>();
        m_widthMultiplier = document["widthMultiplier"].get<float>();
        m_startColor = glm::vec4(document["startColor"][0].get<float>(), document["startColor"][1].get<float>(),
                                 document["startColor"][2].get<float>(), document["startColor"][3].get<float>());
        m_endColor = glm::vec4(document["endColor"][0].get<float>(), document["endColor"][1].get<float>(),
                               document["endColor"][2].get<float>(), document["endColor"][3].get<float>());
        m_loop = document["loop"].get<bool>();
        m_useWorldSpace = document["useWorldSpace"].get<bool>();
        m_alignment = static_cast<LineAlignment>(document["alignment"].get<int>());
        m_textureMode = static_cast<LineTextureMode>(document["textureMode"].get<int>());
        m_textureScale = document["textureScale"].get<float>();
        SetInlineMeshName("Line");
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
