#pragma once

#include <function/renderer/Frustum.h>
#include <function/renderer/RenderIdentity.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/MeshRenderer.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

namespace infernux
{

class SkinnedMeshRenderer;
class Transform;
class SceneRenderer;

/// Extracted rendering state for one scene renderer.
struct RenderProxy
{
    uint64_t objectId = 0;
    RenderProxyHandle identity;
    glm::mat4 worldMatrix{1.0f};
    MeshRef mesh;
    std::shared_ptr<InxMaterial> renderMaterial;
    InxMaterial *renderMaterialRaw = nullptr;
    MeshRenderer *meshRenderer = nullptr;
    Transform *transform = nullptr;
    SkinnedMeshRenderer *skinnedRenderer = nullptr;
    AABB worldBounds;
    size_t drawCallStart = 0;
    size_t drawCallCount = 0;
    bool visible = true;
};

/// Scene extraction state shared by all camera culling operations in a frame.
///
/// BeginFrame opens a new write revision. Publish marks extraction, transform
/// refresh, and primary-camera culling complete. The current renderer is still
/// single-threaded; the published flag establishes the boundary needed by a
/// later immutable/render-thread snapshot without changing frame behaviour.
class RenderWorldSnapshot
{
  public:
    void BeginFrame(uint64_t worldId, uint64_t structuralRevision) noexcept
    {
        ++m_frameRevision;
        m_worldId = worldId;
        m_structuralRevision = structuralRevision;
        m_published = false;
    }

    void Publish() noexcept
    {
        m_published = true;
    }

    void Clear() noexcept
    {
        m_proxies.clear();
        ++m_frameRevision;
        m_worldId = 0;
        m_structuralRevision = 0;
        m_published = true;
    }

    [[nodiscard]] const std::vector<RenderProxy> &Proxies() const noexcept
    {
        return m_proxies;
    }

    [[nodiscard]] uint64_t WorldId() const noexcept
    {
        return m_worldId;
    }

    [[nodiscard]] uint64_t StructuralRevision() const noexcept
    {
        return m_structuralRevision;
    }

    [[nodiscard]] uint64_t FrameRevision() const noexcept
    {
        return m_frameRevision;
    }

    [[nodiscard]] bool IsPublished() const noexcept
    {
        return m_published;
    }

    [[nodiscard]] bool MatchesSource(uint64_t worldId, uint64_t structuralRevision) const noexcept
    {
        return m_published && m_worldId == worldId && m_structuralRevision == structuralRevision;
    }

  private:
    friend class SceneRenderer;

    [[nodiscard]] std::vector<RenderProxy> &MutableProxies() noexcept
    {
        return m_proxies;
    }

    std::vector<RenderProxy> m_proxies;
    uint64_t m_worldId = 0;
    uint64_t m_structuralRevision = 0;
    uint64_t m_frameRevision = 0;
    bool m_published = false;
};

} // namespace infernux
