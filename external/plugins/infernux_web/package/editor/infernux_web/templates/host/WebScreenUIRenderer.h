#pragma once

#include <imgui.h>
#include <webgpu/webgpu_cpp.h>

#include <cstdint>
#include <string>
#include <unordered_map>
#include <utility>

namespace infernux
{
struct TextureCpuData;
}

namespace infernux::web
{

/// Engine-owned Screen UI consumer for the WebGPU Player.
///
/// Python submits the same runtime UI packets used by the Vulkan Player. This
/// class owns only command accumulation, font-atlas upload, and WebGPU draw
/// recording; no visible browser DOM participates in presentation.
class WebScreenUIRenderer final
{
  public:
    WebScreenUIRenderer() = default;
    ~WebScreenUIRenderer();

    bool Initialize(wgpu::Device device, wgpu::Queue queue, wgpu::TextureFormat colorFormat);
    void BeginFrame(uint32_t width, uint32_t height);
    bool BeginFrameCached(uint32_t width, uint32_t height, uint64_t contentRevision);

    void AddFilledRect(int list, float minX, float minY, float maxX, float maxY, float r, float g, float b, float a,
                       float rounding);
    void AddImage(int list, uint64_t textureId, float minX, float minY, float maxX, float maxY, float uv0X, float uv0Y,
                  float uv1X, float uv1Y, float r, float g, float b, float a, float rotation, bool mirrorH,
                  bool mirrorV, float rounding);
    void AddText(int list, float minX, float minY, float maxX, float maxY, const std::string &text, float r, float g,
                 float b, float a, float alignX, float alignY, float fontSize, float wrapWidth, float rotation,
                 bool mirrorH, bool mirrorV, const std::string &fontPath, float lineHeight, float letterSpacing);
    [[nodiscard]] std::pair<float, float> MeasureText(const std::string &text, float fontSize, float wrapWidth,
                                                      const std::string &fontPath, float lineHeight,
                                                      float letterSpacing) const;
    [[nodiscard]] uint64_t UploadTexture(const TextureCpuData &texture, uint64_t replaceTextureId = 0);
    void ReleaseTexture(uint64_t textureId);

    bool Render(wgpu::RenderPassEncoder pass, int list, uint32_t width, uint32_t height);

  private:
    struct GPUVertex
    {
        float position[2];
        float uv[2];
        float color[4];
    };

    struct GPUTexture
    {
        wgpu::Texture texture;
        wgpu::TextureView view;
        wgpu::Sampler sampler;
        wgpu::BindGroup group;
    };

    ImDrawList *DrawList(int list) const;
    bool CreatePipelineAndFontAtlas();
    bool RefreshFontAtlas();
    bool EnsureBuffer(wgpu::Buffer &buffer, uint64_t &capacity, uint64_t required, wgpu::BufferUsage usage);

    wgpu::Device m_device;
    wgpu::Queue m_queue;
    wgpu::TextureFormat m_colorFormat = wgpu::TextureFormat::Undefined;
    wgpu::RenderPipeline m_pipeline;
    wgpu::BindGroupLayout m_textureLayout;
    wgpu::BindGroup m_fontGroup;
    wgpu::Texture m_fontTexture;
    wgpu::TextureView m_fontView;
    wgpu::Sampler m_fontSampler;
    uint32_t m_fontWidth = 0;
    uint32_t m_fontHeight = 0;
    wgpu::Buffer m_vertexBuffer;
    wgpu::Buffer m_indexBuffer;
    uint64_t m_vertexCapacity = 0;
    uint64_t m_indexCapacity = 0;
    std::unordered_map<uint64_t, GPUTexture> m_textures;
    uint64_t m_nextTextureId = 2;
    ImGuiContext *m_context = nullptr;
    ImDrawList *m_camera = nullptr;
    ImDrawList *m_overlay = nullptr;
    bool m_ownsContext = false;
    bool m_cacheValid = false;
    uint32_t m_cachedWidth = 0;
    uint32_t m_cachedHeight = 0;
    uint64_t m_cachedRevision = 0;
    bool m_reportedFirstDraw = false;
    bool m_fontAtlasDirty = false;
};

} // namespace infernux::web
