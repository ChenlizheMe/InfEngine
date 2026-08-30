#include "WebScreenUIRenderer.h"

#include <function/renderer/gui/InxTextLayout.h>
#include <function/resources/InxTexture/InxTexture.h>
#include <imgui_internal.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <limits>
#include <vector>

namespace infernux::web
{
namespace
{
constexpr uint64_t kFontTextureId = 1;
constexpr float kPi = 3.14159265358979323846f;

constexpr const char *kScreenUIShader = R"(
struct VertexInput {
    @location(0) position: vec2<f32>,
    @location(1) uv: vec2<f32>,
    @location(2) color: vec4<f32>,
};
struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) uv: vec2<f32>,
    @location(1) color: vec4<f32>,
};
@group(0) @binding(0) var ui_sampler: sampler;
@group(0) @binding(1) var ui_texture: texture_2d<f32>;

@vertex fn vertex_main(input: VertexInput) -> VertexOutput {
    var output: VertexOutput;
    output.position = vec4<f32>(input.position, 0.0, 1.0);
    output.uv = input.uv;
    output.color = input.color;
    return output;
}

@fragment fn fragment_main(input: VertexOutput) -> @location(0) vec4<f32> {
    return input.color * textureSample(ui_texture, ui_sampler, input.uv);
}
)";

uint64_t GrowCapacity(uint64_t required)
{
    uint64_t capacity = 4096;
    while (capacity < required)
        capacity *= 2;
    return capacity;
}

wgpu::TextureFormat ToWebTextureFormat(TextureFormat format)
{
    switch (format) {
    case TextureFormat::Rgba8UNorm:
        return wgpu::TextureFormat::RGBA8Unorm;
    case TextureFormat::Rgba8Srgb:
        return wgpu::TextureFormat::RGBA8UnormSrgb;
    case TextureFormat::Rgba16UNorm:
        return wgpu::TextureFormat::RGBA16Unorm;
    case TextureFormat::Rgba16Float:
        return wgpu::TextureFormat::RGBA16Float;
    case TextureFormat::Rgba32Float:
        return wgpu::TextureFormat::RGBA32Float;
    default:
        return wgpu::TextureFormat::Undefined;
    }
}

void ResetDrawList(ImDrawList &drawList, uint32_t width, uint32_t height)
{
    drawList._ResetForNewFrame();
    drawList.PushTextureID(ImGui::GetIO().Fonts->TexRef);
    drawList.PushClipRect({0.0f, 0.0f}, {static_cast<float>(width), static_cast<float>(height)});
}

void TransformVertices(ImDrawList &drawList, int firstVertex, float minX, float minY, float maxX, float maxY,
                       float rotation, bool mirrorH, bool mirrorV)
{
    rotation = std::fmod(rotation, 360.0f);
    if (!mirrorH && !mirrorV && std::abs(rotation) < 0.001f)
        return;
    const float radians = rotation * kPi / 180.0f;
    const float cosine = std::cos(radians);
    const float sine = std::sin(radians);
    const ImVec2 pivot{(minX + maxX) * 0.5f, (minY + maxY) * 0.5f};
    for (int index = firstVertex; index < drawList.VtxBuffer.Size; ++index) {
        ImVec2 local{drawList.VtxBuffer[index].pos.x - pivot.x, drawList.VtxBuffer[index].pos.y - pivot.y};
        if (mirrorH)
            local.x = -local.x;
        if (mirrorV)
            local.y = -local.y;
        drawList.VtxBuffer[index].pos = {pivot.x + local.x * cosine - local.y * sine,
                                         pivot.y + local.x * sine + local.y * cosine};
    }
}
} // namespace

WebScreenUIRenderer::~WebScreenUIRenderer()
{
    if (m_context)
        ImGui::SetCurrentContext(m_context);
    if (m_camera)
        IM_DELETE(m_camera);
    if (m_overlay)
        IM_DELETE(m_overlay);
    if (m_ownsContext && m_context)
        ImGui::DestroyContext(m_context);
}

bool WebScreenUIRenderer::Initialize(wgpu::Device device, wgpu::Queue queue, wgpu::TextureFormat colorFormat)
{
    m_device = device;
    m_queue = queue;
    m_colorFormat = colorFormat;
    m_context = ImGui::GetCurrentContext();
    if (!m_context) {
        m_context = ImGui::CreateContext();
        m_ownsContext = true;
    }
    ImGui::SetCurrentContext(m_context);
    ImGuiIO &io = ImGui::GetIO();
    io.DisplaySize = {1280.0f, 720.0f};
    io.DeltaTime = 1.0f / 60.0f;
    unsigned char *pixels = nullptr;
    int atlasWidth = 0;
    int atlasHeight = 0;
    io.Fonts->GetTexDataAsRGBA32(&pixels, &atlasWidth, &atlasHeight);
    if (!pixels || atlasWidth < 1 || atlasHeight < 1)
        return false;
    io.Fonts->SetTexID(static_cast<ImTextureID>(kFontTextureId));
    ImGui::NewFrame();
    ImGui::EndFrame();
    m_camera = IM_NEW(ImDrawList)(ImGui::GetDrawListSharedData());
    m_overlay = IM_NEW(ImDrawList)(ImGui::GetDrawListSharedData());

    wgpu::TextureDescriptor textureDescriptor;
    textureDescriptor.dimension = wgpu::TextureDimension::e2D;
    textureDescriptor.size = {static_cast<uint32_t>(atlasWidth), static_cast<uint32_t>(atlasHeight), 1};
    textureDescriptor.format = wgpu::TextureFormat::RGBA8Unorm;
    textureDescriptor.mipLevelCount = 1;
    textureDescriptor.sampleCount = 1;
    textureDescriptor.usage = wgpu::TextureUsage::CopyDst | wgpu::TextureUsage::TextureBinding;
    m_fontTexture = m_device.CreateTexture(&textureDescriptor);
    m_fontView = m_fontTexture ? m_fontTexture.CreateView() : wgpu::TextureView{};
    wgpu::TexelCopyTextureInfo destination;
    destination.texture = m_fontTexture;
    wgpu::TexelCopyBufferLayout layout;
    layout.bytesPerRow = static_cast<uint32_t>(atlasWidth * 4);
    layout.rowsPerImage = static_cast<uint32_t>(atlasHeight);
    const wgpu::Extent3D extent{static_cast<uint32_t>(atlasWidth), static_cast<uint32_t>(atlasHeight), 1};
    m_queue.WriteTexture(&destination, pixels, static_cast<size_t>(atlasWidth * atlasHeight * 4), &layout, &extent);

    wgpu::SamplerDescriptor samplerDescriptor;
    samplerDescriptor.addressModeU = wgpu::AddressMode::ClampToEdge;
    samplerDescriptor.addressModeV = wgpu::AddressMode::ClampToEdge;
    samplerDescriptor.minFilter = wgpu::FilterMode::Linear;
    samplerDescriptor.magFilter = wgpu::FilterMode::Linear;
    m_fontSampler = m_device.CreateSampler(&samplerDescriptor);
    return m_fontTexture && m_fontView && m_fontSampler && CreatePipelineAndFontAtlas();
}

bool WebScreenUIRenderer::CreatePipelineAndFontAtlas()
{
    std::array<wgpu::BindGroupLayoutEntry, 2> layoutEntries{};
    layoutEntries[0].binding = 0;
    layoutEntries[0].visibility = wgpu::ShaderStage::Fragment;
    layoutEntries[0].sampler.type = wgpu::SamplerBindingType::Filtering;
    layoutEntries[1].binding = 1;
    layoutEntries[1].visibility = wgpu::ShaderStage::Fragment;
    layoutEntries[1].texture.sampleType = wgpu::TextureSampleType::Float;
    layoutEntries[1].texture.viewDimension = wgpu::TextureViewDimension::e2D;
    wgpu::BindGroupLayoutDescriptor layoutDescriptor;
    layoutDescriptor.entryCount = layoutEntries.size();
    layoutDescriptor.entries = layoutEntries.data();
    m_textureLayout = m_device.CreateBindGroupLayout(&layoutDescriptor);

    std::array<wgpu::BindGroupEntry, 2> groupEntries{};
    groupEntries[0].binding = 0;
    groupEntries[0].sampler = m_fontSampler;
    groupEntries[1].binding = 1;
    groupEntries[1].textureView = m_fontView;
    wgpu::BindGroupDescriptor groupDescriptor;
    groupDescriptor.layout = m_textureLayout;
    groupDescriptor.entryCount = groupEntries.size();
    groupDescriptor.entries = groupEntries.data();
    m_fontGroup = m_device.CreateBindGroup(&groupDescriptor);

    wgpu::ShaderSourceWGSL shaderSource;
    shaderSource.code = kScreenUIShader;
    wgpu::ShaderModuleDescriptor shaderDescriptor;
    shaderDescriptor.nextInChain = &shaderSource;
    const wgpu::ShaderModule shader = m_device.CreateShaderModule(&shaderDescriptor);

    std::array<wgpu::VertexAttribute, 3> attributes{};
    attributes[0].format = wgpu::VertexFormat::Float32x2;
    attributes[0].offset = offsetof(GPUVertex, position);
    attributes[0].shaderLocation = 0;
    attributes[1].format = wgpu::VertexFormat::Float32x2;
    attributes[1].offset = offsetof(GPUVertex, uv);
    attributes[1].shaderLocation = 1;
    attributes[2].format = wgpu::VertexFormat::Float32x4;
    attributes[2].offset = offsetof(GPUVertex, color);
    attributes[2].shaderLocation = 2;
    wgpu::VertexBufferLayout vertexLayout;
    vertexLayout.arrayStride = sizeof(GPUVertex);
    vertexLayout.stepMode = wgpu::VertexStepMode::Vertex;
    vertexLayout.attributeCount = attributes.size();
    vertexLayout.attributes = attributes.data();

    wgpu::BlendState blend;
    blend.color.srcFactor = wgpu::BlendFactor::SrcAlpha;
    blend.color.dstFactor = wgpu::BlendFactor::OneMinusSrcAlpha;
    blend.alpha.srcFactor = wgpu::BlendFactor::One;
    blend.alpha.dstFactor = wgpu::BlendFactor::OneMinusSrcAlpha;
    wgpu::ColorTargetState colorTarget;
    colorTarget.format = m_colorFormat;
    colorTarget.blend = &blend;
    colorTarget.writeMask = wgpu::ColorWriteMask::All;
    wgpu::FragmentState fragment;
    fragment.module = shader;
    fragment.entryPoint = "fragment_main";
    fragment.targetCount = 1;
    fragment.targets = &colorTarget;
    wgpu::PipelineLayoutDescriptor pipelineLayoutDescriptor;
    pipelineLayoutDescriptor.bindGroupLayoutCount = 1;
    pipelineLayoutDescriptor.bindGroupLayouts = &m_textureLayout;
    wgpu::RenderPipelineDescriptor descriptor;
    descriptor.layout = m_device.CreatePipelineLayout(&pipelineLayoutDescriptor);
    descriptor.vertex.module = shader;
    descriptor.vertex.entryPoint = "vertex_main";
    descriptor.vertex.bufferCount = 1;
    descriptor.vertex.buffers = &vertexLayout;
    descriptor.fragment = &fragment;
    descriptor.primitive.topology = wgpu::PrimitiveTopology::TriangleList;
    descriptor.primitive.cullMode = wgpu::CullMode::None;
    descriptor.multisample.count = 1;
    m_pipeline = m_device.CreateRenderPipeline(&descriptor);
    return m_textureLayout && m_fontGroup && m_pipeline;
}

ImDrawList *WebScreenUIRenderer::DrawList(int list) const
{
    return list == 0 ? m_camera : m_overlay;
}

void WebScreenUIRenderer::BeginFrame(uint32_t width, uint32_t height)
{
    if (!m_context || !m_camera || !m_overlay)
        return;
    ImGui::SetCurrentContext(m_context);
    m_cacheValid = false;
    ResetDrawList(*m_camera, width, height);
    ResetDrawList(*m_overlay, width, height);
}

bool WebScreenUIRenderer::BeginFrameCached(uint32_t width, uint32_t height, uint64_t contentRevision)
{
    if (m_cacheValid && width == m_cachedWidth && height == m_cachedHeight && contentRevision == m_cachedRevision)
        return true;
    BeginFrame(width, height);
    m_cacheValid = true;
    m_cachedWidth = width;
    m_cachedHeight = height;
    m_cachedRevision = contentRevision;
    return false;
}

void WebScreenUIRenderer::AddFilledRect(int list, float minX, float minY, float maxX, float maxY, float r, float g,
                                        float b, float a, float rounding)
{
    ImDrawList *draw = DrawList(list);
    if (draw)
        draw->AddRectFilled({minX, minY}, {maxX, maxY}, ImGui::ColorConvertFloat4ToU32({r, g, b, a}), rounding);
}

void WebScreenUIRenderer::AddImage(int list, uint64_t textureId, float minX, float minY, float maxX, float maxY,
                                   float uv0X, float uv0Y, float uv1X, float uv1Y, float r, float g, float b, float a,
                                   float rotation, bool mirrorH, bool mirrorV, float rounding)
{
    ImDrawList *draw = DrawList(list);
    if (!draw || m_textures.find(textureId) == m_textures.end())
        return;
    const int firstVertex = draw->VtxBuffer.Size;
    const ImU32 tint = ImGui::ColorConvertFloat4ToU32({r, g, b, a});
    if (rounding > 0.5f)
        draw->AddImageRounded(static_cast<ImTextureID>(textureId), {minX, minY}, {maxX, maxY}, {uv0X, uv0Y},
                              {uv1X, uv1Y}, tint, rounding);
    else
        draw->AddImage(static_cast<ImTextureID>(textureId), {minX, minY}, {maxX, maxY}, {uv0X, uv0Y}, {uv1X, uv1Y},
                       tint);
    TransformVertices(*draw, firstVertex, minX, minY, maxX, maxY, rotation, mirrorH, mirrorV);
}

void WebScreenUIRenderer::AddText(int list, float minX, float minY, float maxX, float maxY, const std::string &text,
                                  float r, float g, float b, float a, float alignX, float alignY, float fontSize,
                                  float wrapWidth, float rotation, bool mirrorH, bool mirrorV,
                                  const std::string &fontPath, float lineHeight, float letterSpacing)
{
    ImDrawList *draw = DrawList(list);
    if (!draw || text.empty())
        return;
    ImGui::SetCurrentContext(m_context);
    const textlayout::TextLayoutResult layout = textlayout::LayoutText(
        {text, fontPath, textlayout::ResolveFontSize(fontSize), wrapWidth, lineHeight, letterSpacing});
    const int firstVertex = draw->VtxBuffer.Size;
    draw->PushTextureID(ImGui::GetIO().Fonts->TexRef);
    textlayout::RenderTextBox(draw, minX, minY, maxX, maxY, layout, ImGui::ColorConvertFloat4ToU32({r, g, b, a}),
                              alignX, alignY, letterSpacing);
    draw->PopTextureID();
    TransformVertices(*draw, firstVertex, minX, minY, maxX, maxY, rotation, mirrorH, mirrorV);
}

std::pair<float, float> WebScreenUIRenderer::MeasureText(const std::string &text, float fontSize, float wrapWidth,
                                                         const std::string &fontPath, float lineHeight,
                                                         float letterSpacing) const
{
    ImGui::SetCurrentContext(m_context);
    const textlayout::TextLayoutResult layout = textlayout::LayoutText(
        {text, fontPath, textlayout::ResolveFontSize(fontSize), wrapWidth, lineHeight, letterSpacing});
    return {layout.totalWidth, layout.totalHeight};
}

uint64_t WebScreenUIRenderer::UploadTexture(const TextureCpuData &texture)
{
    if (!m_device || !m_queue || texture.dimension != TextureDimension::Texture2D || !texture.IsValid())
        return 0;
    const wgpu::TextureFormat format = ToWebTextureFormat(texture.format);
    if (format == wgpu::TextureFormat::Undefined)
        return 0;
    const TextureMipLevel &mip = texture.mipLevels.front();
    if (mip.width == 0 || mip.height == 0 || mip.depth != 1 || mip.byteOffset > texture.bytes.size() ||
        mip.byteSize > texture.bytes.size() - mip.byteOffset || mip.rowPitch > std::numeric_limits<uint32_t>::max())
        return 0;

    wgpu::TextureDescriptor descriptor;
    descriptor.dimension = wgpu::TextureDimension::e2D;
    descriptor.size = {mip.width, mip.height, 1};
    descriptor.format = format;
    descriptor.mipLevelCount = 1;
    descriptor.sampleCount = 1;
    descriptor.usage = wgpu::TextureUsage::CopyDst | wgpu::TextureUsage::TextureBinding;
    GPUTexture gpu;
    gpu.texture = m_device.CreateTexture(&descriptor);
    gpu.view = gpu.texture ? gpu.texture.CreateView() : wgpu::TextureView{};

    wgpu::SamplerDescriptor samplerDescriptor;
    samplerDescriptor.addressModeU = wgpu::AddressMode::ClampToEdge;
    samplerDescriptor.addressModeV = wgpu::AddressMode::ClampToEdge;
    samplerDescriptor.minFilter = wgpu::FilterMode::Linear;
    samplerDescriptor.magFilter = wgpu::FilterMode::Linear;
    gpu.sampler = m_device.CreateSampler(&samplerDescriptor);
    if (!gpu.texture || !gpu.view || !gpu.sampler)
        return 0;

    wgpu::TexelCopyTextureInfo destination;
    destination.texture = gpu.texture;
    wgpu::TexelCopyBufferLayout layout;
    layout.bytesPerRow = static_cast<uint32_t>(mip.rowPitch);
    layout.rowsPerImage = mip.height;
    const wgpu::Extent3D extent{mip.width, mip.height, 1};
    m_queue.WriteTexture(&destination, texture.bytes.data() + mip.byteOffset, static_cast<size_t>(mip.byteSize),
                         &layout, &extent);

    std::array<wgpu::BindGroupEntry, 2> entries{};
    entries[0].binding = 0;
    entries[0].sampler = gpu.sampler;
    entries[1].binding = 1;
    entries[1].textureView = gpu.view;
    wgpu::BindGroupDescriptor groupDescriptor;
    groupDescriptor.layout = m_textureLayout;
    groupDescriptor.entryCount = entries.size();
    groupDescriptor.entries = entries.data();
    gpu.group = m_device.CreateBindGroup(&groupDescriptor);
    if (!gpu.group)
        return 0;

    const uint64_t id = m_nextTextureId++;
    m_textures.emplace(id, std::move(gpu));
    m_cacheValid = false;
    return id;
}

bool WebScreenUIRenderer::EnsureBuffer(wgpu::Buffer &buffer, uint64_t &capacity, uint64_t required,
                                       wgpu::BufferUsage usage)
{
    if (required == 0)
        return false;
    if (buffer && required <= capacity)
        return true;
    capacity = GrowCapacity(required);
    wgpu::BufferDescriptor descriptor;
    descriptor.size = capacity;
    descriptor.usage = usage | wgpu::BufferUsage::CopyDst;
    buffer = m_device.CreateBuffer(&descriptor);
    return static_cast<bool>(buffer);
}

bool WebScreenUIRenderer::Render(wgpu::RenderPassEncoder pass, int list, uint32_t width, uint32_t height)
{
    ImDrawList *draw = DrawList(list);
    if (!pass || !m_pipeline || !draw || draw->VtxBuffer.empty() || draw->IdxBuffer.empty() || width == 0 ||
        height == 0)
        return false;
    std::vector<GPUVertex> vertices(static_cast<size_t>(draw->VtxBuffer.Size));
    for (int index = 0; index < draw->VtxBuffer.Size; ++index) {
        const ImDrawVert &source = draw->VtxBuffer[index];
        GPUVertex &target = vertices[static_cast<size_t>(index)];
        target.position[0] = source.pos.x * 2.0f / static_cast<float>(width) - 1.0f;
        target.position[1] = 1.0f - source.pos.y * 2.0f / static_cast<float>(height);
        target.uv[0] = source.uv.x;
        target.uv[1] = source.uv.y;
        const ImVec4 color = ImGui::ColorConvertU32ToFloat4(source.col);
        target.color[0] = color.x;
        target.color[1] = color.y;
        target.color[2] = color.z;
        target.color[3] = color.w;
    }
    const uint64_t vertexBytes = vertices.size() * sizeof(GPUVertex);
    const uint64_t indexBytes = static_cast<uint64_t>(draw->IdxBuffer.Size) * sizeof(ImDrawIdx);
    if (!EnsureBuffer(m_vertexBuffer, m_vertexCapacity, vertexBytes, wgpu::BufferUsage::Vertex) ||
        !EnsureBuffer(m_indexBuffer, m_indexCapacity, indexBytes, wgpu::BufferUsage::Index))
        return false;
    m_queue.WriteBuffer(m_vertexBuffer, 0, vertices.data(), vertexBytes);
    m_queue.WriteBuffer(m_indexBuffer, 0, draw->IdxBuffer.Data, indexBytes);
    pass.SetPipeline(m_pipeline);
    pass.SetVertexBuffer(0, m_vertexBuffer, 0, vertexBytes);
    pass.SetIndexBuffer(m_indexBuffer, sizeof(ImDrawIdx) == 2 ? wgpu::IndexFormat::Uint16 : wgpu::IndexFormat::Uint32,
                        0, indexBytes);
    for (const ImDrawCmd &command : draw->CmdBuffer) {
        if (command.ElemCount == 0 || command.UserCallback)
            continue;
        const int left = std::max(0, static_cast<int>(std::floor(command.ClipRect.x)));
        const int top = std::max(0, static_cast<int>(std::floor(command.ClipRect.y)));
        const int right = std::min(static_cast<int>(width), static_cast<int>(std::ceil(command.ClipRect.z)));
        const int bottom = std::min(static_cast<int>(height), static_cast<int>(std::ceil(command.ClipRect.w)));
        if (right <= left || bottom <= top)
            continue;
        const uint64_t textureId = static_cast<uint64_t>(command.GetTexID());
        if (textureId == kFontTextureId) {
            pass.SetBindGroup(0, m_fontGroup);
        } else {
            const auto texture = m_textures.find(textureId);
            if (texture == m_textures.end())
                continue;
            pass.SetBindGroup(0, texture->second.group);
        }
        pass.SetScissorRect(static_cast<uint32_t>(left), static_cast<uint32_t>(top),
                            static_cast<uint32_t>(right - left), static_cast<uint32_t>(bottom - top));
        pass.DrawIndexed(command.ElemCount, 1, command.IdxOffset, static_cast<int32_t>(command.VtxOffset), 0);
    }
    if (!m_reportedFirstDraw) {
        std::printf("INFERNUX_WEB_SCREEN_UI_READY vertices=%d indices=%d\n", draw->VtxBuffer.Size,
                    draw->IdxBuffer.Size);
        m_reportedFirstDraw = true;
    }
    return true;
}

} // namespace infernux::web
