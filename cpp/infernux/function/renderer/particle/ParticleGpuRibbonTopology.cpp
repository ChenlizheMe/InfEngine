#include "ParticleGpuRibbonTopology.h"

#include <algorithm>
#include <array>
#include <limits>
#include <string>

namespace infernux::particle
{

namespace
{

constexpr std::string_view CommonBindings = R"glsl(
struct ParticleRenderInstance {
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
    vec4 scale_custom;
    uvec4 ribbon_data;
};
layout(std430, set = 0, binding = 0) readonly buffer Instances { ParticleRenderInstance instances[]; };
layout(std430, set = 0, binding = 1) readonly buffer SourceIndices { uint source_indices[]; };
layout(std430, set = 0, binding = 2) readonly buffer SourceIndirectArguments {
    uint source_vertex_count;
    uint source_instance_count;
    uint source_first_vertex;
    uint source_first_instance;
};
layout(std430, set = 0, binding = 3) buffer InputIndices { uint input_indices[]; };
layout(std430, set = 0, binding = 4) buffer OutputIndices { uint output_indices[]; };
layout(std430, set = 0, binding = 5) buffer Histograms { uint histograms[]; };
layout(std430, set = 0, binding = 6) buffer BlockOffsets { uint block_offsets[]; };
layout(std430, set = 0, binding = 7) buffer GlobalOffsets { uint global_offsets[]; };
layout(std430, set = 0, binding = 8) buffer DispatchArguments {
    uint dispatch_group_count_x;
    uint dispatch_group_count_y;
    uint dispatch_group_count_z;
};
layout(std430, set = 0, binding = 9) buffer RibbonIndirectArguments {
    uint ribbon_vertex_count;
    uint ribbon_instance_count;
    uint ribbon_first_vertex;
    uint ribbon_first_instance;
};
layout(push_constant) uniform RibbonConstants {
    uint capacity;
    uint block_count;
    uint key_field;
    uint digit_shift;
} pc;
uint inx_live_count() { return min(source_instance_count, pc.capacity); }
uint inx_key(uint particle_index) {
    uvec4 data = instances[particle_index].ribbon_data;
    return pc.key_field == 0u ? data.x : (pc.key_field == 1u ? data.y : data.w);
}
)glsl";

std::string BuildShader(std::string_view extensions, std::string_view localSize, std::string_view body)
{
    std::string result;
    result.reserve(extensions.size() + localSize.size() + CommonBindings.size() + body.size() + 32);
    result += "#version 450\n";
    result += extensions;
    result += localSize;
    result += CommonBindings;
    result += body;
    return result;
}

bool IsShaderBytecodeValid(const ShaderBytecode &bytecode) noexcept
{
    return bytecode.words && bytecode.wordCount >= 5 && bytecode.words[0] == 0x07230203u;
}

uint32_t DivideRoundUp(uint32_t value, uint32_t divisor) noexcept
{
    return value == 0 ? 0 : 1u + (value - 1u) / divisor;
}

} // namespace

std::string_view GpuParticleRibbonShaderSources::Reset() noexcept
{
    static const std::string Source = BuildShader({}, "layout(local_size_x = 1) in;\n", R"glsl(
void main() {
    uint live_count = inx_live_count();
    dispatch_group_count_x = (live_count + 255u) / 256u;
    dispatch_group_count_y = 1u;
    dispatch_group_count_z = 1u;
    ribbon_vertex_count = live_count > 1u ? (live_count - 1u) * 6u : 0u;
    ribbon_instance_count = 1u;
    ribbon_first_vertex = 0u;
    ribbon_first_instance = 0u;
}
)glsl");
    return Source;
}

std::string_view GpuParticleRibbonShaderSources::Initialize() noexcept
{
    static const std::string Source = BuildShader({}, "layout(local_size_x = 256) in;\n", R"glsl(
void main() {
    uint index = gl_GlobalInvocationID.x;
    if (index < inx_live_count()) input_indices[index] = source_indices[index];
}
)glsl");
    return Source;
}

std::string_view GpuParticleRibbonShaderSources::Histogram() noexcept
{
    static const std::string Source = BuildShader({}, "layout(local_size_x = 256) in;\n", R"glsl(
shared uint local_histogram[16];
void main() {
    uint local_index = gl_LocalInvocationID.x;
    if (local_index < 16u) local_histogram[local_index] = 0u;
    barrier();
    uint index = gl_GlobalInvocationID.x;
    uint live_count = inx_live_count();
    if (index < live_count) {
        uint digit = (inx_key(input_indices[index]) >> pc.digit_shift) & 15u;
        atomicAdd(local_histogram[digit], 1u);
    }
    barrier();
    if (local_index < 16u)
        histograms[gl_WorkGroupID.x * 16u + local_index] = local_histogram[local_index];
}
)glsl");
    return Source;
}

std::string_view GpuParticleRibbonShaderSources::Scan() noexcept
{
    static const std::string Source = BuildShader({}, "layout(local_size_x = 16) in;\n", R"glsl(
shared uint totals[16];
void main() {
    uint bin = gl_LocalInvocationID.x;
    uint running = 0u;
    uint active_block_count = min(dispatch_group_count_x, pc.block_count);
    for (uint block = 0u; block < active_block_count; ++block) {
        uint offset = block * 16u + bin;
        uint count = histograms[offset];
        block_offsets[offset] = running;
        running += count;
    }
    totals[bin] = running;
    barrier();
    uint base = 0u;
    for (uint prior = 0u; prior < bin; ++prior) base += totals[prior];
    global_offsets[bin] = base;
}
)glsl");
    return Source;
}

std::string_view GpuParticleRibbonShaderSources::Scatter() noexcept
{
    static const std::string Source = BuildShader("#extension GL_KHR_shader_subgroup_basic : require\n"
                                                  "#extension GL_KHR_shader_subgroup_ballot : require\n",
                                                  "layout(local_size_x = 256) in;\n", R"glsl(
const uint INX_MAX_SUBGROUPS = 64u;
shared uint subgroup_counts[16 * INX_MAX_SUBGROUPS];
void main() {
    uint local_index = gl_LocalInvocationID.x;
    uint subgroup_count = (256u + gl_SubgroupSize - 1u) / gl_SubgroupSize;
    uint clear_count = min(subgroup_count, INX_MAX_SUBGROUPS) * 16u;
    for (uint index = local_index; index < clear_count; index += 256u)
        subgroup_counts[index] = 0u;
    barrier();

    uint source_index = gl_GlobalInvocationID.x;
    uint live_count = inx_live_count();
    bool lane_active = source_index < live_count && gl_SubgroupID < INX_MAX_SUBGROUPS;
    uint particle_index = lane_active ? input_indices[source_index] : 0u;
    uint digit = lane_active ? ((inx_key(particle_index) >> pc.digit_shift) & 15u) : 0u;
    uvec4 own_mask = uvec4(0u);
    for (uint bin = 0u; bin < 16u; ++bin) {
        uvec4 mask = subgroupBallot(lane_active && digit == bin);
        if (digit == bin) own_mask = mask;
        if (subgroupElect())
            subgroup_counts[gl_SubgroupID * 16u + bin] = subgroupBallotBitCount(mask);
    }
    barrier();
    if (!lane_active) return;

    uint local_rank = subgroupBallotExclusiveBitCount(own_mask);
    for (uint subgroup = 0u; subgroup < gl_SubgroupID; ++subgroup)
        local_rank += subgroup_counts[subgroup * 16u + digit];
    uint block_offset = gl_WorkGroupID.x * 16u + digit;
    uint destination = global_offsets[digit] + block_offsets[block_offset] + local_rank;
    output_indices[destination] = particle_index;
}
)glsl");
    return Source;
}

bool GpuParticleRibbonProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(reset) && IsShaderBytecodeValid(initialize) && IsShaderBytecodeValid(histogram) &&
           IsShaderBytecodeValid(scan) && IsShaderBytecodeValid(scatter);
}

bool GpuParticleRibbonProgramStorage::Assign(const GpuParticleRibbonProgram &program)
{
    if (!program.IsValid())
        return false;
    const std::array<ShaderBytecode, 5> sources = {program.reset, program.initialize, program.histogram, program.scan,
                                                   program.scatter};
    std::array<std::vector<uint32_t>, 5> candidate;
    for (size_t index = 0; index < sources.size(); ++index)
        candidate[index].assign(sources[index].words, sources[index].words + sources[index].wordCount);
    shaders = std::move(candidate);
    return true;
}

bool GpuParticleRibbonProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleRibbonProgram GpuParticleRibbonProgramStorage::View() const noexcept
{
    return {
        {shaders[0].data(), shaders[0].size()}, {shaders[1].data(), shaders[1].size()},
        {shaders[2].data(), shaders[2].size()}, {shaders[3].data(), shaders[3].size()},
        {shaders[4].data(), shaders[4].size()},
    };
}

ParticleGpuRibbonTopology::~ParticleGpuRibbonTopology()
{
    Destroy();
}

bool ParticleGpuRibbonTopology::Create(rhi::Device &device, const GpuParticleRibbonDesc &desc)
{
    Destroy();
    if (desc.capacity == 0 || desc.capacity > std::numeric_limits<uint32_t>::max() / 6u || !desc.instances.IsValid() ||
        !desc.sourceIndices.IsValid() || !desc.sourceIndirectArguments.IsValid() || !desc.program.IsValid())
        return false;

    m_device = &device;
    m_capacity = desc.capacity;
    m_blockCount = DivideRoundUp(desc.capacity, WorkgroupSize);
    m_instances = desc.instances;
    m_sourceIndices = desc.sourceIndices;
    m_sourceIndirectArguments = desc.sourceIndirectArguments;

    const uint64_t elementBytes = static_cast<uint64_t>(m_capacity) * sizeof(uint32_t);
    const uint64_t blockBytes = static_cast<uint64_t>(m_blockCount) * Radix * sizeof(uint32_t);
    const auto storage = rhi::BufferUsageFlags::Storage;
    const auto storageIndirect = storage | rhi::BufferUsageFlags::Indirect;
    for (auto &buffer : m_indices)
        buffer = device.CreateBuffer({elementBytes, storage});
    m_drawIndirectArguments = device.CreateBuffer({16, storageIndirect});
    m_dispatchArguments = device.CreateBuffer({12, storageIndirect});
    m_histograms = device.CreateBuffer({blockBytes, storage});
    m_blockOffsets = device.CreateBuffer({blockBytes, storage});
    m_globalOffsets = device.CreateBuffer({Radix * sizeof(uint32_t), storage});
    if (!std::all_of(m_indices.begin(), m_indices.end(), [](auto value) { return value.IsValid(); }) ||
        !m_drawIndirectArguments.IsValid() || !m_dispatchArguments.IsValid() || !m_histograms.IsValid() ||
        !m_blockOffsets.IsValid() || !m_globalOffsets.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layout;
    for (uint32_t binding = 0; binding < 10; ++binding)
        layout.entries[layout.entryCount++] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    m_layout = device.CreateBindingLayout(layout);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    for (uint32_t pingPong = 0; pingPong < m_groups.size(); ++pingPong) {
        const std::array<rhi::BufferHandle, 10> buffers = {
            m_instances,  m_sourceIndices, m_sourceIndirectArguments, m_indices[pingPong], m_indices[1u - pingPong],
            m_histograms, m_blockOffsets,  m_globalOffsets,           m_dispatchArguments, m_drawIndirectArguments,
        };
        rhi::BindGroupDesc group;
        group.layout = m_layout;
        for (uint32_t binding = 0; binding < buffers.size(); ++binding)
            group.buffers[group.bufferCount++] = {binding, rhi::BindingType::StorageBuffer, buffers[binding], 0, 0};
        m_groups[pingPong] = device.CreateBindGroup(group);
        if (!m_groups[pingPong].IsValid()) {
            Destroy();
            return false;
        }
    }

    const std::array<ShaderBytecode, 5> shaders = {desc.program.reset, desc.program.initialize, desc.program.histogram,
                                                   desc.program.scan, desc.program.scatter};
    std::array<rhi::ComputePipelineHandle *, 5> pipelines = {
        &m_resetPipeline, &m_initializePipeline, &m_histogramPipeline, &m_scanPipeline, &m_scatterPipeline,
    };
    for (size_t index = 0; index < shaders.size(); ++index) {
        const auto module = device.CreateShaderModule({shaders[index].words, shaders[index].wordCount});
        if (!module.IsValid()) {
            Destroy();
            return false;
        }
        rhi::ComputePipelineDesc pipeline;
        pipeline.computeShader = module;
        pipeline.bindingLayouts[0] = m_layout;
        pipeline.bindingLayoutCount = 1;
        pipeline.pushConstantBytes = sizeof(GpuParticleRibbonConstants);
        *pipelines[index] = device.CreateComputePipeline(pipeline);
        device.Release(module);
        if (!pipelines[index]->IsValid()) {
            Destroy();
            return false;
        }
    }
    return true;
}

void ParticleGpuRibbonTopology::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_scatterPipeline);
        m_device->Release(m_scanPipeline);
        m_device->Release(m_histogramPipeline);
        m_device->Release(m_initializePipeline);
        m_device->Release(m_resetPipeline);
        for (auto group : m_groups)
            m_device->Release(group);
        m_device->Release(m_layout);
        m_device->Release(m_globalOffsets);
        m_device->Release(m_blockOffsets);
        m_device->Release(m_histograms);
        m_device->Release(m_dispatchArguments);
        m_device->Release(m_drawIndirectArguments);
        for (auto buffer : m_indices)
            m_device->Release(buffer);
    }
    m_device = nullptr;
    m_capacity = 0;
    m_blockCount = 0;
    m_instances = {};
    m_sourceIndices = {};
    m_sourceIndirectArguments = {};
    m_indices.fill({});
    m_drawIndirectArguments = {};
    m_dispatchArguments = {};
    m_histograms = {};
    m_blockOffsets = {};
    m_globalOffsets = {};
    m_layout = {};
    m_groups.fill({});
    m_resetPipeline = {};
    m_initializePipeline = {};
    m_histogramPipeline = {};
    m_scanPipeline = {};
    m_scatterPipeline = {};
}

bool ParticleGpuRibbonTopology::IsValid() const noexcept
{
    return m_device && m_capacity > 0 && m_blockCount > 0 && m_instances.IsValid() && m_sourceIndices.IsValid() &&
           m_sourceIndirectArguments.IsValid() && m_indices[0].IsValid() && m_indices[1].IsValid() &&
           m_drawIndirectArguments.IsValid() && m_dispatchArguments.IsValid() && m_histograms.IsValid() &&
           m_blockOffsets.IsValid() && m_globalOffsets.IsValid() && m_layout.IsValid() && m_groups[0].IsValid() &&
           m_groups[1].IsValid() && m_resetPipeline.IsValid() && m_initializePipeline.IsValid() &&
           m_histogramPipeline.IsValid() && m_scanPipeline.IsValid() && m_scatterPipeline.IsValid();
}

GpuParticleRibbonConstants ParticleGpuRibbonTopology::Constants(uint32_t passIndex) const noexcept
{
    GpuParticleRibbonConstants constants;
    constants.capacity = m_capacity;
    constants.blockCount = m_blockCount;
    const uint32_t keyPass = std::min(passIndex / PassesPerKey, KeyCount - 1u);
    constants.keyField = (KeyCount - 1u) - keyPass;
    constants.digitShift = (passIndex % PassesPerKey) * 4u;
    return constants;
}

void ParticleGpuRibbonTopology::RecordReset(const rhi::ComputeCommandEncoder &encoder) const
{
    RecordDirect(encoder, m_resetPipeline, m_groups[0], Constants(), 1);
}

void ParticleGpuRibbonTopology::RecordInitialize(const rhi::ComputeCommandEncoder &encoder) const
{
    RecordIndirect(encoder, m_initializePipeline, m_groups[0], Constants());
}

void ParticleGpuRibbonTopology::RecordHistogram(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const
{
    RecordIndirect(encoder, m_histogramPipeline, m_groups[passIndex % 2u], Constants(passIndex));
}

void ParticleGpuRibbonTopology::RecordScan(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const
{
    RecordDirect(encoder, m_scanPipeline, m_groups[passIndex % 2u], Constants(passIndex), 1);
}

void ParticleGpuRibbonTopology::RecordScatter(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const
{
    RecordIndirect(encoder, m_scatterPipeline, m_groups[passIndex % 2u], Constants(passIndex));
}

void ParticleGpuRibbonTopology::RecordDirect(const rhi::ComputeCommandEncoder &encoder,
                                             rhi::ComputePipelineHandle pipeline, rhi::BindGroupHandle group,
                                             const GpuParticleRibbonConstants &constants, uint32_t groups) const
{
    if (!IsValid() || !encoder.IsValid() || !pipeline.IsValid() || !group.IsValid() || groups == 0)
        return;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.Dispatch(groups, 1, 1);
}

void ParticleGpuRibbonTopology::RecordIndirect(const rhi::ComputeCommandEncoder &encoder,
                                               rhi::ComputePipelineHandle pipeline, rhi::BindGroupHandle group,
                                               const GpuParticleRibbonConstants &constants) const
{
    if (!IsValid() || !encoder.IsValid() || !pipeline.IsValid() || !group.IsValid())
        return;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.DispatchIndirect(m_dispatchArguments);
}

} // namespace infernux::particle
