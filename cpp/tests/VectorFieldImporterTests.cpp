#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetImporter/ConcreteImporters.h>
#include <function/resources/InxTexture/TextureArtifact.h>
#include <platform/filesystem/InxPath.h>

#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

namespace
{
template <typename Callback> void RequireInvalid(Callback callback)
{
    bool rejected = false;
    try {
        callback();
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    assert(rejected);
}
} // namespace

int main()
{
    const std::string source = R"({
        "$schema": "infernux.vector_field",
        "dimensions": [2, 1, 1],
        "storage_order": "x_fastest",
        "bake_basis": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "vectors": [[1, 0, 0], [0, 2, 0]]
    })";
    const auto path = std::filesystem::temp_directory_path() / "infernux-vector-field-import.inxvfield";
    {
        std::ofstream output(path, std::ios::binary | std::ios::trunc);
        output.write(source.data(), static_cast<std::streamsize>(source.size()));
        assert(output.good());
    }

    infernux::ImportRequest request;
    request.sourcePath = infernux::FromFsPath(path);
    request.guid = "vector-field-test";
    request.resourceType = infernux::ResourceType::Texture;
    request.metadata.Init(source.data(), source.size(), request.sourcePath, infernux::ResourceType::Texture);

    const infernux::TextureImporter importer;
    const auto artifact = importer.Import(request);
    assert(artifact.metadata.GetDataAs<std::string>("texture_type") == "vector_field");
    assert(!artifact.metadata.GetDataAs<bool>("srgb"));
    assert(artifact.metadata.GetDataAs<std::string>("texture_compression") == "none");
    assert(artifact.metadata.GetDataAs<std::string>("texture_format") == "rgba16_float");
    assert(artifact.metadata.GetDataAs<std::string>("artifact_dimension") == "3d");
    assert(artifact.metadata.GetDataAs<int>("artifact_depth") == 1);
    assert(artifact.runtimeCpuArtifacts.size() == 1);

    const auto restored = infernux::TextureArtifact::Deserialize(
        artifact.runtimeCpuArtifacts.front().bytes, artifact.metadata.GetDataAs<std::string>("content_hash"));
    assert(restored->dimension == infernux::TextureDimension::Texture3D);
    assert(restored->semantic == infernux::TextureSemantic::VectorField);
    assert(restored->format == infernux::TextureFormat::Rgba16Float);
    assert(restored->valueMin[0] == 0.0f && restored->valueMax[0] == 1.0f);
    assert(restored->valueMin[1] == 0.0f && restored->valueMax[1] == 2.0f);

    infernux::AssetDatabase database;
    assert(database.GetResourcesType(".inxvfield") == infernux::ResourceType::Texture);

    auto invalidRequest = request;
    auto invalidPath = path;
    invalidPath.replace_extension(".png");
    invalidRequest.sourcePath = infernux::FromFsPath(invalidPath);
    invalidRequest.metadata.AddMetadata("texture_type", std::string("vector_field"));
    RequireInvalid([&] { (void)importer.Import(invalidRequest); });
    std::filesystem::remove(path);
    std::cout << "Vector field importer tests passed\n";
    return 0;
}
