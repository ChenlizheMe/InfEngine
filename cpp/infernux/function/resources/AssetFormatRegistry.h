#pragma once

// One native source of truth for source formats that the current runtime can
// actually decode/import.  Keeping this list next to the importer prevents
// the AssetDatabase and Project panel from slowly growing different opinions
// about which files are real assets.

#include <array>
#include <string>
#include <string_view>
#include <vector>

namespace infernux::asset_formats
{

inline constexpr std::array<std::string_view, 15> kTextureExtensions = {
    ".png", ".jpg", ".jpeg", ".jpe", ".bmp", ".tga",       ".gif",    ".psd",
    ".hdr", ".pic", ".pnm",  ".pgm", ".ppm", ".inxvfield", ".inxsdf",
};

// Assimp is the importer for these formats.  The list intentionally contains
// formats implemented by the vendored Assimp build, not merely file suffixes
// that a UI could display.
inline constexpr std::array<std::string_view, 35> kMeshExtensions = {
    ".fbx", ".obj", ".gltf", ".glb",     ".dae", ".3ds", ".ply",  ".stl", ".x",       ".b3d",  ".ase", ".blend",
    ".bvh", ".cob", ".c4d",  ".csm",     ".dxf", ".hmp", ".ifc",  ".iqm", ".irrmesh", ".lwo",  ".lws", ".m3d",
    ".md2", ".md3", ".md4",  ".md5mesh", ".mdc", ".mmd", ".ms3d", ".nff", ".off",     ".ogex", ".x3d",
};

// WAV uses SDL, OGG/Vorbis uses stb_vorbis, and MP3/FLAC use dr_libs.
// Every consumer (database, editor icons and drag payloads) derives from this
// list so a visible AudioClip is always a format the runtime can decode.
inline constexpr std::array<std::string_view, 4> kAudioExtensions = {".wav", ".ogg", ".mp3", ".flac"};

template <size_t N> inline bool Contains(const std::array<std::string_view, N> &extensions, std::string_view ext)
{
    for (const auto candidate : extensions) {
        if (candidate == ext)
            return true;
    }
    return false;
}

template <size_t N> inline std::vector<std::string> ToVector(const std::array<std::string_view, N> &extensions)
{
    std::vector<std::string> result;
    result.reserve(N);
    for (const auto ext : extensions)
        result.emplace_back(ext);
    return result;
}

} // namespace infernux::asset_formats
