#include "InxResourceMeta.h"

#include <core/log/InxLog.h>
#include <nlohmann/json.hpp>
#include <platform/filesystem/DocumentStore.h>
#include <platform/filesystem/InxPath.h>

#include <array>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <limits>
#include <mutex>
#include <random>
#include <sstream>

namespace infernux
{

namespace
{
std::string ComputeContentHashHex(const char *content, size_t contentSize)
{
    // Stable FNV-1a 64-bit hash
    const uint64_t fnvOffset = 14695981039346656037ull;
    const uint64_t fnvPrime = 1099511628211ull;
    uint64_t hash = fnvOffset;

    if (content && contentSize > 0) {
        const unsigned char *ptr = reinterpret_cast<const unsigned char *>(content);
        for (size_t i = 0; i < contentSize; ++i) {
            hash ^= static_cast<uint64_t>(ptr[i]);
            hash *= fnvPrime;
        }
    }

    std::stringstream ss;
    ss << std::hex << std::setfill('0') << std::setw(16) << hash;
    return ss.str();
}

std::string GenerateGuid()
{
    static std::mutex mutex;
    static std::mt19937_64 generator = [] {
        std::random_device device;
        std::array<uint32_t, 10> seedData{};
        for (auto &value : seedData)
            value = device();

        const auto timestamp =
            static_cast<uint64_t>(std::chrono::high_resolution_clock::now().time_since_epoch().count());
        seedData[8] ^= static_cast<uint32_t>(timestamp);
        seedData[9] ^= static_cast<uint32_t>(timestamp >> 32u);
        std::seed_seq seed(seedData.begin(), seedData.end());
        return std::mt19937_64(seed);
    }();

    uint64_t hi = 0;
    uint64_t lo = 0;
    {
        std::lock_guard lock(mutex);
        hi = generator();
        lo = generator();
    }

    std::stringstream stream;
    stream << std::hex << std::setfill('0') << std::setw(16) << hi << std::setw(16) << lo;
    return stream.str();
}

std::string NormalizeMetadataFilePath(const std::string &filePath)
{
    if (filePath.empty())
        return {};

    std::error_code error;
    std::filesystem::path normalized = ToFsPath(filePath);
    if (normalized.is_relative()) {
        auto absolute = std::filesystem::absolute(normalized, error);
        if (!error)
            normalized = std::move(absolute);
        error.clear();
    }
    if (std::filesystem::exists(normalized, error)) {
        auto canonical = std::filesystem::weakly_canonical(normalized, error);
        if (!error)
            normalized = std::move(canonical);
        error.clear();
    }

#ifdef INX_PLATFORM_WINDOWS
    const std::wstring native = normalized.native();
    const DWORD required = GetLongPathNameW(native.c_str(), nullptr, 0);
    if (required > 0) {
        std::wstring expanded(static_cast<size_t>(required), L'\0');
        const DWORD written = GetLongPathNameW(native.c_str(), expanded.data(), required);
        if (written > 0 && written < required) {
            expanded.resize(static_cast<size_t>(written));
            normalized = std::filesystem::path(std::move(expanded));
        }
    }
#endif

    return FromFsPath(normalized.lexically_normal());
}
} // namespace

// ----------------------------------
// InxResourceMeta Implementation
// ----------------------------------
void InxResourceMeta::Init(const char *content, size_t contentSize, const std::string &filePath, ResourceType type)
{
    // Store resource path in metadata (always forward-slash UTF-8 for stable lookups)
    AddMetadata("file_path", NormalizeFilePath(filePath));
    // Set resource type
    AddMetadata("resource_type", type);

    // Calculate content hash (for change detection)
    AddMetadata("content_hash", ComputeContentHashHex(content, contentSize));

    // Generate a random GUID (stable once stored in .meta)
    // This remains unchanged across moves/renames because the meta is preserved.
    AddMetadata("guid", GenerateGuid());

    // Get file modification time
    std::string modTimeStr;
    try {
        if (std::filesystem::exists(ToFsPath(filePath))) {
            auto fileTime = std::filesystem::last_write_time(ToFsPath(filePath));
            auto sctp = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
                fileTime - std::filesystem::file_time_type::clock::now() + std::chrono::system_clock::now());
            auto time_t = std::chrono::system_clock::to_time_t(sctp);
            modTimeStr = std::to_string(time_t);
        } else {
            auto now = std::chrono::system_clock::now();
            auto time_t = std::chrono::system_clock::to_time_t(now);
            modTimeStr = std::to_string(time_t);
        }
    } catch (const std::exception &e) {
        INXLOG_WARN("Failed to get file time: ", e.what());
        auto now = std::chrono::system_clock::now();
        auto time_t = std::chrono::system_clock::to_time_t(now);
        modTimeStr = std::to_string(time_t);
    }
    AddMetadata("last_modified", modTimeStr);
}

void InxResourceMeta::AddMetadata(const std::string &key, const std::any &value)
{
    m_metadata[key] = std::make_pair(InxTypeRegistry::GetInstance().GetTypeName(value.type()), value);
}

const std::string &InxResourceMeta::GetResourceName() const
{
    static const std::string empty;
    auto it = m_metadata.find("resource_name");
    if (it != m_metadata.end()) {
        return std::any_cast<const std::string &>(it->second.second);
    }
    return empty;
}

const std::string &InxResourceMeta::GetHashCode() const
{
    static const std::string empty;
    auto it = m_metadata.find("hash");
    if (it != m_metadata.end()) {
        return std::any_cast<const std::string &>(it->second.second);
    }
    return empty;
}

const std::string &InxResourceMeta::GetGuid() const
{
    static const std::string empty;
    auto it = m_metadata.find("guid");
    if (it != m_metadata.end()) {
        return std::any_cast<const std::string &>(it->second.second);
    }
    return empty;
}

bool InxResourceMeta::HasKey(const std::string &key) const
{
    return m_metadata.find(key) != m_metadata.end();
}

void InxResourceMeta::UpdateFilePath(const std::string &newFilePath)
{
    // Update file_path but keep the same GUID
    // This is used for move/rename operations
    AddMetadata("file_path", NormalizeFilePath(newFilePath));

    // Update last_modified time
    try {
        if (std::filesystem::exists(ToFsPath(newFilePath))) {
            auto fileTime = std::filesystem::last_write_time(ToFsPath(newFilePath));
            auto sctp = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
                fileTime - std::filesystem::file_time_type::clock::now() + std::chrono::system_clock::now());
            auto time_t = std::chrono::system_clock::to_time_t(sctp);
            AddMetadata("last_modified", std::to_string(time_t));
        }
    } catch (const std::exception &e) {
        INXLOG_WARN("Failed to update modification time: ", e.what());
    }
}

const InxResourceMeta::MetadataMap &InxResourceMeta::GetMetadata() const
{
    return m_metadata;
}

const ResourceType &InxResourceMeta::GetResourceType() const
{
    static const ResourceType defaultType = ResourceType::DefaultText;
    auto it = m_metadata.find("resource_type");
    if (it != m_metadata.end()) {
        return std::any_cast<const ResourceType &>(it->second.second);
    }
    return defaultType;
}

std::string InxResourceMeta::GetMetaFilePath(const std::string &resourceFilePath)
{
    return resourceFilePath + ".meta";
}

std::string InxResourceMeta::NormalizeFilePath(const std::string &filePath)
{
    return NormalizeMetadataFilePath(filePath);
}

nlohmann::json InxResourceMeta::SerializeDocument() const
{
    nlohmann::json root;
    nlohmann::json entries = nlohmann::json::object();
    for (const auto &[key, metaPair] : m_metadata) {
        const std::string &typeName = metaPair.first;
        const std::any &value = metaPair.second;

        nlohmann::json entry;
        entry["type"] = typeName;

        if (typeName == "string") {
            entry["value"] = std::any_cast<std::string>(value);
        } else if (typeName == "int") {
            entry["value"] = std::any_cast<int>(value);
        } else if (typeName == "bool") {
            entry["value"] = std::any_cast<bool>(value);
        } else if (typeName == "size_t") {
            entry["value"] = std::any_cast<size_t>(value);
        } else if (typeName == "float") {
            const float number = std::any_cast<float>(value);
            if (!std::isfinite(number))
                throw std::invalid_argument("metadata float must be finite: " + key);
            entry["value"] = number;
        } else if (typeName == "enum infernux::ResourceType") {
            entry["value"] = InxTypeRegistry::GetInstance().ToString(typeName, value);
        } else if (typeName == "json_array" || typeName == "json_object") {
            entry["value"] = nlohmann::json::parse(std::any_cast<std::string>(value));
        } else {
            throw std::invalid_argument("unsupported metadata type for '" + key + "': " + typeName);
        }
        entries[key] = std::move(entry);
    }
    root["metadata"] = std::move(entries);
    return root;
}

void InxResourceMeta::DeserializeDocument(const nlohmann::json &document)
{
    if (!document.is_object() || document.size() != 1 || !document.contains("metadata"))
        throw std::invalid_argument("metadata document must contain exactly metadata");
    if (!document["metadata"].is_object())
        throw std::invalid_argument("metadata must be an object");

    InxResourceMeta staged;
    for (const auto &[key, entry] : document["metadata"].items()) {
        if (key.empty() || !entry.is_object() || entry.size() != 2 || !entry.contains("type") ||
            !entry.contains("value") || !entry["type"].is_string())
            throw std::invalid_argument("invalid metadata entry: " + key);

        const std::string typeName = entry["type"].get<std::string>();
        const auto &value = entry["value"];
        if (typeName == "string") {
            if (!value.is_string())
                throw std::invalid_argument("metadata string value expected: " + key);
            staged.AddMetadata(key, value.get<std::string>());
        } else if (typeName == "int") {
            if (!value.is_number_integer())
                throw std::invalid_argument("metadata int value expected: " + key);
            staged.AddMetadata(key, value.get<int>());
        } else if (typeName == "bool") {
            if (!value.is_boolean())
                throw std::invalid_argument("metadata bool value expected: " + key);
            staged.AddMetadata(key, value.get<bool>());
        } else if (typeName == "size_t") {
            if (!value.is_number_unsigned())
                throw std::invalid_argument("metadata unsigned value expected: " + key);
            staged.AddMetadata(key, value.get<size_t>());
        } else if (typeName == "float") {
            if (!value.is_number())
                throw std::invalid_argument("metadata float value expected: " + key);
            const double number = value.get<double>();
            if (!std::isfinite(number) || number < -std::numeric_limits<float>::max() ||
                number > std::numeric_limits<float>::max())
                throw std::invalid_argument("metadata float must be finite: " + key);
            staged.AddMetadata(key, static_cast<float>(number));
        } else if (typeName == "enum infernux::ResourceType") {
            if (!value.is_string())
                throw std::invalid_argument("metadata ResourceType string expected: " + key);
            const std::any converted = InxTypeRegistry::GetInstance().FromString(typeName, value.get<std::string>());
            staged.AddMetadata(key, std::any_cast<ResourceType>(converted));
        } else if (typeName == "json_array") {
            if (!value.is_array())
                throw std::invalid_argument("metadata JSON array expected: " + key);
            staged.m_metadata[key] = std::make_pair(typeName, std::any(value.dump()));
        } else if (typeName == "json_object") {
            if (!value.is_object())
                throw std::invalid_argument("metadata JSON object expected: " + key);
            staged.m_metadata[key] = std::make_pair(typeName, std::any(value.dump()));
        } else {
            throw std::invalid_argument("unsupported metadata type for '" + key + "': " + typeName);
        }
    }
    m_metadata = std::move(staged.m_metadata);
}

bool InxResourceMeta::SaveToFile(const std::string &metaFilePath) const
{
    try {
        DocumentStore::Instance().WriteAndWait(metaFilePath, SerializeDocument().dump(4) + "\n");

        INXLOG_DEBUG("Meta file saved: ", metaFilePath);
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Exception while saving meta file: ", metaFilePath, " - ", e.what());
        return false;
    }
}

bool InxResourceMeta::LoadFromFile(const std::string &metaFilePath)
{
    std::ifstream file(ToFsPath(metaFilePath));
    if (!file.is_open()) {
        INXLOG_DEBUG("Meta file not found: ", metaFilePath);
        return false;
    }

    try {
        const nlohmann::json root = nlohmann::json::parse(file);
        DeserializeDocument(root);
        INXLOG_DEBUG("Meta file loaded: ", metaFilePath);
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Exception while loading meta file: ", metaFilePath, " - ", e.what());
        throw std::runtime_error("Invalid metadata file '" + metaFilePath + "': " + e.what());
    }
}

} // namespace infernux
