#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>

namespace infernux::rhi
{

template <typename Tag> struct Handle
{
    static constexpr uint32_t InvalidIndex = std::numeric_limits<uint32_t>::max();

    uint32_t index = InvalidIndex;
    uint32_t generation = 0;

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
