#include "AudioSource.h"
#include "AudioEngine.h"
#include <core/log/InxLog.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/scene/ComponentDocumentValidation.h>
#include <function/scene/ComponentFactory.h>
#include <function/scene/GameObject.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace infernux
{

// Register AudioSource with ComponentFactory so it can be created by type name
INFERNUX_REGISTER_VALIDATED_COMPONENT("AudioSource", AudioSource)

AudioSource::AudioSource()
{
    // Default: 1 track
    m_tracks.resize(1);
    m_oneShotVoices.resize(static_cast<size_t>(m_oneShotPoolSize));
}

AudioSource::~AudioSource()
{
    StopAll();
    AudioEngine::Instance().UnregisterSource(this);
}

void AudioSource::Awake()
{
    // Register with AudioEngine for spatial updates
    AudioEngine::Instance().RegisterSource(this);
}

void AudioSource::Start()
{
    if (m_playOnAwake && !m_tracks.empty() && m_tracks[0].clip && m_tracks[0].clip->IsLoaded()) {
        Play(0);
    }
}

void AudioSource::OnEnable()
{
    AudioEngine::Instance().RegisterSource(this);

    for (int i = 0; i < static_cast<int>(m_tracks.size()); ++i) {
        auto &track = m_tracks[i];
        if (track.stream && track.isPlaying && track.isPaused && track.pauseRequestedByDisable) {
            track.pauseRequestedByDisable = false;
            track.isPaused = false;
            AudioEngine::Instance().SetVoicePaused(track.stream, false);
            ApplyTrackGain(i);
        }
    }

    for (int i = 0; i < static_cast<int>(m_oneShotVoices.size()); ++i) {
        auto &voice = m_oneShotVoices[i];
        if (voice.stream && voice.isPaused && voice.pauseRequestedByDisable) {
            voice.pauseRequestedByDisable = false;
            voice.isPaused = false;
            AudioEngine::Instance().SetVoicePaused(voice.stream, false);
            ApplyOneShotGain(i);
        }
    }
}

void AudioSource::OnDisable()
{
    // Pause all playing tracks when component is disabled
    for (int i = 0; i < static_cast<int>(m_tracks.size()); ++i) {
        if (m_tracks[i].isPlaying && !m_tracks[i].isPaused) {
            m_tracks[i].pauseRequestedByDisable = true;
            Pause(i);
        }
    }

    for (int i = 0; i < static_cast<int>(m_oneShotVoices.size()); ++i) {
        auto &voice = m_oneShotVoices[i];
        if (voice.stream && !voice.isPaused) {
            voice.pauseRequestedByDisable = true;
            voice.isPaused = true;
            AudioEngine::Instance().SetVoicePaused(voice.stream, true);
        }
    }

    AudioEngine::Instance().UnregisterSource(this);
}

void AudioSource::OnDestroy()
{
    StopAll();
    AudioEngine::Instance().UnregisterSource(this);
}

void AudioSource::Update(float /*deltaTime*/)
{
    for (int i = 0; i < static_cast<int>(m_tracks.size()); ++i) {
        auto &track = m_tracks[i];
        if (!track.isPlaying || track.isPaused || !track.stream) {
            continue;
        }

        if (!m_loop && AudioEngine::Instance().HasVoiceFinished(track.stream)) {
            StopVoice(i);
        }
    }

    for (int i = 0; i < static_cast<int>(m_oneShotVoices.size()); ++i) {
        auto &voice = m_oneShotVoices[i];
        if (!voice.stream || voice.isPaused) {
            continue;
        }

        if (AudioEngine::Instance().HasVoiceFinished(voice.stream)) {
            StopOneShotVoice(i);
        }
    }
}

// ============================================================================
// Serialization
// ============================================================================

nlohmann::json AudioSource::SerializeDocument() const
{
    json j = Component::SerializeDocument();
    j["volume"] = m_volume;
    j["pitch"] = m_pitch;
    j["loop"] = m_loop;
    j["play_on_awake"] = m_playOnAwake;
    j["mute"] = m_mute;
    j["min_distance"] = m_minDistance;
    j["max_distance"] = m_maxDistance;
    j["one_shot_pool_size"] = m_oneShotPoolSize;
    j["output_bus"] = m_outputBus;
    j["track_count"] = static_cast<int>(m_tracks.size());

    // Serialize per-track data
    json tracksJson = json::array();
    for (const auto &track : m_tracks) {
        json tj;
        tj["volume"] = track.volume;
        if (track.clip) {
            tj["clip_guid"] = track.clip->GetGuid();
        }
        tracksJson.push_back(tj);
    }
    j["tracks"] = tracksJson;

    return j;
}

void AudioSource::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "AudioSource",
                              {"volume", "pitch", "loop", "play_on_awake", "mute", "min_distance", "max_distance",
                               "one_shot_pool_size", "output_bus", "track_count", "tracks"});
    const float volume = RequireFiniteFloat(j, "volume", "AudioSource");
    const float pitch = RequireFiniteFloat(j, "pitch", "AudioSource");
    RequireBoolean(j, "loop", "AudioSource");
    RequireBoolean(j, "play_on_awake", "AudioSource");
    RequireBoolean(j, "mute", "AudioSource");
    const float minDistance = RequireFiniteFloat(j, "min_distance", "AudioSource");
    const float maxDistance = RequireFiniteFloat(j, "max_distance", "AudioSource");
    const int poolSize = RequireInteger(j, "one_shot_pool_size", "AudioSource");
    RequireString(j, "output_bus", "AudioSource");
    const int trackCount = RequireInteger(j, "track_count", "AudioSource");
    const auto &tracks = j["tracks"];
    if (!tracks.is_array() || trackCount < 1 || tracks.size() != static_cast<size_t>(trackCount))
        throw std::invalid_argument("AudioSource.tracks must match positive track_count");
    if (volume < 0.0f || volume > 1.0f || pitch < 0.1f || pitch > 3.0f)
        throw std::invalid_argument("AudioSource volume or pitch is out of range");
    if (minDistance <= 0.0f || maxDistance < minDistance || poolSize < 1)
        throw std::invalid_argument("AudioSource distance or pool settings are invalid");

    for (size_t index = 0; index < tracks.size(); ++index) {
        const auto &track = tracks[index];
        if (!track.is_object())
            throw std::invalid_argument("AudioSource.tracks[" + std::to_string(index) + "] must be an object");
        for (const auto &[key, value] : track.items()) {
            (void)value;
            if (key != "volume" && key != "clip_guid")
                throw std::invalid_argument("AudioSource.tracks[" + std::to_string(index) +
                                            "] contains unknown field '" + key + "'");
        }
        if (!track.contains("volume") || !track["volume"].is_number())
            throw std::invalid_argument("AudioSource track volume is required and must be numeric");
        const double trackVolume = track["volume"].get<double>();
        if (!std::isfinite(trackVolume) || trackVolume < 0.0 || trackVolume > 1.0)
            throw std::invalid_argument("AudioSource track volume is out of range");
        if (track.contains("clip_guid")) {
            if (!track["clip_guid"].is_string() || track["clip_guid"].get_ref<const std::string &>().empty())
                throw std::invalid_argument("AudioSource track clip_guid must be a non-empty string");
        }
    }
}

bool AudioSource::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocument(j);

        auto &registry = AssetRegistry::Instance();
        const auto &tracksJson = j["tracks"];
        std::vector<std::shared_ptr<AudioClip>> stagedClips(tracksJson.size());
        for (size_t index = 0; index < tracksJson.size(); ++index) {
            if (!tracksJson[index].contains("clip_guid"))
                continue;
            const std::string guid = tracksJson[index]["clip_guid"].get<std::string>();
            stagedClips[index] = registry.LoadAsset<AudioClip>(guid, ResourceType::Audio);
            if (!stagedClips[index])
                throw std::invalid_argument("audio clip GUID cannot be resolved: " + guid);
        }

        if (!Component::DeserializeDocument(j))
            return false;

        m_volume = j["volume"].get<float>();
        m_pitch = j["pitch"].get<float>();
        m_loop = j["loop"].get<bool>();
        m_playOnAwake = j["play_on_awake"].get<bool>();
        m_mute = j["mute"].get<bool>();
        m_minDistance = j["min_distance"].get<float>();
        m_maxDistance = j["max_distance"].get<float>();
        m_oneShotPoolSize = j["one_shot_pool_size"].get<int>();
        m_outputBus = j["output_bus"].get<std::string>();

        // Deserialize tracks
        const int trackCount = j["track_count"].get<int>();
        SetTrackCount(trackCount);

        for (int i = 0; i < trackCount; ++i) {
            const auto &tj = tracksJson[i];
            m_tracks[i].volume = tj["volume"].get<float>();
            m_tracks[i].clip = std::move(stagedClips[i]);
        }

        SetVolume(m_volume);
        SetPitch(m_pitch);
        SetMinDistance(m_minDistance);
        SetMaxDistance(m_maxDistance);
        SetOneShotPoolSize(m_oneShotPoolSize);
        for (int i = 0; i < static_cast<int>(m_tracks.size()); ++i) {
            SetTrackVolume(i, m_tracks[i].volume);
        }

        return true;
    } catch (const std::exception &e) {
        INXLOG_WARN("AudioSource::Deserialize failed: ", e.what());
        return false;
    }
}

// ============================================================================
// Track management
// ============================================================================

void AudioSource::SetTrackCount(int count)
{
    if (count < 1) {
        INXLOG_WARN("AudioSource::SetTrackCount: track_count must be >= 1. Clamping ", count, " to 1.");
        count = 1;
    }
    int oldCount = static_cast<int>(m_tracks.size());

    // Stop voices for tracks that are being removed
    for (int i = count; i < oldCount; ++i) {
        StopVoice(i);
    }

    m_tracks.resize(count);
}

void AudioSource::SetTrackClip(int trackIndex, std::shared_ptr<AudioClip> clip)
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        INXLOG_WARN("AudioSource::SetTrackClip: track index ", trackIndex, " out of range");
        return;
    }

    // Stop current playback on this track
    if (m_tracks[trackIndex].isPlaying) {
        StopVoice(trackIndex);
    }
    m_tracks[trackIndex].clip = std::move(clip);
}

std::shared_ptr<AudioClip> AudioSource::GetTrackClip(int trackIndex) const
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return nullptr;
    }
    return m_tracks[trackIndex].clip;
}

void AudioSource::SetTrackVolume(int trackIndex, float volume)
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return;
    }
    const float clamped = std::clamp(volume, 0.0f, 1.0f);
    if (clamped != volume) {
        INXLOG_DEBUG("AudioSource::SetTrackVolume: volume must be in [0, 1]. Clamping ", volume, " to ", clamped, ".");
    }
    m_tracks[trackIndex].volume = clamped;
    ApplyTrackGain(trackIndex);
}

float AudioSource::GetTrackVolume(int trackIndex) const
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return 0.0f;
    }
    return m_tracks[trackIndex].volume;
}

// ============================================================================
// Playback control
// ============================================================================

void AudioSource::Play(int trackIndex)
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        INXLOG_WARN("AudioSource::Play: track index ", trackIndex, " out of range");
        return;
    }

    auto &track = m_tracks[trackIndex];
    if (!track.clip || !track.clip->IsLoaded()) {
        INXLOG_WARN("AudioSource::Play: no clip loaded on track ", trackIndex);
        return;
    }

    // Stop any existing playback on this track
    StopVoice(trackIndex);

    // Start new voice
    StartVoice(trackIndex);
}

void AudioSource::Stop(int trackIndex)
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return;
    }
    StopVoice(trackIndex);
}

void AudioSource::Pause(int trackIndex)
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return;
    }
    auto &track = m_tracks[trackIndex];
    if (track.isPlaying && !track.isPaused && track.stream) {
        track.isPaused = true;
        track.pauseRequestedByDisable = false;
        AudioEngine::Instance().SetVoicePaused(track.stream, true);
    }
}

void AudioSource::UnPause(int trackIndex)
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return;
    }
    auto &track = m_tracks[trackIndex];
    if (track.isPlaying && track.isPaused && track.stream) {
        track.pauseRequestedByDisable = false;
        track.isPaused = false;
        AudioEngine::Instance().SetVoicePaused(track.stream, false);
        ApplyTrackGain(trackIndex);
    }
}

void AudioSource::StopAll()
{
    for (int i = 0; i < static_cast<int>(m_tracks.size()); ++i) {
        StopVoice(i);
    }
    StopOneShots();
}

void AudioSource::PlayOneShot(std::shared_ptr<AudioClip> clip, float volumeScale)
{
    if (!clip || !clip->IsLoaded()) {
        INXLOG_WARN("AudioSource::PlayOneShot: clip is null or not loaded.");
        return;
    }

    const float clampedVolumeScale = std::clamp(volumeScale, 0.0f, 1.0f);
    if (clampedVolumeScale != volumeScale) {
        INXLOG_DEBUG("AudioSource::PlayOneShot: volumeScale must be in [0, 1]. Clamping ", volumeScale, " to ",
                     clampedVolumeScale, ".");
    }

    if (m_oneShotVoices.empty()) {
        SetOneShotPoolSize(m_oneShotPoolSize);
    }

    int selectedVoice = -1;
    uint64_t oldestPlayOrder = std::numeric_limits<uint64_t>::max();
    for (int i = 0; i < static_cast<int>(m_oneShotVoices.size()); ++i) {
        const auto &voice = m_oneShotVoices[i];
        if (!voice.stream) {
            selectedVoice = i;
            break;
        }

        if (voice.playOrder < oldestPlayOrder) {
            oldestPlayOrder = voice.playOrder;
            selectedVoice = i;
        }
    }

    if (selectedVoice < 0) {
        INXLOG_WARN("AudioSource::PlayOneShot: one-shot pool is empty after allocation attempt.");
        return;
    }

    if (m_oneShotVoices[selectedVoice].stream) {
        INXLOG_WARN("AudioSource::PlayOneShot: one-shot pool exhausted. Reusing the oldest pooled voice.");
        StopOneShotVoice(selectedVoice);
    }

    StartOneShotVoice(selectedVoice, std::move(clip), clampedVolumeScale);
}

void AudioSource::StopOneShots()
{
    for (int i = 0; i < static_cast<int>(m_oneShotVoices.size()); ++i) {
        StopOneShotVoice(i);
    }
}

bool AudioSource::IsTrackPlaying(int trackIndex) const
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return false;
    }
    return m_tracks[trackIndex].isPlaying && !m_tracks[trackIndex].isPaused;
}

bool AudioSource::IsTrackPaused(int trackIndex) const
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return false;
    }
    return m_tracks[trackIndex].isPaused;
}

// ============================================================================
// Source-level properties
// ============================================================================

void AudioSource::SetVolume(float volume)
{
    const float clamped = std::clamp(volume, 0.0f, 1.0f);
    if (clamped != volume) {
        INXLOG_DEBUG("AudioSource::SetVolume: volume must be in [0, 1]. Clamping ", volume, " to ", clamped, ".");
    }
    m_volume = clamped;
    ApplyAllTrackGains();
}

void AudioSource::SetPitch(float pitch)
{
    const float clamped = std::clamp(pitch, 0.1f, 3.0f);
    if (clamped != pitch) {
        INXLOG_DEBUG("AudioSource::SetPitch: pitch must be in [0.1, 3.0]. Clamping ", pitch, " to ", clamped, ".");
    }
    m_pitch = clamped;
    ApplyAllTrackGains();
}

void AudioSource::SetMute(bool mute)
{
    m_mute = mute;
    ApplyAllTrackGains();
}

void AudioSource::SetMinDistance(float dist)
{
    const float clamped = std::max(0.001f, dist);
    if (clamped != dist) {
        INXLOG_DEBUG("AudioSource::SetMinDistance: min_distance must be > 0. Clamping ", dist, " to ", clamped, ".");
    }
    m_minDistance = clamped;
    if (m_maxDistance < m_minDistance) {
        INXLOG_DEBUG(
            "AudioSource::SetMinDistance: max_distance was smaller than min_distance. Raising max_distance to ",
            m_minDistance, ".");
        m_maxDistance = m_minDistance;
    }
    ApplyAllTrackGains();
}

void AudioSource::SetMaxDistance(float dist)
{
    const float clamped = std::max(dist, m_minDistance);
    if (clamped != dist) {
        INXLOG_DEBUG("AudioSource::SetMaxDistance: max_distance must be >= min_distance. Clamping ", dist, " to ",
                     clamped, ".");
    }
    m_maxDistance = clamped;
    ApplyAllTrackGains();
}

void AudioSource::SetOneShotPoolSize(int size)
{
    if (size < 1) {
        INXLOG_DEBUG("AudioSource::SetOneShotPoolSize: pool size must be >= 1. Clamping ", size, " to 1.");
        size = 1;
    }

    const int oldSize = static_cast<int>(m_oneShotVoices.size());
    for (int i = size; i < oldSize; ++i) {
        StopOneShotVoice(i);
    }

    m_oneShotPoolSize = size;
    m_oneShotVoices.resize(static_cast<size_t>(m_oneShotPoolSize));
}

uint64_t AudioSource::GetGameObjectId() const
{
    auto *go = GetGameObject();
    return go ? go->GetID() : 0;
}

// ============================================================================
// Spatial audio helpers
// ============================================================================

std::vector<SDL_AudioStream *> AudioSource::GetActiveStreams() const
{
    std::vector<SDL_AudioStream *> streams;
    for (const auto &track : m_tracks) {
        if (track.stream && track.isPlaying) {
            streams.push_back(track.stream);
        }
    }
    for (const auto &voice : m_oneShotVoices) {
        if (voice.stream) {
            streams.push_back(voice.stream);
        }
    }
    return streams;
}

void AudioSource::ApplyAllTrackGains()
{
    for (int i = 0; i < static_cast<int>(m_tracks.size()); ++i) {
        ApplyTrackGain(i);
    }
    for (int i = 0; i < static_cast<int>(m_oneShotVoices.size()); ++i) {
        ApplyOneShotGain(i);
    }
}

void AudioSource::ApplyTrackGain(int trackIndex)
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return;
    }
    auto &track = m_tracks[trackIndex];
    if (!track.stream || !track.isPlaying) {
        return;
    }
    if (track.isPaused) {
        AudioEngine::Instance().UpdateVoiceMix(track.stream, 0.0f, m_pan, m_pitch, m_loop);
        return;
    }

    float gain = m_mute ? 0.0f : (m_volume * track.volume * m_spatialGain);
    AudioEngine::Instance().UpdateVoiceMix(track.stream, gain, m_pan, m_pitch, m_loop);
}

void AudioSource::ApplyOneShotGain(int voiceIndex)
{
    if (voiceIndex < 0 || voiceIndex >= static_cast<int>(m_oneShotVoices.size())) {
        return;
    }

    auto &voice = m_oneShotVoices[voiceIndex];
    if (!voice.stream) {
        return;
    }

    if (voice.isPaused) {
        AudioEngine::Instance().UpdateVoiceMix(voice.stream, 0.0f, m_pan, m_pitch, false);
        return;
    }

    const float gain = m_mute ? 0.0f : (m_volume * voice.volumeScale * m_spatialGain);
    AudioEngine::Instance().UpdateVoiceMix(voice.stream, gain, m_pan, m_pitch, false);
}

// ============================================================================
// Internal voice management
// ============================================================================

void AudioSource::StartVoice(int trackIndex)
{
    auto &engine = AudioEngine::Instance();
    if (!engine.IsInitialized()) {
        INXLOG_WARN("AudioSource::StartVoice: AudioEngine not initialized");
        return;
    }

    auto &track = m_tracks[trackIndex];
    track.stream = engine.CreateVoice(this, track.clip.get());
    if (!track.stream) {
        INXLOG_ERROR("AudioSource::StartVoice: failed to create voice for track ", trackIndex);
        return;
    }

    track.isPlaying = true;
    track.isPaused = false;
    track.pauseRequestedByDisable = false;
    AudioEngine::Instance().SetVoicePaused(track.stream, false);
    ApplyTrackGain(trackIndex);
}

void AudioSource::StopVoice(int trackIndex)
{
    if (trackIndex < 0 || trackIndex >= static_cast<int>(m_tracks.size())) {
        return;
    }
    auto &track = m_tracks[trackIndex];
    if (track.stream) {
        if (AudioEngine::Instance().IsInitialized()) {
            AudioEngine::Instance().DestroyVoice(track.stream);
        }
        track.stream = nullptr;
    }
    track.isPlaying = false;
    track.isPaused = false;
    track.pauseRequestedByDisable = false;
}

void AudioSource::StartOneShotVoice(int voiceIndex, std::shared_ptr<AudioClip> clip, float volumeScale)
{
    auto &engine = AudioEngine::Instance();
    if (!engine.IsInitialized()) {
        INXLOG_WARN("AudioSource::StartOneShotVoice: AudioEngine not initialized");
        return;
    }

    if (voiceIndex < 0 || voiceIndex >= static_cast<int>(m_oneShotVoices.size())) {
        return;
    }

    auto &voice = m_oneShotVoices[voiceIndex];
    voice.clip = std::move(clip);
    voice.volumeScale = volumeScale;
    voice.playOrder = m_nextOneShotPlayOrder++;
    voice.stream = engine.CreateVoice(this, voice.clip.get());
    if (!voice.stream) {
        INXLOG_ERROR("AudioSource::StartOneShotVoice: failed to create pooled voice ", voiceIndex);
        voice.clip.reset();
        voice.volumeScale = 1.0f;
        voice.playOrder = 0;
        return;
    }

    voice.isPaused = false;
    voice.pauseRequestedByDisable = false;
    engine.SetVoicePaused(voice.stream, false);
    ApplyOneShotGain(voiceIndex);
}

void AudioSource::StopOneShotVoice(int voiceIndex)
{
    if (voiceIndex < 0 || voiceIndex >= static_cast<int>(m_oneShotVoices.size())) {
        return;
    }

    auto &voice = m_oneShotVoices[voiceIndex];
    if (voice.stream) {
        if (AudioEngine::Instance().IsInitialized()) {
            AudioEngine::Instance().DestroyVoice(voice.stream);
        }
        voice.stream = nullptr;
    }

    voice.clip.reset();
    voice.isPaused = false;
    voice.pauseRequestedByDisable = false;
    voice.volumeScale = 1.0f;
    voice.playOrder = 0;
}

void AudioSource::CheckLooping(int trackIndex)
{
    (void)trackIndex;
}

void AudioSource::NotifyAudioEngineShutdown()
{
    for (auto &track : m_tracks) {
        track.stream = nullptr;
        track.isPlaying = false;
        track.isPaused = false;
        track.pauseRequestedByDisable = false;
    }

    for (auto &voice : m_oneShotVoices) {
        voice.stream = nullptr;
        voice.clip.reset();
        voice.isPaused = false;
        voice.pauseRequestedByDisable = false;
        voice.volumeScale = 1.0f;
        voice.playOrder = 0;
    }
}

} // namespace infernux
