#include "ParticleGpuBounds.h"

#include <algorithm>
#include <array>
#include <string>
#include <utility>

namespace infernux::particle
{

namespace
{

constexpr std::string_view CommonBindings = R"glsl(
struct ParticleVisibilityInstance {
    vec4 position_radius;
};
layout(std430, set = 0, binding = 0) readonly buffer Visibility {
    ParticleVisibilityInstance visibility[];
};
layout(std430, set = 0, binding = 1) readonly buffer SourceIndirectArguments {
    uint source_vertex_count;
    uint source_instance_count;
    uint source_first_vertex;
    uint source_first_instance;
};
layout(std430, set = 0, binding = 2) buffer Bounds {
    uint min_x;
    uint min_y;
    uint min_z;
    uint max_x;
    uint max_y;
    uint max_z;
    uint bounds_valid;
    uint bounds_reserved;
};
layout(std430, set = 0, binding = 3) buffer DispatchArguments {
    uint dispatch_group_count_x;
    uint dispatch_group_count_y;
    uint dispatch_group_count_z;
};
layout(std430, set = 0, binding = 4) buffer SimulationControl {
    uint any_view_visible;
    uint simulation_allowed;
    uint offscreen_policy;
    uint simulation_control_reserved;
};
layout(std430, set = 0, binding = 5) readonly buffer SourceIndices { uint source_indices[]; };
layout(push_constant) uniform BoundsConstants {
    vec4 manual_lower;
    vec4 manual_upper;
    uint capacity;
    uint bounds_mode;
    uint requested_offscreen_policy;
    uint force_simulation;
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

std::string_view GpuParticleBoundsShaderSources::Prepare() noexcept
{
    static const std::string Source = BuildShader("layout(local_size_x = 1) in;\n", R"glsl(
void main() {
    // Always-simulate emitters do not consume visibility feedback. Leaving
    // that word to the graphics culler removes a cross-queue read/write race
    // while async simulation overlaps the previous frame's rendering.
    uint was_visible = pc.requested_offscreen_policy == 0u ? 1u : atomicExchange(any_view_visible, 0u);
    offscreen_policy = pc.requested_offscreen_policy;
    simulation_allowed = pc.requested_offscreen_policy == 0u || was_visible != 0u || pc.force_simulation != 0u
                             ? 1u
                             : 0u;
    simulation_control_reserved = 0u;
}
)glsl");
    return Source;
}

std::string_view GpuParticleBoundsShaderSources::Reset() noexcept
{
    static const std::string Source = BuildShader("layout(local_size_x = 1) in;\n", R"glsl(
uint inx_ordered_float(float value) {
    uint bits = floatBitsToUint(value);
    return (bits & 0x80000000u) != 0u ? ~bits : (bits ^ 0x80000000u);
}
void main() {
    uint source_count = min(source_instance_count, pc.capacity);
    if (pc.bounds_mode == 1u) {
        min_x = inx_ordered_float(pc.manual_lower.x);
        min_y = inx_ordered_float(pc.manual_lower.y);
        min_z = inx_ordered_float(pc.manual_lower.z);
        max_x = inx_ordered_float(pc.manual_upper.x);
        max_y = inx_ordered_float(pc.manual_upper.y);
        max_z = inx_ordered_float(pc.manual_upper.z);
        bounds_valid = 1u;
        dispatch_group_count_x = 0u;
    } else if (simulation_allowed != 0u) {
        min_x = 0xffffffffu;
        min_y = 0xffffffffu;
        min_z = 0xffffffffu;
        max_x = 0u;
        max_y = 0u;
        max_z = 0u;
        bounds_valid = source_count > 0u ? 1u : 0u;
        dispatch_group_count_x = (source_count + 255u) / 256u;
    } else {
        dispatch_group_count_x = 0u;
    }
    bounds_reserved = 0u;
    dispatch_group_count_y = 1u;
    dispatch_group_count_z = 1u;
}
)glsl");
    return Source;
}

std::string_view GpuParticleBoundsShaderSources::Reduce() noexcept
{
    static const std::string Source = BuildShader("layout(local_size_x = 256) in;\n", R"glsl(
uint inx_ordered_float(float value) {
    uint bits = floatBitsToUint(value);
    return (bits & 0x80000000u) != 0u ? ~bits : (bits ^ 0x80000000u);
}
bool inx_is_finite(float value) {
    return !isnan(value) && !isinf(value);
}
bool inx_is_finite_vec3(vec3 value) {
    return inx_is_finite(value.x) && inx_is_finite(value.y) && inx_is_finite(value.z);
}
shared uvec3 shared_lower[256];
shared uvec3 shared_upper[256];
void main() {
    uint local_index = gl_LocalInvocationID.x;
    uvec3 lower_bits = uvec3(0xffffffffu);
    uvec3 upper_bits = uvec3(0u);
    uint index = gl_GlobalInvocationID.x;
    uint source_count = min(source_instance_count, pc.capacity);
    if (simulation_allowed != 0u && index < source_count) {
        uint particle_index = source_indices[index];
        if (particle_index < pc.capacity) {
            ParticleVisibilityInstance instance = visibility[particle_index];
            vec3 position = instance.position_radius.xyz;
            float radius = instance.position_radius.w;
            bool valid = inx_is_finite_vec3(position) && inx_is_finite(radius);
            if (valid) {
                valid = radius >= 0.0;
                if (valid) {
                    vec3 bounds_lower = position - vec3(radius);
                    vec3 bounds_upper = position + vec3(radius);
                    valid = inx_is_finite_vec3(bounds_lower) && inx_is_finite_vec3(bounds_upper);
                    if (valid) {
                        lower_bits = uvec3(inx_ordered_float(bounds_lower.x), inx_ordered_float(bounds_lower.y),
                                           inx_ordered_float(bounds_lower.z));
                        upper_bits = uvec3(inx_ordered_float(bounds_upper.x), inx_ordered_float(bounds_upper.y),
                                           inx_ordered_float(bounds_upper.z));
                    }
                }
            }
        }
    }

    shared_lower[local_index] = lower_bits;
    shared_upper[local_index] = upper_bits;
    barrier();
    for (uint stride = 128u; stride > 0u; stride >>= 1u) {
        if (local_index < stride) {
            shared_lower[local_index] = min(shared_lower[local_index], shared_lower[local_index + stride]);
            shared_upper[local_index] = max(shared_upper[local_index], shared_upper[local_index + stride]);
        }
        barrier();
    }

    if (simulation_allowed != 0u && local_index == 0u) {
        atomicMin(min_x, shared_lower[0].x);
        atomicMin(min_y, shared_lower[0].y);
        atomicMin(min_z, shared_lower[0].z);
        atomicMax(max_x, shared_upper[0].x);
        atomicMax(max_y, shared_upper[0].y);
        atomicMax(max_z, shared_upper[0].z);
    }
}
)glsl");
    return Source;
}

bool GpuParticleBoundsProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(prepare) && IsShaderBytecodeValid(reset) && IsShaderBytecodeValid(reduce);
}

bool GpuParticleBoundsProgramStorage::Assign(const GpuParticleBoundsProgram &program)
{
    if (!program.IsValid())
        return false;
    const std::array<ShaderBytecode, 3> sources = {program.prepare, program.reset, program.reduce};
    std::array<std::vector<uint32_t>, 3> candidate;
    for (size_t index = 0; index < sources.size(); ++index)
        candidate[index].assign(sources[index].words, sources[index].words + sources[index].wordCount);
    shaders = std::move(candidate);
    return true;
}

bool GpuParticleBoundsProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleBoundsProgram GpuParticleBoundsProgramStorage::View() const noexcept
{
    return {
        {shaders[0].data(), shaders[0].size()},
        {shaders[1].data(), shaders[1].size()},
        {shaders[2].data(), shaders[2].size()},
    };
}

ParticleGpuBounds::~ParticleGpuBounds()
{
    Destroy();
}

bool ParticleGpuBounds::Create(rhi::Device &device, const GpuParticleBoundsDesc &desc)
{
    Destroy();
    if (desc.capacity == 0 || !desc.visibility.IsValid() || !desc.sourceIndices.IsValid() ||
        !desc.sourceIndirectArguments.IsValid() || !desc.simulationControl.IsValid() || !desc.program.IsValid()) {
        return false;
    }

    m_device = &device;
    m_capacity = desc.capacity;
    m_visibility = desc.visibility;
    m_sourceIndices = desc.sourceIndices;
    m_sourceIndirectArguments = desc.sourceIndirectArguments;
    m_simulationControl = desc.simulationControl;
    const auto storage = rhi::BufferUsageFlags::Storage;
    const auto createSharedStorage = [&](uint64_t bytes, rhi::BufferUsageFlags usage) {
        rhi::BufferDesc bufferDesc;
        bufferDesc.byteSize = bytes;
        bufferDesc.usage = usage;
        // Prepare/Reset run inside GpuParticle/Simulation on the independent
        // compute family. A Graphics-only exclusive dispatch buffer hangs that
        // queue the first time Game-only preview creates a new emitter.
        bufferDesc.queueAccess = rhi::QueueAccessFlags::Graphics | rhi::QueueAccessFlags::Compute;
        return device.CreateBuffer(bufferDesc);
    };
    m_bounds = createSharedStorage(BoundsBufferBytes, storage | rhi::BufferUsageFlags::TransferSource);
    m_dispatchArguments = createSharedStorage(DispatchBufferBytes, storage | rhi::BufferUsageFlags::Indirect);
    if (!m_bounds.IsValid() || !m_dispatchArguments.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 6; ++binding)
        layoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 6;
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    const std::array<rhi::BufferHandle, 6> buffers = {m_visibility,        m_sourceIndirectArguments, m_bounds,
                                                      m_dispatchArguments, m_simulationControl,       m_sourceIndices};
    for (uint32_t binding = 0; binding < buffers.size(); ++binding)
        groupDesc.buffers[binding] = {binding, rhi::BindingType::StorageBuffer, buffers[binding], 0, 0};
    groupDesc.bufferCount = static_cast<uint32_t>(buffers.size());
    m_group = device.CreateBindGroup(groupDesc);
    if (!m_group.IsValid()) {
        Destroy();
        return false;
    }

    const std::array<ShaderBytecode, 3> shaders = {desc.program.prepare, desc.program.reset, desc.program.reduce};
    std::array<rhi::ComputePipelineHandle *, 3> pipelines = {&m_preparePipeline, &m_resetPipeline, &m_reducePipeline};
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
        pipelineDesc.pushConstantBytes = sizeof(GpuParticleBoundsConstants);
        *pipelines[index] = device.CreateComputePipeline(pipelineDesc);
        device.Release(shader);
        if (!pipelines[index]->IsValid()) {
            Destroy();
            return false;
        }
    }
    return true;
}

void ParticleGpuBounds::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_reducePipeline);
        m_device->Release(m_resetPipeline);
        m_device->Release(m_preparePipeline);
        m_device->Release(m_group);
        m_device->Release(m_layout);
        m_device->Release(m_dispatchArguments);
        m_device->Release(m_bounds);
    }
    m_device = nullptr;
    m_capacity = 0;
    m_visibility = {};
    m_sourceIndices = {};
    m_sourceIndirectArguments = {};
    m_simulationControl = {};
    m_bounds = {};
    m_dispatchArguments = {};
    m_layout = {};
    m_group = {};
    m_preparePipeline = {};
    m_resetPipeline = {};
    m_reducePipeline = {};
    m_controlPrepared = false;
    m_preparedPolicy = GpuParticleOffscreenPolicy::AlwaysSimulate;
    m_preparedForceSimulation = false;
}

bool ParticleGpuBounds::IsValid() const noexcept
{
    return m_device && m_capacity > 0 && m_visibility.IsValid() && m_sourceIndices.IsValid() &&
           m_sourceIndirectArguments.IsValid() && m_simulationControl.IsValid() && m_bounds.IsValid() &&
           m_dispatchArguments.IsValid() && m_layout.IsValid() && m_group.IsValid() && m_preparePipeline.IsValid() &&
           m_resetPipeline.IsValid() && m_reducePipeline.IsValid();
}

void ParticleGpuBounds::RecordPrepare(const rhi::ComputeCommandEncoder &encoder, GpuParticleOffscreenPolicy policy,
                                      bool forceSimulation) const
{
    if (!IsValid() || !encoder.IsValid())
        return;
    if (m_controlPrepared && policy == GpuParticleOffscreenPolicy::AlwaysSimulate &&
        m_preparedPolicy == GpuParticleOffscreenPolicy::AlwaysSimulate && m_preparedForceSimulation == forceSimulation)
        return;
    GpuParticleBoundsConstants constants;
    constants.capacity = m_capacity;
    constants.offscreenPolicy = policy;
    constants.forceSimulation = forceSimulation ? 1u : 0u;
    Bind(encoder, m_preparePipeline, constants);
    encoder.Dispatch(1, 1, 1);
    m_controlPrepared = true;
    m_preparedPolicy = policy;
    m_preparedForceSimulation = forceSimulation;
}

void ParticleGpuBounds::RecordReset(const rhi::ComputeCommandEncoder &encoder, GpuParticleBoundsMode mode,
                                    const std::array<float, 3> &manualLower,
                                    const std::array<float, 3> &manualUpper) const
{
    GpuParticleBoundsConstants constants;
    constants.capacity = m_capacity;
    constants.boundsMode = mode;
    std::copy(manualLower.begin(), manualLower.end(), constants.manualLower.begin());
    std::copy(manualUpper.begin(), manualUpper.end(), constants.manualUpper.begin());
    Bind(encoder, m_resetPipeline, constants);
    if (IsValid() && encoder.IsValid())
        encoder.Dispatch(1, 1, 1);
}

void ParticleGpuBounds::RecordReduce(const rhi::ComputeCommandEncoder &encoder) const
{
    GpuParticleBoundsConstants constants;
    constants.capacity = m_capacity;
    Bind(encoder, m_reducePipeline, constants);
    if (IsValid() && encoder.IsValid())
        encoder.DispatchIndirect(m_dispatchArguments);
}

void ParticleGpuBounds::Bind(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                             const GpuParticleBoundsConstants &constants) const
{
    if (!IsValid() || !encoder.IsValid() || !pipeline.IsValid())
        return;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, m_group);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
}

} // namespace infernux::particle
