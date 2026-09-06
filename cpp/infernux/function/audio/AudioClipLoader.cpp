#include "AudioClipLoader.h"

#include <core/log/InxLog.h>
#include <function/audio/AudioClip.h>

#include <platform/filesystem/InxPath.h>

#include <filesystem>

namespace infernux
{

// =============================================================================
// Load — decode audio file and create a new AudioClip
// =============================================================================

RuntimeAssetPayload AudioClipLoader::Load(const std::string &filePath, const std::string &guid, AssetDatabase * /*adb*/)
{
    if (filePath.empty() || guid.empty()) {
        INXLOG_WARN("AudioClipLoader::Load: empty filePath or guid");
        return nullptr;
    }

    auto fsPath = ToFsPath(filePath);
    if (!std::filesystem::exists(fsPath)) {
        INXLOG_ERROR("AudioClipLoader::Load: file not found: ", filePath);
        return nullptr;
    }

    auto clip = std::make_shared<AudioClip>();
    if (!clip->LoadFromFile(filePath)) {
        INXLOG_ERROR("AudioClipLoader::Load: failed to decode: ", filePath);
        return nullptr;
    }

    clip->SetGuid(guid);

    return clip;
}

// =============================================================================
// Reload — re-decode audio and replace PCM data in-place
// =============================================================================

bool AudioClipLoader::Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                             AssetDatabase * /*adb*/)
{
    auto clip = existing.Get<AudioClip>();
    if (!clip) {
        INXLOG_WARN("AudioClipLoader::Reload: null existing instance");
        return false;
    }

    // Unload current data and reload from file
    clip->Unload();
    if (!clip->LoadFromFile(filePath)) {
        INXLOG_ERROR("AudioClipLoader::Reload: failed to decode: ", filePath);
        return false;
    }

    // Restore authoritative GUID
    clip->SetGuid(guid);

    return true;
}

// =============================================================================
// ScanDependencies — audio clips have no outgoing asset dependencies
// =============================================================================

size_t AudioClipLoader::EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const
{
    const auto clip = payload.Get<AudioClip>();
    if (!clip)
        throw std::invalid_argument("AudioClipLoader cannot estimate an empty runtime payload");
    return clip->GetRuntimeMemoryBytes();
}

std::set<std::string> AudioClipLoader::ScanDependencies(const std::string & /*filePath*/, AssetDatabase * /*adb*/)
{
    return {};
}

// =============================================================================
// CreateMeta — audio-specific .meta creation
// =============================================================================

void AudioClipLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                                 InxResourceMeta &metaData) const
{
    metaData.Init(content, contentSize, filePath, ResourceType::Audio);

    std::filesystem::path path = ToFsPath(filePath);
    std::string resourceName = FromFsPath(path.stem());

    metaData.AddMetadata("resource_name", resourceName);
    metaData.AddMetadata("file_size", static_cast<int>(contentSize));
    metaData.AddMetadata("file_type", std::string("audio"));
    metaData.AddMetadata("extension", FromFsPath(path.extension()));
}

} // namespace infernux
