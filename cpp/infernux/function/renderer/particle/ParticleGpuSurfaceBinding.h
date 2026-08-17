#pragma once

#include "ParticleOutputSemantics.h"

#include <core/types/ShaderProgramArtifact.h>
#include <function/renderer/rhi/RhiTexture.h>

#include <array>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace infernux
{
class InxMaterial;
class GpuRetirementQueue;
} // namespace infernux

namespace infernux::particle
{

struct GpuBillboardMaterialState
{
    int32_t renderQueue = 3000;
    bool blendEnabled = true;
    bool depthTestEnabled = true;
    bool depthWriteEnabled = false;
    bool premultipliedAlpha = false;
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
    std::shared_ptr<rhi::TextureGpuViewSlot> gpuSlot;
    std::shared_ptr<const rhi::TextureGpuView> gpuView;
};

using GpuBillboardTextureResolver =
    std::function<GpuBillboardTextureLease(const std::string &textureGuid, const std::string &bindingName)>;

/// Owns the linked particle shader's surface descriptor domain.
///
/// The domain is deliberately independent from output geometry:
///   set 2, texture bindings 2..13, material UBO binding 14, scene depth 15.
/// Geometry renderers can therefore share the same material/shader state without
/// consuming bindings reserved for instances, indices, or output-specific data.
class ParticleGpuSurfaceBinding
{
  public:
    ParticleGpuSurfaceBinding() = default;
    ~ParticleGpuSurfaceBinding();

    ParticleGpuSurfaceBinding(const ParticleGpuSurfaceBinding &) = delete;
    ParticleGpuSurfaceBinding &operator=(const ParticleGpuSurfaceBinding &) = delete;
    ParticleGpuSurfaceBinding(ParticleGpuSurfaceBinding &&) = delete;
    ParticleGpuSurfaceBinding &operator=(ParticleGpuSurfaceBinding &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, std::shared_ptr<const ShaderProgramArtifact> shaderProgram,
                              std::shared_ptr<InxMaterial> material, GpuBillboardMaterialState fallbackMaterial,
                              ParticleOutputSemantics semantics, GpuBillboardTextureResolver textureResolver,
                              GpuRetirementQueue *deletionQueue);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] rhi::BindingLayoutHandle Layout() const noexcept
    {
        return m_layout;
    }
    [[nodiscard]] bool SupportsSceneDepth() const noexcept
    {
        return m_supportsSceneDepth;
    }
    [[nodiscard]] bool UsesBindlessTextures() const noexcept
    {
        return m_usesBindlessTextures;
    }
    [[nodiscard]] rhi::BindlessTextureTableBinding BindlessTableBinding() const noexcept
    {
        return m_device && m_usesBindlessTextures ? m_device->GetBindlessTextureTableBinding()
                                                  : rhi::BindlessTextureTableBinding{};
    }
    void MarkBindlessTexturesUsed() noexcept;
    [[nodiscard]] std::shared_ptr<const ShaderProgramArtifact> ShaderProgram() const noexcept
    {
        return m_shaderProgram;
    }

    [[nodiscard]] GpuBillboardMaterialState ResolveMaterialState() const noexcept;
    [[nodiscard]] std::array<float, 4> ResolveMaterialTint() const noexcept;
    [[nodiscard]] float ResolveMaterialFloat(const char *name, float fallback) const noexcept;

    [[nodiscard]] bool RefreshMaterialBuffer(bool force);
    [[nodiscard]] bool RefreshTextureBindings(bool force);
    [[nodiscard]] rhi::BindGroupHandle ResolveBindGroup(rhi::TextureViewHandle sceneDepth = {},
                                                        bool sceneDepthIsDepth = true);

  private:
    struct TextureBindingState
    {
        uint32_t binding = 0;
        uint32_t textureSlot = 0;
        rhi::ShaderStage visibility = rhi::ShaderStage::None;
        std::string name;
        std::string defaultGuid;
        std::string requestedGuid;
        uint64_t requestedVersion = 0;
        rhi::TextureViewHandle texture;
        rhi::SamplerHandle sampler;
        std::shared_ptr<rhi::TextureGpuViewSlot> gpuSlot;
        std::shared_ptr<const rhi::TextureGpuView> gpuView;
        rhi::ResourceIndex resourceIndex{};
        bool pending = false;
        bool fallback = false;
    };

    struct ViewBindGroup
    {
        rhi::TextureViewHandle sceneDepth;
        bool sceneDepthIsDepth = true;
        rhi::BindGroupHandle group;
    };

    [[nodiscard]] rhi::BindGroupHandle CreateBindGroup(const std::vector<TextureBindingState> &textures,
                                                       rhi::TextureViewHandle sceneDepth = {},
                                                       bool sceneDepthIsDepth = true) const;
    [[nodiscard]] std::string ResolveMaterialTextureGuid(const TextureBindingState &binding) const;
    [[nodiscard]] bool RebuildBindGroup();
    [[nodiscard]] bool RefreshTextureIndexBuffer(const std::vector<TextureBindingState> &textures);
    void RetireViewBindGroups();
    void RetireBindGroup(rhi::BindGroupHandle group);
    void RetireTexture(std::shared_ptr<const rhi::TextureGpuView> gpuView);

    rhi::Device *m_device = nullptr;
    std::shared_ptr<const ShaderProgramArtifact> m_shaderProgram;
    std::shared_ptr<InxMaterial> m_material;
    GpuBillboardMaterialState m_fallbackMaterial{};
    ParticleOutputSemantics m_semantics{};
    GpuBillboardTextureResolver m_textureResolver;
    GpuRetirementQueue *m_deletionQueue = nullptr;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    std::vector<ViewBindGroup> m_viewGroups;
    rhi::BufferHandle m_materialBuffer;
    rhi::BufferHandle m_textureIndexBuffer;
    std::vector<TextureBindingState> m_textures;
    GpuBillboardTextureLease m_sceneDepthFallback;
    uint64_t m_materialVersion = 0;
    bool m_materialVersionInitialized = false;
    bool m_usesTexture = false;
    bool m_usesBindlessTextures = false;
    bool m_supportsSceneDepth = false;
};

} // namespace infernux::particle
