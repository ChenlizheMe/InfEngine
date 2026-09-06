#pragma once

#include <function/renderer/InxRenderStruct.h>
#include <vulkan/vulkan.h>

#include <array>
#include <cstddef>

namespace infernux::vk
{

[[nodiscard]] inline VkVertexInputBindingDescription GetVertexBindingDescription() noexcept
{
    VkVertexInputBindingDescription description{};
    description.binding = 0;
    description.stride = sizeof(Vertex);
    description.inputRate = VK_VERTEX_INPUT_RATE_VERTEX;
    return description;
}

[[nodiscard]] inline std::array<VkVertexInputAttributeDescription, 7> GetVertexAttributeDescriptions() noexcept
{
    std::array<VkVertexInputAttributeDescription, 7> descriptions{};
    descriptions[0] = {0, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vertex, pos)};
    descriptions[1] = {1, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vertex, normal)};
    descriptions[2] = {2, 0, VK_FORMAT_R32G32B32A32_SFLOAT, offsetof(Vertex, tangent)};
    descriptions[3] = {3, 0, VK_FORMAT_R32G32B32_SFLOAT, offsetof(Vertex, color)};
    descriptions[4] = {4, 0, VK_FORMAT_R32G32_SFLOAT, offsetof(Vertex, texCoord)};
    descriptions[5] = {5, 0, VK_FORMAT_R32G32B32A32_UINT, offsetof(Vertex, boneIndices)};
    descriptions[6] = {6, 0, VK_FORMAT_R32G32B32A32_SFLOAT, offsetof(Vertex, boneWeights)};
    return descriptions;
}

} // namespace infernux::vk
