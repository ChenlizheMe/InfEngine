#pragma once

#include "ParticleGpuOutputRenderer.h"
#include "ParticleGpuRuntime.h"
#include "ParticleOutputSemantics.h"

#include <core/types/ShaderProgramArtifact.h>
#include <function/renderer/MaterialPassPipeline.h>

#include <array>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace infernux
{
class InxMaterial;
class FrameDeletionQueue;
} // namespace infernux

namespace infernux::particle
{

struct GpuBillboardMaterialState
{
    int32_t renderQueue = 3000;
    bool blendEnabled = true;
    bool depthTestEnabled = true;
    bool depthWriteEnabled = false;
};

enum class GpuBillboardTextureStatus : uint8_t
{
    Ready,
    Pending,
    Failed,
};

struct GpuBillboardTextureLease
{
    GpuBillboardTextureStatus status = GpuBillboardTextureStatus::Failed;
    rhi::TextureViewHandle texture;
    rhi::SamplerHandle sampler;
    std::shared_ptr<void> keepAlive;
    bool releaseHandles = false;
};

using GpuBillboardTextureResolver =
    std::function<GpuBillboardTextureLease(const std::string &textureGuid, const std::string &bindingName)>;
using GpuBillboardTextureVersionResolver = std::function<uint64_t(const std::string &textureGuid)>;

struct GpuBillboardRendererDesc
{
    ShaderBytecode vertexShader;
    ShaderBytecode fragmentShader;
    ShaderBytecode forwardPlusFragmentShader;
    ShaderBytecode pickingFragmentShader;
    std::shared_ptr<const ShaderProgramArtifact> shaderProgram;
    rhi::BufferHandle instances;
    rhi::BufferHandle renderIndices;
    std::shared_ptr<InxMaterial> material;
    GpuBillboardMaterialState fallbackMaterial;
    ParticleOutputSemantics semantics;
    GpuBillboardTextureResolver textureResolver;
    GpuBillboardTextureVersionResolver textureVersionResolver;
    FrameDeletionQueue *deletionQueue = nullptr;
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
                                  rhi::RenderTargetLayoutHandle renderTargetLayout,
                                  const MaterialPassPipelineDescriptor &pass, rhi::BufferHandle indirectArguments,
                                  const GpuBillboardViewConstants &view, rhi::BufferHandle renderIndices = {},
                                  rhi::TextureViewHandle sceneDepth = {}, bool sceneDepthIsDepth = true,
                                  const GpuParticleForwardPlusBindings &forwardPlus = {}) override;
    [[nodiscard]] bool RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                         rhi::RenderTargetLayoutHandle renderTargetLayout,
                                         const MaterialPassPipelineDescriptor &pass,
                                         rhi::BufferHandle indirectArguments, const GpuBillboardViewConstants &view,
                                         uint64_t ownerObjectId, rhi::BufferHandle renderIndices = {}) override;

  private:
    struct PipelineEntry
    {
        rhi::RenderTargetLayoutHandle renderTargetLayout;
        MaterialPassPipelineDescriptor pass;
        rhi::BindingLayoutHandle forwardPlusLayout;
        uint8_t materialStateSignature = 0;
        rhi::GraphicsPipelineHandle pipeline;
    };

    [[nodiscard]] rhi::GraphicsPipelineHandle GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                                  const MaterialPassPipelineDescriptor &pass,
                                                                  rhi::BindingLayoutHandle forwardPlusLayout = {});
    struct TextureBindingState
    {
        uint32_t binding = 0;
        rhi::ShaderStage visibility = rhi::ShaderStage::None;
        std::string name;
        std::string defaultGuid;
        std::string requestedGuid;
        uint64_t requestedVersion = 0;
        rhi::TextureViewHandle texture;
        rhi::SamplerHandle sampler;
        std::shared_ptr<void> keepAlive;
        bool releaseHandles = false;
        bool pending = false;
        bool fallback = false;
    };

    [[nodiscard]] bool UsesLinkedProgram() const noexcept;
    [[nodiscard]] GpuBillboardMaterialState ResolveMaterialState() const noexcept;
    [[nodiscard]] std::array<float, 4> ResolveMaterialTint() const noexcept;
    [[nodiscard]] float ResolveMaterialFloat(const char *name, float fallback) const noexcept;
    [[nodiscard]] std::string ResolveMaterialTextureGuid(const TextureBindingState &binding) const;
    [[nodiscard]] bool RefreshMaterialBuffer(bool force);
    [[nodiscard]] bool RefreshTextureBindings(bool force);
    [[nodiscard]] rhi::BindGroupHandle CreateBindGroup(const std::vector<TextureBindingState> &textures,
                                                       rhi::BufferHandle renderIndices,
                                                       rhi::TextureViewHandle sceneDepth = {},
                                                       bool sceneDepthIsDepth = true) const;
    [[nodiscard]] rhi::BindGroupHandle ResolveBindGroup(rhi::BufferHandle renderIndices,
                                                        rhi::TextureViewHandle sceneDepth = {},
                                                        bool sceneDepthIsDepth = true);
    [[nodiscard]] bool RebuildBindGroup();
    void RetireViewBindGroups();
    void RetireBindGroup(rhi::BindGroupHandle group);
    void RetireTexture(rhi::TextureViewHandle texture, rhi::SamplerHandle sampler, std::shared_ptr<void> keepAlive,
                       bool releaseHandles);

    rhi::Device *m_device = nullptr;
    std::shared_ptr<InxMaterial> m_material;
    std::shared_ptr<const ShaderProgramArtifact> m_shaderProgram;
    GpuBillboardMaterialState m_fallbackMaterial{};
    ParticleOutputSemantics m_semantics{};
    GpuBillboardTextureResolver m_textureResolver;
    GpuBillboardTextureVersionResolver m_textureVersionResolver;
    FrameDeletionQueue *m_deletionQueue = nullptr;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_renderIndices;
    rhi::ShaderModuleHandle m_vertexShader;
    rhi::ShaderModuleHandle m_fragmentShader;
    rhi::ShaderModuleHandle m_forwardPlusVertexShader;
    rhi::ShaderModuleHandle m_forwardPlusFragmentShader;
    rhi::ShaderModuleHandle m_pickingVertexShader;
    rhi::ShaderModuleHandle m_pickingFragmentShader;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    struct ViewBindGroup
    {
        rhi::BufferHandle renderIndices;
        rhi::TextureViewHandle sceneDepth;
        bool sceneDepthIsDepth = true;
        rhi::BindGroupHandle group;
    };
    std::vector<ViewBindGroup> m_viewGroups;
    rhi::BufferHandle m_materialBuffer;
    std::vector<TextureBindingState> m_textures;
    GpuBillboardTextureLease m_sceneDepthFallback;
    uint64_t m_materialVersion = 0;
    bool m_materialVersionInitialized = false;
    bool m_usesTexture = false;
    bool m_supportsSceneDepth = false;
    std::vector<PipelineEntry> m_pipelines;
};

} // namespace infernux::particle
