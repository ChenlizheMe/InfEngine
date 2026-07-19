#include <function/resources/InxMaterial/InxMaterial.h>

#include <cassert>
#include <iostream>
#include <memory>
#include <string>

namespace
{

using infernux::InxMaterial;
using infernux::ShaderAssetReference;

void VerifyRemovedFieldRejection()
{
    InxMaterial material("Current", "lit");
    auto invalid = material.SerializeDocument();
    invalid["material_version"] = 4;

    const auto before = material.SerializeDocument();
    assert(!material.DeserializeDocument(invalid));
    assert(material.SerializeDocument() == before);
}

void VerifyStableReferencesAndClone()
{
    InxMaterial material("Stable", "unlit");
    const ShaderAssetReference vertex{"vertex-guid", "standard", "Assets/Shaders/Standard.vert"};
    const ShaderAssetReference fragment{"fragment-guid", "unlit", "Assets/Shaders/Unlit.frag"};
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
    InxMaterial material("Stable", "unlit");
    material.SetVertShaderReference({"vertex-guid", "standard", "Assets/Shaders/Standard.vert"});
    material.SetFragShaderReference({"fragment-guid", "unlit", "Assets/Shaders/Unlit.frag"});
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
    InxMaterial material("LiveState", "unlit");
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
    InxMaterial material("LiveShader", "unlit");
    const uint64_t initialVersion = material.GetVersion();
    material.SetVertShader("standard");
    assert(material.GetVersion() == initialVersion + 1);
    material.SetVertShader("standard");
    assert(material.GetVersion() == initialVersion + 1);

    const ShaderAssetReference reference{"vertex-guid", "standard", "Assets/Shaders/Standard.vert"};
    material.SetVertShaderReference(reference);
    assert(material.GetVersion() == initialVersion + 2);
    material.SetVertShaderReference(reference);
    assert(material.GetVersion() == initialVersion + 2);

    material.SetShader("unlit");
    assert(material.GetVersion() == initialVersion + 3);
    assert((material.GetVertShaderReference() == ShaderAssetReference{"", "unlit", ""}));
    assert((material.GetFragShaderReference() == ShaderAssetReference{"", "unlit", ""}));
}

} // namespace

int main()
{
    VerifyRemovedFieldRejection();
    VerifyStableReferencesAndClone();
    VerifyTransactionalFailure();
    VerifyRenderStateVersioning();
    VerifyShaderReferenceVersioning();
    std::cout << "Material document tests passed\n";
    return 0;
}
