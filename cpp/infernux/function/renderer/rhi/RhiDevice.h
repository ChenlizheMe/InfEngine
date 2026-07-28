#pragma once

#include "RhiCapabilities.h"
#include "RhiDescriptors.h"

#include <cstdint>

namespace infernux::rhi
{

/// Backend-neutral resource creation surface. Runtime systems retain only
/// RHI handles; Vulkan/WebGPU ownership stays in the concrete adapter.
class Device
{
  public:
    virtual ~Device() = default;

    [[nodiscard]] virtual DeviceId GetDeviceId() const noexcept
    {
        return InvalidDeviceId;
    }
    [[nodiscard]] virtual const DeviceCaps &GetCapabilities() const noexcept
    {
        static const DeviceCaps empty{};
        return empty;
    }

    [[nodiscard]] virtual BufferHandle CreateBuffer(const BufferDesc &desc) = 0;
    [[nodiscard]] virtual TextureHandle CreateTexture(const TextureDesc &desc) = 0;
    [[nodiscard]] virtual TextureViewHandle CreateTextureView(const TextureViewDesc &desc) = 0;
    [[nodiscard]] virtual SamplerHandle CreateSampler(const SamplerDesc &desc) = 0;
    [[nodiscard]] virtual ShaderModuleHandle CreateShaderModule(const ShaderModuleDesc &desc) = 0;
    [[nodiscard]] virtual BindingLayoutHandle CreateBindingLayout(const BindingLayoutDesc &desc) = 0;
    [[nodiscard]] virtual BindGroupHandle CreateBindGroup(const BindGroupDesc &desc) = 0;
    [[nodiscard]] virtual GraphicsPipelineHandle CreateGraphicsPipeline(const GraphicsPipelineDesc &desc) = 0;
    [[nodiscard]] virtual ComputePipelineHandle CreateComputePipeline(const ComputePipelineDesc &desc) = 0;

    virtual bool WriteBuffer(BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize) = 0;
    /// Copy bytes from a host-visible readback buffer after the submission
    /// that populated it has completed. Device-local and upload buffers are
    /// intentionally rejected by concrete backends.
    [[nodiscard]] virtual bool ReadBuffer(BufferHandle handle, uint64_t offset, void *data, uint64_t byteSize)
    {
        (void)handle;
        (void)offset;
        (void)data;
        (void)byteSize;
        return false;
    }

    virtual void Release(BufferHandle handle) noexcept = 0;
    virtual void Release(TextureHandle handle) noexcept = 0;
    virtual void Release(TextureViewHandle handle) noexcept = 0;
    virtual void Release(SamplerHandle handle) noexcept = 0;
    virtual void Release(ShaderModuleHandle handle) noexcept = 0;
    virtual void Release(BindingLayoutHandle handle) noexcept = 0;
    virtual void Release(BindGroupHandle handle) noexcept = 0;
    virtual void Release(GraphicsPipelineHandle handle) noexcept = 0;
    virtual void Release(ComputePipelineHandle handle) noexcept = 0;
};

} // namespace infernux::rhi
