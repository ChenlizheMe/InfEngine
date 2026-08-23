# AudioSource

<div class="class-info">
类位于 <b>Infernux.components.builtin</b>
</div>

**继承自:** [BuiltinComponent](Component.md)

## 描述

音频源组件。在场景中播放声音的扬声器。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.5

AudioSource 拥有 1–16 个 Track，`play_on_awake` 只启动 Track 0。瞬时效果使用池化 One-shot，并在播放仍可能引用时保持 AudioClip 已加载。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| track_count | `int` | Number of audio tracks on this source, valid range 1..16. |
| volume | `float` | 音量（0.0 到 1.0）。 |
| pitch | `float` | 音调。 |
| mute | `bool` | Whether the audio source is muted. |
| loop | `bool` | 是否循环播放。 |
| play_on_awake | `bool` | 是否在 Awake 时自动播放。 |
| min_distance | `float` | 3D 声音的最小距离。 |
| max_distance | `float` | 3D 声音的最大距离。 |
| one_shot_pool_size | `int` | The maximum number of concurrent one-shot sounds. |
| output_bus | `str` | Output mixer/audio bus name. |
| is_playing | `bool` | 当前是否正在播放。 *(只读)* |
| is_paused | `bool` | Whether track 0 is currently paused (convenience). *(只读)* |
| game_object_id | `int` | The ID of the GameObject this component is attached to. *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `set_track_clip(track_index: int, clip: Any) → None` | Assign an AudioClip wrapper or native clip to a zero-based track. |
| `get_track_clip(track_index: int) → Any` | Return the audio clip assigned to the specified track. |
| `get_track_clip_guid(track_index: int) → str` | Return the asset GUID of the clip on the specified track. |
| `set_track_clip_by_guid(track_index: int, guid: str) → None` | Assign an audio clip to a track by asset GUID. |
| `set_track_volume(track_index: int, volume: float) → None` | Set the volume of the specified track. |
| `get_track_volume(track_index: int) → float` | Return the volume of the specified track. |
| `play(track_index: int = ...) → None` | 播放音频。 |
| `stop(track_index: int = ...) → None` | 停止。 |
| `play_one_shot(clip: Any, volume_scale: float = ...) → None` | 播放一次性音效（不影响主 clip）。 |
| `stop_one_shots() → None` | Stop all currently playing one-shot sounds. |
| `pause(track_index: int = ...) → None` | 暂停。 |
| `un_pause(track_index: int = ...) → None` | Resume playback on the specified track. |
| `stop_all() → None` | Stop playback on all tracks and pooled one-shot voices. |
| `is_track_playing(track_index: int) → bool` | Return whether the specified track is currently playing. |
| `is_track_paused(track_index: int) → bool` | Return whether the specified track is currently paused. |
| `serialize() → str` | Serialize the component to a JSON string. |
| `deserialize(json_str: str) → bool` | Deserialize the component from a JSON string. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

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

## 另请参阅

<!-- USER CONTENT START --> see_also
- [AudioClip](AudioClip.md)
- [AudioListener](AudioListener.md)
<!-- USER CONTENT END -->
