#pragma once

#include "InxRenderStruct.h"
#include "shader/ShaderReflection.h"
#include "vk/VertexInputVulkan.h"

#include <unordered_set>
#include <vector>

#include <vulkan/vulkan.h>

namespace infernux
{

/// Keep only vertex buffer attributes whose locations are actually read by the vertex shader.
inline std::vector<VkVertexInputAttributeDescription>
FilterVertexAttributesForReflection(const ShaderReflection &vertexReflection)
{
    const auto allAttributes = vk::GetVertexAttributeDescriptions();
    const auto &shaderInputs = vertexReflection.GetInputs();

    if (shaderInputs.empty()) {
        return {allAttributes.begin(), allAttributes.end()};
    }

    std::unordered_set<uint32_t> consumedLocations;
    consumedLocations.reserve(shaderInputs.size());
    for (const auto &input : shaderInputs) {
        consumedLocations.insert(input.location);
    }

    std::vector<VkVertexInputAttributeDescription> filtered;
    filtered.reserve(allAttributes.size());
    for (const auto &attribute : allAttributes) {
        if (consumedLocations.find(attribute.location) != consumedLocations.end()) {
            filtered.push_back(attribute);
        }
    }
    return filtered;
}

} // namespace infernux
