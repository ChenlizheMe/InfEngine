#include <function/resources/InxMaterial/InxMaterial.h>

#include <cassert>
#include <iostream>
#include <memory>
#include <string>

namespace
{

using infernux::InxMaterial;
using infernux::RenderStateOverride;
using infernux::ShaderAssetReference;

void VerifyRemovedFieldRejection()
{
    InxMaterial material("Current", "Lit");
    auto invalid = material.SerializeDocument();
    invalid["material_version"] = 4;

    const auto before = material.SerializeDocument();
    assert(!material.DeserializeDocument(invalid));
    assert(material.SerializeDocument() == before);
}

void VerifyStableReferencesAndClone()
{
    InxMaterial material("Stable", "Unlit");
    const ShaderAssetReference vertex{"vertex-guid", "Standard", "Assets/Shaders/Standard.vert"};
    const ShaderAssetReference fragment{"fragment-guid", "Unlit", "Assets/Shaders/Unlit.frag"};
    material.SetVertShaderReference(vertex);
    material.SetFragShaderReference(fragment);

    const auto document = material.SerializeDocument();
    InxMaterial restored;
    assert(restored.DeserializeDocument(document));
    assert(restored.GetVertShaderReference() == vertex);
    assert(restored.GetFragShaderReference() == fragment);
    assert(restored.GetShaderId() == "vertex-guid|fragment-guid");

    const std::shared_ptr<InxMaterial> clone = restored.Clone();
    assert(clone);
    assert(clone->GetVertShaderReference() == vertex);
    assert(clone->GetFragShaderReference() == fragment);
}

void VerifyTransactionalFailure()
{
    InxMaterial material("Stable", "Unlit");
    material.SetVertShaderReference({"vertex-guid", "Standard", "Assets/Shaders/Standard.vert"});
    material.SetFragShaderReference({"fragment-guid", "Unlit", "Assets/Shaders/Unlit.frag"});
    const auto before = material.SerializeDocument();

    auto invalid = before;
    invalid["name"] = "PartialMutation";
    invalid["shaders"]["fragment"] = {
        {"guid", ""},
        {"shader_id", ""},
        {"path_hint", "Assets/Shaders/Missing.frag"},
    };
    assert(!material.DeserializeDocument(invalid));
    assert(material.SerializeDocument() == before);

    invalid = before;
    invalid["shaders"]["vertex"]["legacy_path"] = "Assets/Shaders/Legacy.vert";
    assert(!material.DeserializeDocument(invalid));
    assert(material.SerializeDocument() == before);
}

void VerifyRenderStateVersioning()
{
    InxMaterial material("LiveState", "Unlit");
    const uint64_t initialVersion = material.GetVersion();
    material.SetRenderQueue(3042);
    assert(material.GetVersion() == initialVersion + 1);
    material.SetRenderQueue(3042);
    assert(material.GetVersion() == initialVersion + 1);

    auto state = material.GetRenderState();
    state.blendEnable = !state.blendEnable;
    material.SetRenderState(state);
    assert(material.GetVersion() == initialVersion + 2);
    material.SetRenderState(state);
    assert(material.GetVersion() == initialVersion + 2);
}

void VerifyShaderReferenceVersioning()
{
    InxMaterial material("LiveShader", "Unlit");
    const uint64_t initialVersion = material.GetVersion();
    material.SetVertShader("Standard");
    assert(material.GetVersion() == initialVersion + 1);
    material.SetVertShader("Standard");
    assert(material.GetVersion() == initialVersion + 1);

    const ShaderAssetReference reference{"vertex-guid", "Standard", "Assets/Shaders/Standard.vert"};
    material.SetVertShaderReference(reference);
    assert(material.GetVersion() == initialVersion + 2);
    material.SetVertShaderReference(reference);
    assert(material.GetVersion() == initialVersion + 2);

    material.SetShader("Unlit");
    assert(material.GetVersion() == initialVersion + 3);
    assert((material.GetVertShaderReference() == ShaderAssetReference{"", "Unlit", ""}));
    assert((material.GetFragShaderReference() == ShaderAssetReference{"", "Unlit", ""}));
}

void VerifyPropertyRemoval()
{
    InxMaterial material("PropertyRemoval", "Unlit");
    material.SetFloat("blend_enable", 1.0f);
    const uint64_t populatedVersion = material.GetVersion();
    assert(material.HasProperty("blend_enable"));

    assert(material.RemoveProperty("blend_enable"));
    assert(!material.HasProperty("blend_enable"));
    assert(material.GetVersion() == populatedVersion + 1);
    assert(!material.RemoveProperty("blend_enable"));
    assert(material.GetVersion() == populatedVersion + 1);
}

void VerifyShaderDefaultsReplacePreviousShaderState()
{
    InxMaterial material("ShaderDefaults", "Particle Unlit");
    auto particleState = material.GetRenderState();
    particleState.cullMode = VK_CULL_MODE_NONE;
    particleState.depthWriteEnable = false;
    particleState.blendEnable = true;
    particleState.renderQueue = 3000;
    particleState.stencilTestEnable = true;
    particleState.stencilFront.compareOp = VK_COMPARE_OP_ALWAYS;
    particleState.stencilBack.compareOp = VK_COMPARE_OP_ALWAYS;
    material.SetRenderState(particleState);
    material.SetPassTag("particle");

    material.ApplyShaderRenderMeta("", "", "", "", 2000, "", "", "");
    const auto &litState = material.GetRenderState();
    assert(litState.cullMode == VK_CULL_MODE_BACK_BIT);
    assert(litState.depthWriteEnable);
    assert(!litState.blendEnable);
    assert(litState.renderQueue == 2000);
    assert(!litState.stencilTestEnable);
    assert(material.GetPassTag().empty());

    material.ApplyShaderRenderMeta("none", "false", "", "alpha", 3000, "particle", "", "");
    const auto &nextParticleState = material.GetRenderState();
    assert(nextParticleState.cullMode == VK_CULL_MODE_NONE);
    assert(!nextParticleState.depthWriteEnable);
    assert(nextParticleState.blendEnable);
    assert(nextParticleState.renderQueue == 3000);
    assert(material.GetPassTag() == "particle");

    material.ApplyShaderRenderMeta("none", "false", "", "premultiplied", 3000, "particle", "", "");
    const auto &premultipliedState = material.GetRenderState();
    assert(premultipliedState.blendEnable);
    assert(premultipliedState.srcColorBlendFactor == VK_BLEND_FACTOR_ONE);
    assert(premultipliedState.dstColorBlendFactor == VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA);
}

void VerifyMaterialOverridesSurviveShaderDefaults()
{
    InxMaterial material("ShaderOverrides", "Lit");
    auto state = material.GetRenderState();
    state.cullMode = VK_CULL_MODE_FRONT_BIT;
    state.depthWriteEnable = false;
    state.blendEnable = true;
    state.renderQueue = 3456;
    material.SetRenderState(state);
    material.MarkOverride(RenderStateOverride::CullMode);
    material.MarkOverride(RenderStateOverride::DepthWrite);
    material.MarkOverride(RenderStateOverride::BlendEnable);
    material.MarkOverride(RenderStateOverride::RenderQueue);

    material.ApplyShaderRenderMeta("back", "true", "", "off", 2000, "", "", "");
    const auto &preserved = material.GetRenderState();
    assert(preserved.cullMode == VK_CULL_MODE_FRONT_BIT);
    assert(!preserved.depthWriteEnable);
    assert(preserved.blendEnable);
    assert(preserved.renderQueue == 3456);
}

} // namespace

int main()
{
    VerifyRemovedFieldRejection();
    VerifyStableReferencesAndClone();
    VerifyTransactionalFailure();
    VerifyRenderStateVersioning();
    VerifyShaderReferenceVersioning();
    VerifyPropertyRemoval();
    VerifyShaderDefaultsReplacePreviousShaderState();
    VerifyMaterialOverridesSurviveShaderDefaults();
    std::cout << "Material document tests passed\n";
    return 0;
}
