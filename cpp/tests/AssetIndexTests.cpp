#include "function/resources/AssetDatabase/AssetIndex.h"
#include "platform/filesystem/DocumentStore.h"
#include "platform/filesystem/InxPath.h"

#include <array>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

using infernux::AssetIndex;
using infernux::AssetIndexEntry;
using infernux::DocumentStore;
using infernux::ResourceType;

namespace
{

using Clock = std::chrono::steady_clock;

void Require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}

AssetIndexEntry MakeEntry(size_t index)
{
    AssetIndexEntry entry;
    entry.normalizedPath = "c:/project/assets/item-" + std::to_string(index) + ".txt";
    entry.guid = "guid-" + std::to_string(index);
    entry.resourceType = ResourceType::DefaultText;
    entry.source = {100 + index, static_cast<int64_t>(1000 + index)};
    entry.meta = {200 + index, static_cast<int64_t>(2000 + index)};
    entry.contentHash = "hash-" + std::to_string(index);
    if (index > 1)
        entry.dependencies = {"guid-0", "guid-1"};
    entry.metadata.AddMetadata("guid", entry.guid);
    entry.metadata.AddMetadata("resource_type", entry.resourceType);
    entry.metadata.AddMetadata("content_hash", entry.contentHash);
    return entry;
}

double Milliseconds(Clock::time_point start)
{
    return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}

void TestResourceTypeMetadataRoundTrip()
{
    const std::array<std::pair<ResourceType, const char *>, 12> cases = {{
        {ResourceType::Meta, "Meta"},
        {ResourceType::Shader, "Shader"},
        {ResourceType::Texture, "Texture"},
        {ResourceType::Mesh, "Mesh"},
        {ResourceType::Material, "Material"},
        {ResourceType::Script, "Script"},
        {ResourceType::Audio, "Audio"},
        {ResourceType::DefaultText, "DefaultText"},
        {ResourceType::DefaultBinary, "DefaultBinary"},
        {ResourceType::PhysicMaterial, "PhysicMaterial"},
        {ResourceType::RenderEffect, "RenderEffect"},
        {ResourceType::ParticleGraph, "ParticleGraph"},
    }};

    for (const auto &[type, name] : cases) {
        infernux::InxResourceMeta metadata;
        metadata.AddMetadata("resource_type", type);
        const auto document = metadata.SerializeDocument();
        Require(document["metadata"]["resource_type"]["value"] == name,
                "ResourceType metadata serialized to the wrong name");

        infernux::InxResourceMeta restored;
        restored.DeserializeDocument(document);
        Require(restored.GetResourceType() == type, "ResourceType metadata failed strict round-trip");
    }
}

void TestMetadataFilePathCanonicalization()
{
    const auto tempRoot = std::filesystem::temp_directory_path() / "infernux-meta-long-path-stability";
    std::filesystem::create_directories(tempRoot);
    const auto sourcePath = tempRoot / "Race.scene";
    {
        std::ofstream source(sourcePath, std::ios::binary | std::ios::trunc);
        source << "{}\n";
    }

    std::filesystem::path aliasPath = sourcePath;
#ifdef INX_PLATFORM_WINDOWS
    const std::wstring native = sourcePath.native();
    const DWORD required = GetShortPathNameW(native.c_str(), nullptr, 0);
    Require(required > 0, "Windows failed to query a short path for metadata canonicalization");
    std::wstring shortPath(static_cast<size_t>(required), L'\0');
    const DWORD written = GetShortPathNameW(native.c_str(), shortPath.data(), required);
    Require(written > 0 && written < required, "Windows failed to produce a short path for metadata canonicalization");
    shortPath.resize(static_cast<size_t>(written));
    aliasPath = std::filesystem::path(std::move(shortPath));
#endif

    infernux::InxResourceMeta metadata;
    const std::string content = "{}\n";
    metadata.Init(content.data(), content.size(), infernux::FromFsPath(aliasPath), ResourceType::DefaultText);
    const auto canonical = std::filesystem::weakly_canonical(sourcePath).lexically_normal();
    Require(metadata.GetDataAs<std::string>("file_path") == infernux::FromFsPath(canonical),
            "Resource metadata retained a filesystem alias instead of the canonical long path");
    std::filesystem::remove_all(tempRoot);
}

void TestScaleAndStrictRoundTrip()
{
    constexpr size_t entryCount = 10'000;
    constexpr size_t queryCount = 100'000;
    AssetIndex index;
    index.Reset("c:/project");
    for (size_t i = 0; i < entryCount; ++i)
        index.Upsert(MakeEntry(i));

    auto start = Clock::now();
    const auto document = index.SerializeDocument();
    const double serializeMs = Milliseconds(start);
    Require(document["entries"].size() == entryCount, "AssetIndex serialized entry count mismatch");

    AssetIndex restored;
    start = Clock::now();
    restored.DeserializeDocument(document, "c:/project");
    const double deserializeMs = Milliseconds(start);
    Require(restored.Size() == entryCount, "AssetIndex round-trip entry count mismatch");

    start = Clock::now();
    for (size_t i = 0; i < queryCount; ++i) {
        const size_t key = (i * 7919) % entryCount;
        const auto *entry = restored.Find("c:/project/assets/item-" + std::to_string(key) + ".txt");
        Require(entry != nullptr && entry->guid == "guid-" + std::to_string(key), "AssetIndex query mismatch");
    }
    const double queryMs = Milliseconds(start);

    const auto tempRoot = std::filesystem::temp_directory_path() / "infernux-asset-index-tests";
    std::filesystem::create_directories(tempRoot);
    const auto indexPath = tempRoot / "AssetIndex.json";
    start = Clock::now();
    restored.Save(infernux::FromFsPath(indexPath));
    AssetIndex loaded;
    Require(loaded.Load(infernux::FromFsPath(indexPath), "c:/project"), "AssetIndex file load returned a miss");
    const double fileRoundTripMs = Milliseconds(start);
    Require(loaded.Size() == entryCount, "AssetIndex file round-trip entry count mismatch");

    auto invalid = document;
    invalid["entries"][0]["legacy_path"] = invalid["entries"][0]["normalized_path"];
    bool rejected = false;
    try {
        loaded.DeserializeDocument(invalid, "c:/project");
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    Require(rejected, "AssetIndex accepted an unknown entry field");
    Require(loaded.Size() == entryCount, "AssetIndex invalid document partially mutated live state");

    Require(serializeMs < 10'000.0, "AssetIndex 10k serialization exceeded 10 seconds");
    Require(deserializeMs < 10'000.0, "AssetIndex 10k deserialization exceeded 10 seconds");
    Require(queryMs < 2'000.0, "AssetIndex 100k queries exceeded 2 seconds");
    Require(fileRoundTripMs < 15'000.0, "AssetIndex 10k file round-trip exceeded 15 seconds");

    std::cout << "AssetIndex profile: serialize_10k_ms=" << serializeMs << " deserialize_10k_ms=" << deserializeMs
              << " query_100k_ms=" << queryMs << " file_round_trip_10k_ms=" << fileRoundTripMs << '\n';
    DocumentStore::Instance().Shutdown();
    std::filesystem::remove_all(tempRoot);
}

} // namespace

int main()
{
    try {
        TestResourceTypeMetadataRoundTrip();
        TestMetadataFilePathCanonicalization();
        TestScaleAndStrictRoundTrip();
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "AssetIndex test failed: " << error.what() << '\n';
        return 1;
    }
}
