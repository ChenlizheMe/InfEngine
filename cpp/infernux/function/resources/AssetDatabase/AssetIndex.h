#pragma once

#include <core/types/InxFwdType.h>
#include <function/resources/InxResource/InxResourceMeta.h>

#include <cstdint>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{

struct AssetFileFingerprint
{
    uint64_t size = 0;
    int64_t modifiedNs = 0;

    [[nodiscard]] bool operator==(const AssetFileFingerprint &other) const noexcept
    {
        return size == other.size && modifiedNs == other.modifiedNs;
    }
    [[nodiscard]] bool operator!=(const AssetFileFingerprint &other) const noexcept
    {
        return !(*this == other);
    }
};

struct AssetIndexEntry
{
    std::string normalizedPath;
    std::string guid;
    ResourceType resourceType = ResourceType::DefaultText;
    AssetFileFingerprint source;
    AssetFileFingerprint meta;
    std::string contentHash;
    std::vector<std::string> dependencies;
    bool readOnly = false;
    bool importSucceeded = true;
    std::string importError;
    std::string artifactPath;
    InxResourceMeta metadata;
};

class AssetIndex final
{
  public:
    void Reset(std::string normalizedProjectRoot);
    [[nodiscard]] bool Load(const std::string &path, const std::string &normalizedProjectRoot);
    void Save(const std::string &path) const;

    [[nodiscard]] const AssetIndexEntry *Find(const std::string &normalizedPath) const;
    void Upsert(AssetIndexEntry entry);
    [[nodiscard]] size_t Size() const noexcept
    {
        return m_entries.size();
    }

    [[nodiscard]] nlohmann::json SerializeDocument() const;
    void DeserializeDocument(const nlohmann::json &document, const std::string &normalizedProjectRoot);

  private:
    std::string m_projectRoot;
    std::unordered_map<std::string, AssetIndexEntry> m_entries;
};

} // namespace infernux
