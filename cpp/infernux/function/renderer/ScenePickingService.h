#pragma once

#include <cstdint>
#include <function/renderer/ScenePickingTypes.h>
#include <function/renderer/rhi/RhiHandles.h>
#include <function/renderer/vk/RhiVulkanTypes.h>
#include <glm/glm.hpp>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vulkan/vulkan.h>

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

/// On-demand object-ID rendering for the editor Scene View. No pass or
/// readback work is recorded until a request exists.
class ScenePickingService
{
  public:
    ScenePickingService();
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
    void Record(VkCommandBuffer commandBuffer, uint32_t targetWidth, uint32_t targetHeight,
                rhi::BindGroupHandle perViewGroup, const glm::mat4 &viewMatrix);

  private:
    struct TargetGeneration;

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
    rhi::DynamicRenderingCommands m_dynamicRenderingCommands;
    rhi::Synchronization2Commands m_synchronization2Commands;
    particle::ParticleGpuDrawRegistry *m_particleDrawRegistry = nullptr;
    std::shared_ptr<SharedState> m_state = std::make_shared<SharedState>();
    RequestData m_pending;
    bool m_hasPending = false;
    uint64_t m_nextRequestId = 1;

    std::unique_ptr<TargetGeneration> m_target;
};

} // namespace infernux
