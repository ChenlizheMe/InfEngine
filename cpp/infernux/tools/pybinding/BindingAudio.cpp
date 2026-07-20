/**
 * @file BindingAudio.cpp
 * @brief Python bindings for AudioEngine, AudioClip, AudioSource, and AudioListener.
 *
 * Exposes the audio system to Python for editor integration and gameplay scripting.
 */

#include "ComponentBindingRegistry.h"
#include "function/audio/AudioClip.h"
#include "function/audio/AudioEngine.h"
#include "function/audio/AudioListener.h"
#include "function/audio/AudioSource.h"
#include "function/resources/AssetRegistry/AssetRegistry.h"
#include "function/scene/Component.h"
#include "function/scene/GameObject.h"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

namespace infernux
{

void RegisterAudioBindings(py::module_ &m)
{
    // ========================================================================
    // AudioClip — loaded audio data (Unity: AudioClip)
    // ========================================================================
    py::class_<AudioClip, std::shared_ptr<AudioClip>>(m, "AudioClip",
                                                      "Loaded audio clip data.\n"
                                                      "Use AudioClip() and load_from_file() to load.")
        .def(py::init<>())
        .def("load_from_file", &AudioClip::LoadFromFile, py::arg("file_path"),
             "Load audio data from a WAV file. Returns True on success.")
        .def("unload", &AudioClip::Unload, "Unload audio data and free memory")
        .def_property_readonly("is_loaded", &AudioClip::IsLoaded, "Whether the clip has loaded data")
        .def_property_readonly("duration", &AudioClip::GetDuration, "Duration in seconds (Unity: AudioClip.length)")
        .def_property_readonly("sample_count", &AudioClip::GetSampleCount,
                               "Total sample frames (Unity: AudioClip.samples)")
        .def_property_readonly("sample_rate", &AudioClip::GetSampleRate,
                               "Sample rate in Hz (Unity: AudioClip.frequency)")
        .def_property_readonly("channels", &AudioClip::GetChannels, "Number of channels (1=mono, 2=stereo)")
        .def_property_readonly("file_path", &AudioClip::GetFilePath, "Source file path")
        .def_property_readonly("name", &AudioClip::GetName, "Clip name (filename without extension)")
        .def_property_readonly("guid", &AudioClip::GetGuid, "Asset GUID (set by AssetRegistry)")
        .def("__repr__", [](const AudioClip &c) {
            if (!c.IsLoaded())
                return std::string("<AudioClip (not loaded)>");
            return "<AudioClip '" + c.GetName() + "' " + std::to_string(c.GetDuration()) + "s " +
                   std::to_string(c.GetSampleRate()) + "Hz " + std::to_string(c.GetChannels()) + "ch>";
        });

    // ========================================================================
    // AudioSource — playback component (Unity: AudioSource, multi-track)
    // ========================================================================
    py::class_<AudioSource, Component>(m, "AudioSource",
                                       "Audio playback component with multi-track support.\n"
                                       "Attach to a GameObject to play AudioClips.\n"
                                       "Each track can hold a different clip; all tracks\n"
                                       "share the source-level volume, mute, and spatial settings.")
        // Track management
        .def_property("track_count", &AudioSource::GetTrackCount, &AudioSource::SetTrackCount,
                      "Number of audio tracks (default 1). Each track can play independently.")
        .def("set_track_clip", &AudioSource::SetTrackClip, py::arg("track_index"), py::arg("clip"),
             "Assign an AudioClip to a specific track")
        .def("get_track_clip", &AudioSource::GetTrackClip, py::arg("track_index"),
             "Get the AudioClip on a specific track")
        .def(
            "get_track_clip_guid",
            [](const AudioSource &src, int trackIndex) -> std::string {
                auto clip = src.GetTrackClip(trackIndex);
                return clip ? clip->GetGuid() : "";
            },
            py::arg("track_index"), "Get the GUID of the AudioClip on a specific track")
        .def(
            "set_track_clip_by_guid",
            [](AudioSource &src, int trackIndex, const std::string &guid) {
                if (guid.empty()) {
                    src.SetTrackClip(trackIndex, nullptr);
                    return;
                }
                auto &registry = AssetRegistry::Instance();
                if (!registry.IsInitialized())
                    return;
                auto clip = registry.LoadAsset<AudioClip>(guid, ResourceType::Audio);
                if (clip)
                    src.SetTrackClip(trackIndex, std::move(clip));
            },
            py::arg("track_index"), py::arg("guid"), "Set the AudioClip on a track by asset GUID")
        .def("set_track_volume", &AudioSource::SetTrackVolume, py::arg("track_index"), py::arg("volume"),
             "Set per-track volume (0.0–1.0)")
        .def("get_track_volume", &AudioSource::GetTrackVolume, py::arg("track_index"), "Get per-track volume")
        // Playback control (per-track, default track 0)
        .def("play", &AudioSource::Play, py::arg("track_index") = 0, "Start playing a track (default: track 0)")
        .def("stop", &AudioSource::Stop, py::arg("track_index") = 0, "Stop a track (default: track 0)")
        .def("pause", &AudioSource::Pause, py::arg("track_index") = 0, "Pause a track (default: track 0)")
        .def("un_pause", &AudioSource::UnPause, py::arg("track_index") = 0, "Resume a paused track (default: track 0)")
        .def("stop_all", &AudioSource::StopAll, "Stop all tracks")
        .def("play_one_shot", &AudioSource::PlayOneShot, py::arg("clip"), py::arg("volume_scale") = 1.0f,
             "Play a transient clip using the source's pooled one-shot voices")
        .def("stop_one_shots", &AudioSource::StopOneShots, "Stop all pooled one-shot voices")
        .def("is_track_playing", &AudioSource::IsTrackPlaying, py::arg("track_index"),
             "Whether a specific track is currently playing")
        .def("is_track_paused", &AudioSource::IsTrackPaused, py::arg("track_index"),
             "Whether a specific track is paused")
        // Volume / Pitch / Mute (source-level, shared by all tracks)
        .def_property("volume", &AudioSource::GetVolume, &AudioSource::SetVolume,
                      "Source-level volume (0.0 = silence, 1.0 = full). Multiplied with per-track volume.")
        .def_property("pitch", &AudioSource::GetPitch, &AudioSource::SetPitch,
                      "Pitch multiplier (0.1 to 3.0, 1.0 = normal)")
        .def_property("mute", &AudioSource::GetMute, &AudioSource::SetMute, "Mute state (all tracks)")
        // Loop / PlayOnAwake
        .def_property("loop", &AudioSource::GetLoop, &AudioSource::SetLoop, "Whether to loop playback (all tracks)")
        .def_property("play_on_awake", &AudioSource::GetPlayOnAwake, &AudioSource::SetPlayOnAwake,
                      "Whether to auto-play track 0 on Start")
        // 3D spatial
        .def_property("min_distance", &AudioSource::GetMinDistance, &AudioSource::SetMinDistance,
                      "Minimum distance for volume attenuation")
        .def_property("max_distance", &AudioSource::GetMaxDistance, &AudioSource::SetMaxDistance,
                      "Maximum distance for volume attenuation")
        .def_property("one_shot_pool_size", &AudioSource::GetOneShotPoolSize, &AudioSource::SetOneShotPoolSize,
                      "Number of pooled voices reserved for PlayOneShot")
        // Wwise hooks
        .def_property("output_bus", &AudioSource::GetOutputBus, &AudioSource::SetOutputBus,
                      "Output bus name (for future Wwise routing)")
        .def_property_readonly("game_object_id", &AudioSource::GetGameObjectId,
                               "Owning GameObject ID (for Wwise gameObjectId)")
        // Serialization
        .def("serialize", &AudioSource::Serialize, "Serialize to JSON string")
        .def("deserialize", &AudioSource::Deserialize, py::arg("json_str"), "Deserialize from JSON string");

    // ========================================================================
    // AudioListener — scene listener component (Unity: AudioListener)
    // ========================================================================
    py::class_<AudioListener, Component>(m, "AudioListener",
                                         "Audio listener component — the 'ears' in the scene.\n"
                                         "Attach to the main camera GameObject.")
        .def_property_readonly("game_object_id", &AudioListener::GetGameObjectId,
                               "Owning GameObject ID (for Wwise listener registration)")
        // Serialization
        .def("serialize", &AudioListener::Serialize, "Serialize to JSON string")
        .def("deserialize", &AudioListener::Deserialize, py::arg("json_str"), "Deserialize from JSON string");

    // ========================================================================
    // AudioEngine — singleton audio system (not Unity-exposed, engine-level)
    // ========================================================================
    py::class_<AudioEngine>(m, "AudioEngine",
                            "Core audio engine (singleton).\n"
                            "Access via AudioEngine.instance().")
        .def_static(
            "instance", []() -> AudioEngine & { return AudioEngine::Instance(); }, py::return_value_policy::reference,
            "Get the singleton AudioEngine instance")
        .def("initialize", &AudioEngine::Initialize, "Initialize the audio subsystem")
        .def("shutdown", &AudioEngine::Shutdown, "Shutdown the audio subsystem")
        .def_property_readonly("is_initialized", &AudioEngine::IsInitialized, "Whether audio is initialized")
        .def_property("master_volume", &AudioEngine::GetMasterVolume, &AudioEngine::SetMasterVolume,
                      "Master volume (0.0 = silence, 1.0 = full)")
        .def("pause_all", &AudioEngine::PauseAll, "Pause all audio playback")
        .def("resume_all", &AudioEngine::ResumeAll, "Resume all audio playback")
        .def("play_preview", &AudioEngine::PlayPreview, py::arg("file_path"),
             "Play an Editor audio-asset preview through the shared device")
        .def("stop_preview", &AudioEngine::StopPreview, "Stop the active Editor audio-asset preview")
        .def("is_preview_playing", &AudioEngine::IsPreviewPlaying, py::arg("file_path") = "",
             "Whether an Editor audio preview is currently playing")
        .def_property_readonly("preview_path", &AudioEngine::GetPreviewPath,
                               "Source path of the active Editor audio preview")
        .def_property_readonly("is_paused", &AudioEngine::IsPaused, "Whether all audio is globally paused")
        .def_property_readonly("sample_rate", &AudioEngine::GetSampleRate, "Output sample rate in Hz")
        .def_property_readonly("channel_count", &AudioEngine::GetChannelCount, "Output channel count");

    // ========================================================================
    // Register AudioSource and AudioListener with ComponentBindingRegistry
    // ========================================================================
    auto &registry = ComponentBindingRegistry::Instance();
    registry.Register("AudioSource", [](Component *c) -> py::object {
        return py::cast(dynamic_cast<AudioSource *>(c), py::return_value_policy::reference);
    });
    registry.Register("AudioListener", [](Component *c) -> py::object {
        return py::cast(dynamic_cast<AudioListener *>(c), py::return_value_policy::reference);
    });
}

} // namespace infernux
