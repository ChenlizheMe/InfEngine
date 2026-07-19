#include "AssetIndex.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <platform/filesystem/DocumentStore.h>
#include <platform/filesystem/InxPath.h>
#include <stdexcept>
#include <unordered_set>

namespace infernux
{

namespace
{

void RequireExactFields(const nlohmann::json &object, std::initializer_list<const char *> fields,
                        const std::string &location)
{
    if (!object.is_object() || object.size() != fields.size())
        throw std::invalid_argument(location + " has an invalid field set");
    for (const char *field : fields) {
        if (!object.contains(field))
            throw std::invalid_argument(location + " is missing field '" + field + "'");
    }
}

ResourceType ParseResourceType(const nlohmann::json &value, const std::string &location)
{
    if (!value.is_number_integer())
        throw std::invalid_argument(location + " must be an integer");
    const int raw = value.get<int>();
    if (raw < static_cast<int>(ResourceType::Shader) || raw > static_cast<int>(ResourceType::PointCache))
        throw std::invalid_argument(location + " is not a current ResourceType");
    return static_cast<ResourceType>(raw);
}

AssetFileFingerprint ParseFingerprint(const nlohmann::json &document, const std::string &location)
{
    RequireExactFields(document, {"size", "modified_ns"}, location);
    if (!document["size"].is_number_unsigned() || !document["modified_ns"].is_number_integer())
        throw std::invalid_argument(location + " has invalid numeric fields");
    return {document["size"].get<uint64_t>(), document["modified_ns"].get<int64_t>()};
}

nlohmann::json SerializeFingerprint(const AssetFileFingerprint &fingerprint)
{
    return {{"size", fingerprint.size}, {"modified_ns", fingerprint.modifiedNs}};
}

} // namespace

void AssetIndex::Reset(std::string normalizedProjectRoot)
{
    m_projectRoot = std::move(normalizedProjectRoot);
    m_entries.clear();
}

bool AssetIndex::Load(const std::string &path, const std::string &normalizedProjectRoot)
{
    if (!std::filesystem::is_regular_file(ToFsPath(path))) {
        Reset(normalizedProjectRoot);
        return false;
    }

    std::ifstream stream(ToFsPath(path));
    if (!stream.is_open())
        throw std::runtime_error("Cannot open AssetIndex: " + path);
    const nlohmann::json document = nlohmann::json::parse(stream);
    if (!document.is_object() || !document.contains("project_root") || !document["project_root"].is_string())
        throw std::invalid_argument("AssetIndex has no valid project_root");
    if (document["project_root"].get<std::string>() != normalizedProjectRoot) {
        Reset(normalizedProjectRoot);
        return false;
    }
    DeserializeDocument(document, normalizedProjectRoot);
    return true;
}

void AssetIndex::Save(const std::string &path) const
{
    const std::filesystem::path filePath = ToFsPath(path);
    if (filePath.has_parent_path())
        std::filesystem::create_directories(filePath.parent_path());
    DocumentStore::Instance().WriteAndWait(path, SerializeDocument().dump(2) + "\n");
}

const AssetIndexEntry *AssetIndex::Find(const std::string &normalizedPath) const
{
    const auto it = m_entries.find(normalizedPath);
    return it != m_entries.end() ? &it->second : nullptr;
}

void AssetIndex::Upsert(AssetIndexEntry entry)
{
    if (entry.normalizedPath.empty() || entry.guid.empty())
        throw std::invalid_argument("AssetIndex entry path and GUID cannot be empty");
    m_entries[entry.normalizedPath] = std::move(entry);
}

nlohmann::json AssetIndex::SerializeDocument() const
{
    std::vector<const AssetIndexEntry *> ordered;
    ordered.reserve(m_entries.size());
    for (const auto &[path, entry] : m_entries) {
        (void)path;
        ordered.push_back(&entry);
    }
    std::sort(ordered.begin(), ordered.end(),
              [](const auto *left, const auto *right) { return left->normalizedPath < right->normalizedPath; });

    nlohmann::json entries = nlohmann::json::array();
    for (const AssetIndexEntry *entry : ordered) {
        auto dependencies = entry->dependencies;
        std::sort(dependencies.begin(), dependencies.end());
        entries.push_back({{"normalized_path", entry->normalizedPath},
                           {"guid", entry->guid},
                           {"resource_type", static_cast<int>(entry->resourceType)},
                           {"source", SerializeFingerprint(entry->source)},
                           {"meta", SerializeFingerprint(entry->meta)},
                           {"importer_version", entry->importerVersion},
                           {"content_hash", entry->contentHash},
                           {"dependencies", std::move(dependencies)},
                           {"read_only", entry->readOnly},
                           {"import_succeeded", entry->importSucceeded},
                           {"import_error", entry->importError},
                           {"artifact_path", entry->artifactPath},
                           {"metadata", entry->metadata.SerializeDocument()}});
    }

    return {{"schema_version", SchemaVersion}, {"project_root", m_projectRoot}, {"entries", std::move(entries)}};
}

void AssetIndex::DeserializeDocument(const nlohmann::json &document, const std::string &normalizedProjectRoot)
{
    RequireExactFields(document, {"schema_version", "project_root", "entries"}, "AssetIndex");
    if (!document["schema_version"].is_number_integer() || document["schema_version"].get<int>() != SchemaVersion)
        throw std::invalid_argument("AssetIndex requires schema_version 1");
    if (!document["project_root"].is_string() || document["project_root"].get<std::string>() != normalizedProjectRoot)
        throw std::invalid_argument("AssetIndex project_root does not match");
    if (!document["entries"].is_array())
        throw std::invalid_argument("AssetIndex entries must be an array");

    AssetIndex staged;
    staged.m_projectRoot = normalizedProjectRoot;
    std::unordered_set<std::string> guids;
    size_t index = 0;
    for (const auto &item : document["entries"]) {
        const std::string location = "AssetIndex.entries[" + std::to_string(index++) + "]";
        RequireExactFields(item,
                           {"normalized_path", "guid", "resource_type", "source", "meta", "importer_version",
                            "content_hash", "dependencies", "read_only", "import_succeeded", "import_error",
                            "artifact_path", "metadata"},
                           location);
        for (const char *field : {"normalized_path", "guid", "content_hash", "import_error", "artifact_path"}) {
            if (!item[field].is_string())
                throw std::invalid_argument(location + "." + field + " must be a string");
        }
        if (!item["importer_version"].is_number_integer() || item["importer_version"].get<int>() < 0 ||
            !item["read_only"].is_boolean() || !item["import_succeeded"].is_boolean() ||
            !item["dependencies"].is_array())
            throw std::invalid_argument(location + " has invalid typed fields");

        AssetIndexEntry entry;
        entry.normalizedPath = item["normalized_path"].get<std::string>();
        entry.guid = item["guid"].get<std::string>();
        if (entry.normalizedPath.empty() || entry.guid.empty())
            throw std::invalid_argument(location + " path and GUID cannot be empty");
        entry.resourceType = ParseResourceType(item["resource_type"], location + ".resource_type");
        entry.source = ParseFingerprint(item["source"], location + ".source");
        entry.meta = ParseFingerprint(item["meta"], location + ".meta");
        entry.importerVersion = item["importer_version"].get<int>();
        entry.contentHash = item["content_hash"].get<std::string>();
        entry.readOnly = item["read_only"].get<bool>();
        entry.importSucceeded = item["import_succeeded"].get<bool>();
        entry.importError = item["import_error"].get<std::string>();
        entry.artifactPath = item["artifact_path"].get<std::string>();
        entry.metadata.DeserializeDocument(item["metadata"]);

        std::unordered_set<std::string> dependencySet;
        for (const auto &dependency : item["dependencies"]) {
            if (!dependency.is_string() || dependency.get<std::string>().empty())
                throw std::invalid_argument(location + ".dependencies contains an invalid GUID");
            if (!dependencySet.insert(dependency.get<std::string>()).second)
                throw std::invalid_argument(location + ".dependencies contains a duplicate GUID");
            entry.dependencies.push_back(dependency.get<std::string>());
        }

        if (!entry.metadata.HasKey("guid") || entry.metadata.GetGuid() != entry.guid ||
            !entry.metadata.HasKey("resource_type") || entry.metadata.GetResourceType() != entry.resourceType)
            throw std::invalid_argument(location + " metadata identity does not match the index entry");
        if (!guids.insert(entry.guid).second)
            throw std::invalid_argument("AssetIndex contains duplicate GUID: " + entry.guid);
        if (!staged.m_entries.emplace(entry.normalizedPath, std::move(entry)).second)
            throw std::invalid_argument("AssetIndex contains duplicate normalized path");
    }

    *this = std::move(staged);
}

} // namespace infernux
