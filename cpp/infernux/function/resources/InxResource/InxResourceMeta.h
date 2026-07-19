#pragma once
#include <core/reflection/InxTypeRegistry.h>
#include <core/types/InxFwdType.h>

#include <any>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>

namespace infernux
{
// ----------------------------------
// InxResourceMeta Class
// ----------------------------------

class InxResourceMeta
{
  public:
    // Type definitions
    using MetadataType = std::pair<std::string, std::any>;
    using MetadataMap = std::unordered_map<std::string, MetadataType>;

    InxResourceMeta() = default;
    ~InxResourceMeta() = default;

    void Init(const char *content, size_t contentSize, const std::string &filePath, ResourceType type);

    // Copy constructor and assignment operator
    InxResourceMeta(const InxResourceMeta &other) = default;
    InxResourceMeta &operator=(const InxResourceMeta &other) = default;

    // Move constructor and assignment operator
    InxResourceMeta(InxResourceMeta &&other) noexcept = default;
    InxResourceMeta &operator=(InxResourceMeta &&other) noexcept = default;

    // Metadata operations
    void AddMetadata(const std::string &key, const std::any &value);

    /// @brief Get metadata value with type registry
    /// @tparam T The type to retrieve
    /// @param key type key to retrieve the value for
    /// @return The metadata value of type T
    template <typename T> T GetDataAs(const std::string &key) const;

    // Fixed getters
    const std::string &GetResourceName() const;
    const std::string &GetHashCode() const;
    const std::string &GetGuid() const;
    const MetadataMap &GetMetadata() const;
    const ResourceType &GetResourceType() const;

    /// @brief Check if metadata has a specific key
    bool HasKey(const std::string &key) const;

    /// @brief Update file path (for move/rename operations)
    void UpdateFilePath(const std::string &newFilePath);

    // Serialization methods (JSON only)
    [[nodiscard]] nlohmann::json SerializeDocument() const;
    void DeserializeDocument(const nlohmann::json &document);
    bool SaveToFile(const std::string &metaFilePath) const;
    bool LoadFromFile(const std::string &metaFilePath);

    // Generate metadata file path from resource file path
    static std::string GetMetaFilePath(const std::string &resourceFilePath);
    static std::string NormalizeFilePath(const std::string &filePath);

  private:
    MetadataMap m_metadata;
};

} // namespace infernux

// Include template implementations
#include "InxResourceMeta.inl"
