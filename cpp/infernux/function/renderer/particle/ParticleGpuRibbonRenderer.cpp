#include "ParticleGpuRibbonRenderer.h"

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
    vec4 custom_data;
    vec4 previous_position_history;
};
layout(std430, set = 0, binding = 0) readonly buffer Instances { ParticleRenderInstance instances[]; };
layout(std430, set = 0, binding = 1) readonly buffer SortedIndices { uint sorted_indices[]; };
layout(std430, set = 0, binding = 2) readonly buffer VisibleSegments { uint visible_segments[]; };
layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;
layout(location = 0) out vec3 out_world_position;
layout(location = 1) out vec3 out_normal;
layout(location = 2) out vec4 out_tangent;
layout(location = 3) out vec3 out_color;
layout(location = 4) out vec2 out_uv;
layout(location = 5) out float out_view_depth;
layout(location = 9) out vec2 out_particle_local_uv;
layout(location = 10) out vec2 out_particle_next_uv;
layout(location = 11) out float out_particle_blend;
layout(location = 12) out float out_particle_age;
layout(location = 13) flat out uint out_particle_id;
layout(location = 14) out float out_particle_alpha;
layout(location = 15) flat out uint out_layer_mask;

const uint endpoint_for_vertex[6] = uint[](0u, 0u, 1u, 0u, 1u, 1u);
const float side_for_vertex[6] = float[](-1.0, 1.0, 1.0, -1.0, 1.0, -1.0);

void main() {
    bool compacted_segments = view.rendering_control.w > 0.5;
    uint segment = compacted_segments ? visible_segments[uint(gl_InstanceIndex)] : uint(gl_VertexIndex) / 6u;
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

    vec4 particle_color = point.color;
    float order_coordinate = float(point.ribbon_data.y);
    bool repeat_uv = view.rendering_control.z > 0.5;
    vec2 ribbon_uv = vec2(repeat_uv ? order_coordinate * view.rendering_control.y
                                    : order_coordinate / view.rendering_control.y,
                           side_for_vertex[corner] > 0.0 ? 1.0 : 0.0);
    vec3 surface_normal = normalize(cross(side, tangent));
    out_world_position = world_position;
    out_normal = surface_normal;
    out_tangent = vec4(tangent, 1.0);
    out_color = particle_color.rgb;
    out_uv = ribbon_uv;
    out_view_depth = gl_Position.w;
    out_particle_local_uv = ribbon_uv;
    out_particle_next_uv = ribbon_uv;
    out_particle_blend = 0.0;
    out_particle_age = clamp(point.scale_custom.w, 0.0, 1.0);
    out_particle_id = point.ribbon_data.w;
    out_particle_alpha = connected ? particle_color.a : 0.0;
    out_layer_mask = floatBitsToUint(view.lighting_control.w);
}
)glsl";

constexpr std::string_view PickingFragmentSource = R"glsl(#version 450
layout(location = 14) in float in_particle_alpha;
layout(location = 0) out uvec2 out_object_id;

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;

void main() {
    if (!(in_particle_alpha > 0.0)) discard;
    out_object_id = uvec2(floatBitsToUint(view.material_tint.x), floatBitsToUint(view.material_tint.y));
}
)glsl";

constexpr std::string_view MotionVertexSource = R"glsl(#version 450
struct ParticleRenderInstance {
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
    vec4 scale_custom;
    uvec4 ribbon_data;
    vec4 custom_data;
    vec4 previous_position_history;
};
layout(std430, set = 0, binding = 0) readonly buffer Instances { ParticleRenderInstance instances[]; };
layout(std430, set = 0, binding = 1) readonly buffer SortedIndices { uint sorted_indices[]; };
layout(std430, set = 0, binding = 2) readonly buffer VisibleSegments { uint visible_segments[]; };
layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;
layout(location = 0) out vec4 out_color;
layout(location = 1) out vec2 out_motion;

const uint endpoint_for_vertex[6] = uint[](0u, 0u, 1u, 0u, 1u, 1u);
const float side_for_vertex[6] = float[](-1.0, 1.0, 1.0, -1.0, 1.0, -1.0);

vec3 ribbon_side(vec3 first_position, vec3 second_position) {
    vec3 tangent = second_position - first_position;
    float tangent_length = length(tangent);
    tangent = tangent_length > 1e-7 ? tangent / tangent_length : normalize(view.camera_up.xyz);
    vec3 camera_forward = cross(view.camera_right.xyz, view.camera_up.xyz);
    vec3 side = cross(camera_forward, tangent);
    float side_length = length(side);
    return side_length > 1e-7 ? side / side_length : normalize(view.camera_right.xyz);
}

void main() {
    bool compacted_segments = view.rendering_control.w > 0.5;
    uint segment = compacted_segments ? visible_segments[uint(gl_InstanceIndex)] : uint(gl_VertexIndex) / 6u;
    uint corner = uint(gl_VertexIndex) % 6u;
    ParticleRenderInstance first = instances[sorted_indices[segment]];
    ParticleRenderInstance second = instances[sorted_indices[segment + 1u]];
    bool connected = first.ribbon_data.x == second.ribbon_data.x && (second.ribbon_data.z & 1u) == 0u;

    vec3 first_position = first.position_size.xyz;
    vec3 second_position = connected ? second.position_size.xyz : first_position;
    vec3 previous_first_position = first.previous_position_history.xyz;
    vec3 previous_second_position = connected ? second.previous_position_history.xyz : previous_first_position;
    vec3 side = ribbon_side(first_position, second_position);
    vec3 previous_side = ribbon_side(previous_first_position, previous_second_position);

    uint endpoint = endpoint_for_vertex[corner];
    ParticleRenderInstance point = endpoint == 0u ? first : second;
    vec3 center = endpoint == 0u ? first_position : second_position;
    vec3 previous_center = endpoint == 0u ? previous_first_position : previous_second_position;
    float half_width = max(abs(point.position_size.w), 0.0) * 0.5;
    float vertex_side = side_for_vertex[corner];
    vec4 current_clip = view.view_projection * vec4(center + side * vertex_side * half_width, 1.0);
    vec4 previous_clip =
        view.previous_view_projection * vec4(previous_center + previous_side * vertex_side * half_width, 1.0);
    gl_Position = current_clip;
    vec2 current_ndc = current_clip.xy / max(abs(current_clip.w), 1e-6);
    vec2 previous_ndc = previous_clip.xy / max(abs(previous_clip.w), 1e-6);
    out_motion = (current_ndc - previous_ndc) * vec2(0.5, -0.5);
    out_color = point.color * view.material_tint;
    if (!connected) out_color.a = 0.0;
}
)glsl";

constexpr std::string_view MotionFragmentSource = R"glsl(#version 450
layout(location = 0) in vec4 in_color;
layout(location = 1) in vec2 in_motion;
layout(location = 0) out vec2 out_motion;
void main() {
    if (in_color.a <= 0.0) discard;
    out_motion = in_motion;
}
)glsl";

bool IsShaderBytecodeValid(const ShaderBytecode &bytecode) noexcept
{
    return bytecode.words && bytecode.wordCount >= 5 && bytecode.words[0] == 0x07230203u;
}

std::vector<uint32_t> CopySpirvWords(const std::vector<char> &bytes)
{
    if (bytes.empty() || bytes.size() % sizeof(uint32_t) != 0)
        return {};
    std::vector<uint32_t> words(bytes.size() / sizeof(uint32_t));
    std::memcpy(words.data(), bytes.data(), bytes.size());
    return words;
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

std::string_view GpuParticleRibbonRenderShaderSources::PickingFragment() noexcept
{
    return PickingFragmentSource;
}

std::string_view GpuParticleRibbonRenderShaderSources::MotionVertex() noexcept
{
    return MotionVertexSource;
}

std::string_view GpuParticleRibbonRenderShaderSources::MotionFragment() noexcept
{
    return MotionFragmentSource;
}

bool GpuParticleRibbonRenderProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(vertex) && IsShaderBytecodeValid(pickingFragment) &&
           IsShaderBytecodeValid(motionVertex) && IsShaderBytecodeValid(motionFragment);
}

bool GpuParticleRibbonRenderProgramStorage::Assign(const GpuParticleRibbonRenderProgram &program)
{
    if (!program.IsValid())
        return false;
    const std::array<ShaderBytecode, 4> sources = {program.vertex, program.pickingFragment, program.motionVertex,
                                                   program.motionFragment};
    std::array<std::vector<uint32_t>, 4> candidate;
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
        {shaders[3].data(), shaders[3].size()},
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
        desc.uvScale <= 0.0f || !desc.semantics.IsValid() || desc.semantics.castShadows || !desc.shaderProgram ||
        !desc.shaderProgram->IsValid() || desc.shaderProgram->domain != ShaderProgramDomain::ParticleSprite ||
        !desc.textureResolver) {
        return false;
    }
    const auto *linkedVariant = desc.shaderProgram->FindVariant(ShaderCompileTarget::Forward);
    const auto *linkedForwardPlusVariant = desc.shaderProgram->FindVariant(ShaderCompileTarget::ForwardPlus);
    if (!linkedVariant || (desc.semantics.receiveSceneLighting && !linkedForwardPlusVariant))
        return false;
    const auto linkedFragmentWords = CopySpirvWords(linkedVariant->fragmentSpirv);
    const auto linkedForwardPlusFragmentWords =
        linkedForwardPlusVariant ? CopySpirvWords(linkedForwardPlusVariant->fragmentSpirv) : std::vector<uint32_t>{};
    if (linkedFragmentWords.empty() || (desc.semantics.receiveSceneLighting && linkedForwardPlusFragmentWords.empty()))
        return false;

    m_device = &device;
    m_topology = desc.topology;
    m_shaderProgram = desc.shaderProgram;
    m_semantics = desc.semantics;
    m_semantics.sortMode = ParticleSortMode::None;
    m_uvMode = desc.uvMode;
    m_uvScale = desc.uvScale;
    m_vertexShader = device.CreateShaderModule({desc.program.vertex.words, desc.program.vertex.wordCount});
    m_fragmentShader = device.CreateShaderModule({linkedFragmentWords.data(), linkedFragmentWords.size()});
    if (m_semantics.receiveSceneLighting) {
        m_forwardPlusFragmentShader =
            device.CreateShaderModule({linkedForwardPlusFragmentWords.data(), linkedForwardPlusFragmentWords.size()});
    }
    m_pickingFragmentShader =
        device.CreateShaderModule({desc.program.pickingFragment.words, desc.program.pickingFragment.wordCount});
    m_motionVertexShader =
        device.CreateShaderModule({desc.program.motionVertex.words, desc.program.motionVertex.wordCount});
    m_motionFragmentShader =
        device.CreateShaderModule({desc.program.motionFragment.words, desc.program.motionFragment.wordCount});
    if (!m_vertexShader.IsValid() || !m_fragmentShader.IsValid() || !m_pickingFragmentShader.IsValid() ||
        !m_motionVertexShader.IsValid() || !m_motionFragmentShader.IsValid() ||
        (m_semantics.receiveSceneLighting && !m_forwardPlusFragmentShader.IsValid())) {
        Destroy();
        return false;
    }
    rhi::BindingLayoutDesc layout;
    layout.entries[layout.entryCount++] = {0, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    layout.entries[layout.entryCount++] = {1, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    layout.entries[layout.entryCount++] = {2, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    m_geometryLayout = device.CreateBindingLayout(layout);
    m_emptyLayout = device.CreateBindingLayout({});
    rhi::BindGroupDesc emptyGroupDesc;
    emptyGroupDesc.layout = m_emptyLayout;
    m_emptyGroup = device.CreateBindGroup(emptyGroupDesc);
    if (!m_geometryLayout.IsValid() || !m_emptyLayout.IsValid() || !m_emptyGroup.IsValid() ||
        !m_surface.Create(device, desc.shaderProgram, desc.material, desc.fallbackMaterial, desc.semantics,
                          desc.textureResolver, desc.deletionQueue)) {
        Destroy();
        return false;
    }
    m_geometryGroup = CreateGeometryGroup(m_topology->SortedIndexBuffer());
    if (!m_geometryGroup.IsValid()) {
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
        m_device->Release(m_geometryGroup);
        m_device->Release(m_emptyGroup);
        m_device->Release(m_geometryLayout);
        m_device->Release(m_emptyLayout);
        m_device->Release(m_pickingFragmentShader);
        m_device->Release(m_motionFragmentShader);
        m_device->Release(m_motionVertexShader);
        m_device->Release(m_forwardPlusFragmentShader);
        m_device->Release(m_fragmentShader);
        m_device->Release(m_vertexShader);
    }
    m_surface.Destroy();
    m_device = nullptr;
    m_topology.reset();
    m_shaderProgram.reset();
    m_semantics = {};
    m_uvMode = ParticleRibbonUvMode::Stretch;
    m_uvScale = 1.0f;
    m_vertexShader = {};
    m_fragmentShader = {};
    m_forwardPlusFragmentShader = {};
    m_pickingFragmentShader = {};
    m_motionVertexShader = {};
    m_motionFragmentShader = {};
    m_geometryLayout = {};
    m_geometryGroup = {};
    m_emptyLayout = {};
    m_emptyGroup = {};
    m_viewGroups.clear();
    m_pipelines.clear();
}

bool ParticleGpuRibbonRenderer::IsValid() const noexcept
{
    return m_device && m_topology && m_topology->IsValid() && m_vertexShader.IsValid() && m_fragmentShader.IsValid() &&
           m_pickingFragmentShader.IsValid() && m_motionVertexShader.IsValid() && m_motionFragmentShader.IsValid() &&
           m_geometryLayout.IsValid() && m_geometryGroup.IsValid() && m_emptyLayout.IsValid() &&
           m_emptyGroup.IsValid() && m_surface.IsValid() &&
           (!m_semantics.receiveSceneLighting || m_forwardPlusFragmentShader.IsValid());
}

int32_t ParticleGpuRibbonRenderer::RenderQueue() const noexcept
{
    return m_surface.ResolveMaterialState().renderQueue;
}

rhi::BufferHandle ParticleGpuRibbonRenderer::InstanceBuffer() const noexcept
{
    return m_topology ? m_topology->InstanceBuffer() : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRibbonRenderer::RenderIndexBuffer() const noexcept
{
    return m_topology ? m_topology->SortedIndexBuffer() : rhi::BufferHandle{};
}

rhi::BindGroupHandle ParticleGpuRibbonRenderer::CreateGeometryGroup(rhi::BufferHandle renderIndices) const
{
    if (!m_device || !m_geometryLayout.IsValid() || !InstanceBuffer().IsValid() || !renderIndices.IsValid())
        return {};
    rhi::BindGroupDesc group;
    group.layout = m_geometryLayout;
    group.buffers[group.bufferCount++] = {0, rhi::BindingType::StorageBuffer, InstanceBuffer(), 0, 0};
    group.buffers[group.bufferCount++] = {1, rhi::BindingType::StorageBuffer, RenderIndexBuffer(), 0, 0};
    group.buffers[group.bufferCount++] = {2, rhi::BindingType::StorageBuffer, renderIndices, 0, 0};
    return m_device->CreateBindGroup(group);
}

rhi::BindGroupHandle ParticleGpuRibbonRenderer::ResolveGeometryGroup(rhi::BufferHandle renderIndices)
{
    if (!renderIndices.IsValid() || renderIndices == RenderIndexBuffer())
        return m_geometryGroup;
    const auto found = std::find_if(m_viewGroups.begin(), m_viewGroups.end(),
                                    [&](const auto &entry) { return entry.renderIndices == renderIndices; });
    if (found != m_viewGroups.end())
        return found->group;
    const auto group = CreateGeometryGroup(renderIndices);
    if (group.IsValid())
        m_viewGroups.push_back({renderIndices, group});
    return group;
}

bool ParticleGpuRibbonRenderer::RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                           rhi::RenderTargetLayoutHandle renderTargetLayout,
                                           const MaterialPassPipelineDescriptor &pass,
                                           rhi::BufferHandle indirectArguments, const GpuParticleViewConstants &view,
                                           rhi::BufferHandle renderIndices, rhi::TextureViewHandle sceneDepth,
                                           bool sceneDepthIsDepth, const GpuParticlePerViewBindings &perView)
{
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid() || !m_surface.RefreshMaterialBuffer(false) ||
        !m_surface.RefreshTextureBindings(false))
        return false;
    if (m_semantics.softParticles && !sceneDepth.IsValid())
        return false;
    const bool usesForwardPlusLighting =
        pass.target == ShaderCompileTarget::ForwardPlus && m_semantics.receiveSceneLighting;
    const bool usesPerViewBindings =
        pass.target == ShaderCompileTarget::Forward || pass.target == ShaderCompileTarget::ForwardPlus;
    if (usesPerViewBindings && !perView.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass,
                                              usesPerViewBindings ? perView.layout : rhi::BindingLayoutHandle{});
    const auto geometryGroup = ResolveGeometryGroup(renderIndices);
    const auto surfaceGroup = m_surface.ResolveBindGroup(sceneDepth, sceneDepthIsDepth);
    if (!pipeline.IsValid() || !geometryGroup.IsValid() || !surfaceGroup.IsValid())
        return false;
    auto constants = view;
    constants.materialTint = {1.0f, 1.0f, 1.0f, 1.0f};
    constants.lightingControl[0] = usesForwardPlusLighting ? 1.0f : 0.0f;
    constants.renderingControl[0] = m_semantics.receiveShadows ? 1.0f : 0.0f;
    constants.renderingControl[1] = m_uvScale;
    constants.renderingControl[2] = m_uvMode == ParticleRibbonUvMode::Repeat ? 1.0f : 0.0f;
    constants.renderingControl[3] = renderIndices.IsValid() && renderIndices != RenderIndexBuffer() ? 1.0f : 0.0f;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, geometryGroup);
    encoder.BindGroup(pipeline, 1, usesPerViewBindings ? perView.group : m_emptyGroup);
    encoder.BindGroup(pipeline, 2, surfaceGroup);
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
    const auto geometryGroup = ResolveGeometryGroup(renderIndices);
    const auto surfaceGroup = m_surface.ResolveBindGroup();
    if (!pipeline.IsValid() || !geometryGroup.IsValid() || !surfaceGroup.IsValid())
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
    constants.renderingControl[3] = renderIndices.IsValid() && renderIndices != RenderIndexBuffer() ? 1.0f : 0.0f;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, geometryGroup);
    encoder.BindGroup(pipeline, 1, m_emptyGroup);
    encoder.BindGroup(pipeline, 2, surfaceGroup);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

rhi::GraphicsPipelineHandle
ParticleGpuRibbonRenderer::GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                               const MaterialPassPipelineDescriptor &pass,
                                               rhi::BindingLayoutHandle perViewLayout)
{
    if (!renderTargetLayout.IsValid() || !pass.IsValid() ||
        (pass.target != ShaderCompileTarget::Forward && pass.target != ShaderCompileTarget::ForwardPlus &&
         pass.target != ShaderCompileTarget::Picking && pass.target != ShaderCompileTarget::Motion))
        return {};
    const bool picking = pass.target == ShaderCompileTarget::Picking;
    const bool motion = pass.target == ShaderCompileTarget::Motion;
    const bool usesForwardPlusLighting =
        pass.target == ShaderCompileTarget::ForwardPlus && m_semantics.receiveSceneLighting;
    const bool usesPerViewBindings =
        pass.target == ShaderCompileTarget::Forward || pass.target == ShaderCompileTarget::ForwardPlus;
    if (usesPerViewBindings && !perViewLayout.IsValid())
        return {};
    const auto state = picking  ? GpuBillboardMaterialState{3000, false, true, true}
                       : motion ? GpuBillboardMaterialState{3000, false, true, false}
                                : m_surface.ResolveMaterialState();
    const uint8_t signature = PipelineStateSignature(state);
    const auto found = std::find_if(m_pipelines.begin(), m_pipelines.end(), [&](const auto &entry) {
        return entry.renderTargetLayout == renderTargetLayout && entry.pass == pass &&
               entry.perViewLayout == perViewLayout && entry.materialStateSignature == signature;
    });
    if (found != m_pipelines.end())
        return found->pipeline;

    rhi::GraphicsPipelineDesc desc;
    desc.vertexShader = motion ? m_motionVertexShader : m_vertexShader;
    desc.fragmentShader = picking  ? m_pickingFragmentShader
                          : motion ? m_motionFragmentShader
                                   : (usesForwardPlusLighting ? m_forwardPlusFragmentShader : m_fragmentShader);
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
    desc.bindingLayouts[0] = m_geometryLayout;
    desc.bindingLayouts[1] = usesPerViewBindings ? perViewLayout : m_emptyLayout;
    desc.bindingLayouts[2] = m_surface.Layout();
    desc.bindingLayoutCount = 3;
    desc.pushConstantStages = rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment;
    desc.pushConstantBytes = sizeof(GpuParticleViewConstants);
    const auto pipeline = m_device->CreateGraphicsPipeline(desc);
    if (pipeline.IsValid())
        m_pipelines.push_back({renderTargetLayout, pass, perViewLayout, signature, pipeline});
    return pipeline;
}

} // namespace infernux::particle
