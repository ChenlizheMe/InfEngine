#include "InxDefaultLoader.hpp"

#include <function/resources/AssetFormatRegistry.h>

#include <algorithm>
#include <core/log/InxLog.h>
#include <filesystem>
#include <fstream>
#include <platform/filesystem/InxPath.h>
#include <set>
#include <sstream>
#include <vector>

namespace infernux
{
InxDefaultTextLoader::InxDefaultTextLoader(ResourceType resourceType) : m_resourceType(resourceType)
{
    if (resourceType != ResourceType::DefaultText && resourceType != ResourceType::RenderEffect &&
        resourceType != ResourceType::ParticleGraph)
        throw std::invalid_argument("InxDefaultTextLoader requires a text resource type");
}

void InxDefaultTextLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                                      InxResourceMeta &metaData) const
{
    INXLOG_DEBUG("Creating metadata for text file: ", filePath);
    metaData.Init(content, contentSize, filePath, m_resourceType);

    std::filesystem::path path = ToFsPath(filePath);
    std::string extension = FromFsPath(path.extension());

    // For text files, analyze content
    std::string contentStr;
    if (content && contentSize > 0) {
        contentStr.assign(content, contentSize);
    }
    size_t lineCount = std::count(contentStr.begin(), contentStr.end(), '\n') + 1;

    // Add metadata specific to text files
    metaData.AddMetadata("file_type", std::string("text"));
    metaData.AddMetadata("file_extension", extension);
    metaData.AddMetadata("is_readable", true);
    metaData.AddMetadata("line_count", lineCount);
    metaData.AddMetadata("character_count", contentStr.length());

    // Check encoding (simple detection)
    bool hasNonAscii = std::any_of(contentStr.begin(), contentStr.end(), [](unsigned char c) { return c > 127; });
    metaData.AddMetadata("encoding", hasNonAscii ? std::string("utf-8") : std::string("ascii"));

    metaData.AddMetadata("file_size", contentSize);

    INXLOG_DEBUG("Text file metadata created for ", filePath, " lines: ", lineCount, " chars: ", contentStr.length());
}

// ================== InxDefaultBinaryLoader Implementation ==================

InxDefaultBinaryLoader::InxDefaultBinaryLoader()
{
}

void InxDefaultBinaryLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                                        InxResourceMeta &metaData) const
{
    metaData.Init(content, contentSize, filePath, ResourceType::DefaultBinary);

    std::filesystem::path path = ToFsPath(filePath);
    std::string extension = FromFsPath(path.extension());

    // Add basic metadata for binary files
    metaData.AddMetadata("file_type", std::string("binary"));
    metaData.AddMetadata("file_extension", extension);
    metaData.AddMetadata("is_readable", false);

    // Determine binary type based on extension only (no content analysis needed)
    std::string binaryType = GetBinaryTypeFromExtension(extension);
    metaData.AddMetadata("binary_type", binaryType);

    metaData.AddMetadata("file_size", contentSize);
    std::string sizeCategory;
    if (contentSize < 1024) {
        sizeCategory = "tiny";
    } else if (contentSize < 1024 * 1024) {
        sizeCategory = "small";
    } else if (contentSize < 10 * 1024 * 1024) {
        sizeCategory = "medium";
    } else {
        sizeCategory = "large";
    }
    metaData.AddMetadata("size_category", sizeCategory);

    INXLOG_DEBUG("Binary file metadata created for ", filePath, ", type: ", binaryType);
}

std::string InxDefaultBinaryLoader::GetBinaryTypeFromExtension(const std::string &extension) const
{
    // Convert extension to lowercase
    std::string ext = extension;
    std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);

    // Check common binary types by extension
    if (ext == ".exe" || ext == ".dll" || ext == ".so" || ext == ".dylib") {
        return "executable";
    } else if (ext == ".jpg" || ext == ".jpeg" || ext == ".png" || ext == ".gif" || ext == ".bmp" || ext == ".tiff" ||
               ext == ".ico" || ext == ".webp") {
        return "image";
    } else if (asset_formats::Contains(asset_formats::kAudioExtensions, ext)) {
        return "audio";
    } else if (ext == ".mp4" || ext == ".avi" || ext == ".mkv" || ext == ".mov" || ext == ".wmv" || ext == ".flv" ||
               ext == ".webm") {
        return "video";
    } else if (ext == ".zip" || ext == ".rar" || ext == ".7z" || ext == ".tar" || ext == ".gz" || ext == ".bz2" ||
               ext == ".xz") {
        return "archive";
    } else if (ext == ".pdf") {
        return "document";
    } else if (ext == ".ttf" || ext == ".otf" || ext == ".woff" || ext == ".woff2" || ext == ".eot") {
        return "font";
    }

    return "binary";
}

} // namespace infernux
