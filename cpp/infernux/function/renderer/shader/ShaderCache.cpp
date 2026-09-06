#include "ShaderCache.h"
#include <core/log/InxLog.h>
#include <fstream>
#include <nlohmann/json.hpp>
#include <platform/filesystem/InxPath.h>

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace infernux
{

// ============================================================================
// ShaderCache Implementation
// ============================================================================

void ShaderCache::Initialize(const std::string &cacheDirectory)
{
    std::lock_guard<std::mutex> lock(m_mutex);

    if (m_initialized) {
        return;
    }

    m_cacheDirectory = cacheDirectory;

    // Create cache directory if it doesn't exist
    fs::path cacheDir = ToFsPath(m_cacheDirectory);
    if (!fs::exists(cacheDir)) {
        fs::create_directories(cacheDir);
        INXLOG_INFO("ShaderCache: Created cache directory: ", m_cacheDirectory);
    }

    // Load existing cache index
    LoadCacheIndex();

    m_initialized = true;
    INXLOG_INFO("ShaderCache: Initialized with ", m_cache.size(), " cached entries");
}

void ShaderCache::Shutdown()
{
    std::lock_guard<std::mutex> lock(m_mutex);

    if (!m_initialized) {
        return;
    }

    SaveCacheIndex();
    m_cache.clear();
    m_initialized = false;

    INXLOG_INFO("ShaderCache: Shutdown complete");
}

bool ShaderCache::HasCached(const ShaderCacheKey &key) const
{
    std::lock_guard<std::mutex> lock(m_mutex);

    size_t hash = key.Hash();
    auto it = m_cache.find(hash);
    if (it == m_cache.end()) {
        return false;
    }

    // Check if source hash matches (source hasn't changed)
    return it->second.valid && it->second.sourceHash == key.sourceHash;
}

CachedSpirv ShaderCache::GetCached(const ShaderCacheKey &key) const
{
    std::lock_guard<std::mutex> lock(m_mutex);

    size_t hash = key.Hash();
    auto it = m_cache.find(hash);
    if (it != m_cache.end() && it->second.valid && it->second.sourceHash == key.sourceHash) {
        return it->second;
    }

    return CachedSpirv{}; // Return invalid entry
}

void ShaderCache::Store(const ShaderCacheKey &key, const std::vector<uint32_t> &spirv)
{
    std::lock_guard<std::mutex> lock(m_mutex);

    size_t hash = key.Hash();

    CachedSpirv cached;
    cached.spirvData = spirv;
    cached.sourceHash = key.sourceHash;
    cached.compileTimestamp =
        std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    cached.valid = true;

    m_cache[hash] = cached;

    // Save to disk immediately
    fs::path cachePath = GetCacheFilePath(key);
    SaveToDisk(cachePath, cached);
}

void ShaderCache::ClearCache()
{
    std::lock_guard<std::mutex> lock(m_mutex);

    m_cache.clear();

    // Delete all files in cache directory
    fs::path cacheDir = ToFsPath(m_cacheDirectory);
    if (fs::exists(cacheDir)) {
        for (const auto &entry : fs::directory_iterator(cacheDir)) {
            if (entry.path().extension() == ".spv" || entry.path().extension() == ".cache") {
                fs::remove(entry.path());
            }
        }
    }

    INXLOG_INFO("ShaderCache: Cache cleared");
}

uint64_t ShaderCache::ComputeSourceHash(const std::string &source)
{
    // FNV-1a hash
    uint64_t hash = 14695981039346656037ULL;
    for (char c : source) {
        hash ^= static_cast<uint64_t>(c);
        hash *= 1099511628211ULL;
    }
    return hash;
}

uint64_t ShaderCache::ComputeSourceHashWithDefines(const std::string &source, const std::vector<std::string> &defines)
{
    std::string combined;
    for (const auto &define : defines) {
        combined += "#define " + define + "\n";
    }
    combined += source;
    return ComputeSourceHash(combined);
}

size_t ShaderCache::GetCacheSize() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_cache.size();
}

void ShaderCache::PrecompileShaders(
    const std::string &shaderDirectory,
    std::function<std::vector<uint32_t>(const std::string &, const std::vector<std::string> &)> compileFunction)
{
    fs::path shaderDir = ToFsPath(shaderDirectory);
    if (!fs::exists(shaderDir)) {
        INXLOG_WARN("ShaderCache::PrecompileShaders: Directory does not exist: ", shaderDirectory);
        return;
    }

    INXLOG_INFO("ShaderCache: Precompiling shaders in ", shaderDirectory);

    std::vector<std::string> shaderExtensions = {".vert", ".frag"};

    for (const auto &entry : fs::recursive_directory_iterator(shaderDir)) {
        if (!entry.is_regular_file()) {
            continue;
        }

        std::string ext = FromFsPath(entry.path().extension());
        bool isShader = false;
        for (const auto &shaderExt : shaderExtensions) {
            if (ext == shaderExt) {
                isShader = true;
                break;
            }
        }

        if (!isShader) {
            continue;
        }

        std::string shaderPath = FromFsPath(entry.path());

        // Read shader source
        std::ifstream file(entry.path());
        if (!file.is_open()) {
            INXLOG_WARN("ShaderCache: Failed to open shader: ", shaderPath);
            continue;
        }

        std::string source((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
        file.close();

        // Create cache key for base variant (no keywords)
        ShaderCacheKey key;
        key.shaderPath = shaderPath;
        key.sourceHash = ComputeSourceHash(source);

        // Check if already cached and up-to-date
        if (HasCached(key)) {
            continue;
        }

        // Compile shader
        try {
            std::vector<uint32_t> spirv = compileFunction(shaderPath, {});
            if (!spirv.empty()) {
                Store(key, spirv);
                INXLOG_INFO("ShaderCache: Precompiled shader: ", shaderPath);
            }
        } catch (const std::exception &e) {
            INXLOG_ERROR("ShaderCache: Failed to precompile shader '", shaderPath, "': ", e.what());
        }
    }

    // Save index after precompilation
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        SaveCacheIndex();
    }

    INXLOG_INFO("ShaderCache: Precompilation complete, ", m_cache.size(), " shaders cached");
}

fs::path ShaderCache::GetCacheFilePath(const ShaderCacheKey &key) const
{
    // Generate a unique filename from the hash
    size_t hash = key.Hash();
    std::ostringstream ss;
    ss << std::hex << hash << ".spv";
    return ToFsPath(m_cacheDirectory) / ss.str();
}

CachedSpirv ShaderCache::LoadFromDisk(const fs::path &path) const
{
    CachedSpirv cached;
    cached.valid = false;

    if (!fs::exists(path)) {
        return cached;
    }

    std::ifstream file(path, std::ios::binary);
    if (!file.is_open()) {
        return cached;
    }

    // Read header
    uint64_t magic, sourceHash, timestamp;
    uint32_t spirvSize;

    file.read(reinterpret_cast<char *>(&magic), sizeof(magic));
    if (magic != 0x5350495256434143ULL) { // "SPIRVCAC" in hex
        return cached;
    }

    file.read(reinterpret_cast<char *>(&sourceHash), sizeof(sourceHash));
    file.read(reinterpret_cast<char *>(&timestamp), sizeof(timestamp));
    file.read(reinterpret_cast<char *>(&spirvSize), sizeof(spirvSize));

    // Read SPIR-V data
    cached.spirvData.resize(spirvSize);
    file.read(reinterpret_cast<char *>(cached.spirvData.data()), spirvSize * sizeof(uint32_t));

    cached.sourceHash = sourceHash;
    cached.compileTimestamp = timestamp;
    cached.valid = true;

    return cached;
}

void ShaderCache::SaveToDisk(const fs::path &path, const CachedSpirv &cached) const
{
    std::ofstream file(path, std::ios::binary);
    if (!file.is_open()) {
        INXLOG_WARN("ShaderCache: Failed to save cache to ", FromFsPath(path));
        return;
    }

    // Write header
    uint64_t magic = 0x5350495256434143ULL; // "SPIRVCAC"
    uint32_t spirvSize = static_cast<uint32_t>(cached.spirvData.size());

    file.write(reinterpret_cast<const char *>(&magic), sizeof(magic));
    file.write(reinterpret_cast<const char *>(&cached.sourceHash), sizeof(cached.sourceHash));
    file.write(reinterpret_cast<const char *>(&cached.compileTimestamp), sizeof(cached.compileTimestamp));
    file.write(reinterpret_cast<const char *>(&spirvSize), sizeof(spirvSize));

    // Write SPIR-V data
    file.write(reinterpret_cast<const char *>(cached.spirvData.data()), spirvSize * sizeof(uint32_t));
}

void ShaderCache::LoadCacheIndex()
{
    fs::path indexPath = ToFsPath(m_cacheDirectory) / "cache_index.json";
    if (!fs::exists(indexPath)) {
        return;
    }

    try {
        std::ifstream file(indexPath);
        if (!file.is_open()) {
            return;
        }

        json j;
        file >> j;

        for (const auto &entry : j["entries"]) {
            size_t keyHash = entry["keyHash"].get<size_t>();
            std::string spvFile = entry["spvFile"].get<std::string>();

            fs::path spvPath = ToFsPath(m_cacheDirectory) / spvFile;
            CachedSpirv cached = LoadFromDisk(spvPath);
            if (cached.valid) {
                m_cache[keyHash] = cached;
            }
        }
    } catch (const std::exception &e) {
        INXLOG_WARN("ShaderCache: Failed to load cache index: ", e.what());
    }
}

void ShaderCache::SaveCacheIndex()
{
    fs::path indexPath = ToFsPath(m_cacheDirectory) / "cache_index.json";

    try {
        json j;
        json entries = json::array();

        for (const auto &[keyHash, cached] : m_cache) {
            if (cached.valid) {
                json entry;
                entry["keyHash"] = keyHash;
                std::ostringstream ss;
                ss << std::hex << keyHash << ".spv";
                entry["spvFile"] = ss.str();
                entry["sourceHash"] = cached.sourceHash;
                entry["timestamp"] = cached.compileTimestamp;
                entries.push_back(entry);
            }
        }

        j["entries"] = entries;

        std::ofstream file(indexPath);
        file << j.dump(2);
    } catch (const std::exception &e) {
        INXLOG_WARN("ShaderCache: Failed to save cache index: ", e.what());
    }
}

} // namespace infernux
