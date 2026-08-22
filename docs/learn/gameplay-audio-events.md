<!-- language:en -->

<span class="mini-tag">Gameplay · Chapter 7</span>

# Audio and gameplay signals

Audio feedback is most useful at the moment gameplay state changes: a collision begins, a button confirms an action, or an objective completes. In this chapter you will configure the real multi-track `AudioSource` API, play a looping track, and route a collision callback to a pooled one-shot sound without polling for the same event in `update()`.

<div class="learn-article-toc"><strong>In this chapter</strong><a href="#audio-model">AudioSource model</a><a href="#editor-setup">Editor setup</a><a href="#complete-component">Complete component</a><a href="#one-shot-signals">One-shot signals</a><a href="#public-api">Public API</a><a href="#verify">Verify the result</a><a href="#common-errors">Common errors</a><a href="#next">Next chapter</a></div>

<figure class="learn-figure">
  <img src="../assets/learn/gameplay-audio-events.webp" alt="diagram separating per-frame state observation from one-shot audio, UI, and gameplay reactions" loading="lazy" decoding="async">
  <figcaption>Observe continuous state where necessary, then send one clear reaction when the state changes.</figcaption>
</figure>

## Understand the AudioSource model {#audio-model}

Infernux `AudioSource` is a multi-track component. It has no single `clip` property. Set `track_count`, assign clips with `set_track_clip(index, clip)` or `set_track_clip_by_guid(index, guid)`, and control each zero-based track with `play(index)`, `pause(index)`, `un_pause(index)`, and `stop(index)`.

All tracks share the source-level `volume`, `pitch`, `mute`, `loop`, `min_distance`, `max_distance`, and `output_bus` settings. Each track also has its own volume. The effective track level therefore includes both `source.volume` and `source.get_track_volume(index)`.

Short gameplay sounds use `play_one_shot(clip, volume_scale)`. The source owns a pool of transient voices, so several hits can overlap without creating temporary GameObjects or consuming persistent tracks. `one_shot_pool_size` controls the maximum concurrent one-shot voices and defaults to 8.

An `AudioListener` must exist in the scene. For screen-centered or effectively 2D sound, keep the source on or near the listener. Distance attenuation starts at `min_distance` and reaches its minimum at `max_distance`.

## Prepare the scene {#editor-setup}

This walkthrough builds on the collision scene from Chapter 5.

1. Add `Assets/Audio/music_loop.wav` and `Assets/Audio/hit.wav` to the project. The current runtime wrapper reliably supports WAV decoding.
2. Select the main camera and add an **AudioListener** component. Keep one active listener in the scene.
3. Select the player and add an **AudioSource** component. Leave **Track Count** at `1`; the script will assign track 0. Disable **Play On Awake** because the script starts playback after loading the clip.
4. Keep the player's Collider and Rigidbody from the physics chapter, and keep a Collider on the object it will hit. `on_collision_enter()` requires a real collision pair.
5. Create `Assets/Scripts/gameplay_audio.py`, paste the component below, and attach `GameplayAudio` to the same player GameObject as the AudioSource.

The AudioSource Inspector exposes source settings first, followed by a **Tracks** section. Each track has a Clip reference and Volume slider. During Play mode it also shows a Play/Stop control and status. This tutorial assigns the clips in code so the complete example has one reproducible setup path.

## Build the complete component {#complete-component}

```python
from Infernux.components import AudioSource, InxComponent
from Infernux.core import AudioClip
from Infernux.debug import Debug


class GameplayAudio(InxComponent):
    def awake(self):
        self._source = None
        self._music_clip = None
        self._hit_clip = None

    def start(self):
        self._source = self.game_object.get_component(AudioSource)
        if self._source is None:
            Debug.log_error("GameplayAudio requires an AudioSource.", self)
            return

        self._music_clip = AudioClip.load("Assets/Audio/music_loop.wav")
        self._hit_clip = AudioClip.load("Assets/Audio/hit.wav")
        if self._music_clip is None or self._hit_clip is None:
            Debug.log_error("Could not load the gameplay WAV files.", self)
            return

        self._source.track_count = 1
        self._source.volume = 0.8
        self._source.loop = True
        self._source.play_on_awake = False
        self._source.one_shot_pool_size = 8
        self._source.set_track_clip(0, self._music_clip)
        self._source.set_track_volume(0, 0.35)
        self._source.play(0)

    def on_collision_enter(self, collision):
        self.play_hit()

    def play_hit(self):
        """A public one-shot reaction; a UI Button may call this too."""
        if self._source is not None and self._hit_clip is not None:
            self._source.play_one_shot(self._hit_clip, 0.9)

    def on_destroy(self):
        if self._source is not None:
            self._source.stop_all()
        if self._music_clip is not None:
            self._music_clip.unload()
        if self._hit_clip is not None:
            self._hit_clip.unload()
```

`AudioClip.load()` returns an `AudioClip` or `None`. Keep the wrapper referenced while its track or one-shot may still use the native clip. The cleanup stops all persistent tracks and one-shots before unloading the clips.

Track 0 carries long-lived music. The hit sound does not replace track 0, and repeated collision entries can overlap through the one-shot pool. `loop` applies to the source's persistent tracks; use one-shots for transient reactions.

## Send one reaction per signal {#one-shot-signals}

`on_collision_enter()` is already a transition callback. It runs when contact begins, so it is the correct place to emit one hit sound. `on_collision_stay()` runs on every fixed-update while contact continues and would repeatedly consume one-shot voices.

The same rule applies when a system only exposes continuous state. Remember the previous state and react only on the edge:

```python
def awake(self):
    self._was_complete = False

def update(self, delta_time):
    is_complete = self.objective_progress >= 1.0
    if is_complete and not self._was_complete:
        self.play_hit()
    self._was_complete = is_complete
```

When an existing event is available, call `play_hit()` directly from that event. A UI Button from Chapter 6 can bind the same public method. This keeps the sound, UI change, and gameplay consequence attached to one state transition.

## AudioSource public API {#public-api}

The following members are declared by the current Python wrapper and type stub.

| Member | Current behavior |
| --- | --- |
| `track_count: int` | Number of persistent tracks. Inspector range: 1–16; default: 1. |
| `volume: float` | Source-level volume shared by all tracks. Inspector range: 0–1; default: 1. |
| `pitch: float` | Source pitch multiplier. Inspector range: 0.1–3; default: 1. |
| `mute: bool` | Mutes all tracks. |
| `loop: bool` | Loops persistent tracks. |
| `play_on_awake: bool` | Automatically plays track 0 when the component starts. |
| `min_distance`, `max_distance` | Start and end distances for spatial attenuation. |
| `one_shot_pool_size: int` | Maximum concurrent pooled one-shot voices; script-only in the current Inspector. |
| `output_bus: str` | Output bus name; script-only in the current Inspector. |
| `is_playing`, `is_paused` | Read-only convenience state for track 0. |
| `game_object_id` | Read-only owning GameObject ID. |

| Method | Purpose |
| --- | --- |
| `set_track_clip(i, clip)` | Assign a Python `AudioClip`, native clip, or `None` to zero-based track `i`. |
| `get_track_clip(i)` | Return the native clip assigned to track `i`, or `None`. |
| `set_track_clip_by_guid(i, guid)` | Resolve and assign a registered audio asset; an empty GUID clears it. |
| `get_track_clip_guid(i)` | Return the assigned asset GUID, or an empty string. |
| `set_track_volume(i, volume)` / `get_track_volume(i)` | Write or read the per-track volume. |
| `play(i=0)` / `stop(i=0)` | Start or stop one persistent track. |
| `pause(i=0)` / `un_pause(i=0)` | Pause or resume one persistent track. |
| `is_track_playing(i)` / `is_track_paused(i)` | Query one track. |
| `play_one_shot(clip, volume_scale=1.0)` | Play a transient clip through the source's voice pool. |
| `stop_one_shots()` | Stop every active pooled one-shot voice. |
| `stop_all()` | Stop all persistent tracks and all one-shots. |
| `serialize()` / `deserialize(json_str)` | Export or restore the component's JSON representation. |

Use `set_track_clip_by_guid()` for authored asset references when the AssetRegistry/AssetDatabase is initialized. For a direct runtime load, use `AudioClip.load(path)` followed by `set_track_clip()` as in the example.

## Verify the result {#verify}

1. Enter Play mode. Track 0 should begin and continue looping at a quieter level than the source volume.
2. Move the player into the obstacle. `hit.wav` should play once when contact begins.
3. Separate the colliders and collide again. A second hit should play. Holding the colliders together should produce no repeated hit.
4. Trigger several distinct collisions quickly. Overlapping hit sounds should use the one-shot pool while music continues on track 0.
5. Select the player during Play mode. The Track 0 status should read **Playing**. Stop Play mode and confirm that no clip-loading error appears in Console.

For a quick API check, add temporary logs for `self._source.is_track_playing(0)`, `self._music_clip.duration`, and `self._hit_clip.channels`, then remove them after verification.

## Common errors {#common-errors}

- **Using `source.clip`**: this property is not public. Assign a numbered track with `set_track_clip()`.
- **Loading MP3 or OGG based on old UI text**: the current reliable decode path is WAV. Convert the tutorial clips to WAV.
- **Unloading too early**: keep each `AudioClip` wrapper alive until every source using it has stopped.
- **Playing in `on_collision_stay()`**: this callback repeats each fixed step. Use `on_collision_enter()` for one sound per contact.
- **No sound in the scene**: confirm that one active AudioListener exists, the clip loaded, the source is not muted, and the source is inside the attenuation range.
- **Every new hit cuts off an older hit**: increase `one_shot_pool_size` to the concurrency your game needs, with a deliberate upper bound.
- **Expecting `is_playing` to summarize every voice**: it reports track 0. Query another persistent track with `is_track_playing(i)`; one-shot state has no public per-voice query.
- **Calling `play_on_awake` after a runtime assignment and waiting for it to fire**: automatic playback belongs to component start. Call `play(0)` after assigning a clip at runtime.

## Next: multi-frame sequences {#next}

Audio reactions are instantaneous signals. The next chapter uses Unity-style generator coroutines to express reactions that unfold across frames: delays, fixed-step handoffs, frame-end work, conditions, cancellation, and child sequences.

<!-- language:zh -->

<span class="mini-tag">游戏玩法 · 第 7 章</span>

# 音频与游戏信号

音频反馈最适合出现在游戏状态发生变化的时刻，例如碰撞开始、按钮确认操作或目标完成。本章会配置当前真实的多轨 `AudioSource` API，播放一条循环轨道，并让碰撞回调触发池化的一次性音效，无需在 `update()` 中反复检查同一事件。

<div class="learn-article-toc"><strong>本章内容</strong><a href="#zh-audio-model">AudioSource 模型</a><a href="#zh-editor-setup">编辑器设置</a><a href="#zh-complete-component">完整组件</a><a href="#zh-one-shot-signals">一次性信号</a><a href="#zh-public-api">公开 API</a><a href="#zh-verify">验证结果</a><a href="#zh-common-errors">常见错误</a><a href="#zh-next">下一章</a></div>

<figure class="learn-figure">
  <img src="../assets/learn/gameplay-audio-events.webp" alt="将逐帧状态观察与一次性音频、UI 和玩法反应分开的示意图" loading="lazy" decoding="async">
  <figcaption>持续状态可按需观察；状态变化时只发送一次清晰的反应。</figcaption>
</figure>

## 理解 AudioSource 模型 {#zh-audio-model}

Infernux 的 `AudioSource` 是多轨组件，没有单一的 `clip` 属性。先设置 `track_count`，再用 `set_track_clip(index, clip)` 或 `set_track_clip_by_guid(index, guid)` 分配音频。轨道索引从 0 开始，可用 `play(index)`、`pause(index)`、`un_pause(index)` 和 `stop(index)` 分别控制。

所有轨道共享 `volume`、`pitch`、`mute`、`loop`、`min_distance`、`max_distance` 与 `output_bus`。每条轨道还有独立音量，因此最终轨道音量同时受 `source.volume` 和 `source.get_track_volume(index)` 影响。

短促的游戏音效使用 `play_one_shot(clip, volume_scale)`。音源内部持有瞬时声部池，多次命中可以重叠播放，无需创建临时 GameObject，也不会占用持续轨道。`one_shot_pool_size` 控制一次性声部的最大并发数，默认值为 8。

场景中必须存在 `AudioListener`。屏幕中心音效或近似 2D 的音效可把音源放在监听器附近。距离衰减从 `min_distance` 开始，在 `max_distance` 处达到最低音量。

## 准备场景 {#zh-editor-setup}

以下步骤沿用第 5 章的碰撞场景。

1. 把 `music_loop.wav` 和 `hit.wav` 放入 `Assets/Audio`。当前运行时封装可稳定解码 WAV。
2. 选择主摄像机，添加 **AudioListener** 组件。场景中保留一个启用的监听器。
3. 选择玩家，添加 **AudioSource** 组件。**Track Count** 保持 `1`，脚本会设置轨道 0。关闭 **Play On Awake**，脚本会在音频加载完成后启动播放。
4. 保留物理章节中的玩家 Collider 与 Rigidbody，并给障碍物保留 Collider。`on_collision_enter()` 需要有效的碰撞组合。
5. 创建 `Assets/Scripts/gameplay_audio.py`，粘贴下面的组件，再把 `GameplayAudio` 挂到 AudioSource 所在的玩家 GameObject。

AudioSource Inspector 先显示音源级设置，后面是 **Tracks** 区域。每条轨道都有 Clip 引用和 Volume 滑块；Play 模式下还会显示 Play/Stop 控件与状态。本教程在代码中分配音频，便于完整复现。

## 编写完整组件 {#zh-complete-component}

```python
from Infernux.components import AudioSource, InxComponent
from Infernux.core import AudioClip
from Infernux.debug import Debug


class GameplayAudio(InxComponent):
    def awake(self):
        self._source = None
        self._music_clip = None
        self._hit_clip = None

    def start(self):
        self._source = self.game_object.get_component(AudioSource)
        if self._source is None:
            Debug.log_error("GameplayAudio requires an AudioSource.", self)
            return

        self._music_clip = AudioClip.load("Assets/Audio/music_loop.wav")
        self._hit_clip = AudioClip.load("Assets/Audio/hit.wav")
        if self._music_clip is None or self._hit_clip is None:
            Debug.log_error("Could not load the gameplay WAV files.", self)
            return

        self._source.track_count = 1
        self._source.volume = 0.8
        self._source.loop = True
        self._source.play_on_awake = False
        self._source.one_shot_pool_size = 8
        self._source.set_track_clip(0, self._music_clip)
        self._source.set_track_volume(0, 0.35)
        self._source.play(0)

    def on_collision_enter(self, collision):
        self.play_hit()

    def play_hit(self):
        """公开的一次性反应；UI Button 也可以调用它。"""
        if self._source is not None and self._hit_clip is not None:
            self._source.play_one_shot(self._hit_clip, 0.9)

    def on_destroy(self):
        if self._source is not None:
            self._source.stop_all()
        if self._music_clip is not None:
            self._music_clip.unload()
        if self._hit_clip is not None:
            self._hit_clip.unload()
```

`AudioClip.load()` 返回 `AudioClip` 或 `None`。轨道或一次性音效仍可能使用原生音频时，需要保留 Python 封装引用。清理阶段先停止全部持续轨道和一次性音效，再卸载音频。

轨道 0 承载持续音乐。命中音效不会替换轨道 0，多次碰撞进入可通过一次性声部池重叠播放。`loop` 作用于音源的持续轨道；瞬时反应适合使用一次性播放。

## 每个信号只发送一次反应 {#zh-one-shot-signals}

`on_collision_enter()` 本身就是状态变化回调，只在接触开始时运行，适合发出一次命中音效。`on_collision_stay()` 会在接触持续期间的每次固定更新中运行，容易连续占用一次性声部。

某些系统只提供连续状态时，可以保存上一帧状态，只在变化边沿执行反应：

```python
def awake(self):
    self._was_complete = False

def update(self, delta_time):
    is_complete = self.objective_progress >= 1.0
    if is_complete and not self._was_complete:
        self.play_hit()
    self._was_complete = is_complete
```

已有事件入口时，直接从事件调用 `play_hit()`。第 6 章的 UI Button 也能绑定这个公开方法。音效、UI 更新和玩法结果便可归到同一次状态变化中。

## AudioSource 公开 API {#zh-public-api}

下表来自当前 Python 封装与类型声明。

| 成员 | 当前行为 |
| --- | --- |
| `track_count: int` | 持续轨道数量。Inspector 范围为 1–16，默认值为 1。 |
| `volume: float` | 所有轨道共享的音源音量。Inspector 范围为 0–1，默认值为 1。 |
| `pitch: float` | 音源音高倍率。Inspector 范围为 0.1–3，默认值为 1。 |
| `mute: bool` | 静音全部轨道。 |
| `loop: bool` | 让持续轨道循环播放。 |
| `play_on_awake: bool` | 组件启动时自动播放轨道 0。 |
| `min_distance`、`max_distance` | 空间衰减的起始与结束距离。 |
| `one_shot_pool_size: int` | 池化一次性声部的最大并发数；当前 Inspector 不显示。 |
| `output_bus: str` | 输出总线名称；当前 Inspector 不显示。 |
| `is_playing`、`is_paused` | 轨道 0 的只读便捷状态。 |
| `game_object_id` | 所属 GameObject 的只读 ID。 |

| 方法 | 用途 |
| --- | --- |
| `set_track_clip(i, clip)` | 给索引为 `i` 的轨道分配 Python `AudioClip`、原生音频或 `None`。 |
| `get_track_clip(i)` | 返回轨道 `i` 的原生音频；未分配时返回 `None`。 |
| `set_track_clip_by_guid(i, guid)` | 解析并分配已注册音频资源；空 GUID 会清除轨道。 |
| `get_track_clip_guid(i)` | 返回轨道资源 GUID；未分配时返回空字符串。 |
| `set_track_volume(i, volume)` / `get_track_volume(i)` | 写入或读取单轨音量。 |
| `play(i=0)` / `stop(i=0)` | 启动或停止一条持续轨道。 |
| `pause(i=0)` / `un_pause(i=0)` | 暂停或恢复一条持续轨道。 |
| `is_track_playing(i)` / `is_track_paused(i)` | 查询指定轨道。 |
| `play_one_shot(clip, volume_scale=1.0)` | 通过音源的声部池播放瞬时音频。 |
| `stop_one_shots()` | 停止当前全部一次性音效。 |
| `stop_all()` | 停止全部持续轨道和一次性音效。 |
| `serialize()` / `deserialize(json_str)` | 导出或恢复组件的 JSON 表示。 |

AssetRegistry/AssetDatabase 已初始化时，可用 `set_track_clip_by_guid()` 处理已编辑资源引用。运行时按路径直接加载时，使用示例中的 `AudioClip.load(path)` 和 `set_track_clip()`。

## 验证结果 {#zh-verify}

1. 进入 Play 模式。轨道 0 应开始循环，音量低于音源总音量。
2. 让玩家撞上障碍物。接触开始时，`hit.wav` 应播放一次。
3. 分开两个 Collider，再次碰撞。应听到第二次音效；持续贴住时不会重复播放。
4. 快速制造多次独立碰撞。命中音效应通过一次性声部池重叠，轨道 0 的音乐继续播放。
5. Play 模式下选择玩家，轨道 0 状态应显示 **Playing**。停止 Play 模式，确认 Console 中没有音频加载错误。

需要快速核对 API 时，可临时记录 `self._source.is_track_playing(0)`、`self._music_clip.duration` 与 `self._hit_clip.channels`，验证后删除日志。

## 常见错误 {#zh-common-errors}

- **使用 `source.clip`**：当前公开 API 没有这个属性。请用 `set_track_clip()` 分配编号轨道。
- **根据旧界面文字加载 MP3 或 OGG**：当前稳定解码路径为 WAV。请先转换教程音频。
- **过早卸载**：每个音频的所有使用者停止后，再调用 `unload()`。
- **在 `on_collision_stay()` 中播放**：该回调每个固定步都会运行。一次接触一次音效应使用 `on_collision_enter()`。
- **场景中没有声音**：检查是否存在一个启用的 AudioListener、音频是否加载成功、音源是否静音，以及音源是否位于衰减范围内。
- **新命中会截断旧音效**：按游戏实际并发需求提高 `one_shot_pool_size`，同时设置明确上限。
- **用 `is_playing` 汇总全部声部**：该属性只报告轨道 0。其他持续轨道用 `is_track_playing(i)` 查询；当前公开 API 没有单个一次性声部状态查询。
- **运行时分配音频后等待 `play_on_awake` 生效**：自动播放发生在组件启动阶段。运行时分配完成后请调用 `play(0)`。

## 下一章：跨帧流程 {#zh-next}

音频反应是即时信号。下一章会使用 Unity 风格的生成器协程组织跨帧反应，覆盖延时、固定步切换、帧末工作、条件等待、取消和子流程。
