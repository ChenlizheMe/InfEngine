#include <platform/filesystem/InxPack.h>
#include <platform/filesystem/InxPath.h>

#include <chrono>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#endif

namespace
{
std::filesystem::path UniqueTestRoot()
{
    const auto nonce = std::chrono::steady_clock::now().time_since_epoch().count();
    return std::filesystem::temp_directory_path() / ("infernux-pack-" + std::to_string(nonce));
}

void Require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}

void WriteSource(const std::filesystem::path &path, const std::vector<uint8_t> &bytes)
{
    std::filesystem::create_directories(path.parent_path());
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    Require(static_cast<bool>(output), "cannot create test source file");
    output.write(reinterpret_cast<const char *>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    Require(static_cast<bool>(output), "cannot write test source file");
}

std::vector<uint8_t> ReadFile(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    Require(static_cast<bool>(input), "cannot open test package");
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void RequireNoTemporaryFiles(const std::filesystem::path &root)
{
    for (const auto &entry : std::filesystem::directory_iterator(root)) {
        const std::string name = infernux::FromFsPath(entry.path().filename());
        Require(name.find(".tmp.inxpkg.") == std::string::npos, "InxPack left a temporary output file behind");
    }
}
} // namespace

int main()
{
    using namespace infernux;
    using namespace infernux::inxpack;

    std::cerr << "[InxPackTests] main\n" << std::flush;
    std::cerr << "[InxPackTests] UniqueTestRoot\n" << std::flush;
    const auto root = UniqueTestRoot();
    const auto cleanup = [&root]() {
        std::error_code cleanupError;
        std::filesystem::remove_all(root, cleanupError);
        return cleanupError;
    };

    const char *phase = "setup";
    try {
        const auto sourceDirectory = root / ToFsPath("源文件目录");
        const auto sourcePath = sourceDirectory / ToFsPath("中文源文件.bin");
        const auto packagePath = root / ToFsPath("中文临时目录") / ToFsPath("中文运行包.inxrt");
        const auto extractRoot = root / ToFsPath("解包结果");
        const std::vector<uint8_t> expected = {0x00, 0x11, 0x22, 0x80, 0xfe, 0xff};

        phase = "create Chinese source file";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        std::filesystem::create_directories(sourceDirectory);
        {
            std::ofstream source(sourcePath, std::ios::binary | std::ios::trunc);
            Require(static_cast<bool>(source), "cannot create source file");
            source.write(reinterpret_cast<const char *>(expected.data()),
                         static_cast<std::streamsize>(expected.size()));
            Require(static_cast<bool>(source), "cannot write source file");
        }

        phase = "compression options and deterministic output";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        Require(ResolveCompressionLevel({}) == kDevelopmentCompressionLevel, "the default compression profile changed");
        Require(ResolveCompressionLevel({CompressionProfile::Release, 0}) == kReleaseCompressionLevel,
                "the release compression profile changed");
        Require(ResolveCompressionLevel({CompressionProfile::Development, 7}) == 7,
                "an explicit compression level was not selected");
        for (const int invalidLevel : {kMinimumCompressionLevel - 1, kMaximumCompressionLevel + 1}) {
            bool rejected = false;
            try {
                ValidateCompressionLevel(invalidLevel);
            } catch (const std::invalid_argument &) {
                rejected = true;
            }
            Require(rejected, "an invalid compression level was accepted");
        }

        const auto compressibleSource = sourceDirectory / ToFsPath("可压缩源文件.bin");
        std::vector<uint8_t> compressibleBytes(64 * 1024);
        for (size_t index = 0; index < compressibleBytes.size(); ++index)
            compressibleBytes[index] = static_cast<uint8_t>((index / 256) % 7);
        WriteSource(compressibleSource, compressibleBytes);
        const auto deterministicA = root / ToFsPath("确定性A.inxrt");
        const auto deterministicB = root / ToFsPath("确定性B.inxrt");
        const auto releasePackage = root / ToFsPath("release.inxrt");
        const std::vector<SourceFile> compressibleSources = {{"Runtime/Resources.bin", compressibleSource}};
        const Manifest defaultA = Write(deterministicA, compressibleSources);
        const Manifest defaultB = Write(deterministicB, compressibleSources);
        Require(ReadFile(deterministicA) == ReadFile(deterministicB), "default InxPack output is not deterministic");
        const Manifest release =
            Write(releasePackage, compressibleSources, {CompressionProfile::Release, kReleaseCompressionLevel});
        Require(release.entries.size() == 1 && release.entries.front().rawBytes == compressibleBytes.size(),
                "explicit release compression produced an invalid manifest");

        phase = "Write";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        const Manifest written = Write(packagePath, {{"Assets/中文/源文件.bin", sourcePath}});
        Require(written.entries.size() == 1, "Write returned an unexpected entry count");
        Require(std::filesystem::is_regular_file(packagePath), "Write did not create the package");

        phase = "ReadManifest";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        const Manifest read = ReadManifest(packagePath);
        Require(read.entries.size() == 1, "ReadManifest returned an unexpected entry count");
        Require(read.entries.front().path == "Assets/中文/源文件.bin", "ReadManifest returned the wrong entry path");

        phase = "ReadEntry";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        Require(ReadEntry(packagePath, read.entries.front().path) == expected, "ReadEntry returned unexpected bytes");

        phase = "Extract";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        Extract(packagePath, extractRoot);
        const auto extractedPath = extractRoot / ToFsPath("Assets/中文/源文件.bin");
        Require(std::filesystem::is_regular_file(extractedPath), "Extract did not create the entry");
        {
            std::ifstream extracted(extractedPath, std::ios::binary);
            Require(static_cast<bool>(extracted), "cannot open extracted entry");
            const std::vector<uint8_t> actual((std::istreambuf_iterator<char>(extracted)),
                                              std::istreambuf_iterator<char>());
            Require(actual == expected, "Extract returned unexpected bytes");
        }

        phase = "Unicode replacement";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        const auto replacementSource = sourceDirectory / ToFsPath("替换源文件.bin");
        const std::vector<uint8_t> replacementBytes = {0xaa, 0xbb, 0xcc, 0xdd};
        WriteSource(replacementSource, replacementBytes);
        Write(packagePath, {{"Assets/中文/源文件.bin", replacementSource}});
        Require(ReadEntry(packagePath, "Assets/中文/源文件.bin") == replacementBytes,
                "Unicode package replacement did not publish the new package");

#ifdef _WIN32
        phase = "failed replacement preserves old package";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        const auto lockedSource = sourceDirectory / ToFsPath("锁定替换源文件.bin");
        const std::vector<uint8_t> lockedBytes = {0x10, 0x20, 0x30};
        WriteSource(lockedSource, lockedBytes);
        Write(packagePath, {{"Assets/中文/源文件.bin", replacementSource}});
        HANDLE packageLock = ::CreateFileW(packagePath.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        Require(packageLock != INVALID_HANDLE_VALUE, "cannot lock package for replacement test");
        bool replacementFailed = false;
        try {
            Write(packagePath, {{"Assets/中文/源文件.bin", lockedSource}});
        } catch (const std::exception &) {
            replacementFailed = true;
        }
        ::CloseHandle(packageLock);
        Require(replacementFailed, "locked package replacement unexpectedly succeeded");
        Require(ReadEntry(packagePath, "Assets/中文/源文件.bin") == replacementBytes,
                "failed replacement damaged the previous valid package");
#endif

        phase = "concurrent writes";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        constexpr size_t writerCount = 8;
        std::vector<std::filesystem::path> concurrentSources;
        concurrentSources.reserve(writerCount);
        for (size_t index = 0; index < writerCount; ++index) {
            const auto concurrentSource = sourceDirectory / ToFsPath("并发源文件" + std::to_string(index) + ".bin");
            WriteSource(concurrentSource, {static_cast<uint8_t>(index), 0x42, static_cast<uint8_t>(index + 1), 0xfe});
            concurrentSources.push_back(concurrentSource);
        }
        std::mutex failureMutex;
        std::exception_ptr concurrentFailure;
        std::vector<std::thread> writers;
        writers.reserve(writerCount);
        for (const auto &concurrentSource : concurrentSources) {
            writers.emplace_back([&packagePath, &failureMutex, &concurrentFailure, concurrentSource]() {
                try {
                    Write(packagePath, {{"Assets/中文/并发.bin", concurrentSource}});
                } catch (...) {
                    std::lock_guard lock(failureMutex);
                    if (!concurrentFailure)
                        concurrentFailure = std::current_exception();
                }
            });
        }
        for (auto &writer : writers)
            writer.join();
        if (concurrentFailure)
            std::rethrow_exception(concurrentFailure);
        const Manifest concurrentManifest = ReadManifest(packagePath);
        Require(concurrentManifest.entries.size() == 1, "concurrent writes produced an invalid manifest");
        const auto concurrentBytes = ReadEntry(packagePath, "Assets/中文/并发.bin");
        Require(concurrentBytes.size() == 4 && concurrentBytes[1] == 0x42 && concurrentBytes[3] == 0xfe,
                "concurrent writes produced unexpected package contents");
        RequireNoTemporaryFiles(packagePath.parent_path());

        phase = "cleanup";
        std::cerr << "[InxPackTests] " << phase << '\n' << std::flush;
        const std::error_code cleanupError = cleanup();
        if (cleanupError)
            throw std::runtime_error("cleanup failed: " + cleanupError.message());
        return 0;
    } catch (const std::exception &error) {
        cleanup();
        std::cerr << "InxPackTests failed during " << phase << ": " << error.what() << '\n';
        return 1;
    }
}
