# AudioClip

<div class="class-info">
类位于 <b>Infernux.core</b>
</div>

## 描述

音频剪辑资源。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.7

当前可靠解码器支持 WAV。当 AudioSource Track 或 One-shot 仍可能引用时，应保持 Clip 已加载。
<!-- USER CONTENT END -->

## 构造函数

| 签名 | 描述 |
|------|------|
| `AudioClip.__init__(native: CppAudioClip) → None` | Wrap an existing C++ AudioClip. |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| native | `CppAudioClip` | The underlying C++ AudioClip object. *(只读)* |
| is_loaded | `bool` | Whether the audio data is loaded in memory. *(只读)* |
| duration | `float` | 时长（秒）。 *(只读)* |
| sample_count | `int` | Total number of audio samples. *(只读)* |
| sample_rate | `int` | 采样率。 *(只读)* |
| channels | `int` | 声道数。 *(只读)* |
| name | `str` | 剪辑名称。 *(只读)* |
| file_path | `str` | The file path the clip was loaded from. *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `unload() → None` | Unload the audio data from memory. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static AudioClip.load(file_path: str) → Optional[AudioClip]` | 从文件加载音频剪辑。 |
| `static AudioClip.from_native(native: CppAudioClip) → AudioClip` | Wrap an existing C++ AudioClip instance. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 运算符

| 方法 | 返回值 |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
```python
import infernux as inx

clip = inx.AudioClip.load("Assets/Audio/click.wav")
if clip is not None:
    print(clip.name, clip.duration, clip.sample_rate, clip.channels)
```
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [AudioSource](AudioSource.md)
<!-- USER CONTENT END -->
