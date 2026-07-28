#include "ParticleGpuMigrator.h"

#include <algorithm>
#include <limits>

namespace infernux::particle
{

namespace
{

constexpr std::string_view ResetShader = R"glsl(#version 450
layout(local_size_x = 1, local_size_y = 1, local_size_z = 1) in;

layout(std430, set = 0, binding = 0) readonly buffer SourceStates { uint source_states[]; };
layout(std430, set = 0, binding = 1) readonly buffer SourceCounters {
    uint source_free_count;
    uint source_visible_count;
    uint source_dropped_count;
    uint source_reserved_count;
};
layout(std430, set = 0, binding = 2) buffer DestinationStates { uint destination_states[]; };
layout(std430, set = 0, binding = 3) buffer DestinationFreeList { uint destination_free_slots[]; };
layout(std430, set = 0, binding = 4) buffer DestinationCounters {
    uint destination_free_count;
    uint destination_visible_count;
    uint destination_dropped_count;
    uint destination_reserved_count;
};
layout(std430, set = 0, binding = 5) readonly buffer CopyRanges { uvec4 copy_ranges[]; };
layout(std430, set = 0, binding = 6) readonly buffer DefaultState { uint default_state[]; };
layout(push_constant) uniform MigrationConstants {
    uint source_capacity;
    uint destination_capacity;
    uint source_stride_words;
    uint destination_stride_words;
    uint copy_range_count;
    uint invocation_count;
    uint reserved0;
    uint reserved1;
} pc;

void main() {
    destination_free_count = 0u;
    destination_visible_count = 0u;
    destination_dropped_count = source_dropped_count;
    destination_reserved_count = source_reserved_count;
}
)glsl";

constexpr std::string_view MigrateShader = R"glsl(#version 450
layout(local_size_x = 256, local_size_y = 1, local_size_z = 1) in;

layout(std430, set = 0, binding = 0) readonly buffer SourceStates { uint source_states[]; };
layout(std430, set = 0, binding = 1) readonly buffer SourceCounters {
    uint source_free_count;
    uint source_visible_count;
    uint source_dropped_count;
    uint source_reserved_count;
};
layout(std430, set = 0, binding = 2) buffer DestinationStates { uint destination_states[]; };
layout(std430, set = 0, binding = 3) buffer DestinationFreeList { uint destination_free_slots[]; };
layout(std430, set = 0, binding = 4) buffer DestinationCounters {
    uint destination_free_count;
    uint destination_visible_count;
    uint destination_dropped_count;
    uint destination_reserved_count;
};
layout(std430, set = 0, binding = 5) readonly buffer CopyRanges { uvec4 copy_ranges[]; };
layout(std430, set = 0, binding = 6) readonly buffer DefaultState { uint default_state[]; };
layout(push_constant) uniform MigrationConstants {
    uint source_capacity;
    uint destination_capacity;
    uint source_stride_words;
    uint destination_stride_words;
    uint copy_range_count;
    uint invocation_count;
    uint reserved0;
    uint reserved1;
} pc;

void main() {
    uint index = gl_GlobalInvocationID.x;
    if (index >= pc.invocation_count) return;

    bool source_alive = false;
    uint source_base = index * pc.source_stride_words;
    if (index < pc.source_capacity) source_alive = source_states[source_base] != 0u;
    if (index >= pc.destination_capacity) {
        if (source_alive) atomicAdd(destination_dropped_count, 1u);
        return;
    }

    uint destination_base = index * pc.destination_stride_words;
    for (uint word = 0u; word < pc.destination_stride_words; ++word)
        destination_states[destination_base + word] = default_state[word];

    if (source_alive) {
        // Migrated particles have already completed Init. The lifecycle flag is
        // also valid when migrating from the pre-gate alive-word layout.
        destination_states[destination_base] = source_states[source_base] | 2u;
        destination_states[destination_base + 1u] = source_states[source_base + 1u];
        for (uint range_index = 0u; range_index < pc.copy_range_count; ++range_index) {
            uvec4 range = copy_ranges[range_index];
            for (uint word = 0u; word < range.z; ++word)
                destination_states[destination_base + range.y + word] =
                    source_states[source_base + range.x + word];
        }
    } else {
        uint free_index = atomicAdd(destination_free_count, 1u);
        destination_free_slots[free_index] = index;
    }
}
)glsl";

bool IsSpirv(ShaderBytecode shader) noexcept
{
    return shader.words && shader.wordCount >= 5 && shader.words[0] == 0x07230203u;
}

uint32_t GroupCount(uint32_t count) noexcept
{
    return count == 0 ? 0 : 1 + (count - 1) / ParticleGpuMigrator::WorkgroupSize;
}

} // namespace

bool GpuParticleMigrationProgram::IsValid() const noexcept
{
    return IsSpirv(reset) && IsSpirv(migrate);
}

bool GpuParticleMigrationProgramStorage::Assign(const GpuParticleMigrationProgram &program)
{
    if (!program.IsValid())
        return false;
    reset.assign(program.reset.words, program.reset.words + program.reset.wordCount);
    migrate.assign(program.migrate.words, program.migrate.words + program.migrate.wordCount);
    return true;
}

bool GpuParticleMigrationProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleMigrationProgram GpuParticleMigrationProgramStorage::View() const noexcept
{
    return {{reset.data(), reset.size()}, {migrate.data(), migrate.size()}};
}

std::string_view GpuParticleMigrationShaderSources::Reset() noexcept
{
    return ResetShader;
}

std::string_view GpuParticleMigrationShaderSources::Migrate() noexcept
{
    return MigrateShader;
}

ParticleGpuMigrator::~ParticleGpuMigrator()
{
    Destroy();
}

bool ParticleGpuMigrator::Create(rhi::Device &device, const GpuParticleMigrationDesc &desc)
{
    Destroy();
    if (desc.sourceCapacity == 0 || desc.destinationCapacity == 0 || desc.sourceStride == 0 ||
        desc.destinationStride == 0 || desc.sourceStride % sizeof(uint32_t) != 0 ||
        desc.destinationStride % sizeof(uint32_t) != 0 || !desc.sourceStates.IsValid() ||
        !desc.sourceCounters.IsValid() || !desc.destinationStates.IsValid() || !desc.destinationFreeList.IsValid() ||
        !desc.destinationCounters.IsValid() || desc.defaultStateWords.empty() ||
        desc.defaultStateWords.size() != desc.destinationStride / sizeof(uint32_t) || !desc.program.IsValid() ||
        desc.copyRanges.size() > std::numeric_limits<uint32_t>::max())
        return false;

    const uint32_t sourceStrideWords = desc.sourceStride / sizeof(uint32_t);
    const uint32_t destinationStrideWords = desc.destinationStride / sizeof(uint32_t);
    for (const auto &range : desc.copyRanges) {
        if (range.wordCount == 0 || range.sourceOffsetWords < 2 || range.destinationOffsetWords < 2 ||
            range.sourceOffsetWords >= sourceStrideWords || range.destinationOffsetWords >= destinationStrideWords ||
            range.wordCount > sourceStrideWords - range.sourceOffsetWords ||
            range.wordCount > destinationStrideWords - range.destinationOffsetWords)
            return false;
    }

    m_device = &device;
    m_sourceStates = desc.sourceStates;
    m_sourceCounters = desc.sourceCounters;
    m_destinationStates = desc.destinationStates;
    m_destinationFreeList = desc.destinationFreeList;
    m_destinationCounters = desc.destinationCounters;
    m_constants.sourceCapacity = desc.sourceCapacity;
    m_constants.destinationCapacity = desc.destinationCapacity;
    m_constants.sourceStrideWords = sourceStrideWords;
    m_constants.destinationStrideWords = destinationStrideWords;
    m_constants.copyRangeCount = static_cast<uint32_t>(desc.copyRanges.size());
    m_constants.invocationCount = std::max(desc.sourceCapacity, desc.destinationCapacity);

    rhi::BufferDesc upload;
    upload.usage = rhi::BufferUsageFlags::Storage;
    upload.memory = rhi::BufferMemory::Upload;
    upload.byteSize = std::max<size_t>(desc.copyRanges.size(), 1) * sizeof(GpuParticleMigrationRange);
    m_copyRanges = device.CreateBuffer(upload);
    upload.byteSize = desc.defaultStateWords.size() * sizeof(uint32_t);
    m_defaultState = device.CreateBuffer(upload);
    const GpuParticleMigrationRange emptyRange{};
    const void *copyRangeData = desc.copyRanges.empty() ? static_cast<const void *>(&emptyRange)
                                                        : static_cast<const void *>(desc.copyRanges.data());
    const size_t copyRangeBytes = std::max<size_t>(desc.copyRanges.size(), 1) * sizeof(GpuParticleMigrationRange);
    if (!m_copyRanges.IsValid() || !m_defaultState.IsValid() ||
        !device.WriteBuffer(m_copyRanges, 0, copyRangeData, copyRangeBytes) ||
        !device.WriteBuffer(m_defaultState, 0, desc.defaultStateWords.data(),
                            desc.defaultStateWords.size() * sizeof(uint32_t))) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 7; ++binding)
        layoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 7;
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    const std::array<rhi::BufferHandle, 7> buffers = {
        m_sourceStates,        m_sourceCounters, m_destinationStates, m_destinationFreeList,
        m_destinationCounters, m_copyRanges,     m_defaultState,
    };
    for (uint32_t binding = 0; binding < buffers.size(); ++binding) {
        groupDesc.buffers[binding].binding = binding;
        groupDesc.buffers[binding].type = rhi::BindingType::StorageBuffer;
        groupDesc.buffers[binding].buffer = buffers[binding];
    }
    groupDesc.bufferCount = static_cast<uint32_t>(buffers.size());
    m_group = device.CreateBindGroup(groupDesc);
    if (!m_group.IsValid()) {
        Destroy();
        return false;
    }

    const std::array<ShaderBytecode, 2> shaders = {desc.program.reset, desc.program.migrate};
    std::array<rhi::ComputePipelineHandle *, 2> pipelines = {&m_resetPipeline, &m_migratePipeline};
    for (size_t index = 0; index < shaders.size(); ++index) {
        const auto module = device.CreateShaderModule({shaders[index].words, shaders[index].wordCount});
        if (!module.IsValid()) {
            Destroy();
            return false;
        }
        rhi::ComputePipelineDesc pipelineDesc;
        pipelineDesc.computeShader = module;
        pipelineDesc.bindingLayouts[0] = m_layout;
        pipelineDesc.bindingLayoutCount = 1;
        pipelineDesc.pushConstantBytes = sizeof(GpuParticleMigrationConstants);
        *pipelines[index] = device.CreateComputePipeline(pipelineDesc);
        device.Release(module);
        if (!pipelines[index]->IsValid()) {
            Destroy();
            return false;
        }
    }
    return true;
}

void ParticleGpuMigrator::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_migratePipeline);
        m_device->Release(m_resetPipeline);
        m_device->Release(m_group);
        m_device->Release(m_layout);
        m_device->Release(m_defaultState);
        m_device->Release(m_copyRanges);
    }
    m_device = nullptr;
    m_sourceStates = {};
    m_sourceCounters = {};
    m_destinationStates = {};
    m_destinationFreeList = {};
    m_destinationCounters = {};
    m_copyRanges = {};
    m_defaultState = {};
    m_layout = {};
    m_group = {};
    m_resetPipeline = {};
    m_migratePipeline = {};
    m_constants = {};
    m_resetRecorded = false;
    m_recorded = false;
}

bool ParticleGpuMigrator::IsValid() const noexcept
{
    return m_device && m_group.IsValid() && m_resetPipeline.IsValid() && m_migratePipeline.IsValid();
}

void ParticleGpuMigrator::RecordReset(const rhi::ComputeCommandEncoder &encoder)
{
    if (!IsValid() || !encoder.IsValid() || m_recorded)
        return;
    encoder.BindPipeline(m_resetPipeline);
    encoder.BindGroup(m_resetPipeline, 0, m_group);
    encoder.PushConstants(m_resetPipeline, sizeof(m_constants), &m_constants);
    encoder.Dispatch(1, 1, 1);
    m_resetRecorded = true;
}

void ParticleGpuMigrator::RecordMigrate(const rhi::ComputeCommandEncoder &encoder)
{
    if (!IsValid() || !encoder.IsValid() || !m_resetRecorded || m_recorded)
        return;
    encoder.BindPipeline(m_migratePipeline);
    encoder.BindGroup(m_migratePipeline, 0, m_group);
    encoder.PushConstants(m_migratePipeline, sizeof(m_constants), &m_constants);
    encoder.Dispatch(GroupCount(m_constants.invocationCount), 1, 1);
    m_recorded = true;
}

} // namespace infernux::particle
