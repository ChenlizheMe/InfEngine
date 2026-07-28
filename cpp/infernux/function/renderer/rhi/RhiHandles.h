#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>

namespace infernux::rhi
{

using DeviceId = uint16_t;

inline constexpr DeviceId InvalidDeviceId = 0;

/// Returns a process-wide device identity shared by every RHI backend DLL.
/// Identities are not recycled during the process lifetime so stale handles
/// cannot become valid when a backend device is destroyed and recreated.
[[nodiscard]] DeviceId AllocateDeviceId() noexcept;

/// RHI handles remain 64-bit, but their generation is split into an owning
/// device domain and a per-slot generation. This makes an accidental handle
/// hand-off between two adapters fail validation instead of resolving to an
/// unrelated resource at the same slot index.
[[nodiscard]] constexpr uint32_t ComposeHandleGeneration(DeviceId device, uint16_t generation) noexcept
{
    return static_cast<uint32_t>(device) << 16u | generation;
}

template <typename Tag> struct Handle
{
    static constexpr uint32_t InvalidIndex = std::numeric_limits<uint32_t>::max();

    uint32_t index = InvalidIndex;
    uint32_t generation = 0;

    [[nodiscard]] constexpr DeviceId Device() const noexcept
    {
        return static_cast<DeviceId>(generation >> 16u);
    }

    [[nodiscard]] constexpr uint16_t Version() const noexcept
    {
        return static_cast<uint16_t>(generation & 0xffffu);
    }

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return index != InvalidIndex && generation != 0;
    }

    friend constexpr bool operator==(Handle lhs, Handle rhs) noexcept
    {
        return lhs.index == rhs.index && lhs.generation == rhs.generation;
    }

    friend constexpr bool operator!=(Handle lhs, Handle rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

template <typename Tag> struct HandleHash
{
    [[nodiscard]] size_t operator()(Handle<Tag> handle) const noexcept
    {
        const uint64_t value = static_cast<uint64_t>(handle.generation) << 32 | handle.index;
        return std::hash<uint64_t>{}(value);
    }
};

struct BufferTag;
struct TextureTag;
struct TextureViewTag;
struct SamplerTag;
struct ShaderModuleTag;
struct BindingLayoutTag;
struct BindGroupTag;
struct GraphicsPipelineTag;
struct ComputePipelineTag;
struct RenderTargetLayoutTag;

using BufferHandle = Handle<BufferTag>;
using TextureHandle = Handle<TextureTag>;
using TextureViewHandle = Handle<TextureViewTag>;
using SamplerHandle = Handle<SamplerTag>;
using ShaderModuleHandle = Handle<ShaderModuleTag>;
using BindingLayoutHandle = Handle<BindingLayoutTag>;
using BindGroupHandle = Handle<BindGroupTag>;
using GraphicsPipelineHandle = Handle<GraphicsPipelineTag>;
using ComputePipelineHandle = Handle<ComputePipelineTag>;
using RenderTargetLayoutHandle = Handle<RenderTargetLayoutTag>;

static_assert(sizeof(BufferHandle) == 8);
static_assert(sizeof(TextureViewHandle) == 8);
static_assert(sizeof(GraphicsPipelineHandle) == 8);

} // namespace infernux::rhi
