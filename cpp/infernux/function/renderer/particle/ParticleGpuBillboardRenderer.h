#pragma once

#include "ParticleGpuRuntime.h"

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
    rhi::BufferHandle instances;
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
    [[nodiscard]] std::array<float, 4> ResolveMaterialTint() const noexcept;
    [[nodiscard]] std::string ResolveMaterialTextureGuid() const;
    [[nodiscard]] bool RefreshTextureBinding(bool force);
    void RetireTextureBinding(rhi::BindGroupHandle group, rhi::TextureViewHandle texture, rhi::SamplerHandle sampler,
                              std::shared_ptr<void> keepAlive);

    rhi::Device *m_device = nullptr;
    std::shared_ptr<InxMaterial> m_material;
    GpuBillboardMaterialState m_fallbackMaterial{};
    GpuBillboardTextureResolver m_textureResolver;
    GpuBillboardTextureVersionResolver m_textureVersionResolver;
    FrameDeletionQueue *m_deletionQueue = nullptr;
    rhi::BufferHandle m_instances;
    rhi::ShaderModuleHandle m_vertexShader;
    rhi::ShaderModuleHandle m_fragmentShader;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    rhi::TextureViewHandle m_texture;
    rhi::SamplerHandle m_sampler;
    std::shared_ptr<void> m_textureKeepAlive;
    std::string m_textureGuid;
    uint64_t m_textureVersion = 0;
    bool m_texturePending = false;
    bool m_textureFallback = false;
    bool m_usesTexture = false;
    std::vector<PipelineEntry> m_pipelines;
};

static_assert(sizeof(GpuBillboardViewConstants) == 112);

} // namespace infernux::particle
