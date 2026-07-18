#pragma once

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

    [[nodiscard]] virtual BufferHandle CreateBuffer(const BufferDesc &desc) = 0;
    [[nodiscard]] virtual ShaderModuleHandle CreateShaderModule(const ShaderModuleDesc &desc) = 0;
    [[nodiscard]] virtual BindingLayoutHandle CreateBindingLayout(const BindingLayoutDesc &desc) = 0;
    [[nodiscard]] virtual BindGroupHandle CreateBindGroup(const BindGroupDesc &desc) = 0;
    [[nodiscard]] virtual GraphicsPipelineHandle CreateGraphicsPipeline(const GraphicsPipelineDesc &desc) = 0;
    [[nodiscard]] virtual ComputePipelineHandle CreateComputePipeline(const ComputePipelineDesc &desc) = 0;

    virtual bool WriteBuffer(BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize) = 0;

    virtual void Release(BufferHandle handle) noexcept = 0;
    virtual void Release(ShaderModuleHandle handle) noexcept = 0;
    virtual void Release(BindingLayoutHandle handle) noexcept = 0;
    virtual void Release(BindGroupHandle handle) noexcept = 0;
    virtual void Release(GraphicsPipelineHandle handle) noexcept = 0;
    virtual void Release(ComputePipelineHandle handle) noexcept = 0;
};

} // namespace infernux::rhi
