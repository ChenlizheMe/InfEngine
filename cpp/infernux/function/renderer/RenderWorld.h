#pragma once

#include <function/renderer/Frustum.h>
#include <function/renderer/InxRenderStruct.h>
#include <function/renderer/RenderIdentity.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

namespace infernux
{

class SceneRenderExtractor;

/// Immutable structural values copied from a scene renderer at extraction.
/// No Scene, GameObject, Component, or Transform pointer may cross this boundary.
struct RenderProxyStructuralData
{
    uint64_t objectId = 0;
    uint32_t layerMask = 1u;
    bool isStatic = false;
    RenderProxyHandle identity;
    std::shared_ptr<InxMaterial> renderMaterial;
    int32_t renderQueue = 2000;
    uintptr_t materialSortKey = 0;
};

struct RenderProxyFrameData
{
    glm::mat4 worldMatrix{1.0f};
    AABB worldBounds;
    bool visible = true;
};

struct RenderProxyCacheData
{
    size_t drawCallStart = 0;
    size_t drawCallCount = 0;
};

struct RenderProxy
{
    RenderProxyStructuralData structural;
    RenderProxyFrameData frame;
    RenderProxyCacheData cache;
};

struct RenderViewData
{
    glm::mat4 view{1.0f};
    glm::mat4 projection{1.0f};
    glm::mat4 viewProjection{1.0f};
    glm::vec3 position{0.0f, 0.0f, 5.0f};
    glm::vec3 forward{0.0f, 0.0f, 1.0f};
    glm::vec3 up{0.0f, 1.0f, 0.0f};
    uint32_t cullingMask = 0xFFFFFFFFu;
    uint64_t cameraId = 0;
    bool valid = false;
};

/// One complete renderer-frontend publication. The object is writable only
/// while owned by RenderWorldSnapshot::BeginFrame(); Publish() transfers it as
/// shared_ptr<const RenderWorldFrame>, so render consumers cannot mutate it.
class RenderWorldFrame final
{
  public:
    [[nodiscard]] const std::vector<RenderProxy> &Proxies() const noexcept
    {
        return m_proxies;
    }

    [[nodiscard]] const DrawCallResult &DrawCalls() const noexcept
    {
        return m_drawCalls;
    }

    [[nodiscard]] const RenderViewData &PrimaryView() const noexcept
    {
        return m_primaryView;
    }

    [[nodiscard]] uint64_t WorldId() const noexcept
    {
        return m_worldId;
    }

    [[nodiscard]] uint64_t StructuralRevision() const noexcept
    {
        return m_structuralRevision;
    }

    [[nodiscard]] uint64_t TransformRevision() const noexcept
    {
        return m_transformRevision;
    }

    [[nodiscard]] uint64_t ContentRevision() const noexcept
    {
        return m_contentRevision;
    }

    [[nodiscard]] uint64_t FrameRevision() const noexcept
    {
        return m_frameRevision;
    }

    [[nodiscard]] bool MatchesSource(uint64_t worldId, uint64_t structuralRevision) const noexcept
    {
        return m_worldId == worldId && m_structuralRevision == structuralRevision;
    }

  private:
    friend class RenderWorldSnapshot;
    friend class SceneRenderExtractor;

    std::vector<RenderProxy> m_proxies;
    DrawCallResult m_drawCalls;
    RenderViewData m_primaryView;
    uint64_t m_worldId = 0;
    uint64_t m_structuralRevision = 0;
    uint64_t m_transformRevision = 0;
    uint64_t m_contentRevision = 0;
    uint64_t m_frameRevision = 0;
};

/// Atomic immutable publication boundary between scene extraction and render
/// consumption. Older frames remain valid while a consumer owns an acquired
/// shared pointer. Reusable frames are recycled only after all consumers drop
/// them, avoiding both frame-sized copies and mutation of published state.
class RenderWorldSnapshot final
{
  public:
    RenderWorldSnapshot() = default;
    RenderWorldSnapshot(const RenderWorldSnapshot &) = delete;
    RenderWorldSnapshot &operator=(const RenderWorldSnapshot &) = delete;

    [[nodiscard]] std::shared_ptr<const RenderWorldFrame> Acquire() const noexcept
    {
        return std::atomic_load_explicit(&m_published, std::memory_order_acquire);
    }

    void Clear()
    {
        RenderWorldFrame &frame = BeginFrame(0, 0);
        frame.m_proxies.clear();
        frame.m_drawCalls = {};
        frame.m_primaryView = {};
        frame.m_worldId = 0;
        frame.m_structuralRevision = 0;
        frame.m_transformRevision = 0;
        frame.m_contentRevision = 0;
        Publish();
    }

    [[nodiscard]] uint64_t WorldId() const noexcept
    {
        const auto frame = Acquire();
        return frame ? frame->WorldId() : 0;
    }

    [[nodiscard]] uint64_t StructuralRevision() const noexcept
    {
        const auto frame = Acquire();
        return frame ? frame->StructuralRevision() : 0;
    }

    [[nodiscard]] uint64_t ContentRevision() const noexcept
    {
        const auto frame = Acquire();
        return frame ? frame->ContentRevision() : 0;
    }

    [[nodiscard]] uint64_t FrameRevision() const noexcept
    {
        const auto frame = Acquire();
        return frame ? frame->FrameRevision() : m_frameRevision;
    }

    [[nodiscard]] bool IsPublished() const noexcept
    {
        return static_cast<bool>(Acquire());
    }

    [[nodiscard]] bool MatchesSource(uint64_t worldId, uint64_t structuralRevision) const noexcept
    {
        const auto frame = Acquire();
        return frame && frame->MatchesSource(worldId, structuralRevision);
    }

  private:
    friend class SceneRenderExtractor;

    [[nodiscard]] RenderWorldFrame &BeginFrame(uint64_t, uint64_t)
    {
        // A frame in the recycle list is writable only when this publisher is
        // its sole owner. Consumers may retain any older publication freely.
        for (auto it = m_recycled.begin(); it != m_recycled.end(); ++it) {
            if (it->use_count() != 1)
                continue;
            m_writable = std::move(*it);
            m_recycled.erase(it);
            break;
        }
        if (!m_writable)
            m_writable = std::make_shared<RenderWorldFrame>();

        m_writable->m_frameRevision = ++m_frameRevision;
        return *m_writable;
    }

    [[nodiscard]] RenderWorldFrame &WritableFrame() noexcept
    {
        return *m_writable;
    }

    void Publish()
    {
        if (!m_writable)
            return;
        std::shared_ptr<const RenderWorldFrame> next = std::move(m_writable);
        auto previous = std::atomic_exchange_explicit(&m_published, std::move(next), std::memory_order_acq_rel);
        if (previous)
            m_recycled.push_back(std::const_pointer_cast<RenderWorldFrame>(std::move(previous)));

        // Do not retain an unbounded number of frames when a slow consumer
        // intentionally keeps historical publications alive.
        if (m_recycled.size() > 8) {
            m_recycled.erase(m_recycled.begin(), m_recycled.begin() + static_cast<ptrdiff_t>(m_recycled.size() - 8));
        }
    }

    mutable std::shared_ptr<const RenderWorldFrame> m_published;
    std::shared_ptr<RenderWorldFrame> m_writable;
    std::vector<std::shared_ptr<RenderWorldFrame>> m_recycled;
    uint64_t m_frameRevision = 0;
};

} // namespace infernux
