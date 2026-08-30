#pragma once

#include <function/renderer/RenderWorld.h>
#include <function/scene/SceneRenderExtractor.h>
#include <webgpu/webgpu_cpp.h>

#include <array>
#include <cstdint>
#include <memory>
#include <string>
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

    /// Extract, upload, and record the directional shadow map before the main
    /// render pass begins.
    bool Prepare(wgpu::CommandEncoder encoder, uint32_t width, uint32_t height);

    /// Draw the prepared procedural sky and shadowed scene geometry.
    bool RenderPrepared(wgpu::RenderPassEncoder pass);

    /// Acceptance-only feature switches used to prove that sky and shadows
    /// materially affect the composed browser frame. Gameplay cannot access
    /// these switches through the Python runtime surface.
    void SetSkyEnabledForDiagnostics(bool enabled) noexcept;
    void SetShadowsEnabledForDiagnostics(bool enabled) noexcept;

  private:
    struct WebVertex
    {
        float position[3];
        float normal[3];
        float color[4];
        float emission[4];
        // x = metallic, y = smoothness, z = occlusion, w = unlit.
        float material[4];
    };

    struct WebDrawRange
    {
        uint32_t firstIndex = 0;
        uint32_t indexCount = 0;
        bool transparent = false;
        bool castsShadows = true;
        bool line = false;
    };

    struct alignas(16) CameraData
    {
        struct alignas(16) PunctualLightData
        {
            glm::vec4 positionRange{0.0f};
            glm::vec4 colorIntensity{0.0f};
            glm::vec4 directionOuterCos{0.0f};
            // x = inner cone cosine, y = 0 point / 1 spot.
            glm::vec4 parameters{0.0f};
        };

        glm::mat4 viewProjection{1.0f};
        glm::mat4 inverseViewProjection{1.0f};
        glm::mat4 lightViewProjection{1.0f};
        glm::vec4 cameraPosition{0.0f, 0.0f, 0.0f, 1.0f};
        glm::vec4 lightDirectionStrength{0.35f, 0.82f, 0.45f, 0.0f};
        glm::vec4 lightColorIntensity{1.0f};
        glm::vec4 skyTopExposure{0.431f, 0.494f, 0.612f, 1.0f};
        glm::vec4 skyHorizon{0.651f, 0.725f, 0.816f, 1.0f};
        glm::vec4 skyGround{0.345f, 0.345f, 0.345f, 1.0f};
        glm::vec4 ambient{0.16f, 0.17f, 0.20f, 1.0f};
        glm::vec4 ambientSky{0.16f, 0.17f, 0.20f, 1.0f};
        // xyz = equator radiance, w = 0 flat / 1 gradient.
        glm::vec4 ambientEquator{0.16f, 0.17f, 0.20f, 0.0f};
        glm::vec4 ambientGround{0.16f, 0.17f, 0.20f, 1.0f};
        glm::uvec4 lightCounts{0u};
        std::array<PunctualLightData, 8> punctualLights{};
    };

    bool CreatePipelines();
    bool CreateShadowResources();
    bool EnsureBuffer(wgpu::Buffer &buffer, uint64_t &capacity, uint64_t required, wgpu::BufferUsage usage);
    bool BuildFrame(uint32_t width, uint32_t height);
    void ReportFrameIssue(const char *issue);

    wgpu::Device m_device;
    wgpu::Queue m_queue;
    wgpu::TextureFormat m_colorFormat = wgpu::TextureFormat::Undefined;
    wgpu::RenderPipeline m_opaquePipeline;
    wgpu::RenderPipeline m_transparentPipeline;
    wgpu::RenderPipeline m_skyPipeline;
    wgpu::RenderPipeline m_shadowPipeline;
    wgpu::BindGroupLayout m_cameraLayout;
    wgpu::BindGroup m_cameraGroup;
    wgpu::BindGroupLayout m_shadowCameraLayout;
    wgpu::BindGroup m_shadowCameraGroup;
    wgpu::Buffer m_cameraBuffer;
    wgpu::Buffer m_vertexBuffer;
    wgpu::Buffer m_indexBuffer;
    wgpu::Texture m_shadowTexture;
    wgpu::TextureView m_shadowView;
    wgpu::Sampler m_shadowSampler;
    uint64_t m_vertexCapacity = 0;
    uint64_t m_indexCapacity = 0;
    wgpu::Texture m_depthTexture;
    wgpu::TextureView m_depthView;
    uint32_t m_depthWidth = 0;
    uint32_t m_depthHeight = 0;
    uint32_t m_shadowResolution = 2048;
    SceneRenderExtractor m_extractor;
    RenderWorldSnapshot m_world;
    std::vector<WebVertex> m_vertices;
    std::vector<uint32_t> m_indices;
    std::vector<WebDrawRange> m_drawRanges;
    CameraData m_cameraData;
    std::string m_lastFrameIssue;
    bool m_reportedFirstFrame = false;
    bool m_reportedLineDraw = false;
    bool m_framePrepared = false;
    bool m_drawSky = true;
    bool m_shadowEnabled = false;
    bool m_diagnosticSkyEnabled = true;
    bool m_diagnosticShadowsEnabled = true;
};

} // namespace infernux::web
