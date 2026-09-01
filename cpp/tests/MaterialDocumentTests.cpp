#include <core/types/ShaderProgramArtifact.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <cassert>
#include <iostream>
#include <memory>
#include <string>

namespace
{

using infernux::InxMaterial;
using infernux::MaterialBlendFactor;
using infernux::MaterialCompareOp;
using infernux::MaterialCullMode;
using infernux::RenderStateOverride;
using infernux::ShaderAssetReference;
using infernux::ShaderProgramArtifact;
using infernux::ShaderProgramPropertyBinding;
using infernux::ShaderProgramStageMask;

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
    invalid["shaders"]["vertex"]["unexpected"] = true;
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
    particleState.cullMode = MaterialCullMode::None;
    particleState.depthWriteEnable = false;
    particleState.blendEnable = true;
    particleState.renderQueue = 3000;
    particleState.stencilTestEnable = true;
    particleState.stencilFront.compareOp = MaterialCompareOp::Always;
    particleState.stencilBack.compareOp = MaterialCompareOp::Always;
    material.SetRenderState(particleState);
    material.SetPassTag("particle");

    material.ApplyShaderRenderMeta("", "", "", "", 2000, "", "", "");
    const auto &litState = material.GetRenderState();
    assert(litState.cullMode == MaterialCullMode::Back);
    assert(litState.depthWriteEnable);
    assert(!litState.blendEnable);
    assert(litState.renderQueue == 2000);
    assert(!litState.stencilTestEnable);
    assert(material.GetPassTag().empty());

    material.ApplyShaderRenderMeta("none", "false", "", "alpha", 3000, "particle", "", "");
    const auto &nextParticleState = material.GetRenderState();
    assert(nextParticleState.cullMode == MaterialCullMode::None);
    assert(!nextParticleState.depthWriteEnable);
    assert(nextParticleState.blendEnable);
    assert(nextParticleState.renderQueue == 3000);
    assert(material.GetPassTag() == "particle");

    material.ApplyShaderRenderMeta("none", "false", "", "premultiplied", 3000, "particle", "", "");
    const auto &premultipliedState = material.GetRenderState();
    assert(premultipliedState.blendEnable);
    assert(premultipliedState.srcColorBlendFactor == MaterialBlendFactor::One);
    assert(premultipliedState.dstColorBlendFactor == MaterialBlendFactor::OneMinusSourceAlpha);
}

void VerifyMaterialOverridesSurviveShaderDefaults()
{
    InxMaterial material("ShaderOverrides", "Lit");
    auto state = material.GetRenderState();
    state.cullMode = MaterialCullMode::Front;
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
    assert(preserved.cullMode == MaterialCullMode::Front);
    assert(!preserved.depthWriteEnable);
    assert(preserved.blendEnable);
    assert(preserved.renderQueue == 3456);
}

void VerifyBuiltinSixWaySmokeMaterial()
{
    const auto material = InxMaterial::CreateParticleSixWaySmokeMaterial();
    assert(material);
    assert(material->IsBuiltin());
    assert(material->GetVertShaderName() == "Particle Sprite");
    assert(material->GetFragShaderName() == "Particle Six-Way Smoke");

    const auto document = material->SerializeDocument();
    assert(document.at("builtin").get<bool>());
    assert(document.at("properties").at("positiveAxesMap").at("guid") == "white");
    assert(document.at("properties").at("negativeAxesMap").at("guid") == "black");
    assert(document.at("properties").at("ambientIntensity").at("value") == 0.0f);

    const auto &state = material->GetRenderState();
    assert(state.renderQueue == 3000);
    assert(state.cullMode == MaterialCullMode::None);
    assert(state.depthTestEnable);
    assert(!state.depthWriteEnable);
    assert(state.blendEnable);
    assert(state.srcColorBlendFactor == MaterialBlendFactor::One);
    assert(state.dstColorBlendFactor == MaterialBlendFactor::OneMinusSourceAlpha);
}

void VerifyBackendNeutralRenderStateSchema()
{
    static_assert(static_cast<uint32_t>(MaterialCullMode::None) == 0);
    static_assert(static_cast<uint32_t>(MaterialCullMode::Front) == 1);
    static_assert(static_cast<uint32_t>(MaterialCullMode::Back) == 2);
    static_assert(static_cast<uint32_t>(MaterialCompareOp::Always) == 7);
    static_assert(static_cast<uint32_t>(MaterialBlendFactor::One) == 1);
    static_assert(static_cast<uint32_t>(MaterialBlendFactor::OneMinusSourceAlpha) == 7);

    InxMaterial material("PortableSchema", "Lit");
    auto state = material.GetRenderState();
    state.cullMode = MaterialCullMode::Front;
    state.depthCompareOp = MaterialCompareOp::GreaterOrEqual;
    state.srcColorBlendFactor = MaterialBlendFactor::One;
    state.dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    material.SetRenderState(state);

    const auto document = material.SerializeDocument();
    const auto &renderState = document.at("renderState");
    assert(renderState.at("cullMode") == 1);
    assert(renderState.at("depthCompareOp") == 6);
    assert(renderState.at("srcColorBlendFactor") == 1);
    assert(renderState.at("dstColorBlendFactor") == 7);

    InxMaterial restored;
    assert(restored.DeserializeDocument(document));
    assert(restored.GetRenderState() == state);
}

void VerifySparseMaterialUsesLinkedShaderDefaults()
{
    InxMaterial material("Sparse", "Lit");
    material.SetColor("baseColor", glm::vec4(0.25f, 0.5f, 0.75f, 1.0f));

    ShaderProgramArtifact artifact;
    ShaderProgramPropertyBinding baseColor;
    baseColor.name = "baseColor";
    baseColor.type = "Color";
    baseColor.defaultValue = "[1.0,1.0,1.0,1.0]";
    baseColor.stages = ShaderProgramStageMask::Fragment;
    baseColor.bufferOffset = 0;
    baseColor.byteSize = 16;
    baseColor.byteAlignment = 16;
    artifact.properties.push_back(baseColor);

    ShaderProgramPropertyBinding smoothness;
    smoothness.name = "smoothness";
    smoothness.type = "Float";
    smoothness.defaultValue = "0.5";
    smoothness.stages = ShaderProgramStageMask::Fragment;
    smoothness.bufferOffset = 16;
    smoothness.byteSize = 4;
    smoothness.byteAlignment = 4;
    artifact.properties.push_back(smoothness);

    assert(material.SynchronizeShaderPropertyDefaults(artifact));
    assert(std::get<glm::vec4>(material.GetProperty("baseColor")->value) == glm::vec4(0.25f, 0.5f, 0.75f, 1.0f));
    assert(std::get<float>(material.GetProperty("smoothness")->value) == 0.5f);
    const uint64_t synchronizedVersion = material.GetVersion();
    assert(!material.SynchronizeShaderPropertyDefaults(artifact));
    assert(material.GetVersion() == synchronizedVersion);
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
    VerifyBuiltinSixWaySmokeMaterial();
    VerifyBackendNeutralRenderStateSchema();
    VerifySparseMaterialUsesLinkedShaderDefaults();
    std::cout << "Material document tests passed\n";
    return 0;
}
