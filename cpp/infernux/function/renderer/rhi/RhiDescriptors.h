#pragma once

#include "RhiHandles.h"
#include "RhiTypes.h"

#include <array>
#include <cstddef>
#include <cstdint>

namespace infernux::rhi
{

enum class ShaderStage : uint8_t
{
    None = 0,
    Vertex = 1u << 0,
    Fragment = 1u << 1,
    Compute = 1u << 2,
};

[[nodiscard]] constexpr ShaderStage operator|(ShaderStage lhs, ShaderStage rhs) noexcept
{
    return static_cast<ShaderStage>(static_cast<uint8_t>(lhs) | static_cast<uint8_t>(rhs));
}

[[nodiscard]] constexpr bool HasShaderStage(ShaderStage available, ShaderStage required) noexcept
{
    return (static_cast<uint8_t>(available) & static_cast<uint8_t>(required)) == static_cast<uint8_t>(required);
}

enum class PrimitiveTopology : uint8_t
{
    TriangleList,
    TriangleStrip,
    LineList,
};

enum class CullMode : uint8_t
{
    None,
    Front,
    Back,
};

enum class FrontFace : uint8_t
{
    Clockwise,
    CounterClockwise,
};

enum class CompareFunction : uint8_t
{
    Never,
    Less,
    Equal,
    LessEqual,
    Greater,
    NotEqual,
    GreaterEqual,
    Always,
};

enum class FilterMode : uint8_t
{
    Nearest,
    Linear,
};

enum class AddressMode : uint8_t
{
    Repeat,
    MirroredRepeat,
    ClampToEdge,
};

enum class BindingType : uint8_t
{
    UniformBuffer,
    StorageBuffer,
    SampledTexture,
    StorageTexture,
    Sampler,
    CombinedTextureSampler,
};

enum class BufferUsageFlags : uint16_t
{
    None = 0,
    Storage = 1u << 0,
    Uniform = 1u << 1,
    Vertex = 1u << 2,
    Index = 1u << 3,
    Indirect = 1u << 4,
    TransferSource = 1u << 5,
    TransferDestination = 1u << 6,
};

[[nodiscard]] constexpr BufferUsageFlags operator|(BufferUsageFlags lhs, BufferUsageFlags rhs) noexcept
{
    return static_cast<BufferUsageFlags>(static_cast<uint16_t>(lhs) | static_cast<uint16_t>(rhs));
}

[[nodiscard]] constexpr bool HasBufferUsage(BufferUsageFlags available, BufferUsageFlags required) noexcept
{
    return (static_cast<uint16_t>(available) & static_cast<uint16_t>(required)) == static_cast<uint16_t>(required);
}

enum class BufferMemory : uint8_t
{
    DeviceLocal,
    Upload,
    Readback,
};

struct BufferDesc
{
    uint64_t byteSize = 0;
    BufferUsageFlags usage = BufferUsageFlags::None;
    BufferMemory memory = BufferMemory::DeviceLocal;
    const void *initialData = nullptr;
    uint64_t initialDataBytes = 0;
};

struct ShaderModuleDesc
{
    const uint32_t *spirv = nullptr;
    size_t wordCount = 0;
};

struct SamplerDesc
{
    FilterMode minFilter = FilterMode::Linear;
    FilterMode magFilter = FilterMode::Linear;
    FilterMode mipFilter = FilterMode::Linear;
    AddressMode addressU = AddressMode::Repeat;
    AddressMode addressV = AddressMode::Repeat;
    AddressMode addressW = AddressMode::Repeat;
    float minLod = 0.0f;
    float maxLod = 0.0f;
    float maxAnisotropy = 1.0f;
};

struct BindingLayoutEntry
{
    uint32_t binding = 0;
    BindingType type = BindingType::UniformBuffer;
    ShaderStage visibility = ShaderStage::None;
    uint32_t count = 1;
};

struct TextureBinding
{
    uint32_t binding = 0;
    TextureViewHandle texture;
    SamplerHandle sampler;
    bool depthRead = false;
};

struct BufferBinding
{
    uint32_t binding = 0;
    BindingType type = BindingType::StorageBuffer;
    BufferHandle buffer;
    uint64_t offset = 0;
    uint64_t byteSize = 0;
};

struct BindingLayoutDesc
{
    static constexpr size_t MaxEntries = 16;

    std::array<BindingLayoutEntry, MaxEntries> entries{};
    uint32_t entryCount = 0;
};

struct BindGroupDesc
{
    static constexpr size_t MaxBufferBindings = 16;

    BindingLayoutHandle layout;
    std::array<BufferBinding, MaxBufferBindings> buffers{};
    uint32_t bufferCount = 0;
};

struct RasterState
{
    CullMode cullMode = CullMode::Back;
    FrontFace frontFace = FrontFace::CounterClockwise;
    bool wireframe = false;
};

struct DepthState
{
    bool testEnabled = false;
    bool writeEnabled = false;
    CompareFunction compare = CompareFunction::LessEqual;
};

struct ColorTargetState
{
    PixelFormat format = PixelFormat::Undefined;
    bool blendEnabled = false;
    uint8_t writeMask = 0x0f;
};

struct GraphicsPipelineDesc
{
    static constexpr size_t MaxColorTargets = 8;
    static constexpr size_t MaxBindingLayouts = 8;

    ShaderModuleHandle vertexShader;
    ShaderModuleHandle fragmentShader;
    RenderTargetLayoutHandle renderTargetLayout;
    PrimitiveTopology topology = PrimitiveTopology::TriangleList;
    RasterState raster;
    DepthState depth;
    SampleCount samples = SampleCount::One;
    std::array<ColorTargetState, MaxColorTargets> colorTargets{};
    uint32_t colorTargetCount = 0;
    std::array<BindingLayoutHandle, MaxBindingLayouts> bindingLayouts{};
    uint32_t bindingLayoutCount = 0;
    ShaderStage pushConstantStages = ShaderStage::None;
    uint32_t pushConstantBytes = 0;
};

struct ComputePipelineDesc
{
    static constexpr size_t MaxBindingLayouts = 8;

    ShaderModuleHandle computeShader;
    std::array<BindingLayoutHandle, MaxBindingLayouts> bindingLayouts{};
    uint32_t bindingLayoutCount = 0;
    uint32_t pushConstantBytes = 0;
};

} // namespace infernux::rhi
