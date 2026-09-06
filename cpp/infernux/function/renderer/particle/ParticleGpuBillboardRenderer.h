#pragma once

#include "ParticleGpuOutputRenderer.h"
#include "ParticleGpuRuntime.h"
#include "ParticleGpuSurfaceBinding.h"
#include "ParticleOutputSemantics.h"
#include <function/renderer/rhi/RhiTexture.h>

#include <core/types/ShaderProgramArtifact.h>
#include <function/renderer/MaterialPassPipeline.h>

#include <cstdint>
#include <functional>
#include <memory>
#include <vector>

namespace infernux
{
class InxMaterial;
class GpuRetirementQueue;
} // namespace infernux

namespace infernux::particle
{

struct GpuBillboardRendererDesc
{
    ShaderBytecode vertexShader;
    ShaderBytecode pickingFragmentShader;
    ShaderBytecode motionVertexShader;
    ShaderBytecode motionFragmentShader;
    std::shared_ptr<const ShaderProgramArtifact> shaderProgram;
    rhi::BufferHandle instances;
    rhi::BufferHandle renderIndices;
    std::shared_ptr<InxMaterial> material;
    GpuBillboardMaterialState fallbackMaterial;
    ParticleOutputSemantics semantics;
    uint32_t flipbookColumns = 1;
    uint32_t flipbookRows = 1;
    GpuBillboardTextureResolver textureResolver;
    GpuRetirementQueue *deletionQueue = nullptr;
};

using GpuBillboardViewConstants = GpuParticleViewConstants;

class ParticleGpuBillboardRenderer : public ParticleGpuOutputRenderer
{
  public:
    ParticleGpuBillboardRenderer() = default;
    ~ParticleGpuBillboardRenderer() override;

    ParticleGpuBillboardRenderer(const ParticleGpuBillboardRenderer &) = delete;
    ParticleGpuBillboardRenderer &operator=(const ParticleGpuBillboardRenderer &) = delete;
    ParticleGpuBillboardRenderer(ParticleGpuBillboardRenderer &&) = delete;
    ParticleGpuBillboardRenderer &operator=(ParticleGpuBillboardRenderer &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuBillboardRendererDesc &desc);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept override;
    [[nodiscard]] int32_t RenderQueue() const noexcept override;
    [[nodiscard]] bool RequiresSceneDepth() const noexcept override
    {
        return m_semantics.softParticles;
    }
    [[nodiscard]] uint32_t VertexCount() const noexcept override
    {
        return 6;
    }
    [[nodiscard]] rhi::BufferHandle InstanceBuffer() const noexcept override
    {
        return m_instances;
    }
    [[nodiscard]] rhi::BufferHandle RenderIndexBuffer() const noexcept override
    {
        return m_renderIndices;
    }

    [[nodiscard]] bool RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                  const MaterialPassPipelineDescriptor &pass, rhi::BufferHandle indirectArguments,
                                  const GpuBillboardViewConstants &view, rhi::BufferHandle renderIndices = {},
                                  rhi::TextureViewHandle sceneDepth = {}, bool sceneDepthIsDepth = true,
                                  const GpuParticlePerViewBindings &perView = {}) override;
    [[nodiscard]] bool RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                         const MaterialPassPipelineDescriptor &pass,
                                         rhi::BufferHandle indirectArguments, const GpuBillboardViewConstants &view,
                                         uint64_t ownerObjectId, rhi::BufferHandle renderIndices = {}) override;

  private:
    struct PipelineEntry
    {
        MaterialPassPipelineDescriptor pass;
        rhi::BindingLayoutHandle perViewLayout;
        uint8_t materialStateSignature = 0;
        rhi::GraphicsPipelineHandle pipeline;
    };

    [[nodiscard]] rhi::GraphicsPipelineHandle GetOrCreatePipeline(const MaterialPassPipelineDescriptor &pass,
                                                                  rhi::BindingLayoutHandle perViewLayout = {});
    [[nodiscard]] GpuBillboardMaterialState ResolveMaterialState() const noexcept;
    void RetireViewBindGroups();
    void RetireBindGroup(rhi::BindGroupHandle group);
    [[nodiscard]] rhi::BindGroupHandle CreateGeometryGroup(rhi::BufferHandle renderIndices) const;
    [[nodiscard]] rhi::BindGroupHandle ResolveGeometryGroup(rhi::BufferHandle renderIndices);

    rhi::Device *m_device = nullptr;
    std::shared_ptr<const ShaderProgramArtifact> m_shaderProgram;
    GpuRetirementQueue *m_deletionQueue = nullptr;
    ParticleOutputSemantics m_semantics{};
    uint32_t m_flipbookColumns = 1;
    uint32_t m_flipbookRows = 1;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_renderIndices;
    rhi::ShaderModuleHandle m_vertexShader;
    rhi::ShaderModuleHandle m_fragmentShader;
    rhi::ShaderModuleHandle m_forwardPlusFragmentShader;
    rhi::ShaderModuleHandle m_pickingVertexShader;
    rhi::ShaderModuleHandle m_pickingFragmentShader;
    rhi::ShaderModuleHandle m_motionVertexShader;
    rhi::ShaderModuleHandle m_motionFragmentShader;
    rhi::BindingLayoutHandle m_geometryLayout;
    rhi::BindGroupHandle m_geometryGroup;
    rhi::BindingLayoutHandle m_emptyLayout;
    rhi::BindGroupHandle m_emptyGroup;
    struct ViewBindGroup
    {
        rhi::BufferHandle renderIndices;
        rhi::BindGroupHandle group;
    };
    std::vector<ViewBindGroup> m_viewGroups;
    ParticleGpuSurfaceBinding m_surface;
    std::vector<PipelineEntry> m_pipelines;
};

} // namespace infernux::particle
