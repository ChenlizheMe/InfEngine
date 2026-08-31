#include "WebPostProcessRenderer.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>

namespace infernux::web
{
namespace
{
constexpr auto kHdrFormat = wgpu::TextureFormat::RGBA16Float;

constexpr char kDownsampleShader[] = R"wgsl(
struct Parameters {
    inverse_resolution: vec2<f32>,
    bloom_threshold: f32,
    knee: f32,
    clamp_max: f32,
    prefilter: f32,
    padding: vec2<f32>,
};
@group(0) @binding(0) var source_sampler: sampler;
@group(0) @binding(1) var source_texture: texture_2d<f32>;
@group(0) @binding(2) var<uniform> parameters: Parameters;
struct VertexOutput { @builtin(position) position: vec4<f32>, @location(0) uv: vec2<f32>, };
@vertex fn vertex_main(@builtin(vertex_index) index: u32) -> VertexOutput {
    let positions = array<vec2<f32>, 3>(vec2<f32>(-1.0, -1.0), vec2<f32>(3.0, -1.0), vec2<f32>(-1.0, 3.0));
    var output: VertexOutput;
    output.position = vec4<f32>(positions[index], 0.0, 1.0);
    output.uv = positions[index] * vec2<f32>(0.5, -0.5) + vec2<f32>(0.5, 0.5);
    return output;
}
fn sample_source(uv: vec2<f32>) -> vec3<f32> {
    return textureSampleLevel(source_texture, source_sampler, uv, 0.0).rgb;
}
fn downsample_13(uv: vec2<f32>) -> vec3<f32> {
    let t = parameters.inverse_resolution;
    let a = sample_source(uv + t * vec2<f32>(-1.0, -1.0));
    let b = sample_source(uv + t * vec2<f32>( 0.0, -1.0));
    let c = sample_source(uv + t * vec2<f32>( 1.0, -1.0));
    let d = sample_source(uv + t * vec2<f32>(-1.0,  0.0));
    let e = sample_source(uv);
    let f = sample_source(uv + t * vec2<f32>( 1.0,  0.0));
    let g = sample_source(uv + t * vec2<f32>(-1.0,  1.0));
    let h = sample_source(uv + t * vec2<f32>( 0.0,  1.0));
    let i = sample_source(uv + t * vec2<f32>( 1.0,  1.0));
    let j = sample_source(uv + t * vec2<f32>(-0.5, -0.5));
    let k = sample_source(uv + t * vec2<f32>( 0.5, -0.5));
    let l = sample_source(uv + t * vec2<f32>(-0.5,  0.5));
    let m = sample_source(uv + t * vec2<f32>( 0.5,  0.5));
    return e * 0.125 + (a + c + g + i) * 0.03125 +
           (b + d + f + h) * 0.0625 + (j + k + l + m) * 0.125;
}
fn quadratic_threshold(color: vec3<f32>) -> vec3<f32> {
    let brightness = max(max(color.r, color.g), color.b);
    let soft_knee = parameters.bloom_threshold * parameters.knee;
    var rq = clamp(brightness - parameters.bloom_threshold + soft_knee, 0.0, 2.0 * soft_knee);
    rq = rq * rq / (4.0 * soft_knee + 0.00001);
    let contribution = max(rq, brightness - parameters.bloom_threshold) / max(brightness, 0.00001);
    return color * max(contribution, 0.0);
}
@fragment fn fragment_main(input: VertexOutput) -> @location(0) vec4<f32> {
    var color = select(downsample_13(input.uv), sample_source(input.uv), parameters.prefilter > 0.5);
    if (parameters.prefilter > 0.5) {
        color = quadratic_threshold(min(color, vec3<f32>(parameters.clamp_max)));
    }
    return vec4<f32>(color, 1.0);
}
)wgsl";

constexpr char kUpsampleShader[] = R"wgsl(
struct Parameters { inverse_resolution: vec2<f32>, scatter: f32, padding: f32, };
@group(0) @binding(0) var source_sampler: sampler;
@group(0) @binding(1) var source_texture: texture_2d<f32>;
@group(0) @binding(2) var higher_texture: texture_2d<f32>;
@group(0) @binding(3) var<uniform> parameters: Parameters;
struct VertexOutput { @builtin(position) position: vec4<f32>, @location(0) uv: vec2<f32>, };
@vertex fn vertex_main(@builtin(vertex_index) index: u32) -> VertexOutput {
    let positions = array<vec2<f32>, 3>(vec2<f32>(-1.0, -1.0), vec2<f32>(3.0, -1.0), vec2<f32>(-1.0, 3.0));
    var output: VertexOutput;
    output.position = vec4<f32>(positions[index], 0.0, 1.0);
    output.uv = positions[index] * vec2<f32>(0.5, -0.5) + vec2<f32>(0.5, 0.5);
    return output;
}
fn source(uv: vec2<f32>) -> vec3<f32> { return textureSampleLevel(source_texture, source_sampler, uv, 0.0).rgb; }
@fragment fn fragment_main(input: VertexOutput) -> @location(0) vec4<f32> {
    let t = parameters.inverse_resolution;
    var low = source(input.uv + vec2<f32>(-t.x, -t.y));
    low += source(input.uv + vec2<f32>(0.0, -t.y)) * 2.0;
    low += source(input.uv + vec2<f32>(t.x, -t.y));
    low += source(input.uv + vec2<f32>(-t.x, 0.0)) * 2.0;
    low += source(input.uv) * 4.0;
    low += source(input.uv + vec2<f32>(t.x, 0.0)) * 2.0;
    low += source(input.uv + vec2<f32>(-t.x, t.y));
    low += source(input.uv + vec2<f32>(0.0, t.y)) * 2.0;
    low += source(input.uv + vec2<f32>(t.x, t.y));
    low /= 16.0;
    let high = textureSampleLevel(higher_texture, source_sampler, input.uv, 0.0).rgb;
    return vec4<f32>(mix(high, low, parameters.scatter), 1.0);
}
)wgsl";

constexpr char kResolveShader[] = R"wgsl(
struct Parameters {
    bloom_intensity: f32,
    exposure: f32,
    encode_srgb: f32,
    tonemapping_mode: f32,
    bloom_tint: vec3<f32>,
    padding: f32,
};
@group(0) @binding(0) var source_sampler: sampler;
@group(0) @binding(1) var scene_color: texture_2d<f32>;
@group(0) @binding(2) var bloom_color: texture_2d<f32>;
@group(0) @binding(3) var<uniform> parameters: Parameters;
struct VertexOutput { @builtin(position) position: vec4<f32>, @location(0) uv: vec2<f32>, };
@vertex fn vertex_main(@builtin(vertex_index) index: u32) -> VertexOutput {
    let positions = array<vec2<f32>, 3>(vec2<f32>(-1.0, -1.0), vec2<f32>(3.0, -1.0), vec2<f32>(-1.0, 3.0));
    var output: VertexOutput;
    output.position = vec4<f32>(positions[index], 0.0, 1.0);
    output.uv = positions[index] * vec2<f32>(0.5, -0.5) + vec2<f32>(0.5, 0.5);
    return output;
}
fn aces_film(color: vec3<f32>) -> vec3<f32> {
    let input_matrix = mat3x3<f32>(
        vec3<f32>(0.59719, 0.07600, 0.02840), vec3<f32>(0.35458, 0.90834, 0.13383),
        vec3<f32>(0.04823, 0.01566, 0.83777));
    let output_matrix = mat3x3<f32>(
        vec3<f32>(1.60475, -0.10208, -0.00327), vec3<f32>(-0.53108, 1.10813, -0.07276),
        vec3<f32>(-0.07367, -0.00605, 1.07602));
    var value = input_matrix * color;
    let numerator = value * (value + vec3<f32>(0.0245786)) - vec3<f32>(0.000090537);
    let denominator = value * (0.983729 * value + vec3<f32>(0.4329510)) + vec3<f32>(0.238081);
    value = numerator / max(denominator, vec3<f32>(0.000001));
    return clamp(output_matrix * value, vec3<f32>(0.0), vec3<f32>(1.0));
}
fn linear_to_srgb_channel(value: f32) -> f32 {
    return select(1.055 * pow(max(value, 0.0), 1.0 / 2.4) - 0.055, value * 12.92, value <= 0.0031308);
}
fn linear_to_srgb(color: vec3<f32>) -> vec3<f32> {
    return vec3<f32>(linear_to_srgb_channel(color.r), linear_to_srgb_channel(color.g), linear_to_srgb_channel(color.b));
}
@fragment fn fragment_main(input: VertexOutput) -> @location(0) vec4<f32> {
    let scene = textureSampleLevel(scene_color, source_sampler, input.uv, 0.0).rgb;
    let bloom = textureSampleLevel(bloom_color, source_sampler, input.uv, 0.0).rgb;
    var color = (scene + bloom * parameters.bloom_tint * parameters.bloom_intensity) * parameters.exposure;
    let mode = u32(parameters.tonemapping_mode + 0.5);
    if (mode == 1u) { color = color / (color + vec3<f32>(1.0)); }
    if (mode == 2u) { color = aces_film(color); }
    if (mode == 0u) { color = clamp(color, vec3<f32>(0.0), vec3<f32>(1.0)); }
    if (parameters.encode_srgb > 0.5) { color = linear_to_srgb(color); }
    return vec4<f32>(color, 1.0);
}
)wgsl";

bool IsSrgb(wgpu::TextureFormat format)
{
    return format == wgpu::TextureFormat::RGBA8UnormSrgb || format == wgpu::TextureFormat::BGRA8UnormSrgb;
}

wgpu::ShaderModule Shader(wgpu::Device device, const char *source)
{
    wgpu::ShaderSourceWGSL wgsl;
    wgsl.code = source;
    wgpu::ShaderModuleDescriptor descriptor;
    descriptor.nextInChain = &wgsl;
    return device.CreateShaderModule(&descriptor);
}

wgpu::RenderPipeline Pipeline(wgpu::Device device, wgpu::BindGroupLayout layout, wgpu::ShaderModule shader,
                              wgpu::TextureFormat format)
{
    wgpu::ColorTargetState target;
    target.format = format;
    wgpu::FragmentState fragment;
    fragment.module = shader;
    fragment.entryPoint = "fragment_main";
    fragment.targetCount = 1;
    fragment.targets = &target;
    wgpu::PipelineLayoutDescriptor pipelineLayout;
    pipelineLayout.bindGroupLayoutCount = 1;
    pipelineLayout.bindGroupLayouts = &layout;
    wgpu::RenderPipelineDescriptor descriptor;
    descriptor.layout = device.CreatePipelineLayout(&pipelineLayout);
    descriptor.vertex.module = shader;
    descriptor.vertex.entryPoint = "vertex_main";
    descriptor.fragment = &fragment;
    descriptor.primitive.topology = wgpu::PrimitiveTopology::TriangleList;
    descriptor.primitive.cullMode = wgpu::CullMode::None;
    return device.CreateRenderPipeline(&descriptor);
}

wgpu::Buffer UniformBuffer(wgpu::Device device, uint64_t bytes)
{
    wgpu::BufferDescriptor descriptor;
    descriptor.size = bytes;
    descriptor.usage = wgpu::BufferUsage::Uniform | wgpu::BufferUsage::CopyDst;
    return device.CreateBuffer(&descriptor);
}
} // namespace

bool WebPostProcessRenderer::Initialize(wgpu::Device device, wgpu::TextureFormat surfaceFormat)
{
    m_device = device;
    m_surfaceFormat = surfaceFormat;
    if (!m_device || surfaceFormat == wgpu::TextureFormat::Undefined)
        return false;
    wgpu::SamplerDescriptor sampler;
    sampler.addressModeU = wgpu::AddressMode::ClampToEdge;
    sampler.addressModeV = wgpu::AddressMode::ClampToEdge;
    sampler.minFilter = wgpu::FilterMode::Linear;
    sampler.magFilter = wgpu::FilterMode::Linear;
    m_sampler = m_device.CreateSampler(&sampler);
    m_resolveValues.encodeSrgb = IsSrgb(surfaceFormat) ? 0.0f : 1.0f;
    m_resolveParameters = UniformBuffer(m_device, sizeof(ResolveParameters));
    return m_sampler && m_resolveParameters && CreatePipelines();
}

bool WebPostProcessRenderer::CreatePipelines()
{
    std::array<wgpu::BindGroupLayoutEntry, 3> downEntries{};
    downEntries[0].binding = 0;
    downEntries[0].visibility = wgpu::ShaderStage::Fragment;
    downEntries[0].sampler.type = wgpu::SamplerBindingType::Filtering;
    downEntries[1].binding = 1;
    downEntries[1].visibility = wgpu::ShaderStage::Fragment;
    downEntries[1].texture.sampleType = wgpu::TextureSampleType::Float;
    downEntries[1].texture.viewDimension = wgpu::TextureViewDimension::e2D;
    downEntries[2].binding = 2;
    downEntries[2].visibility = wgpu::ShaderStage::Fragment;
    downEntries[2].buffer.type = wgpu::BufferBindingType::Uniform;
    downEntries[2].buffer.minBindingSize = sizeof(DownsampleParameters);
    wgpu::BindGroupLayoutDescriptor downLayout;
    downLayout.entryCount = downEntries.size();
    downLayout.entries = downEntries.data();
    m_downsampleLayout = m_device.CreateBindGroupLayout(&downLayout);

    std::array<wgpu::BindGroupLayoutEntry, 4> upEntries{};
    upEntries[0] = downEntries[0];
    upEntries[1] = downEntries[1];
    upEntries[2].binding = 2;
    upEntries[2].visibility = wgpu::ShaderStage::Fragment;
    upEntries[2].texture.sampleType = wgpu::TextureSampleType::Float;
    upEntries[2].texture.viewDimension = wgpu::TextureViewDimension::e2D;
    upEntries[3].binding = 3;
    upEntries[3].visibility = wgpu::ShaderStage::Fragment;
    upEntries[3].buffer.type = wgpu::BufferBindingType::Uniform;
    upEntries[3].buffer.minBindingSize = sizeof(UpsampleParameters);
    wgpu::BindGroupLayoutDescriptor upLayout;
    upLayout.entryCount = upEntries.size();
    upLayout.entries = upEntries.data();
    m_upsampleLayout = m_device.CreateBindGroupLayout(&upLayout);

    std::array<wgpu::BindGroupLayoutEntry, 4> resolveEntries = upEntries;
    resolveEntries[3].buffer.minBindingSize = sizeof(ResolveParameters);
    wgpu::BindGroupLayoutDescriptor resolveLayout;
    resolveLayout.entryCount = resolveEntries.size();
    resolveLayout.entries = resolveEntries.data();
    m_resolveLayout = m_device.CreateBindGroupLayout(&resolveLayout);

    m_downsamplePipeline = Pipeline(m_device, m_downsampleLayout, Shader(m_device, kDownsampleShader), kHdrFormat);
    m_upsamplePipeline = Pipeline(m_device, m_upsampleLayout, Shader(m_device, kUpsampleShader), kHdrFormat);
    m_resolvePipeline = Pipeline(m_device, m_resolveLayout, Shader(m_device, kResolveShader), m_surfaceFormat);
    return m_downsampleLayout && m_upsampleLayout && m_resolveLayout && m_downsamplePipeline && m_upsamplePipeline &&
           m_resolvePipeline;
}

bool WebPostProcessRenderer::Resize(uint32_t width, uint32_t height)
{
    width = std::max(1u, width);
    height = std::max(1u, height);
    if (m_width == width && m_height == height && m_sceneColorView && m_resolveGroup)
        return true;
    m_width = width;
    m_height = height;
    return CreateSceneTarget() && CreateBloomTargets() && CreateResolveBindGroup();
}

bool WebPostProcessRenderer::Configure(const Settings &settings)
{
    m_settings = settings;
    m_settings.bloomThreshold = std::max(0.0f, settings.bloomThreshold);
    m_settings.bloomIntensity = std::max(0.0f, settings.bloomIntensity);
    m_settings.bloomScatter = std::clamp(settings.bloomScatter, 0.0f, 1.0f);
    m_settings.bloomClamp = std::max(0.0f, settings.bloomClamp);
    m_settings.bloomIterations = std::clamp(settings.bloomIterations, 1u, 8u);
    m_settings.toneMappingMode = std::min(settings.toneMappingMode, 2u);
    m_settings.exposure = std::max(0.0001f, settings.exposure);
    if (m_width == 0 || m_height == 0)
        return true;
    return CreateBloomTargets() && CreateResolveBindGroup();
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

bool WebPostProcessRenderer::CreateBloomTargets()
{
    m_bloomLevels.clear();
    if (!m_settings.bloomEnabled)
        return true;
    const uint32_t count = std::clamp(m_settings.bloomIterations, 1u, 8u);
    m_bloomLevels.resize(count);
    for (uint32_t index = 0; index < count; ++index) {
        auto &level = m_bloomLevels[index];
        level.width = std::max(1u, m_width >> (index + 1u));
        level.height = std::max(1u, m_height >> (index + 1u));
        wgpu::TextureDescriptor texture;
        texture.size = {level.width, level.height, 1};
        texture.dimension = wgpu::TextureDimension::e2D;
        texture.format = kHdrFormat;
        texture.mipLevelCount = 1;
        texture.sampleCount = 1;
        texture.usage = wgpu::TextureUsage::RenderAttachment | wgpu::TextureUsage::TextureBinding;
        level.downTexture = m_device.CreateTexture(&texture);
        level.downView = level.downTexture ? level.downTexture.CreateView() : wgpu::TextureView{};
        level.upTexture = m_device.CreateTexture(&texture);
        level.upView = level.upTexture ? level.upTexture.CreateView() : wgpu::TextureView{};
        level.downParameters = UniformBuffer(m_device, sizeof(DownsampleParameters));
        if (!level.downView || !level.upView || !level.downParameters)
            return false;
        const uint32_t sourceWidth = index == 0 ? m_width : m_bloomLevels[index - 1].width;
        const uint32_t sourceHeight = index == 0 ? m_height : m_bloomLevels[index - 1].height;
        DownsampleParameters values;
        values.inverseWidth = 1.0f / static_cast<float>(sourceWidth);
        values.inverseHeight = 1.0f / static_cast<float>(sourceHeight);
        values.threshold = m_settings.bloomThreshold;
        values.knee = 0.5f;
        values.clampMax = m_settings.bloomClamp;
        values.prefilter = index == 0 ? 1.0f : 0.0f;
        m_device.GetQueue().WriteBuffer(level.downParameters, 0, &values, sizeof(values));
        std::array<wgpu::BindGroupEntry, 3> entries{};
        entries[0].binding = 0;
        entries[0].sampler = m_sampler;
        entries[1].binding = 1;
        entries[1].textureView = index == 0 ? m_sceneColorView : m_bloomLevels[index - 1].downView;
        entries[2].binding = 2;
        entries[2].buffer = level.downParameters;
        entries[2].size = sizeof(DownsampleParameters);
        wgpu::BindGroupDescriptor group;
        group.layout = m_downsampleLayout;
        group.entryCount = entries.size();
        group.entries = entries.data();
        level.downGroup = m_device.CreateBindGroup(&group);
        if (!level.downGroup)
            return false;
    }
    for (uint32_t higherIndex = 0; higherIndex + 1 < count; ++higherIndex) {
        const uint32_t lowerIndex = higherIndex + 1;
        auto &higher = m_bloomLevels[higherIndex];
        const auto &lower = m_bloomLevels[lowerIndex];
        higher.upParameters = UniformBuffer(m_device, sizeof(UpsampleParameters));
        UpsampleParameters values;
        values.inverseWidth = 1.0f / static_cast<float>(lower.width);
        values.inverseHeight = 1.0f / static_cast<float>(lower.height);
        values.scatter = m_settings.bloomScatter;
        m_device.GetQueue().WriteBuffer(higher.upParameters, 0, &values, sizeof(values));
        std::array<wgpu::BindGroupEntry, 4> entries{};
        entries[0].binding = 0;
        entries[0].sampler = m_sampler;
        entries[1].binding = 1;
        entries[1].textureView = lowerIndex + 1 == count ? lower.downView : lower.upView;
        entries[2].binding = 2;
        entries[2].textureView = higher.downView;
        entries[3].binding = 3;
        entries[3].buffer = higher.upParameters;
        entries[3].size = sizeof(UpsampleParameters);
        wgpu::BindGroupDescriptor group;
        group.layout = m_upsampleLayout;
        group.entryCount = entries.size();
        group.entries = entries.data();
        higher.upGroup = m_device.CreateBindGroup(&group);
        if (!higher.upParameters || !higher.upGroup)
            return false;
    }
    return true;
}

bool WebPostProcessRenderer::CreateResolveBindGroup()
{
    const wgpu::TextureView bloom =
        m_bloomLevels.empty() ? m_sceneColorView
                              : (m_bloomLevels.size() == 1 ? m_bloomLevels[0].downView : m_bloomLevels[0].upView);
    m_resolveValues.bloomIntensity = m_settings.bloomEnabled ? m_settings.bloomIntensity : 0.0f;
    m_resolveValues.exposure = m_settings.exposure;
    m_resolveValues.toneMappingMode = static_cast<float>(m_settings.toneMappingMode);
    m_resolveValues.bloomTint = m_settings.bloomTint;
    m_device.GetQueue().WriteBuffer(m_resolveParameters, 0, &m_resolveValues, sizeof(m_resolveValues));
    std::array<wgpu::BindGroupEntry, 4> entries{};
    entries[0].binding = 0;
    entries[0].sampler = m_sampler;
    entries[1].binding = 1;
    entries[1].textureView = m_sceneColorView;
    entries[2].binding = 2;
    entries[2].textureView = bloom;
    entries[3].binding = 3;
    entries[3].buffer = m_resolveParameters;
    entries[3].size = sizeof(ResolveParameters);
    wgpu::BindGroupDescriptor group;
    group.layout = m_resolveLayout;
    group.entryCount = entries.size();
    group.entries = entries.data();
    m_resolveGroup = m_device.CreateBindGroup(&group);
    return static_cast<bool>(m_resolveGroup);
}

bool WebPostProcessRenderer::RecordColorPass(wgpu::CommandEncoder encoder, wgpu::TextureView target,
                                             wgpu::RenderPipeline pipeline, wgpu::BindGroup group)
{
    if (!encoder || !target || !pipeline || !group)
        return false;
    wgpu::RenderPassColorAttachment attachment;
    attachment.view = target;
    attachment.loadOp = wgpu::LoadOp::Clear;
    attachment.storeOp = wgpu::StoreOp::Store;
    attachment.clearValue = {0.0, 0.0, 0.0, 1.0};
    wgpu::RenderPassDescriptor descriptor;
    descriptor.colorAttachmentCount = 1;
    descriptor.colorAttachments = &attachment;
    wgpu::RenderPassEncoder pass = encoder.BeginRenderPass(&descriptor);
    pass.SetPipeline(pipeline);
    pass.SetBindGroup(0, group);
    pass.Draw(3, 1, 0, 0);
    pass.End();
    return true;
}

wgpu::TextureFormat WebPostProcessRenderer::SceneColorFormat() const noexcept
{
    return kHdrFormat;
}

wgpu::TextureView WebPostProcessRenderer::SceneColorView() const noexcept
{
    return m_sceneColorView;
}

bool WebPostProcessRenderer::PrepareBloom(wgpu::CommandEncoder encoder)
{
    if (!m_settings.bloomEnabled)
        return true;
    for (auto &level : m_bloomLevels) {
        if (!RecordColorPass(encoder, level.downView, m_downsamplePipeline, level.downGroup))
            return false;
    }
    if (m_bloomLevels.size() > 1) {
        for (size_t index = m_bloomLevels.size() - 1; index > 0; --index) {
            auto &higher = m_bloomLevels[index - 1];
            if (!RecordColorPass(encoder, higher.upView, m_upsamplePipeline, higher.upGroup))
                return false;
        }
    }
    return true;
}

bool WebPostProcessRenderer::Render(wgpu::RenderPassEncoder pass)
{
    if (!pass || !m_resolvePipeline || !m_resolveGroup || !m_sceneColorView)
        return false;
    pass.SetPipeline(m_resolvePipeline);
    pass.SetBindGroup(0, m_resolveGroup);
    pass.Draw(3, 1, 0, 0);
    if (!m_reportedReady) {
        std::printf("INFERNUX_WEB_POST_PROCESS_READY hdr=rgba16float bloom=%d iterations=%u tonemap=%u\n",
                    m_settings.bloomEnabled ? 1 : 0, m_settings.bloomIterations, m_settings.toneMappingMode);
        m_reportedReady = true;
    }
    return true;
}

} // namespace infernux::web
