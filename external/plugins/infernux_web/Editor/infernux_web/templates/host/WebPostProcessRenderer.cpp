#include "WebPostProcessRenderer.h"

#include <algorithm>
#include <array>
#include <cstdio>

namespace infernux::web
{
namespace
{
constexpr auto kHdrFormat = wgpu::TextureFormat::RGBA16Float;

constexpr char kPostProcessShader[] = R"wgsl(
struct Parameters {
    inverse_resolution: vec2<f32>,
    bloom_threshold: f32,
    bloom_intensity: f32,
    exposure: f32,
    encode_srgb: f32,
    padding: vec2<f32>,
};

@group(0) @binding(0) var scene_sampler: sampler;
@group(0) @binding(1) var scene_color: texture_2d<f32>;
@group(0) @binding(2) var<uniform> parameters: Parameters;

struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) uv: vec2<f32>,
};

@vertex
fn vertex_main(@builtin(vertex_index) vertex_index: u32) -> VertexOutput {
    var positions = array<vec2<f32>, 3>(
        vec2<f32>(-1.0, -1.0),
        vec2<f32>(3.0, -1.0),
        vec2<f32>(-1.0, 3.0)
    );
    var output: VertexOutput;
    output.position = vec4<f32>(positions[vertex_index], 0.0, 1.0);
    output.uv = positions[vertex_index] * vec2<f32>(0.5, -0.5) + vec2<f32>(0.5, 0.5);
    return output;
}

fn bright_part(color: vec3<f32>) -> vec3<f32> {
    let brightness = max(max(color.r, color.g), color.b);
    let contribution = max(brightness - parameters.bloom_threshold, 0.0) / max(brightness, 0.0001);
    return color * contribution;
}

fn bloom(uv: vec2<f32>) -> vec3<f32> {
    // A compact multi-radius tent filter. Keeping this in the resolve pass
    // avoids extra full-size transient textures while preserving HDR values.
    let texel = parameters.inverse_resolution;
    var result = bright_part(textureSampleLevel(scene_color, scene_sampler, uv, 0.0).rgb) * 0.18;
    let offsets = array<vec2<f32>, 12>(
        vec2<f32>( 1.0,  0.0), vec2<f32>(-1.0,  0.0),
        vec2<f32>( 0.0,  1.0), vec2<f32>( 0.0, -1.0),
        vec2<f32>( 2.0,  2.0), vec2<f32>(-2.0,  2.0),
        vec2<f32>( 2.0, -2.0), vec2<f32>(-2.0, -2.0),
        vec2<f32>( 6.0,  0.0), vec2<f32>(-6.0,  0.0),
        vec2<f32>( 0.0,  6.0), vec2<f32>( 0.0, -6.0)
    );
    let weights = array<f32, 12>(
        0.09, 0.09, 0.09, 0.09,
        0.055, 0.055, 0.055, 0.055,
        0.025, 0.025, 0.025, 0.025
    );
    for (var index = 0; index < 12; index = index + 1) {
        let sample_color = textureSampleLevel(scene_color, scene_sampler, uv + offsets[index] * texel, 0.0).rgb;
        result += bright_part(sample_color) * weights[index];
    }
    return result * parameters.bloom_intensity;
}

fn aces_film(color: vec3<f32>) -> vec3<f32> {
    let a = 2.51;
    let b = 0.03;
    let c = 2.43;
    let d = 0.59;
    let e = 0.14;
    return clamp((color * (a * color + b)) / (color * (c * color + d) + e), vec3<f32>(0.0), vec3<f32>(1.0));
}

@fragment
fn fragment_main(input: VertexOutput) -> @location(0) vec4<f32> {
    let hdr = textureSampleLevel(scene_color, scene_sampler, input.uv, 0.0).rgb;
    var color = aces_film((hdr + bloom(input.uv)) * parameters.exposure);
    if (parameters.encode_srgb > 0.5) {
        color = pow(color, vec3<f32>(1.0 / 2.2));
    }
    return vec4<f32>(color, 1.0);
}
)wgsl";

bool IsSrgb(wgpu::TextureFormat format)
{
    return format == wgpu::TextureFormat::RGBA8UnormSrgb || format == wgpu::TextureFormat::BGRA8UnormSrgb;
}
} // namespace

bool WebPostProcessRenderer::Initialize(wgpu::Device device, wgpu::TextureFormat surfaceFormat)
{
    m_device = device;
    m_surfaceFormat = surfaceFormat;
    if (!m_device || surfaceFormat == wgpu::TextureFormat::Undefined)
        return false;

    wgpu::SamplerDescriptor samplerDescriptor;
    samplerDescriptor.addressModeU = wgpu::AddressMode::ClampToEdge;
    samplerDescriptor.addressModeV = wgpu::AddressMode::ClampToEdge;
    samplerDescriptor.minFilter = wgpu::FilterMode::Linear;
    samplerDescriptor.magFilter = wgpu::FilterMode::Linear;
    m_sampler = m_device.CreateSampler(&samplerDescriptor);

    wgpu::BufferDescriptor bufferDescriptor;
    bufferDescriptor.size = sizeof(Parameters);
    bufferDescriptor.usage = wgpu::BufferUsage::Uniform | wgpu::BufferUsage::CopyDst;
    m_parameterBuffer = m_device.CreateBuffer(&bufferDescriptor);

    std::array<wgpu::BindGroupLayoutEntry, 3> entries{};
    entries[0].binding = 0;
    entries[0].visibility = wgpu::ShaderStage::Fragment;
    entries[0].sampler.type = wgpu::SamplerBindingType::Filtering;
    entries[1].binding = 1;
    entries[1].visibility = wgpu::ShaderStage::Fragment;
    entries[1].texture.sampleType = wgpu::TextureSampleType::Float;
    entries[1].texture.viewDimension = wgpu::TextureViewDimension::e2D;
    entries[2].binding = 2;
    entries[2].visibility = wgpu::ShaderStage::Fragment;
    entries[2].buffer.type = wgpu::BufferBindingType::Uniform;
    entries[2].buffer.minBindingSize = sizeof(Parameters);
    wgpu::BindGroupLayoutDescriptor layoutDescriptor;
    layoutDescriptor.entryCount = entries.size();
    layoutDescriptor.entries = entries.data();
    m_layout = m_device.CreateBindGroupLayout(&layoutDescriptor);

    m_parameters.encodeSrgb = IsSrgb(surfaceFormat) ? 0.0f : 1.0f;
    return m_sampler && m_parameterBuffer && m_layout && CreatePipeline();
}

bool WebPostProcessRenderer::CreatePipeline()
{
    wgpu::ShaderSourceWGSL shaderSource;
    shaderSource.code = kPostProcessShader;
    wgpu::ShaderModuleDescriptor shaderDescriptor;
    shaderDescriptor.nextInChain = &shaderSource;
    const wgpu::ShaderModule shader = m_device.CreateShaderModule(&shaderDescriptor);

    wgpu::ColorTargetState colorTarget;
    colorTarget.format = m_surfaceFormat;
    wgpu::FragmentState fragment;
    fragment.module = shader;
    fragment.entryPoint = "fragment_main";
    fragment.targetCount = 1;
    fragment.targets = &colorTarget;
    wgpu::PipelineLayoutDescriptor pipelineLayoutDescriptor;
    pipelineLayoutDescriptor.bindGroupLayoutCount = 1;
    pipelineLayoutDescriptor.bindGroupLayouts = &m_layout;
    wgpu::RenderPipelineDescriptor pipelineDescriptor;
    pipelineDescriptor.layout = m_device.CreatePipelineLayout(&pipelineLayoutDescriptor);
    pipelineDescriptor.vertex.module = shader;
    pipelineDescriptor.vertex.entryPoint = "vertex_main";
    pipelineDescriptor.fragment = &fragment;
    pipelineDescriptor.primitive.topology = wgpu::PrimitiveTopology::TriangleList;
    pipelineDescriptor.primitive.cullMode = wgpu::CullMode::None;
    m_pipeline = m_device.CreateRenderPipeline(&pipelineDescriptor);
    return static_cast<bool>(m_pipeline);
}

bool WebPostProcessRenderer::Resize(uint32_t width, uint32_t height)
{
    width = std::max(1u, width);
    height = std::max(1u, height);
    if (m_width == width && m_height == height && m_sceneColorView && m_bindGroup)
        return true;
    m_width = width;
    m_height = height;
    m_parameters.inverseWidth = 1.0f / static_cast<float>(width);
    m_parameters.inverseHeight = 1.0f / static_cast<float>(height);
    return CreateSceneTarget() && CreateSceneBindGroup();
}

bool WebPostProcessRenderer::CreateSceneTarget()
{
    wgpu::TextureDescriptor descriptor;
    descriptor.size = {m_width, m_height, 1};
    descriptor.dimension = wgpu::TextureDimension::e2D;
    descriptor.format = kHdrFormat;
    descriptor.mipLevelCount = 1;
    descriptor.sampleCount = 1;
    descriptor.usage = wgpu::TextureUsage::RenderAttachment | wgpu::TextureUsage::TextureBinding;
    m_sceneColor = m_device.CreateTexture(&descriptor);
    m_sceneColorView = m_sceneColor ? m_sceneColor.CreateView() : wgpu::TextureView{};
    return static_cast<bool>(m_sceneColorView);
}

bool WebPostProcessRenderer::CreateSceneBindGroup()
{
    std::array<wgpu::BindGroupEntry, 3> entries{};
    entries[0].binding = 0;
    entries[0].sampler = m_sampler;
    entries[1].binding = 1;
    entries[1].textureView = m_sceneColorView;
    entries[2].binding = 2;
    entries[2].buffer = m_parameterBuffer;
    entries[2].size = sizeof(Parameters);
    wgpu::BindGroupDescriptor descriptor;
    descriptor.layout = m_layout;
    descriptor.entryCount = entries.size();
    descriptor.entries = entries.data();
    m_bindGroup = m_device.CreateBindGroup(&descriptor);
    return static_cast<bool>(m_bindGroup);
}

wgpu::TextureFormat WebPostProcessRenderer::SceneColorFormat() const noexcept
{
    return kHdrFormat;
}

wgpu::TextureView WebPostProcessRenderer::SceneColorView() const noexcept
{
    return m_sceneColorView;
}

bool WebPostProcessRenderer::Render(wgpu::RenderPassEncoder pass)
{
    if (!pass || !m_pipeline || !m_bindGroup || !m_sceneColorView)
        return false;
    m_device.GetQueue().WriteBuffer(m_parameterBuffer, 0, &m_parameters, sizeof(m_parameters));
    pass.SetPipeline(m_pipeline);
    pass.SetBindGroup(0, m_bindGroup);
    pass.Draw(3, 1, 0, 0);
    if (!m_reportedReady) {
        std::printf("INFERNUX_WEB_POST_PROCESS_READY hdr=rgba16float bloom=1 tonemap=aces\n");
        m_reportedReady = true;
    }
    return true;
}

} // namespace infernux::web
