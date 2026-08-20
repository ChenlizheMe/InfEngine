from __future__ import annotations

import argparse
import re
from pathlib import Path


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"ImGui Vulkan patch anchor '{label}' matched {count} times")
    return source.replace(old, new, 1)


def patch_backend(source: str) -> str:
    source = _replace_once(
        source,
        "    VkCommandBuffer             TexCommandBuffer;\n",
        "    VkCommandBuffer             TexCommandBuffer;\n"
        "    ImVector<VkDescriptorSet>   LinearColorTextures;\n",
        "linear texture storage",
    )

    shader_pattern = re.compile(
        r"static uint32_t __glsl_shader_frag_spv\[\] =\n\{\n.*?\n\};",
        re.DOTALL,
    )
    source, count = shader_pattern.subn(
        "static uint32_t __glsl_shader_frag_spv[] =\n"
        "{\n"
        '#include "infernux_imgui_frag.u32"\n'
        "};",
        source,
        count=1,
    )
    if count != 1:
        raise RuntimeError("ImGui Vulkan fragment shader anchor did not match exactly once")

    source = _replace_once(
        source,
        "static inline VkDeviceSize AlignBufferSize(VkDeviceSize size, VkDeviceSize alignment)\n"
        "{\n"
        "    return (size + alignment - 1) & ~(alignment - 1);\n"
        "}\n",
        "static inline VkDeviceSize AlignBufferSize(VkDeviceSize size, VkDeviceSize alignment)\n"
        "{\n"
        "    return (size + alignment - 1) & ~(alignment - 1);\n"
        "}\n\n"
        "static bool ImGui_ImplVulkan_IsTextureLinearColor(ImGui_ImplVulkan_Data* bd, VkDescriptorSet descriptor_set)\n"
        "{\n"
        "    for (int i = 0; i < bd->LinearColorTextures.Size; ++i)\n"
        "        if (bd->LinearColorTextures[i] == descriptor_set)\n"
        "            return true;\n"
        "    return false;\n"
        "}\n",
        "linear texture lookup",
    )

    source = _replace_once(
        source,
        "                last_desc_set = desc_set;\n\n"
        "                // Draw\n",
        "                last_desc_set = desc_set;\n\n"
        "                const int linear_color = ImGui_ImplVulkan_IsTextureLinearColor(bd, desc_set) ? 1 : 0;\n"
        "                vkCmdPushConstants(command_buffer, bd->PipelineLayout, VK_SHADER_STAGE_FRAGMENT_BIT,\n"
        "                                   sizeof(float) * 4, sizeof(linear_color), &linear_color);\n\n"
        "                // Draw\n",
        "draw color-space push constant",
    )

    source = _replace_once(
        source,
        "        VkPushConstantRange push_constants[1] = {};\n"
        "        push_constants[0].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;\n"
        "        push_constants[0].offset = sizeof(float) * 0;\n"
        "        push_constants[0].size = sizeof(float) * 4;\n",
        "        VkPushConstantRange push_constants[2] = {};\n"
        "        push_constants[0].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;\n"
        "        push_constants[0].offset = sizeof(float) * 0;\n"
        "        push_constants[0].size = sizeof(float) * 4;\n"
        "        push_constants[1].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;\n"
        "        push_constants[1].offset = sizeof(float) * 4;\n"
        "        push_constants[1].size = sizeof(int);\n",
        "fragment push constant range",
    )
    source = _replace_once(
        source,
        "        layout_info.pushConstantRangeCount = 1;\n",
        "        layout_info.pushConstantRangeCount = 2;\n",
        "push constant range count",
    )

    source = _replace_once(
        source,
        "void ImGui_ImplVulkan_RemoveTexture(VkDescriptorSet descriptor_set)\n"
        "{\n"
        "    ImGui_ImplVulkan_Data* bd = ImGui_ImplVulkan_GetBackendData();\n",
        "void ImGui_ImplVulkan_SetTextureLinearColor(VkDescriptorSet descriptor_set, bool linear_color)\n"
        "{\n"
        "    ImGui_ImplVulkan_Data* bd = ImGui_ImplVulkan_GetBackendData();\n"
        "    if (bd == nullptr || descriptor_set == VK_NULL_HANDLE)\n"
        "        return;\n\n"
        "    for (int i = 0; i < bd->LinearColorTextures.Size; ++i)\n"
        "    {\n"
        "        if (bd->LinearColorTextures[i] != descriptor_set)\n"
        "            continue;\n"
        "        if (!linear_color)\n"
        "            bd->LinearColorTextures.erase(bd->LinearColorTextures.Data + i);\n"
        "        return;\n"
        "    }\n"
        "    if (linear_color)\n"
        "        bd->LinearColorTextures.push_back(descriptor_set);\n"
        "}\n\n"
        "void ImGui_ImplVulkan_RemoveTexture(VkDescriptorSet descriptor_set)\n"
        "{\n"
        "    ImGui_ImplVulkan_Data* bd = ImGui_ImplVulkan_GetBackendData();\n"
        "    ImGui_ImplVulkan_SetTextureLinearColor(descriptor_set, false);\n",
        "texture color-space API",
    )
    return source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    patched = patch_backend(args.source.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(patched, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
