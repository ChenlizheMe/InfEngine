#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>

#include <webgpu/webgpu_cpp.h>

#include <array>
#include <memory>
#include <string>
#include <vector>

namespace infernux::web
{

inline constexpr uint32_t WebSamplerBindingBase = 500;
inline constexpr uint32_t WebPushConstantGroup = 0;
inline constexpr uint32_t WebPushConstantBinding = 999;

[[nodiscard]] constexpr uint32_t WebSamplerBinding(uint32_t textureBinding) noexcept
{
    return WebSamplerBindingBase + textureBinding;
}

class WebGpuRhiDevice;

struct WebGpuGraphicsCommandContext final
{
    WebGpuRhiDevice *device = nullptr;
    wgpu::RenderPassEncoder pass;
    rhi::GraphicsPipelineHandle pipeline;
    std::array<rhi::BindGroupHandle, rhi::GraphicsPipelineDesc::MaxBindingLayouts> groups{};
    wgpu::Buffer pushConstantBuffer;
    uint32_t pushConstantBytes = 0;
};

struct WebGpuComputeCommandContext final
{
    WebGpuRhiDevice *device = nullptr;
    wgpu::ComputePassEncoder pass;
    rhi::ComputePipelineHandle pipeline;
    std::array<rhi::BindGroupHandle, rhi::ComputePipelineDesc::MaxBindingLayouts> groups{};
    wgpu::Buffer pushConstantBuffer;
    uint32_t pushConstantBytes = 0;
};

struct WebGpuTransferCommandContext final
{
    WebGpuRhiDevice *device = nullptr;
    wgpu::CommandEncoder encoder;
};

class WebGpuRhiDevice final : public rhi::Device
{
  public:
    WebGpuRhiDevice(wgpu::Device device, wgpu::Queue queue, uint32_t maxStorageBuffersPerStage);
    ~WebGpuRhiDevice() override;

    WebGpuRhiDevice(const WebGpuRhiDevice &) = delete;
    WebGpuRhiDevice &operator=(const WebGpuRhiDevice &) = delete;

    [[nodiscard]] rhi::DeviceId GetDeviceId() const noexcept override;
    [[nodiscard]] const rhi::DeviceCaps &GetCapabilities() const noexcept override;
    [[nodiscard]] const rhi::DeviceCapabilityState &GetCapabilityState() const noexcept override;
    [[nodiscard]] std::shared_ptr<rhi::DeviceLifetime> GetLifetime() const noexcept override;

    [[nodiscard]] rhi::BufferHandle CreateBuffer(const rhi::BufferDesc &desc) override;
    [[nodiscard]] rhi::TextureHandle CreateTexture(const rhi::TextureDesc &desc) override;
    [[nodiscard]] rhi::TextureViewHandle CreateTextureView(const rhi::TextureViewDesc &desc) override;
    [[nodiscard]] rhi::SamplerHandle CreateSampler(const rhi::SamplerDesc &desc) override;
    [[nodiscard]] rhi::ShaderModuleHandle CreateShaderModule(const rhi::ShaderModuleDesc &desc) override;
    [[nodiscard]] rhi::BindingLayoutHandle CreateBindingLayout(const rhi::BindingLayoutDesc &desc) override;
    [[nodiscard]] rhi::BindGroupHandle CreateBindGroup(const rhi::BindGroupDesc &desc) override;
    [[nodiscard]] rhi::GraphicsPipelineHandle CreateGraphicsPipeline(const rhi::GraphicsPipelineDesc &desc) override;
    [[nodiscard]] rhi::ComputePipelineHandle CreateComputePipeline(const rhi::ComputePipelineDesc &desc) override;

    bool WriteBuffer(rhi::BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize) override;
    [[nodiscard]] bool ReadBuffer(rhi::BufferHandle handle, uint64_t offset, void *data, uint64_t byteSize) override;

    void Release(rhi::BufferHandle handle) noexcept override;
    void Release(rhi::TextureHandle handle) noexcept override;
    void Release(rhi::TextureViewHandle handle) noexcept override;
    void Release(rhi::SamplerHandle handle) noexcept override;
    void Release(rhi::ShaderModuleHandle handle) noexcept override;
    void Release(rhi::BindingLayoutHandle handle) noexcept override;
    void Release(rhi::BindGroupHandle handle) noexcept override;
    void Release(rhi::GraphicsPipelineHandle handle) noexcept override;
    void Release(rhi::ComputePipelineHandle handle) noexcept override;

    [[nodiscard]] rhi::GraphicsCommandEncoder MakeGraphicsCommandEncoder(WebGpuGraphicsCommandContext &context,
                                                                         wgpu::RenderPassEncoder pass) noexcept;
    [[nodiscard]] rhi::ComputeCommandEncoder MakeComputeCommandEncoder(WebGpuComputeCommandContext &context,
                                                                       wgpu::ComputePassEncoder pass) noexcept;
    [[nodiscard]] rhi::TransferCommandEncoder MakeTransferCommandEncoder(WebGpuTransferCommandContext &context,
                                                                         wgpu::CommandEncoder encoder) noexcept;

    /// Narrow bridge for browser-native render passes that consume buffers
    /// produced by backend-neutral compute systems.
    [[nodiscard]] wgpu::Buffer GetNativeBuffer(rhi::BufferHandle handle) const noexcept;
    [[nodiscard]] wgpu::Device NativeDevice() const noexcept
    {
        return m_device;
    }
    [[nodiscard]] wgpu::Queue NativeQueue() const noexcept
    {
        return m_queue;
    }

    [[nodiscard]] const std::string &LastError() const noexcept;
    void ClearError() noexcept;

  private:
    struct BufferPayload final
    {
        wgpu::Buffer buffer;
        uint64_t byteSize = 0;
    };

    struct TexturePayload final
    {
        wgpu::Texture texture;
        rhi::TextureDesc desc{};
    };

    struct TextureViewPayload final
    {
        wgpu::TextureView view;
    };

    struct SamplerPayload final
    {
        wgpu::Sampler sampler;
    };

    struct ShaderModulePayload final
    {
        wgpu::ShaderModule module;
        std::string source;
    };

    struct BindingLayoutPayload final
    {
        wgpu::BindGroupLayout layout;
        rhi::BindingLayoutDesc desc{};
    };

    struct BindGroupPayload final
    {
        wgpu::BindGroup group;
        rhi::BindGroupDesc desc{};
    };

    struct GraphicsPipelinePayload final
    {
        wgpu::RenderPipeline pipeline;
        std::array<wgpu::BindGroupLayout, rhi::GraphicsPipelineDesc::MaxBindingLayouts> layouts{};
        uint32_t layoutCount = 0;
        uint32_t pushConstantBytes = 0;
    };

    struct ComputePipelinePayload final
    {
        wgpu::ComputePipeline pipeline;
        std::array<wgpu::BindGroupLayout, rhi::ComputePipelineDesc::MaxBindingLayouts> layouts{};
        std::array<std::vector<uint32_t>, rhi::ComputePipelineDesc::MaxBindingLayouts> usedBindings{};
        uint32_t layoutCount = 0;
        uint32_t pushConstantBytes = 0;
    };

    template <typename Payload> struct Slot final
    {
        Payload payload{};
        uint16_t generation = 1;
        uint32_t nextFree = UINT32_MAX;
        bool occupied = false;
    };

    template <typename HandleType, typename Payload>
    [[nodiscard]] HandleType Register(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, Payload payload);
    template <typename HandleType, typename Payload>
    void ReleaseSlot(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, HandleType handle) noexcept;
    template <typename HandleType, typename Payload>
    [[nodiscard]] Payload *Resolve(std::vector<Slot<Payload>> &slots, HandleType handle) noexcept;
    template <typename HandleType, typename Payload>
    [[nodiscard]] const Payload *Resolve(const std::vector<Slot<Payload>> &slots, HandleType handle) const noexcept;

    [[nodiscard]] wgpu::BindGroupLayout CreateNativeBindingLayout(const rhi::BindingLayoutDesc &desc,
                                                                  bool includePushConstant,
                                                                  const std::vector<uint32_t> *usedBindings = nullptr);
    [[nodiscard]] wgpu::BindGroup CreateNativeBindGroup(const rhi::BindGroupDesc *desc, wgpu::BindGroupLayout layout,
                                                        wgpu::Buffer pushConstantBuffer,
                                                        const std::vector<uint32_t> *usedBindings = nullptr);
    [[nodiscard]] wgpu::Buffer CreatePushConstantBuffer(uint32_t byteSize, const void *data);
    void BindGraphicsGroup(WebGpuGraphicsCommandContext &context, uint32_t setIndex);
    void BindComputeGroup(WebGpuComputeCommandContext &context, uint32_t setIndex);
    void SetError(std::string message);

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
    static void ResolveTexture(void *context, rhi::TextureHandle source, rhi::TextureHandle destination,
                               const rhi::TextureResolveRegion &region);

    static const rhi::GraphicsCommandEncoder::Dispatch s_graphicsDispatch;
    static const rhi::ComputeCommandEncoder::DispatchTable s_computeDispatch;
    static const rhi::TransferCommandEncoder::DispatchTable s_transferDispatch;

    rhi::DeviceId m_deviceId = rhi::InvalidDeviceId;
    wgpu::Device m_device;
    wgpu::Queue m_queue;
    rhi::DeviceCaps m_capabilities{};
    rhi::DeviceCapabilityState m_capabilityState{};
    std::shared_ptr<rhi::DeviceLifetime> m_lifetime = std::make_shared<rhi::DeviceLifetime>();
    std::string m_lastError;

    std::vector<Slot<BufferPayload>> m_buffers;
    std::vector<Slot<TexturePayload>> m_textures;
    std::vector<Slot<TextureViewPayload>> m_textureViews;
    std::vector<Slot<SamplerPayload>> m_samplers;
    std::vector<Slot<ShaderModulePayload>> m_shaderModules;
    std::vector<Slot<BindingLayoutPayload>> m_bindingLayouts;
    std::vector<Slot<BindGroupPayload>> m_bindGroups;
    std::vector<Slot<GraphicsPipelinePayload>> m_graphicsPipelines;
    std::vector<Slot<ComputePipelinePayload>> m_computePipelines;

    uint32_t m_freeBuffer = UINT32_MAX;
    uint32_t m_freeTexture = UINT32_MAX;
    uint32_t m_freeTextureView = UINT32_MAX;
    uint32_t m_freeSampler = UINT32_MAX;
    uint32_t m_freeShaderModule = UINT32_MAX;
    uint32_t m_freeBindingLayout = UINT32_MAX;
    uint32_t m_freeBindGroup = UINT32_MAX;
    uint32_t m_freeGraphicsPipeline = UINT32_MAX;
    uint32_t m_freeComputePipeline = UINT32_MAX;
};

} // namespace infernux::web
