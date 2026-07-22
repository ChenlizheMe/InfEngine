#pragma once

#include <function/renderer/rhi/RhiDevice.h>
#include <function/scene/LightingData.h>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>

namespace infernux::lighting
{

struct alignas(16) CanonicalLightGpuHeader
{
    glm::uvec4 countsAndGeneration{};
};

static_assert(sizeof(CanonicalLightGpuHeader) == 16);

struct CanonicalLightUpload
{
    CanonicalLightGpuHeader header{};
    std::vector<std::byte> bytes;
};

[[nodiscard]] inline CanonicalLightUpload BuildCanonicalLightUpload(const CanonicalLightSnapshot &snapshot)
{
    CanonicalLightUpload upload;
    upload.header.countsAndGeneration =
        glm::uvec4(static_cast<uint32_t>(snapshot.directionalLights.size()),
                   static_cast<uint32_t>(snapshot.localLights.size()), static_cast<uint32_t>(snapshot.generation),
                   static_cast<uint32_t>(snapshot.generation >> 32u));

    const size_t directionalBytes = snapshot.directionalLights.size() * sizeof(CanonicalLightData);
    const size_t localBytes = snapshot.localLights.size() * sizeof(CanonicalLightData);
    upload.bytes.resize(sizeof(CanonicalLightGpuHeader) + directionalBytes + localBytes);
    std::memcpy(upload.bytes.data(), &upload.header, sizeof(upload.header));
    if (directionalBytes > 0) {
        std::memcpy(upload.bytes.data() + sizeof(upload.header), snapshot.directionalLights.data(), directionalBytes);
    }
    if (localBytes > 0) {
        std::memcpy(upload.bytes.data() + sizeof(upload.header) + directionalBytes, snapshot.localLights.data(),
                    localBytes);
    }
    return upload;
}

struct CanonicalLightGpuFrame
{
    rhi::BufferHandle buffer{};
    uint64_t capacityBytes = 0;
    uint64_t dataBytes = 0;
    uint32_t directionalCount = 0;
    uint32_t localCount = 0;
    uint64_t generation = 0;
};

class CanonicalLightGpuBuffer
{
  public:
    CanonicalLightGpuBuffer() = default;
    ~CanonicalLightGpuBuffer();

    CanonicalLightGpuBuffer(const CanonicalLightGpuBuffer &) = delete;
    CanonicalLightGpuBuffer &operator=(const CanonicalLightGpuBuffer &) = delete;

    [[nodiscard]] bool Initialize(rhi::Device &device, uint32_t framesInFlight);
    void Shutdown() noexcept;

    /// The caller must have waited for this frame slot before Update.
    [[nodiscard]] bool Update(uint32_t frameIndex, const CanonicalLightSnapshot &snapshot);

    [[nodiscard]] const CanonicalLightGpuFrame &Frame(uint32_t frameIndex) const;
    [[nodiscard]] uint32_t FrameCount() const noexcept
    {
        return static_cast<uint32_t>(m_frames.size());
    }

  private:
    rhi::Device *m_device = nullptr;
    std::vector<CanonicalLightGpuFrame> m_frames;
};

} // namespace infernux::lighting
