#pragma once

#include <webgpu/webgpu_cpp.h>

#include <cstdint>

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
    bool Initialize(wgpu::Device device, wgpu::TextureFormat surfaceFormat);
    bool Resize(uint32_t width, uint32_t height);

    [[nodiscard]] wgpu::TextureFormat SceneColorFormat() const noexcept;
    [[nodiscard]] wgpu::TextureView SceneColorView() const noexcept;
    [[nodiscard]] bool Render(wgpu::RenderPassEncoder pass);

  private:
    struct Parameters
    {
        float inverseWidth = 1.0f;
        float inverseHeight = 1.0f;
        float bloomThreshold = 1.0f;
        float bloomIntensity = 0.75f;
        float exposure = 1.0f;
        float encodeSrgb = 1.0f;
        float padding0 = 0.0f;
        float padding1 = 0.0f;
    };

    bool CreatePipeline();
    bool CreateSceneTarget();
    bool CreateSceneBindGroup();

    wgpu::Device m_device;
    wgpu::TextureFormat m_surfaceFormat = wgpu::TextureFormat::Undefined;
    wgpu::Texture m_sceneColor;
    wgpu::TextureView m_sceneColorView;
    wgpu::Sampler m_sampler;
    wgpu::Buffer m_parameterBuffer;
    wgpu::BindGroupLayout m_layout;
    wgpu::BindGroup m_bindGroup;
    wgpu::RenderPipeline m_pipeline;
    uint32_t m_width = 0;
    uint32_t m_height = 0;
    Parameters m_parameters;
    bool m_reportedReady = false;
};

} // namespace infernux::web
