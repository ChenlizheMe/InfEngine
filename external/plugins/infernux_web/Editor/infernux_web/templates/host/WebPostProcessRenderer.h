#pragma once

#include <webgpu/webgpu_cpp.h>

#include <array>
#include <cstdint>
#include <vector>

namespace infernux::web
{

/// Owns the Web Player HDR scene target and resolves it to the browser surface.
///
/// The Web Player must not render lighting directly into the swapchain: doing
/// so clips every value above one before Bloom and tone mapping can observe it.
/// This class establishes the same essential ordering as the desktop renderer:
/// HDR scene -> Bloom -> ACES -> presentation.
class WebPostProcessRenderer final
{
  public:
    struct Settings
    {
        bool bloomEnabled = false;
        float bloomThreshold = 1.0f;
        float bloomIntensity = 0.8f;
        float bloomScatter = 0.7f;
        float bloomClamp = 65472.0f;
        std::array<float, 3> bloomTint = {1.0f, 1.0f, 1.0f};
        uint32_t bloomIterations = 5;
        uint32_t toneMappingMode = 0;
        float exposure = 1.0f;
    };

    bool Initialize(wgpu::Device device, wgpu::TextureFormat surfaceFormat);
    bool Resize(uint32_t width, uint32_t height);
    bool Configure(const Settings &settings);

    [[nodiscard]] wgpu::TextureFormat SceneColorFormat() const noexcept;
    [[nodiscard]] wgpu::TextureView SceneColorView() const noexcept;
    [[nodiscard]] bool PrepareBloom(wgpu::CommandEncoder encoder);
    [[nodiscard]] bool Render(wgpu::RenderPassEncoder pass);

  private:
    struct DownsampleParameters
    {
        float inverseWidth = 1.0f;
        float inverseHeight = 1.0f;
        float threshold = 1.0f;
        float knee = 0.5f;
        float clampMax = 65472.0f;
        float prefilter = 0.0f;
        float padding0 = 0.0f;
        float padding1 = 0.0f;
    };
    struct UpsampleParameters
    {
        float inverseWidth = 1.0f;
        float inverseHeight = 1.0f;
        float scatter = 0.7f;
        float padding = 0.0f;
    };
    struct ResolveParameters
    {
        float bloomIntensity = 0.0f;
        float exposure = 1.0f;
        float encodeSrgb = 1.0f;
        float toneMappingMode = 0.0f;
        std::array<float, 3> bloomTint = {1.0f, 1.0f, 1.0f};
        float padding = 0.0f;
    };
    struct BloomLevel
    {
        uint32_t width = 1;
        uint32_t height = 1;
        wgpu::Texture downTexture;
        wgpu::TextureView downView;
        wgpu::Texture upTexture;
        wgpu::TextureView upView;
        wgpu::Buffer downParameters;
        wgpu::BindGroup downGroup;
        wgpu::Buffer upParameters;
        wgpu::BindGroup upGroup;
    };

    bool CreatePipelines();
    bool CreateSceneTarget();
    bool CreateBloomTargets();
    bool CreateResolveBindGroup();
    bool RecordColorPass(wgpu::CommandEncoder encoder, wgpu::TextureView target, wgpu::RenderPipeline pipeline,
                         wgpu::BindGroup group);

    wgpu::Device m_device;
    wgpu::TextureFormat m_surfaceFormat = wgpu::TextureFormat::Undefined;
    wgpu::Texture m_sceneColor;
    wgpu::TextureView m_sceneColorView;
    wgpu::Sampler m_sampler;
    wgpu::BindGroupLayout m_downsampleLayout;
    wgpu::BindGroupLayout m_upsampleLayout;
    wgpu::BindGroupLayout m_resolveLayout;
    wgpu::RenderPipeline m_downsamplePipeline;
    wgpu::RenderPipeline m_upsamplePipeline;
    wgpu::RenderPipeline m_resolvePipeline;
    wgpu::Buffer m_resolveParameters;
    wgpu::BindGroup m_resolveGroup;
    std::vector<BloomLevel> m_bloomLevels;
    uint32_t m_width = 0;
    uint32_t m_height = 0;
    Settings m_settings;
    ResolveParameters m_resolveValues;
    bool m_reportedReady = false;
};

} // namespace infernux::web
