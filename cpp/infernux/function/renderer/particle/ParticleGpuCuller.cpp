#include "ParticleGpuCuller.h"

#include <array>
#include <string>

namespace infernux::particle
{

namespace
{

constexpr std::string_view CommonBindings = R"glsl(
struct ParticleInstance {
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
    vec4 scale_custom;
    uvec4 ribbon_data;
    vec4 custom_data;
    vec4 previous_position_history;
};
layout(std430, set = 0, binding = 0) readonly buffer Instances { ParticleInstance instances[]; };
layout(std430, set = 0, binding = 1) readonly buffer SourceIndirectArguments {
    uint source_vertex_count;
    uint source_instance_count;
    uint source_first_vertex;
    uint source_first_instance;
};
layout(std430, set = 0, binding = 2) buffer VisibleIndices { uint visible_indices[]; };
layout(std430, set = 0, binding = 3) buffer DrawIndirectArguments {
    uint draw_vertex_count;
    uint draw_instance_count;
    uint draw_first_vertex;
    uint draw_first_instance;
};
layout(std430, set = 0, binding = 4) buffer SortDispatchArguments {
    uint sort_group_count_x;
    uint sort_group_count_y;
    uint sort_group_count_z;
    uint stats_source_count;
    uint stats_flags;
};
layout(std430, set = 0, binding = 5) readonly buffer Bounds {
    uint bounds_min_x;
    uint bounds_min_y;
    uint bounds_min_z;
    uint bounds_max_x;
    uint bounds_max_y;
    uint bounds_max_z;
    uint bounds_valid;
    uint bounds_reserved;
};
layout(std430, set = 0, binding = 6) buffer SimulationControl {
    uint any_view_visible;
    uint simulation_allowed;
    uint offscreen_policy;
    uint simulation_control_reserved;
};
layout(std430, set = 0, binding = 7) readonly buffer SourceIndices { uint source_indices[]; };
layout(push_constant) uniform CullConstants {
    vec4 frustum_planes[6];
    uint capacity;
    uint vertex_count;
    uint mode;
    uint reserved2;
} pc;
)glsl";

std::string BuildShader(std::string_view localSize, std::string_view body)
{
    std::string result;
    result.reserve(localSize.size() + CommonBindings.size() + body.size() + 16);
    result += "#version 450\n";
    result += localSize;
    result += CommonBindings;
    result += body;
    return result;
}

bool IsShaderBytecodeValid(const ShaderBytecode &bytecode) noexcept
{
    return bytecode.words && bytecode.wordCount >= 5 && bytecode.words[0] == 0x07230203u;
}

} // namespace

std::string_view GpuParticleCullShaderSources::Reset() noexcept
{
    static const std::string Source = BuildShader("layout(local_size_x = 1) in;\n", R"glsl(
void main() {
    bool ribbon_segments = pc.mode == 1u;
    draw_vertex_count = ribbon_segments ? 6u : pc.vertex_count;
    draw_instance_count = 0u;
    draw_first_vertex = source_first_vertex;
    draw_first_instance = source_first_instance;
    uint source_count = ribbon_segments
        ? min(source_vertex_count / 6u, pc.capacity > 0u ? pc.capacity - 1u : 0u)
        : min(source_instance_count, pc.capacity);
    stats_source_count = source_count;
    stats_flags = (bounds_valid != 0u ? 1u : 0u) | (ribbon_segments ? 4u : 0u);
    bool bounds_visible = true;
    bool bounds_fully_inside = false;
    if (bounds_valid != 0u) {
        uint ordered_min[3] = uint[3](bounds_min_x, bounds_min_y, bounds_min_z);
        uint ordered_max[3] = uint[3](bounds_max_x, bounds_max_y, bounds_max_z);
        vec3 lower;
        vec3 upper;
        for (uint axis = 0u; axis < 3u; ++axis) {
            uint min_bits = (ordered_min[axis] & 0x80000000u) != 0u
                                ? (ordered_min[axis] ^ 0x80000000u)
                                : ~ordered_min[axis];
            uint max_bits = (ordered_max[axis] & 0x80000000u) != 0u
                                ? (ordered_max[axis] ^ 0x80000000u)
                                : ~ordered_max[axis];
            lower[axis] = uintBitsToFloat(min_bits);
            upper[axis] = uintBitsToFloat(max_bits);
        }
        bounds_fully_inside = true;
        for (uint plane_index = 0u; plane_index < 6u; ++plane_index) {
            vec4 plane = pc.frustum_planes[plane_index];
            vec3 positive_vertex = mix(lower, upper, greaterThanEqual(plane.xyz, vec3(0.0)));
            if (dot(plane.xyz, positive_vertex) + plane.w < 0.0)
                bounds_visible = false;
            vec3 negative_vertex = mix(lower, upper, lessThan(plane.xyz, vec3(0.0)));
            if (dot(plane.xyz, negative_vertex) + plane.w < 0.0)
                bounds_fully_inside = false;
        }
    }
    if (bounds_valid == 0u || bounds_visible) stats_flags |= 2u;
    // The cull pass already declares draw arguments as compute read/write. Use
    // the high bit as a transient marker so the fast path does not introduce a
    // second shader read of the profile-only stats buffer.
    if (bounds_fully_inside) draw_instance_count = 0x80000000u;
    if (bounds_valid == 0u || bounds_visible) atomicOr(any_view_visible, 1u);
    sort_group_count_x = bounds_visible ? (source_count + 255u) / 256u : 0u;
    sort_group_count_y = 1u;
    sort_group_count_z = 1u;
}
)glsl");
    return Source;
}

std::string_view GpuParticleCullShaderSources::Cull() noexcept
{
    static const std::string Source = BuildShader("layout(local_size_x = 256) in;\n", R"glsl(
shared uint local_visible_count;
shared uint global_base;

bool inx_visible_sphere(vec3 center, float radius) {
    for (uint plane_index = 0u; plane_index < 6u; ++plane_index) {
        vec4 plane = pc.frustum_planes[plane_index];
        if (dot(plane.xyz, center) + plane.w < -radius) return false;
    }
    return true;
}
void main() {
    bool bounds_fully_inside = (draw_instance_count & 0x80000000u) != 0u;
    if (gl_LocalInvocationIndex == 0u) {
        local_visible_count = 0u;
        global_base = 0u;
        draw_instance_count = 0u;
    }
    barrier();

    uint source_index = gl_GlobalInvocationID.x;
    bool ribbon_segments = pc.mode == 1u;
    uint source_count = ribbon_segments
        ? min(source_vertex_count / 6u, pc.capacity > 0u ? pc.capacity - 1u : 0u)
        : min(source_instance_count, pc.capacity);
    bool visible = false;
    uint output_value = 0u;
    if (ribbon_segments) {
        if (source_index < source_count) {
            uint first_index = source_indices[source_index];
            uint second_index = source_indices[source_index + 1u];
            if (first_index < pc.capacity && second_index < pc.capacity) {
                ParticleInstance first = instances[first_index];
                ParticleInstance second = instances[second_index];
                if (first.ribbon_data.x == second.ribbon_data.x && (second.ribbon_data.z & 1u) == 0u) {
                    visible = bounds_fully_inside || inx_visible_sphere(
                                                          (first.position_size.xyz + second.position_size.xyz) * 0.5,
                                                          length(second.position_size.xyz - first.position_size.xyz) * 0.5 +
                                                              max(abs(first.position_size.w), abs(second.position_size.w)) *
                                                                  0.5);
                    output_value = source_index;
                }
            }
        }
    } else {
        if (source_index < source_count) {
            uint particle_index = source_indices[source_index];
            if (particle_index < pc.capacity) {
                ParticleInstance instance = instances[particle_index];
                visible = bounds_fully_inside || inx_visible_sphere(
                                                   instance.position_size.xyz,
                                                   abs(instance.position_size.w) *
                                                       max(max(abs(instance.scale_custom.x), abs(instance.scale_custom.y)),
                                                           abs(instance.scale_custom.z)) *
                                                       1.41421356237);
                output_value = particle_index;
            }
        }
    }

    uint local_output_index = 0u;
    if (visible) local_output_index = atomicAdd(local_visible_count, 1u);
    barrier();
    if (gl_LocalInvocationIndex == 0u) global_base = atomicAdd(draw_instance_count, local_visible_count);
    barrier();

    if (visible && global_base < pc.capacity && local_output_index < pc.capacity - global_base) {
        visible_indices[global_base + local_output_index] = output_value;
    }
}
)glsl");
    return Source;
}

std::string_view GpuParticleCullShaderSources::Finalize() noexcept
{
    static const std::string Source = BuildShader("layout(local_size_x = 1) in;\n", R"glsl(
void main() {
    // Reset uses the high bit as a transient fully-inside marker. Cull normally
    // clears it before writing the real count, but a zero-source dispatch is
    // intentionally skipped and still reaches this finalizer.
    uint visible_count = min(draw_instance_count & 0x7fffffffu, pc.capacity);
    draw_instance_count = visible_count;
    sort_group_count_x = (visible_count + 255u) / 256u;
    sort_group_count_y = 1u;
    sort_group_count_z = 1u;
}
)glsl");
    return Source;
}

bool GpuParticleCullProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(reset) && IsShaderBytecodeValid(cull) && IsShaderBytecodeValid(finalize);
}

bool GpuParticleCullProgramStorage::Assign(const GpuParticleCullProgram &program)
{
    if (!program.IsValid())
        return false;
    const std::array<ShaderBytecode, 3> sources = {program.reset, program.cull, program.finalize};
    std::array<std::vector<uint32_t>, 3> candidate;
    for (size_t index = 0; index < sources.size(); ++index)
        candidate[index].assign(sources[index].words, sources[index].words + sources[index].wordCount);
    shaders = std::move(candidate);
    return true;
}

bool GpuParticleCullProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleCullProgram GpuParticleCullProgramStorage::View() const noexcept
{
    return {
        {shaders[0].data(), shaders[0].size()},
        {shaders[1].data(), shaders[1].size()},
        {shaders[2].data(), shaders[2].size()},
    };
}

ParticleGpuCuller::~ParticleGpuCuller()
{
    Destroy();
}

bool ParticleGpuCuller::Create(rhi::Device &device, const GpuParticleCullerDesc &desc)
{
    Destroy();
    if (desc.capacity == 0 || desc.vertexCount == 0 || !desc.instances.IsValid() ||
        !desc.sourceIndirectArguments.IsValid() || !desc.sourceIndices.IsValid() || !desc.bounds.IsValid() ||
        !desc.simulationControl.IsValid() || !desc.program.IsValid()) {
        return false;
    }

    m_device = &device;
    m_capacity = desc.capacity;
    m_vertexCount = desc.vertexCount;
    m_instances = desc.instances;
    m_sourceIndirectArguments = desc.sourceIndirectArguments;
    m_sourceIndices = desc.sourceIndices;
    m_bounds = desc.bounds;
    m_simulationControl = desc.simulationControl;
    m_mode = desc.mode;
    const auto storage = rhi::BufferUsageFlags::Storage;
    m_visibleIndices = device.CreateBuffer({static_cast<uint64_t>(desc.capacity) * sizeof(uint32_t), storage});
    m_drawIndirectArguments =
        device.CreateBuffer({16, storage | rhi::BufferUsageFlags::Indirect | rhi::BufferUsageFlags::TransferSource});
    m_sortDispatchArguments =
        device.CreateBuffer({sizeof(GpuParticleCullDispatchState),
                             storage | rhi::BufferUsageFlags::Indirect | rhi::BufferUsageFlags::TransferSource});
    if (!m_visibleIndices.IsValid() || !m_drawIndirectArguments.IsValid() || !m_sortDispatchArguments.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 8; ++binding)
        layoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 8;
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    const std::array<rhi::BufferHandle, 8> buffers = {
        m_instances, m_sourceIndirectArguments, m_visibleIndices, m_drawIndirectArguments, m_sortDispatchArguments,
        m_bounds,    m_simulationControl,       m_sourceIndices,
    };
    for (uint32_t binding = 0; binding < buffers.size(); ++binding)
        groupDesc.buffers[binding] = {binding, rhi::BindingType::StorageBuffer, buffers[binding], 0, 0};
    groupDesc.bufferCount = static_cast<uint32_t>(buffers.size());
    m_group = device.CreateBindGroup(groupDesc);
    if (!m_group.IsValid()) {
        Destroy();
        return false;
    }

    const std::array<ShaderBytecode, 3> shaders = {desc.program.reset, desc.program.cull, desc.program.finalize};
    std::array<rhi::ComputePipelineHandle *, 3> pipelines = {&m_resetPipeline, &m_cullPipeline, &m_finalizePipeline};
    for (size_t index = 0; index < shaders.size(); ++index) {
        const auto shader = device.CreateShaderModule({shaders[index].words, shaders[index].wordCount});
        if (!shader.IsValid()) {
            Destroy();
            return false;
        }
        rhi::ComputePipelineDesc pipelineDesc;
        pipelineDesc.computeShader = shader;
        pipelineDesc.bindingLayouts[0] = m_layout;
        pipelineDesc.bindingLayoutCount = 1;
        pipelineDesc.pushConstantBytes = sizeof(GpuParticleCullConstants);
        *pipelines[index] = device.CreateComputePipeline(pipelineDesc);
        device.Release(shader);
        if (!pipelines[index]->IsValid()) {
            Destroy();
            return false;
        }
    }
    return true;
}

void ParticleGpuCuller::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_finalizePipeline);
        m_device->Release(m_cullPipeline);
        m_device->Release(m_resetPipeline);
        m_device->Release(m_group);
        m_device->Release(m_layout);
        m_device->Release(m_sortDispatchArguments);
        m_device->Release(m_drawIndirectArguments);
        m_device->Release(m_visibleIndices);
    }
    m_device = nullptr;
    m_capacity = 0;
    m_vertexCount = 0;
    m_instances = {};
    m_sourceIndirectArguments = {};
    m_sourceIndices = {};
    m_bounds = {};
    m_simulationControl = {};
    m_mode = GpuParticleCullMode::Instances;
    m_visibleIndices = {};
    m_drawIndirectArguments = {};
    m_sortDispatchArguments = {};
    m_layout = {};
    m_group = {};
    m_resetPipeline = {};
    m_cullPipeline = {};
    m_finalizePipeline = {};
}

bool ParticleGpuCuller::IsValid() const noexcept
{
    return m_device && m_capacity > 0 && m_vertexCount > 0 && m_instances.IsValid() &&
           m_sourceIndirectArguments.IsValid() && m_sourceIndices.IsValid() && m_bounds.IsValid() &&
           m_simulationControl.IsValid() && m_visibleIndices.IsValid() && m_drawIndirectArguments.IsValid() &&
           m_sortDispatchArguments.IsValid() && m_layout.IsValid() && m_group.IsValid() && m_resetPipeline.IsValid() &&
           m_cullPipeline.IsValid() && m_finalizePipeline.IsValid();
}

void ParticleGpuCuller::RecordReset(const rhi::ComputeCommandEncoder &encoder,
                                    const std::array<float, PlaneCount * 4> &frustumPlanes) const
{
    GpuParticleCullConstants constants;
    constants.frustumPlanes = frustumPlanes;
    constants.capacity = m_capacity;
    constants.vertexCount = m_vertexCount;
    constants.mode = m_mode;
    Record(encoder, m_resetPipeline, constants, 1);
}

void ParticleGpuCuller::RecordCull(const rhi::ComputeCommandEncoder &encoder,
                                   const std::array<float, PlaneCount * 4> &frustumPlanes) const
{
    GpuParticleCullConstants constants;
    constants.frustumPlanes = frustumPlanes;
    constants.capacity = m_capacity;
    constants.vertexCount = m_vertexCount;
    constants.mode = m_mode;
    if (!IsValid() || !encoder.IsValid() || !m_cullPipeline.IsValid())
        return;
    encoder.BindPipeline(m_cullPipeline);
    encoder.BindGroup(m_cullPipeline, 0, m_group);
    encoder.PushConstants(m_cullPipeline, sizeof(constants), &constants);
    encoder.DispatchIndirect(m_sortDispatchArguments);
}

void ParticleGpuCuller::RecordFinalize(const rhi::ComputeCommandEncoder &encoder) const
{
    GpuParticleCullConstants constants;
    constants.capacity = m_capacity;
    constants.vertexCount = m_vertexCount;
    constants.mode = m_mode;
    Record(encoder, m_finalizePipeline, constants, 1);
}

void ParticleGpuCuller::Record(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                               const GpuParticleCullConstants &constants, uint32_t groups) const
{
    if (!IsValid() || !encoder.IsValid() || !pipeline.IsValid() || groups == 0)
        return;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, m_group);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.Dispatch(groups, 1, 1);
}

} // namespace infernux::particle
