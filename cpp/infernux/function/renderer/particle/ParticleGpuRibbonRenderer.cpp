#include "ParticleGpuRibbonRenderer.h"

#include <core/types/ColorSpace.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <algorithm>
#include <cmath>
#include <cstring>

namespace infernux::particle
{

namespace
{

constexpr std::string_view VertexSource = R"glsl(#version 450
struct ParticleRenderInstance {
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
    vec4 scale_custom;
    uvec4 ribbon_data;
};
layout(std430, set = 0, binding = 0) readonly buffer Instances { ParticleRenderInstance instances[]; };
layout(std430, set = 0, binding = 1) readonly buffer SortedIndices { uint sorted_indices[]; };
layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
} view;
layout(location = 0) out vec4 out_color;
layout(location = 1) out vec2 out_uv;
layout(location = 15) flat out uvec2 out_object_id;

const uint endpoint_for_vertex[6] = uint[](0u, 0u, 1u, 0u, 1u, 1u);
const float side_for_vertex[6] = float[](-1.0, 1.0, 1.0, -1.0, 1.0, -1.0);

void main() {
    uint segment = uint(gl_VertexIndex) / 6u;
    uint corner = uint(gl_VertexIndex) % 6u;
    ParticleRenderInstance first = instances[sorted_indices[segment]];
    ParticleRenderInstance second = instances[sorted_indices[segment + 1u]];
    bool connected = first.ribbon_data.x == second.ribbon_data.x && (second.ribbon_data.z & 1u) == 0u;

    vec3 first_position = first.position_size.xyz;
    vec3 second_position = connected ? second.position_size.xyz : first_position;
    vec3 tangent = second_position - first_position;
    float tangent_length = length(tangent);
    tangent = tangent_length > 1e-7 ? tangent / tangent_length : normalize(view.camera_up.xyz);
    vec3 camera_forward = cross(view.camera_right.xyz, view.camera_up.xyz);
    vec3 side = cross(camera_forward, tangent);
    float side_length = length(side);
    side = side_length > 1e-7 ? side / side_length : normalize(view.camera_right.xyz);

    uint endpoint = endpoint_for_vertex[corner];
    ParticleRenderInstance point = endpoint == 0u ? first : second;
    vec3 center = endpoint == 0u ? first_position : second_position;
    float half_width = max(abs(point.position_size.w), 0.0) * 0.5;
    vec3 world_position = center + side * side_for_vertex[corner] * half_width;
    gl_Position = view.view_projection * vec4(world_position, 1.0);

    out_color = point.color * view.material_tint;
    if (!connected) out_color.a = 0.0;
    float order_coordinate = float(point.ribbon_data.y);
    bool repeat_uv = view.rendering_control.z > 0.5;
    out_uv = vec2(repeat_uv ? order_coordinate * view.rendering_control.y
                            : order_coordinate / view.rendering_control.y,
                  side_for_vertex[corner] > 0.0 ? 1.0 : 0.0);
    out_object_id = floatBitsToUint(view.material_tint.xy);
}
)glsl";

constexpr std::string_view FragmentSource = R"glsl(#version 450
layout(location = 0) in vec4 in_color;
layout(location = 1) in vec2 in_uv;
layout(location = 0) out vec4 out_color;
void main() {
    if (in_color.a <= 0.0) discard;
    out_color = in_color;
}
)glsl";

constexpr std::string_view PickingFragmentSource = R"glsl(#version 450
layout(location = 0) in vec4 in_color;
layout(location = 15) flat in uvec2 in_object_id;
layout(location = 0) out uvec2 out_object_id;
void main() {
    if (in_color.a <= 0.0) discard;
    out_object_id = in_object_id;
}
)glsl";

bool IsShaderBytecodeValid(const ShaderBytecode &bytecode) noexcept
{
    return bytecode.words && bytecode.wordCount >= 5 && bytecode.words[0] == 0x07230203u;
}

uint8_t PipelineStateSignature(const GpuBillboardMaterialState &state) noexcept
{
    return static_cast<uint8_t>((state.blendEnabled ? 1u : 0u) | (state.depthTestEnabled ? 2u : 0u) |
                                (state.depthWriteEnabled ? 4u : 0u) | (state.premultipliedAlpha ? 8u : 0u));
}

} // namespace

std::string_view GpuParticleRibbonRenderShaderSources::Vertex() noexcept
{
    return VertexSource;
}

std::string_view GpuParticleRibbonRenderShaderSources::Fragment() noexcept
{
    return FragmentSource;
}

std::string_view GpuParticleRibbonRenderShaderSources::PickingFragment() noexcept
{
    return PickingFragmentSource;
}

bool GpuParticleRibbonRenderProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(vertex) && IsShaderBytecodeValid(fragment) && IsShaderBytecodeValid(pickingFragment);
}

bool GpuParticleRibbonRenderProgramStorage::Assign(const GpuParticleRibbonRenderProgram &program)
{
    if (!program.IsValid())
        return false;
    const std::array<ShaderBytecode, 3> sources = {program.vertex, program.fragment, program.pickingFragment};
    std::array<std::vector<uint32_t>, 3> candidate;
    for (size_t index = 0; index < sources.size(); ++index)
        candidate[index].assign(sources[index].words, sources[index].words + sources[index].wordCount);
    shaders = std::move(candidate);
    return true;
}

bool GpuParticleRibbonRenderProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleRibbonRenderProgram GpuParticleRibbonRenderProgramStorage::View() const noexcept
{
    return {
        {shaders[0].data(), shaders[0].size()},
        {shaders[1].data(), shaders[1].size()},
        {shaders[2].data(), shaders[2].size()},
    };
}

ParticleGpuRibbonRenderer::~ParticleGpuRibbonRenderer()
{
    Destroy();
}

bool ParticleGpuRibbonRenderer::Create(rhi::Device &device, const GpuRibbonRendererDesc &desc)
{
    Destroy();
    if (!desc.program.IsValid() || !desc.topology || !desc.topology->IsValid() || !std::isfinite(desc.uvScale) ||
        desc.uvScale <= 0.0f || !desc.semantics.IsValid() || desc.semantics.castShadows ||
        desc.semantics.softParticles || desc.semantics.receiveSceneLighting || desc.semantics.receiveShadows) {
        return false;
    }
    m_device = &device;
    m_topology = desc.topology;
    m_material = desc.material;
    m_fallbackMaterial = desc.fallbackMaterial;
    m_semantics = desc.semantics;
    m_semantics.sortMode = ParticleSortMode::None;
    m_uvMode = desc.uvMode;
    m_uvScale = desc.uvScale;
    m_vertexShader = device.CreateShaderModule({desc.program.vertex.words, desc.program.vertex.wordCount});
    m_fragmentShader = device.CreateShaderModule({desc.program.fragment.words, desc.program.fragment.wordCount});
    m_pickingFragmentShader =
        device.CreateShaderModule({desc.program.pickingFragment.words, desc.program.pickingFragment.wordCount});
    if (!m_vertexShader.IsValid() || !m_fragmentShader.IsValid() || !m_pickingFragmentShader.IsValid()) {
        Destroy();
        return false;
    }
    rhi::BindingLayoutDesc layout;
    layout.entries[layout.entryCount++] = {0, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    layout.entries[layout.entryCount++] = {1, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    m_layout = device.CreateBindingLayout(layout);
    m_group = CreateBindGroup(m_topology->SortedIndexBuffer());
    if (!m_layout.IsValid() || !m_group.IsValid()) {
        Destroy();
        return false;
    }
    return true;
}

void ParticleGpuRibbonRenderer::Destroy() noexcept
{
    if (m_device) {
        for (const auto &entry : m_pipelines)
            m_device->Release(entry.pipeline);
        for (const auto &entry : m_viewGroups)
            m_device->Release(entry.group);
        m_device->Release(m_group);
        m_device->Release(m_layout);
        m_device->Release(m_pickingFragmentShader);
        m_device->Release(m_fragmentShader);
        m_device->Release(m_vertexShader);
    }
    m_device = nullptr;
    m_topology.reset();
    m_material.reset();
    m_fallbackMaterial = {};
    m_semantics = {};
    m_uvMode = ParticleRibbonUvMode::Stretch;
    m_uvScale = 1.0f;
    m_vertexShader = {};
    m_fragmentShader = {};
    m_pickingFragmentShader = {};
    m_layout = {};
    m_group = {};
    m_viewGroups.clear();
    m_pipelines.clear();
}

bool ParticleGpuRibbonRenderer::IsValid() const noexcept
{
    return m_device && m_topology && m_topology->IsValid() && m_vertexShader.IsValid() && m_fragmentShader.IsValid() &&
           m_pickingFragmentShader.IsValid() && m_layout.IsValid() && m_group.IsValid();
}

int32_t ParticleGpuRibbonRenderer::RenderQueue() const noexcept
{
    return ResolveMaterialState().renderQueue;
}

rhi::BufferHandle ParticleGpuRibbonRenderer::InstanceBuffer() const noexcept
{
    return m_topology ? m_topology->InstanceBuffer() : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRibbonRenderer::RenderIndexBuffer() const noexcept
{
    return m_topology ? m_topology->SortedIndexBuffer() : rhi::BufferHandle{};
}

GpuBillboardMaterialState ParticleGpuRibbonRenderer::ResolveMaterialState() const noexcept
{
    if (!m_material || m_material->IsDeleted())
        return m_fallbackMaterial;
    const auto &state = m_material->GetRenderState();
    return {state.renderQueue, state.blendEnable, state.depthTestEnable, state.depthWriteEnable,
            state.srcColorBlendFactor == VK_BLEND_FACTOR_ONE &&
                state.dstColorBlendFactor == VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA};
}

std::array<float, 4> ParticleGpuRibbonRenderer::ResolveMaterialTint() const noexcept
{
    if (!m_material || m_material->IsDeleted())
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const auto *property = m_material->GetProperty("baseColor");
    if (!property || (property->type != MaterialPropertyType::Color && property->type != MaterialPropertyType::Float4))
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const auto *value = std::get_if<glm::vec4>(&property->value);
    if (!value)
        return {1.0f, 1.0f, 1.0f, 1.0f};
    // Authored Color properties are sRGB; shading runs in linear space.
    const glm::vec4 tint =
        property->type == MaterialPropertyType::Color ? inx::color::SrgbToLinear(*value) : *value;
    return {tint.x, tint.y, tint.z, tint.w};
}

rhi::BindGroupHandle ParticleGpuRibbonRenderer::CreateBindGroup(rhi::BufferHandle renderIndices) const
{
    if (!m_device || !m_layout.IsValid() || !InstanceBuffer().IsValid() || !renderIndices.IsValid())
        return {};
    rhi::BindGroupDesc group;
    group.layout = m_layout;
    group.buffers[group.bufferCount++] = {0, rhi::BindingType::StorageBuffer, InstanceBuffer(), 0, 0};
    group.buffers[group.bufferCount++] = {1, rhi::BindingType::StorageBuffer, renderIndices, 0, 0};
    return m_device->CreateBindGroup(group);
}

rhi::BindGroupHandle ParticleGpuRibbonRenderer::ResolveBindGroup(rhi::BufferHandle renderIndices)
{
    if (!renderIndices.IsValid() || renderIndices == RenderIndexBuffer())
        return m_group;
    const auto found = std::find_if(m_viewGroups.begin(), m_viewGroups.end(),
                                    [&](const auto &entry) { return entry.renderIndices == renderIndices; });
    if (found != m_viewGroups.end())
        return found->group;
    const auto group = CreateBindGroup(renderIndices);
    if (group.IsValid())
        m_viewGroups.push_back({renderIndices, group});
    return group;
}

bool ParticleGpuRibbonRenderer::RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                           rhi::RenderTargetLayoutHandle renderTargetLayout,
                                           const MaterialPassPipelineDescriptor &pass,
                                           rhi::BufferHandle indirectArguments, const GpuParticleViewConstants &view,
                                           rhi::BufferHandle renderIndices, rhi::TextureViewHandle sceneDepth,
                                           bool sceneDepthIsDepth, const GpuParticleForwardPlusBindings &forwardPlus)
{
    (void)sceneDepth;
    (void)sceneDepthIsDepth;
    (void)forwardPlus;
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass);
    const auto group = ResolveBindGroup(renderIndices);
    if (!pipeline.IsValid() || !group.IsValid())
        return false;
    auto constants = view;
    constants.materialTint = ResolveMaterialTint();
    constants.renderingControl[1] = m_uvScale;
    constants.renderingControl[2] = m_uvMode == ParticleRibbonUvMode::Repeat ? 1.0f : 0.0f;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

bool ParticleGpuRibbonRenderer::RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                                  rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                  const MaterialPassPipelineDescriptor &pass,
                                                  rhi::BufferHandle indirectArguments,
                                                  const GpuParticleViewConstants &view, uint64_t ownerObjectId,
                                                  rhi::BufferHandle renderIndices)
{
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid() || ownerObjectId == 0)
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass);
    const auto group = ResolveBindGroup(renderIndices);
    if (!pipeline.IsValid() || !group.IsValid())
        return false;
    auto constants = view;
    const std::array<uint32_t, 4> objectId = {
        static_cast<uint32_t>(ownerObjectId),
        static_cast<uint32_t>(ownerObjectId >> 32u),
        0u,
        0x3f800000u,
    };
    std::memcpy(constants.materialTint.data(), objectId.data(), sizeof(objectId));
    constants.renderingControl[1] = m_uvScale;
    constants.renderingControl[2] = m_uvMode == ParticleRibbonUvMode::Repeat ? 1.0f : 0.0f;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

rhi::GraphicsPipelineHandle
ParticleGpuRibbonRenderer::GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                               const MaterialPassPipelineDescriptor &pass)
{
    if (!renderTargetLayout.IsValid() || !pass.IsValid() ||
        (pass.target != ShaderCompileTarget::Forward && pass.target != ShaderCompileTarget::ForwardPlus &&
         pass.target != ShaderCompileTarget::Picking))
        return {};
    const bool picking = pass.target == ShaderCompileTarget::Picking;
    const auto state = picking ? GpuBillboardMaterialState{3000, false, true, true} : ResolveMaterialState();
    const uint8_t signature = PipelineStateSignature(state);
    const auto found = std::find_if(m_pipelines.begin(), m_pipelines.end(), [&](const auto &entry) {
        return entry.renderTargetLayout == renderTargetLayout && entry.pass == pass &&
               entry.materialStateSignature == signature;
    });
    if (found != m_pipelines.end())
        return found->pipeline;

    rhi::GraphicsPipelineDesc desc;
    desc.vertexShader = m_vertexShader;
    desc.fragmentShader = picking ? m_pickingFragmentShader : m_fragmentShader;
    desc.renderTargetLayout = renderTargetLayout;
    desc.raster.cullMode = rhi::CullMode::None;
    desc.depth.testEnabled = state.depthTestEnabled && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.depth.writeEnabled = state.depthWriteEnabled && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.samples = pass.samples;
    for (size_t index = 0; index < pass.colorFormats.size(); ++index) {
        desc.colorTargets[index].format = pass.colorFormats[index];
        desc.colorTargets[index].blendEnabled = state.blendEnabled;
        desc.colorTargets[index].premultipliedAlpha = state.premultipliedAlpha;
    }
    desc.colorTargetCount = static_cast<uint32_t>(pass.colorFormats.size());
    desc.bindingLayouts[0] = m_layout;
    desc.bindingLayoutCount = 1;
    desc.pushConstantStages = rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment;
    desc.pushConstantBytes = sizeof(GpuParticleViewConstants);
    const auto pipeline = m_device->CreateGraphicsPipeline(desc);
    if (pipeline.IsValid())
        m_pipelines.push_back({renderTargetLayout, pass, signature, pipeline});
    return pipeline;
}

} // namespace infernux::particle
