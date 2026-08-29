#pragma once

#include <function/renderer/RenderWorld.h>
#include <function/scene/SceneRenderExtractor.h>
#include <webgpu/webgpu_cpp.h>

#include <cstdint>
#include <vector>

namespace infernux::web
{

/// WebGPU consumer for the engine's immutable scene publication.
///
/// Scene traversal remains in SceneRenderExtractor. This backend owns only
/// WebGPU resources and converts the published draw stream into one portable
/// lit mesh pass.
class WebSceneRenderer final
{
  public:
    bool Initialize(wgpu::Device device, wgpu::Queue queue, wgpu::TextureFormat colorFormat);
    void Resize(uint32_t width, uint32_t height);

    [[nodiscard]] bool HasDepthTarget() const noexcept;
    [[nodiscard]] wgpu::TextureView GetDepthView() const noexcept;

    /// Extract and draw the current game-camera view. Returns false when no
    /// renderable game camera or geometry is available.
    bool Render(wgpu::RenderPassEncoder pass, uint32_t width, uint32_t height);

  private:
    struct WebVertex
    {
        float position[3];
        float normal[3];
        float color[4];
    };

    bool CreatePipeline();
    bool EnsureBuffer(wgpu::Buffer &buffer, uint64_t &capacity, uint64_t required, wgpu::BufferUsage usage);
    bool BuildFrame(uint32_t width, uint32_t height);

    wgpu::Device m_device;
    wgpu::Queue m_queue;
    wgpu::TextureFormat m_colorFormat = wgpu::TextureFormat::Undefined;
    wgpu::RenderPipeline m_pipeline;
    wgpu::BindGroupLayout m_cameraLayout;
    wgpu::BindGroup m_cameraGroup;
    wgpu::Buffer m_cameraBuffer;
    wgpu::Buffer m_vertexBuffer;
    wgpu::Buffer m_indexBuffer;
    uint64_t m_vertexCapacity = 0;
    uint64_t m_indexCapacity = 0;
    wgpu::Texture m_depthTexture;
    wgpu::TextureView m_depthView;
    uint32_t m_depthWidth = 0;
    uint32_t m_depthHeight = 0;
    SceneRenderExtractor m_extractor;
    RenderWorldSnapshot m_world;
    std::vector<WebVertex> m_vertices;
    std::vector<uint32_t> m_indices;
    bool m_reportedFirstFrame = false;
};

} // namespace infernux::web
