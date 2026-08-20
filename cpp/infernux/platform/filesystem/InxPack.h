#pragma once

#include <array>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace infernux::inxpack
{

// The shipping container is deliberately a single current format.  This is
// not compatible with the former JSON/LZMA INXPCK1 experiment.
inline constexpr std::array<char, 8> kMagic = {'I', 'N', 'X', 'P', 'K', 'G', '\0', '\0'};
inline constexpr uint32_t kFormatRevision = 0x00010000u;
inline constexpr uint32_t kAlignment = 64u;

enum class Codec : uint8_t
{
    Store = 0,
    Zstandard = 1,
};

enum class CompressionProfile : uint8_t
{
    Development = 0,
    Release = 1,
};

inline constexpr int kDevelopmentCompressionLevel = 3;
// Player builds are frequent editor operations. Level 6 retains most of
// Zstandard's size win without turning finalization into an offline encode.
inline constexpr int kReleaseCompressionLevel = 6;
inline constexpr int kMinimumCompressionLevel = 1;
inline constexpr int kMaximumCompressionLevel = 22;

struct WriteOptions
{
    CompressionProfile profile = CompressionProfile::Development;
    // Zero selects the profile default. Non-zero values are validated against
    // the public range above before any source is read or output is created.
    int compressionLevel = 0;
};

struct SourceFile
{
    std::string path;
    std::filesystem::path source;
};

struct Entry
{
    std::string path;
    uint64_t offset = 0;
    uint64_t storedBytes = 0;
    uint64_t rawBytes = 0;
    Codec codec = Codec::Store;
    std::array<uint8_t, 32> hash{};
    std::array<uint8_t, 32> storedHash{};
};

struct Manifest
{
    uint32_t revision = kFormatRevision;
    uint64_t rawBytes = 0;
    uint64_t storedBytes = 0;
    uint64_t payloadBytes = 0;
    uint64_t archiveBytes = 0;
    std::array<uint8_t, 32> archiveHash{};
    std::vector<Entry> entries;
};

/// Return a validated public Zstandard level.
int ValidateCompressionLevel(int compressionLevel);

/// Resolve the effective level without changing the package format.
int ResolveCompressionLevel(const WriteOptions &options);

/// Write one deterministic native InxPack from filesystem sources.
Manifest Write(const std::filesystem::path &destination, std::vector<SourceFile> sources,
               const WriteOptions &options = {});

/// Read and fully validate the fixed header, TOC, payload and package hash.
Manifest ReadManifest(const std::filesystem::path &path);

/// Validate and extract the package.  Empty allowedRoots means no root filter.
Manifest Extract(const std::filesystem::path &path, const std::filesystem::path &destination,
                 const std::vector<std::string> &allowedRoots = {});

/// Read one validated entry without exposing the container algorithm to Python.
std::vector<uint8_t> ReadEntry(const std::filesystem::path &path, const std::string &entryPath);

std::string HashToHex(const std::array<uint8_t, 32> &hash);
const char *CodecName(Codec codec) noexcept;

} // namespace infernux::inxpack
