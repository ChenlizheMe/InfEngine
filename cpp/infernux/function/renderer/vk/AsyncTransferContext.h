/**
 * @file AsyncTransferContext.h
 * @brief Pooled, optionally-async upload pipeline backed by a dedicated
 *        transfer queue when the device exposes one.
 *
 * Designed as a thin successor to VkResourceManager::BeginSingleTimeCommands
 * / EndSingleTimeCommands. Where the latter always submits on the graphics
 * queue (and therefore steals throughput from the renderer), this context
 * routes work to the dedicated DMA queue advertised by modern discrete GPUs.
 *
 * Architectural shape (matches what UE5 / Frostbite / Unity HDRP do for
 * texture streaming):
 *
 *   * One VkCommandPool per worker thread (today: a single shared pool
 *     guarded by std::mutex; the per-thread split lands in Phase 5d once
 *     multi-threaded recording goes online).
 *   * Free-list pools of VkFence / VkCommandBuffer mirror the Phase 5b
 *     pattern in VkResourceManager so steady-state uploads hit zero
 *     kernel allocations.
 *   * Submission goes to the transfer queue if HasDedicatedTransferQueue();
 *     otherwise it transparently falls back to the graphics queue so call
 *     sites never need to branch. Runtime construction always supplies a
 *     VulkanQueueManager; the raw queue fallback exists only for isolated
 *     backend tests that do not construct the full renderer.
 *
 * Queue family ownership transfer is the caller's responsibility. The
 * recommended pattern is documented on Begin/End below: emit a release
 * barrier with srcQueueFamilyIndex = transferFamily,
 * dstQueueFamilyIndex = graphicsFamily before EndAsync(), then on the
 * graphics queue (typically inside the next render frame) emit the
 * matching acquire barrier with the same family pair before the first
 * sample/draw that uses the resource.
 */

#pragma once

#include "VkTypes.h"
#include <atomic>
#include <cstdint>
#include <function/renderer/rhi/RhiSubmission.h>
#include <mutex>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux
{
namespace vk
{

class VkDeviceContext;
class VulkanQueueManager;

/**
 * @brief Opaque handle returned from EndAsync() for caller-side completion polling.
 *
 * Holds a raw pointer to a context-owned tracker. Querying / waiting goes
 * back through AsyncTransferContext::IsComplete / Wait so the context can
 * recycle the underlying fence + command buffer once the GPU signals.
 */
struct AsyncSubmissionHandle
{
    uint64_t id = 0; ///< Monotonic id, 0 == invalid
    uint64_t timelineValue = 0;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return id != 0;
    }
};

/**
 * @brief Pooled async-upload command recorder + submitter.
 */
class AsyncTransferContext
{
  public:
    AsyncTransferContext() = default;
    ~AsyncTransferContext();

    // Non-copyable, non-movable — held by VkDeviceContext.
    AsyncTransferContext(const AsyncTransferContext &) = delete;
    AsyncTransferContext &operator=(const AsyncTransferContext &) = delete;
    AsyncTransferContext(AsyncTransferContext &&) = delete;
    AsyncTransferContext &operator=(AsyncTransferContext &&) = delete;

    /**
     * @brief Initialize against a fully-created device context.
     *
     * Builds the upload command pool against the transfer queue family if
     * the device advertises a dedicated one, otherwise against the graphics
     * family (legacy fallback path).
     */
    bool Initialize(VkDevice device, uint32_t transferQueueFamily, VkQueue transferQueue,
                    bool hasDedicatedTransferQueue, bool enableTimelineSemaphore = false,
                    VulkanQueueManager *queueManager = nullptr,
                    rhi::QueueRole submissionRole = rhi::QueueRole::Transfer);

    /**
     * @brief Tear down all pooled fences / command buffers.
     *
     * Must be called BEFORE the owning device is destroyed. Idempotent.
     */
    void Destroy() noexcept;

    /**
     * @brief Start recording an upload command buffer.
     *
     * The returned VkCommandBuffer is in the recording state (the
     * vkBeginCommandBuffer call has already happened). Callers append
     * vkCmdCopyBuffer / vkCmdCopyBufferToImage / vkCmdPipelineBarrier as
     * usual.
     *
     * If the upload targets a resource that will be sampled on the graphics
     * queue, callers MUST end recording with a release barrier
     * (srcQueueFamilyIndex = transferFamily, dstQueueFamilyIndex =
     * graphicsFamily, srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
     * dstAccessMask = 0) and then arrange for the matching acquire barrier
     * on the graphics queue before first use. The acquire barrier should
     * use srcAccessMask = 0, dstAccessMask = VK_ACCESS_SHADER_READ_BIT
     * (or whatever the consumer needs), srcStageMask = TOP_OF_PIPE_BIT,
     * dstStageMask = the consumer's stage. See the Vulkan spec
     * §7.7.4 "Queue Family Ownership Transfer" for the canonical example.
     */
    [[nodiscard]] VkCommandBuffer Begin();

    /**
     * @brief Submit a recorded command buffer and block until the GPU
     *        signals completion. Use for the legacy synchronous path.
     */
    void EndSync(VkCommandBuffer cmd);

    /**
     * @brief Submit asynchronously. Returns a handle that can be polled
     *        via IsComplete() or blocked on via Wait(). The caller is
     *        responsible for keeping any source CPU staging memory alive
     *        until completion.
     */
    [[nodiscard]] AsyncSubmissionHandle EndAsync(VkCommandBuffer cmd);

    /**
     * @brief Non-blocking completion poll. Returns true once the GPU has
     *        signalled the upload's fence (also recycles the fence + cmd
     *        buffer back into the free list as a side effect).
     */
    [[nodiscard]] bool IsComplete(AsyncSubmissionHandle handle);

    /**
     * @brief Block until the upload completes. Returns immediately if the
     *        handle is invalid or already retired.
     */
    void Wait(AsyncSubmissionHandle handle);

    /**
     * @brief True iff the underlying queue is on a different family than
     *        graphics — implies submissions actually run in parallel.
     */
    [[nodiscard]] bool IsAsyncCapable() const noexcept
    {
        return m_hasDedicatedQueue;
    }

    /**
     * @brief Queue family the upload command buffers are recorded against.
     *
     * Callers building queue-family-ownership-transfer barriers need this
     * for srcQueueFamilyIndex on the release side.
     */
    [[nodiscard]] uint32_t GetQueueFamily() const noexcept
    {
        return m_queueFamily;
    }

    [[nodiscard]] VkSemaphore GetTimelineSemaphore() const noexcept
    {
        return m_timelineSemaphore;
    }

  private:
    struct InFlightUpload
    {
        uint64_t id = 0;
        VkFence fence = VK_NULL_HANDLE;
        VkCommandBuffer cmd = VK_NULL_HANDLE;
        rhi::SubmissionTicket ticket;
    };

    /// @brief Recycle (or lazily create) a fence ready for re-submission.
    [[nodiscard]] VkFence AcquireFenceLocked();

    /// @brief Recycle (or lazily create + record-prep) a command buffer.
    [[nodiscard]] VkCommandBuffer AcquireCommandBufferLocked();

    /// @brief Reap any in-flight uploads whose fence has signalled.
    void ReapCompletedLocked();

    /// @brief Drop the in-flight tracker for `id` (called when the caller
    ///        observes completion or explicitly waits). Returns the cmd
    ///        buffer + fence so the caller can recycle them under the lock.
    void RetireLocked(uint64_t id, VkFence &fence, VkCommandBuffer &cmd);

    VkDevice m_device = VK_NULL_HANDLE;
    VkCommandPool m_pool = VK_NULL_HANDLE;
    VkQueue m_queue = VK_NULL_HANDLE;
    uint32_t m_queueFamily = 0;
    bool m_hasDedicatedQueue = false;
    VulkanQueueManager *m_queueManager = nullptr;
    rhi::QueueRole m_submissionRole = rhi::QueueRole::Transfer;
    VkSemaphore m_timelineSemaphore = VK_NULL_HANDLE;
    std::atomic<uint64_t> m_nextTimelineValue{1};

    std::mutex m_mutex;
    std::vector<VkFence> m_freeFences;
    std::vector<VkFence> m_allFences;
    std::vector<VkCommandBuffer> m_freeCmdBuffers;
    std::vector<VkCommandBuffer> m_allCmdBuffers;
    std::vector<InFlightUpload> m_inFlight;
    std::atomic<uint64_t> m_nextId{1};
};

} // namespace vk
} // namespace infernux
