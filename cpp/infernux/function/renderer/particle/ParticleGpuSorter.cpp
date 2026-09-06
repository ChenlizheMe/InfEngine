#include "ParticleGpuSorter.h"

#include <algorithm>
#include <array>
#include <limits>
#include <string>

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
layout(std430, set = 0, binding = 1) readonly buffer IndirectArguments {
    uint vertex_count;
    uint instance_count;
    uint first_vertex;
    uint first_instance;
} indirect_args;
layout(std430, set = 0, binding = 2) buffer InputKeys { uint input_keys[]; };
layout(std430, set = 0, binding = 3) buffer InputIndices { uint input_indices[]; };
layout(std430, set = 0, binding = 4) buffer OutputKeys { uint output_keys[]; };
layout(std430, set = 0, binding = 5) buffer OutputIndices { uint output_indices[]; };
layout(std430, set = 0, binding = 6) buffer Histograms { uint histograms[]; };
layout(std430, set = 0, binding = 7) buffer BlockOffsets { uint block_offsets[]; };
layout(std430, set = 0, binding = 8) buffer GlobalOffsets { uint global_offsets[]; };
layout(std430, set = 0, binding = 9) readonly buffer SourceIndices { uint source_indices[]; };
layout(std430, set = 0, binding = 10) readonly buffer DispatchArguments {
    uint dispatch_group_count_x;
    uint dispatch_group_count_y;
    uint dispatch_group_count_z;
};
layout(push_constant) uniform SortConstants {
    mat4 view;
    uint capacity;
    uint block_count;
    uint digit_shift;
    uint descending;
} pc;
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

std::string_view GpuParticleSortShaderSources::Small() noexcept
{
    static const std::string Source = BuildShader({}, "layout(local_size_x = 256) in;\n", R"glsl(
const uint INX_SMALL_SORT_CAPACITY = 1024u;
const uint INX_SMALL_SORT_LANES = 256u;
const uint INX_SMALL_SORT_VALUES_PER_LANE = 4u;
shared uvec2 shared_keys[INX_SMALL_SORT_CAPACITY];
shared uint shared_indices[INX_SMALL_SORT_CAPACITY];

uint inx_ordered_float(float value) {
    uint bits = floatBitsToUint(value);
    return (bits & 0x80000000u) != 0u ? ~bits : (bits ^ 0x80000000u);
}

bool inx_greater(uvec2 left_key, uint left_index, uvec2 right_key, uint right_index) {
    return left_key.x > right_key.x ||
           (left_key.x == right_key.x &&
            (left_key.y > right_key.y || (left_key.y == right_key.y && left_index > right_index)));
}

bool inx_less(uvec2 left_key, uint left_index, uvec2 right_key, uint right_index) {
    return inx_greater(right_key, right_index, left_key, left_index);
}

uvec2 inx_particle_sort_key(float view_depth, uint particle_id) {
    uint depth_key = inx_ordered_float(view_depth);
    depth_key = pc.descending != 0u ? ~depth_key : depth_key;
    return uvec2(depth_key, particle_id);
}

void main() {
    uint lane = gl_LocalInvocationID.x;
    uint live_count = min(indirect_args.instance_count, pc.capacity);
    for (uint item = 0u; item < INX_SMALL_SORT_VALUES_PER_LANE; ++item) {
        uint index = lane + item * INX_SMALL_SORT_LANES;
        uint particle_index = index < live_count ? source_indices[index] : 0xffffffffu;
        uvec2 key = uvec2(0xffffffffu, 0xffffffffu);
        if (index < live_count) {
            vec4 view_position = pc.view * vec4(visibility[particle_index].position_radius.xyz, 1.0);
            key = inx_particle_sort_key(view_position.z, particle_index);
        }
        shared_keys[index] = key;
        shared_indices[index] = particle_index;
    }
    barrier();

    // Sort the full power-of-two workspace. Invalid lanes carry the maximum
    // tuple and therefore remain at the tail; every compare/exchange pair is
    // handled by exactly one lane before the barrier.
    for (uint k = 2u; k <= INX_SMALL_SORT_CAPACITY; k <<= 1u) {
        for (uint j = k >> 1u; j > 0u; j >>= 1u) {
            for (uint item = 0u; item < INX_SMALL_SORT_VALUES_PER_LANE; ++item) {
                uint index = lane + item * INX_SMALL_SORT_LANES;
                uint partner = index ^ j;
                if (partner > index) {
                    const bool ascending = (index & k) == 0u;
                    const bool swap = ascending
                                          ? inx_greater(shared_keys[index], shared_indices[index],
                                                        shared_keys[partner], shared_indices[partner])
                                          : inx_less(shared_keys[index], shared_indices[index],
                                                     shared_keys[partner], shared_indices[partner]);
                    if (swap) {
                        uvec2 key = shared_keys[index];
                        uint particle_index = shared_indices[index];
                        shared_keys[index] = shared_keys[partner];
                        shared_indices[index] = shared_indices[partner];
                        shared_keys[partner] = key;
                        shared_indices[partner] = particle_index;
                    }
                }
            }
            barrier();
        }
    }

    for (uint item = 0u; item < INX_SMALL_SORT_VALUES_PER_LANE; ++item) {
        uint index = lane + item * INX_SMALL_SORT_LANES;
        if (index < live_count)
            input_indices[index] = shared_indices[index];
    }
}
)glsl");
    return Source;
}

std::string_view GpuParticleSortShaderSources::Generate() noexcept
{
    static const std::string Source = BuildShader({}, "layout(local_size_x = 256) in;\n", R"glsl(
uint inx_ordered_float(float value) {
    uint bits = floatBitsToUint(value);
    return (bits & 0x80000000u) != 0u ? ~bits : (bits ^ 0x80000000u);
}
uint inx_particle_sort_key(float view_depth, uint particle_id) {
    uint depth_key = inx_ordered_float(view_depth);
    depth_key = pc.descending != 0u ? ~depth_key : depth_key;
    return (depth_key & 0xffffff00u) | (particle_id & 0xffu);
}
void main() {
    uint index = gl_GlobalInvocationID.x;
    uint live_count = min(indirect_args.instance_count, pc.capacity);
    if (index >= live_count) return;
    uint particle_index = source_indices[index];
    vec4 view_position = pc.view * vec4(visibility[particle_index].position_radius.xyz, 1.0);
    input_keys[index] = inx_particle_sort_key(view_position.z, particle_index);
    input_indices[index] = particle_index;
}
)glsl");
    return Source;
}

std::string_view GpuParticleSortShaderSources::Histogram() noexcept
{
    static const std::string Source = BuildShader({}, "layout(local_size_x = 256) in;\n", R"glsl(
shared uint local_histogram[16];
uint inx_key_digit(uint key) {
    return (key >> pc.digit_shift) & 15u;
}
void main() {
    uint local_index = gl_LocalInvocationID.x;
    if (local_index < 16u) local_histogram[local_index] = 0u;
    barrier();
    uint index = gl_GlobalInvocationID.x;
    uint live_count = min(indirect_args.instance_count, pc.capacity);
    if (index < live_count) {
        uint digit = inx_key_digit(input_keys[index]);
        atomicAdd(local_histogram[digit], 1u);
    }
    barrier();
    if (local_index < 16u)
        histograms[gl_WorkGroupID.x * 16u + local_index] = local_histogram[local_index];
}
)glsl");
    return Source;
}

std::string_view GpuParticleSortShaderSources::Scan() noexcept
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

std::string_view GpuParticleSortShaderSources::Scatter() noexcept
{
    static const std::string Source = BuildShader("#extension GL_KHR_shader_subgroup_basic : require\n"
                                                  "#extension GL_KHR_shader_subgroup_ballot : require\n",
                                                  "layout(local_size_x = 256) in;\n", R"glsl(
const uint INX_MAX_SUBGROUPS = 64u;
shared uint subgroup_counts[16 * INX_MAX_SUBGROUPS];
uint inx_key_digit(uint key) {
    return (key >> pc.digit_shift) & 15u;
}
void main() {
    uint local_index = gl_LocalInvocationID.x;
    uint subgroup_count = (256u + gl_SubgroupSize - 1u) / gl_SubgroupSize;
    uint clear_count = min(subgroup_count, INX_MAX_SUBGROUPS) * 16u;
    for (uint index = local_index; index < clear_count; index += 256u)
        subgroup_counts[index] = 0u;
    barrier();

    uint source_index = gl_GlobalInvocationID.x;
    uint live_count = min(indirect_args.instance_count, pc.capacity);
    bool lane_active = source_index < live_count && gl_SubgroupID < INX_MAX_SUBGROUPS;
    uint key = lane_active ? input_keys[source_index] : 0xffffffffu;
    uint digit = inx_key_digit(key);
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
    output_keys[destination] = key;
    output_indices[destination] = input_indices[source_index];
}
)glsl");
    return Source;
}

bool GpuParticleSortProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(small) && IsShaderBytecodeValid(generate) && IsShaderBytecodeValid(histogram) &&
           IsShaderBytecodeValid(scan) && IsShaderBytecodeValid(scatter);
}

bool GpuParticleSortProgramStorage::Assign(const GpuParticleSortProgram &program)
{
    if (!program.IsValid())
        return false;
    const std::array<ShaderBytecode, 5> sources = {program.small, program.generate, program.histogram, program.scan,
                                                   program.scatter};
    std::array<std::vector<uint32_t>, 5> candidate;
    for (size_t index = 0; index < sources.size(); ++index)
        candidate[index].assign(sources[index].words, sources[index].words + sources[index].wordCount);
    shaders = std::move(candidate);
    return true;
}

bool GpuParticleSortProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleSortProgram GpuParticleSortProgramStorage::View() const noexcept
{
    return {
        {shaders[0].data(), shaders[0].size()}, {shaders[1].data(), shaders[1].size()},
        {shaders[2].data(), shaders[2].size()}, {shaders[3].data(), shaders[3].size()},
        {shaders[4].data(), shaders[4].size()},
    };
}

ParticleGpuSorter::~ParticleGpuSorter()
{
    Destroy();
}

bool ParticleGpuSorter::Create(rhi::Device &device, const GpuParticleSorterDesc &desc)
{
    Destroy();
    if (desc.capacity == 0 || !desc.visibility.IsValid() || !desc.indirectArguments.IsValid() ||
        !desc.sourceIndices.IsValid() || !desc.dispatchArguments.IsValid() || !desc.program.IsValid())
        return false;

    const uint64_t elementBytes = static_cast<uint64_t>(desc.capacity) * sizeof(uint32_t);
    const uint64_t keyBytes = static_cast<uint64_t>(desc.capacity) * PackedKeyStride;
    const uint32_t blockCount = DivideRoundUp(desc.capacity, WorkgroupSize);
    if (blockCount == 0 || blockCount > std::numeric_limits<uint32_t>::max() / Radix)
        return false;
    const uint64_t blockBytes = static_cast<uint64_t>(blockCount) * Radix * sizeof(uint32_t);
    const auto storage = rhi::BufferUsageFlags::Storage;
    const auto createSharedStorage = [&](uint64_t bytes) {
        rhi::BufferDesc bufferDesc;
        bufferDesc.byteSize = bytes;
        bufferDesc.usage = storage;
        bufferDesc.queueAccess = rhi::QueueAccessFlags::Graphics | rhi::QueueAccessFlags::Compute;
        return device.CreateBuffer(bufferDesc);
    };

    m_device = &device;
    m_capacity = desc.capacity;
    m_blockCount = blockCount;
    m_visibility = desc.visibility;
    m_indirectArguments = desc.indirectArguments;
    m_sourceIndices = desc.sourceIndices;
    m_dispatchArguments = desc.dispatchArguments;
    for (auto &buffer : m_keys)
        buffer = createSharedStorage(keyBytes);
    for (auto &buffer : m_indices)
        buffer = createSharedStorage(elementBytes);
    m_histograms = createSharedStorage(blockBytes);
    m_blockOffsets = createSharedStorage(blockBytes);
    m_globalOffsets = createSharedStorage(Radix * sizeof(uint32_t));
    if (!std::all_of(m_keys.begin(), m_keys.end(), [](auto value) { return value.IsValid(); }) ||
        !std::all_of(m_indices.begin(), m_indices.end(), [](auto value) { return value.IsValid(); }) ||
        !m_histograms.IsValid() || !m_blockOffsets.IsValid() || !m_globalOffsets.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 11; ++binding)
        layoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 11;
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    for (uint32_t pingPong = 0; pingPong < m_groups.size(); ++pingPong) {
        const uint32_t output = 1u - pingPong;
        const std::array<rhi::BufferHandle, 11> buffers = {
            m_visibility,    m_indirectArguments, m_keys[pingPong],    m_indices[pingPong],
            m_keys[output],  m_indices[output],   m_histograms,        m_blockOffsets,
            m_globalOffsets, m_sourceIndices,     m_dispatchArguments,
        };
        rhi::BindGroupDesc groupDesc;
        groupDesc.layout = m_layout;
        for (uint32_t binding = 0; binding < buffers.size(); ++binding)
            groupDesc.buffers[binding] = {binding, rhi::BindingType::StorageBuffer, buffers[binding], 0, 0};
        groupDesc.bufferCount = static_cast<uint32_t>(buffers.size());
        m_groups[pingPong] = device.CreateBindGroup(groupDesc);
        if (!m_groups[pingPong].IsValid()) {
            Destroy();
            return false;
        }
    }

    const std::array<ShaderBytecode, 5> shaders = {desc.program.small, desc.program.generate, desc.program.histogram,
                                                   desc.program.scan, desc.program.scatter};
    std::array<rhi::ComputePipelineHandle *, 5> pipelines = {&m_smallPipeline, &m_generatePipeline,
                                                             &m_histogramPipeline, &m_scanPipeline, &m_scatterPipeline};
    for (size_t index = 0; index < shaders.size(); ++index) {
        const auto shader =
            device.CreateShaderModule(rhi::ShaderModuleDesc::FromSpirV(shaders[index].words, shaders[index].wordCount));
        if (!shader.IsValid()) {
            Destroy();
            return false;
        }
        rhi::ComputePipelineDesc pipelineDesc;
        pipelineDesc.computeShader = shader;
        pipelineDesc.bindingLayouts[0] = m_layout;
        pipelineDesc.bindingLayoutCount = 1;
        pipelineDesc.pushConstantBytes = sizeof(GpuParticleSortConstants);
        *pipelines[index] = device.CreateComputePipeline(pipelineDesc);
        device.Release(shader);
        if (!pipelines[index]->IsValid()) {
            Destroy();
            return false;
        }
    }
    return true;
}

void ParticleGpuSorter::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_smallPipeline);
        m_device->Release(m_scatterPipeline);
        m_device->Release(m_scanPipeline);
        m_device->Release(m_histogramPipeline);
        m_device->Release(m_generatePipeline);
        for (auto group : m_groups)
            m_device->Release(group);
        m_device->Release(m_layout);
        m_device->Release(m_globalOffsets);
        m_device->Release(m_blockOffsets);
        m_device->Release(m_histograms);
        for (auto buffer : m_indices)
            m_device->Release(buffer);
        for (auto buffer : m_keys)
            m_device->Release(buffer);
    }
    m_device = nullptr;
    m_capacity = 0;
    m_blockCount = 0;
    m_visibility = {};
    m_indirectArguments = {};
    m_sourceIndices = {};
    m_dispatchArguments = {};
    m_keys.fill({});
    m_indices.fill({});
    m_histograms = {};
    m_blockOffsets = {};
    m_globalOffsets = {};
    m_layout = {};
    m_groups.fill({});
    m_smallPipeline = {};
    m_generatePipeline = {};
    m_histogramPipeline = {};
    m_scanPipeline = {};
    m_scatterPipeline = {};
}

bool ParticleGpuSorter::IsValid() const noexcept
{
    return m_device && m_capacity > 0 && m_blockCount > 0 && m_visibility.IsValid() && m_indirectArguments.IsValid() &&
           m_sourceIndices.IsValid() && m_dispatchArguments.IsValid() && m_layout.IsValid() && m_groups[0].IsValid() &&
           m_groups[1].IsValid() && m_generatePipeline.IsValid() && m_histogramPipeline.IsValid() &&
           m_scanPipeline.IsValid() && m_scatterPipeline.IsValid() && m_smallPipeline.IsValid();
}

GpuParticleSortConstants ParticleGpuSorter::Constants(uint32_t passIndex) const noexcept
{
    GpuParticleSortConstants constants;
    constants.capacity = m_capacity;
    constants.blockCount = m_blockCount;
    constants.digitShift = (passIndex % PassCount) * 4u;
    return constants;
}

void ParticleGpuSorter::RecordSmall(const rhi::ComputeCommandEncoder &encoder, const std::array<float, 16> &view,
                                    ParticleSortMode mode) const
{
    auto constants = Constants();
    constants.view = view;
    constants.descending = mode == ParticleSortMode::BackToFront ? 1u : 0u;
    RecordDirect(encoder, m_smallPipeline, m_groups[0], constants, 1);
}

void ParticleGpuSorter::RecordGenerate(const rhi::ComputeCommandEncoder &encoder, const std::array<float, 16> &view,
                                       ParticleSortMode mode) const
{
    auto constants = Constants();
    constants.view = view;
    constants.descending = mode == ParticleSortMode::BackToFront ? 1u : 0u;
    RecordIndirect(encoder, m_generatePipeline, m_groups[0], constants);
}

void ParticleGpuSorter::RecordHistogram(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const
{
    const auto constants = Constants(passIndex);
    RecordIndirect(encoder, m_histogramPipeline, m_groups[passIndex % 2u], constants);
}

void ParticleGpuSorter::RecordScan(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const
{
    const auto constants = Constants(passIndex);
    RecordDirect(encoder, m_scanPipeline, m_groups[passIndex % 2u], constants, 1);
}

void ParticleGpuSorter::RecordScatter(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const
{
    const auto constants = Constants(passIndex);
    RecordIndirect(encoder, m_scatterPipeline, m_groups[passIndex % 2u], constants);
}

void ParticleGpuSorter::RecordDirect(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                                     rhi::BindGroupHandle group, const GpuParticleSortConstants &constants,
                                     uint32_t groups) const
{
    if (!IsValid() || !encoder.IsValid() || !pipeline.IsValid() || !group.IsValid() || groups == 0)
        return;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.Dispatch(groups, 1, 1);
}

void ParticleGpuSorter::RecordIndirect(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                                       rhi::BindGroupHandle group, const GpuParticleSortConstants &constants) const
{
    if (!IsValid() || !encoder.IsValid() || !pipeline.IsValid() || !group.IsValid())
        return;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.DispatchIndirect(m_dispatchArguments);
}

} // namespace infernux::particle
