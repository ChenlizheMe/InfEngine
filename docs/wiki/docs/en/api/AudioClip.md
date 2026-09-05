# AudioClip

<div class="class-info">
class in <b>Infernux.core</b>
</div>

## Description

Pythonic wrapper around C++ AudioClip.

Use ``AudioClip.load("Assets/Audio/foo.wav")`` and pass the returned
wrapper (or ``clip.native``) to ``AudioSource.set_track_clip`` or
``AudioSource.play_one_shot``. Do not unload a clip while an AudioSource is
still using it.

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.3.7

The current reliable decoder supports WAV. Keep a loaded clip alive while an AudioSource track or one-shot may still reference it.
<!-- USER CONTENT END -->

## Constructors

| Signature | Description |
|------|------|
| `AudioClip.__init__(native: CppAudioClip) → None` | Wrap an existing C++ AudioClip. |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| native | `CppAudioClip` | The underlying C++ AudioClip object. *(read-only)* |
| is_loaded | `bool` | Whether the audio data is loaded in memory. *(read-only)* |
| duration | `float` | Duration of the audio clip in seconds. *(read-only)* |
| sample_count | `int` | Total number of audio samples. *(read-only)* |
| sample_rate | `int` | Sample rate in Hz (e.g. *(read-only)* |
| channels | `int` | Number of audio channels (1=mono, 2=stereo). *(read-only)* |
| name | `str` | The display name of the audio clip. *(read-only)* |
| file_path | `str` | The file path the clip was loaded from. *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `unload() → None` | Unload the audio data from memory. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static AudioClip.load(file_path: str) → Optional[AudioClip]` | Load an audio clip from a file path. |
| `static AudioClip.from_native(native: CppAudioClip) → AudioClip` | Wrap an existing C++ AudioClip instance. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Operators

| Method | Returns |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
import infernux as inx

clip = inx.AudioClip.load("Assets/Audio/click.wav")
if clip is not None:
    print(clip.name, clip.duration, clip.sample_rate, clip.channels)
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [AudioSource](AudioSource.md)
<!-- USER CONTENT END -->
