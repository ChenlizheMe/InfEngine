#include "InxPack.h"

#include "InxPath.h"

#include <zstd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <limits>
#include <mutex>
#include <optional>
#include <set>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <thread>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <unistd.h>
#endif

namespace infernux::inxpack
{
namespace
{

constexpr uint32_t kHeaderBytes = 256;
constexpr uint32_t kTocPrefixBytes = 32;
constexpr uint32_t kEntryBytes = 128;
constexpr size_t kHashChunkBytes = 64u * 1024u;
constexpr std::array<char, 4> kTocMagic = {'T', 'O', 'C', '0'};

#pragma pack(push, 1)
struct HeaderDisk
{
    char magic[8];
    uint32_t revision;
    uint32_t headerBytes;
    uint32_t flags;
    uint32_t entryBytes;
    uint64_t tocOffset;
    uint64_t tocBytes;
    uint64_t payloadOffset;
    uint64_t payloadBytes;
    uint64_t entryCount;
    uint64_t stringBytes;
    uint64_t rawBytes;
    uint8_t tocHash[32];
    uint8_t payloadHash[32];
    uint8_t packageHash[32];
    uint8_t reserved[80];
};

struct TocPrefixDisk
{
    char magic[4];
    uint32_t revision;
    uint32_t entryBytes;
    uint32_t flags;
    uint64_t entryCount;
    uint64_t stringBytes;
};

struct EntryDisk
{
    uint64_t pathOffset;
    uint32_t pathBytes;
    uint32_t flags;
    uint64_t payloadOffset;
    uint64_t storedBytes;
    uint64_t rawBytes;
    uint8_t codec;
    uint8_t codecReserved[7];
    uint8_t hash[32];
    uint8_t storedHash[32];
    uint32_t alignment;
    uint32_t reserved;
    uint8_t tailReserved[8];
};
#pragma pack(pop)

static_assert(sizeof(HeaderDisk) == kHeaderBytes);
static_assert(sizeof(TocPrefixDisk) == kTocPrefixBytes);
static_assert(sizeof(EntryDisk) == kEntryBytes);

constexpr uint64_t AlignUp(uint64_t value, uint64_t alignment)
{
    if (value > std::numeric_limits<uint64_t>::max() - (alignment - 1u))
        throw std::overflow_error("InxPack alignment overflow");
    return (value + alignment - 1u) / alignment * alignment;
}

uint64_t CheckedAdd(uint64_t left, uint64_t right, const char *what)
{
    if (left > std::numeric_limits<uint64_t>::max() - right)
        throw std::runtime_error(std::string("InxPack ") + what + " overflows");
    return left + right;
}

uint64_t CheckedMul(uint64_t left, uint64_t right, const char *what)
{
    if (right != 0 && left > std::numeric_limits<uint64_t>::max() / right)
        throw std::runtime_error(std::string("InxPack ") + what + " overflows");
    return left * right;
}

bool ReserveTemporaryPath(const std::filesystem::path &path)
{
#ifdef _WIN32
    HANDLE handle =
        ::CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY, nullptr);
    if (handle != INVALID_HANDLE_VALUE) {
        ::CloseHandle(handle);
        return true;
    }
    const DWORD error = ::GetLastError();
    if (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS)
        return false;
    throw std::system_error(static_cast<int>(error), std::system_category(), "InxPack cannot reserve temporary output");
#else
    const int descriptor = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (descriptor >= 0) {
        ::close(descriptor);
        return true;
    }
    if (errno == EEXIST)
        return false;
    throw std::system_error(errno, std::generic_category(), "InxPack cannot reserve temporary output");
#endif
}

std::filesystem::path MakeUniqueTemporaryPath(const std::filesystem::path &target)
{
    static std::atomic<uint64_t> sequence{0};
    const uint64_t timestamp = static_cast<uint64_t>(std::chrono::steady_clock::now().time_since_epoch().count());
    const uint64_t thread = static_cast<uint64_t>(std::hash<std::thread::id>{}(std::this_thread::get_id()));
#ifdef _WIN32
    const uint64_t process = static_cast<uint64_t>(::GetCurrentProcessId());
#else
    const uint64_t process = static_cast<uint64_t>(::getpid());
#endif

    for (uint32_t attempt = 0; attempt < 1024; ++attempt) {
        const uint64_t serial = sequence.fetch_add(1, std::memory_order_relaxed);
        auto candidate = target;
        candidate += ".tmp.inxpkg." + std::to_string(process) + "." + std::to_string(thread) + "." +
                     std::to_string(timestamp) + "." + std::to_string(serial + attempt);
        if (ReserveTemporaryPath(candidate))
            return candidate;
    }
    throw std::runtime_error("InxPack exhausted unique temporary output names");
}

class TemporaryOutputGuard
{
  public:
    explicit TemporaryOutputGuard(std::filesystem::path path) : path_(std::move(path))
    {
    }
    ~TemporaryOutputGuard()
    {
        if (!active_)
            return;
        std::error_code ignored;
        std::filesystem::remove(path_, ignored);
    }

    void release() noexcept
    {
        active_ = false;
    }

  private:
    std::filesystem::path path_;
    bool active_ = true;
};

bool ReplaceFile(const std::filesystem::path &source, const std::filesystem::path &target, std::error_code &error)
{
#ifdef _WIN32
    if (::MoveFileExW(source.c_str(), target.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        error.clear();
        return true;
    }
    error = std::error_code(static_cast<int>(::GetLastError()), std::system_category());
    return false;
#else
    std::filesystem::rename(source, target, error);
    return !error;
#endif
}

bool IsTransientReplaceError(const std::error_code &error) noexcept
{
#ifdef _WIN32
    return error.value() == ERROR_ACCESS_DENIED || error.value() == ERROR_SHARING_VIOLATION ||
           error.value() == ERROR_LOCK_VIOLATION;
#else
    return error.value() == EACCES || error.value() == EAGAIN || error.value() == EBUSY;
#endif
}

class Sha256
{
  public:
    Sha256()
    {
        state_ = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                  0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
    }

    void Update(const uint8_t *data, size_t size)
    {
        while (size != 0) {
            const size_t available = sizeof(buffer_) - bufferSize_;
            const size_t count = std::min(available, size);
            std::memcpy(buffer_.data() + bufferSize_, data, count);
            bufferSize_ += count;
            data += count;
            size -= count;
            bitCount_ += static_cast<uint64_t>(count) * 8u;
            if (bufferSize_ == sizeof(buffer_)) {
                Transform(buffer_.data());
                bufferSize_ = 0;
            }
        }
    }

    std::array<uint8_t, 32> Final()
    {
        const uint64_t originalBits = bitCount_;
        const uint8_t one = 0x80;
        Update(&one, 1);
        const uint8_t zero = 0;
        while (bufferSize_ != 56)
            Update(&zero, 1);
        std::array<uint8_t, 8> length{};
        for (size_t index = 0; index < length.size(); ++index)
            length[length.size() - index - 1] = static_cast<uint8_t>(originalBits >> (index * 8));
        Update(length.data(), length.size());

        std::array<uint8_t, 32> result{};
        for (size_t index = 0; index < state_.size(); ++index) {
            for (size_t byte = 0; byte < sizeof(uint32_t); ++byte)
                result[index * 4 + byte] = static_cast<uint8_t>(state_[index] >> (24 - byte * 8));
        }
        return result;
    }

  private:
    static constexpr std::array<uint32_t, 64> kRoundConstants = {
        0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
        0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
        0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
        0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
        0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
        0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
        0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
        0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u};

    static uint32_t RotateRight(uint32_t value, uint32_t count)
    {
        return (value >> count) | (value << (32u - count));
    }

    static uint32_t Ch(uint32_t x, uint32_t y, uint32_t z)
    {
        return (x & y) ^ (~x & z);
    }

    static uint32_t Maj(uint32_t x, uint32_t y, uint32_t z)
    {
        return (x & y) ^ (x & z) ^ (y & z);
    }

    void Transform(const uint8_t *block)
    {
        std::array<uint32_t, 64> words{};
        for (size_t index = 0; index < 16; ++index)
            words[index] =
                (static_cast<uint32_t>(block[index * 4]) << 24) | (static_cast<uint32_t>(block[index * 4 + 1]) << 16) |
                (static_cast<uint32_t>(block[index * 4 + 2]) << 8) | static_cast<uint32_t>(block[index * 4 + 3]);
        for (size_t index = 16; index < words.size(); ++index) {
            const uint32_t s0 =
                RotateRight(words[index - 15], 7) ^ RotateRight(words[index - 15], 18) ^ (words[index - 15] >> 3);
            const uint32_t s1 =
                RotateRight(words[index - 2], 17) ^ RotateRight(words[index - 2], 19) ^ (words[index - 2] >> 10);
            words[index] = words[index - 16] + s0 + words[index - 7] + s1;
        }

        uint32_t a = state_[0], b = state_[1], c = state_[2], d = state_[3];
        uint32_t e = state_[4], f = state_[5], g = state_[6], h = state_[7];
        for (size_t index = 0; index < words.size(); ++index) {
            const uint32_t sum1 = RotateRight(e, 6) ^ RotateRight(e, 11) ^ RotateRight(e, 25);
            const uint32_t choice = Ch(e, f, g);
            const uint32_t temporary1 = h + sum1 + choice + kRoundConstants[index] + words[index];
            const uint32_t sum0 = RotateRight(a, 2) ^ RotateRight(a, 13) ^ RotateRight(a, 22);
            const uint32_t majority = Maj(a, b, c);
            const uint32_t temporary2 = sum0 + majority;
            h = g;
            g = f;
            f = e;
            e = d + temporary1;
            d = c;
            c = b;
            b = a;
            a = temporary1 + temporary2;
        }
        state_[0] += a;
        state_[1] += b;
        state_[2] += c;
        state_[3] += d;
        state_[4] += e;
        state_[5] += f;
        state_[6] += g;
        state_[7] += h;
    }

    std::array<uint32_t, 8> state_{};
    std::array<uint8_t, 64> buffer_{};
    size_t bufferSize_ = 0;
    uint64_t bitCount_ = 0;
};

std::array<uint8_t, 32> HashBytes(const uint8_t *data, size_t size)
{
    Sha256 digest;
    digest.Update(data, size);
    return digest.Final();
}

std::array<uint8_t, 32> CopyHash(const uint8_t *data)
{
    std::array<uint8_t, 32> result{};
    std::copy(data, data + result.size(), result.begin());
    return result;
}

std::string HashFile(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error("InxPack cannot open file for hashing: " + FromFsPath(path));
    Sha256 digest;
    std::vector<uint8_t> buffer(kHashChunkBytes);
    while (input) {
        input.read(reinterpret_cast<char *>(buffer.data()), static_cast<std::streamsize>(buffer.size()));
        const auto count = input.gcount();
        if (count > 0)
            digest.Update(buffer.data(), static_cast<size_t>(count));
    }
    if (!input.eof())
        throw std::runtime_error("InxPack failed while hashing file: " + FromFsPath(path));
    return HashToHex(digest.Final());
}

std::array<uint8_t, 32> HashFileRange(const std::filesystem::path &path, uint64_t offset, uint64_t size,
                                      const std::optional<std::pair<uint64_t, uint64_t>> &zeroRange = std::nullopt)
{
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error("InxPack cannot open package for hashing: " + FromFsPath(path));
    input.seekg(static_cast<std::streamoff>(offset));
    Sha256 digest;
    std::vector<uint8_t> buffer(kHashChunkBytes);
    uint64_t remaining = size;
    uint64_t cursor = offset;
    while (remaining != 0) {
        const size_t count = static_cast<size_t>(std::min<uint64_t>(remaining, buffer.size()));
        input.read(reinterpret_cast<char *>(buffer.data()), static_cast<std::streamsize>(count));
        if (input.gcount() != static_cast<std::streamsize>(count))
            throw std::runtime_error("InxPack package ended during checksum validation");
        if (zeroRange) {
            const uint64_t zeroStart = zeroRange->first;
            const uint64_t zeroEnd = zeroRange->first + zeroRange->second;
            const uint64_t chunkEnd = cursor + count;
            if (cursor < zeroEnd && chunkEnd > zeroStart) {
                const uint64_t begin = std::max(cursor, zeroStart) - cursor;
                const uint64_t end = std::min(chunkEnd, zeroEnd) - cursor;
                std::fill(buffer.begin() + static_cast<size_t>(begin), buffer.begin() + static_cast<size_t>(end), 0);
            }
        }
        digest.Update(buffer.data(), count);
        cursor += count;
        remaining -= count;
    }
    return digest.Final();
}

std::string ToHex(const uint8_t *data, size_t size)
{
    static constexpr char digits[] = "0123456789abcdef";
    std::string result;
    result.reserve(size * 2);
    for (size_t index = 0; index < size; ++index) {
        result.push_back(digits[data[index] >> 4]);
        result.push_back(digits[data[index] & 0x0f]);
    }
    return result;
}

std::vector<uint8_t> ReadBytes(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input)
        throw std::runtime_error("InxPack cannot open source: " + FromFsPath(path));
    const auto end = input.tellg();
    if (end < 0 || static_cast<uint64_t>(end) > std::numeric_limits<size_t>::max())
        throw std::runtime_error("InxPack source is too large: " + FromFsPath(path));
    std::vector<uint8_t> bytes(static_cast<size_t>(end));
    input.seekg(0);
    if (!bytes.empty() &&
        !input.read(reinterpret_cast<char *>(bytes.data()), static_cast<std::streamsize>(bytes.size())))
        throw std::runtime_error("InxPack failed while reading source: " + FromFsPath(path));
    return bytes;
}

std::string NormalizePath(const std::string &path)
{
    std::string normalized = NormalizePortablePath(path);
    if (normalized.empty() || normalized.front() == '/' || normalized.find(':') != std::string::npos)
        throw std::invalid_argument("InxPack path must be portable and relative: " + path);
    std::vector<std::string> parts;
    size_t begin = 0;
    while (begin <= normalized.size()) {
        const size_t end = normalized.find('/', begin);
        const std::string part = normalized.substr(begin, end == std::string::npos ? std::string::npos : end - begin);
        if (part.empty() || part == "." || part == "..")
            throw std::invalid_argument("InxPack path contains an unsafe component: " + path);
        parts.push_back(part);
        if (end == std::string::npos)
            break;
        begin = end + 1;
    }
    std::string result;
    for (const auto &part : parts) {
        if (!result.empty())
            result += '/';
        result += part;
    }
    return result;
}

void WriteExact(std::ostream &output, const void *data, size_t size)
{
    output.write(static_cast<const char *>(data), static_cast<std::streamsize>(size));
    if (!output)
        throw std::runtime_error("InxPack failed while writing output");
}

void ReadExact(std::istream &input, void *data, size_t size)
{
    input.read(static_cast<char *>(data), static_cast<std::streamsize>(size));
    if (!input)
        throw std::runtime_error("InxPack ended before a fixed record was complete");
}

void WriteZeros(std::ostream &output, uint64_t count)
{
    std::array<uint8_t, 4096> zeros{};
    while (count != 0) {
        const auto amount = static_cast<size_t>(std::min<uint64_t>(count, zeros.size()));
        WriteExact(output, zeros.data(), amount);
        count -= amount;
    }
}

void WriteZerosHashed(std::ostream &output, uint64_t count, Sha256 &digest)
{
    std::array<uint8_t, 4096> zeros{};
    while (count != 0) {
        const auto amount = static_cast<size_t>(std::min<uint64_t>(count, zeros.size()));
        WriteExact(output, zeros.data(), amount);
        digest.Update(zeros.data(), amount);
        count -= amount;
    }
}

bool IsAllowedRoot(const std::string &path, const std::vector<std::string> &allowedRoots)
{
    if (allowedRoots.empty())
        return true;
    const size_t separator = path.find('/');
    const std::string root = path.substr(0, separator);
    return std::find(allowedRoots.begin(), allowedRoots.end(), root) != allowedRoots.end();
}

Manifest ParseManifest(std::ifstream &input, const std::filesystem::path &path, const bool validateWholeArchive)
{
    HeaderDisk header{};
    ReadExact(input, &header, sizeof(header));
    if (!std::equal(std::begin(header.magic), std::end(header.magic), kMagic.begin()))
        throw std::runtime_error("unsupported InxPack magic (legacy ZIP/LZMA packages are not accepted): " +
                                 FromFsPath(path));
    if (header.revision != kFormatRevision || header.headerBytes != sizeof(HeaderDisk) ||
        header.entryBytes != sizeof(EntryDisk))
        throw std::runtime_error("unsupported InxPack revision or record size: " + FromFsPath(path));
    if (header.tocOffset != sizeof(HeaderDisk) || header.tocOffset % kAlignment != 0 ||
        header.payloadOffset % kAlignment != 0 || header.entryCount > std::numeric_limits<size_t>::max())
        throw std::runtime_error("invalid InxPack offsets or alignment: " + FromFsPath(path));

    const uint64_t tocEnd = CheckedAdd(header.tocOffset, header.tocBytes, "TOC range");
    const uint64_t payloadEnd = CheckedAdd(header.payloadOffset, header.payloadBytes, "payload range");
    if (header.payloadOffset < tocEnd)
        throw std::runtime_error("invalid InxPack TOC/payload ordering: " + FromFsPath(path));

    input.seekg(0, std::ios::end);
    const auto end = input.tellg();
    if (end < 0 || static_cast<uint64_t>(end) < payloadEnd)
        throw std::runtime_error("truncated InxPack payload: " + FromFsPath(path));
    const uint64_t fileBytes = static_cast<uint64_t>(end);
    if (header.tocBytes > std::numeric_limits<size_t>::max())
        throw std::runtime_error("InxPack TOC is too large: " + FromFsPath(path));

    input.seekg(static_cast<std::streamoff>(header.tocOffset));
    std::vector<uint8_t> toc(static_cast<size_t>(header.tocBytes));
    ReadExact(input, toc.data(), toc.size());
    if (HashBytes(toc.data(), toc.size()) != CopyHash(header.tocHash))
        throw std::runtime_error("InxPack TOC checksum mismatch: " + FromFsPath(path));
    if (validateWholeArchive) {
        // packageHash covers the payload, fixed header and TOC. payloadHash is
        // deliberately not recomputed as that would reread every large Player
        // archive without adding corruption coverage.
        const auto packageHash = HashFileRange(
            path, 0, fileBytes,
            std::make_pair(static_cast<uint64_t>(offsetof(HeaderDisk, packageHash)), sizeof(header.packageHash)));
        if (packageHash != CopyHash(header.packageHash))
            throw std::runtime_error("InxPack package checksum mismatch: " + FromFsPath(path));
    }

    if (toc.size() < sizeof(TocPrefixDisk))
        throw std::runtime_error("InxPack TOC is truncated: " + FromFsPath(path));
    TocPrefixDisk prefix{};
    std::memcpy(&prefix, toc.data(), sizeof(prefix));
    if (!std::equal(std::begin(prefix.magic), std::end(prefix.magic), kTocMagic.begin()) ||
        prefix.revision != kFormatRevision || prefix.entryBytes != sizeof(EntryDisk) ||
        prefix.entryCount != header.entryCount || prefix.stringBytes != header.stringBytes)
        throw std::runtime_error("InxPack TOC header is invalid: " + FromFsPath(path));
    const size_t tocDataBytes = toc.size() - sizeof(TocPrefixDisk);
    if (prefix.entryCount > tocDataBytes / sizeof(EntryDisk))
        throw std::runtime_error("InxPack TOC entry table overflows: " + FromFsPath(path));
    const uint64_t recordsBytes = CheckedMul(prefix.entryCount, sizeof(EntryDisk), "TOC entry table");
    const uint64_t stringsOffset = CheckedAdd(sizeof(TocPrefixDisk), recordsBytes, "TOC string offset");
    if (stringsOffset > toc.size() || prefix.stringBytes > toc.size() - stringsOffset)
        throw std::runtime_error("InxPack TOC tables are out of range: " + FromFsPath(path));

    Manifest manifest;
    manifest.revision = header.revision;
    manifest.rawBytes = header.rawBytes;
    manifest.storedBytes = 0;
    manifest.payloadBytes = header.payloadBytes;
    manifest.archiveBytes = fileBytes;
    std::copy(std::begin(header.packageHash), std::end(header.packageHash), manifest.archiveHash.begin());
    std::set<std::string> paths;
    manifest.entries.reserve(static_cast<size_t>(prefix.entryCount));
    for (uint64_t index = 0; index < prefix.entryCount; ++index) {
        EntryDisk disk{};
        std::memcpy(&disk, toc.data() + sizeof(TocPrefixDisk) + index * sizeof(EntryDisk), sizeof(disk));
        if (disk.pathBytes > std::numeric_limits<uint32_t>::max() || disk.pathOffset > prefix.stringBytes ||
            disk.pathBytes > prefix.stringBytes - disk.pathOffset || disk.payloadOffset % kAlignment != 0 ||
            disk.payloadOffset > header.payloadBytes || disk.storedBytes > header.payloadBytes - disk.payloadOffset ||
            disk.storedBytes > std::numeric_limits<size_t>::max() || disk.rawBytes > std::numeric_limits<size_t>::max())
            throw std::runtime_error("InxPack entry is out of range: " + FromFsPath(path));
        const std::string rawPath(reinterpret_cast<const char *>(toc.data() + stringsOffset + disk.pathOffset),
                                  disk.pathBytes);
        const std::string normalized = NormalizePath(rawPath);
        if (normalized != rawPath || !paths.insert(normalized).second)
            throw std::runtime_error("InxPack entry path is not canonical or is duplicated: " + rawPath);
        if (disk.codec != static_cast<uint8_t>(Codec::Store) && disk.codec != static_cast<uint8_t>(Codec::Zstandard))
            throw std::runtime_error("InxPack entry uses an unsupported codec: " + normalized);
        if (disk.alignment != kAlignment)
            throw std::runtime_error("InxPack entry alignment is invalid: " + normalized);

        Entry entry;
        entry.path = normalized;
        entry.offset = disk.payloadOffset;
        entry.storedBytes = disk.storedBytes;
        entry.rawBytes = disk.rawBytes;
        entry.codec = static_cast<Codec>(disk.codec);
        std::copy(std::begin(disk.hash), std::end(disk.hash), entry.hash.begin());
        std::copy(std::begin(disk.storedHash), std::end(disk.storedHash), entry.storedHash.begin());
        manifest.storedBytes = CheckedAdd(manifest.storedBytes, entry.storedBytes, "stored byte count");
        manifest.entries.push_back(std::move(entry));
    }
    return manifest;
}

HeaderDisk MakeHeader(const Manifest &manifest, uint64_t tocOffset, uint64_t tocBytes, uint64_t payloadOffset,
                      uint64_t stringBytes, const std::array<uint8_t, 32> &tocHash,
                      const std::array<uint8_t, 32> &payloadHash, const std::array<uint8_t, 32> &packageHash)
{
    HeaderDisk header{};
    std::copy(kMagic.begin(), kMagic.end(), std::begin(header.magic));
    header.revision = kFormatRevision;
    header.headerBytes = sizeof(HeaderDisk);
    header.entryBytes = sizeof(EntryDisk);
    header.tocOffset = tocOffset;
    header.tocBytes = tocBytes;
    header.payloadOffset = payloadOffset;
    header.payloadBytes = manifest.payloadBytes;
    header.entryCount = manifest.entries.size();
    header.stringBytes = stringBytes;
    header.rawBytes = manifest.rawBytes;
    std::copy(tocHash.begin(), tocHash.end(), std::begin(header.tocHash));
    std::copy(payloadHash.begin(), payloadHash.end(), std::begin(header.payloadHash));
    std::copy(packageHash.begin(), packageHash.end(), std::begin(header.packageHash));
    return header;
}

} // namespace

std::string HashToHex(const std::array<uint8_t, 32> &hash)
{
    return ToHex(hash.data(), hash.size());
}

const char *CodecName(Codec codec) noexcept
{
    return codec == Codec::Zstandard ? "zstd" : "store";
}

int ValidateCompressionLevel(const int compressionLevel)
{
    if (compressionLevel < kMinimumCompressionLevel || compressionLevel > kMaximumCompressionLevel)
        throw std::invalid_argument("InxPack compression level must be between " +
                                    std::to_string(kMinimumCompressionLevel) + " and " +
                                    std::to_string(kMaximumCompressionLevel));
    return compressionLevel;
}

int ResolveCompressionLevel(const WriteOptions &options)
{
    if (options.compressionLevel != 0)
        return ValidateCompressionLevel(options.compressionLevel);

    switch (options.profile) {
    case CompressionProfile::Development:
        return kDevelopmentCompressionLevel;
    case CompressionProfile::Release:
        return kReleaseCompressionLevel;
    default:
        throw std::invalid_argument("InxPack compression profile is invalid");
    }
}

Manifest Write(const std::filesystem::path &destination, std::vector<SourceFile> sources, const WriteOptions &options)
{
    const int compressionLevel = ResolveCompressionLevel(options);
    std::set<std::string> paths;
    for (auto &source : sources) {
        source.path = NormalizePath(source.path);
        if (!paths.insert(source.path).second)
            throw std::invalid_argument("duplicate InxPack entry path: " + source.path);
    }
    std::sort(sources.begin(), sources.end(),
              [](const SourceFile &left, const SourceFile &right) { return left.path < right.path; });
    if (sources.empty())
        throw std::invalid_argument("InxPack cannot be empty");

    Manifest manifest;
    manifest.entries.reserve(sources.size());
    std::vector<std::string> strings;
    strings.reserve(sources.size());
    for (const auto &source : sources) {
        Entry entry;
        entry.path = source.path;
        manifest.entries.push_back(std::move(entry));
        strings.push_back(source.path);
    }

    uint64_t stringBytes = 0;
    for (const auto &value : strings)
        stringBytes += value.size();
    const uint64_t tocBytes =
        AlignUp(sizeof(TocPrefixDisk) + manifest.entries.size() * sizeof(EntryDisk) + stringBytes, kAlignment);
    const uint64_t tocOffset = sizeof(HeaderDisk);
    const uint64_t payloadOffset = AlignUp(tocOffset + tocBytes, kAlignment);
    const std::filesystem::path destinationPath = destination;
    if (!destinationPath.parent_path().empty())
        std::filesystem::create_directories(destinationPath.parent_path());
    // Keep the temporary output as a filesystem::path so Unicode Windows paths
    // stay wide all the way through the exclusive reservation and publish.
    const auto temporaryOutputPath = MakeUniqueTemporaryPath(destinationPath);
    TemporaryOutputGuard temporaryGuard(temporaryOutputPath);
    Sha256 payloadDigest;
    {
        std::ofstream output(temporaryOutputPath, std::ios::binary | std::ios::trunc);
        if (!output)
            throw std::runtime_error("InxPack cannot create output: " + FromFsPath(destination));
        HeaderDisk placeholder{};
        WriteExact(output, &placeholder, sizeof(placeholder));
        WriteZeros(output, tocBytes);
        WriteZeros(output, payloadOffset - tocOffset - tocBytes);

        uint64_t cursor = 0;
        for (size_t index = 0; index < sources.size(); ++index) {
            const auto raw = ReadBytes(sources[index].source);
            std::vector<uint8_t> compressed(ZSTD_compressBound(raw.size()));
            const size_t compressedBytes =
                ZSTD_compress(compressed.data(), compressed.size(), raw.data(), raw.size(), compressionLevel);
            if (ZSTD_isError(compressedBytes))
                throw std::runtime_error(std::string("InxPack zstd compression failed: ") +
                                         ZSTD_getErrorName(compressedBytes));
            compressed.resize(compressedBytes);

            auto &entry = manifest.entries[index];
            entry.offset = cursor;
            entry.rawBytes = raw.size();
            entry.hash = HashBytes(raw.data(), raw.size());
            const std::vector<uint8_t> &stored = compressed.size() + 16 < raw.size() ? compressed : raw;
            entry.codec = &stored == &compressed ? Codec::Zstandard : Codec::Store;
            entry.storedBytes = stored.size();
            entry.storedHash = HashBytes(stored.data(), stored.size());
            manifest.rawBytes += entry.rawBytes;
            manifest.storedBytes += entry.storedBytes;

            WriteExact(output, stored.data(), stored.size());
            payloadDigest.Update(stored.data(), stored.size());
            cursor = AlignUp(cursor + stored.size(), kAlignment);
            const uint64_t written = entry.offset + stored.size();
            WriteZerosHashed(output, cursor - written, payloadDigest);
        }
        manifest.payloadBytes = cursor;
        output.flush();
        if (!output)
            throw std::runtime_error("InxPack failed to flush output: " + FromFsPath(destination));
    }

    std::vector<uint8_t> toc(static_cast<size_t>(tocBytes));
    TocPrefixDisk prefix{};
    std::copy(kTocMagic.begin(), kTocMagic.end(), std::begin(prefix.magic));
    prefix.revision = kFormatRevision;
    prefix.entryBytes = sizeof(EntryDisk);
    prefix.entryCount = manifest.entries.size();
    prefix.stringBytes = stringBytes;
    std::memcpy(toc.data(), &prefix, sizeof(prefix));
    const uint64_t stringOffset = sizeof(TocPrefixDisk) + manifest.entries.size() * sizeof(EntryDisk);
    uint64_t pathOffset = 0;
    for (size_t index = 0; index < manifest.entries.size(); ++index) {
        EntryDisk disk{};
        const auto &entry = manifest.entries[index];
        disk.pathOffset = pathOffset;
        disk.pathBytes = static_cast<uint32_t>(entry.path.size());
        disk.payloadOffset = entry.offset;
        disk.storedBytes = entry.storedBytes;
        disk.rawBytes = entry.rawBytes;
        disk.codec = static_cast<uint8_t>(entry.codec);
        std::copy(entry.hash.begin(), entry.hash.end(), std::begin(disk.hash));
        std::copy(entry.storedHash.begin(), entry.storedHash.end(), std::begin(disk.storedHash));
        disk.alignment = kAlignment;
        std::memcpy(toc.data() + sizeof(TocPrefixDisk) + index * sizeof(EntryDisk), &disk, sizeof(disk));
        std::memcpy(toc.data() + stringOffset + pathOffset, entry.path.data(), entry.path.size());
        pathOffset += entry.path.size();
    }

    const auto tocHash = HashBytes(toc.data(), toc.size());
    {
        std::fstream output(temporaryOutputPath, std::ios::binary | std::ios::in | std::ios::out);
        if (!output)
            throw std::runtime_error("InxPack cannot reopen output: " + FromFsPath(destination));
        output.seekp(static_cast<std::streamoff>(tocOffset));
        WriteExact(output, toc.data(), toc.size());
        output.flush();
        if (!output)
            throw std::runtime_error("InxPack failed to write its table of contents: " + FromFsPath(destination));
    }

    const auto payloadHash = payloadDigest.Final();
    HeaderDisk header = MakeHeader(manifest, tocOffset, tocBytes, payloadOffset, stringBytes, tocHash, payloadHash,
                                   HashBytes(nullptr, 0));
    {
        std::fstream output(temporaryOutputPath, std::ios::binary | std::ios::in | std::ios::out);
        if (!output)
            throw std::runtime_error("InxPack cannot reopen output: " + FromFsPath(destination));
        output.seekp(0);
        WriteExact(output, &header, sizeof(header));
    }
    const auto packageHash = HashFileRange(
        temporaryOutputPath, 0, static_cast<uint64_t>(std::filesystem::file_size(temporaryOutputPath)),
        std::make_pair(static_cast<uint64_t>(offsetof(HeaderDisk, packageHash)), sizeof(header.packageHash)));
    header = MakeHeader(manifest, tocOffset, tocBytes, payloadOffset, stringBytes, tocHash, payloadHash, packageHash);
    {
        std::fstream output(temporaryOutputPath, std::ios::binary | std::ios::in | std::ios::out);
        if (!output)
            throw std::runtime_error("InxPack cannot publish output: " + FromFsPath(destination));
        output.seekp(0);
        WriteExact(output, &header, sizeof(header));
    }
    std::error_code replaceError;
    bool published = false;
    for (uint32_t attempt = 0; attempt < 64; ++attempt) {
        if (ReplaceFile(temporaryOutputPath, destinationPath, replaceError)) {
            published = true;
            break;
        }
        if (!IsTransientReplaceError(replaceError) || attempt + 1 == 64)
            break;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    if (!published)
        throw std::runtime_error("InxPack cannot publish output: " + replaceError.message());
    temporaryGuard.release();

    manifest.archiveBytes = std::filesystem::file_size(destinationPath);
    manifest.archiveHash = packageHash;
    return manifest;
}

Manifest ReadManifest(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error("InxPack cannot open package: " + FromFsPath(path));
    return ParseManifest(input, path, true);
}

std::vector<uint8_t> ReadEntry(const std::filesystem::path &path, const std::string &entryPath)
{
    std::ifstream manifestInput(path, std::ios::binary);
    if (!manifestInput)
        throw std::runtime_error("InxPack cannot open package: " + FromFsPath(path));
    const Manifest manifest = ParseManifest(manifestInput, path, false);
    const std::string normalized = NormalizePath(entryPath);
    const auto found = std::find_if(manifest.entries.begin(), manifest.entries.end(),
                                    [&normalized](const Entry &entry) { return entry.path == normalized; });
    if (found == manifest.entries.end())
        throw std::runtime_error("InxPack entry was not found: " + normalized);

    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error("InxPack cannot open package: " + FromFsPath(path));
    HeaderDisk header{};
    ReadExact(input, &header, sizeof(header));
    const uint64_t absoluteOffset = CheckedAdd(header.payloadOffset, found->offset, "entry offset");
    input.seekg(static_cast<std::streamoff>(absoluteOffset));
    if (!input)
        throw std::runtime_error("InxPack entry offset is outside the package: " + found->path);
    std::vector<uint8_t> stored(static_cast<size_t>(found->storedBytes));
    if (!stored.empty())
        ReadExact(input, stored.data(), stored.size());
    if (HashBytes(stored.data(), stored.size()) != found->storedHash)
        throw std::runtime_error("InxPack stored block checksum mismatch: " + found->path);

    std::vector<uint8_t> raw(static_cast<size_t>(found->rawBytes));
    if (found->codec == Codec::Store) {
        if (stored.size() != raw.size())
            throw std::runtime_error("InxPack stored block size mismatch: " + found->path);
        raw = std::move(stored);
    } else if (found->codec == Codec::Zstandard) {
        const size_t decoded = ZSTD_decompress(raw.data(), raw.size(), stored.data(), stored.size());
        if (ZSTD_isError(decoded) || decoded != raw.size())
            throw std::runtime_error("InxPack zstd decompression failed: " + found->path);
    } else {
        throw std::runtime_error("InxPack entry uses an unsupported codec: " + found->path);
    }
    if (HashBytes(raw.data(), raw.size()) != found->hash)
        throw std::runtime_error("InxPack raw block checksum mismatch: " + found->path);
    return raw;
}

Manifest Extract(const std::filesystem::path &path, const std::filesystem::path &destination,
                 const std::vector<std::string> &allowedRoots)
{
    std::ifstream manifestInput(path, std::ios::binary);
    if (!manifestInput)
        throw std::runtime_error("InxPack cannot open package: " + FromFsPath(path));
    const Manifest manifest = ParseManifest(manifestInput, path, false);
    const auto &targetRoot = destination;
    if (!targetRoot.empty())
        std::filesystem::create_directories(targetRoot);
    HeaderDisk header{};
    manifestInput.seekg(0);
    ReadExact(manifestInput, &header, sizeof(header));

    // Resolve and create the complete directory tree before workers start.
    // Apart from avoiding duplicate create_directories calls, this keeps the
    // parallel phase limited to independent package reads, decompression,
    // validation and file writes.
    std::vector<std::filesystem::path> outputs;
    outputs.reserve(manifest.entries.size());
    for (const auto &entry : manifest.entries) {
        if (!IsAllowedRoot(entry.path, allowedRoots))
            throw std::runtime_error("InxPack entry root is not allowed: " + entry.path);
        std::filesystem::path output = targetRoot;
        size_t begin = 0;
        while (begin < entry.path.size()) {
            const size_t end = entry.path.find('/', begin);
            output /= ToFsPath(entry.path.substr(begin, end == std::string::npos ? std::string::npos : end - begin));
            if (end == std::string::npos)
                break;
            begin = end + 1;
        }
        if (!output.parent_path().empty())
            std::filesystem::create_directories(output.parent_path());
        outputs.push_back(std::move(output));
    }

    const auto extractEntry = [&](std::ifstream &input, size_t index) {
        const auto &entry = manifest.entries[index];
        const uint64_t absoluteOffset = CheckedAdd(header.payloadOffset, entry.offset, "entry offset");
        input.seekg(static_cast<std::streamoff>(absoluteOffset));
        if (!input)
            throw std::runtime_error("InxPack entry offset is outside the package: " + entry.path);
        std::vector<uint8_t> stored(static_cast<size_t>(entry.storedBytes));
        if (!stored.empty())
            ReadExact(input, stored.data(), stored.size());
        if (HashBytes(stored.data(), stored.size()) != entry.storedHash)
            throw std::runtime_error("InxPack stored block checksum mismatch: " + entry.path);

        std::vector<uint8_t> raw(static_cast<size_t>(entry.rawBytes));
        if (entry.codec == Codec::Store) {
            if (stored.size() != raw.size())
                throw std::runtime_error("InxPack stored block size mismatch: " + entry.path);
            raw = std::move(stored);
        } else if (entry.codec == Codec::Zstandard) {
            const size_t decoded = ZSTD_decompress(raw.data(), raw.size(), stored.data(), stored.size());
            if (ZSTD_isError(decoded) || decoded != raw.size())
                throw std::runtime_error("InxPack zstd decompression failed: " + entry.path);
        } else {
            throw std::runtime_error("InxPack entry uses an unsupported codec: " + entry.path);
        }
        if (HashBytes(raw.data(), raw.size()) != entry.hash)
            throw std::runtime_error("InxPack raw block checksum mismatch: " + entry.path);

        std::ofstream destinationFile(outputs[index], std::ios::binary | std::ios::trunc);
        if (!destinationFile || (!raw.empty() && !destinationFile.write(reinterpret_cast<const char *>(raw.data()),
                                                                        static_cast<std::streamsize>(raw.size()))))
            throw std::runtime_error("InxPack cannot extract entry: " + entry.path);
    };

    // Archive entries are independently compressed and hashed. Bounded
    // parallel extraction uses otherwise idle cores during a cold Player
    // launch without creating an unbounded number of disk writers on slower
    // machines. Every worker owns its input stream and output file.
    const size_t hardwareThreads = std::max<size_t>(1, std::thread::hardware_concurrency());
    const size_t workerCount = std::min({manifest.entries.size(), hardwareThreads, size_t{4}});
    std::atomic<size_t> nextEntry{0};
    std::atomic<bool> failed{false};
    std::exception_ptr failure;
    std::mutex failureMutex;
    std::vector<std::thread> workers;
    workers.reserve(workerCount);
    for (size_t worker = 0; worker < workerCount; ++worker) {
        workers.emplace_back([&] {
            try {
                std::ifstream input(path, std::ios::binary);
                if (!input)
                    throw std::runtime_error("InxPack cannot open package: " + FromFsPath(path));
                while (!failed.load(std::memory_order_acquire)) {
                    const size_t index = nextEntry.fetch_add(1, std::memory_order_relaxed);
                    if (index >= manifest.entries.size())
                        break;
                    extractEntry(input, index);
                }
            } catch (...) {
                failed.store(true, std::memory_order_release);
                std::lock_guard lock(failureMutex);
                if (!failure)
                    failure = std::current_exception();
            }
        });
    }
    for (auto &worker : workers)
        worker.join();
    if (failure)
        std::rethrow_exception(failure);
    return manifest;
}

} // namespace infernux::inxpack
