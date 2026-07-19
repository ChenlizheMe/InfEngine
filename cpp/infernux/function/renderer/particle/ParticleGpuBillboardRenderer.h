#pragma once

#include "ParticleGpuRuntime.h"

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
};

using GpuBillboardTextureResolver =
    std::function<GpuBillboardTextureLease(const std::string &textureGuid, const std::string &bindingName)>;
using GpuBillboardTextureVersionResolver = std::function<uint64_t(const std::string &textureGuid)>;

struct GpuBillboardRendererDesc
{
    ShaderBytecode vertexShader;
    ShaderBytecode fragmentShader;
    std::shared_ptr<const ShaderProgramArtifact> shaderProgram;
    rhi::BufferHandle instances;
    rhi::BufferHandle renderIndices;
    std::shared_ptr<InxMaterial> material;
    GpuBillboardMaterialState fallbackMaterial;
    GpuBillboardTextureResolver textureResolver;
    GpuBillboardTextureVersionResolver textureVersionResolver;
    FrameDeletionQueue *deletionQueue = nullptr;
};

struct alignas(16) GpuBillboardViewConstants
{
    std::array<float, 16> viewProjection{};
    std::array<float, 4> cameraRight{};
    std::array<float, 4> cameraUp{};
    std::array<float, 4> materialTint{1.0f, 1.0f, 1.0f, 1.0f};
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
    [[nodiscard]] rhi::BufferHandle RenderIndexBuffer() const noexcept
    {
        return m_renderIndices;
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
        bool pending = false;
        bool fallback = false;
    };

    [[nodiscard]] bool UsesLinkedProgram() const noexcept;
    [[nodiscard]] GpuBillboardMaterialState ResolveMaterialState() const noexcept;
    [[nodiscard]] std::array<float, 4> ResolveMaterialTint() const noexcept;
    [[nodiscard]] std::string ResolveMaterialTextureGuid(const TextureBindingState &binding) const;
    [[nodiscard]] bool RefreshMaterialBuffer(bool force);
    [[nodiscard]] bool RefreshTextureBindings(bool force);
    [[nodiscard]] rhi::BindGroupHandle CreateBindGroup(const std::vector<TextureBindingState> &textures) const;
    [[nodiscard]] bool RebuildBindGroup();
    void RetireBindGroup(rhi::BindGroupHandle group);
    void RetireTexture(rhi::TextureViewHandle texture, rhi::SamplerHandle sampler, std::shared_ptr<void> keepAlive);

    rhi::Device *m_device = nullptr;
    std::shared_ptr<InxMaterial> m_material;
    std::shared_ptr<const ShaderProgramArtifact> m_shaderProgram;
    GpuBillboardMaterialState m_fallbackMaterial{};
    GpuBillboardTextureResolver m_textureResolver;
    GpuBillboardTextureVersionResolver m_textureVersionResolver;
    FrameDeletionQueue *m_deletionQueue = nullptr;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_renderIndices;
    rhi::ShaderModuleHandle m_vertexShader;
    rhi::ShaderModuleHandle m_fragmentShader;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    rhi::BufferHandle m_materialBuffer;
    std::vector<TextureBindingState> m_textures;
    uint64_t m_materialVersion = 0;
    bool m_materialVersionInitialized = false;
    bool m_usesTexture = false;
    std::vector<PipelineEntry> m_pipelines;
};

static_assert(sizeof(GpuBillboardViewConstants) == 112);

} // namespace infernux::particle
