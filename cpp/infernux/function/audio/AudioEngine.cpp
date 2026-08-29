#include "AudioEngine.h"

#include "AudioClip.h"
#include "AudioListener.h"
#include "AudioSource.h"

#include <core/log/InxLog.h>
#include <function/scene/GameObject.h>
#include <function/scene/Transform.h>

#include <SDL3/SDL.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <glm/glm.hpp>

namespace infernux
{

struct AudioEngine::AudioVoiceState
{
    std::mutex mutex;
    std::vector<float> pcmFrames;
    size_t frameCount = 0;
    double cursor = 0.0;
    float gain = 1.0f;
    float pan = 0.0f;
    float pitch = 1.0f;
    bool loop = false;
    bool destroyed = false;
    bool finished = false;
};

AudioEngine &AudioEngine::Instance()
{
    // Intentionally leaked — shut down explicitly in Infernux::Cleanup().
    static AudioEngine *instance = new AudioEngine();
    return *instance;
}

AudioEngine::~AudioEngine()
{
    Shutdown();
}

bool AudioEngine::Initialize()
{
    if (m_initialized) {
        INXLOG_WARN("AudioEngine already initialized");
        return true;
    }

    if (!SDL_InitSubSystem(SDL_INIT_AUDIO)) {
        INXLOG_ERROR("Failed to initialize SDL audio subsystem: ", SDL_GetError());
        return false;
    }

    SDL_AudioSpec requestedSpec = {};
    requestedSpec.format = SDL_AUDIO_F32;
    requestedSpec.channels = 2;
    requestedSpec.freq = 44100;

    m_deviceId = SDL_OpenAudioDevice(SDL_AUDIO_DEVICE_DEFAULT_PLAYBACK, &requestedSpec);
    if (m_deviceId == 0) {
        INXLOG_ERROR("Failed to open audio device: ", SDL_GetError());
        SDL_QuitSubSystem(SDL_INIT_AUDIO);
        return false;
    }

    SDL_AudioSpec actualSpec = {};
    int sampleFrames = 0;
    if (SDL_GetAudioDeviceFormat(m_deviceId, &actualSpec, &sampleFrames)) {
        m_deviceSpec = actualSpec;
        INXLOG_INFO("Audio device opened: ", m_deviceSpec.freq, " Hz, ", m_deviceSpec.channels,
                    " ch, format=", static_cast<int>(m_deviceSpec.format));
    } else {
        m_deviceSpec = requestedSpec;
        INXLOG_WARN("Could not query device format, using requested spec");
    }

    if (!SDL_ResumeAudioDevice(m_deviceId)) {
        INXLOG_ERROR("Failed to resume audio device: ", SDL_GetError());
        SDL_CloseAudioDevice(m_deviceId);
        m_deviceId = 0;
        SDL_QuitSubSystem(SDL_INIT_AUDIO);
        return false;
    }

    m_initialized = true;
    INXLOG_INFO("AudioEngine initialized successfully");
    return true;
}

void AudioEngine::Shutdown()
{
    if (!m_initialized) {
        return;
    }

    INXLOG_DEBUG("AudioEngine shutting down...");
    StopPreview();

    std::vector<AudioSource *> sources;
    {
        std::lock_guard<std::mutex> lock(m_sourcesMutex);
        sources.assign(m_registeredSources.begin(), m_registeredSources.end());
        m_registeredSources.clear();
    }
    for (AudioSource *source : sources) {
        if (source) {
            source->NotifyAudioEngineShutdown();
        }
    }

    std::vector<SDL_AudioStream *> streams;
    {
        std::lock_guard<std::mutex> lock(m_streamsMutex);
        streams = m_activeStreams;
        m_activeStreams.clear();
        m_voiceStates.clear();
    }

    for (SDL_AudioStream *stream : streams) {
        if (!stream) {
            continue;
        }
        SDL_LockAudioStream(stream);
        SDL_SetAudioStreamGetCallback(stream, nullptr, nullptr);
        SDL_UnlockAudioStream(stream);
        SDL_UnbindAudioStream(stream);
        SDL_DestroyAudioStream(stream);
    }

    if (m_deviceId != 0) {
        SDL_CloseAudioDevice(m_deviceId);
        m_deviceId = 0;
    }

    SDL_QuitSubSystem(SDL_INIT_AUDIO);

    {
        std::lock_guard<std::mutex> lock(m_listenersMutex);
        m_registeredListeners.clear();
    }

    m_activeListener = nullptr;
    m_globalPaused = false;
    m_initialized = false;
    INXLOG_INFO("AudioEngine shut down");
}

float AudioEngine::ComputeAttenuation(float distance, float minDist, float maxDist)
{
    if (maxDist <= minDist) {
        return distance <= minDist ? 1.0f : 0.0f;
    }
    if (distance <= minDist) {
        return 1.0f;
    }
    if (distance >= maxDist) {
        return 0.0f;
    }
    return 1.0f - (distance - minDist) / (maxDist - minDist);
}

void SDLCALL AudioEngine::FeedVoiceStream(void *userdata, SDL_AudioStream *stream, int additional_amount,
                                          int total_amount)
{
    (void)total_amount;

    auto *voice = static_cast<AudioVoiceState *>(userdata);
    if (!voice || additional_amount <= 0) {
        return;
    }

    const int bytesPerFrame = static_cast<int>(sizeof(float) * 2);
    const int requestedFrames = std::max(1, additional_amount / bytesPerFrame);
    std::vector<float> output(static_cast<size_t>(requestedFrames) * 2, 0.0f);

    std::lock_guard<std::mutex> voiceLock(voice->mutex);
    if (voice->destroyed || voice->pcmFrames.empty() || voice->frameCount == 0) {
        return;
    }

    const float clampedPan = std::clamp(voice->pan, -1.0f, 1.0f);
    const float pan01 = (clampedPan + 1.0f) * 0.5f;
    const float leftGain = voice->gain * std::cos(pan01 * 1.57079632679f);
    const float rightGain = voice->gain * std::sin(pan01 * 1.57079632679f);
    const double step = std::max(0.01, static_cast<double>(voice->pitch));

    bool finished = false;
    for (int frame = 0; frame < requestedFrames; ++frame) {
        if (!voice->loop && voice->cursor >= static_cast<double>(voice->frameCount)) {
            finished = true;
            break;
        }

        double frameCursor = voice->cursor;
        while (voice->loop && frameCursor >= static_cast<double>(voice->frameCount)) {
            frameCursor -= static_cast<double>(voice->frameCount);
            voice->cursor = frameCursor;
        }

        const size_t index0 = std::min(static_cast<size_t>(frameCursor), voice->frameCount - 1);
        const size_t index1 =
            voice->loop ? (index0 + 1) % voice->frameCount : std::min(index0 + 1, voice->frameCount - 1);
        const float fraction = static_cast<float>(frameCursor - static_cast<double>(index0));

        const float left0 = voice->pcmFrames[index0 * 2];
        const float right0 = voice->pcmFrames[index0 * 2 + 1];
        const float left1 = voice->pcmFrames[index1 * 2];
        const float right1 = voice->pcmFrames[index1 * 2 + 1];

        const float mono0 = 0.5f * (left0 + right0);
        const float mono1 = 0.5f * (left1 + right1);
        const float monoSample = mono0 + (mono1 - mono0) * fraction;

        output[static_cast<size_t>(frame) * 2] = monoSample * leftGain;
        output[static_cast<size_t>(frame) * 2 + 1] = monoSample * rightGain;
        voice->cursor += step;
    }

    voice->finished = finished;

    if (!SDL_PutAudioStreamData(stream, output.data(), requestedFrames * bytesPerFrame)) {
        INXLOG_WARN("AudioEngine: failed to push streamed audio data: ", SDL_GetError());
    }
}

std::shared_ptr<AudioEngine::AudioVoiceState> AudioEngine::GetVoiceState(SDL_AudioStream *stream) const
{
    if (!stream) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(m_streamsMutex);
    auto it = m_voiceStates.find(stream);
    return it != m_voiceStates.end() ? it->second : nullptr;
}

AudioListener *AudioEngine::FindBestListenerLocked(AudioListener *exclude) const
{
    for (AudioListener *candidate : m_registeredListeners) {
        if (!candidate || candidate == exclude || candidate->IsDestroyed() || !candidate->IsEnabled()) {
            continue;
        }

        auto *gameObject = candidate->GetGameObject();
        if (!gameObject || !gameObject->IsActiveInHierarchy()) {
            continue;
        }

        return candidate;
    }

    return nullptr;
}

void AudioEngine::Update(float /*deltaTime*/)
{
    if (!m_initialized) {
        return;
    }

    SDL_AudioStream *finishedPreview = nullptr;
    {
        std::lock_guard<std::mutex> previewLock(m_previewMutex);
        if (m_previewStream && HasVoiceFinished(m_previewStream))
            finishedPreview = m_previewStream;
    }
    if (finishedPreview)
        StopPreview();

    glm::vec3 listenerPos(0.0f);
    glm::vec3 listenerRight(1.0f, 0.0f, 0.0f);
    bool hasListener = false;

    if (m_activeListener) {
        auto *listenerGO = m_activeListener->GetGameObject();
        if (listenerGO) {
            auto *listenerTr = listenerGO->GetTransform();
            if (listenerTr) {
                listenerPos = listenerTr->GetWorldPosition();
                listenerRight = listenerTr->GetRight();
                hasListener = true;
            }
        }
    }

    std::lock_guard<std::mutex> lock(m_sourcesMutex);
    for (auto *source : m_registeredSources) {
        if (!source) {
            continue;
        }

        auto streams = source->GetActiveStreams();
        if (streams.empty()) {
            continue;
        }

        if (hasListener) {
            auto *sourceGO = source->GetGameObject();
            auto *sourceTr = sourceGO ? sourceGO->GetTransform() : nullptr;
            if (sourceTr) {
                const glm::vec3 sourcePos = sourceTr->GetWorldPosition();
                const float distance = glm::length(sourcePos - listenerPos);
                const float spatialGain =
                    ComputeAttenuation(distance, source->GetMinDistance(), source->GetMaxDistance());
                const glm::vec3 toSource = distance > 0.001f ? (sourcePos - listenerPos) / distance : glm::vec3(0.0f);
                const float pan = glm::dot(toSource, listenerRight);
                source->SetComputedSpatialGain(spatialGain);
                source->SetComputedPan(pan);
            }
        } else {
            source->SetComputedSpatialGain(1.0f);
            source->SetComputedPan(0.0f);
        }

        source->ApplyAllTrackGains();
    }
}

SDL_AudioStream *AudioEngine::CreateVoice(AudioSource * /*source*/, AudioClip *clip)
{
    if (!m_initialized || !clip || !clip->IsLoaded()) {
        return nullptr;
    }

    SDL_AudioSpec playbackSpec = {};
    playbackSpec.format = SDL_AUDIO_F32;
    playbackSpec.channels = 2;
    playbackSpec.freq = m_deviceSpec.freq > 0 ? m_deviceSpec.freq : 44100;

    Uint8 *convertedData = nullptr;
    int convertedLength = 0;
    const auto &clipData = clip->GetData();
    SDL_AudioSpec sourceSpec = {};
    sourceSpec.format = clip->GetFormat();
    sourceSpec.channels = clip->GetChannels();
    sourceSpec.freq = clip->GetSampleRate();

    if (!SDL_ConvertAudioSamples(&sourceSpec, clipData.data(), static_cast<int>(clipData.size()), &playbackSpec,
                                 &convertedData, &convertedLength)) {
        INXLOG_ERROR("Failed to convert audio clip for playback: ", SDL_GetError());
        return nullptr;
    }

    SDL_AudioStream *stream = SDL_CreateAudioStream(&playbackSpec, &m_deviceSpec);
    if (!stream) {
        INXLOG_ERROR("Failed to create audio stream: ", SDL_GetError());
        SDL_free(convertedData);
        return nullptr;
    }

    auto voiceState = std::make_shared<AudioVoiceState>();
    voiceState->pcmFrames.resize(static_cast<size_t>(convertedLength) / sizeof(float));
    std::memcpy(voiceState->pcmFrames.data(), convertedData, static_cast<size_t>(convertedLength));
    voiceState->frameCount = voiceState->pcmFrames.size() / 2;
    SDL_free(convertedData);

    if (!SDL_SetAudioStreamGetCallback(stream, &AudioEngine::FeedVoiceStream, voiceState.get())) {
        INXLOG_ERROR("Failed to register audio stream callback: ", SDL_GetError());
        SDL_DestroyAudioStream(stream);
        return nullptr;
    }

    if (!SDL_BindAudioStream(m_deviceId, stream)) {
        INXLOG_ERROR("Failed to bind audio stream to device: ", SDL_GetError());
        SDL_DestroyAudioStream(stream);
        return nullptr;
    }

    if (!SDL_ResumeAudioStreamDevice(stream)) {
        INXLOG_ERROR("Failed to resume audio stream device: ", SDL_GetError());
        SDL_UnbindAudioStream(stream);
        SDL_DestroyAudioStream(stream);
        return nullptr;
    }

    {
        std::lock_guard<std::mutex> lock(m_streamsMutex);
        m_activeStreams.push_back(stream);
        m_voiceStates.emplace(stream, std::move(voiceState));
    }

    return stream;
}

void AudioEngine::DestroyVoice(SDL_AudioStream *stream)
{
    if (!stream) {
        return;
    }

    std::shared_ptr<AudioVoiceState> state;
    {
        std::lock_guard<std::mutex> lock(m_streamsMutex);
        auto it = m_voiceStates.find(stream);
        if (it != m_voiceStates.end()) {
            state = it->second;
            m_voiceStates.erase(it);
        }
        m_activeStreams.erase(std::remove(m_activeStreams.begin(), m_activeStreams.end(), stream),
                              m_activeStreams.end());
    }

    if (state) {
        std::lock_guard<std::mutex> voiceLock(state->mutex);
        state->destroyed = true;
    }

    SDL_LockAudioStream(stream);
    SDL_SetAudioStreamGetCallback(stream, nullptr, nullptr);
    SDL_UnlockAudioStream(stream);
    SDL_UnbindAudioStream(stream);
    SDL_DestroyAudioStream(stream);
}

void AudioEngine::UpdateVoiceMix(SDL_AudioStream *stream, float gain, float pan, float pitch, bool loop)
{
    auto state = GetVoiceState(stream);
    if (!state) {
        return;
    }

    std::lock_guard<std::mutex> lock(state->mutex);
    state->gain = std::max(0.0f, gain);
    state->pan = std::clamp(pan, -1.0f, 1.0f);
    state->pitch = std::clamp(pitch, 0.1f, 3.0f);
    state->loop = loop;
    if (state->loop && state->finished) {
        state->finished = false;
    }
}

void AudioEngine::SetVoicePaused(SDL_AudioStream *stream, bool paused)
{
    if (!stream) {
        return;
    }

    if (paused) {
        SDL_PauseAudioStreamDevice(stream);
    } else {
        SDL_ResumeAudioStreamDevice(stream);
    }
}

bool AudioEngine::HasVoiceFinished(SDL_AudioStream *stream) const
{
    auto state = GetVoiceState(stream);
    if (!state) {
        return true;
    }

    std::lock_guard<std::mutex> lock(state->mutex);
    return state->finished;
}

bool AudioEngine::PlayPreview(const std::string &filePath)
{
    if (!m_initialized || filePath.empty())
        return false;

    StopPreview();
    auto clip = std::make_shared<AudioClip>();
    if (!clip->LoadFromFile(filePath))
        return false;
    SDL_AudioStream *stream = CreateVoice(nullptr, clip.get());
    if (!stream)
        return false;
    UpdateVoiceMix(stream, 1.0f, 0.0f, 1.0f, false);

    std::lock_guard<std::mutex> lock(m_previewMutex);
    m_previewClip = std::move(clip);
    m_previewStream = stream;
    m_previewPath = filePath;
    return true;
}

void AudioEngine::StopPreview()
{
    SDL_AudioStream *stream = nullptr;
    {
        std::lock_guard<std::mutex> lock(m_previewMutex);
        stream = m_previewStream;
        m_previewStream = nullptr;
        m_previewClip.reset();
        m_previewPath.clear();
    }
    if (stream)
        DestroyVoice(stream);
}

bool AudioEngine::IsPreviewPlaying(const std::string &filePath) const
{
    SDL_AudioStream *stream = nullptr;
    {
        std::lock_guard<std::mutex> lock(m_previewMutex);
        if (!filePath.empty() && filePath != m_previewPath)
            return false;
        stream = m_previewStream;
    }
    return stream && !HasVoiceFinished(stream);
}

std::string AudioEngine::GetPreviewPath() const
{
    std::lock_guard<std::mutex> lock(m_previewMutex);
    return m_previewPath;
}

void AudioEngine::RegisterSource(AudioSource *source)
{
    if (!source) {
        return;
    }
    std::lock_guard<std::mutex> lock(m_sourcesMutex);
    m_registeredSources.insert(source);
}

void AudioEngine::UnregisterSource(AudioSource *source)
{
    if (!source) {
        return;
    }
    std::lock_guard<std::mutex> lock(m_sourcesMutex);
    m_registeredSources.erase(source);
}

void AudioEngine::RegisterListener(AudioListener *listener)
{
    if (!listener) {
        return;
    }

    std::lock_guard<std::mutex> lock(m_listenersMutex);
    const bool inserted = m_registeredListeners.insert(listener).second;

    if (!m_activeListener || m_activeListener == listener || m_activeListener->IsDestroyed() ||
        !m_activeListener->IsEnabled() || !m_activeListener->GetGameObject() ||
        !m_activeListener->GetGameObject()->IsActiveInHierarchy()) {
        m_activeListener = listener;
        return;
    }

    if (!inserted) {
        return;
    }

    const char *activeName =
        m_activeListener->GetGameObject() ? m_activeListener->GetGameObject()->GetName().c_str() : "<unknown>";
    const char *standbyName = listener->GetGameObject() ? listener->GetGameObject()->GetName().c_str() : "<unknown>";
    INXLOG_INFO("AudioListener: only one listener can be active. Keeping '", activeName, "' active; listener '",
                standbyName, "' is on standby.");
}

void AudioEngine::UnregisterListener(AudioListener *listener)
{
    if (!listener) {
        return;
    }

    AudioListener *promoted = nullptr;
    bool lostActiveListener = false;
    {
        std::lock_guard<std::mutex> lock(m_listenersMutex);
        m_registeredListeners.erase(listener);

        if (m_activeListener == listener) {
            lostActiveListener = true;
            promoted = FindBestListenerLocked(listener);
            m_activeListener = promoted;
        }
    }

    if (promoted) {
        const char *promotedName =
            promoted->GetGameObject() ? promoted->GetGameObject()->GetName().c_str() : "<unknown>";
        INXLOG_INFO("AudioListener: active listener changed. Promoting standby listener '", promotedName, "'.");
    } else if (lostActiveListener) {
        INXLOG_INFO("AudioListener: there is currently no active listener in the scene.");
    }
}

void AudioEngine::SetActiveListener(AudioListener *listener)
{
    if (!listener) {
        std::lock_guard<std::mutex> lock(m_listenersMutex);
        m_activeListener = FindBestListenerLocked();
        return;
    }

    std::lock_guard<std::mutex> lock(m_listenersMutex);
    m_registeredListeners.insert(listener);
    m_activeListener = listener;
}

void AudioEngine::SetMasterVolume(float volume)
{
    m_masterVolume = std::clamp(volume, 0.0f, 1.0f);
    if (m_deviceId != 0) {
        SDL_SetAudioDeviceGain(m_deviceId, m_masterVolume);
    }
}

void AudioEngine::PauseAll()
{
    if (m_deviceId != 0) {
        SDL_PauseAudioDevice(m_deviceId);
        m_globalPaused = true;
    }
}

void AudioEngine::ResumeAll()
{
    if (m_deviceId != 0) {
        SDL_ResumeAudioDevice(m_deviceId);
        m_globalPaused = false;
    }
}

size_t AudioEngine::GetActiveVoiceCount() const
{
    std::lock_guard<std::mutex> lock(m_streamsMutex);
    return m_activeStreams.size();
}

} // namespace infernux
