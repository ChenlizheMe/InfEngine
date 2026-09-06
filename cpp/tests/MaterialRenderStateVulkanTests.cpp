#include <function/renderer/vk/MaterialRenderStateVulkan.h>

#include <cassert>
#include <iostream>

namespace
{

using namespace infernux;

void VerifyPersistedValuesMatchVulkan()
{
    static_assert(vk::ToVkCullMode(MaterialCullMode::None) == VK_CULL_MODE_NONE);
    static_assert(vk::ToVkCullMode(MaterialCullMode::Front) == VK_CULL_MODE_FRONT_BIT);
    static_assert(vk::ToVkCullMode(MaterialCullMode::Back) == VK_CULL_MODE_BACK_BIT);
    static_assert(vk::ToVkFrontFace(MaterialFrontFace::Clockwise) == VK_FRONT_FACE_CLOCKWISE);
    static_assert(vk::ToVkPolygonMode(MaterialPolygonMode::Line) == VK_POLYGON_MODE_LINE);
    static_assert(vk::ToVkPrimitiveTopology(MaterialPrimitiveTopology::LineStrip) == VK_PRIMITIVE_TOPOLOGY_LINE_STRIP);
    static_assert(vk::ToVkCompareOp(MaterialCompareOp::GreaterOrEqual) == VK_COMPARE_OP_GREATER_OR_EQUAL);
    static_assert(vk::ToVkBlendFactor(MaterialBlendFactor::OneMinusSourceAlpha) == VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA);
    static_assert(vk::ToVkBlendOp(MaterialBlendOp::ReverseSubtract) == VK_BLEND_OP_REVERSE_SUBTRACT);
}

void VerifyStencilConversion()
{
    MaterialStencilOpState state{};
    state.failOp = MaterialStencilOp::Replace;
    state.passOp = MaterialStencilOp::IncrementAndClamp;
    state.depthFailOp = MaterialStencilOp::DecrementAndWrap;
    state.compareOp = MaterialCompareOp::Always;
    state.compareMask = 0x12;
    state.writeMask = 0x34;
    state.reference = 0x56;

    const VkStencilOpState converted = vk::ToVkStencilOpState(state);
    assert(converted.failOp == VK_STENCIL_OP_REPLACE);
    assert(converted.passOp == VK_STENCIL_OP_INCREMENT_AND_CLAMP);
    assert(converted.depthFailOp == VK_STENCIL_OP_DECREMENT_AND_WRAP);
    assert(converted.compareOp == VK_COMPARE_OP_ALWAYS);
    assert(converted.compareMask == 0x12);
    assert(converted.writeMask == 0x34);
    assert(converted.reference == 0x56);
}

} // namespace

int main()
{
    VerifyPersistedValuesMatchVulkan();
    VerifyStencilConversion();
    std::cout << "Material Vulkan render-state conversion tests passed\n";
    return 0;
}
