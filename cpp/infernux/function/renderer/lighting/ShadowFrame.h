#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <optional>
#include <tuple>
#include <utility>
#include <vector>

#include <glm/glm.hpp>
#include <glm/gtc/constants.hpp>
#include <glm/gtc/matrix_transform.hpp>

namespace infernux::lighting
{

constexpr uint32_t MaxShadowViews = 64;
constexpr uint32_t DirectionalCascadeCount = 4;

struct ShadowDepthRange
{
    float nearDepth = 0.0f;
    float farDepth = 0.0f;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return std::isfinite(nearDepth) && std::isfinite(farDepth) && nearDepth > 0.0f && farDepth > nearDepth;
    }
};

enum class ShadowViewType : uint32_t
{
    DirectionalCascade,
    Spot,
    PointFace,
    AreaFace,
};

struct ShadowAtlasRect
{
    uint32_t x = 0;
    uint32_t y = 0;
    uint32_t size = 0;
    uint32_t guard = 0;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return size > guard * 2u;
    }
    [[nodiscard]] uint32_t InnerSize() const noexcept
    {
        return IsValid() ? size - guard * 2u : 0u;
    }
    [[nodiscard]] glm::vec4 ScaleOffset(uint32_t atlasSize) const noexcept
    {
        if (!IsValid() || atlasSize == 0)
            return {};
        const float inverseAtlas = 1.0f / static_cast<float>(atlasSize);
        return {static_cast<float>(InnerSize()) * inverseAtlas, static_cast<float>(InnerSize()) * inverseAtlas,
                static_cast<float>(x + guard) * inverseAtlas, static_cast<float>(y + guard) * inverseAtlas};
    }
};

class ShadowAtlasAllocator
{
  public:
    explicit ShadowAtlasAllocator(uint32_t atlasSize) : m_atlasSize(atlasSize)
    {
        if (atlasSize > 0)
            m_free.push_back({0, 0, atlasSize, atlasSize});
    }

    [[nodiscard]] std::optional<ShadowAtlasRect> Allocate(uint32_t requestedInnerSize, uint32_t guard = 2)
    {
        if (requestedInnerSize == 0 || m_free.empty())
            return std::nullopt;
        if (requestedInnerSize > m_atlasSize)
            return std::nullopt;
        const uint32_t allocationSize = requestedInnerSize;
        if (allocationSize <= guard * 2u)
            return std::nullopt;

        auto best = m_free.end();
        for (auto it = m_free.begin(); it != m_free.end(); ++it) {
            if (it->width < allocationSize || it->height < allocationSize)
                continue;
            const uint64_t area = static_cast<uint64_t>(it->width) * it->height;
            const uint64_t bestArea = best == m_free.end() ? std::numeric_limits<uint64_t>::max()
                                                           : static_cast<uint64_t>(best->width) * best->height;
            if (area < bestArea || (area == bestArea && std::tie(it->y, it->x) < std::tie(best->y, best->x))) {
                best = it;
            }
        }
        if (best == m_free.end())
            return std::nullopt;

        const FreeRect free = *best;
        m_free.erase(best);
        if (free.width > allocationSize)
            m_free.push_back({free.x + allocationSize, free.y, free.width - allocationSize, allocationSize});
        if (free.height > allocationSize)
            m_free.push_back({free.x, free.y + allocationSize, free.width, free.height - allocationSize});
        return ShadowAtlasRect{free.x, free.y, allocationSize, guard};
    }

    template <size_t Count>
    [[nodiscard]] std::optional<std::array<ShadowAtlasRect, Count>>
    AllocateBatch(const std::array<uint32_t, Count> &requestedInnerSizes, uint32_t guard = 2)
    {
        ShadowAtlasAllocator staged = *this;
        std::array<ShadowAtlasRect, Count> allocations{};
        for (size_t index = 0; index < Count; ++index) {
            const auto allocation = staged.Allocate(requestedInnerSizes[index], guard);
            if (!allocation)
                return std::nullopt;
            allocations[index] = *allocation;
        }
        *this = std::move(staged);
        return allocations;
    }

    template <size_t Count>
    [[nodiscard]] std::optional<std::array<ShadowAtlasRect, Count>>
    AllocateBatchWithFallback(const std::array<uint32_t, Count> &preferredSizes, uint32_t minimumSize,
                              uint32_t guard = 2)
    {
        auto candidateSizes = preferredSizes;
        for (;;) {
            if (const auto allocation = AllocateBatch(candidateSizes, guard))
                return allocation;

            bool reduced = false;
            for (uint32_t &size : candidateSizes) {
                if (size <= minimumSize)
                    continue;
                size = std::max(size / 2u, minimumSize);
                reduced = true;
            }
            if (!reduced)
                return std::nullopt;
        }
    }

    [[nodiscard]] uint32_t AtlasSize() const noexcept
    {
        return m_atlasSize;
    }

  private:
    struct FreeRect
    {
        uint32_t x = 0;
        uint32_t y = 0;
        uint32_t width = 0;
        uint32_t height = 0;
    };

    uint32_t m_atlasSize = 0;
    std::vector<FreeRect> m_free;
};

struct ShadowView
{
    uint64_t lightId = 0;
    ShadowViewType type = ShadowViewType::DirectionalCascade;
    uint32_t subView = 0;
    glm::mat4 viewProjection{1.0f};
    ShadowAtlasRect atlas;
    float nearPlane = 0.1f;
    float farPlane = 1.0f;
    float worldUnitsPerTexel = 1.0f;
    float filterRadiusTexels = 1.5f;
    float splitNear = 0.0f;
    float splitFar = 0.0f;
    // Directional views store a normalized direction toward the light. Local
    // views store the light position and resolve the direction per vertex.
    glm::vec3 lightVector{0.0f, 1.0f, 0.0f};
    bool lightVectorIsPosition = false;
    float depthBiasTexels = 0.0f;
    float normalBiasTexels = 0.0f;
    // Light-space basis of this view (world space). Camera-facing effects
    // (particle billboards) reuse it to face the light in the caster pass.
    glm::vec3 viewRight{1.0f, 0.0f, 0.0f};
    glm::vec3 viewUp{0.0f, 1.0f, 0.0f};
    uint32_t cullingMask = 0xffffffffu;
};

struct ShadowLightAssignment
{
    uint64_t lightId = 0;
    uint32_t firstView = 0;
    uint32_t viewCount = 0;
};

struct ShadowFrame
{
    uint32_t atlasSize = 0;
    std::vector<ShadowView> views;
    std::vector<ShadowLightAssignment> assignments;

    [[nodiscard]] const ShadowLightAssignment *Find(uint64_t lightId) const noexcept
    {
        const auto found = std::find_if(assignments.begin(), assignments.end(),
                                        [lightId](const auto &assignment) { return assignment.lightId == lightId; });
        return found == assignments.end() ? nullptr : &*found;
    }
};

struct ShadowCamera
{
    glm::vec3 position{0.0f};
    glm::vec3 forward{0.0f, 0.0f, -1.0f};
    glm::vec3 right{1.0f, 0.0f, 0.0f};
    glm::vec3 up{0.0f, 1.0f, 0.0f};
    float nearClip = 0.1f;
    float farClip = 1000.0f;
    float verticalFovRadians = glm::radians(60.0f);
    float aspect = 16.0f / 9.0f;
    bool orthographic = false;
    float orthographicHalfHeight = 5.0f;
};

[[nodiscard]] inline std::array<float, 5> PracticalCascadeSplits(float nearClip, float farClip, float lambda)
{
    nearClip = std::max(nearClip, 0.001f);
    farClip = std::max(farClip, nearClip + 0.001f);
    lambda = glm::clamp(lambda, 0.0f, 1.0f);
    std::array<float, 5> splits{};
    splits[0] = nearClip;
    for (uint32_t index = 1; index < splits.size(); ++index) {
        const float ratio = static_cast<float>(index) / static_cast<float>(splits.size() - 1u);
        const float logarithmic = nearClip * std::pow(farClip / nearClip, ratio);
        const float uniform = nearClip + (farClip - nearClip) * ratio;
        splits[index] = glm::mix(uniform, logarithmic, lambda);
    }
    splits.back() = farClip;
    return splits;
}

/// Longest camera-space distance four cascades are allowed to span for a
/// scene whose visible content starts near the camera. Beyond this the lit
/// pass fades shadows out instead of stretching the last cascade into
/// meter-sized texels. Scenes viewed from afar scale the cap with the
/// nearest visible receiver so distant-only content keeps its shadows.
constexpr float MaxStableShadowDistance = 200.0f;

[[nodiscard]] inline std::array<float, 5> AdaptiveCascadeSplits(float nearClip, const ShadowDepthRange &visibleRange,
                                                                float cameraFarClip)
{
    nearClip = std::max(nearClip, 0.001f);
    cameraFarClip = std::max(cameraFarClip, nearClip + 0.001f);
    const float visibleNear =
        visibleRange.IsValid() ? glm::clamp(visibleRange.nearDepth, nearClip, cameraFarClip) : nearClip;
    const float visibleFar =
        visibleRange.IsValid() ? glm::clamp(visibleRange.farDepth, visibleNear, cameraFarClip) : cameraFarClip;

    // 10% headroom keeps the far-fade band of the last cascade beyond the
    // farthest visible receiver when the range fits the scene tightly.
    const float distanceCap = std::max(MaxStableShadowDistance, visibleNear * 4.0f);
    const float shadowFar = glm::clamp(visibleFar * 1.1f, nearClip + 0.01f, std::min(distanceCap, cameraFarClip));
    // Anchor the first cascade near the closest visible receiver instead of
    // the camera near plane. When the camera looks at a distant band of
    // content (empty foreground), starting at the near plane would spend the
    // dense cascades on empty meters. The 0.5 factor keeps margin for content
    // that moves toward the camera between two frames.
    const float shadowNear = glm::clamp(std::max(nearClip, visibleNear * 0.5f), nearClip, shadowFar * 0.5f);
    const float distributionNear = glm::clamp(visibleNear, shadowNear, shadowFar * 0.8f);

    // Practical split scheme: a logarithmic distribution concentrates
    // resolution near the camera, the uniform term bounds how much of the
    // range the far cascades have to swallow. The blend adapts to the range
    // ratio: a shallow scene (e.g. 15 m of visible depth) distributes almost
    // uniformly, because a log split would spend the two half-atlas cascades
    // on millimeter texels in the first few meters while the far cascades —
    // where most receivers actually sit — blur out. Deep ranges keep the
    // log-heavy blend so close-up shadows stay dense.
    const float rangeRatio = shadowFar / std::max(distributionNear, 0.001f);
    const float lambda = glm::clamp(0.35f + 0.13f * (std::log2(rangeRatio) - 3.0f), 0.35f, 0.75f);
    std::array<float, 5> splits{};
    splits[0] = shadowNear;
    for (uint32_t index = 1; index < DirectionalCascadeCount; ++index) {
        const float ratio = static_cast<float>(index) / static_cast<float>(DirectionalCascadeCount);
        const float logarithmic = distributionNear * std::pow(shadowFar / distributionNear, ratio);
        const float uniform = distributionNear + (shadowFar - distributionNear) * ratio;
        splits[index] = std::max(glm::mix(uniform, logarithmic, lambda), splits[index - 1] + 0.001f);
    }
    splits.back() = std::max(shadowFar, splits[DirectionalCascadeCount - 1] + 0.001f);
    return splits;
}

[[nodiscard]] inline std::array<glm::vec3, 8> FrustumSliceCorners(const ShadowCamera &camera, float sliceNear,
                                                                  float sliceFar)
{
    const float tangent = std::tan(camera.verticalFovRadians * 0.5f);
    const float nearHalfHeight = camera.orthographic ? camera.orthographicHalfHeight : tangent * sliceNear;
    const float nearHalfWidth = nearHalfHeight * camera.aspect;
    const float farHalfHeight = camera.orthographic ? camera.orthographicHalfHeight : tangent * sliceFar;
    const float farHalfWidth = farHalfHeight * camera.aspect;
    const glm::vec3 nearCenter = camera.position + camera.forward * sliceNear;
    const glm::vec3 farCenter = camera.position + camera.forward * sliceFar;
    return {nearCenter - camera.right * nearHalfWidth - camera.up * nearHalfHeight,
            nearCenter + camera.right * nearHalfWidth - camera.up * nearHalfHeight,
            nearCenter + camera.right * nearHalfWidth + camera.up * nearHalfHeight,
            nearCenter - camera.right * nearHalfWidth + camera.up * nearHalfHeight,
            farCenter - camera.right * farHalfWidth - camera.up * farHalfHeight,
            farCenter + camera.right * farHalfWidth - camera.up * farHalfHeight,
            farCenter + camera.right * farHalfWidth + camera.up * farHalfHeight,
            farCenter - camera.right * farHalfWidth + camera.up * farHalfHeight};
}

[[nodiscard]] inline ShadowView BuildStableDirectionalCascade(uint64_t lightId, uint32_t cascade,
                                                              const std::array<glm::vec3, 8> &corners,
                                                              glm::vec3 rayDirection, float splitNear, float splitFar,
                                                              const ShadowAtlasRect &atlas)
{
    glm::vec3 center(0.0f);
    for (const glm::vec3 &corner : corners)
        center += corner;
    center /= static_cast<float>(corners.size());

    float radius = 0.0f;
    for (const glm::vec3 &corner : corners)
        radius = std::max(radius, glm::length(corner - center));
    radius = std::ceil(std::max(radius, 0.5f) * 16.0f) / 16.0f;

    rayDirection =
        glm::dot(rayDirection, rayDirection) > 0.0f ? glm::normalize(rayDirection) : glm::vec3(0.0f, -1.0f, 0.0f);
    glm::vec3 up(0.0f, 1.0f, 0.0f);
    if (std::abs(glm::dot(rayDirection, up)) > 0.99f)
        up = {0.0f, 0.0f, 1.0f};

    const float casterMargin = std::max(radius, 20.0f);
    const float eyeDistance = radius + casterMargin;
    const uint32_t innerSize = std::max(atlas.InnerSize(), 1u);
    const float worldUnitsPerTexel = (radius * 2.0f) / static_cast<float>(innerSize);

    // Quantize in an origin-anchored light coordinate system. A look-at view
    // centered on the cascade always transforms its own center to (0, 0), so
    // snapping that value is a no-op and leaves the cascade shimmering.
    const glm::mat4 lightRotation = glm::lookAtRH(glm::vec3(0.0f), rayDirection, up);
    glm::vec3 centerInLight = glm::vec3(lightRotation * glm::vec4(center, 1.0f));
    centerInLight.x = std::round(centerInLight.x / worldUnitsPerTexel) * worldUnitsPerTexel;
    centerInLight.y = std::round(centerInLight.y / worldUnitsPerTexel) * worldUnitsPerTexel;
    const glm::vec3 snappedWorldCenter = glm::vec3(glm::inverse(lightRotation) * glm::vec4(centerInLight, 1.0f));
    const glm::mat4 view = glm::lookAtRH(snappedWorldCenter - rayDirection * eyeDistance, snappedWorldCenter, up);

    const float nearPlane = 0.1f;
    const float farPlane = eyeDistance + radius + casterMargin;
    const glm::mat4 projection = glm::orthoRH_ZO(-radius, radius, -radius, radius, nearPlane, farPlane);
    ShadowView result{};
    result.lightId = lightId;
    result.type = ShadowViewType::DirectionalCascade;
    result.subView = cascade;
    result.viewProjection = projection * view;
    result.atlas = atlas;
    result.nearPlane = nearPlane;
    result.farPlane = farPlane;
    result.worldUnitsPerTexel = worldUnitsPerTexel;
    result.splitNear = splitNear;
    result.splitFar = splitFar;
    result.lightVector = -rayDirection;
    result.viewRight = glm::vec3(view[0][0], view[1][0], view[2][0]);
    result.viewUp = glm::vec3(view[0][1], view[1][1], view[2][1]);
    return result;
}

[[nodiscard]] inline ShadowView BuildStableDirectionalCascade(uint64_t lightId, uint32_t cascade,
                                                              const ShadowCamera &camera, glm::vec3 rayDirection,
                                                              float splitNear, float splitFar,
                                                              const ShadowAtlasRect &atlas)
{
    return BuildStableDirectionalCascade(lightId, cascade, FrustumSliceCorners(camera, splitNear, splitFar),
                                         rayDirection, splitNear, splitFar, atlas);
}

[[nodiscard]] inline ShadowView BuildSpotShadowView(uint64_t lightId, glm::vec3 position, glm::vec3 rayDirection,
                                                    float outerAngleDegrees, float range, const ShadowAtlasRect &atlas)
{
    rayDirection =
        glm::dot(rayDirection, rayDirection) > 0.0f ? glm::normalize(rayDirection) : glm::vec3(0.0f, 0.0f, -1.0f);
    glm::vec3 up(0.0f, 1.0f, 0.0f);
    if (std::abs(glm::dot(rayDirection, up)) > 0.99f)
        up = {0.0f, 0.0f, 1.0f};
    const float nearPlane = std::max(0.01f, range * 0.001f);
    const float farPlane = std::max(range, nearPlane + 0.01f);
    const float fov = glm::radians(glm::clamp(outerAngleDegrees, 1.0f, 179.0f));
    const glm::mat4 view = glm::lookAtRH(position, position + rayDirection, up);
    const glm::mat4 projection = glm::perspectiveRH_ZO(fov, 1.0f, nearPlane, farPlane);
    const float footprint = 2.0f * farPlane * std::tan(fov * 0.5f);
    const float worldUnitsPerTexel = footprint / static_cast<float>(std::max(atlas.InnerSize(), 1u));
    ShadowView result{};
    result.lightId = lightId;
    result.type = ShadowViewType::Spot;
    result.viewProjection = projection * view;
    result.atlas = atlas;
    result.nearPlane = nearPlane;
    result.farPlane = farPlane;
    result.worldUnitsPerTexel = worldUnitsPerTexel;
    result.lightVector = position;
    result.lightVectorIsPosition = true;
    result.viewRight = glm::vec3(view[0][0], view[1][0], view[2][0]);
    result.viewUp = glm::vec3(view[0][1], view[1][1], view[2][1]);
    return result;
}

[[nodiscard]] inline std::array<ShadowView, 6> BuildPointShadowViews(uint64_t lightId, glm::vec3 position, float range,
                                                                     const std::array<ShadowAtlasRect, 6> &atlas,
                                                                     ShadowViewType type = ShadowViewType::PointFace)
{
    constexpr std::array<glm::vec3, 6> directions = {glm::vec3(1, 0, 0),  glm::vec3(-1, 0, 0), glm::vec3(0, 1, 0),
                                                     glm::vec3(0, -1, 0), glm::vec3(0, 0, 1),  glm::vec3(0, 0, -1)};
    constexpr std::array<glm::vec3, 6> ups = {glm::vec3(0, -1, 0), glm::vec3(0, -1, 0), glm::vec3(0, 0, 1),
                                              glm::vec3(0, 0, -1), glm::vec3(0, -1, 0), glm::vec3(0, -1, 0)};
    const float nearPlane = std::max(0.01f, range * 0.001f);
    const float farPlane = std::max(range, nearPlane + 0.01f);
    const glm::mat4 projection = glm::perspectiveRH_ZO(glm::half_pi<float>(), 1.0f, nearPlane, farPlane);
    std::array<ShadowView, 6> views{};
    for (uint32_t face = 0; face < views.size(); ++face) {
        const glm::mat4 view = glm::lookAtRH(position, position + directions[face], ups[face]);
        const float worldUnitsPerTexel = (farPlane * 2.0f) / static_cast<float>(std::max(atlas[face].InnerSize(), 1u));
        ShadowView &result = views[face];
        result.lightId = lightId;
        result.type = type;
        result.subView = face;
        result.viewProjection = projection * view;
        result.atlas = atlas[face];
        result.nearPlane = nearPlane;
        result.farPlane = farPlane;
        result.worldUnitsPerTexel = worldUnitsPerTexel;
        result.lightVector = position;
        result.lightVectorIsPosition = true;
        result.viewRight = glm::vec3(view[0][0], view[1][0], view[2][0]);
        result.viewUp = glm::vec3(view[0][1], view[1][1], view[2][1]);
    }
    return views;
}

} // namespace infernux::lighting
