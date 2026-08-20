#include <platform/filesystem/InxPack.h>
#include <platform/filesystem/InxPath.h>

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <pybind11/pybind11.h>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#else
#include <signal.h>
#include <sys/types.h>
#endif

namespace py = pybind11;

namespace
{

py::dict InxPackManifestToPython(const infernux::inxpack::Manifest &manifest)
{
    py::dict result;
    result["format"] = "infernux-native-inxpack";
    result["revision"] = manifest.revision;
    result["codec"] = "zstd-or-store";
    result["file_count"] = manifest.entries.size();
    result["raw_bytes"] = manifest.rawBytes;
    result["stored_bytes"] = manifest.storedBytes;
    result["payload_bytes"] = manifest.payloadBytes;
    result["archive_bytes"] = manifest.archiveBytes;
    result["archive_sha256"] = infernux::inxpack::HashToHex(manifest.archiveHash);

    py::list files;
    for (const auto &entry : manifest.entries) {
        py::dict item;
        item["path"] = entry.path;
        item["offset"] = entry.offset;
        item["stored_bytes"] = entry.storedBytes;
        item["raw_bytes"] = entry.rawBytes;
        item["codec"] = infernux::inxpack::CodecName(entry.codec);
        item["sha256"] = infernux::inxpack::HashToHex(entry.hash);
        item["stored_sha256"] = infernux::inxpack::HashToHex(entry.storedHash);
        files.append(std::move(item));
    }
    result["files"] = std::move(files);
    return result;
}

std::vector<std::string> InxPackAllowedRootsFromPython(py::handle value)
{
    if (value.is_none())
        return {};
    if (!py::isinstance<py::sequence>(value) || py::isinstance<py::str>(value))
        throw std::invalid_argument("InxPack allowed_roots must be a sequence or None");

    const py::sequence sequence = py::reinterpret_borrow<py::sequence>(value);
    std::vector<std::string> roots;
    roots.reserve(sequence.size());
    for (const py::handle item : sequence)
        roots.push_back(py::cast<std::string>(item));
    return roots;
}

infernux::inxpack::WriteOptions InxPackWriteOptionsFromPython(py::handle compressionLevel, const std::string &profile)
{
    infernux::inxpack::WriteOptions options;
    if (profile == "development")
        options.profile = infernux::inxpack::CompressionProfile::Development;
    else if (profile == "release")
        options.profile = infernux::inxpack::CompressionProfile::Release;
    else
        throw std::invalid_argument("InxPack compression profile must be 'development' or 'release'");

    if (!compressionLevel.is_none()) {
        options.compressionLevel = py::cast<int>(compressionLevel);
        infernux::inxpack::ValidateCompressionLevel(options.compressionLevel);
    }
    return options;
}

} // namespace

PYBIND11_MODULE(_InfernuxBootstrap, m)
{
    m.def(
        "_inxpack_write",
        [](py::handle sources, const std::string &destination, py::handle compressionLevel,
           const std::string &profile) {
            std::vector<infernux::inxpack::SourceFile> sourceFiles;
            if (!py::isinstance<py::sequence>(sources) || py::isinstance<py::str>(sources))
                throw std::invalid_argument("InxPack sources must be a sequence of (logical_path, source_path) pairs");
            const py::sequence sequence = py::reinterpret_borrow<py::sequence>(sources);
            sourceFiles.reserve(sequence.size());
            for (const py::handle item : sequence) {
                if (!py::isinstance<py::sequence>(item) || py::isinstance<py::str>(item))
                    throw std::invalid_argument("InxPack source entry must be a two-value sequence");
                const py::sequence pair = py::reinterpret_borrow<py::sequence>(item);
                if (pair.size() != 2)
                    throw std::invalid_argument("InxPack source entry must contain logical and filesystem paths");
                sourceFiles.push_back(
                    {py::cast<std::string>(pair[0]), infernux::ToFsPath(py::cast<std::string>(pair[1]))});
            }
            const auto destinationPath = infernux::ToFsPath(destination);
            const auto options = InxPackWriteOptionsFromPython(compressionLevel, profile);
            infernux::inxpack::Manifest manifest;
            {
                py::gil_scoped_release release;
                manifest = infernux::inxpack::Write(destinationPath, std::move(sourceFiles), options);
            }
            return InxPackManifestToPython(manifest);
        },
        py::arg("sources"), py::arg("destination"), py::arg("compression_level") = py::none(),
        py::arg("profile") = "development", "Write the single native InxPack format using Store/Zstandard codecs.");
    m.def(
        "_inxpack_read_manifest",
        [](const std::string &path) {
            return InxPackManifestToPython(infernux::inxpack::ReadManifest(infernux::ToFsPath(path)));
        },
        py::arg("path"), "Read and fully validate a native InxPack manifest.");
    m.def(
        "_inxpack_extract",
        [](const std::string &path, const std::string &destination, py::handle allowedRoots) {
            return InxPackManifestToPython(infernux::inxpack::Extract(infernux::ToFsPath(path),
                                                                      infernux::ToFsPath(destination),
                                                                      InxPackAllowedRootsFromPython(allowedRoots)));
        },
        py::arg("path"), py::arg("destination"), py::arg("allowed_roots") = py::none(),
        "Validate and extract a native InxPack with an optional allowed root filter.");
    m.def(
        "_inxpack_read_entry",
        [](const std::string &path, const std::string &entryPath) {
            const auto bytes = infernux::inxpack::ReadEntry(infernux::ToFsPath(path), entryPath);
            return py::bytes(reinterpret_cast<const char *>(bytes.data()), bytes.size());
        },
        py::arg("path"), py::arg("entry_path"), "Read one validated native InxPack entry.");
    m.def(
        "_inxplayer_show_error",
        [](const py::str &title, const py::str &message) {
#if defined(_WIN32)
            const std::wstring wideTitle = title.cast<std::wstring>();
            const std::wstring wideMessage = message.cast<std::wstring>();
            MessageBoxW(nullptr, wideMessage.c_str(), wideTitle.c_str(), MB_OK | MB_ICONERROR | MB_TASKMODAL);
#else
            const std::string titleText = title.cast<std::string>();
            const std::string messageText = message.cast<std::string>();
            std::fprintf(stderr, "%s: %s\n", titleText.c_str(), messageText.c_str());
            std::fflush(stderr);
#endif
        },
        py::arg("title"), py::arg("message"), "Show a startup error without importing the Python ctypes stack.");
    m.def(
        "_inxplayer_process_is_alive",
        [](const int64_t pid) {
            if (pid <= 0)
                return false;
#if defined(_WIN32)
            if (static_cast<uint64_t>(pid) > std::numeric_limits<DWORD>::max())
                return false;
            const HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, static_cast<DWORD>(pid));
            if (process == nullptr)
                return GetLastError() == ERROR_ACCESS_DENIED;

            DWORD exitCode = 0;
            const BOOL queried = GetExitCodeProcess(process, &exitCode);
            CloseHandle(process);
            return queried == FALSE || exitCode == STILL_ACTIVE;
#else
            if (::kill(static_cast<pid_t>(pid), 0) == 0)
                return true;
            return errno == EPERM;
#endif
        },
        py::arg("pid"), "Return whether a Player cache publisher process is alive.");
}
