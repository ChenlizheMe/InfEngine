#pragma once

#include <vulkan/vulkan.h>

// Infernux patches a generated copy of Dear ImGui's Vulkan backend at CMake
// configure time. Keeping the extension declaration here leaves the upstream
// submodule untouched and makes a clean checkout reproducible.
void ImGui_ImplVulkan_SetTextureLinearColor(VkDescriptorSet descriptorSet, bool linearColor);
