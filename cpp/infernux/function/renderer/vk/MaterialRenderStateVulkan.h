#pragma once

#include <function/resources/InxMaterial/InxMaterial.h>
#include <vulkan/vulkan.h>

namespace infernux::vk
{

[[nodiscard]] constexpr VkCullModeFlags ToVkCullMode(MaterialCullMode value) noexcept
{
    return static_cast<VkCullModeFlags>(value);
}

[[nodiscard]] constexpr VkFrontFace ToVkFrontFace(MaterialFrontFace value) noexcept
{
    return static_cast<VkFrontFace>(value);
}

[[nodiscard]] constexpr VkPolygonMode ToVkPolygonMode(MaterialPolygonMode value) noexcept
{
    return static_cast<VkPolygonMode>(value);
}

[[nodiscard]] constexpr VkPrimitiveTopology ToVkPrimitiveTopology(MaterialPrimitiveTopology value) noexcept
{
    return static_cast<VkPrimitiveTopology>(value);
}

[[nodiscard]] constexpr VkCompareOp ToVkCompareOp(MaterialCompareOp value) noexcept
{
    return static_cast<VkCompareOp>(value);
}

[[nodiscard]] constexpr VkStencilOp ToVkStencilOp(MaterialStencilOp value) noexcept
{
    return static_cast<VkStencilOp>(value);
}

[[nodiscard]] constexpr VkStencilOpState ToVkStencilOpState(const MaterialStencilOpState &value) noexcept
{
    return {ToVkStencilOp(value.failOp),
            ToVkStencilOp(value.passOp),
            ToVkStencilOp(value.depthFailOp),
            ToVkCompareOp(value.compareOp),
            value.compareMask,
            value.writeMask,
            value.reference};
}

[[nodiscard]] constexpr VkBlendFactor ToVkBlendFactor(MaterialBlendFactor value) noexcept
{
    return static_cast<VkBlendFactor>(value);
}

[[nodiscard]] constexpr VkBlendOp ToVkBlendOp(MaterialBlendOp value) noexcept
{
    return static_cast<VkBlendOp>(value);
}

} // namespace infernux::vk
