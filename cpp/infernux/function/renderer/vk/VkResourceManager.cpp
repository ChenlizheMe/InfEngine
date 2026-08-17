/**
 * @file VkResourceManager.cpp
 * @brief Implementation of Vulkan resource management
 */

#include "VkResourceManager.h"
#include "AsyncTransferContext.h"
#include "DescriptorBindTrace.h"
#include "RhiVulkanTypes.h"
#include "VkDeviceContext.h"
#include "VulkanQueueManager.h"
#include "VulkanRhiDevice.h"
#include <SDL3/SDL.h>
#include <core/error/InxError.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>

namespace infernux
{
namespace vk
{

namespace
{
constexpr VkDeviceSize MinimumStagingClass = 4ULL * 1024ULL;
constexpr VkDeviceSize MaximumPooledStagingClass = 16ULL * 1024ULL * 1024ULL;
constexpr VkDeviceSize StagingPoolBudget = 64ULL * 1024ULL * 1024ULL;

VkDeviceSize StagingClassFor(VkDeviceSize requestedSize)
{
    if (requestedSize > MaximumPooledStagingClass)
        return requestedSize;
    VkDeviceSize sizeClass = MinimumStagingClass;
    while (sizeClass < requestedSize)
        sizeClass *= 2;
    return sizeClass;
}

struct ReadbackFormatInfo
{
    uint32_t channels;
    uint32_t bytesPerPixel;
    const char *elementType;
};

struct TextureFormatLayout
{
    uint32_t blockWidth = 1;
    uint32_t blockHeight = 1;
    uint32_t bytesPerBlock = 0;
};

TextureFormatLayout GetTextureFormatLayout(VkFormat format)
{
    switch (format) {
    case VK_FORMAT_R8G8B8A8_UNORM:
    case VK_FORMAT_R8G8B8A8_SRGB:
        return {1, 1, 4};
    case VK_FORMAT_R4G4B4A4_UNORM_PACK16:
        return {1, 1, 2};
    case VK_FORMAT_R16G16B16A16_UNORM:
    case VK_FORMAT_R16G16B16A16_SFLOAT:
        return {1, 1, 8};
    case VK_FORMAT_R32G32B32A32_SFLOAT:
        return {1, 1, 16};
    case VK_FORMAT_BC1_RGBA_UNORM_BLOCK:
    case VK_FORMAT_BC1_RGBA_SRGB_BLOCK:
    case VK_FORMAT_BC4_UNORM_BLOCK:
        return {4, 4, 8};
    case VK_FORMAT_BC3_UNORM_BLOCK:
    case VK_FORMAT_BC3_SRGB_BLOCK:
    case VK_FORMAT_BC5_UNORM_BLOCK:
    case VK_FORMAT_BC6H_UFLOAT_BLOCK:
    case VK_FORMAT_BC7_UNORM_BLOCK:
    case VK_FORMAT_BC7_SRGB_BLOCK:
        return {4, 4, 16};
    default:
        throw std::invalid_argument("RHI texture upload format has no supported byte layout");
    }
}

VkDeviceSize AlignUploadOffset(VkDeviceSize value)
{
    constexpr VkDeviceSize Alignment = 16;
    if (value > std::numeric_limits<VkDeviceSize>::max() - (Alignment - 1))
        throw std::overflow_error("RHI texture upload staging size overflow");
    return (value + Alignment - 1) & ~(Alignment - 1);
}

ReadbackFormatInfo GetReadbackFormatInfo(VkFormat format)
{
    switch (format) {
    case VK_FORMAT_R8G8B8A8_UNORM:
    case VK_FORMAT_R8G8B8A8_SRGB:
    case VK_FORMAT_B8G8R8A8_UNORM:
    case VK_FORMAT_B8G8R8A8_SRGB:
        return {4, 4, "uint8"};
    case VK_FORMAT_R16G16B16A16_SFLOAT:
        return {4, 8, "float16"};
    case VK_FORMAT_R32G32B32A32_SFLOAT:
        return {4, 16, "float32"};
    case VK_FORMAT_R32_SFLOAT:
        return {1, 4, "float32"};
    case VK_FORMAT_R32G32_UINT:
        return {2, 8, "uint32"};
    default:
        throw std::invalid_argument("Image format is not supported by GPU readback");
    }
}

void WaitForFencePumpingEvents(VkDevice device, VkFence fence)
{
    constexpr uint64_t kPollTimeoutNs = 50'000'000; // 50 ms
    while (true) {
        VkResult result = vkWaitForFences(device, 1, &fence, VK_TRUE, kPollTimeoutNs);
        if (result == VK_SUCCESS) {
            return;
        }
        if (result != VK_TIMEOUT) {
            INXLOG_ERROR("VkResourceManager::EndSingleTimeCommands fence wait failed: ", result);
            return;
        }
        SDL_PumpEvents();
    }
}

} // namespace

// ============================================================================
// Constructor / Destructor / Move
// ============================================================================

VkResourceManager::~VkResourceManager()
{
    Destroy();
}

const std::shared_ptr<VkBufferHandle> &BufferUploadTicket::GetBuffer() const
{
    if (!m_published || !m_destination)
        throw std::logic_error("GPU buffer upload has not been published");
    return m_destination;
}

std::shared_ptr<rhi::BufferResource> BufferUploadTicket::GetRhiBuffer() const
{
    auto resource = m_rhiBuffer.lock();
    if (!m_published || !resource)
        throw std::logic_error("RHI buffer upload has not been published");
    return resource;
}

const std::shared_ptr<rhi::TextureResource> &TextureUploadTicket::GetTexture() const
{
    if (!m_published || !m_texture)
        throw std::logic_error("GPU texture upload has not been published");
    return m_texture;
}

const std::vector<uint8_t> &ImageReadbackTicket::GetData() const
{
    const ImageReadbackStatus status = GetStatus();
    if (status == ImageReadbackStatus::Failed)
        throw std::runtime_error(m_error);
    if (status != ImageReadbackStatus::Completed)
        throw std::logic_error("GPU image readback has not completed");
    return m_data;
}

void ImageReadbackTicket::Cancel() noexcept
{
    ImageReadbackStatus expected = ImageReadbackStatus::Pending;
    m_status.compare_exchange_strong(expected, ImageReadbackStatus::Cancelled, std::memory_order_acq_rel);
}

GraphicsImageReadbackRecorder::~GraphicsImageReadbackRecorder()
{
    Reset();
}

GraphicsImageReadbackRecorder::GraphicsImageReadbackRecorder(GraphicsImageReadbackRecorder &&other) noexcept
    : m_manager(other.m_manager), m_ticket(std::move(other.m_ticket)), m_commandBuffer(other.m_commandBuffer)
{
    other.m_manager = nullptr;
    other.m_commandBuffer = VK_NULL_HANDLE;
}

GraphicsImageReadbackRecorder &GraphicsImageReadbackRecorder::operator=(GraphicsImageReadbackRecorder &&other) noexcept
{
    if (this == &other)
        return *this;
    Reset();
    m_manager = other.m_manager;
    m_ticket = std::move(other.m_ticket);
    m_commandBuffer = other.m_commandBuffer;
    other.m_manager = nullptr;
    other.m_commandBuffer = VK_NULL_HANDLE;
    return *this;
}

std::shared_ptr<ImageReadbackTicket> GraphicsImageReadbackRecorder::Submit(std::function<void()> releaseResources)
{
    if (!m_manager)
        throw std::logic_error("Graphics image readback recorder is no longer active");
    return m_manager->SubmitGraphicsImageReadback(*this, std::move(releaseResources));
}

void GraphicsImageReadbackRecorder::Reset() noexcept
{
    if (m_manager)
        m_manager->AbandonGraphicsImageReadback(*this);
}

// ============================================================================
// Initialization
// ============================================================================

bool VkResourceManager::Initialize(VkDeviceContext &context, VulkanQueueManager *queueManager)
{
    m_ownerThread = std::this_thread::get_id();
    m_device = context.GetDevice();
    m_physicalDevice = context.GetPhysicalDevice();
    m_vmaAllocator = context.GetVmaAllocator();
    m_graphicsQueue = context.GetGraphicsQueue();
    m_rhiDevice = &context.GetRhiDevice();
    m_deviceLifetime = m_rhiDevice->GetLifetime();
    m_queueManager = queueManager;

    // Create command pool
    VkCommandPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    poolInfo.queueFamilyIndex = context.GetQueueIndices().graphicsFamily.value();
    poolInfo.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;

    if (vkCreateCommandPool(m_device, &poolInfo, nullptr, &m_commandPool) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create command pool");
        return false;
    }

    INXLOG_INFO("VkResourceManager initialized");
    return true;
}

void VkResourceManager::Destroy() noexcept
{
    if (m_device == VK_NULL_HANDLE) {
        return;
    }

    DrainBufferUploads();
    DrainAsyncGraphicsSubmissions();
    DrainImageReadbacks();

    if (!m_skipWaitIdle) {
        vkDeviceWaitIdle(m_device);
    }
    ClearStagingPool();

    // Destroy samplers
    if (m_linearSampler != VK_NULL_HANDLE) {
        vkDestroySampler(m_device, m_linearSampler, nullptr);
        m_linearSampler = VK_NULL_HANDLE;
    }

    if (m_nearestSampler != VK_NULL_HANDLE) {
        vkDestroySampler(m_device, m_nearestSampler, nullptr);
        m_nearestSampler = VK_NULL_HANDLE;
    }

    // Tear down the single-time-command pools.
    // m_allSingleTimeCmdBuffers is owned by m_commandPool — destroying the
    // pool below frees them implicitly, so we only need to clear the lists.
    std::vector<rhi::SubmissionSerial> abandonedCompletionEpochs;
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        abandonedCompletionEpochs.reserve(m_singleTimeCompletionEpochs.size());
        for (const auto &[commandBuffer, epoch] : m_singleTimeCompletionEpochs) {
            (void)commandBuffer;
            abandonedCompletionEpochs.push_back(epoch);
        }
        m_singleTimeCompletionEpochs.clear();
        for (VkFence fence : m_allSingleTimeFences) {
            if (fence != VK_NULL_HANDLE) {
                vkDestroyFence(m_device, fence, nullptr);
            }
        }
        m_allSingleTimeFences.clear();
        m_freeSingleTimeFences.clear();
        m_allSingleTimeCmdBuffers.clear();
        m_freeSingleTimeCmdBuffers.clear();
    }
    for (const auto epoch : abandonedCompletionEpochs) {
        if (m_queueManager)
            m_queueManager->CompleteCompletionEpoch(epoch);
    }
    if (!abandonedCompletionEpochs.empty())
        vkdebug::ClearDescriptorRecordingContext();

    // Destroy command pool
    if (m_commandPool != VK_NULL_HANDLE) {
        vkDestroyCommandPool(m_device, m_commandPool, nullptr);
        m_commandPool = VK_NULL_HANDLE;
    }

    m_device = VK_NULL_HANDLE;
    m_physicalDevice = VK_NULL_HANDLE;
    m_graphicsQueue = VK_NULL_HANDLE;
    m_rhiDevice = nullptr;
    m_deviceLifetime.reset();
    m_queueManager = nullptr;
    m_asyncTransfer = nullptr;
    m_asyncReadback = nullptr;
}

// ============================================================================
// Buffer Management
// ============================================================================

std::unique_ptr<VkBufferHandle> VkResourceManager::CreateVertexBuffer(const void *data, VkDeviceSize size)
{
    // Create staging buffer
    auto stagingBuffer = CreateStagingBuffer(size);
    if (!stagingBuffer) {
        return nullptr;
    }

    // Copy data to staging buffer
    stagingBuffer->CopyFrom(data, size, 0);

    // Create device-local vertex buffer
    auto vertexBuffer = CreateBufferInternal(size, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
                                             VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);

    if (!vertexBuffer) {
        return nullptr;
    }

    // Copy from staging to vertex buffer
    CopyBuffer(stagingBuffer->GetBuffer(), vertexBuffer->GetBuffer(), size);

    return vertexBuffer;
}

std::unique_ptr<VkBufferHandle> VkResourceManager::CreateIndexBuffer(const void *data, VkDeviceSize size)
{
    // Create staging buffer
    auto stagingBuffer = CreateStagingBuffer(size);
    if (!stagingBuffer) {
        return nullptr;
    }

    // Copy data to staging buffer
    stagingBuffer->CopyFrom(data, size, 0);

    // Create device-local index buffer
    auto indexBuffer = CreateBufferInternal(size, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_INDEX_BUFFER_BIT,
                                            VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);

    if (!indexBuffer) {
        return nullptr;
    }

    // Copy from staging to index buffer
    CopyBuffer(stagingBuffer->GetBuffer(), indexBuffer->GetBuffer(), size);

    return indexBuffer;
}

std::shared_ptr<BufferUploadTicket> VkResourceManager::BeginBufferUpload(const rhi::BufferUploadRequest &request)
{
    const void *data = request.data;
    const VkDeviceSize size = static_cast<VkDeviceSize>(request.byteSize);
    const VkBufferUsageFlags finalUsage = rhi::ToVkBufferUsage(request.usage);
    if (m_device == VK_NULL_HANDLE || m_vmaAllocator == VK_NULL_HANDLE)
        throw std::logic_error("GPU buffer upload requires an initialized resource manager");
    if (!data || size == 0)
        throw std::invalid_argument("GPU buffer upload requires non-empty source data");
    if (finalUsage == 0)
        throw std::invalid_argument("GPU buffer upload has no supported destination usage");
    auto ticket = std::make_shared<BufferUploadTicket>();
    ticket->m_manager = this;
    ticket->m_size = size;
    ticket->m_staging = AcquireStagingBuffer(size);
    if (!ticket->m_staging)
        throw std::runtime_error("failed to allocate GPU upload staging buffer");
    ticket->m_staging->CopyFrom(data, size, 0);

    std::vector<uint32_t> queueFamilies;
    const bool canSubmitAsync = m_asyncTransfer && m_asyncTransfer->IsAsyncCapable() &&
                                m_asyncTransfer->GetTimelineSemaphore() != VK_NULL_HANDLE;
    if (canSubmitAsync)
        queueFamilies = {m_graphicsQueueFamily, m_asyncTransfer->GetQueueFamily()};
    ticket->m_destination =
        std::shared_ptr<VkBufferHandle>(CreateBufferInternal(size, VK_BUFFER_USAGE_TRANSFER_DST_BIT | finalUsage,
                                                             VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, queueFamilies)
                                            .release());
    if (!ticket->m_destination)
        throw std::runtime_error("failed to allocate GPU upload destination buffer");
    ++m_bufferUploadSubmissionCount;

    if (!canSubmitAsync) {
        CopyBuffer(ticket->m_staging->GetBuffer(), ticket->m_destination->GetBuffer(), size);
        RecycleStagingBuffer(std::move(ticket->m_staging));
        ticket->m_complete = true;
        ticket->m_published = true;
        return ticket;
    }

    VkCommandBuffer commandBuffer = m_asyncTransfer->Begin();
    if (commandBuffer == VK_NULL_HANDLE)
        throw std::runtime_error("failed to begin asynchronous GPU buffer upload");
    VkBufferCopy copy{};
    copy.size = size;
    vkCmdCopyBuffer(commandBuffer, ticket->m_staging->GetBuffer(), ticket->m_destination->GetBuffer(), 1, &copy);
    ticket->m_upload = m_asyncTransfer->EndAsync(commandBuffer);
    if (!ticket->m_upload.IsValid())
        throw std::runtime_error("failed to submit asynchronous GPU buffer upload");
    ticket->m_uploadTimelineValue = ticket->m_upload.timelineValue;
    ticket->m_async = true;
    m_pendingBufferUploads.push_back(ticket);
    return ticket;
}

bool VkResourceManager::TryPublishBufferUpload(const std::shared_ptr<BufferUploadTicket> &ticket)
{
    if (!ticket || ticket->m_manager != this)
        throw std::invalid_argument("GPU buffer upload ticket belongs to another resource manager");
    if (ticket->m_published)
        return true;
    const auto publishTimelineDependency = [this, &ticket] {
        if (ticket->m_uploadTimelineValue == 0 || !m_asyncTransfer ||
            m_asyncTransfer->GetTimelineSemaphore() == VK_NULL_HANDLE)
            return;
        m_requiredUploadTimelineValue = std::max(m_requiredUploadTimelineValue, ticket->m_uploadTimelineValue);
        ++m_timelineUploadPublicationCount;
        ticket->m_uploadTimelineValue = 0;
    };
    if (ticket->m_complete) {
        publishTimelineDependency();
        ticket->m_published = true;
        return true;
    }
    if (!ticket->m_upload.IsValid() || !m_asyncTransfer)
        throw std::logic_error("GPU buffer upload ticket has no live transfer submission");
    if (ticket->m_uploadTimelineValue != 0 && m_asyncTransfer->GetTimelineSemaphore() != VK_NULL_HANDLE) {
        publishTimelineDependency();
        ticket->m_published = true;
        return true;
    }
    if (!m_asyncTransfer->IsComplete(ticket->m_upload))
        return false;

    ticket->m_upload = {};
    RecycleStagingBuffer(std::move(ticket->m_staging));
    ticket->m_complete = true;
    ticket->m_published = true;
    m_pendingBufferUploads.erase(std::remove(m_pendingBufferUploads.begin(), m_pendingBufferUploads.end(), ticket),
                                 m_pendingBufferUploads.end());
    return true;
}

std::shared_ptr<rhi::BufferResource>
VkResourceManager::GetPublishedRhiBuffer(const std::shared_ptr<BufferUploadTicket> &ticket)
{
    if (!ticket || ticket->m_manager != this)
        throw std::invalid_argument("GPU buffer upload ticket belongs to another resource manager");
    if (!ticket->m_published || !ticket->m_destination)
        throw std::logic_error("GPU buffer upload has not been published");
    auto resource = ticket->m_rhiBuffer.lock();
    if (!resource) {
        if (!m_rhiDevice)
            throw std::logic_error("GPU buffer upload has no RHI device");
        const auto handle = m_rhiDevice->RegisterBuffer(ticket->m_destination->GetBuffer(), ticket->m_size);
        if (!handle.IsValid())
            throw std::runtime_error("failed to register uploaded GPU buffer with the RHI device");
        resource = std::make_shared<rhi::BufferResource>(*m_rhiDevice, handle, ticket->m_size, ticket->m_destination);
        ticket->m_rhiBuffer = resource;
    }
    return resource;
}

void VkResourceManager::DrainBufferUploads() noexcept
{
    if (m_asyncTransfer) {
        for (const auto &ticket : m_pendingBufferUploads) {
            if (!ticket || ticket->m_complete || !ticket->m_upload.IsValid())
                continue;
            try {
                m_asyncTransfer->Wait(ticket->m_upload);
                ticket->m_upload = {};
                RecycleStagingBuffer(std::move(ticket->m_staging));
                ticket->m_complete = true;
            } catch (...) {
                INXLOG_ERROR("Failed while draining a pending GPU buffer upload");
            }
        }
    }
    m_pendingBufferUploads.clear();

    if (m_asyncTransfer) {
        for (const auto &ticket : m_pendingTextureUploads) {
            if (!ticket || ticket->m_complete || !ticket->m_upload.IsValid())
                continue;
            try {
                m_asyncTransfer->Wait(ticket->m_upload);
                ticket->m_upload = {};
                RecycleStagingBuffer(std::move(ticket->m_staging));
                ticket->m_complete = true;
            } catch (...) {
                INXLOG_ERROR("Failed while draining a pending GPU texture upload");
            }
        }
    }
    m_pendingTextureUploads.clear();
}

std::shared_ptr<TextureUploadTicket> VkResourceManager::BeginTextureUpload(const rhi::TextureUploadRequest &request)
{
    if (m_device == VK_NULL_HANDLE || !m_rhiDevice)
        throw std::logic_error("RHI texture upload requires an initialized resource manager");
    if (!rhi::HasTextureUsage(request.texture.usage, rhi::TextureUsageFlags::Sampled) ||
        !rhi::HasTextureUsage(request.texture.usage, rhi::TextureUsageFlags::TransferDestination) ||
        request.texture.samples != rhi::SampleCount::One)
        throw std::invalid_argument("RHI texture upload requires a single-sample sampled transfer destination");
    if (!request.subresources || request.subresourceCount == 0 || request.view.texture.IsValid())
        throw std::invalid_argument("RHI texture upload requires source subresources and an unbound view descriptor");

    const VkFormat format = rhi::ToVkFormat(request.texture.format);
    const TextureFormatLayout layout = GetTextureFormatLayout(format);
    const uint32_t arrayLayers =
        request.texture.dimension == rhi::TextureDimension::Texture3D ? 1U : request.texture.depthOrLayers;
    if (arrayLayers == 0 || request.texture.mipLevels == 0)
        throw std::invalid_argument("RHI texture upload dimensions are empty");

    std::vector<bool> uploaded(static_cast<size_t>(request.texture.mipLevels) * arrayLayers, false);
    std::vector<VkBufferImageCopy> regions;
    regions.reserve(request.subresourceCount);
    std::vector<VkDeviceSize> stagingOffsets;
    stagingOffsets.reserve(request.subresourceCount);
    VkDeviceSize stagingBytes = 0;
    VkDeviceSize residentBytes = 0;

    for (uint32_t index = 0; index < request.subresourceCount; ++index) {
        const auto &source = request.subresources[index];
        if (!source.data || source.byteSize == 0 || source.mipLevel >= request.texture.mipLevels ||
            source.layerCount == 0)
            throw std::invalid_argument("RHI texture upload contains an empty or out-of-range subresource");

        const uint32_t mipWidth = (std::max)(1U, request.texture.width >> source.mipLevel);
        const uint32_t mipHeight = request.texture.dimension == rhi::TextureDimension::Texture1D
                                       ? 1U
                                       : (std::max)(1U, request.texture.height >> source.mipLevel);
        const uint32_t mipDepth = request.texture.dimension == rhi::TextureDimension::Texture3D
                                      ? (std::max)(1U, request.texture.depthOrLayers >> source.mipLevel)
                                      : 1U;
        if (source.width != mipWidth || source.height != mipHeight || source.depth != mipDepth)
            throw std::invalid_argument("RHI texture upload subresource extent does not match its mip level");
        if (request.texture.dimension == rhi::TextureDimension::Texture3D) {
            if (source.baseLayer != 0 || source.layerCount != 1)
                throw std::invalid_argument("RHI Texture3D uploads cannot address array layers");
        } else if (source.baseLayer >= arrayLayers || source.layerCount > arrayLayers - source.baseLayer) {
            throw std::invalid_argument("RHI texture upload array layer range is out of bounds");
        }

        const uint64_t blockColumns = (mipWidth + layout.blockWidth - 1) / layout.blockWidth;
        const uint64_t blockRows = (mipHeight + layout.blockHeight - 1) / layout.blockHeight;
        const uint64_t rowPitch = blockColumns * layout.bytesPerBlock;
        const uint64_t slicePitch = rowPitch * blockRows;
        const uint64_t expectedBytes = slicePitch * mipDepth * source.layerCount;
        if (source.rowPitch != rowPitch || source.slicePitch != slicePitch || source.byteSize != expectedBytes)
            throw std::invalid_argument("RHI texture upload subresource byte layout is not tightly packed");

        for (uint32_t layer = 0; layer < source.layerCount; ++layer) {
            const size_t coverage = static_cast<size_t>(source.mipLevel) * arrayLayers + source.baseLayer + layer;
            if (uploaded[coverage])
                throw std::invalid_argument("RHI texture upload contains overlapping subresources");
            uploaded[coverage] = true;
        }

        stagingBytes = AlignUploadOffset(stagingBytes);
        stagingOffsets.push_back(stagingBytes);
        if (source.byteSize > std::numeric_limits<VkDeviceSize>::max() - stagingBytes ||
            source.byteSize > std::numeric_limits<VkDeviceSize>::max() - residentBytes)
            throw std::overflow_error("RHI texture upload byte size overflow");
        stagingBytes += source.byteSize;
        residentBytes += source.byteSize;

        VkBufferImageCopy region{};
        region.bufferOffset = stagingOffsets.back();
        region.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, source.mipLevel, source.baseLayer, source.layerCount};
        region.imageExtent = {source.width, source.height, source.depth};
        regions.push_back(region);
    }
    if (std::find(uploaded.begin(), uploaded.end(), false) != uploaded.end())
        throw std::invalid_argument("RHI texture upload must initialize every mip and array layer");

    auto ticket = std::make_shared<TextureUploadTicket>();
    ticket->m_manager = this;
    ticket->m_residentBytes = residentBytes;
    ticket->m_staging = AcquireStagingBuffer(stagingBytes);
    if (!ticket->m_staging)
        throw std::runtime_error("failed to allocate RHI texture staging buffer");
    for (uint32_t index = 0; index < request.subresourceCount; ++index)
        ticket->m_staging->CopyFrom(request.subresources[index].data, request.subresources[index].byteSize,
                                    stagingOffsets[index]);

    const rhi::TextureHandle texture = m_rhiDevice->CreateTexture(request.texture);
    if (!texture.IsValid())
        throw std::runtime_error("failed to allocate RHI texture");
    rhi::TextureViewDesc viewDesc = request.view;
    viewDesc.texture = texture;
    const rhi::TextureViewHandle view = m_rhiDevice->CreateTextureView(viewDesc);
    if (!view.IsValid()) {
        m_rhiDevice->Release(texture);
        throw std::runtime_error("failed to allocate RHI texture view");
    }
    const rhi::SamplerHandle sampler = m_rhiDevice->CreateSampler(request.sampler);
    if (!sampler.IsValid()) {
        m_rhiDevice->Release(view);
        m_rhiDevice->Release(texture);
        throw std::runtime_error("failed to allocate RHI texture sampler");
    }
    ticket->m_texture = std::make_shared<rhi::TextureResource>(*m_rhiDevice, texture, view, sampler, residentBytes,
                                                               request.texture.format, viewDesc);

    const bool canSubmitAsync = m_asyncTransfer && m_asyncTransfer->IsAsyncCapable() &&
                                m_asyncTransfer->GetTimelineSemaphore() != VK_NULL_HANDLE;
    VkCommandBuffer commandBuffer = canSubmitAsync ? m_asyncTransfer->Begin() : BeginSingleTimeCommands();
    if (commandBuffer == VK_NULL_HANDLE)
        throw std::runtime_error("failed to begin RHI texture upload");
    const VkImage image = m_rhiDevice->Resolve(texture);
    if (image == VK_NULL_HANDLE)
        throw std::logic_error("RHI texture upload lost its Vulkan image");

    VkImageMemoryBarrier barrier{};
    barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    barrier.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.image = image;
    barrier.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, request.texture.mipLevels, 0, arrayLayers};
    barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                         nullptr, 0, nullptr, 1, &barrier);
    vkCmdCopyBufferToImage(commandBuffer, ticket->m_staging->GetBuffer(), image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                           static_cast<uint32_t>(regions.size()), regions.data());

    barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    barrier.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    barrier.dstAccessMask = canSubmitAsync ? 0 : VK_ACCESS_SHADER_READ_BIT;
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         canSubmitAsync ? VK_PIPELINE_STAGE_TRANSFER_BIT
                                        : VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                         0, 0, nullptr, 0, nullptr, 1, &barrier);

    if (canSubmitAsync) {
        ticket->m_upload = m_asyncTransfer->EndAsync(commandBuffer);
        if (!ticket->m_upload.IsValid())
            throw std::runtime_error("failed to submit asynchronous RHI texture upload");
        ticket->m_uploadTimelineValue = ticket->m_upload.timelineValue;
        ticket->m_async = true;
        m_pendingTextureUploads.push_back(ticket);
    } else {
        EndSingleTimeCommands(commandBuffer);
        RecycleStagingBuffer(std::move(ticket->m_staging));
        ticket->m_complete = true;
        ticket->m_published = true;
    }
    return ticket;
}

bool VkResourceManager::TryPublishTextureUpload(const std::shared_ptr<TextureUploadTicket> &ticket)
{
    if (!ticket || ticket->m_manager != this)
        throw std::invalid_argument("GPU texture upload ticket belongs to another resource manager");
    if (ticket->m_published)
        return true;
    const auto publishTimelineDependency = [this, &ticket] {
        if (ticket->m_uploadTimelineValue == 0 || !m_asyncTransfer ||
            m_asyncTransfer->GetTimelineSemaphore() == VK_NULL_HANDLE)
            return;
        m_requiredUploadTimelineValue = std::max(m_requiredUploadTimelineValue, ticket->m_uploadTimelineValue);
        ++m_timelineUploadPublicationCount;
        ticket->m_uploadTimelineValue = 0;
    };
    if (ticket->m_complete) {
        publishTimelineDependency();
        ticket->m_published = true;
        return true;
    }
    if (!ticket->m_upload.IsValid() || !m_asyncTransfer)
        throw std::logic_error("GPU texture upload ticket has no live transfer submission");
    if (ticket->m_uploadTimelineValue != 0 && m_asyncTransfer->GetTimelineSemaphore() != VK_NULL_HANDLE) {
        publishTimelineDependency();
        ticket->m_published = true;
        return true;
    }
    if (!m_asyncTransfer->IsComplete(ticket->m_upload))
        return false;
    ticket->m_upload = {};
    RecycleStagingBuffer(std::move(ticket->m_staging));
    m_pendingTextureUploads.erase(std::remove(m_pendingTextureUploads.begin(), m_pendingTextureUploads.end(), ticket),
                                  m_pendingTextureUploads.end());
    ticket->m_complete = true;
    ticket->m_published = true;
    return true;
}

void VkResourceManager::PollGpuUploads()
{
    if (!m_asyncTransfer)
        return;

    size_t bufferWriteIndex = 0;
    for (size_t index = 0; index < m_pendingBufferUploads.size(); ++index) {
        auto &ticket = m_pendingBufferUploads[index];
        if (ticket && m_asyncTransfer->IsComplete(ticket->m_upload)) {
            ticket->m_upload = {};
            RecycleStagingBuffer(std::move(ticket->m_staging));
            ticket->m_complete = true;
            continue;
        }
        if (bufferWriteIndex != index)
            m_pendingBufferUploads[bufferWriteIndex] = std::move(ticket);
        ++bufferWriteIndex;
    }
    m_pendingBufferUploads.resize(bufferWriteIndex);

    size_t textureWriteIndex = 0;
    for (size_t index = 0; index < m_pendingTextureUploads.size(); ++index) {
        auto &ticket = m_pendingTextureUploads[index];
        if (ticket && m_asyncTransfer->IsComplete(ticket->m_upload)) {
            ticket->m_upload = {};
            RecycleStagingBuffer(std::move(ticket->m_staging));
            ticket->m_complete = true;
            continue;
        }
        if (textureWriteIndex != index)
            m_pendingTextureUploads[textureWriteIndex] = std::move(ticket);
        ++textureWriteIndex;
    }
    m_pendingTextureUploads.resize(textureWriteIndex);
}

VkSemaphore VkResourceManager::GetUploadTimelineSemaphore() const noexcept
{
    return m_asyncTransfer ? m_asyncTransfer->GetTimelineSemaphore() : VK_NULL_HANDLE;
}

std::shared_ptr<ImageReadbackTicket> VkResourceManager::BeginImageReadback(VkImage image, VkImageLayout layout,
                                                                           VkImageAspectFlags aspect,
                                                                           VkPipelineStageFlags sourceStage,
                                                                           VkAccessFlags sourceAccess, uint32_t width,
                                                                           uint32_t height, VkFormat format)
{
    AssertReadbackThread();
    if (image == VK_NULL_HANDLE || width == 0 || height == 0)
        throw std::invalid_argument("GPU image readback requires a live image and non-zero dimensions");

    // Render-target captures share the graphics submission path used by the
    // material and mesh preview renderers. Besides keeping queue submission
    // externally synchronized, this path waits for the latest upload timeline
    // value and owns command-buffer/fence recycling in one place.
    auto recorder = BeginGraphicsImageReadback(width, height, format);
    VkCommandBuffer commandBuffer = recorder.GetCommandBuffer();

    VkImageMemoryBarrier barrier{};
    barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    barrier.oldLayout = layout;
    barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.image = image;
    barrier.subresourceRange.aspectMask = aspect;
    barrier.subresourceRange.baseMipLevel = 0;
    barrier.subresourceRange.levelCount = 1;
    barrier.subresourceRange.baseArrayLayer = 0;
    barrier.subresourceRange.layerCount = 1;
    barrier.srcAccessMask = sourceAccess;
    barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    vkCmdPipelineBarrier(commandBuffer, sourceStage, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1,
                         &barrier);

    VkBufferImageCopy region{};
    region.imageSubresource.aspectMask = aspect;
    region.imageSubresource.mipLevel = 0;
    region.imageSubresource.baseArrayLayer = 0;
    region.imageSubresource.layerCount = 1;
    region.imageExtent = {width, height, 1};
    vkCmdCopyImageToBuffer(commandBuffer, image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, recorder.GetStagingBuffer(), 1,
                           &region);

    barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    barrier.newLayout = layout;
    barrier.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    barrier.dstAccessMask = sourceAccess;
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_TRANSFER_BIT, sourceStage, 0, 0, nullptr, 0, nullptr, 1,
                         &barrier);

    return recorder.Submit();
}

GraphicsImageReadbackRecorder VkResourceManager::BeginGraphicsImageReadback(uint32_t width, uint32_t height,
                                                                            VkFormat format)
{
    AssertReadbackThread();
    if (width == 0 || height == 0)
        throw std::invalid_argument("Graphics image readback requires non-zero dimensions");

    const ReadbackFormatInfo formatInfo = GetReadbackFormatInfo(format);
    const uint64_t pixelCount = static_cast<uint64_t>(width) * height;
    if (pixelCount > std::numeric_limits<size_t>::max() / formatInfo.bytesPerPixel)
        throw std::overflow_error("Graphics image readback byte size overflow");

    auto ticket = std::make_shared<ImageReadbackTicket>();
    ticket->m_width = width;
    ticket->m_height = height;
    ticket->m_channelCount = formatInfo.channels;
    ticket->m_elementType = formatInfo.elementType;
    ticket->m_byteSize = static_cast<size_t>(pixelCount * formatInfo.bytesPerPixel);
    ticket->m_staging = AcquireStagingBuffer(ticket->m_byteSize);
    if (!ticket->m_staging)
        throw std::runtime_error("Failed to allocate graphics image readback staging buffer");

    VkCommandBuffer commandBuffer = BeginSingleTimeCommands();
    if (commandBuffer == VK_NULL_HANDLE) {
        RecycleStagingBuffer(std::move(ticket->m_staging));
        throw std::runtime_error("Failed to begin graphics image readback command buffer");
    }

    GraphicsImageReadbackRecorder recorder;
    recorder.m_manager = this;
    recorder.m_ticket = std::move(ticket);
    recorder.m_commandBuffer = commandBuffer;
    return recorder;
}

std::shared_ptr<ImageReadbackTicket>
VkResourceManager::SubmitGraphicsImageReadback(GraphicsImageReadbackRecorder &recorder,
                                               std::function<void()> releaseResources)
{
    AssertReadbackThread();
    if (recorder.m_manager != this || !recorder.m_ticket || recorder.m_commandBuffer == VK_NULL_HANDLE)
        throw std::invalid_argument("Graphics image readback recorder belongs to another resource manager");

    auto ticket = std::move(recorder.m_ticket);
    const VkCommandBuffer commandBuffer = recorder.m_commandBuffer;
    recorder.m_manager = nullptr;
    recorder.m_commandBuffer = VK_NULL_HANDLE;

    try {
        ticket->m_graphicsSubmission = EndSingleTimeCommandsAsync(commandBuffer, std::move(releaseResources));
    } catch (...) {
        RecycleStagingBuffer(std::move(ticket->m_staging));
        throw;
    }
    m_pendingImageReadbacks.push_back(ticket);
    return ticket;
}

void VkResourceManager::AbandonGraphicsImageReadback(GraphicsImageReadbackRecorder &recorder) noexcept
{
    auto ticket = std::move(recorder.m_ticket);
    const VkCommandBuffer commandBuffer = recorder.m_commandBuffer;
    recorder.m_manager = nullptr;
    recorder.m_commandBuffer = VK_NULL_HANDLE;

    if (commandBuffer != VK_NULL_HANDLE) {
        rhi::SubmissionSerial completionEpoch = rhi::InvalidSubmissionSerial;
        {
            std::lock_guard<std::mutex> guard(m_singleTimeMutex);
            const auto found = m_singleTimeCompletionEpochs.find(commandBuffer);
            if (found != m_singleTimeCompletionEpochs.end()) {
                completionEpoch = found->second;
                m_singleTimeCompletionEpochs.erase(found);
            }
        }
        if (completionEpoch != rhi::InvalidSubmissionSerial) {
            vkdebug::PopDescriptorRecordingContext();
            if (m_queueManager)
                m_queueManager->CompleteCompletionEpoch(completionEpoch);
        }
        vkResetCommandBuffer(commandBuffer, 0);
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        m_freeSingleTimeCmdBuffers.push_back(commandBuffer);
    }
    if (ticket)
        RecycleStagingBuffer(std::move(ticket->m_staging));
}

void VkResourceManager::FinalizeImageReadback(const std::shared_ptr<ImageReadbackTicket> &ticket) noexcept
{
    if (!ticket)
        return;
    if (ticket->GetStatus() == ImageReadbackStatus::Pending) {
        void *mapped = ticket->m_staging ? ticket->m_staging->Map() : nullptr;
        if (mapped) {
            ticket->m_data.resize(ticket->m_byteSize);
            std::memcpy(ticket->m_data.data(), mapped, ticket->m_byteSize);
            ticket->m_staging->Unmap();
            ticket->m_status.store(ImageReadbackStatus::Completed, std::memory_order_release);
        } else {
            ticket->m_error = "Failed to map GPU image readback staging buffer";
            ticket->m_status.store(ImageReadbackStatus::Failed, std::memory_order_release);
        }
    }
    RecycleStagingBuffer(std::move(ticket->m_staging));
    ticket->m_submission = {};
    ticket->m_graphicsSubmission.reset();
}

void VkResourceManager::PollImageReadbacks()
{
    AssertReadbackThread();
    size_t writeIndex = 0;
    for (size_t index = 0; index < m_pendingImageReadbacks.size(); ++index) {
        auto &ticket = m_pendingImageReadbacks[index];
        const bool graphicsComplete =
            ticket && ticket->m_graphicsSubmission && ticket->m_graphicsSubmission->IsComplete();
        const bool transferComplete = ticket && !ticket->m_graphicsSubmission && m_asyncReadback &&
                                      m_asyncReadback->IsComplete(ticket->m_submission);
        if (graphicsComplete || transferComplete) {
            FinalizeImageReadback(ticket);
            continue;
        }
        if (writeIndex != index)
            m_pendingImageReadbacks[writeIndex] = std::move(ticket);
        ++writeIndex;
    }
    m_pendingImageReadbacks.resize(writeIndex);
}

void VkResourceManager::DrainImageReadbacks() noexcept
{
    if (std::any_of(m_pendingImageReadbacks.begin(), m_pendingImageReadbacks.end(), [](const auto &ticket) {
            return ticket && ticket->m_graphicsSubmission && !ticket->m_graphicsSubmission->IsComplete();
        }))
        DrainAsyncGraphicsSubmissions();

    for (const auto &ticket : m_pendingImageReadbacks) {
        if (!ticket)
            continue;
        if (ticket->m_graphicsSubmission && ticket->m_graphicsSubmission->IsComplete()) {
            FinalizeImageReadback(ticket);
            continue;
        }
        if (m_asyncReadback && ticket->m_submission.IsValid()) {
            try {
                m_asyncReadback->Wait(ticket->m_submission);
                FinalizeImageReadback(ticket);
            } catch (...) {
                ticket->m_error = "Failed while draining a pending GPU image readback";
                ticket->m_status.store(ImageReadbackStatus::Failed, std::memory_order_release);
            }
        }
    }
    m_pendingImageReadbacks.clear();
}

uint64_t VkResourceManager::GetPendingImageReadbackBytes() const noexcept
{
    uint64_t bytes = 0;
    for (const auto &ticket : m_pendingImageReadbacks) {
        if (ticket)
            bytes += ticket->m_byteSize;
    }
    return bytes;
}

void VkResourceManager::AssertReadbackThread() const
{
    if (std::this_thread::get_id() != m_ownerThread)
        throw std::logic_error("GPU image readback submission and polling require the renderer owner thread");
}

std::unique_ptr<VkBufferHandle> VkResourceManager::CreateUniformBuffer(VkDeviceSize size)
{
    // TRANSFER_DST_BIT is required for vkCmdUpdateBuffer (multi-camera UBO updates)
    return CreateBufferInternal(size, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
}

std::unique_ptr<VkBufferHandle> VkResourceManager::CreateStorageBuffer(VkDeviceSize size, bool deviceLocal)
{
    VkMemoryPropertyFlags properties =
        deviceLocal ? VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT
                    : (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);

    VkBufferUsageFlags usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    if (deviceLocal) {
        usage |= VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    }

    return CreateBufferInternal(size, usage, properties);
}

std::unique_ptr<VkBufferHandle> VkResourceManager::CreateStagingBuffer(VkDeviceSize size)
{
    return CreateBufferInternal(size, VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
}

std::shared_ptr<VkBufferHandle> VkResourceManager::AcquireStagingBuffer(VkDeviceSize size)
{
    if (size == 0)
        throw std::invalid_argument("staging buffer acquisition requires a non-zero size");
    const VkDeviceSize sizeClass = StagingClassFor(size);
    if (sizeClass <= MaximumPooledStagingClass) {
        auto found = m_stagingPool.find(sizeClass);
        if (found != m_stagingPool.end() && !found->second.empty()) {
            auto buffer = std::move(found->second.back());
            found->second.pop_back();
            if (found->second.empty())
                m_stagingPool.erase(found);
            m_stagingPoolBytes -= sizeClass;
            --m_stagingPoolBufferCount;
            ++m_stagingReuseCount;
            return buffer;
        }
    }

    auto buffer = CreateStagingBuffer(sizeClass);
    if (!buffer)
        return {};
    ++m_stagingAllocationCount;
    return std::shared_ptr<VkBufferHandle>(std::move(buffer));
}

void VkResourceManager::RecycleStagingBuffer(std::shared_ptr<VkBufferHandle> buffer) noexcept
{
    if (!buffer || !buffer->IsValid())
        return;
    const VkDeviceSize sizeClass = buffer->GetSize();
    if (sizeClass > MaximumPooledStagingClass || sizeClass > StagingPoolBudget - m_stagingPoolBytes ||
        buffer.use_count() != 1) {
        ++m_stagingDiscardCount;
        return;
    }
    m_stagingPool[sizeClass].push_back(std::move(buffer));
    m_stagingPoolBytes += sizeClass;
    ++m_stagingPoolBufferCount;
}

void VkResourceManager::ClearStagingPool() noexcept
{
    m_stagingPool.clear();
    m_stagingPoolBytes = 0;
    m_stagingPoolBufferCount = 0;
}

void VkResourceManager::CopyBuffer(VkBuffer srcBuffer, VkBuffer dstBuffer, VkDeviceSize size)
{
    VkCommandBuffer cmdBuffer = BeginSingleTimeCommands();

    VkBufferCopy copyRegion{};
    copyRegion.size = size;
    vkCmdCopyBuffer(cmdBuffer, srcBuffer, dstBuffer, 1, &copyRegion);

    EndSingleTimeCommands(cmdBuffer);
}

std::unique_ptr<VkBufferHandle> VkResourceManager::CreateBufferInternal(VkDeviceSize size, VkBufferUsageFlags usage,
                                                                        VkMemoryPropertyFlags properties,
                                                                        const std::vector<uint32_t> &queueFamilies)
{
    auto buffer = std::make_unique<VkBufferHandle>();
    if (!buffer->Create(m_vmaAllocator, m_device, size, usage, properties, queueFamilies, m_deviceLifetime)) {
        return nullptr;
    }
    return buffer;
}

// ============================================================================
// Image and Texture Management
// ============================================================================

std::unique_ptr<VkImageHandle> VkResourceManager::CreateImage(uint32_t width, uint32_t height, VkFormat format,
                                                              VkImageUsageFlags usage, VkMemoryPropertyFlags properties)
{
    auto image = std::make_unique<VkImageHandle>();
    if (!image->Create(m_vmaAllocator, m_device, width, height, format, VK_IMAGE_TILING_OPTIMAL, usage, properties,
                       VK_SAMPLE_COUNT_1_BIT, 1, m_deviceLifetime)) {
        return nullptr;
    }
    return image;
}

std::unique_ptr<VkImageHandle> VkResourceManager::CreateDepthBuffer(uint32_t width, uint32_t height, VkFormat format)
{
    if (format == VK_FORMAT_UNDEFINED) {
        format = FindDepthFormat();
    }

    auto depthImage = CreateImage(width, height, format, VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT,
                                  VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);

    if (!depthImage) {
        return nullptr;
    }

    // Create image view
    if (!depthImage->CreateView(format, rhi::ToVkImageAspectMask(format), 1)) {
        return nullptr;
    }

    return depthImage;
}

void VkResourceManager::TransitionImageLayout(VkImage image, VkFormat format, VkImageLayout oldLayout,
                                              VkImageLayout newLayout)
{
    VkCommandBuffer cmdBuffer = BeginSingleTimeCommands();

    VkImageMemoryBarrier barrier{};
    barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    barrier.oldLayout = oldLayout;
    barrier.newLayout = newLayout;
    barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.image = image;
    barrier.subresourceRange.baseMipLevel = 0;
    barrier.subresourceRange.levelCount = 1;
    barrier.subresourceRange.baseArrayLayer = 0;
    barrier.subresourceRange.layerCount = 1;

    if (newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL) {
        barrier.subresourceRange.aspectMask = rhi::ToVkImageAspectMask(format);
    } else {
        barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    }

    VkPipelineStageFlags srcStage;
    VkPipelineStageFlags dstStage;

    if (oldLayout == VK_IMAGE_LAYOUT_UNDEFINED && newLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL) {
        barrier.srcAccessMask = 0;
        barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        srcStage = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        dstStage = VK_PIPELINE_STAGE_TRANSFER_BIT;
    } else if (oldLayout == VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL &&
               newLayout == VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL) {
        barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        barrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
        srcStage = VK_PIPELINE_STAGE_TRANSFER_BIT;
        dstStage = VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;
    } else if (oldLayout == VK_IMAGE_LAYOUT_UNDEFINED &&
               newLayout == VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL) {
        barrier.srcAccessMask = 0;
        barrier.dstAccessMask =
            VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
        srcStage = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        dstStage = VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT;
    } else {
        INXLOG_WARN("Unsupported layout transition");
        srcStage = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
        dstStage = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    }

    vkCmdPipelineBarrier(cmdBuffer, srcStage, dstStage, 0, 0, nullptr, 0, nullptr, 1, &barrier);

    EndSingleTimeCommands(cmdBuffer);
}

void VkResourceManager::CopyBufferToImage(VkBuffer buffer, VkImage image, uint32_t width, uint32_t height)
{
    VkCommandBuffer cmdBuffer = BeginSingleTimeCommands();

    VkBufferImageCopy region{};
    region.bufferOffset = 0;
    region.bufferRowLength = 0;
    region.bufferImageHeight = 0;
    region.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    region.imageSubresource.mipLevel = 0;
    region.imageSubresource.baseArrayLayer = 0;
    region.imageSubresource.layerCount = 1;
    region.imageOffset = {0, 0, 0};
    region.imageExtent = {width, height, 1};

    vkCmdCopyBufferToImage(cmdBuffer, buffer, image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);

    EndSingleTimeCommands(cmdBuffer);
}

VkFormat VkResourceManager::FindDepthFormat() const
{
    std::vector<VkFormat> candidates = {VK_FORMAT_D32_SFLOAT, VK_FORMAT_D32_SFLOAT_S8_UINT,
                                        VK_FORMAT_D24_UNORM_S8_UINT};

    for (VkFormat format : candidates) {
        VkFormatProperties props;
        vkGetPhysicalDeviceFormatProperties(m_physicalDevice, format, &props);

        if (props.optimalTilingFeatures & VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT) {
            return format;
        }
    }

    INXLOG_ERROR("Failed to find supported depth format");
    return VK_FORMAT_D32_SFLOAT;
}

bool VkResourceManager::HasStencilComponent(VkFormat format)
{
    return format == VK_FORMAT_D32_SFLOAT_S8_UINT || format == VK_FORMAT_D24_UNORM_S8_UINT;
}

// ============================================================================
// Command Buffer Management
// ============================================================================

CommandBufferAllocation VkResourceManager::AllocatePrimaryCommandBuffer()
{
    VkCommandBufferAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocInfo.commandPool = m_commandPool;
    allocInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocInfo.commandBufferCount = 1;

    CommandBufferAllocation allocation;
    allocation.pool = m_commandPool;

    if (vkAllocateCommandBuffers(m_device, &allocInfo, &allocation.cmdBuffer) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to allocate primary command buffer");
        return {};
    }

    return allocation;
}

CommandBufferAllocation VkResourceManager::AllocateSecondaryCommandBuffer()
{
    VkCommandBufferAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocInfo.commandPool = m_commandPool;
    allocInfo.level = VK_COMMAND_BUFFER_LEVEL_SECONDARY;
    allocInfo.commandBufferCount = 1;

    CommandBufferAllocation allocation;
    allocation.pool = m_commandPool;

    if (vkAllocateCommandBuffers(m_device, &allocInfo, &allocation.cmdBuffer) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to allocate secondary command buffer");
        return {};
    }

    return allocation;
}

void VkResourceManager::FreeCommandBuffer(const CommandBufferAllocation &allocation)
{
    if (allocation.cmdBuffer != VK_NULL_HANDLE && allocation.pool != VK_NULL_HANDLE) {
        vkFreeCommandBuffers(m_device, allocation.pool, 1, &allocation.cmdBuffer);
    }
}

VkCommandBuffer VkResourceManager::BeginSingleTimeCommands()
{
    // ────────────────────────────────────────────────────────────────────
    // Phase 5b: pool command buffers and fences instead of churning them
    // per upload. Hot path now hits zero kernel allocations after the
    // first few warm-up uploads — see VkResourceManager.h for rationale.
    // ────────────────────────────────────────────────────────────────────
    VkCommandBuffer cmdBuffer = VK_NULL_HANDLE;
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        if (!m_freeSingleTimeCmdBuffers.empty()) {
            cmdBuffer = m_freeSingleTimeCmdBuffers.back();
            m_freeSingleTimeCmdBuffers.pop_back();
        }
    }

    if (cmdBuffer == VK_NULL_HANDLE) {
        VkCommandBufferAllocateInfo allocInfo{};
        allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
        allocInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        allocInfo.commandPool = m_commandPool;
        allocInfo.commandBufferCount = 1;
        if (vkAllocateCommandBuffers(m_device, &allocInfo, &cmdBuffer) != VK_SUCCESS)
            return VK_NULL_HANDLE;

        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        m_allSingleTimeCmdBuffers.push_back(cmdBuffer);
    } else {
        // Recycled buffer carries left-over recording state — reset before reuse.
        if (vkResetCommandBuffer(cmdBuffer, 0) != VK_SUCCESS)
            return VK_NULL_HANDLE;
    }

    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;

    if (vkBeginCommandBuffer(cmdBuffer, &beginInfo) != VK_SUCCESS) {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        m_freeSingleTimeCmdBuffers.push_back(cmdBuffer);
        return VK_NULL_HANDLE;
    }

    if (m_queueManager) {
        const auto epoch = m_queueManager->ReserveCompletionEpoch();
        if (epoch == rhi::InvalidSubmissionSerial) {
            (void)vkEndCommandBuffer(cmdBuffer);
            std::lock_guard<std::mutex> guard(m_singleTimeMutex);
            m_freeSingleTimeCmdBuffers.push_back(cmdBuffer);
            return VK_NULL_HANDLE;
        }
        {
            std::lock_guard<std::mutex> guard(m_singleTimeMutex);
            m_singleTimeCompletionEpochs[cmdBuffer] = epoch;
        }
        vkdebug::PushDescriptorRecordingContext(&m_rhiDevice->GetDescriptorManager(), epoch);
    }

    return cmdBuffer;
}

void VkResourceManager::EndSingleTimeCommands(VkCommandBuffer cmdBuffer)
{
    rhi::SubmissionSerial completionEpoch = rhi::InvalidSubmissionSerial;
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        const auto found = m_singleTimeCompletionEpochs.find(cmdBuffer);
        if (found != m_singleTimeCompletionEpochs.end()) {
            completionEpoch = found->second;
            m_singleTimeCompletionEpochs.erase(found);
        }
    }
    if (completionEpoch != rhi::InvalidSubmissionSerial)
        vkdebug::PopDescriptorRecordingContext();
    const auto finishEpoch = [&] {
        if (m_queueManager && completionEpoch != rhi::InvalidSubmissionSerial)
            m_queueManager->CompleteCompletionEpoch(completionEpoch);
    };
    if (vkEndCommandBuffer(cmdBuffer) != VK_SUCCESS) {
        finishEpoch();
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        m_freeSingleTimeCmdBuffers.push_back(cmdBuffer);
        return;
    }

    // Acquire (or lazily create) a recycled fence.
    VkFence submitFence = VK_NULL_HANDLE;
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        if (!m_freeSingleTimeFences.empty()) {
            submitFence = m_freeSingleTimeFences.back();
            m_freeSingleTimeFences.pop_back();
        }
    }
    if (submitFence == VK_NULL_HANDLE) {
        VkFenceCreateInfo fenceInfo{};
        fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        if (vkCreateFence(m_device, &fenceInfo, nullptr, &submitFence) != VK_SUCCESS) {
            INXLOG_ERROR("VkResourceManager::EndSingleTimeCommands: vkCreateFence failed");
            finishEpoch();
            std::lock_guard<std::mutex> guard(m_singleTimeMutex);
            m_freeSingleTimeCmdBuffers.push_back(cmdBuffer);
            return;
        }
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        m_allSingleTimeFences.push_back(submitFence);
    } else {
        vkResetFences(m_device, 1, &submitFence);
    }

    VkSubmitInfo submitInfo{};
    submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submitInfo.commandBufferCount = 1;
    submitInfo.pCommandBuffers = &cmdBuffer;

    VkTimelineSemaphoreSubmitInfo timelineSubmit{};
    VkSemaphore uploadTimeline = GetUploadTimelineSemaphore();
    VkPipelineStageFlags uploadWaitStage = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    uint64_t uploadValue = GetRequiredUploadTimelineValue();
    if (uploadTimeline != VK_NULL_HANDLE && uploadValue != 0) {
        timelineSubmit.sType = VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;
        timelineSubmit.waitSemaphoreValueCount = 1;
        timelineSubmit.pWaitSemaphoreValues = &uploadValue;
        submitInfo.pNext = &timelineSubmit;
        submitInfo.waitSemaphoreCount = 1;
        submitInfo.pWaitSemaphores = &uploadTimeline;
        submitInfo.pWaitDstStageMask = &uploadWaitStage;
    }

    const auto submission =
        m_queueManager ? m_queueManager->Reserve(rhi::QueueRole::Graphics) : rhi::SubmissionTicket{};
    const VkResult submitResult = m_queueManager ? m_queueManager->SubmitReserved(submission, submitInfo, submitFence)
                                                 : vkQueueSubmit(m_graphicsQueue, 1, &submitInfo, submitFence);
    if (submitResult != VK_SUCCESS) {
        INXLOG_ERROR("VkResourceManager::EndSingleTimeCommands: graphics submit failed with VkResult ",
                     static_cast<int>(submitResult));
        {
            std::lock_guard<std::mutex> guard(m_singleTimeMutex);
            m_freeSingleTimeFences.push_back(submitFence);
            m_freeSingleTimeCmdBuffers.push_back(cmdBuffer);
        }
        finishEpoch();
        return;
    }
    WaitForFencePumpingEvents(m_device, submitFence);
    if (m_queueManager && submission.IsValid())
        m_queueManager->MarkCompleted(submission);
    finishEpoch();

    // Return both objects to the free list — the fence is reusable after
    // vkResetFences (above) and the command buffer is reusable after the
    // GPU has signalled (which is what WaitForFencePumpingEvents proved).
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        m_freeSingleTimeFences.push_back(submitFence);
        m_freeSingleTimeCmdBuffers.push_back(cmdBuffer);
    }
}

std::shared_ptr<GraphicsSubmissionTicket>
VkResourceManager::EndSingleTimeCommandsAsync(VkCommandBuffer cmdBuffer, std::function<void()> releaseResources)
{
    AssertReadbackThread();
    if (cmdBuffer == VK_NULL_HANDLE)
        throw std::invalid_argument("Asynchronous graphics submission requires a live command buffer");

    rhi::SubmissionSerial completionEpoch = rhi::InvalidSubmissionSerial;
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        const auto found = m_singleTimeCompletionEpochs.find(cmdBuffer);
        if (found != m_singleTimeCompletionEpochs.end()) {
            completionEpoch = found->second;
            m_singleTimeCompletionEpochs.erase(found);
        }
    }
    if (completionEpoch != rhi::InvalidSubmissionSerial)
        vkdebug::PopDescriptorRecordingContext();
    auto recycleUnsubmitted = [&](VkFence fence = VK_NULL_HANDLE) {
        {
            std::lock_guard<std::mutex> guard(m_singleTimeMutex);
            if (fence != VK_NULL_HANDLE)
                m_freeSingleTimeFences.push_back(fence);
            m_freeSingleTimeCmdBuffers.push_back(cmdBuffer);
        }
        if (m_queueManager && completionEpoch != rhi::InvalidSubmissionSerial)
            m_queueManager->CompleteCompletionEpoch(completionEpoch);
    };
    if (vkEndCommandBuffer(cmdBuffer) != VK_SUCCESS) {
        recycleUnsubmitted();
        throw std::runtime_error("Failed to end asynchronous graphics command buffer");
    }

    VkFence submitFence = VK_NULL_HANDLE;
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        if (!m_freeSingleTimeFences.empty()) {
            submitFence = m_freeSingleTimeFences.back();
            m_freeSingleTimeFences.pop_back();
        }
    }
    if (submitFence == VK_NULL_HANDLE) {
        VkFenceCreateInfo fenceInfo{};
        fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        if (vkCreateFence(m_device, &fenceInfo, nullptr, &submitFence) != VK_SUCCESS) {
            recycleUnsubmitted();
            throw std::runtime_error("Failed to create asynchronous graphics submission fence");
        }
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        m_allSingleTimeFences.push_back(submitFence);
    } else if (vkResetFences(m_device, 1, &submitFence) != VK_SUCCESS) {
        recycleUnsubmitted(submitFence);
        throw std::runtime_error("Failed to reset asynchronous graphics submission fence");
    }

    VkSubmitInfo submitInfo{};
    submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submitInfo.commandBufferCount = 1;
    submitInfo.pCommandBuffers = &cmdBuffer;

    VkTimelineSemaphoreSubmitInfo timelineSubmit{};
    const VkSemaphore uploadTimeline = GetUploadTimelineSemaphore();
    const VkPipelineStageFlags uploadWaitStage = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    const uint64_t uploadValue = GetRequiredUploadTimelineValue();
    if (uploadTimeline != VK_NULL_HANDLE && uploadValue != 0) {
        timelineSubmit.sType = VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;
        timelineSubmit.waitSemaphoreValueCount = 1;
        timelineSubmit.pWaitSemaphoreValues = &uploadValue;
        submitInfo.pNext = &timelineSubmit;
        submitInfo.waitSemaphoreCount = 1;
        submitInfo.pWaitSemaphores = &uploadTimeline;
        submitInfo.pWaitDstStageMask = &uploadWaitStage;
    }

    const auto submission =
        m_queueManager ? m_queueManager->Reserve(rhi::QueueRole::Graphics) : rhi::SubmissionTicket{};
    const VkResult submitResult = m_queueManager ? m_queueManager->SubmitReserved(submission, submitInfo, submitFence)
                                                 : vkQueueSubmit(m_graphicsQueue, 1, &submitInfo, submitFence);
    if (submitResult != VK_SUCCESS) {
        recycleUnsubmitted(submitFence);
        throw std::runtime_error("Failed to submit asynchronous graphics command buffer");
    }
    auto ticket = std::make_shared<GraphicsSubmissionTicket>();
    ticket->m_commandBuffer = cmdBuffer;
    ticket->m_fence = submitFence;
    ticket->m_submission = submission;
    ticket->m_completionEpoch = completionEpoch;
    ticket->m_releaseResources = std::move(releaseResources);
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        m_pendingAsyncGraphicsSubmissions.push_back(ticket);
    }
    ++m_asyncGraphicsSubmissionCount;
    return ticket;
}

void VkResourceManager::PollAsyncGraphicsSubmissions()
{
    AssertReadbackThread();
    std::vector<std::shared_ptr<GraphicsSubmissionTicket>> completed;
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        size_t writeIndex = 0;
        for (size_t index = 0; index < m_pendingAsyncGraphicsSubmissions.size(); ++index) {
            auto &submission = m_pendingAsyncGraphicsSubmissions[index];
            const VkResult status = vkGetFenceStatus(m_device, submission->m_fence);
            if (status == VK_SUCCESS) {
                m_freeSingleTimeFences.push_back(submission->m_fence);
                m_freeSingleTimeCmdBuffers.push_back(submission->m_commandBuffer);
                submission->m_fence = VK_NULL_HANDLE;
                submission->m_commandBuffer = VK_NULL_HANDLE;
                completed.push_back(std::move(submission));
                continue;
            }
            if (status != VK_NOT_READY)
                throw std::runtime_error("Failed to poll asynchronous graphics submission fence");
            if (writeIndex != index)
                m_pendingAsyncGraphicsSubmissions[writeIndex] = std::move(submission);
            ++writeIndex;
        }
        m_pendingAsyncGraphicsSubmissions.resize(writeIndex);
    }

    for (const auto &submission : completed) {
        if (m_queueManager && submission->m_submission.IsValid())
            m_queueManager->MarkCompleted(submission->m_submission);
        if (m_queueManager && submission->m_completionEpoch != rhi::InvalidSubmissionSerial)
            m_queueManager->CompleteCompletionEpoch(submission->m_completionEpoch);
        try {
            if (submission->m_releaseResources)
                submission->m_releaseResources();
        } catch (const std::exception &error) {
            INXLOG_ERROR("Asynchronous graphics resource release failed: ", error.what());
        } catch (...) {
            INXLOG_ERROR("Asynchronous graphics resource release failed with an unknown exception");
        }
        submission->m_releaseResources = {};
        submission->m_complete.store(true, std::memory_order_release);
    }
}

void VkResourceManager::DrainAsyncGraphicsSubmissions() noexcept
{
    std::vector<std::shared_ptr<GraphicsSubmissionTicket>> completed;
    {
        std::lock_guard<std::mutex> guard(m_singleTimeMutex);
        for (auto &submission : m_pendingAsyncGraphicsSubmissions) {
            if (!submission || submission->m_fence == VK_NULL_HANDLE)
                continue;
            WaitForFencePumpingEvents(m_device, submission->m_fence);
            m_freeSingleTimeFences.push_back(submission->m_fence);
            m_freeSingleTimeCmdBuffers.push_back(submission->m_commandBuffer);
            submission->m_fence = VK_NULL_HANDLE;
            submission->m_commandBuffer = VK_NULL_HANDLE;
            completed.push_back(std::move(submission));
        }
        m_pendingAsyncGraphicsSubmissions.clear();
    }

    for (const auto &submission : completed) {
        if (m_queueManager && submission->m_submission.IsValid())
            m_queueManager->MarkCompleted(submission->m_submission);
        if (m_queueManager && submission->m_completionEpoch != rhi::InvalidSubmissionSerial)
            m_queueManager->CompleteCompletionEpoch(submission->m_completionEpoch);
        try {
            if (submission->m_releaseResources)
                submission->m_releaseResources();
        } catch (...) {
            INXLOG_ERROR("Asynchronous graphics resource release failed during shutdown");
        }
        submission->m_releaseResources = {};
        submission->m_complete.store(true, std::memory_order_release);
    }
}

// ============================================================================
// Sampler Management
// ============================================================================

VkSampler VkResourceManager::GetLinearSampler()
{
    if (m_linearSampler != VK_NULL_HANDLE) {
        return m_linearSampler;
    }

    VkPhysicalDeviceProperties properties{};
    vkGetPhysicalDeviceProperties(m_physicalDevice, &properties);

    VkSamplerCreateInfo samplerInfo{};
    samplerInfo.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    samplerInfo.magFilter = VK_FILTER_LINEAR;
    samplerInfo.minFilter = VK_FILTER_LINEAR;
    samplerInfo.addressModeU = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.addressModeV = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.addressModeW = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.anisotropyEnable = VK_TRUE;
    samplerInfo.maxAnisotropy = properties.limits.maxSamplerAnisotropy;
    samplerInfo.borderColor = VK_BORDER_COLOR_INT_OPAQUE_BLACK;
    samplerInfo.unnormalizedCoordinates = VK_FALSE;
    samplerInfo.compareEnable = VK_FALSE;
    samplerInfo.compareOp = VK_COMPARE_OP_ALWAYS;
    samplerInfo.mipmapMode = VK_SAMPLER_MIPMAP_MODE_LINEAR;

    if (vkCreateSampler(m_device, &samplerInfo, nullptr, &m_linearSampler) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create linear sampler");
        return VK_NULL_HANDLE;
    }

    return m_linearSampler;
}

VkSampler VkResourceManager::GetNearestSampler()
{
    if (m_nearestSampler != VK_NULL_HANDLE) {
        return m_nearestSampler;
    }

    VkSamplerCreateInfo samplerInfo{};
    samplerInfo.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    samplerInfo.magFilter = VK_FILTER_NEAREST;
    samplerInfo.minFilter = VK_FILTER_NEAREST;
    samplerInfo.addressModeU = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.addressModeV = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.addressModeW = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.anisotropyEnable = VK_FALSE;
    samplerInfo.borderColor = VK_BORDER_COLOR_INT_OPAQUE_BLACK;
    samplerInfo.unnormalizedCoordinates = VK_FALSE;
    samplerInfo.compareEnable = VK_FALSE;
    samplerInfo.compareOp = VK_COMPARE_OP_ALWAYS;
    samplerInfo.mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST;

    if (vkCreateSampler(m_device, &samplerInfo, nullptr, &m_nearestSampler) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create nearest sampler");
        return VK_NULL_HANDLE;
    }

    return m_nearestSampler;
}

VkCommandPool VkResourceManager::GetCommandPool() const
{
    return m_commandPool;
}

std::unique_ptr<VkSamplerHandle> VkResourceManager::CreateSampler(VkFilter filter, VkSamplerAddressMode addressMode)
{
    auto sampler = std::make_unique<VkSamplerHandle>();
    if (!sampler->Create(m_device, m_physicalDevice, filter, addressMode)) {
        return nullptr;
    }
    return sampler;
}

} // namespace vk
} // namespace infernux
