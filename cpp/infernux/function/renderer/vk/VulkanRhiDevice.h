#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>

#include <cstdint>
#include <vector>
#include <vk_mem_alloc.h>
#include <vulkan/vulkan.h>

namespace infernux::vk
{

class VulkanRhiDevice;

struct VulkanGraphicsCommandContext
{
    VulkanRhiDevice *device = nullptr;
    VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
    rhi::GraphicsPipelineHandle boundPipeline;
};

struct VulkanComputeCommandContext
{
    VulkanRhiDevice *device = nullptr;
    VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
    rhi::ComputePipelineHandle boundPipeline;
};

struct VulkanTransferCommandContext
{
    VulkanRhiDevice *device = nullptr;
    VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
};

class VulkanRhiDevice final : public rhi::Device
{
  public:
    VulkanRhiDevice() = default;
    explicit VulkanRhiDevice(VkDevice device, VmaAllocator allocator = VK_NULL_HANDLE) noexcept
        : m_device(device), m_allocator(allocator)
    {
    }

    VulkanRhiDevice(const VulkanRhiDevice &) = delete;
    VulkanRhiDevice &operator=(const VulkanRhiDevice &) = delete;
    VulkanRhiDevice(VulkanRhiDevice &&) = delete;
    VulkanRhiDevice &operator=(VulkanRhiDevice &&) = delete;

    ~VulkanRhiDevice();

    void Reset(VkDevice device = VK_NULL_HANDLE, VmaAllocator allocator = VK_NULL_HANDLE) noexcept;

    [[nodiscard]] rhi::BufferHandle RegisterBuffer(VkBuffer buffer, uint64_t byteSize = 0);
    [[nodiscard]] rhi::TextureHandle RegisterTexture(VkImage image);
    [[nodiscard]] rhi::TextureViewHandle RegisterTextureView(VkImageView view);
    [[nodiscard]] rhi::SamplerHandle RegisterSampler(VkSampler sampler);
    [[nodiscard]] rhi::ShaderModuleHandle RegisterShaderModule(VkShaderModule module);
    [[nodiscard]] rhi::BindingLayoutHandle RegisterBindingLayout(VkDescriptorSetLayout layout);
    [[nodiscard]] rhi::BindGroupHandle RegisterBindGroup(VkDescriptorSet set);
    [[nodiscard]] rhi::GraphicsPipelineHandle RegisterGraphicsPipeline(VkPipeline pipeline, VkPipelineLayout layout);
    [[nodiscard]] rhi::ComputePipelineHandle RegisterComputePipeline(VkPipeline pipeline, VkPipelineLayout layout);
    /// Create a compute pipeline from backend-neutral RHI handles. The
    /// returned pipeline and its layout are owned by this adapter.
    [[nodiscard]] rhi::BufferHandle CreateBuffer(const rhi::BufferDesc &desc) override;
    [[nodiscard]] rhi::ShaderModuleHandle CreateShaderModule(const rhi::ShaderModuleDesc &desc) override;
    [[nodiscard]] rhi::BindingLayoutHandle CreateBindingLayout(const rhi::BindingLayoutDesc &desc) override;
    [[nodiscard]] rhi::BindGroupHandle CreateBindGroup(const rhi::BindGroupDesc &desc) override;
    [[nodiscard]] rhi::GraphicsPipelineHandle CreateGraphicsPipeline(const rhi::GraphicsPipelineDesc &desc) override;
    [[nodiscard]] rhi::ComputePipelineHandle CreateComputePipeline(const rhi::ComputePipelineDesc &desc) override;
    bool WriteBuffer(rhi::BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize) override;
    [[nodiscard]] rhi::RenderTargetLayoutHandle RegisterRenderTargetLayout(VkRenderPass renderPass);

    void Release(rhi::BufferHandle handle) noexcept override;
    void Release(rhi::TextureHandle handle) noexcept;
    void Release(rhi::TextureViewHandle handle) noexcept;
    void Release(rhi::SamplerHandle handle) noexcept;
    void Release(rhi::ShaderModuleHandle handle) noexcept override;
    void Release(rhi::BindingLayoutHandle handle) noexcept override;
    void Release(rhi::BindGroupHandle handle) noexcept override;
    void Release(rhi::GraphicsPipelineHandle handle) noexcept override;
    void Release(rhi::ComputePipelineHandle handle) noexcept override;
    void Release(rhi::RenderTargetLayoutHandle handle) noexcept;

    [[nodiscard]] VkBuffer Resolve(rhi::BufferHandle handle) const noexcept;
    [[nodiscard]] VkImage Resolve(rhi::TextureHandle handle) const noexcept;
    [[nodiscard]] VkImageView Resolve(rhi::TextureViewHandle handle) const noexcept;
    [[nodiscard]] VkSampler Resolve(rhi::SamplerHandle handle) const noexcept;
    [[nodiscard]] VkShaderModule Resolve(rhi::ShaderModuleHandle handle) const noexcept;
    [[nodiscard]] VkDescriptorSetLayout Resolve(rhi::BindingLayoutHandle handle) const noexcept;
    [[nodiscard]] VkDescriptorSet Resolve(rhi::BindGroupHandle handle) const noexcept;
    [[nodiscard]] VkRenderPass Resolve(rhi::RenderTargetLayoutHandle handle) const noexcept;

    [[nodiscard]] rhi::GraphicsCommandEncoder MakeGraphicsCommandEncoder(VulkanGraphicsCommandContext &context,
                                                                         VkCommandBuffer commandBuffer) noexcept;
    [[nodiscard]] rhi::ComputeCommandEncoder MakeComputeCommandEncoder(VulkanComputeCommandContext &context,
                                                                       VkCommandBuffer commandBuffer) noexcept;
    [[nodiscard]] rhi::TransferCommandEncoder MakeTransferCommandEncoder(VulkanTransferCommandContext &context,
                                                                         VkCommandBuffer commandBuffer) noexcept;

  private:
    struct BufferPayload
    {
        VkBuffer buffer = VK_NULL_HANDLE;
        VmaAllocation allocation = VK_NULL_HANDLE;
        void *mappedData = nullptr;
        uint64_t byteSize = 0;
        bool owned = false;
    };

    struct ShaderModulePayload
    {
        VkShaderModule module = VK_NULL_HANDLE;
        bool owned = false;
    };

    struct BindingLayoutPayload
    {
        VkDescriptorSetLayout layout = VK_NULL_HANDLE;
        bool owned = false;
    };

    struct BindGroupPayload
    {
        VkDescriptorSet set = VK_NULL_HANDLE;
        VkDescriptorPool pool = VK_NULL_HANDLE;
        bool owned = false;
    };

    struct GraphicsPipelinePayload
    {
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkPipelineLayout layout = VK_NULL_HANDLE;
        bool ownsPipeline = false;
        bool ownsLayout = false;
    };

    template <typename Payload> struct Slot
    {
        Payload payload{};
        uint32_t generation = 1;
        uint32_t nextFree = UINT32_MAX;
        bool occupied = false;
    };

    template <typename HandleType, typename Payload>
    [[nodiscard]] static HandleType Register(std::vector<Slot<Payload>> &slots, uint32_t &freeHead,
                                             const Payload &payload);
    template <typename HandleType, typename Payload>
    static void Release(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, HandleType handle) noexcept;
    template <typename Payload> static void ResetSlots(std::vector<Slot<Payload>> &slots, uint32_t &freeHead) noexcept;
    template <typename HandleType, typename Payload>
    [[nodiscard]] static const Payload *Resolve(const std::vector<Slot<Payload>> &slots, HandleType handle) noexcept;

    [[nodiscard]] const GraphicsPipelinePayload *ResolvePipeline(rhi::GraphicsPipelineHandle handle) const noexcept;
    [[nodiscard]] const GraphicsPipelinePayload *ResolvePipeline(rhi::ComputePipelineHandle handle) const noexcept;
    [[nodiscard]] VkDescriptorPool CreateDescriptorPool();
    [[nodiscard]] VkDescriptorSet AllocateDescriptorSet(VkDescriptorSetLayout layout, VkDescriptorPool &pool);
    void DestroyOwnedResources() noexcept;

    static void BindPipeline(void *context, rhi::GraphicsPipelineHandle pipeline);
    static void BindGroup(void *context, rhi::GraphicsPipelineHandle pipeline, uint32_t setIndex,
                          rhi::BindGroupHandle group);
    static void PushConstants(void *context, rhi::GraphicsPipelineHandle pipeline, rhi::ShaderStage stages,
                              uint32_t byteSize, const void *data);
    static void Draw(void *context, uint32_t vertexCount, uint32_t instanceCount, uint32_t firstVertex,
                     uint32_t firstInstance);
    static void DrawIndirect(void *context, rhi::BufferHandle arguments, uint64_t offset, uint32_t drawCount,
                             uint32_t stride);

    static void BindComputePipeline(void *context, rhi::ComputePipelineHandle pipeline);
    static void BindComputeGroup(void *context, rhi::ComputePipelineHandle pipeline, uint32_t setIndex,
                                 rhi::BindGroupHandle group);
    static void PushComputeConstants(void *context, rhi::ComputePipelineHandle pipeline, uint32_t byteSize,
                                     const void *data);
    static void Dispatch(void *context, uint32_t groupCountX, uint32_t groupCountY, uint32_t groupCountZ);
    static void DispatchIndirect(void *context, rhi::BufferHandle arguments, uint64_t offset);

    static void CopyBuffer(void *context, rhi::BufferHandle source, rhi::BufferHandle destination,
                           const rhi::BufferCopyRegion &region);
    static void CopyTexture(void *context, rhi::TextureHandle source, rhi::TextureHandle destination,
                            const rhi::TextureCopyRegion &region);

    static const rhi::GraphicsCommandEncoder::Dispatch s_graphicsDispatch;
    static const rhi::ComputeCommandEncoder::DispatchTable s_computeDispatch;
    static const rhi::TransferCommandEncoder::DispatchTable s_transferDispatch;

    VkDevice m_device = VK_NULL_HANDLE;
    VmaAllocator m_allocator = VK_NULL_HANDLE;
    std::vector<Slot<BufferPayload>> m_buffers;
    std::vector<Slot<VkImage>> m_textures;
    std::vector<Slot<VkImageView>> m_textureViews;
    std::vector<Slot<VkSampler>> m_samplers;
    std::vector<Slot<ShaderModulePayload>> m_shaderModules;
    std::vector<Slot<BindingLayoutPayload>> m_bindingLayouts;
    std::vector<Slot<BindGroupPayload>> m_bindGroups;
    std::vector<Slot<GraphicsPipelinePayload>> m_graphicsPipelines;
    std::vector<Slot<GraphicsPipelinePayload>> m_computePipelines;
    std::vector<Slot<VkRenderPass>> m_renderTargetLayouts;
    std::vector<VkDescriptorPool> m_ownedDescriptorPools;

    uint32_t m_freeBuffer = UINT32_MAX;
    uint32_t m_freeTexture = UINT32_MAX;
    uint32_t m_freeTextureView = UINT32_MAX;
    uint32_t m_freeSampler = UINT32_MAX;
    uint32_t m_freeShaderModule = UINT32_MAX;
    uint32_t m_freeBindingLayout = UINT32_MAX;
    uint32_t m_freeBindGroup = UINT32_MAX;
    uint32_t m_freeGraphicsPipeline = UINT32_MAX;
    uint32_t m_freeComputePipeline = UINT32_MAX;
    uint32_t m_freeRenderTargetLayout = UINT32_MAX;
};

} // namespace infernux::vk
