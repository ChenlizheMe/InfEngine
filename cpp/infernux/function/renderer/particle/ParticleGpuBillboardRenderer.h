#pragma once

#include "ParticleGpuRuntime.h"

#include <function/renderer/MaterialPassPipeline.h>

#include <array>
#include <cstdint>
#include <memory>
#include <vector>

namespace infernux
{
class InxMaterial;
}

namespace infernux::particle
{

struct GpuBillboardMaterialState
{
    int32_t renderQueue = 3000;
    bool blendEnabled = true;
    bool depthTestEnabled = true;
    bool depthWriteEnabled = false;
};

struct GpuBillboardRendererDesc
{
    ShaderBytecode vertexShader;
    ShaderBytecode fragmentShader;
    rhi::BufferHandle instances;
    std::shared_ptr<InxMaterial> material;
    GpuBillboardMaterialState fallbackMaterial;
};

struct alignas(16) GpuBillboardViewConstants
{
    std::array<float, 16> viewProjection{};
    std::array<float, 4> cameraRight{};
    std::array<float, 4> cameraUp{};
};

class ParticleGpuBillboardRenderer
{
  public:
    ParticleGpuBillboardRenderer() = default;
    ~ParticleGpuBillboardRenderer();

    ParticleGpuBillboardRenderer(const ParticleGpuBillboardRenderer &) = delete;
    ParticleGpuBillboardRenderer &operator=(const ParticleGpuBillboardRenderer &) = delete;
    ParticleGpuBillboardRenderer(ParticleGpuBillboardRenderer &&) = delete;
    ParticleGpuBillboardRenderer &operator=(ParticleGpuBillboardRenderer &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuBillboardRendererDesc &desc);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] int32_t RenderQueue() const noexcept;
    [[nodiscard]] rhi::BufferHandle InstanceBuffer() const noexcept
    {
        return m_instances;
    }

    [[nodiscard]] bool RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                  rhi::RenderTargetLayoutHandle renderTargetLayout,
                                  const MaterialPassPipelineDescriptor &pass, rhi::BufferHandle indirectArguments,
                                  const GpuBillboardViewConstants &view);

  private:
    struct PipelineEntry
    {
        rhi::RenderTargetLayoutHandle renderTargetLayout;
        MaterialPassPipelineDescriptor pass;
        uint8_t materialStateSignature = 0;
        rhi::GraphicsPipelineHandle pipeline;
    };

    [[nodiscard]] rhi::GraphicsPipelineHandle GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                                  const MaterialPassPipelineDescriptor &pass);
    [[nodiscard]] GpuBillboardMaterialState ResolveMaterialState() const noexcept;

    rhi::Device *m_device = nullptr;
    std::shared_ptr<InxMaterial> m_material;
    GpuBillboardMaterialState m_fallbackMaterial{};
    rhi::BufferHandle m_instances;
    rhi::ShaderModuleHandle m_vertexShader;
    rhi::ShaderModuleHandle m_fragmentShader;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    std::vector<PipelineEntry> m_pipelines;
};

static_assert(sizeof(GpuBillboardViewConstants) == 96);

} // namespace infernux::particle
