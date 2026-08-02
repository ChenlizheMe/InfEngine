#pragma once

#include <SDL3/SDL_audio.h>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace infernux
{

/**
 * @brief An audio clip holds decoded PCM audio data in memory.
 *
 * AudioClip is the audio equivalent of a Texture — it represents loaded,
 * ready-to-play audio data. Clips are decoded into PCM data in memory and
 * can be referenced by AudioSource components.
 *
 * Unity API alignment:
 * - AudioClip.length       → GetDuration()
 * - AudioClip.samples      → GetSampleCount()
 * - AudioClip.frequency    → GetSampleRate()
 * - AudioClip.channels     → GetChannels()
 * - AudioClip.loadState    → IsLoaded()
 */
class AudioClip
{
  public:
    AudioClip() = default;
    ~AudioClip();

    // Non-copyable, movable
    AudioClip(const AudioClip &) = delete;
    AudioClip &operator=(const AudioClip &) = delete;
    AudioClip(AudioClip &&other) noexcept;
    AudioClip &operator=(AudioClip &&other) noexcept;

    // ========================================================================
    // Loading
    // ========================================================================

    /// @brief Load audio data from a supported audio file
    /// @param filePath Absolute path to a supported audio file (.wav or .ogg)
    /// @return true on success
    bool LoadFromFile(const std::string &filePath);

    /// @brief Unload audio data and free memory
    void Unload();

    /// @brief Whether the clip has loaded data
    [[nodiscard]] bool IsLoaded() const
    {
        return m_loaded;
    }

    // ========================================================================
    // Properties (Unity-aligned)
    // ========================================================================

    /// @brief Duration of the clip in seconds
    [[nodiscard]] float GetDuration() const;

    /// @brief Total number of sample frames (per channel)
    [[nodiscard]] uint32_t GetSampleCount() const;

    /// @brief Sample rate in Hz (e.g. 44100)
    [[nodiscard]] int GetSampleRate() const
    {
        return m_spec.freq;
    }

    /// @brief Number of audio channels (1=mono, 2=stereo)
    [[nodiscard]] int GetChannels() const
    {
        return m_spec.channels;
    }

    /// @brief SDL audio format of the loaded data
    [[nodiscard]] SDL_AudioFormat GetFormat() const
    {
        return m_spec.format;
    }

    /// @brief Get the raw PCM data buffer
    [[nodiscard]] const std::vector<uint8_t> &GetData() const
    {
        return m_data;
    }

    /// @brief Get the source file path
    [[nodiscard]] const std::string &GetFilePath() const
    {
        return m_filePath;
    }

    /// @brief Get the clip name (filename without extension)
    [[nodiscard]] const std::string &GetName() const
    {
        return m_name;
    }

    /// @brief Get the asset GUID (set by AudioClipLoader during Load/Reload)
    [[nodiscard]] const std::string &GetGuid() const
    {
        return m_guid;
    }

    /// @brief Set the asset GUID (called by AudioClipLoader)
    void SetGuid(const std::string &guid)
    {
        m_guid = guid;
    }

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept;

  private:
    bool m_loaded = false;
    std::string m_filePath;
    std::string m_name;
    std::string m_guid;

    SDL_AudioSpec m_spec = {};
    std::vector<uint8_t> m_data; ///< Decoded PCM data (owned copy)
    uint32_t m_dataLength = 0;

    /// @brief Read .meta import settings and apply post-load transformations
    void ApplyImportSettings();

    /// @brief Mix multi-channel data down to mono (in-place)
    void ConvertToMono();
};

} // namespace infernux
