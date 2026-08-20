from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[2]
    / "cpp"
    / "infernux"
    / "function"
    / "renderer"
    / "vk"
    / "VulkanRhiDevice.cpp"
).read_text(encoding="utf-8")


def test_descriptor_indexing_uses_valid_core_and_extension_contracts():
    assert "descriptor.descriptorIndexing" not in SOURCE
    assert "m_descriptorIndexingEXT.descriptorIndexing" not in SOURCE
    assert "v12.descriptorIndexing == VK_TRUE" in SOURCE
    assert "descriptor.runtimeDescriptorArray == VK_TRUE" in SOURCE
    assert "descriptor.descriptorBindingPartiallyBound == VK_TRUE" in SOURCE
    assert "descriptor.descriptorBindingVariableDescriptorCount == VK_TRUE" in SOURCE
    assert "descriptor.shaderSampledImageArrayNonUniformIndexing == VK_TRUE" in SOURCE
