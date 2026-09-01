#pragma once

#include "ParticleGpuBillboardRenderer.h"
#include "ParticleGpuOutputRenderer.h"
#include "ParticleGpuRibbonTopology.h"
#include "ParticleGpuSurfaceBinding.h"

#include <array>
#include <memory>
#include <string_view>
#include <vector>

namespace infernux::particle
{

enum class ParticleRibbonUvMode : uint8_t
{
    Stretch,
    Repeat,
};

struct GpuParticleRibbonRenderShaderSources
{
    [[nodiscard]] static std::string_view Vertex() noexcept;
    [[nodiscard]] static std::string_view PickingFragment() noexcept;
    [[nodiscard]] static std::string_view MotionVertex() noexcept;
    [[nodiscard]] static std::string_view MotionFragment() noexcept;
};

struct GpuParticleRibbonRenderProgram
{
    ShaderBytecode vertex;
    ShaderBytecode pickingFragment;
    ShaderBytecode motionVertex;
    ShaderBytecode motionFragment;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleRibbonRenderProgramStorage
{
    std::array<std::vector<uint32_t>, 4> shaders;

    [[nodiscard]] bool Assign(const GpuParticleRibbonRenderProgram &program);
    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] GpuParticleRibbonRenderProgram View() const noexcept;
};

struct GpuRibbonRendererDesc
{
    GpuParticleRibbonRenderProgram program;
    std::shared_ptr<ParticleGpuRibbonTopology> topology;
    std::shared_ptr<const ShaderProgramArtifact> shaderProgram;
    std::shared_ptr<InxMaterial> material;
    GpuBillboardMaterialState fallbackMaterial;
    ParticleOutputSemantics semantics;
    ParticleRibbonUvMode uvMode = ParticleRibbonUvMode::Stretch;
    float uvScale = 1.0f;
    GpuBillboardTextureResolver textureResolver;
    GpuRetirementQueue *deletionQueue = nullptr;
};

/// Procedural, camera-facing ribbon renderer. One indirect draw emits six
/// vertices per adjacent pair in the globally sorted ribbon topology.
class ParticleGpuRibbonRenderer final : public ParticleGpuOutputRenderer
{
  public:
    ParticleGpuRibbonRenderer() = default;
    ~ParticleGpuRibbonRenderer() override;

    ParticleGpuRibbonRenderer(const ParticleGpuRibbonRenderer &) = delete;
    ParticleGpuRibbonRenderer &operator=(const ParticleGpuRibbonRenderer &) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuRibbonRendererDesc &desc);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept override;
    [[nodiscard]] int32_t RenderQueue() const noexcept override;
    [[nodiscard]] bool RequiresSceneDepth() const noexcept override
    {
        return m_semantics.softParticles;
    }
    [[nodiscard]] bool CanCastShadows() const noexcept override
    {
        return false;
    }
    [[nodiscard]] uint32_t VertexCount() const noexcept override
    {
        return 6;
    }
    [[nodiscard]] rhi::BufferHandle InstanceBuffer() const noexcept override;
    [[nodiscard]] rhi::BufferHandle RenderIndexBuffer() const noexcept override;

    [[nodiscard]] bool RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                  const MaterialPassPipelineDescriptor &pass, rhi::BufferHandle indirectArguments,
                                  const GpuParticleViewConstants &view, rhi::BufferHandle renderIndices = {},
                                  rhi::TextureViewHandle sceneDepth = {}, bool sceneDepthIsDepth = true,
                                  const GpuParticlePerViewBindings &perView = {}) override;
    [[nodiscard]] bool RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                         const MaterialPassPipelineDescriptor &pass,
                                         rhi::BufferHandle indirectArguments, const GpuParticleViewConstants &view,
                                         uint64_t ownerObjectId, rhi::BufferHandle renderIndices = {}) override;

  private:
    struct PipelineEntry
    {
        MaterialPassPipelineDescriptor pass;
        rhi::BindingLayoutHandle perViewLayout;
        uint8_t materialStateSignature = 0;
        rhi::GraphicsPipelineHandle pipeline;
    };
    struct ViewGroup
    {
        rhi::BufferHandle renderIndices;
        rhi::BindGroupHandle group;
    };

    [[nodiscard]] rhi::BindGroupHandle CreateGeometryGroup(rhi::BufferHandle renderIndices) const;
    [[nodiscard]] rhi::BindGroupHandle ResolveGeometryGroup(rhi::BufferHandle renderIndices);
    [[nodiscard]] rhi::GraphicsPipelineHandle GetOrCreatePipeline(const MaterialPassPipelineDescriptor &pass,
                                                                  rhi::BindingLayoutHandle perViewLayout = {});

    rhi::Device *m_device = nullptr;
    std::shared_ptr<ParticleGpuRibbonTopology> m_topology;
    std::shared_ptr<const ShaderProgramArtifact> m_shaderProgram;
    ParticleOutputSemantics m_semantics{};
    ParticleRibbonUvMode m_uvMode = ParticleRibbonUvMode::Stretch;
    float m_uvScale = 1.0f;
    rhi::ShaderModuleHandle m_vertexShader;
    rhi::ShaderModuleHandle m_fragmentShader;
    rhi::ShaderModuleHandle m_forwardPlusFragmentShader;
    rhi::ShaderModuleHandle m_pickingFragmentShader;
    rhi::ShaderModuleHandle m_motionVertexShader;
    rhi::ShaderModuleHandle m_motionFragmentShader;
    rhi::BindingLayoutHandle m_geometryLayout;
    rhi::BindGroupHandle m_geometryGroup;
    rhi::BindingLayoutHandle m_emptyLayout;
    rhi::BindGroupHandle m_emptyGroup;
    std::vector<ViewGroup> m_viewGroups;
    ParticleGpuSurfaceBinding m_surface;
    std::vector<PipelineEntry> m_pipelines;
};

} // namespace infernux::particle
