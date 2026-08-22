<!-- language:en -->

<span class="mini-tag">Custom Rendering · Chapter 5</span>

# Reusable RenderEffect assets

A fullscreen shader describes a pixel operation. A reusable effect also needs a registered implementation, typed parameters, resource requirements, and an asset that scenes can reference. Infernux calls that asset a **RenderEffect**.

Keep the four records separate: the shader does GPU work, the Python feature builds passes, the `.effect` file stores values, and a RenderStack slot decides where that asset runs.

<div class="learn-article-toc"><strong>In this chapter</strong><a href="#fullscreen-shader">Fullscreen shader</a><a href="#effect-class">Effect feature</a><a href="#discovery-path">Discovery and creation</a><a href="#effect-asset">Effect assets</a><a href="#resource-contract">Resources and topology</a><a href="#groups-runtime">Groups and runtime edits</a></div>

<div class="learn-note"><strong>First-pass finish line.</strong><p>Build the Edge Fade shader and feature, create one <code>.effect</code> asset, mount it in the final stage, and change its strength in the Inspector. Once the Game view updates, the core workflow is complete; resource topology, groups, reload recovery, and runtime edits are follow-up tools.</p></div>

<figure class="learn-figure">
  <img src="../assets/learn/real-smoke-portal.webp" alt="real Infernux frame with volumetric smoke and a lit portal scene" loading="lazy" decoding="async">
  <figcaption>A real Infernux frame. Geometry and particles make the scene; mounted effects work on the image and buffers produced by that scene.</figcaption>
</figure>

<figure class="learn-figure">
  <img src="../assets/learn/effect-asset-flow.webp" alt="schematic from fullscreen shader through effect feature and asset to RenderStack slot" loading="lazy" decoding="async">
  <figcaption>This web-style schematic separates implementation, reusable values, and placement. The current Editor layout differs from this drawing.</figcaption>
</figure>

## Start with a fullscreen shader {#fullscreen-shader}

```glsl
#version 450

ShaderInfo {
    Name "Edge Fade"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
    }
    PushConstants pc {
        Float intensity
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

void main() {
    vec4 source = texture(_SourceTex, inUV);
    vec2 centered = inUV * 2.0 - 1.0;
    float edge = smoothstep(0.25, 1.0, dot(centered, centered));
    source.rgb *= 1.0 - edge * pc.intensity;
    outColor = source;
}
```

`Capabilities [Fullscreen]` selects the fullscreen geometry contract. `Resources` and `PushConstants` describe bindings without hand-written Vulkan layouts. `Hidden On` keeps this implementation shader out of ordinary Material selection.

`fullscreen_quad("Edge Fade")` records one fragment-shader ID. At runtime, `FullscreenRenderer` always pairs it with the engine-owned vertex shader whose `ShaderInfo Name` is `Fullscreen Triangle`. Keep that built-in ID in `get_shader_list()` as shown below, but do not add a path for it to the asset: there is no project `Fullscreen Triangle` file to reference. List only the project fragment shader in the `.effect` asset's `dependencies`. Such a shader may have no Material owner available to preload it before graph compilation.

## Register the effect feature {#effect-class}

A one-pass color transform can derive from `FullScreenEffect`:

```python
from Infernux.components.fields import serialized_field
from Infernux.rendergraph.graph import Format
from Infernux.renderstack import (
    FullScreenEffect,
    RoutePolicy,
    render_effect_feature,
)


@render_effect_feature(
    "game.post.edge_fade",
    route_policy=RoutePolicy.MASK_AND_MODIFY,
)
class EdgeFadeEffect(FullScreenEffect):
    name = "Edge Fade"
    menu_path = "Post-processing/Edge Fade"
    injection_point = "before_post_process"
    default_order = 200
    modifies = {"color"}

    intensity: float = serialized_field(
        default=0.35,
        range=(0.0, 1.0),
        slider=False,
    )

    def get_shader_list(self):
        return ["Fullscreen Triangle", "Edge Fade"]

    def setup_passes(self, graph, bus):
        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_edge_fade_out",
            pass_name="EdgeFade_Apply",
            shader_name="Edge Fade",
            format=Format.RGBA16_SFLOAT,
            params={"intensity": float(self.intensity)},
        )
```

`setup_passes()` reads handles from the stage-local resource bus, adds graph passes, then publishes changed handles back to that bus. `apply_single_source_effect()` implements the common color-in, temporary-target, color-out pattern. A multi-pass effect can build its own texture and pass chain.

The class still defines `injection_point` and `default_order` because `FullScreenEffect` inherits the older `RenderPass` contract. RenderStack asset mounting ignores both values when it chooses a stage and orders slots. For `.effect` assets, the pipeline's `EffectStage` and the scene's slot order have final authority. These two class values remain compatibility metadata and a useful conventional hint.

## From source file to mounted effect {#discovery-path}

Use this minimal project layout; all three authored files live below `Assets`:

```text
Assets/
  Rendering/
    edge_fade_effect.py
    Edge Fade.effect
  Shaders/
    edge_fade.frag
```

Then complete the path in this order:

1. Save `Assets/Rendering/edge_fade_effect.py` with the registered class above and save `Assets/Shaders/edge_fade.frag` with the fullscreen shader. The scan root is the current project's `Assets` directory. Hidden directories and `__pycache__`, `build`, `dist`, `.venv`, `venv`, and `.runtime` are skipped; Python source candidates must mention `render_effect_feature` or `register_render_effect_feature`.
2. Save `Assets/Rendering/Edge Fade.effect` with the strict JSON below. Querying its `feature_type` triggers the candidate import and registry lookup. The Project panel's **Create > Render Effect** submenu currently creates built-in types only, so a custom type still starts as authored JSON. Creation refuses an existing `Edge Fade.effect` path instead of replacing it.
3. Import compiles the document before publication: it validates the four-key schema, resolves the registered feature, rejects unknown parameters, records the passes emitted by `setup_passes()`, prepares declared shader dependencies, and writes the successful product under `Library/Artifacts/RenderEffect/<guid>.inxeffect`. The source receives a `.meta` GUID; references retain both that GUID and `path_hint`.
4. Add a RenderStack component, select its GameObject, and drag `Edge Fade.effect` into `final`. RenderStack resolves the enabled slot, instantiates a feature for that mount, and invokes `setup_passes()` when the graph reaches `final`.
5. Verify the result in Game view: set `intensity` to `0` and then `1`. The center remains unchanged while the corners darken at `1`. Confirm that **Effect Compile Errors** is empty in the RenderStack Inspector. A missing project shader should instead report `failed to prepare effect shader dependency`; an unimportable feature ends as `unknown render effect feature` for the mounted stage and slot, while the original Python import exception is retained by discovery and logged during the failed import/reload path.

Discovery, asset import, and graph compilation respond to data changes. They do not scan source every frame.

## The `.effect` document {#effect-asset}

```json
{
  "$schema": "infernux.render_effect",
  "dependencies": [
    {
      "guid": "",
      "path_hint": "Assets/Shaders/edge_fade.frag"
    }
  ],
  "feature_type": "game.post.edge_fade",
  "parameters": {
    "intensity": 0.35
  }
}
```

The parser is strict: these four top-level fields are the complete schema. Mount stage, scope, queue, enable state, and order live elsewhere. The same asset can be mounted more than once, including at different valid stages.

Select a `.effect` in the Project panel to edit its typed parameters in the asset Inspector. The same parameters also appear inline when the asset is expanded under a RenderStack slot.

<figure class="learn-figure">
  <img src="../assets/learn/effect-dual-edit.webp" alt="web-style schematic of RenderEffect parameters expanded under a RenderStack slot" loading="lazy" decoding="async">
  <figcaption>This partial interface schematic shows the RenderStack Slot editor only. The separate asset Inspector is opened by selecting the `.effect` in Project; the current native layout and labels may differ.</figcaption>
</figure>

The current editing rules are authoritative:

- A direct `.effect` mount has one shared loaded `RenderEffect` document. Asset Inspector and Slot Inspector send edits to that same document, so the most recently accepted edit is immediately visible in both views and in every direct mount of the asset. A direct slot has no private parameter override.
- Every accepted Inspector edit enters the global Undo history as a document edit. Undo reverses the latest accepted edit regardless of which view made it, republishes the restored in-memory document, and schedules persistence again. An external file reload is a filesystem consequence and creates no Undo entry.
- The `.effect` document autosaves through a 0.5-second debounced snapshot. Saving the scene persists Slot identity, stage, order, asset reference, and enabled state; it does not replace the separate asset autosave. Closing or changing scenes drains pending autosave work through the resource-document lifecycle.
- Watcher notifications that exactly match an Editor write are acknowledged. A notification arriving while that local write is pending is deferred. Once a different durable revision is confirmed, this non-scene asset follows the disk revision automatically: queued local asset persistence is cancelled, the loaded resource is refreshed, and both views update.
- External schema changes receive no automatic parameter migration. Removing or renaming a serialized field makes old source parameters or group overrides unknown; compilation is rejected until the JSON is updated. Existing Slots keep their GUID/path reference. If external reimport or compilation fails, the loaded source and artifact stay on the last successfully published revision and the document enters a diagnostic/conflict state.

## Declare what the effect reads and writes {#resource-contract}

`requires`, `modifies`, and `creates` describe semantic resources:

| Declaration | Contract |
| --- | --- |
| `requires` | Read-only input, such as `depth` or `normal` |
| `modifies` | Read and write; it also counts as a requirement |
| `creates` | A new semantic resource published for later work |

RenderStack collects `requires ∪ modifies` from all enabled slots before the pipeline is built. This lets the pipeline produce optional geometry buffers such as normals or motion only when an effect asks for them. At the mount point, the `EffectStage` contract decides which handles enter the local bus. An effect that needs depth or motion must bind and check those stage-local handles; an unavailable handle produces a stage-and-slot compile diagnostic in the built-in implementations.

Keep the declaration in sync with `setup_passes()`. Declaring `modifies = {"color"}` does not write color by itself; the implementation must publish the replacement handle to the bus.

`creates` is also declarative. The implementation must create the graph resource and call `bus.set("semantic_name", handle)`. A later effect can consume it in the same stage; a later stage receives it only when that pipeline stage includes the semantic in `inputs`. The current `ResourceBus.set()` replaces an existing handle with the same semantic name, so two successful effects that publish one name are resolved by Slot/Group order and the later publisher wins. There is no automatic duplicate-`creates` diagnostic. Use separate semantic names when both products must survive.

Route-policy conflicts are rejected separately. For example, an `ADDITIVE_EXTRACT` effect cannot share one route with a color-replacement policy, and `CUSTOM_FEATURE` cannot mix with built-in policies. The graph-build diagnostic lists the affected stage IDs and the policy incompatibility.

Failure recovery has three concrete boundaries:

1. Feature registration is replaceable only by the same source identity. A second source registering `game.post.edge_fade` raises `already registered`; the first registration remains active.
2. Effect/group import is compile-then-publish. A malformed document, missing dependency, group cycle, unknown override, or feature failure leaves the previous artifact and loaded asset active. If creation wrote the new source file before its first import failed, that source file remains in `Assets`; fix it and reimport it or remove it explicitly.
3. During Stage compilation, each effect starts with snapshots of the graph pass/texture/topology lists and local bus. If its `setup_passes()` raises, only additions from that effect are removed and its bus snapshot is restored. **Effect Compile Errors** records `<stage_id>/<slot_id>: <error>`; other slots can still compile. If a broader pipeline rebuild raises, the Editor keeps the last valid graph. A packaged Player refuses the Editor's default-pipeline fallback and leaves the failure visible for packaged-product repair.

Most parameter edits only change a parameter block. Put a field in the decorator's `topology_parameters` only when it can change pass count, resource shape, or binding layout. Built-in Bloom, for example, marks `max_iterations`; changing it rebuilds the graph, while changing intensity updates runtime data.

## Effect groups, policy, and runtime edits {#groups-runtime}

Before stacking groups, pin down the four records this course uses. A **RenderEffect feature** is a Python class registered with `render_effect_feature(type_id, ...)`; it owns `setup_passes()` and the parameter schema. A **`.effect` asset** stores a `feature_type` plus concrete parameter values, and `RenderEffect` is its mutable runtime wrapper. An **`.effectgroup`** is an asset document whose entries reference effects or nested groups with optional overrides; when mounted, the group expands in place and has no separate runtime object. **EffectStage** and **EffectSlot** belong to the pipeline and the scene, and the next chapter covers them.

An `.effectgroup` is an ordered list of `.effect` or nested `.effectgroup` references. Each entry has a stable `entry_id`, an enabled flag, and optional parameter overrides. The group Inspector available today can add references, enable entries, rename them, move them up or down, remove them, and edit referenced source effects. After the group is mounted, editing a projected effect under its RenderStack slot writes the group entry's override, leaving the source `.effect` value intact.

```json
{
  "$schema": "infernux.render_effect_group",
  "entries": [
    {
      "entry_id": "edge_fade",
      "asset": {
        "guid": "",
        "path_hint": "Assets/Rendering/Edge Fade.effect"
      },
      "enabled": true,
      "overrides": {
        "intensity": 0.6
      }
    }
  ]
}
```

When mounted, a group expands in place. Final execution order is:

1. RenderStack slot order at that `EffectStage`.
2. For a group slot, group entry order, recursively.
3. Pass order emitted by each feature's `setup_passes()`.

Disabled slots and disabled group entries are skipped. The effect's `default_order` and `injection_point` leave this sequence unchanged.

Route policy controls how a route- or layer-scoped image is returned to its parent composite:

| Policy | Use |
| --- | --- |
| `INLINE` | Work directly on the current contribution |
| `MASK_AND_MODIFY` | Change selected existing pixels without a wider silhouette |
| `ISOLATE_AND_COMPOSITE` | Process an isolated image, then composite it back |
| `ADDITIVE_EXTRACT` | Return additive energy such as bloom |
| `CUSTOM_FEATURE` | Let specialized feature code own composition |

At runtime, `RenderEffect` provides typed getters and setters for floats, integers, booleans, vectors, and colors. Loaded assets are shared. `clone()` creates an isolated runtime-only copy with no source path or GUID, so edits to the clone do not save over the project asset.

**Evidence note.** These rules follow `renderstack/discovery.py` (`discover_effect_features`) and `render_effect_compiler.py` (`compile_and_publish`, `_compile_effect`, and `compile_effect_slots`), `render_effect_inspector.py`, `resource_documents.py`, `assets.py`, `graph.py::fullscreen_quad`, and `FullscreenRenderer.cpp` in the current repository tree. The generated `.inxeffect` is a derived product; the `.effect`, feature source, fragment shader, scene Slot list, and their GUID metadata remain the authored evidence.

The next chapter covers the stage and slot that decide which image a feature receives.

<!-- language:zh -->

<span class="mini-tag">自定义渲染 · 第 5 章</span>

# 可复用的 RenderEffect 资源

全屏 Shader 描述一次像素计算。要把它做成可复用效果，还需要注册实现、带类型的参数、资源需求，以及可被场景引用的资产。Infernux 把这份资产称为 **RenderEffect**。

这四层各有职责：Shader 负责 GPU 计算，Python Feature 负责建立 Pass，`.effect` 保存数值，RenderStack Slot 决定资产在哪里执行。

<div class="learn-article-toc"><strong>本章内容</strong><a href="#fullscreen-shader_1">全屏 Shader</a><a href="#effect-class_1">Effect Feature</a><a href="#discovery-path_1">发现与创建</a><a href="#effect-asset_1">Effect 资源</a><a href="#resource-contract_1">资源与拓扑</a><a href="#groups-runtime_1">EffectGroup 与运行时修改</a></div>

<div class="learn-note"><strong>第一次阅读的完成点。</strong><p>完成 Edge Fade Shader 与 Feature，创建一份 <code>.effect</code> 资源，把它挂到 final 阶段，再从 Inspector 修改强度。Game 画面同步变化后，核心流程已经跑通；资源拓扑、EffectGroup、重载恢复和运行时修改可以按需要继续阅读。</p></div>

<figure class="learn-figure">
  <img src="../assets/learn/real-smoke-portal.webp" alt="带体积烟雾和传送门光照的 Infernux 真实画面" loading="lazy" decoding="async">
  <figcaption>Infernux 的真实画面。几何与粒子生成场景内容，挂载的 Effect 处理它们组成的图像和 Buffer。</figcaption>
</figure>

<figure class="learn-figure">
  <img src="../assets/learn/effect-asset-flow.webp" alt="从全屏 Shader、Effect Feature、Effect 资产到 RenderStack Slot 的示意图" loading="lazy" decoding="async">
  <figcaption>网页式示意图展示实现、可复用数值与执行位置三层数据；当前 Editor 的布局与图中不同。</figcaption>
</figure>

## 从全屏 Shader 开始 {#fullscreen-shader_1}

```glsl
#version 450

ShaderInfo {
    Name "Edge Fade"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
    }
    PushConstants pc {
        Float intensity
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

void main() {
    vec4 source = texture(_SourceTex, inUV);
    vec2 centered = inUV * 2.0 - 1.0;
    float edge = smoothstep(0.25, 1.0, dot(centered, centered));
    source.rgb *= 1.0 - edge * pc.intensity;
    outColor = source;
}
```

`Capabilities [Fullscreen]` 选择全屏几何契约。`Resources` 与 `PushConstants` 描述绑定，无需手写 Vulkan layout。`Hidden On` 会让这份实现 Shader 避开普通 Material 选择列表。

`fullscreen_quad("Edge Fade")` 记录一个 Fragment Shader ID。运行时的 `FullscreenRenderer` 始终把它与引擎内置 Vertex Shader `Fullscreen Triangle` 配对；这个名称来自它的 `ShaderInfo Name`。请像下面的示例一样把内置 ID 留在 `get_shader_list()` 中，但不要给它添加资产路径，因为项目中没有可引用的 `Fullscreen Triangle` 文件。`.effect` 的 `dependencies` 只需列出项目自定义 Fragment Shader。它通常没有 Material 作为所有者，材质系统也就无法保证在图编译前完成预加载。

## 注册 Effect Feature {#effect-class_1}

单 Pass 颜色变换可以继承 `FullScreenEffect`：

```python
from Infernux.components.fields import serialized_field
from Infernux.rendergraph.graph import Format
from Infernux.renderstack import (
    FullScreenEffect,
    RoutePolicy,
    render_effect_feature,
)


@render_effect_feature(
    "game.post.edge_fade",
    route_policy=RoutePolicy.MASK_AND_MODIFY,
)
class EdgeFadeEffect(FullScreenEffect):
    name = "Edge Fade"
    menu_path = "Post-processing/Edge Fade"
    injection_point = "before_post_process"
    default_order = 200
    modifies = {"color"}

    intensity: float = serialized_field(
        default=0.35,
        range=(0.0, 1.0),
        slider=False,
    )

    def get_shader_list(self):
        return ["Fullscreen Triangle", "Edge Fade"]

    def setup_passes(self, graph, bus):
        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_edge_fade_out",
            pass_name="EdgeFade_Apply",
            shader_name="Edge Fade",
            format=Format.RGBA16_SFLOAT,
            params={"intensity": float(self.intensity)},
        )
```

`setup_passes()` 从当前阶段的局部 Resource Bus 读取 Handle，向图中加入 Pass，再把变更后的 Handle 发布回 Bus。`apply_single_source_effect()` 封装常见的“读颜色、写临时纹理、发布新颜色”流程；多 Pass 效果可以自行建立纹理链与 Pass 链。

类里仍有 `injection_point` 和 `default_order`，因为 `FullScreenEffect` 继承了旧的 `RenderPass` 契约。RenderStack 挂载 Effect 资产时会忽略这两个值，不用它们选阶段，也不用它们排列 Slot。对于 `.effect` 资产，管线的 `EffectStage` 与场景的 Slot 顺序拥有最终权威。这两个类字段继续承担兼容元数据和约定提示的作用。

## 从源码到挂载 {#discovery-path_1}

先建立下面的最小项目布局，三个编写文件都位于 `Assets` 下：

```text
Assets/
  Rendering/
    edge_fade_effect.py
    Edge Fade.effect
  Shaders/
    edge_fade.frag
```

然后按以下顺序完成接入：

1. 把上面的注册类保存到 `Assets/Rendering/edge_fade_effect.py`，把全屏 Shader 保存到 `Assets/Shaders/edge_fade.frag`。扫描根目录是当前项目的 `Assets`。隐藏目录以及 `__pycache__`、`build`、`dist`、`.venv`、`venv`、`.runtime` 会被跳过；Python 源码候选文件必须出现 `render_effect_feature` 或 `register_render_effect_feature`。
2. 使用下方严格 JSON 保存 `Assets/Rendering/Edge Fade.effect`。系统查询其中的 `feature_type` 时，会导入候选模块并查找注册项。Project 面板的 **Create > Render Effect** 子菜单当前只创建内置类型，自定义类型仍需编写 JSON。创建操作发现 `Edge Fade.effect` 已存在时会拒绝覆盖。
3. 导入过程先编译文档，再发布结果：检查四键 Schema、解析注册 Feature、拒绝未知参数、记录 `setup_passes()` 生成的 Pass、准备声明的 Shader 依赖，最后把成功产物写到 `Library/Artifacts/RenderEffect/<guid>.inxeffect`。源码通过 `.meta` 获得 GUID；引用同时保留 GUID 与 `path_hint`。
4. 加入 RenderStack 组件并选中其 GameObject，把 `Edge Fade.effect` 拖入 `final`。RenderStack 解析启用的 Slot，为这次挂载实例化 Feature，并在图到达 `final` 时调用 `setup_passes()`。
5. 在 Game 视图验收：先把 `intensity` 设为 `0`，再设为 `1`。中心应保持原样，四角在 `1` 时变暗。确认 RenderStack Inspector 的 **Effect Compile Errors** 为空。项目 Shader 缺失时应出现 `failed to prepare effect shader dependency`；Feature 无法导入时，挂载位置最终显示 `unknown render effect feature`，其中带 Stage 与 Slot，原始 Python 导入异常则由发现系统保留，并在失败的导入或重载路径中记录。

发现、资产导入与图编译由数据变化触发，运行时不会每帧扫描源码。

## `.effect` 文档 {#effect-asset_1}

```json
{
  "$schema": "infernux.render_effect",
  "dependencies": [
    {
      "guid": "",
      "path_hint": "Assets/Shaders/edge_fade.frag"
    }
  ],
  "feature_type": "game.post.edge_fade",
  "parameters": {
    "intensity": 0.35
  }
}
```

解析器采用严格 Schema，顶层只有这四个字段。挂载阶段、作用域、Queue、启用状态和顺序由其它数据负责。同一资产可以被挂载多次，也可以出现在不同的有效阶段。

在 Project 面板中选中 `.effect`，可以在资产 Inspector 编辑带类型的参数。展开 RenderStack Slot 后，同样的参数也会内联显示。

<figure class="learn-figure">
  <img src="../assets/learn/effect-dual-edit.webp" alt="在 RenderStack Slot 下展开 RenderEffect 参数的网页式示意图" loading="lazy" decoding="async">
  <figcaption>这张局部界面示意图只展示 RenderStack Slot 编辑器。另一处资产 Inspector 需要在 Project 中选中 `.effect` 后打开；当前原生布局与标签可能有所不同。</figcaption>
</figure>

当前编辑规则如下：

- 直接挂载的 `.effect` 只有一份共享的已加载 `RenderEffect` 文档。资产 Inspector 与 Slot Inspector 都把修改提交给这份文档，因此最后一次被接受的修改会立即出现在两个视图和该资产的所有直接挂载中。直接 Slot 没有私有参数 Override。
- 每次被接受的 Inspector 修改都会作为文档操作进入全局 Undo 历史。Undo 会撤销最后一次被接受的修改，不受修改入口影响；恢复后的内存文档会再次发布，并重新安排持久化。外部文件重载属于文件系统结果，不会新增 Undo 条目。
- `.effect` 文档通过 0.5 秒防抖快照自动保存。保存场景会持久化 Slot 身份、Stage、顺序、资产引用与启用状态；`.effect` 资产仍由自己的自动保存负责。关闭场景或切换场景时，资源文档生命周期会排空待完成的自动保存。
- 与 Editor 写入内容完全相同的 Watcher 通知会被确认并忽略。通知在本地写入尚未完成时会被延后。系统确认磁盘上出现另一份持久 Revision 后，这类非 Scene 资产会自动跟随磁盘内容：排队中的本地资产持久化会取消，已加载资源会刷新，两个视图也会更新。
- 外部 Schema 变化没有自动参数迁移。删除或重命名序列化字段后，旧 Source 参数或 Group Override 会成为未知参数；更新 JSON 后才能通过编译。现有 Slot 继续保留 GUID/路径引用。外部重新导入或编译失败时，已加载 Source 与 Artifact 会保持最后一次成功发布的 Revision，文档进入诊断或冲突状态。

## 声明读写资源 {#resource-contract_1}

`requires`、`modifies` 与 `creates` 描述语义资源：

| 声明 | 契约 |
| --- | --- |
| `requires` | 只读输入，例如 `depth` 或 `normal` |
| `modifies` | 读写资源，同时也算作需求 |
| `creates` | 新建并发布给后续步骤的语义资源 |

构建管线前，RenderStack 会收集所有启用 Slot 的 `requires ∪ modifies`。这样法线、Motion 等可选几何 Buffer 只会在有 Effect 需要时生成。到了挂载点，`EffectStage` 契约决定局部 Bus 能拿到哪些 Handle。需要深度或 Motion 的 Effect 应绑定并检查这些阶段局部资源；内置实现发现资源缺失时，会生成带 Stage 与 Slot 的编译诊断。

声明必须和 `setup_passes()` 一致。写下 `modifies = {"color"}` 不会自动修改颜色，Feature 仍要把替换后的 Handle 发布回 Bus。

`creates` 同样只负责声明。实现代码必须创建图资源，并调用 `bus.set("semantic_name", handle)`。同一 Stage 中的后续 Effect 可以消费它；后续 Stage 只有在管线把该语义列入 `inputs` 时才能收到它。当前 `ResourceBus.set()` 会替换同名语义的已有 Handle，所以两个成功 Effect 发布同一名称时，结果由 Slot/Group 顺序决定，后发布者生效。系统目前没有重复 `creates` 的自动诊断；需要同时保留两份产物时，请使用不同语义名称。

Route Policy 冲突走另一条校验路径。例如，`ADDITIVE_EXTRACT` 无法与颜色替换 Policy 共用同一 Route，`CUSTOM_FEATURE` 也无法与内置 Policy 混用。图构建诊断会列出相关 Stage ID 与 Policy 不兼容原因。

失败恢复有三个明确边界：

1. Feature 注册只允许相同 Source 身份更新。另一份 Source 注册 `game.post.edge_fade` 时会抛出 `already registered`，首次注册项继续生效。
2. Effect/Group 导入采用“编译完成后发布”。文档格式错误、依赖缺失、Group 循环、未知 Override 或 Feature 失败时，上一份 Artifact 与已加载资产继续生效。如果创建流程已经写入新 Source，首次导入随后失败，这个 Source 文件会留在 `Assets` 中；修复后重新导入，或显式移除该文件。
3. 编译 Stage 时，每个 Effect 都会先保存图的 Pass、Texture、Topology 列表和局部 Bus 快照。`setup_passes()` 抛出异常后，只移除该 Effect 添加的内容，并恢复其 Bus 快照。**Effect Compile Errors** 记录 `<stage_id>/<slot_id>: <error>`，其它 Slot 仍可继续编译。更大范围的 Pipeline 重建抛出异常时，Editor 保留上一份有效图。打包 Player 不采用 Editor 的默认管线回退，打包产物问题会保持可见，等待修复对应产物。

大多数参数修改只需更新参数块。会改变 Pass 数量、资源形状或绑定布局的字段才应放进装饰器的 `topology_parameters`。例如内置 Bloom 把 `max_iterations` 列为拓扑参数；改迭代次数会重建图，改强度只更新运行时数据。

## EffectGroup、Policy 与运行时修改 {#groups-runtime_1}

在叠加 Group 之前，先固定本课程用到的四类记录。**RenderEffect Feature** 是注册了 `render_effect_feature(type_id, ...)` 的 Python 类，它拥有 `setup_passes()` 和参数 Schema。**`.effect` 资产**保存 `feature_type` 与具体参数值，`RenderEffect` 是它的可变运行时包装。**`.effectgroup`** 是资产文档，条目引用 Effect 或嵌套 Group，可以带 Override；挂载时 Group 就地展开，没有独立的运行时对象。**EffectStage** 与 **EffectSlot** 属于管线与场景，下一章介绍。

`.effectgroup` 是一份有序的 `.effect` 或嵌套 `.effectgroup` 引用列表。每项有稳定的 `entry_id`、启用状态和可选参数 Override。当前已经存在的 EffectGroup Inspector 可以添加引用、启停条目、改名、上下移动、删除，并编辑被引用的源 Effect。组挂入 RenderStack 后，在 Slot 下修改展开出的 Effect 会写入该组条目的 Override，源 `.effect` 数值保持不变。

```json
{
  "$schema": "infernux.render_effect_group",
  "entries": [
    {
      "entry_id": "edge_fade",
      "asset": {
        "guid": "",
        "path_hint": "Assets/Rendering/Edge Fade.effect"
      },
      "enabled": true,
      "overrides": {
        "intensity": 0.6
      }
    }
  ]
}
```

挂载后，EffectGroup 会在所在位置展开。最终执行顺序是：

1. 该 `EffectStage` 中的 RenderStack Slot 顺序。
2. Slot 引用 EffectGroup 时，按组内条目顺序递归展开。
3. 每个 Feature 在 `setup_passes()` 中加入 Pass 的顺序。

禁用的 Slot 与组条目会被跳过。`default_order` 和 Effect 的 `injection_point` 都不会改变这条序列。

Route Policy 决定 Route 或 Layer 局部图像怎样返回父级合成：

| Policy | 用途 |
| --- | --- |
| `INLINE` | 直接处理当前贡献 |
| `MASK_AND_MODIFY` | 修改已选像素，不扩张轮廓 |
| `ISOLATE_AND_COMPOSITE` | 处理隔离图像，再合回父级 |
| `ADDITIVE_EXTRACT` | 返回 Bloom 等加法能量 |
| `CUSTOM_FEATURE` | 由专用 Feature 负责合成 |

运行时的 `RenderEffect` 提供 Float、Int、Bool、向量和颜色的类型化 Getter/Setter。加载的资产默认共享。`clone()` 会生成没有源路径和 GUID 的纯运行时副本，修改它不会覆盖项目资产。

**证据说明。** 上述规则来自当前仓库中的 `renderstack/discovery.py`（`discover_effect_features`）与 `render_effect_compiler.py`（`compile_and_publish`、`_compile_effect`、`compile_effect_slots`）、`render_effect_inspector.py`、`resource_documents.py`、`assets.py`、`graph.py::fullscreen_quad` 与 `FullscreenRenderer.cpp`。生成的 `.inxeffect` 属于派生产物；`.effect`、Feature 源码、Fragment Shader、场景 Slot 列表及其 GUID 元数据才是需要保留的编写证据。

下一章继续解释 Stage 与 Slot 如何决定 Feature 实际收到哪一张图。
