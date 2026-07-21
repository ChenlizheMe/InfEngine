#pragma once

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vulkan/vulkan.h>
#include <function/renderer/rhi/RhiHandles.h>

namespace infernux
{

class InxVkCoreModular;
namespace particle
{
class ParticleGpuDrawRegistry;
}

namespace vk
{
class VkImageHandle;
}

enum class ScenePickStatus : uint8_t
{
    Pending,
    Completed,
    Failed,
    Cancelled,
    Unknown
};

struct ScenePickSnapshot
{
    uint64_t requestId = 0;
    ScenePickStatus status = ScenePickStatus::Unknown;
    uint64_t objectId = 0;
    std::string error;
};

/// On-demand object-ID rendering for the editor Scene View. No pass or
/// readback work is recorded until a request exists.
class ScenePickingService
{
  public:
    ScenePickingService() = default;
    ~ScenePickingService();

    ScenePickingService(const ScenePickingService &) = delete;
    ScenePickingService &operator=(const ScenePickingService &) = delete;

    void Initialize(InxVkCoreModular *core);
    void SetParticleGpuDrawRegistry(particle::ParticleGpuDrawRegistry *registry) noexcept
    {
        m_particleDrawRegistry = registry;
    }
    void Destroy();

    [[nodiscard]] uint64_t Request(float x, float y, float viewportWidth, float viewportHeight);
    [[nodiscard]] ScenePickSnapshot Query(uint64_t requestId) const;
    [[nodiscard]] bool HasPendingRecord() const noexcept;

    /// Record one picking render and one-pixel copy into the current graphics
    /// command buffer. Completion is published after the frame fence retires.
    void Record(VkCommandBuffer commandBuffer, uint32_t targetWidth, uint32_t targetHeight);

  private:
    struct RequestData
    {
        uint64_t id = 0;
        float x = 0.0f;
        float y = 0.0f;
        float viewportWidth = 1.0f;
        float viewportHeight = 1.0f;
    };

    struct SharedState
    {
        mutable std::mutex mutex;
        std::unordered_map<uint64_t, ScenePickSnapshot> snapshots;
    };

    [[nodiscard]] bool EnsureTarget(uint32_t width, uint32_t height);
    void DestroyTarget();
    void PublishFailure(uint64_t requestId, const std::string &error);

    InxVkCoreModular *m_core = nullptr;
    particle::ParticleGpuDrawRegistry *m_particleDrawRegistry = nullptr;
    std::shared_ptr<SharedState> m_state = std::make_shared<SharedState>();
    RequestData m_pending;
    bool m_hasPending = false;
    uint64_t m_nextRequestId = 1;

    std::unique_ptr<vk::VkImageHandle> m_color;
    std::unique_ptr<vk::VkImageHandle> m_depth;
    VkRenderPass m_renderPass = VK_NULL_HANDLE;
    VkFramebuffer m_framebuffer = VK_NULL_HANDLE;
    uint32_t m_width = 0;
    uint32_t m_height = 0;
    VkFormat m_depthFormat = VK_FORMAT_UNDEFINED;
    rhi::RenderTargetLayoutHandle m_renderTargetLayout;
};

} // namespace infernux
