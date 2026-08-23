# AudioSource

<div class="class-info">
class in <b>Infernux.components.builtin</b>
</div>

**Inherits from:** [BuiltinComponent](Component.md)

## Description

Multi-track audio playback component.

Infernux does not expose Unity's single ``clip`` field. Instead one
AudioSource owns ``track_count`` tracks. Assign each track with
``set_track_clip(index, clip)`` or ``set_track_clip_by_guid(index, guid)``,
then call ``play(index)``. ``play_on_awake`` only auto-plays track 0.

For transient SFX, prefer ``play_one_shot(clip, volume_scale)`` rather than
creating temporary AudioSource objects. Sources are spatialized; for "2D"
audio, place the AudioSource on/near the AudioListener's GameObject.

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.3.5

AudioSource owns 1–16 tracks; `play_on_awake` starts only track 0. Use pooled one-shots for transient effects and keep assigned AudioClip objects loaded while playback may use them.
<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| track_count | `int` | Number of audio tracks on this source, valid range 1..16. |
| volume | `float` | The overall volume of the audio source. |
| pitch | `float` | The pitch multiplier of the audio source. |
| mute | `bool` | Whether the audio source is muted. |
| loop | `bool` | Whether all tracks loop when they reach the end. |
| play_on_awake | `bool` | Whether track 0 plays automatically during component start. |
| min_distance | `float` | Distance where 3D attenuation begins. |
| max_distance | `float` | Distance where 3D attenuation reaches minimum volume. |
| one_shot_pool_size | `int` | The maximum number of concurrent one-shot sounds. |
| output_bus | `str` | Output mixer/audio bus name. |
| is_playing | `bool` | Whether track 0 is currently playing (convenience). *(read-only)* |
| is_paused | `bool` | Whether track 0 is currently paused (convenience). *(read-only)* |
| game_object_id | `int` | The ID of the GameObject this component is attached to. *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `set_track_clip(track_index: int, clip: Any) → None` | Assign an AudioClip wrapper or native clip to a zero-based track. |
| `get_track_clip(track_index: int) → Any` | Return the audio clip assigned to the specified track. |
| `get_track_clip_guid(track_index: int) → str` | Return the asset GUID of the clip on the specified track. |
| `set_track_clip_by_guid(track_index: int, guid: str) → None` | Assign an audio clip to a track by asset GUID. |
| `set_track_volume(track_index: int, volume: float) → None` | Set the volume of the specified track. |
| `get_track_volume(track_index: int) → float` | Return the volume of the specified track. |
| `play(track_index: int = ...) → None` | Start playback on the specified zero-based track. |
| `stop(track_index: int = ...) → None` | Stop playback on the specified zero-based track. |
| `play_one_shot(clip: Any, volume_scale: float = ...) → None` | Play a transient clip using the source's pooled one-shot voices. |
| `stop_one_shots() → None` | Stop all currently playing one-shot sounds. |
| `pause(track_index: int = ...) → None` | Pause playback on the specified track. |
| `un_pause(track_index: int = ...) → None` | Resume playback on the specified track. |
| `stop_all() → None` | Stop playback on all tracks and pooled one-shot voices. |
| `is_track_playing(track_index: int) → bool` | Return whether the specified track is currently playing. |
| `is_track_paused(track_index: int) → bool` | Return whether the specified track is currently paused. |
| `serialize() → str` | Serialize the component to a JSON string. |
| `deserialize(json_str: str) → bool` | Deserialize the component from a JSON string. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
from Infernux import AudioSource, GameObject
from Infernux.core.audio_clip import AudioClip

audio_object = GameObject.find("Ambience")
clip = AudioClip.load("Assets/Audio/ambience.wav")
if audio_object is not None and clip is not None:
    source = audio_object.get_component(AudioSource)
    if source is not None:
        source.set_track_clip(0, clip)
        source.loop = True
        source.play(0)
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [AudioClip](AudioClip.md)
- [AudioListener](AudioListener.md)
<!-- USER CONTENT END -->
