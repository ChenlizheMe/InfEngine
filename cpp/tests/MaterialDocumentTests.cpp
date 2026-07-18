#include <function/resources/InxMaterial/InxMaterial.h>

#include <cassert>
#include <iostream>
#include <memory>
#include <string>

namespace
{

using infernux::InxMaterial;
using infernux::ShaderAssetReference;

void VerifyLegacyMigration()
{
    InxMaterial material("Legacy", "lit");
    auto legacy = material.SerializeDocument();
    legacy["material_version"] = 3;
    legacy["shaders"]["vertex"] = "standard";
    legacy["shaders"]["fragment"] = "lit";

    assert(material.DeserializeDocument(legacy));
    const auto migrated = material.SerializeDocument();
    assert(migrated["material_version"].get<int>() == 4);
    assert(migrated["shaders"]["vertex"]["guid"].get<std::string>().empty());
    assert(migrated["shaders"]["vertex"]["shader_id"].get<std::string>() == "standard");
    assert(migrated["shaders"]["fragment"]["shader_id"].get<std::string>() == "lit");
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

} // namespace

int main()
{
    VerifyLegacyMigration();
    VerifyStableReferencesAndClone();
    VerifyTransactionalFailure();
    std::cout << "Material document tests passed\n";
    return 0;
}
