#include "TextureLoader.h"

#include <core/log/InxLog.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/InxFileLoader/InxTextureLoader.hpp>
#include <function/resources/InxTexture/InxTexture.h>
#include <function/resources/InxTexture/TextureArtifact.h>
#include <function/resources/InxTexture/TextureDecoder.h>

#include <platform/filesystem/InxPath.h>
#include <stb_image.h>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>

namespace infernux
{

// =============================================================================
// CreateMeta — texture-specific .meta creation (dimensions, format)
// =============================================================================

void TextureLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                               InxResourceMeta &metaData) const
{
    metaData.Init(content, contentSize, filePath, ResourceType::Texture);

    std::filesystem::path path = ToFsPath(filePath);
    std::string extension = FromFsPath(path.extension());
    std::transform(extension.begin(), extension.end(), extension.begin(), ::tolower);
    const bool isTextVolume = extension == ".inxvfield" || extension == ".inxsdf";

    // Get image dimensions without fully loading the pixel data
    int width = 0, height = 0, channels = 0;
    const auto *fileBytes = reinterpret_cast<const unsigned char *>(content);
    if (!isTextVolume && fileBytes && contentSize > 0 &&
        contentSize <= static_cast<size_t>(std::numeric_limits<int>::max()) &&
        stbi_info_from_memory(fileBytes, static_cast<int>(contentSize), &width, &height, &channels)) {
        metaData.AddMetadata("width", width);
        metaData.AddMetadata("height", height);
        metaData.AddMetadata("channels", channels);
    } else if (!isTextVolume && fileBytes && contentSize > 0) {
        InxTextureData pnmInfo = InxTextureLoader::LoadFromMemory(fileBytes, contentSize, filePath);
        if (pnmInfo.IsValid()) {
            metaData.AddMetadata("width", pnmInfo.width);
            metaData.AddMetadata("height", pnmInfo.height);
            metaData.AddMetadata("channels", pnmInfo.channels);
        }
    }

    metaData.AddMetadata("file_type", std::string("texture"));
    metaData.AddMetadata("file_extension", extension);

    static const std::unordered_map<std::string, std::string> formatMap = {
        {".png", "PNG"},
        {".jpg", "JPEG"},
        {".jpeg", "JPEG"},
        {".bmp", "BMP"},
        {".tga", "TGA"},
        {".gif", "GIF"},
        {".psd", "PSD"},
        {".hdr", "HDR"},
        {".pic", "PIC"},
        {".pnm", "PNM"},
        {".pgm", "PGM"},
        {".ppm", "PPM"},
        {".inxvfield", "Infernux Vector Field"},
        {".inxsdf", "Infernux Signed Distance Field"},
    };
    auto fmtIt = formatMap.find(extension);
    metaData.AddMetadata("source_container", fmtIt != formatMap.end() ? fmtIt->second : std::string("Unknown"));
    metaData.AddMetadata("is_binary", !isTextVolume);

    metaData.AddMetadata("file_size", contentSize);
}

// =============================================================================
// Load — create an InxTexture with import settings from .meta
// =============================================================================

RuntimeAssetPayload TextureLoader::Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb)
{
    if (!adb)
        throw std::invalid_argument("TextureLoader requires an AssetDatabase");
    if (filePath.empty() || guid.empty())
        throw std::invalid_argument("TextureLoader requires a path and GUID");
    const auto metadata = adb->GetMetaByGuid(guid);
    if (!metadata)
        throw std::invalid_argument("TextureLoader could not resolve metadata for GUID: " + guid);
    if (!metadata->HasKey("content_hash"))
        throw std::invalid_argument("TextureLoader metadata has no source content hash");

    std::shared_ptr<const TextureCpuData> cpuData;
    const std::string sourceHash = metadata->GetDataAs<std::string>("content_hash");
    const std::string artifactPath = adb->GetRuntimeArtifactPath(guid, ResourceType::Texture);
    if (!artifactPath.empty() && std::filesystem::is_regular_file(ToFsPath(artifactPath))) {
        try {
            std::ifstream artifactFile(ToFsPath(artifactPath), std::ios::binary | std::ios::ate);
            if (!artifactFile.is_open())
                throw std::runtime_error("failed to open texture artifact");
            const auto artifactSize = artifactFile.tellg();
            if (artifactSize <= 0)
                throw std::runtime_error("texture artifact is empty");
            std::string bytes(static_cast<size_t>(artifactSize), '\0');
            artifactFile.seekg(0);
            artifactFile.read(bytes.data(), artifactSize);
            if (!artifactFile)
                throw std::runtime_error("failed to read texture artifact");
            cpuData = TextureArtifact::Deserialize(bytes, sourceHash);
        } catch (const std::exception &exception) {
            INXLOG_WARN("TextureLoader: rejected derived artifact for '", filePath, "': ", exception.what(),
                        "; falling back to source decode");
        }
    }
    if (!cpuData)
        cpuData = TextureDecoder::Decode(filePath, *metadata);

    auto texture = std::make_shared<InxTexture>();
    texture->SetGuid(guid);
    texture->SetFilePath(filePath);
    texture->SetName(FromFsPath(ToFsPath(filePath).stem()));

    texture->ApplyImportSettings(*metadata);
    texture->SetCpuData(std::move(cpuData));

    return texture;
}

// =============================================================================
// Reload — refresh import settings in-place (pointer identity preserved)
// =============================================================================

bool TextureLoader::Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                           AssetDatabase *adb)
{
    auto tex = existing.Get<InxTexture>();
    if (!tex) {
        INXLOG_WARN("TextureLoader::Reload: null existing instance");
        return false;
    }

    const auto freshPayload = Load(filePath, guid, adb);
    const auto fresh = freshPayload.Get<InxTexture>();
    tex->SetFilePath(fresh->GetFilePath());
    tex->SetName(fresh->GetName());
    tex->SetTextureType(fresh->GetTextureType());
    tex->SetSrgb(fresh->IsSrgb());
    tex->SetGenerateMipmaps(fresh->GenerateMipmaps());
    tex->SetMaxSize(fresh->GetMaxSize());
    tex->SetFilterMode(fresh->GetFilterMode());
    tex->SetWrapMode(fresh->GetWrapMode());
    tex->SetAnisoLevel(fresh->GetAnisoLevel());
    tex->SetCpuData(fresh->GetCpuData());

    INXLOG_INFO("TextureLoader: reloaded '", tex->GetName(), "' in-place (GUID: ", guid, ")");
    return true;
}

// =============================================================================
// ScanDependencies — textures have no outgoing dependencies
// =============================================================================

size_t TextureLoader::EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const
{
    const auto texture = payload.Get<InxTexture>();
    if (!texture)
        throw std::invalid_argument("TextureLoader cannot estimate an empty runtime payload");
    return texture->GetRuntimeMemoryBytes();
}

std::set<std::string> TextureLoader::ScanDependencies(const std::string & /*filePath*/, AssetDatabase * /*adb*/)
{
    return {};
}

} // namespace infernux
