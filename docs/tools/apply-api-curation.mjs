import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const ROOT = process.cwd();
const CHECK = process.argv.includes("--check");
const DOCUMENTED_RELEASE = JSON.parse(
  fs.readFileSync(path.join(ROOT, "docs", "docs-manifest.json"), "utf8"),
).documented_release;

function forCurrentRelease(value) {
  return value.replaceAll("0.2.1", DOCUMENTED_RELEASE);
}

function withoutRemovedGuideLinks(value) {
  return value
    .split(/\r?\n/)
    .filter((line) => !/^\s*-\s+\[[^\]]+\]\(\.\.\/(?:learn|manual|architecture)\//.test(line))
    .join("\n")
    .replace(/\[([^\]]+)\]\(\.\.\/(?:learn|manual|architecture)\/[^)]+\)/g, "$1");
}

const examples = {
  InxComponent: `\`\`\`python
import infernux as inx


class Patrol(inx.InxComponent):
    speed: float = inx.serialized_field(default=2.0, range=(0.0, 10.0))

    def update(self, delta_time: float) -> None:
        self.transform.translate(inx.Vector3(self.speed * delta_time, 0.0, 0.0))
\`\`\``,
  GameObject: `\`\`\`python
import infernux as inx

player = inx.GameObject.find("Player")
if player is not None and player.active_in_hierarchy:
    body = player.get_component(inx.Rigidbody)
    if body is not None:
        body.use_gravity = True
\`\`\``,
  serialized_field: `\`\`\`python
import infernux as inx


class ProjectileSettings(inx.InxComponent):
    speed: float = inx.serialized_field(
        default=20.0,
        range=(0.0, 100.0),
        tooltip="World units per second",
        slider=True,
    )
    notes: str = inx.serialized_field(default="", multiline=True)
\`\`\``,
  Input: `\`\`\`python
import infernux as inx


class KeyboardMover(inx.InxComponent):
    speed: float = 4.0

    def update(self, delta_time: float) -> None:
        axis = float(inx.input.Input.get_key(inx.input.KeyCode.D)) - float(inx.input.Input.get_key(inx.input.KeyCode.A))
        self.transform.translate(inx.Vector3(axis * self.speed * delta_time, 0.0, 0.0))
\`\`\``,
  Time: `\`\`\`python
import infernux as inx


class PauseClock(inx.InxComponent):
    def update(self, delta_time: float) -> None:
        gameplay_seconds = inx.Time.time
        menu_step = inx.Time.unscaled_delta_time
        print(gameplay_seconds, menu_step)
\`\`\``,
  Rigidbody: `\`\`\`python
import infernux as inx


class Thruster(inx.InxComponent):
    def start(self) -> None:
        self.body = self.game_object.get_component(inx.Rigidbody)

    def fixed_update(self, fixed_delta_time: float) -> None:
        if self.body is not None:
            self.body.add_force(inx.Vector3(0.0, 12.0, 0.0))
\`\`\``,
  Physics: `\`\`\`python
import infernux as inx


class GroundProbe(inx.InxComponent):
    def is_grounded(self) -> bool:
        hit = inx.physics.Physics.raycast(
            self.transform.position,
            inx.Vector3(0.0, -1.0, 0.0),
            max_distance=1.1,
        )
        return hit is not None
\`\`\``,
  UICanvas: `\`\`\`python
import infernux as inx

ui_root = inx.GameObject.find("UI")
if ui_root is not None:
    canvas = ui_root.get_component(inx.ui.UICanvas)
    if canvas is not None:
        canvas.ui_scale_mode = inx.ui.UIScaleMode.ScaleWithScreenSize
        scale_x, scale_y, text_scale = canvas.compute_scale(1280, 720)
\`\`\``,
  Camera: `\`\`\`python
import infernux as inx

camera_object = inx.GameObject.find("Main Camera")
if camera_object is not None:
    camera = camera_object.get_component(inx.Camera)
    if camera is not None:
        camera.projection_mode = 1  # Orthographic
        camera.orthographic_size = 5.0
\`\`\``,
  MeshRenderer: `\`\`\`python
import infernux as inx

display = inx.GameObject.find("DisplayObject")
if display is not None:
    renderer = display.get_component(inx.MeshRenderer)
    if renderer is not None:
        renderer.set_primitive_mesh(inx.PrimitiveType.Cube)
        renderer.casts_shadows = True
\`\`\``,
  Light: `\`\`\`python
import infernux as inx

light_object = inx.GameObject.find("Key Light")
if light_object is not None:
    light = light_object.get_component(inx.Light)
    if light is not None:
        light.light_type = 1  # Point
        light.intensity = 2.0
        light.range = 12.0
\`\`\``,
  AudioSource: `\`\`\`python
import infernux as inx

audio_object = inx.GameObject.find("Ambience")
clip = inx.AudioClip.load("Assets/Audio/ambience.wav")
if audio_object is not None and clip is not None:
    source = audio_object.get_component(inx.AudioSource)
    if source is not None:
        source.set_track_clip(0, clip)
        source.loop = True
        source.play(0)
\`\`\``,
  AudioListener: `\`\`\`python
import infernux as inx

camera_object = inx.GameObject.find("Main Camera")
if camera_object is not None:
    listener = camera_object.get_component(inx.AudioListener)
    if listener is None:
        listener = camera_object.add_component(inx.AudioListener)
\`\`\``,
  AudioClip: `\`\`\`python
import infernux as inx

clip = inx.AudioClip.load("Assets/Audio/click.wav")
if clip is not None:
    print(clip.name, clip.duration, clip.sample_rate, clip.channels)
\`\`\``,
};

const english = {
  InxComponent: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Use this base for project gameplay components. Read [Your First Component](../learn/first-component.md) before relying on the lifecycle table alone.`,
    see_also: `- [Your First Component](../learn/first-component.md)
- [Scenes and Objects](../manual/scenes-and-objects.md)
- [GameObject](GameObject.md)
- [serialized_field](serialized_field.md)
- [Time](Time.md)`,
  },
  GameObject: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

A GameObject owns a Transform and a set of components. Distinguish \`active_self\` from the derived \`active_in_hierarchy\`, and prefer component lookup by type.`,
    see_also: `- [Scenes and Objects](../manual/scenes-and-objects.md)
- [Your First Component](../learn/first-component.md)
- [Transform](Transform.md)
- [InxComponent](InxComponent.md)`,
  },
  serialized_field: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Keep an explicit type annotation beside each serialized field. Metadata controls Inspector presentation and validation; it does not replace runtime checks for missing object or asset references.`,
  },
  Input: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Use held-state queries for continuous actions and down/up queries for one-frame edges. Gameplay mouse work should use Game viewport coordinates and respect Game focus.`,
    see_also: `- [Input and Time](../manual/input-and-time.md)
- [KeyCode](KeyCode.md)
- [Time](Time.md)
- [Camera](Camera.md)`,
  },
  Time: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Use scaled time for gameplay and unscaled time for UI or diagnostics that continue while paused. Physics work belongs to fixed updates.`,
    see_also: `- [Input and Time](../manual/input-and-time.md)
- [InxComponent](InxComponent.md)
- [Rigidbody](Rigidbody.md)`,
  },
  Rigidbody: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

For dynamic bodies, use forces or velocity rather than writing Transform every frame. Use move operations for kinematic bodies and issue simulation commands from fixed updates.`,
    see_also: `- [Physics Manual](../manual/physics.md)
- [Physics](Physics.md)
- [Collider](Collider.md)
- [InxComponent](InxComponent.md)`,
  },
  Physics: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Spatial queries accept layer masks and explicit trigger handling. Keep query volumes and distances bounded, and use the simplest query that answers the gameplay question.`,
    see_also: `- [Physics Manual](../manual/physics.md)
- [Rigidbody](Rigidbody.md)
- [Collider](Collider.md)
- [BoxCollider](BoxCollider.md)`,
  },
  UICanvas: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Design in reference-resolution pixels, then select a scale and screen-match policy for the supported aspect ratios. Decorative elements should not be raycast targets.`,
    see_also: `- [Screen-space UI Manual](../manual/ui.md)
- [UIText](UIText.md)
- [UIImage](UIImage.md)
- [UIButton](UIButton.md)`,
  },
  Camera: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Use a perspective Camera for normal 3D depth and an orthographic Camera for scale-stable 2D framing. Keep near/far clipping proportional to scene scale.`,
    see_also: `- [2D Foundations](../learn/2d-foundations.md)
- [3D Foundations](../learn/3d-foundations.md)
- [CameraProjection](CameraProjection.md)
- [Rendering and RenderStack](../manual/rendering-and-renderstack.md)`,
  },
  MeshRenderer: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

A MeshRenderer can use an inline primitive or an imported mesh asset and supports multiple material slots. Prove mesh and material assignment before debugging lighting effects.`,
    see_also: `- [3D Foundations](../learn/3d-foundations.md)
- [Material](Material.md)
- [Light](Light.md)
- [RenderStack](RenderStack.md)`,
  },
  Light: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Choose light type, range, intensity, color, and shadow settings for the scene scale. Confirm unshadowed lighting first, then enable shadows and tune bias.`,
    see_also: `- [3D Foundations](../learn/3d-foundations.md)
- [LightType](LightType.md)
- [MeshRenderer](MeshRenderer.md)
- [Rendering and RenderStack](../manual/rendering-and-renderstack.md)`,
  },
  AudioSource: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

AudioSource owns 1–16 tracks; \`play_on_awake\` starts only track 0. Use pooled one-shots for transient effects and keep assigned AudioClip objects loaded while playback may use them.`,
    see_also: `- [Audio Workflow](../learn/audio-workflow.md)
- [AudioClip](AudioClip.md)
- [AudioListener](AudioListener.md)
- [Input and Time](../manual/input-and-time.md)`,
  },
  AudioListener: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

Place one intended active listener at the scene's listening position, normally on the active Camera or player head. Source distance is measured relative to it.`,
    see_also: `- [Audio Workflow](../learn/audio-workflow.md)
- [AudioSource](AudioSource.md)
- [Camera](Camera.md)`,
  },
  AudioClip: {
    description: `**Status:** Preview · **Verified with:** 0.2.1

The current reliable decoder supports WAV. Keep a loaded clip alive while an AudioSource track or one-shot may still reference it.`,
    see_also: `- [Audio Workflow](../learn/audio-workflow.md)
- [AudioSource](AudioSource.md)
- [Assets and Meta Files](../manual/assets-and-meta.md)`,
  },
};

const chinese = {
  InxComponent: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

项目玩法组件应继承此基类。不要只阅读生命周期表；请先完成[第一个组件](../learn/first-component.md)。`,
    see_also: `- [第一个组件](../learn/first-component.md)
- [场景与对象](../manual/scenes-and-objects.md)
- [GameObject](GameObject.md)
- [serialized_field](serialized_field.md)
- [Time](Time.md)`,
  },
  GameObject: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

GameObject 拥有 Transform 与一组组件。注意 \`active_self\` 与派生状态 \`active_in_hierarchy\` 的区别，并优先按类型查找组件。`,
    see_also: `- [场景与对象](../manual/scenes-and-objects.md)
- [第一个组件](../learn/first-component.md)
- [Transform](Transform.md)
- [InxComponent](InxComponent.md)`,
  },
  serialized_field: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

每个序列化字段都应保留明确类型标注。元数据用于 Inspector 展示与校验，不能替代对缺失对象或资源引用的运行时检查。`,
  },
  Input: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

连续动作使用按住状态，一帧边沿操作使用 down/up。游戏鼠标逻辑应使用 Game 视口坐标并尊重 Game 焦点。`,
    see_also: `- [输入与时间](../manual/input-and-time.md)
- [KeyCode](KeyCode.md)
- [Time](Time.md)
- [Camera](Camera.md)`,
  },
  Time: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

玩法使用缩放时间；暂停后仍继续的 UI 或诊断使用非缩放时间。物理工作放入固定更新。`,
    see_also: `- [输入与时间](../manual/input-and-time.md)
- [InxComponent](InxComponent.md)
- [Rigidbody](Rigidbody.md)`,
  },
  Rigidbody: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

动态刚体应使用力或速度，不要每帧写 Transform。运动学刚体使用移动操作，仿真命令从固定更新发出。`,
    see_also: `- [物理手册](../manual/physics.md)
- [Physics](Physics.md)
- [Collider](Collider.md)
- [InxComponent](InxComponent.md)`,
  },
  Physics: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

空间查询接受 Layer Mask 和明确的 Trigger 处理。应限制查询体积与距离，并选择能回答玩法问题的最简单查询。`,
    see_also: `- [物理手册](../manual/physics.md)
- [Rigidbody](Rigidbody.md)
- [Collider](Collider.md)
- [BoxCollider](BoxCollider.md)`,
  },
  UICanvas: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

使用参考分辨率像素设计，然后为支持的宽高比选择缩放与屏幕匹配策略。装饰元素不应参与 Raycast。`,
    see_also: `- [屏幕空间 UI 手册](../manual/ui.md)
- [UIText](UIText.md)
- [UIImage](UIImage.md)
- [UIButton](UIButton.md)`,
  },
  Camera: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

普通 3D 深度使用透视 Camera，尺度稳定的 2D 构图使用正交 Camera。Near/Far 裁剪应与场景尺度匹配。`,
    see_also: `- [2D 基础](../learn/2d-foundations.md)
- [3D 基础](../learn/3d-foundations.md)
- [CameraProjection](CameraProjection.md)
- [渲染与 RenderStack](../manual/rendering-and-renderstack.md)`,
  },
  MeshRenderer: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

MeshRenderer 可使用内联基础体或导入网格，并支持多个材质槽。排查光照效果前先证明网格和材质分配正确。`,
    see_also: `- [3D 基础](../learn/3d-foundations.md)
- [Material](Material.md)
- [Light](Light.md)
- [RenderStack](RenderStack.md)`,
  },
  Light: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

根据场景尺度选择 Light 类型、范围、强度、颜色和阴影。先确认无阴影光照，再启用阴影并调整 Bias。`,
    see_also: `- [3D 基础](../learn/3d-foundations.md)
- [LightType](LightType.md)
- [MeshRenderer](MeshRenderer.md)
- [渲染与 RenderStack](../manual/rendering-and-renderstack.md)`,
  },
  AudioSource: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

AudioSource 拥有 1–16 个 Track，\`play_on_awake\` 只启动 Track 0。瞬时效果使用池化 One-shot，并在播放仍可能引用时保持 AudioClip 已加载。`,
    see_also: `- [音频工作流](../learn/audio-workflow.md)
- [AudioClip](AudioClip.md)
- [AudioListener](AudioListener.md)
- [输入与时间](../manual/input-and-time.md)`,
  },
  AudioListener: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

在场景收听位置保留一个预期活动 Listener，通常位于活动 Camera 或玩家头部。Source 距离以它为基准。`,
    see_also: `- [音频工作流](../learn/audio-workflow.md)
- [AudioSource](AudioSource.md)
- [Camera](Camera.md)`,
  },
  AudioClip: {
    description: `**状态：** Preview · **验证版本：** 0.2.1

当前可靠解码器支持 WAV。当 AudioSource Track 或 One-shot 仍可能引用时，应保持 Clip 已加载。`,
    see_also: `- [音频工作流](../learn/audio-workflow.md)
- [AudioSource](AudioSource.md)
- [资源与 Meta 文件](../manual/assets-and-meta.md)`,
  },
};

function replaceSection(text, section, content, file) {
  const pattern = new RegExp(
    `(<!-- USER CONTENT START --> ${section}\\r?\\n)[\\s\\S]*?(\\r?\\n<!-- USER CONTENT END -->)`,
  );
  if (!pattern.test(text)) {
    throw new Error(`${file}: missing user-content section '${section}'`);
  }
  const eol = text.includes("\r\n") ? "\r\n" : "\n";
  const localized = content.replaceAll("\n", eol);
  return text.replace(pattern, `$1${localized}$2`);
}

const stale = new Set();

const generatedAliasPages = {
  en: `# Format

<div class="class-info">
enum in <b>Infernux.rendergraph</b>
</div>

## Description

Texture format for render targets. This public alias maps to the native \`PixelFormat\` enum.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Values

| Name | Description |
|------|------|
| RGBA8_UNORM |  |
| RGBA8_SRGB |  |
| BGRA8_UNORM |  |
| RGBA16_SFLOAT |  |
| RGBA32_SFLOAT |  |
| R32_SFLOAT |  |
| R8_UNORM |  |
| R8G8_UNORM |  |
| RG16_SFLOAT |  |
| A2R10G10B10_UNORM |  |
| R16_SFLOAT |  |
| D32_SFLOAT |  |
| D24_UNORM_S8_UINT |  |

<!-- USER CONTENT START --> enum_values

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| is_depth | \`bool\` | Returns True if this format is a depth format. *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.2.1. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [RenderGraph](RenderGraph.md)
- [Rendering and RenderStack](../manual/rendering-and-renderstack.md)
<!-- USER CONTENT END -->
`,
  zh: `# Format

<div class="class-info">
枚举位于 <b>Infernux.rendergraph</b>
</div>

## 描述

渲染目标使用的纹理格式。这个公共别名映射到原生 \`PixelFormat\` 枚举。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 枚举值

| 名称 | 描述 |
|------|------|
| RGBA8_UNORM |  |
| RGBA8_SRGB |  |
| BGRA8_UNORM |  |
| RGBA16_SFLOAT |  |
| RGBA32_SFLOAT |  |
| R32_SFLOAT |  |
| R8_UNORM |  |
| R8G8_UNORM |  |
| RG16_SFLOAT |  |
| A2R10G10B10_UNORM |  |
| R16_SFLOAT |  |
| D32_SFLOAT |  |
| D24_UNORM_S8_UINT |  |

<!-- USER CONTENT START --> enum_values

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| is_depth | \`bool\` | 如果该格式为深度格式则返回 True。*（只读）* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.2.1 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [RenderGraph](RenderGraph.md)
- [渲染与 RenderStack](../manual/rendering-and-renderstack.md)
<!-- USER CONTENT END -->
`,
};

function ensureGeneratedAliasPages() {
  for (const [language, content] of Object.entries(generatedAliasPages)) {
    const relative = path.join("docs", "wiki", "docs", language, "api", "Format.md");
    const file = path.join(ROOT, relative);
    const current = fs.existsSync(file) ? fs.readFileSync(file, "utf8").replaceAll("\r\n", "\n") : "";
    const expected = withoutRemovedGuideLinks(forCurrentRelease(content));
    if (current === expected) continue;
    if (CHECK) stale.add(relative);
    else fs.writeFileSync(file, expected, "utf8");
  }
}

function insertBefore(text, anchor, line, relative) {
  if (text.includes(line)) return text;
  if (!text.includes(anchor)) throw new Error(`${relative}: missing API navigation anchor '${anchor.trim()}'`);
  return text.replace(anchor, `${line}\n${anchor}`);
}

function ensureGeneratedAliasNavigation() {
  const files = [
    {
      relative: path.join("docs", "wiki", "mkdocs.yml"),
      entries: [
        ["      - RenderGraph: en/api/RenderGraph.md", "      - Format: en/api/Format.md"],
        ["      - RenderGraph: zh/api/RenderGraph.md", "      - Format: zh/api/Format.md"],
      ],
    },
    {
      relative: path.join("docs", "wiki", "mkdocs_api_nav.yml"),
      entries: [
        ["#     - RenderGraph: en/api/RenderGraph.md", "#     - Format: en/api/Format.md"],
        ["#     - RenderGraph: zh/api/RenderGraph.md", "#     - Format: zh/api/Format.md"],
      ],
    },
  ];

  for (const item of files) {
    const file = path.join(ROOT, item.relative);
    const original = fs.readFileSync(file, "utf8").replaceAll("\r\n", "\n");
    let updated = original;
    for (const [anchor, line] of item.entries) updated = insertBefore(updated, anchor, line, item.relative);
    if (updated === original) continue;
    if (CHECK) stale.add(item.relative);
    else fs.writeFileSync(file, updated, "utf8");
  }
}

ensureGeneratedAliasPages();
ensureGeneratedAliasNavigation();

for (const [language, entries] of Object.entries({ en: english, zh: chinese })) {
  for (const [symbol, sections] of Object.entries(entries)) {
    const relative = path.join("docs", "wiki", "docs", language, "api", `${symbol}.md`);
    const file = path.join(ROOT, relative);
    if (!fs.existsSync(file)) {
      throw new Error(`${relative}: curated API page does not exist`);
    }

    const original = fs.readFileSync(file, "utf8");
    let updated = original;
    updated = replaceSection(updated, "description", withoutRemovedGuideLinks(forCurrentRelease(sections.description)), relative);
    updated = replaceSection(updated, "example", examples[symbol], relative);
    if (sections.see_also) {
      updated = replaceSection(updated, "see_also", withoutRemovedGuideLinks(sections.see_also), relative);
    }

    if (updated !== original) {
      if (CHECK) {
        stale.add(relative);
      } else {
        fs.writeFileSync(file, updated, "utf8");
      }
    }
  }
}

for (const language of ["en", "zh"]) {
  const apiRoot = path.join(ROOT, "docs", "wiki", "docs", language, "api");
  const fallback = forCurrentRelease(language === "en"
    ? "> **Example status:** No curated example has been verified for this symbol in 0.2.1. Use the signatures above; do not infer behavior from similarly named APIs in other engines."
    : "> **示例状态：** 当前尚未为此符号验证 0.2.1 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。");

  for (const name of fs.readdirSync(apiRoot).filter((entry) => entry.endsWith(".md") && entry !== "index.md")) {
    const file = path.join(apiRoot, name);
    const relative = path.relative(ROOT, file);
    const original = fs.readFileSync(file, "utf8");
    const exampleSection = original.match(
      /<!-- USER CONTENT START --> example\r?\n([\s\S]*?)\r?\n<!-- USER CONTENT END -->/,
    )?.[1] || "";
    const replaceable = exampleSection.includes("TODO: Add example")
      || exampleSection.includes("**Example status:** No curated example has been verified")
      || exampleSection.includes("**示例状态：** 当前尚未为此符号验证");
    if (!replaceable) continue;
    const updated = replaceSection(original, "example", fallback, relative);
    if (updated !== original) {
      if (CHECK) stale.add(relative);
      else fs.writeFileSync(file, updated, "utf8");
    }
  }
}

if (stale.size > 0) {
  console.error("Curated API sections are stale:");
  for (const file of stale) console.error(`- ${file}`);
  process.exitCode = 1;
} else if (CHECK) {
  console.log("Curated API sections are current.");
} else {
  console.log("Applied curated status, examples, and related documentation to core API pages.");
}
