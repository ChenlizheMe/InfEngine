#include "CanonicalLightGpuBuffer.h"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace infernux::lighting
{

namespace
{

uint64_t BufferCapacity(uint64_t required)
{
    constexpr uint64_t minimum = 256;
    uint64_t capacity = minimum;
    while (capacity < required) {
        if (capacity > std::numeric_limits<uint64_t>::max() / 2u)
            return required;
        capacity *= 2u;
    }
    return capacity;
}

} // namespace

CanonicalLightGpuBuffer::~CanonicalLightGpuBuffer()
{
    Shutdown();
}

bool CanonicalLightGpuBuffer::Initialize(rhi::Device &device, uint32_t framesInFlight)
{
    Shutdown();
    if (framesInFlight == 0)
        return false;
    m_device = &device;
    m_frames.resize(framesInFlight);
    return true;
}

void CanonicalLightGpuBuffer::Shutdown() noexcept
{
    if (m_device) {
        for (auto &frame : m_frames)
            m_device->Release(frame.buffer);
    }
    m_frames.clear();
    m_device = nullptr;
}

bool CanonicalLightGpuBuffer::Update(uint32_t frameIndex, const CanonicalLightSnapshot &snapshot)
{
    if (!m_device || frameIndex >= m_frames.size())
        return false;

    CanonicalLightUpload upload = BuildCanonicalLightUpload(snapshot);
    auto &frame = m_frames[frameIndex];
    if (!frame.buffer.IsValid() || frame.capacityBytes < upload.bytes.size()) {
        const uint64_t capacity = BufferCapacity(upload.bytes.size());
        rhi::BufferDesc desc;
        desc.byteSize = capacity;
        desc.usage = rhi::BufferUsageFlags::Storage | rhi::BufferUsageFlags::TransferSource;
        desc.memory = rhi::BufferMemory::Upload;
        rhi::BufferHandle replacement = m_device->CreateBuffer(desc);
        if (!replacement.IsValid())
            return false;
        m_device->Release(frame.buffer);
        frame.buffer = replacement;
        frame.capacityBytes = capacity;
    }

    if (!m_device->WriteBuffer(frame.buffer, 0, upload.bytes.data(), upload.bytes.size()))
        return false;

    frame.dataBytes = upload.bytes.size();
    frame.directionalCount = static_cast<uint32_t>(snapshot.directionalLights.size());
    frame.localCount = static_cast<uint32_t>(snapshot.localLights.size());
    frame.generation = snapshot.generation;
    return true;
}

const CanonicalLightGpuFrame &CanonicalLightGpuBuffer::Frame(uint32_t frameIndex) const
{
    if (frameIndex >= m_frames.size())
        throw std::out_of_range("canonical light GPU frame index is out of range");
    return m_frames[frameIndex];
}

} // namespace infernux::lighting
