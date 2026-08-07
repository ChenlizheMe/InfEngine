#pragma once

#include "AudioClip.h"
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetRef.h>
#include <function/scene/Component.h>

#include <SDL3/SDL_audio.h>
#include <memory>
#include <string>
#include <vector>

namespace infernux
{

/**
 * @brief A single audio track within an AudioSource.
 *
 * Each track has its own clip, stream, playback state, and per-track volume.
 * All tracks in a source share the source-level settings (spatial, mute, pitch).
 */
struct AudioTrack
{
    AssetRef<AudioClip> clipAsset;
    std::shared_ptr<AudioClip> transientClip;
    SDL_AudioStream *stream = nullptr;
    bool isPlaying = false;
    bool isPaused = false;
    bool pauseRequestedByDisable = false;
    float volume = 1.0f; ///< Per-track volume multiplier (0.0–1.0)

    [[nodiscard]] std::shared_ptr<AudioClip> GetClip() const
    {
        const auto asset = clipAsset.Get();
        return asset ? asset : transientClip;
    }
};

struct AudioOneShotVoice
{
    std::shared_ptr<AudioClip> clip;
    SDL_AudioStream *stream = nullptr;
    bool isPaused = false;
    bool pauseRequestedByDisable = false;
    float volumeScale = 1.0f;
    uint64_t playOrder = 0;
};

/**
 * @brief AudioSource component — plays audio in the scene.
 *
 * Attached to a GameObject, AudioSource provides Unity-aligned playback
 * control with multi-track support. Unlike Unity (one clip per source),
 * Infernux's AudioSource supports multiple tracks that share the same
 * spatial and source-level settings (volume, mute, pitch, 3D attenuation).
 *
 * Multi-track design:
 * - track_count      → Get/SetTrackCount(int)  — number of tracks (default 1)
 * - set_track_clip   → SetTrackClip(int, clip)  — assign clip to a track
 * - play(track)      → Play(int)               — play a specific track
 * - stop(track)      → Stop(int)               — stop a specific track
 *
 * Convenience: Play() / Get/SetClip() default to track 0.
 *
 * All-3D approach: every source is spatialised.  For "2D" audio,
 * attach the AudioSource to the same GameObject as the AudioListener
 * (typically the camera).
 */
class AudioSource : public Component
{
  public:
    AudioSource();
    ~AudioSource() override;

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "AudioSource";
    }

    // ========================================================================
    // Lifecycle
    // ========================================================================

    void Awake() override;
    void Start() override;
    void OnEnable() override;
    void OnDisable() override;
    void OnDestroy() override;
    void Update(float deltaTime) override;

    // ========================================================================
    // Serialization
    // ========================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;

    // ========================================================================
    // Track management
    // ========================================================================

    /// @brief Set the number of tracks (resize, default = 1)
    void SetTrackCount(int count);

    /// @brief Get the number of tracks
    [[nodiscard]] int GetTrackCount() const
    {
        return static_cast<int>(m_tracks.size());
    }

    /// @brief Assign a clip to a specific track
    void SetTrackClip(int trackIndex, std::shared_ptr<AudioClip> clip);

    /// @brief Get the clip on a specific track
    [[nodiscard]] std::shared_ptr<AudioClip> GetTrackClip(int trackIndex) const;

    /// @brief Assign a stable audio asset reference, including a currently missing GUID.
    void SetTrackClipGuid(int trackIndex, const std::string &guid);

    /// @brief Get the stable audio asset GUID on a specific track.
    [[nodiscard]] std::string GetTrackClipGuid(int trackIndex) const;

    /// @brief Invalidate or refresh track caches after an audio asset mutation.
    void OnAudioClipAssetEvent(const std::string &guid, AssetEvent event);

    /// @brief Set per-track volume (0.0–1.0)
    void SetTrackVolume(int trackIndex, float volume);

    /// @brief Get per-track volume
    [[nodiscard]] float GetTrackVolume(int trackIndex) const;

    // ========================================================================
    // Playback control (per-track)
    // ========================================================================

    /// @brief Play a specific track (default: track 0)
    void Play(int trackIndex = 0);

    /// @brief Stop a specific track (default: track 0)
    void Stop(int trackIndex = 0);

    /// @brief Pause a specific track (default: track 0)
    void Pause(int trackIndex = 0);

    /// @brief Resume a specific track (default: track 0)
    void UnPause(int trackIndex = 0);

    /// @brief Stop all tracks
    void StopAll();

    /// @brief Play a transient one-shot clip using the source's internal voice pool
    void PlayOneShot(std::shared_ptr<AudioClip> clip, float volumeScale = 1.0f);

    /// @brief Stop all currently playing one-shot voices
    void StopOneShots();

    /// @brief Whether a specific track is currently playing
    [[nodiscard]] bool IsTrackPlaying(int trackIndex) const;

    /// @brief Whether a specific track is paused
    [[nodiscard]] bool IsTrackPaused(int trackIndex) const;

    // ========================================================================
    // Track 0 convenience API
    // ========================================================================

    /// @brief Whether track 0 is currently playing
    [[nodiscard]] bool IsPlaying() const
    {
        return IsTrackPlaying(0);
    }

    /// @brief Whether track 0 is paused
    [[nodiscard]] bool IsPaused() const
    {
        return IsTrackPaused(0);
    }

    // ========================================================================
    // Clip property (track 0 convenience)
    // ========================================================================

    /// @brief Set the audio clip on track 0
    void SetClip(std::shared_ptr<AudioClip> clip)
    {
        SetTrackClip(0, std::move(clip));
    }

    /// @brief Get the audio clip on track 0
    [[nodiscard]] std::shared_ptr<AudioClip> GetClip() const
    {
        return GetTrackClip(0);
    }

    // ========================================================================
    // Source-level properties (shared by all tracks)
    // ========================================================================

    /// @brief Set volume (0.0 = silence, 1.0 = full)
    void SetVolume(float volume);

    /// @brief Get volume
    [[nodiscard]] float GetVolume() const
    {
        return m_volume;
    }

    /// @brief Set pitch multiplier (0.1 to 3.0, 1.0 = normal)
    void SetPitch(float pitch);

    /// @brief Get pitch
    [[nodiscard]] float GetPitch() const
    {
        return m_pitch;
    }

    /// @brief Set whether to loop playback (applies to all tracks)
    void SetLoop(bool loop)
    {
        m_loop = loop;
    }

    /// @brief Get whether looping is enabled
    [[nodiscard]] bool GetLoop() const
    {
        return m_loop;
    }

    /// @brief Set whether to start playing on Awake (track 0)
    void SetPlayOnAwake(bool playOnAwake)
    {
        m_playOnAwake = playOnAwake;
    }

    /// @brief Get whether play-on-awake is enabled
    [[nodiscard]] bool GetPlayOnAwake() const
    {
        return m_playOnAwake;
    }

    /// @brief Set mute state (applies to all tracks)
    void SetMute(bool mute);

    /// @brief Get mute state
    [[nodiscard]] bool GetMute() const
    {
        return m_mute;
    }

    // ========================================================================
    // 3D spatial properties (all-3D approach)
    // ========================================================================

    /// @brief Minimum distance for volume attenuation
    void SetMinDistance(float dist);
    [[nodiscard]] float GetMinDistance() const
    {
        return m_minDistance;
    }

    /// @brief Maximum distance for volume attenuation
    void SetMaxDistance(float dist);
    [[nodiscard]] float GetMaxDistance() const
    {
        return m_maxDistance;
    }

    /// @brief Set the size of the one-shot voice pool used by PlayOneShot
    void SetOneShotPoolSize(int size);

    /// @brief Get the current one-shot pool size
    [[nodiscard]] int GetOneShotPoolSize() const
    {
        return static_cast<int>(m_oneShotVoices.size());
    }

    // ========================================================================
    // Spatial audio (set by AudioEngine::Update)
    // ========================================================================

    /// @brief Set computed spatial gain (called by AudioEngine)
    void SetComputedSpatialGain(float gain)
    {
        m_spatialGain = gain;
    }

    /// @brief Set computed stereo pan (called by AudioEngine)
    void SetComputedPan(float pan)
    {
        m_pan = pan;
    }

    /// @brief Apply gain to all active track streams (called by AudioEngine)
    void ApplyAllTrackGains();

    /// @brief Get all active SDL streams for spatial processing
    [[nodiscard]] std::vector<SDL_AudioStream *> GetActiveStreams() const;

    // ========================================================================
    // Wwise extensibility hooks
    // ========================================================================

    /// @brief Set output bus name (for future Wwise routing)
    void SetOutputBus(const std::string &busName)
    {
        m_outputBus = busName;
    }
    [[nodiscard]] const std::string &GetOutputBus() const
    {
        return m_outputBus;
    }

    /// @brief Get the owning GameObject ID (for Wwise gameObjectId)
    [[nodiscard]] uint64_t GetGameObjectId() const;

    /// @brief Called by AudioEngine during shutdown to invalidate raw stream handles safely
    void NotifyAudioEngineShutdown();

  private:
    void AssignTrackClipReference(int trackIndex, const std::string &guid, std::shared_ptr<AudioClip> clip);

    /// @brief Internal: create voice stream and start playback for a track
    void StartVoice(int trackIndex);

    /// @brief Internal: destroy voice stream for a track
    void StopVoice(int trackIndex);

    /// @brief Internal: create a one-shot voice from the source-owned pool
    void StartOneShotVoice(int voiceIndex, std::shared_ptr<AudioClip> clip, float volumeScale);

    /// @brief Internal: destroy a one-shot voice in the pool
    void StopOneShotVoice(int voiceIndex);

    /// @brief Internal: re-feed loop data when stream drains for a track
    void CheckLooping(int trackIndex);

    /// @brief Compute and apply gain for a single track
    void ApplyTrackGain(int trackIndex);

    /// @brief Compute and apply gain for a single one-shot voice
    void ApplyOneShotGain(int voiceIndex);

    // Tracks
    std::vector<AudioTrack> m_tracks;
    std::vector<AudioOneShotVoice> m_oneShotVoices;

    // Source-level properties (shared)
    float m_volume = 1.0f;
    float m_pitch = 1.0f;
    bool m_loop = false;
    bool m_playOnAwake = true;
    bool m_mute = false;
    float m_minDistance = 1.0f;
    float m_maxDistance = 500.0f;
    int m_oneShotPoolSize = 8;
    std::string m_outputBus = "Master";
    uint64_t m_nextOneShotPlayOrder = 1;

    // Spatial computed values (set by AudioEngine::Update each frame)
    float m_spatialGain = 1.0f;
    float m_pan = 0.0f; // -1 = left, 0 = center, 1 = right
};

} // namespace infernux
