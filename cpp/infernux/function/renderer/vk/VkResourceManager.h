/**
 * @file VkResourceManager.h
 * @brief Vulkan resource management - buffers, images, textures, and command pools
 *
 * This class manages GPU resource allocation and provides:
 * - Staging buffer pooling for efficient uploads
 * - Backend-neutral RHI texture uploads
 * - Depth buffer management
 * - Command pool and command buffer management
 * - Queue-manager-backed uploads and readbacks
 *
 * Architecture Notes:
 * - Resources are reference counted or managed via unique ownership
 * - Staging uploads use double buffering for async transfers (future)
 * - Designed for easy integration with RenderGraph
 *
 * Usage:
 *   VkResourceManager resources;
 *   resources.Initialize(deviceContext, &queueManager);
 *
 *   // Create a vertex buffer
 *   auto vertexBuffer = resources.CreateVertexBuffer(vertices.data(), vertices.size() * sizeof(Vertex));
 */

#pragma once

#include <function/renderer/rhi/RhiSubmission.h>

#include "../rhi/RhiBuffer.h"
#include "../rhi/RhiTexture.h"
#include "../rhi/RhiUpload.h"
#include "AsyncTransferContext.h"
#include "VkHandle.h"
#include "VkTypes.h"
#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace infernux
{
namespace vk
{

// Forward declarations
class VkDeviceContext;
class VulkanQueueManager;
class VkResourceManager;
class GraphicsSubmissionTicket;
class GraphicsImageReadbackRecorder;
class VulkanRhiDevice;

class BufferUploadTicket final
{
  public:
    [[nodiscard]] bool IsComplete() const noexcept
    {
        return m_complete;
    }
    [[nodiscard]] bool IsPublished() const noexcept
    {
        return m_published;
    }
    [[nodiscard]] bool IsAsync() const noexcept
    {
        return m_async;
    }
    [[nodiscard]] VkDeviceSize GetSize() const noexcept
    {
        return m_size;
    }
    [[nodiscard]] const std::shared_ptr<VkBufferHandle> &GetBuffer() const;
    [[nodiscard]] std::shared_ptr<rhi::BufferResource> GetRhiBuffer() const;

  private:
    friend class VkResourceManager;
    VkResourceManager *m_manager = nullptr;
    std::shared_ptr<VkBufferHandle> m_staging;
    std::shared_ptr<VkBufferHandle> m_destination;
    // Publication must not make the upload queue an owner of the public RHI
    // resource. m_destination independently keeps the Vulkan allocation alive
    // until transfer completion.
    std::weak_ptr<rhi::BufferResource> m_rhiBuffer;
    AsyncSubmissionHandle m_upload;
    VkDeviceSize m_size = 0;
    bool m_complete = false;
    bool m_published = false;
    bool m_async = false;
};

class TextureUploadTicket final
{
  public:
    [[nodiscard]] bool IsComplete() const noexcept
    {
        return m_complete;
    }
    [[nodiscard]] bool IsPublished() const noexcept
    {
        return m_published;
    }
    [[nodiscard]] bool IsAsync() const noexcept
    {
        return m_async;
    }
    [[nodiscard]] VkDeviceSize GetResidentBytes() const noexcept
    {
        return m_residentBytes;
    }
    [[nodiscard]] const std::shared_ptr<rhi::TextureResource> &GetTexture() const;

  private:
    friend class VkResourceManager;
    VkResourceManager *m_manager = nullptr;
    std::shared_ptr<VkBufferHandle> m_staging;
    std::shared_ptr<rhi::TextureResource> m_texture;
    AsyncSubmissionHandle m_upload;
    VkDeviceSize m_residentBytes = 0;
    bool m_complete = false;
    bool m_published = false;
    bool m_async = false;
};

enum class ImageReadbackStatus : uint8_t
{
    Pending,
    Completed,
    Failed,
    Cancelled,
};

class ImageReadbackTicket final
{
  public:
    [[nodiscard]] ImageReadbackStatus GetStatus() const noexcept
    {
        return m_status.load(std::memory_order_acquire);
    }
    [[nodiscard]] bool IsDone() const noexcept
    {
        return GetStatus() != ImageReadbackStatus::Pending;
    }
    [[nodiscard]] uint32_t GetWidth() const noexcept
    {
        return m_width;
    }
    [[nodiscard]] uint32_t GetHeight() const noexcept
    {
        return m_height;
    }
    [[nodiscard]] uint32_t GetChannelCount() const noexcept
    {
        return m_channelCount;
    }
    [[nodiscard]] const std::string &GetElementType() const noexcept
    {
        return m_elementType;
    }
    [[nodiscard]] size_t GetByteSize() const noexcept
    {
        return m_byteSize;
    }
    [[nodiscard]] const std::string &GetError() const noexcept
    {
        return m_error;
    }
    [[nodiscard]] const std::vector<uint8_t> &GetData() const;
    void Cancel() noexcept;

  private:
    friend class VkResourceManager;
    friend class GraphicsImageReadbackRecorder;
    std::shared_ptr<VkBufferHandle> m_staging;
    AsyncSubmissionHandle m_submission;
    std::shared_ptr<GraphicsSubmissionTicket> m_graphicsSubmission;
    std::atomic<ImageReadbackStatus> m_status{ImageReadbackStatus::Pending};
    std::vector<uint8_t> m_data;
    std::string m_elementType;
    std::string m_error;
    uint32_t m_width = 0;
    uint32_t m_height = 0;
    uint32_t m_channelCount = 0;
    size_t m_byteSize = 0;
};

class GraphicsSubmissionTicket final
{
  public:
    [[nodiscard]] bool IsComplete() const noexcept
    {
        return m_complete.load(std::memory_order_acquire);
    }

  private:
    friend class VkResourceManager;
    VkCommandBuffer m_commandBuffer = VK_NULL_HANDLE;
    VkFence m_fence = VK_NULL_HANDLE;
    rhi::SubmissionTicket m_submission;
    rhi::SubmissionSerial m_completionEpoch = rhi::InvalidSubmissionSerial;
    std::function<void()> m_releaseResources;
    std::atomic<bool> m_complete{false};
};

class GraphicsImageReadbackRecorder final
{
  public:
    GraphicsImageReadbackRecorder() = default;
    ~GraphicsImageReadbackRecorder();

    GraphicsImageReadbackRecorder(const GraphicsImageReadbackRecorder &) = delete;
    GraphicsImageReadbackRecorder &operator=(const GraphicsImageReadbackRecorder &) = delete;
    GraphicsImageReadbackRecorder(GraphicsImageReadbackRecorder &&other) noexcept;
    GraphicsImageReadbackRecorder &operator=(GraphicsImageReadbackRecorder &&other) noexcept;

    [[nodiscard]] VkCommandBuffer GetCommandBuffer() const noexcept
    {
        return m_commandBuffer;
    }
    [[nodiscard]] VkBuffer GetStagingBuffer() const noexcept
    {
        return m_ticket && m_ticket->m_staging ? m_ticket->m_staging->GetBuffer() : VK_NULL_HANDLE;
    }
    [[nodiscard]] std::shared_ptr<ImageReadbackTicket> Submit(std::function<void()> releaseResources = {});

  private:
    friend class VkResourceManager;
    void Reset() noexcept;

    VkResourceManager *m_manager = nullptr;
    std::shared_ptr<ImageReadbackTicket> m_ticket;
    VkCommandBuffer m_commandBuffer = VK_NULL_HANDLE;
};

/**
 * @brief Descriptor binding information
 */
struct DescriptorBindingInfo
{
    uint32_t binding = 0;
    VkDescriptorType type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    VkShaderStageFlags stageFlags = VK_SHADER_STAGE_ALL;
    uint32_t count = 1;
};

/**
 * @brief Command buffer allocation result
 */
struct CommandBufferAllocation
{
    VkCommandBuffer cmdBuffer = VK_NULL_HANDLE;
    VkCommandPool pool = VK_NULL_HANDLE;
};

/**
 * @brief Manages Vulkan resources - buffers, images, descriptors, etc.
 */
class VkResourceManager
{
  public:
    VkResourceManager() = default;
    ~VkResourceManager();

    // Bound to one device/transfer context for its full lifetime.
    VkResourceManager(const VkResourceManager &) = delete;
    VkResourceManager &operator=(const VkResourceManager &) = delete;
    VkResourceManager(VkResourceManager &&other) = delete;
    VkResourceManager &operator=(VkResourceManager &&other) = delete;

    // ========================================================================
    // Initialization
    // ========================================================================

    /**
     * @brief Initialize the resource manager
     *
     * @param context Device context for Vulkan access
     * @return true if initialization succeeded
     */
    bool Initialize(VkDeviceContext &context, VulkanQueueManager *queueManager = nullptr);

    /**
     * @brief Cleanup all managed resources
     */
    void Destroy() noexcept;

    /// @brief Check if initialized
    [[nodiscard]] bool IsValid() const
    {
        return m_device != VK_NULL_HANDLE;
    }

    /// @brief Get the internal command pool
    [[nodiscard]] VkCommandPool GetCommandPool() const;

    // ========================================================================
    // Buffer Management
    // ========================================================================

    /**
     * @brief Create a vertex buffer with data
     *
     * @param data Pointer to vertex data
     * @param size Size in bytes
     * @return Unique pointer to buffer handle
     */
    [[nodiscard]] std::unique_ptr<VkBufferHandle> CreateVertexBuffer(const void *data, VkDeviceSize size);

    /**
     * @brief Create an index buffer with data
     *
     * @param data Pointer to index data
     * @param size Size in bytes
     * @return Unique pointer to buffer handle
     */
    [[nodiscard]] std::unique_ptr<VkBufferHandle> CreateIndexBuffer(const void *data, VkDeviceSize size);

    [[nodiscard]] std::shared_ptr<BufferUploadTicket> BeginBufferUpload(const rhi::BufferUploadRequest &request);
    [[nodiscard]] bool TryPublishBufferUpload(const std::shared_ptr<BufferUploadTicket> &ticket);
    [[nodiscard]] std::shared_ptr<rhi::BufferResource>
    GetPublishedRhiBuffer(const std::shared_ptr<BufferUploadTicket> &ticket);
    void DrainBufferUploads() noexcept;

    [[nodiscard]] std::shared_ptr<TextureUploadTicket> BeginTextureUpload(const rhi::TextureUploadRequest &request);
    [[nodiscard]] bool TryPublishTextureUpload(const std::shared_ptr<TextureUploadTicket> &ticket);
    void PollGpuUploads();

    [[nodiscard]] std::shared_ptr<ImageReadbackTicket>
    BeginImageReadback(VkImage image, VkImageLayout layout, VkImageAspectFlags aspect, VkPipelineStageFlags sourceStage,
                       VkAccessFlags sourceAccess, uint32_t width, uint32_t height, VkFormat format);
    [[nodiscard]] GraphicsImageReadbackRecorder BeginGraphicsImageReadback(uint32_t width, uint32_t height,
                                                                           VkFormat format);
    void PollImageReadbacks();
    void DrainImageReadbacks() noexcept;
    [[nodiscard]] size_t GetPendingImageReadbackCount() const noexcept
    {
        return m_pendingImageReadbacks.size();
    }
    [[nodiscard]] uint64_t GetPendingImageReadbackBytes() const noexcept;
    [[nodiscard]] size_t GetPendingGpuTransferCount() const noexcept
    {
        return m_pendingBufferUploads.size() + m_pendingTextureUploads.size();
    }
    [[nodiscard]] uint64_t GetTimelineUploadPublicationCount() const noexcept
    {
        return m_timelineUploadPublicationCount;
    }
    [[nodiscard]] uint64_t GetBufferUploadSubmissionCount() const noexcept
    {
        return m_bufferUploadSubmissionCount;
    }
    [[nodiscard]] bool IsUploadTimelineEnabled() const noexcept
    {
        return m_asyncTransfer && m_asyncTransfer->IsAsyncCapable() &&
               m_asyncTransfer->GetTimelineSemaphore() != VK_NULL_HANDLE;
    }
    [[nodiscard]] VkSemaphore GetUploadTimelineSemaphore() const noexcept;
    [[nodiscard]] uint64_t GetRequiredUploadTimelineValue() const noexcept
    {
        return m_requiredUploadTimelineValue;
    }

    [[nodiscard]] VkDeviceSize GetStagingPoolBytes() const noexcept
    {
        return m_stagingPoolBytes;
    }
    [[nodiscard]] size_t GetStagingPoolBufferCount() const noexcept
    {
        return m_stagingPoolBufferCount;
    }
    [[nodiscard]] uint64_t GetStagingAllocationCount() const noexcept
    {
        return m_stagingAllocationCount;
    }
    [[nodiscard]] uint64_t GetStagingReuseCount() const noexcept
    {
        return m_stagingReuseCount;
    }
    [[nodiscard]] uint64_t GetStagingDiscardCount() const noexcept
    {
        return m_stagingDiscardCount;
    }

    /**
     * @brief Create a uniform buffer
     *
     * @param size Size in bytes
     * @return Unique pointer to buffer handle (host-visible for frequent updates)
     */
    [[nodiscard]] std::unique_ptr<VkBufferHandle> CreateUniformBuffer(VkDeviceSize size);

    /**
     * @brief Create a storage buffer
     *
     * @param size Size in bytes
     * @param deviceLocal If true, create device-local buffer
     * @return Unique pointer to buffer handle
     */
    [[nodiscard]] std::unique_ptr<VkBufferHandle> CreateStorageBuffer(VkDeviceSize size, bool deviceLocal = true);

    /**
     * @brief Create a staging buffer for data uploads
     *
     * @param size Size in bytes
     * @return Unique pointer to buffer handle (host-visible)
     */
    [[nodiscard]] std::unique_ptr<VkBufferHandle> CreateStagingBuffer(VkDeviceSize size);

    /**
     * @brief Copy buffer data using a one-shot command buffer
     *
     * @param srcBuffer Source buffer
     * @param dstBuffer Destination buffer
     * @param size Size to copy (0 = entire buffer)
     */
    void CopyBuffer(VkBuffer srcBuffer, VkBuffer dstBuffer, VkDeviceSize size);

    // ========================================================================
    // Image and Texture Management
    // ========================================================================

    /**
     * @brief Create an image handle
     *
     * @param width Image width
     * @param height Image height
     * @param format Image format
     * @param usage Usage flags
     * @param properties Memory properties
     * @return Unique pointer to image handle
     */
    [[nodiscard]] std::unique_ptr<VkImageHandle>
    CreateImage(uint32_t width, uint32_t height, VkFormat format, VkImageUsageFlags usage,
                VkMemoryPropertyFlags properties = VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);

    /**
     * @brief Create a depth buffer
     *
     * @param width Width
     * @param height Height
     * @param format Depth format (VK_FORMAT_UNDEFINED = auto-select)
     * @return Unique pointer to image handle with view
     */
    [[nodiscard]] std::unique_ptr<VkImageHandle> CreateDepthBuffer(uint32_t width, uint32_t height,
                                                                   VkFormat format = VK_FORMAT_UNDEFINED);

    /**
     * @brief Transition image layout
     *
     * @param image Image to transition
     * @param format Image format
     * @param oldLayout Current layout
     * @param newLayout Target layout
     */
    void TransitionImageLayout(VkImage image, VkFormat format, VkImageLayout oldLayout, VkImageLayout newLayout);

    /**
     * @brief Copy buffer to image
     *
     * @param buffer Source buffer
     * @param image Destination image
     * @param width Image width
     * @param height Image height
     */
    void CopyBufferToImage(VkBuffer buffer, VkImage image, uint32_t width, uint32_t height);

    // ========================================================================
    // Command Buffer Management
    // ========================================================================

    /**
     * @brief Allocate a primary command buffer
     * @return Allocated command buffer
     */
    [[nodiscard]] CommandBufferAllocation AllocatePrimaryCommandBuffer();

    /**
     * @brief Allocate a secondary command buffer
     * @return Allocated command buffer
     */
    [[nodiscard]] CommandBufferAllocation AllocateSecondaryCommandBuffer();

    /**
     * @brief Free a command buffer
     * @param allocation Command buffer allocation to free
     */
    void FreeCommandBuffer(const CommandBufferAllocation &allocation);

    /**
     * @brief Begin a one-shot command buffer for immediate submission
     * @return Command buffer ready for recording
     */
    [[nodiscard]] VkCommandBuffer BeginSingleTimeCommands();

    /**
     * @brief End and submit a one-shot command buffer
     * @param cmdBuffer Command buffer to submit
     */
    void EndSingleTimeCommands(VkCommandBuffer cmdBuffer);
    [[nodiscard]] std::shared_ptr<GraphicsSubmissionTicket>
    EndSingleTimeCommandsAsync(VkCommandBuffer cmdBuffer, std::function<void()> releaseResources = {});
    void PollAsyncGraphicsSubmissions();
    void DrainAsyncGraphicsSubmissions() noexcept;
    [[nodiscard]] size_t GetPendingAsyncGraphicsSubmissionCount() const noexcept
    {
        return m_pendingAsyncGraphicsSubmissions.size();
    }
    [[nodiscard]] uint64_t GetAsyncGraphicsSubmissionCount() const noexcept
    {
        return m_asyncGraphicsSubmissionCount;
    }

    // ========================================================================
    // Sampler Management
    // ========================================================================

    /**
     * @brief Get or create a standard linear sampler
     */
    [[nodiscard]] VkSampler GetLinearSampler();

    /**
     * @brief Get or create a standard nearest sampler
     */
    [[nodiscard]] VkSampler GetNearestSampler();

    /**
     * @brief Create a custom sampler
     */
    [[nodiscard]] std::unique_ptr<VkSamplerHandle>
    CreateSampler(VkFilter filter = VK_FILTER_LINEAR,
                  VkSamplerAddressMode addressMode = VK_SAMPLER_ADDRESS_MODE_REPEAT);

    // ========================================================================
    // Accessors
    // ========================================================================

    [[nodiscard]] VkDevice GetDevice() const
    {
        return m_device;
    }
    [[nodiscard]] VkPhysicalDevice GetPhysicalDevice() const
    {
        return m_physicalDevice;
    }
    [[nodiscard]] VkQueue GetGraphicsQueue() const
    {
        return m_graphicsQueue;
    }

  private:
    friend class GraphicsImageReadbackRecorder;
    // ========================================================================
    // Internal Methods
    // ========================================================================

    /// @brief Create a buffer with given usage and properties
    std::unique_ptr<VkBufferHandle> CreateBufferInternal(VkDeviceSize size, VkBufferUsageFlags usage,
                                                         VkMemoryPropertyFlags properties,
                                                         const std::vector<uint32_t> &queueFamilies = {});
    std::shared_ptr<VkBufferHandle> AcquireStagingBuffer(VkDeviceSize size);
    void RecycleStagingBuffer(std::shared_ptr<VkBufferHandle> buffer) noexcept;
    void ClearStagingPool() noexcept;
    void FinalizeImageReadback(const std::shared_ptr<ImageReadbackTicket> &ticket) noexcept;
    std::shared_ptr<ImageReadbackTicket> SubmitGraphicsImageReadback(GraphicsImageReadbackRecorder &recorder,
                                                                     std::function<void()> releaseResources);
    void AbandonGraphicsImageReadback(GraphicsImageReadbackRecorder &recorder) noexcept;
    void AssertReadbackThread() const;

    /// @brief Get best depth format for the device
    VkFormat FindDepthFormat() const;

    /// @brief Check if format has stencil component
    static bool HasStencilComponent(VkFormat format);

  public:
    void SetSkipWaitIdle(bool v)
    {
        m_skipWaitIdle = v;
    }

    /// Configure the transfer context used by explicit Buffer/TextureUploadTicket submissions.
    void SetAsyncTransferContext(class AsyncTransferContext *transfer, uint32_t graphicsQueueFamily)
    {
        m_asyncTransfer = transfer;
        m_graphicsQueueFamily = graphicsQueueFamily;
    }

    void SetAsyncReadbackContext(class AsyncTransferContext *readback)
    {
        m_asyncReadback = readback;
    }

  private:
    bool m_skipWaitIdle = false;
    VmaAllocator m_vmaAllocator = VK_NULL_HANDLE;
    VkDevice m_device = VK_NULL_HANDLE;
    VkPhysicalDevice m_physicalDevice = VK_NULL_HANDLE;
    VkQueue m_graphicsQueue = VK_NULL_HANDLE;
    VulkanRhiDevice *m_rhiDevice = nullptr;
    VulkanQueueManager *m_queueManager = nullptr;
    VkCommandPool m_commandPool = VK_NULL_HANDLE;
    std::thread::id m_ownerThread;

    // Optional plug-in async-transfer context. Lifetime is owned externally
    // (typically InxVkCoreModular::m_asyncTransferContext) — VkResourceManager
    // never destroys it. nullptr means "always use the synchronous path".
    class AsyncTransferContext *m_asyncTransfer = nullptr;
    class AsyncTransferContext *m_asyncReadback = nullptr;
    uint32_t m_graphicsQueueFamily = 0;
    std::vector<std::shared_ptr<BufferUploadTicket>> m_pendingBufferUploads;
    std::vector<std::shared_ptr<TextureUploadTicket>> m_pendingTextureUploads;
    std::vector<std::shared_ptr<ImageReadbackTicket>> m_pendingImageReadbacks;
    std::unordered_map<VkDeviceSize, std::vector<std::shared_ptr<VkBufferHandle>>> m_stagingPool;
    VkDeviceSize m_stagingPoolBytes = 0;
    size_t m_stagingPoolBufferCount = 0;
    uint64_t m_stagingAllocationCount = 0;
    uint64_t m_stagingReuseCount = 0;
    uint64_t m_stagingDiscardCount = 0;
    uint64_t m_requiredUploadTimelineValue = 0;
    uint64_t m_timelineUploadPublicationCount = 0;
    uint64_t m_bufferUploadSubmissionCount = 0;

    // Cached samplers
    VkSampler m_linearSampler = VK_NULL_HANDLE;
    VkSampler m_nearestSampler = VK_NULL_HANDLE;

    // ────────────────────────────────────────────────────────────────────
    // Single-time-command pools.
    //
    // Pre Phase 5b every BeginSingleTimeCommands / EndSingleTimeCommands
    // pair did vkAllocateCommandBuffers + vkCreateFence + vkQueueSubmit +
    // vkWaitForFences + vkDestroyFence + vkFreeCommandBuffers, which is
    // 4 kernel-driver round-trips PER tiny upload. Texture-heavy scenes
    // were spending hundreds of microseconds per asset in driver allocation
    // alone before any actual GPU work happened.
    //
    // The free-list below recycles fences and command buffers so steady-state
    // usage hits the kernel exactly twice per submit (vkBeginCommandBuffer +
    // vkQueueSubmit) and the wait itself.
    //
    // The mutex protects against concurrent uploads from background asset
    // loading threads (Phase 5d will widen this to per-thread pools).
    // ────────────────────────────────────────────────────────────────────
    std::vector<VkCommandBuffer> m_freeSingleTimeCmdBuffers;
    std::vector<VkFence> m_freeSingleTimeFences;
    std::vector<VkFence> m_allSingleTimeFences;             // owned, destroyed in Destroy()
    std::vector<VkCommandBuffer> m_allSingleTimeCmdBuffers; // owned via m_commandPool
    std::unordered_map<VkCommandBuffer, rhi::SubmissionSerial> m_singleTimeCompletionEpochs;
    std::vector<std::shared_ptr<GraphicsSubmissionTicket>> m_pendingAsyncGraphicsSubmissions;
    uint64_t m_asyncGraphicsSubmissionCount = 0;
    std::mutex m_singleTimeMutex;
};

} // namespace vk
} // namespace infernux
