#pragma once

#include <cstddef>
#include <cstdint>

namespace infernux::rhi
{

enum class PixelFormat : uint8_t
{
    Undefined = 0,
    R8UNorm,
    RG8UNorm,
    RGBA8UNorm,
    RGBA8Srgb,
    BGRA8UNorm,
    BGRA8Srgb,
    R16SFloat,
    RG16SFloat,
    RGBA16SFloat,
    RGBA16UNorm,
    R32SFloat,
    RG32UInt,
    RGBA32SFloat,
    RGB10A2UNorm,
    RGBA4UNormPack16,
    BC1RgbaUNorm,
    BC1RgbaSrgb,
    BC3UNorm,
    BC3Srgb,
    BC4UNorm,
    BC5UNorm,
    BC6HUFloat,
    BC7UNorm,
    BC7Srgb,
    D32SFloat,
    D24UNormS8UInt,
    Count,
};

constexpr size_t kPixelFormatCount = static_cast<size_t>(PixelFormat::Count);

enum class SampleCount : uint8_t
{
    One = 1,
    Two = 2,
    Four = 4,
    Eight = 8,
};

enum class PipelineStage : uint32_t
{
    None = 0,
    Top = 1u << 0,
    DrawIndirect = 1u << 1,
    VertexInput = 1u << 2,
    VertexShader = 1u << 3,
    FragmentShader = 1u << 4,
    EarlyDepth = 1u << 5,
    LateDepth = 1u << 6,
    ColorOutput = 1u << 7,
    ComputeShader = 1u << 8,
    Transfer = 1u << 9,
    Bottom = 1u << 10,
    Host = 1u << 11,
    AllGraphics = 1u << 12,
    AllCommands = 1u << 13,
};

enum class Access : uint32_t
{
    None = 0,
    IndirectRead = 1u << 0,
    IndexRead = 1u << 1,
    VertexRead = 1u << 2,
    UniformRead = 1u << 3,
    ShaderRead = 1u << 4,
    ShaderWrite = 1u << 5,
    ColorRead = 1u << 6,
    ColorWrite = 1u << 7,
    DepthRead = 1u << 8,
    DepthWrite = 1u << 9,
    TransferRead = 1u << 10,
    TransferWrite = 1u << 11,
    HostRead = 1u << 12,
    HostWrite = 1u << 13,
    MemoryRead = 1u << 14,
    MemoryWrite = 1u << 15,
};

enum class TextureLayout : uint8_t
{
    Automatic = 0,
    Undefined,
    General,
    ColorAttachment,
    DepthStencilAttachment,
    DepthStencilReadOnly,
    ShaderReadOnly,
    TransferSource,
    TransferDestination,
    Present,
};

[[nodiscard]] constexpr PipelineStage operator|(PipelineStage lhs, PipelineStage rhs) noexcept
{
    return static_cast<PipelineStage>(static_cast<uint32_t>(lhs) | static_cast<uint32_t>(rhs));
}

[[nodiscard]] constexpr PipelineStage operator&(PipelineStage lhs, PipelineStage rhs) noexcept
{
    return static_cast<PipelineStage>(static_cast<uint32_t>(lhs) & static_cast<uint32_t>(rhs));
}

[[nodiscard]] constexpr Access operator|(Access lhs, Access rhs) noexcept
{
    return static_cast<Access>(static_cast<uint32_t>(lhs) | static_cast<uint32_t>(rhs));
}

[[nodiscard]] constexpr Access operator&(Access lhs, Access rhs) noexcept
{
    return static_cast<Access>(static_cast<uint32_t>(lhs) & static_cast<uint32_t>(rhs));
}

[[nodiscard]] constexpr bool HasAny(PipelineStage value, PipelineStage flags) noexcept
{
    return (value & flags) != PipelineStage::None;
}

[[nodiscard]] constexpr bool HasAny(Access value, Access flags) noexcept
{
    return (value & flags) != Access::None;
}

[[nodiscard]] constexpr bool IsDepthFormat(PixelFormat format) noexcept
{
    return format == PixelFormat::D32SFloat || format == PixelFormat::D24UNormS8UInt;
}

[[nodiscard]] constexpr bool IsValidPixelFormat(PixelFormat format) noexcept
{
    return format > PixelFormat::Undefined && format < PixelFormat::Count;
}

} // namespace infernux::rhi
