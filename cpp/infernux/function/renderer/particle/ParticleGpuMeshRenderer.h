#pragma once

#include "ParticleGpuBillboardRenderer.h"

#include <function/resources/InxMesh/InxMesh.h>

#include <array>
#include <memory>
#include <vector>

namespace infernux::particle
{

struct GpuMeshRendererDesc
{
    ShaderBytecode vertexShader;
    ShaderBytecode fragmentShader;
    ShaderBytecode pickingFragmentShader;
    rhi::BufferHandle instances;
    rhi::BufferHandle renderIndices;
    std::shared_ptr<InxMesh> mesh;
    rhi::BufferHandle meshVertices;
    rhi::BufferHandle meshIndices;
    uint32_t indexCount = 0;
    std::shared_ptr<void> meshBufferKeepAlive;
    std::shared_ptr<InxMaterial> material;
    GpuBillboardMaterialState fallbackMaterial;
};

class ParticleGpuMeshRenderer final : public ParticleGpuOutputRenderer
{
  public:
    ParticleGpuMeshRenderer() = default;
    ~ParticleGpuMeshRenderer() override;

    ParticleGpuMeshRenderer(const ParticleGpuMeshRenderer &) = delete;
    ParticleGpuMeshRenderer &operator=(const ParticleGpuMeshRenderer &) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuMeshRendererDesc &desc);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept override;
    [[nodiscard]] int32_t RenderQueue() const noexcept override;
    [[nodiscard]] bool RequiresSceneDepth() const noexcept override
    {
        return false;
    }
    [[nodiscard]] uint32_t VertexCount() const noexcept override
    {
        return m_indexCount;
    }
    [[nodiscard]] rhi::BufferHandle InstanceBuffer() const noexcept override
    {
        return m_instances;
    }
    [[nodiscard]] rhi::BufferHandle RenderIndexBuffer() const noexcept override
    {
        return m_renderIndices;
    }
    [[nodiscard]] const std::vector<GpuParticleStaticBuffer> &StaticVertexStorageBuffers() const noexcept override
    {
        return m_staticVertexStorageBuffers;
    }

    [[nodiscard]] bool RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                  rhi::RenderTargetLayoutHandle renderTargetLayout,
                                  const MaterialPassPipelineDescriptor &pass, rhi::BufferHandle indirectArguments,
                                  const GpuParticleViewConstants &view, rhi::BufferHandle renderIndices = {},
                                  rhi::TextureViewHandle sceneDepth = {}, bool sceneDepthIsDepth = true) override;
    [[nodiscard]] bool RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                         rhi::RenderTargetLayoutHandle renderTargetLayout,
                                         const MaterialPassPipelineDescriptor &pass,
                                         rhi::BufferHandle indirectArguments, const GpuParticleViewConstants &view,
                                         uint64_t ownerObjectId, rhi::BufferHandle renderIndices = {}) override;

  private:
    struct PipelineEntry
    {
        rhi::RenderTargetLayoutHandle renderTargetLayout;
        MaterialPassPipelineDescriptor pass;
        uint8_t materialStateSignature = 0;
        rhi::GraphicsPipelineHandle pipeline;
    };
    struct ViewGroup
    {
        rhi::BufferHandle renderIndices;
        rhi::BindGroupHandle group;
    };

    [[nodiscard]] GpuBillboardMaterialState ResolveMaterialState() const noexcept;
    [[nodiscard]] std::array<float, 4> ResolveMaterialTint() const noexcept;
    [[nodiscard]] rhi::BindGroupHandle CreateBindGroup(rhi::BufferHandle renderIndices) const;
    [[nodiscard]] rhi::BindGroupHandle ResolveBindGroup(rhi::BufferHandle renderIndices);
    [[nodiscard]] rhi::GraphicsPipelineHandle GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                                  const MaterialPassPipelineDescriptor &pass);

    rhi::Device *m_device = nullptr;
    std::shared_ptr<InxMesh> m_mesh;
    std::shared_ptr<void> m_meshBufferKeepAlive;
    std::shared_ptr<InxMaterial> m_material;
    GpuBillboardMaterialState m_fallbackMaterial{};
    uint32_t m_indexCount = 0;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_renderIndices;
    rhi::BufferHandle m_meshVertices;
    rhi::BufferHandle m_meshIndices;
    std::vector<GpuParticleStaticBuffer> m_staticVertexStorageBuffers;
    rhi::ShaderModuleHandle m_vertexShader;
    rhi::ShaderModuleHandle m_fragmentShader;
    rhi::ShaderModuleHandle m_pickingFragmentShader;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    std::vector<ViewGroup> m_viewGroups;
    std::vector<PipelineEntry> m_pipelines;
};

} // namespace infernux::particle
