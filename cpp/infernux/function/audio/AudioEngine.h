#pragma once

#include <SDL3/SDL_audio.h>
#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

// Forward declarations
class AudioClip;
class AudioSource;
class AudioListener;

/**
 * @brief Core audio engine managing SDL3 audio device and stream mixing.
 *
 * Singleton that owns the SDL3 audio device, manages active voices
 * (AudioStreams), and performs per-frame mixing with 3D spatialization.
 *
 * Design:
 * - One logical SDL3 audio device for all playback
 * - Each playing AudioSource owns SDL_AudioStream voices bound to the device
 * - Per-frame Update() adjusts stream gain/panning for 3D positioning
 * - Thread-safe: SDL3 audio runs its own mixing thread; we only push data
 */
class AudioEngine
{
  public:
    /// @brief Get the singleton instance
    static AudioEngine &Instance();

    /// @brief Initialize the audio subsystem (call once at engine startup)
    /// @return true on success
    bool Initialize();

    /// @brief Shutdown the audio subsystem (call at engine cleanup)
    void Shutdown();

    /// @brief Whether the audio engine is initialized
    [[nodiscard]] bool IsInitialized() const
    {
        return m_initialized;
    }

    /// @brief Per-frame update: recalculate 3D spatialization for all active sources
    /// @param deltaTime Time since last frame
    void Update(float deltaTime);

    // ========================================================================
    // Voice management (called by AudioSource)
    // ========================================================================

    /// @brief Create an SDL_AudioStream for a source and bind it to the device
    /// @param source The AudioSource requesting playback
    /// @param clip The AudioClip to play
    /// @return Stream handle, or nullptr on failure
    SDL_AudioStream *CreateVoice(AudioSource *source, AudioClip *clip);

    /// @brief Destroy a voice stream and unbind it from the device
    /// @param stream The stream to destroy
    void DestroyVoice(SDL_AudioStream *stream);

    /// @brief Update per-voice spatial mix and playback parameters
    void UpdateVoiceMix(SDL_AudioStream *stream, float gain, float pan, float pitch, bool loop);

    /// @brief Pause or resume a specific voice at the stream level
    void SetVoicePaused(SDL_AudioStream *stream, bool paused);

    /// @brief Query whether a voice has reached the end of its clip
    [[nodiscard]] bool HasVoiceFinished(SDL_AudioStream *stream) const;

    // ========================================================================
    // Editor asset preview
    // ========================================================================

    /// Play one non-spatial clip through the shared audio device.
    bool PlayPreview(const std::string &filePath);
    void StopPreview();
    [[nodiscard]] bool IsPreviewPlaying(const std::string &filePath = {}) const;
    [[nodiscard]] std::string GetPreviewPath() const;

    // ========================================================================
    // Source registration (for spatial audio)
    // ========================================================================

    /// @brief Register an AudioSource for per-frame spatial updates
    void RegisterSource(AudioSource *source);

    /// @brief Unregister an AudioSource (called on disable/destroy)
    void UnregisterSource(AudioSource *source);

    // ========================================================================
    // Listener management
    // ========================================================================

    /// @brief Register an AudioListener so it can become active or standby
    void RegisterListener(AudioListener *listener);

    /// @brief Unregister an AudioListener and promote a standby listener if needed
    void UnregisterListener(AudioListener *listener);

    /// @brief Register an AudioListener as active
    void SetActiveListener(AudioListener *listener);

    /// @brief Get the currently active listener
    [[nodiscard]] AudioListener *GetActiveListener() const
    {
        return m_activeListener;
    }

    // ========================================================================
    // Global audio settings
    // ========================================================================

    /// @brief Set master volume (0.0 = silence, 1.0 = full)
    void SetMasterVolume(float volume);

    /// @brief Get master volume
    [[nodiscard]] float GetMasterVolume() const
    {
        return m_masterVolume;
    }

    /// @brief Pause all audio playback
    void PauseAll();

    /// @brief Resume all audio playback
    void ResumeAll();

    /// @brief Whether all audio is paused
    [[nodiscard]] bool IsPaused() const
    {
        return m_globalPaused;
    }

    /// @brief Get the output sample rate
    [[nodiscard]] int GetSampleRate() const
    {
        return m_deviceSpec.freq;
    }

    /// @brief Get the output channel count
    [[nodiscard]] int GetChannelCount() const
    {
        return m_deviceSpec.channels;
    }

    ~AudioEngine();

  private:
    struct AudioVoiceState;

    AudioEngine() = default;

    AudioEngine(const AudioEngine &) = delete;
    AudioEngine &operator=(const AudioEngine &) = delete;

    /// @brief Compute distance-based attenuation (inverse-distance clamped)
    static float ComputeAttenuation(float distance, float minDist, float maxDist);

    static void SDLCALL FeedVoiceStream(void *userdata, SDL_AudioStream *stream, int additional_amount,
                                        int total_amount);

    std::shared_ptr<AudioVoiceState> GetVoiceState(SDL_AudioStream *stream) const;
    AudioListener *FindBestListenerLocked(AudioListener *exclude = nullptr) const;

    bool m_initialized = false;
    bool m_globalPaused = false;
    float m_masterVolume = 1.0f;

    SDL_AudioDeviceID m_deviceId = 0;
    SDL_AudioSpec m_deviceSpec = {};

    AudioListener *m_activeListener = nullptr;

    /// Active voice streams (for tracking and cleanup)
    std::vector<SDL_AudioStream *> m_activeStreams;
    mutable std::mutex m_streamsMutex;
    std::unordered_map<SDL_AudioStream *, std::shared_ptr<AudioVoiceState>> m_voiceStates;

    mutable std::mutex m_previewMutex;
    std::shared_ptr<AudioClip> m_previewClip;
    SDL_AudioStream *m_previewStream = nullptr;
    std::string m_previewPath;

    /// Registered AudioSources for spatial updates
    std::unordered_set<AudioSource *> m_registeredSources;
    mutable std::mutex m_sourcesMutex;

    /// Registered AudioListeners. Only one is active; the rest are standby listeners.
    std::unordered_set<AudioListener *> m_registeredListeners;
    mutable std::mutex m_listenersMutex;
};

} // namespace infernux
