#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>

#include <cstddef>
#include <cstdint>
#include <string_view>
#include <vector>

namespace infernux::lighting
{

struct ForwardPlusGridConfig
{
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t tileCountX = 0;
    uint32_t tileCountY = 0;
    uint32_t tileCount = 0;
    uint32_t localLightCount = 0;
    uint32_t indexStride = 0;
    uint32_t domainMask = 0;
    uint64_t headerBytes = 0;
    uint64_t indexBytes = 0;

    [[nodiscard]] bool IsValid() const noexcept;
};

[[nodiscard]] ForwardPlusGridConfig BuildForwardPlusGridConfig(uint32_t width, uint32_t height,
                                                               uint32_t localLightCount, uint32_t domainMask);

struct ForwardPlusGridProgram
{
    const uint32_t *words = nullptr;
    size_t wordCount = 0;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct alignas(16) ForwardPlusGridConstants
{
    float viewProjection[16]{};
    float viewportAndProjectionScale[4]{};
    uint32_t gridAndLights[4]{};
    uint32_t domainAndStride[4]{};
};

static_assert(sizeof(ForwardPlusGridConstants) == 112);

struct ForwardPlusGridFrame
{
    rhi::BufferHandle headers;
    rhi::BufferHandle indices;
    rhi::BindGroupHandle bindGroup;
    rhi::BufferHandle canonicalLights;
    ForwardPlusGridConfig config{};
    uint64_t headerCapacityBytes = 0;
    uint64_t indexCapacityBytes = 0;
};

struct ForwardPlusRetiredResources
{
    rhi::BindGroupHandle bindGroup;
    rhi::BufferHandle headers;
    rhi::BufferHandle indices;
};

/// Per-view, frame-isolated tiled light-list builder. It owns no scene state;
/// callers provide the current canonical LightData buffer and camera matrices.
class ForwardPlusLightGrid
{
  public:
    static constexpr uint32_t TileSize = 16;

    ForwardPlusLightGrid() = default;
    ~ForwardPlusLightGrid();

    ForwardPlusLightGrid(const ForwardPlusLightGrid &) = delete;
    ForwardPlusLightGrid &operator=(const ForwardPlusLightGrid &) = delete;

    [[nodiscard]] bool Initialize(rhi::Device &device, uint32_t framesInFlight, const ForwardPlusGridProgram &program);
    void Shutdown() noexcept;

    /// The caller must wait for the selected frame slot before preparing it.
    [[nodiscard]] bool PrepareFrame(uint32_t frameIndex, uint32_t width, uint32_t height, uint32_t localLightCount,
                                    uint32_t domainMask, rhi::BufferHandle canonicalLights);
    void Record(uint32_t frameIndex, const rhi::ComputeCommandEncoder &encoder,
                const ForwardPlusGridConstants &constants) const;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] uint32_t FrameCount() const noexcept;
    [[nodiscard]] const ForwardPlusGridFrame &Frame(uint32_t frameIndex) const;
    [[nodiscard]] std::vector<ForwardPlusRetiredResources> TakeRetiredResources();

    [[nodiscard]] static std::string_view ShaderSource() noexcept;

  private:
    [[nodiscard]] bool RebuildBindGroup(ForwardPlusGridFrame &frame, rhi::BufferHandle canonicalLights);

    rhi::Device *m_device = nullptr;
    rhi::BindingLayoutHandle m_layout;
    rhi::ComputePipelineHandle m_pipeline;
    std::vector<ForwardPlusGridFrame> m_frames;
    std::vector<ForwardPlusRetiredResources> m_retired;
};

} // namespace infernux::lighting
