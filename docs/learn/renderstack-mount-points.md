<!-- language:en -->

<span class="mini-tag">Custom Rendering · Chapter 6</span>

# RenderStack mount points and effect scope

The same `.effect` can process a queue route, a combined layer, or the completed scene. Its shader stays the same; the image and resources at the mount point change.

This chapter starts with the Editor workflow available today. Pipeline authoring syntax comes later.

<div class="learn-article-toc"><strong>In this chapter</strong><a href="#stack-workflow">Editor workflow</a><a href="#terms-authority">Terms and authority</a><a href="#scope">Four scopes</a><a href="#composition">Composition and overflow</a><a href="#repeat-mounts">Repeated mounts</a><a href="#verification">Failure and reload checks</a><a href="#frame-tail">The standard frame tail</a></div>

<div class="learn-note"><strong>First-pass finish line.</strong><p>Mount one known-good Effect in one declared Slot, move it to a second Slot, and compare the Game view. That exercise establishes the Stage/Slot mental model. Route and Layer scope, repeated mounts, orphan recovery, and the standard frame tail explain the more specialized cases afterward.</p></div>

<figure class="learn-figure">
  <img src="../assets/learn/real-voxel-continent.webp" alt="real Infernux render of a voxel continent with depth of field and color treatment" loading="lazy" decoding="async">
  <figcaption>An Infernux frame used as a visual reference for layered rendering. The image alone does not establish its scene revision, mounted assets, or MSAA value; the reproducible Scope Probe below supplies the behavioral evidence for this chapter.</figcaption>
</figure>

## Start in the Editor {#stack-workflow}

1. Create or open a scene that contains a **RenderStack** component, then select its GameObject.
2. Choose the active pipeline from the RenderStack Inspector. Its serialized settings appear under the pipeline selector.
3. Read the **Effect Stages** section. Each list comes from an `EffectStage` declared by that pipeline.
4. Drag a `.effect` or `.effectgroup` asset from the Project panel into the intended list. The picker accepts both file types.
5. Reorder the list to change execution order inside that stage. A slot also carries its own enabled state.
6. Expand a mounted item to edit parameters inline. A direct `.effect` mount edits the shared source values; an effect projected from a group edits that group entry's override.

The Inspector shows only declared Effect Stages. Internal render passes, layers, composites, temporary images, and injection points stay out of this view. This is the complete current built-in set:

| Pipeline | Effect Stages in topology order | Scope |
| --- | --- | --- |
| Default Forward | `after_opaque`, `after_sky`, `after_transparent`, `after_camera_ui`, `final`, `after_screen_ui` | `after_opaque` is Stage; the other five are Composite |
| Default Forward+ | `after_opaque`, `after_sky`, `after_transparent`, `after_camera_ui`, `final`, `after_screen_ui` | Same scopes as Default Forward |
| Default Deferred | `after_gbuffer`, `after_opaque`, `after_sky`, `after_transparent`, `after_camera_ui`, `final`, `after_screen_ui` | `after_gbuffer` and `after_opaque` are Stage; the other five are Composite |

<figure class="learn-figure">
  <img src="../assets/learn/renderstack-inspector.webp" alt="partial web-style RenderStack panel showing four representative mount point lists" loading="lazy" decoding="async">
  <figcaption>A partial web-style illustration with four representative lists. It omits built-in stages such as `after_sky`, `after_transparent`, and Deferred's `after_gbuffer`; the table above is complete. The drawing does not reproduce the native Inspector pixel for pixel.</figcaption>
</figure>

Parameter edits normally update GPU parameter blocks. Each compiled effect binds a named parameter block, and steady frames upload only the changed parameters through `update_parameter_blocks`. Slot order, group structure, enabled topology, and fields declared in `topology_parameters` invalidate the graph instead. A failed individual effect is rolled back and listed in **Effect Compile Errors** with its Stage/Slot path; the rebuilt graph can continue without that effect. A broader topology failure keeps the last valid graph and last valid Inspector topology in the Editor. In a packaged Player, a pipeline build failure does not substitute Default Forward.

## Five terms, four sources of authority {#terms-authority}

These names describe different records:

| Term | Concrete meaning | Authority |
| --- | --- | --- |
| `injection_point` | A named RenderGraph boundary used by the older external `RenderPass` path. `before_post_process` and `after_post_process` are standard examples. | The pipeline places it in topology. A `FullScreenEffect.injection_point` class value does not mount a `.effect` asset. |
| `EffectStage` | A pipeline declaration with `stable_id`, scope, resource inputs/outputs, and capabilities. | The active pipeline decides which stages exist, their cross-stage order, and the image owned by each one. |
| mount point | The user-facing name for an attachment location. In current RenderStack authoring, one mount point is represented by one `EffectStage`. | Its `stable_id` is the scene-to-pipeline contract. |
| slot | One serialized `EffectSlot`: `slot_id`, `stage_id`, asset reference, and enabled state. | The scene RenderStack owns the ordered slot list for each declared stage. |
| `.effectgroup` entry | One enabled reference with `entry_id` and optional overrides. Entries may reference effects or nested groups. | The group document owns entry order within its slot. |

The resulting order has a simple hierarchy:

1. The active pipeline topology orders `EffectStage` declarations.
2. RenderStack slot order controls effects inside one stage.
3. A group expands at its slot position and preserves entry order recursively.
4. Each feature appends its own passes in `setup_passes()` order.

`default_order` and the effect class's `injection_point` do not take part in these four steps. They remain metadata inherited from `RenderPass`. Dragging an asset to `final` makes `final` its actual location, even when the class says `before_post_process`.

RenderStack rejects a new slot for an undeclared stage. If a pipeline change removes a stage used by saved slots, those slots are preserved as orphans under **Missing Effect Stages**. The current Inspector has no remap button. Given the affected `stack` component in an Editor script, the public repair call is `stack.remap_orphan_effect_stage("old_stage", "new_stage")`; it moves every orphan with the old ID, preserves their order and `slot_id`, invalidates the graph, and returns the moved count. The target must exist in the active pipeline. Save the scene after the call.

## Four effect scopes {#scope}

`EffectScope` states which image set a stage owns:

| Scope | Image received | Typical use |
| --- | --- | --- |
| Route | One queue selection rendered through one path | Pixelate a selected render queue |
| Layer | The combined result of several routes in one layer | Outline Forward and Deferred contributions together |
| Stage | Everything accumulated in one opaque or transparent domain | Apply fog after all opaque geometry |
| Composite | The scene accumulated at that pipeline position | Grade scene color after sky and transparency |

The scope belongs to the `EffectStage`; it is absent from the `.effect` asset and from the slot. This lets one reusable asset work at several scopes when their resource contracts and route policies are compatible.

The built-in stages follow the same accumulation. `after_opaque` receives the opaque domain only: scene color plus depth, before sky and transparency. `after_sky` adds the skybox. `after_transparent` receives the complete scene composite, still in linear HDR. `after_camera_ui` adds the Camera Overlay UI on top. `final` runs before display encoding and is the intended home for post-processing chains. `after_screen_ui` receives the display-encoded image plus Screen Overlay UI. Chapter 1 of this course introduced the distinction: Camera Overlay canvases join the scene before post-processing, while Screen Overlay canvases draw after the single linear-to-sRGB conversion and therefore avoid scene effects by default.

The stage contract supplies a local semantic resource bus. Its `inputs` decide whether `color`, `depth`, `normal`, `motion`, or another handle reaches the effect. The renderer also gathers `requires ∪ modifies` from enabled assets early enough to request optional geometry buffers. Both sides must agree: requesting `motion` can make the pipeline produce it, while mounting at a stage that does not expose `motion` still fails the local contract.

Stable IDs are asset-facing API. A pipeline author can refactor internal pass names and temporary textures while keeping `after_opaque` or `final` stable. Renaming a stage changes the scene contract and leaves old slots orphaned until they are remapped.

Here is one minimal high-level pipeline that exposes all four scopes through current public calls:

```python
from Infernux.renderstack import Queue, RenderPipeline


class ScopeProbePipeline(RenderPipeline):
    name = "Scope Probe"

    def define(self, pipeline):
        pipeline.frame(hdr=True)
        with pipeline.opaque() as opaque:
            with opaque.layer("Selected Objects") as selected:
                selected.forward(Queue(1000, 1099)).effects(
                    "route_probe", label="Route Probe"
                )
                selected.forward(Queue(1100, 1199))
                selected.effects("layer_probe", label="Layer Probe")
            opaque.otherwise().forward()
            opaque.effects("opaque_probe", label="Opaque Stage Probe")
        pipeline.effects("scene_probe", label="Scene Composite Probe")
        pipeline.screen_ui()
```

Save this Python file under `Assets`, select **Scope Probe**, and place test objects in Material Queues `1000`, `1100`, and `1200`. Mount the Edge Fade asset from the previous chapter in one probe list at a time:

| Mount | Observable input and result |
| --- | --- |
| `route_probe` | Only the Forward route for Queue `1000..1099` is processed before returning to its layer. Queue `1100` and `1200` stay unchanged. |
| `layer_probe` | The combined Queue `1000..1199` layer is processed once. Queue `1200`, drawn by `otherwise()`, stays unchanged. |
| `opaque_probe` | The opaque domain's accumulated result is processed after the selected layer and the `otherwise()` route, so all three queues change. |
| `scene_probe` | The scene composite available at that pipeline position is processed. In this sample it follows opaque work and precedes the standard Screen UI tail. |

Enable one probe Slot at a time for the first check. Enabling all four intentionally runs four separate copies of the effect and compounds the visual result.

## Composition, masks, and overflow {#composition}

An isolated route often needs pixels outside its geometry silhouette. Gaussian blur, glow, displacement, and outlines can expand the contribution. Clipping the processed image back to its original mask removes that overflow. Compositing without depth can place it over nearer geometry.

The mount scope and route policy handle this together:

- `MASK_AND_MODIFY` suits changes confined to selected existing pixels.
- `ISOLATE_AND_COMPOSITE` suits an isolated contribution that may grow and must return through depth/alpha composition.
- `ADDITIVE_EXTRACT` suits light-like energy added to the parent image.
- `INLINE` uses the current contribution directly.
- A layer-scoped stage lets several routes become one image before processing.

Two overlapping isolated effects still require a pipeline decision. They can run and composite in route order, join a layer first, or move to a later stage. The active pipeline topology records that choice; slot order only settles effects that already share one stage.

Reproduce overflow and policy conflict with the Scope Probe pipeline:

1. Create the built-in Bloom asset from **Create > Render Effect > Bloom**, mount it at `route_probe`, and use a bright Queue `1000` object whose silhouette overlaps a nearer Queue `1200` object. Bloom uses `ADDITIVE_EXTRACT`; its light can extend beyond the isolated silhouette and returns through the route's composition policy. Check both the halo outside the source mask and the nearer object's occlusion.
2. Mount Edge Fade beside Bloom in the same `route_probe`. Edge Fade uses `MASK_AND_MODIFY`, a color-replacement policy. The route-policy merge rejects additive extraction mixed with color replacement and reports the affected Stage in the graph-build diagnostic. Move one asset to `layer_probe` or a later Composite stage, or remove it, then confirm the graph rebuilds.
3. For a custom `creates` resource, mount two effects that both call `bus.set()` with the same semantic. Current ResourceBus behavior is ordered replacement: the later Slot/Group entry wins, with no duplicate-name diagnostic. Rename one semantic when both results must remain available.

## Mounting one asset more than once {#repeat-mounts}

One `.effect` asset can appear in several Slots and scopes. Direct mounts resolve to the shared loaded source document, so an Inspector parameter edit changes every direct mount. Each mount still creates a separate feature instance, pass chain, parameter-block namespace, and execution cost. Runtime binding IDs use `<stage_id>/<slot_id>/<source_index>`, so the same asset mounted at `route_probe` and `scene_probe` is diagnosed independently.

A group entry's override belongs to the `.effectgroup` document. Mounting that group twice reuses its effective group values in both places; editing the projected value changes the entry override for every mount of that group. `entry_id` values must be unique inside one group, while separate groups may reuse an ID. Runtime Stage diagnostics identify the owning Stage, Slot, and flattened source index; they do not use `entry_id` as a globally unique address.

Execution remains deterministic: pipeline Stage order, then Slot order, then recursive group order, then each feature's emitted Pass order. Reusing an asset never deduplicates execution.

## Orphans, failures, and save/reload evidence {#verification}

Use these checks after editing a RenderStack:

1. **Orphan:** Mount Edge Fade at `route_probe`, save the scene, rename the declaration to `route_probe_v2`, and save the Python file. **Missing Effect Stages** should list `route_probe` and its asset reference. The old Slot remains serialized and does not execute. Call `stack.remap_orphan_effect_stage("route_probe", "route_probe_v2")`, expect `1`, then save the scene and confirm the warning clears.
2. **Per-effect failure:** Add `requires = {"color", "missing_probe"}` to a copy of Edge Fade and put `if bus.get("missing_probe") is None: raise ValueError("missing effect-stage resource: missing_probe")` at the start of its `setup_passes()`. Mount it at `route_probe`. **Effect Compile Errors** should include `<stage>/<slot>` and `missing_probe`. Passes and bus publications made by that failing effect are removed; other Slots can remain visible. The explicit check matters because declarations describe the contract and custom implementations still own their bus validation.
3. **Last valid topology:** Begin with a visible valid graph, then temporarily declare a duplicate `scene_probe` stable ID and save. The Editor logs `Pipeline topology is invalid` and continues showing the last valid Inspector topology/render graph. Remove the duplicate and save again; invalidation allows a fresh build. This retained image is evidence of the old graph, so use the cleared diagnostic plus a deliberate visible parameter change to prove the new graph became active.
4. **Save/reload:** Put two differently named assets in `scene_probe`, record their top-to-bottom order and enabled states, save the scene, close it, and reopen it. The Inspector must restore the same order and state. For source-control evidence, the scene's serialized `effect_slots` records each `slot_id`, `stage_id`, GUID/path reference, and enabled value. Asset parameter values remain in their separate `.effect` or `.effectgroup` documents.

## The built-in frame tail {#frame-tail}

The built-in pipelines use this exact order:

1. `_ScreenUI_Camera` draws Camera Overlay canvases into scene color.
2. `after_camera_ui` exposes an `EffectStage`.
3. `before_post_process` marks a legacy injection point.
4. `final` exposes the final post-processing `EffectStage`.
5. `after_post_process` marks a legacy injection point.
6. `_DisplayEncode` clamps linear RGB to `[0,1]`, converts it to sRGB, optionally applies Camera dithering, and writes `_display_encode`; `_DisplayEncode_Commit` copies that encoded result back to scene color.
7. `_ScreenUI_Overlay` draws Screen Overlay canvases over the encoded image.
8. `after_screen_ui` exposes the complete display-space image as an `EffectStage`.

The named points have distinct color and UI semantics:

- **`after_camera_ui`** receives linear scene color after Camera Overlay UI has joined it. Effects mounted here include that UI, and the later `final` chain can still process the result.
- **`final`** starts before display encoding. It is an ordered mount list with no built-in tone-mapping pass. The built-in Tone Mapping asset is optional. When mounted here, it compresses linear HDR to linear LDR; place HDR effects before its Slot and linear-LDR effects after it.
- **Display encoding** performs the one linear-to-sRGB conversion. With no tone mapper mounted, values above `1.0` are clipped here. Tone mapping and display encoding are separate operations.
- **Screen UI** is a draw pass after display encoding, not an Effect Stage. Screen Overlay menus and HUD elements therefore avoid the scene's bloom, grading, and tone mapping by default.
- **`after_screen_ui`** receives encoded scene color plus Screen UI. No second display-encode pass follows it. Effects mounted here must operate in display space and preserve an encoded `[0,1]` result for presentation.

The continuous frame tail is:

```text
Camera UI -> after_camera_ui -> before_post_process -> final
          -> after_post_process -> Display Encode + Commit
          -> Screen UI -> after_screen_ui -> graph output -> presentation
```

A Tone Mapping `.effect` appears only when the user mounts that asset inside `final`:

```text
final: [HDR effects] -> [Tone Mapping .effect, optional] -> [linear-LDR effects]
```

RenderStack finalization guarantees display encoding, Screen UI overlay, and `after_screen_ui` when a custom pipeline omits that terminal section. `after_camera_ui` and `final` remain pipeline-declared stages; all built-in pipelines declare both.

This distinction also explains why `before_post_process` and `after_post_process` are absent from the current Effect Stages Inspector: they are injection points. Asset authors mount post-processing effects into `final` and arrange the chain with slots or an `.effectgroup`.

**Evidence note.** The stage set and frame tail above follow `default_forward_pipeline.py`, `default_forward_plus_pipeline.py`, `default_deferred_pipeline.py`, `graph.py::screen_ui_section`, `display_encode.frag`, and `render_stack.py` in the current tree. The Scope Probe calls match `pipeline_dsl.py`; orphan identity and diagnostics match `EffectSlot`, `remap_orphan_effect_stage()`, and `compile_effect_slots()`.

<!-- language:zh -->

<span class="mini-tag">自定义渲染 · 第 6 章</span>

# RenderStack 挂载点与效果作用域

同一份 `.effect` 可以处理一条 Queue Route、合并后的 Layer，也可以处理完整场景。Shader 本身保持不变，挂载点提供的图像与资源会发生变化。

本章从当前已经存在的 Editor 工作流讲起，管线编写语法留到后续章节。

<div class="learn-article-toc"><strong>本章内容</strong><a href="#stack-workflow_1">Editor 工作流</a><a href="#terms-authority_1">术语与权威</a><a href="#scope_1">四种作用域</a><a href="#composition_1">合成与外溢</a><a href="#repeat-mounts_1">重复挂载</a><a href="#verification_1">失败与重载检查</a><a href="#frame-tail_1">标准帧尾</a></div>

<div class="learn-note"><strong>第一次阅读的完成点。</strong><p>把一份已经验证可用的 Effect 挂到一个管线声明的 Slot，再移动到另一个 Slot，对照 Game 画面。这个练习先建立 Stage 与 Slot 的基本认识；Route/Layer 作用域、重复挂载、孤儿恢复和标准帧尾用于解释后面的特殊场景。</p></div>

<figure class="learn-figure">
  <img src="../assets/learn/real-voxel-continent.webp" alt="带景深与色彩处理的 Infernux 体素大陆真实画面" loading="lazy" decoding="async">
  <figcaption>这张 Infernux 画面用于展示分层渲染的视觉参考。单张图片无法确定其场景 Revision、挂载资产或 MSAA 数值；本章后面的 Scope Probe 才提供可复现的行为证据。</figcaption>
</figure>

## 先在 Editor 中挂载 {#stack-workflow_1}

1. 创建或打开带有 **RenderStack** 组件的场景，然后选中该 GameObject。
2. 在 RenderStack Inspector 中选择活动管线。管线的序列化设置会显示在选择器下方。
3. 查看 **Effect Stages** 区域。每个列表都来自该管线声明的一个 `EffectStage`。
4. 从 Project 面板把 `.effect` 或 `.effectgroup` 拖进目标列表。资产选择器支持这两种文件。
5. 调整列表顺序，就能改变同一阶段内的执行顺序。每个 Slot 也有自己的启用状态。
6. 展开挂载项，可以内联编辑参数。直接挂载的 `.effect` 会修改共享 Source 数值；从 Group 展开的 Effect 会修改该 Group 条目的 Override。

Inspector 只显示管线声明的 Effect Stage。内部 Render Pass、Layer、Composite、临时图像和 injection point 都不会进入这个视图。当前内置 Stage 的完整集合如下：

| 管线 | 按拓扑顺序排列的 Effect Stage | Scope |
| --- | --- | --- |
| Default Forward | `after_opaque`、`after_sky`、`after_transparent`、`after_camera_ui`、`final`、`after_screen_ui` | `after_opaque` 是 Stage，其余五个是 Composite |
| Default Forward+ | `after_opaque`、`after_sky`、`after_transparent`、`after_camera_ui`、`final`、`after_screen_ui` | Scope 与 Default Forward 相同 |
| Default Deferred | `after_gbuffer`、`after_opaque`、`after_sky`、`after_transparent`、`after_camera_ui`、`final`、`after_screen_ui` | `after_gbuffer` 与 `after_opaque` 是 Stage，其余五个是 Composite |

<figure class="learn-figure">
  <img src="../assets/learn/renderstack-inspector.webp" alt="展示四个代表性挂载点列表的 RenderStack 局部网页式面板图" loading="lazy" decoding="async">
  <figcaption>这张网页式局部示意图只画了四个代表性列表，省略了 `after_sky`、`after_transparent` 和 Deferred 的 `after_gbuffer` 等内置 Stage；上表才是完整集合。当前原生 Inspector 的像素布局与图中不同。</figcaption>
</figure>

普通参数修改通常只更新 GPU 参数块。每个编译后的 Effect 绑定一个命名参数块，稳态帧只通过 `update_parameter_blocks` 上传发生变化的参数。Slot 顺序、EffectGroup 结构、影响拓扑的启用状态，以及列入 `topology_parameters` 的字段则让图失效并触发重建。单个 Effect 失败时，其修改会回滚，**Effect Compile Errors** 按 Stage/Slot 路径显示错误，新图可以跳过该 Effect 后继续完成。更大范围的拓扑失败时，Editor 保留上一份有效渲染图和 Inspector 拓扑。打包 Player 遇到管线构建失败时不会替换为 Default Forward。

## 五个术语，四层权威 {#terms-authority_1}

这些名称对应不同的数据：

| 术语 | 具体含义 | 权威来源 |
| --- | --- | --- |
| `injection_point` | RenderGraph 中供旧式外部 `RenderPass` 使用的命名边界。标准例子有 `before_post_process` 与 `after_post_process`。 | 管线决定它在拓扑中的位置。`FullScreenEffect.injection_point` 类字段无法替 `.effect` 完成挂载。 |
| `EffectStage` | 管线声明，包含 `stable_id`、Scope、资源输入/输出和 Capability。 | 活动管线决定有哪些 Stage、Stage 之间的顺序，以及各 Stage 拥有哪张图。 |
| mount point | 面向用户的“可挂载位置”概念。当前 RenderStack 中，一个 mount point 由一个 `EffectStage` 表示。 | `stable_id` 是场景与管线之间的契约。 |
| slot | 一条序列化 `EffectSlot`，包含 `slot_id`、`stage_id`、资产引用与启用状态。 | 场景 RenderStack 拥有每个已声明 Stage 的有序 Slot 列表。 |
| `.effectgroup` 条目 | 一条带 `entry_id`、启用状态和可选 Override 的引用，可以继续引用 Effect 或嵌套 Group。 | Group 文档决定该 Slot 内的条目顺序。 |

最终顺序按以下层级确定：

1. 活动管线拓扑排列各个 `EffectStage`。
2. RenderStack Slot 顺序排列同一 Stage 内的 Effect。
3. EffectGroup 在自己的 Slot 位置展开，并递归保留条目顺序。
4. 每个 Feature 按 `setup_passes()` 的调用顺序加入内部 Pass。

`default_order` 与 Effect 类的 `injection_point` 不参与这四步。它们是从 `RenderPass` 继承下来的元数据。把资产拖进 `final` 后，它的实际位置就是 `final`，即使类字段写着 `before_post_process`。

RenderStack 会拒绝向未声明 Stage 新增 Slot。管线变化导致旧 Stage 消失后，相关 Slot 会作为 Orphan 保留，并显示在 **Missing Effect Stages** 下。当前 Inspector 没有 Remap 按钮。在 Editor 脚本中已经持有相关 `stack` 组件时，可以调用公开修复入口 `stack.remap_orphan_effect_stage("old_stage", "new_stage")`。它会移动使用旧 ID 的全部 Orphan，保留顺序与 `slot_id`，让图失效并返回移动数量。目标 Stage 必须存在于活动管线中；调用后请保存场景。

## 四种效果作用域 {#scope_1}

`EffectScope` 说明 Stage 拥有哪一组图像：

| Scope | Effect 收到的图像 | 常见用途 |
| --- | --- | --- |
| Route | 一段 Queue 通过一条路径得到的结果 | 只像素化选定 Render Queue |
| Layer | 同一 Layer 内多条 Route 的合并结果 | 一起描边 Forward 与 Deferred 贡献 |
| Stage | 某个不透明域或透明域当前累计的全部内容 | 所有不透明物体之后加雾 |
| Composite | 管线运行到该位置时累计的场景 | 天空与透明物之后统一调色 |

Scope 属于 `EffectStage`，不会写进 `.effect` 资产或 Slot。同一份可复用资产只要满足资源契约和 Route Policy，就可以用于多个 Scope。

内置 Stage 遵循同样的累加顺序。`after_opaque` 只收到不透明域：场景颜色与深度，天空与透明物体还没进来。`after_sky` 加上天空盒。`after_transparent` 收到完整场景合成，仍在线性 HDR 空间。`after_camera_ui` 在其上叠加 Camera Overlay UI。`final` 位于显示编码之前，是后处理链的默认归宿。`after_screen_ui` 收到显示编码后的图像与 Screen Overlay UI。本课程第一章介绍了这个区别：Camera Overlay Canvas 在后处理前进入场景，Screen Overlay Canvas 在唯一的 linear-to-sRGB 转换之后绘制，因此默认不受场景效果影响。

Stage 契约会建立局部语义 Resource Bus。它的 `inputs` 决定 `color`、`depth`、`normal`、`motion` 等 Handle 能否到达 Effect。渲染器也会提前汇总启用资产的 `requires ∪ modifies`，以便请求可选几何 Buffer。两边必须一致：请求 `motion` 可以促使管线生成它；挂到没有暴露 `motion` 的 Stage 时，局部契约仍会失败。

稳定 ID 属于面向资产的 API。管线作者可以重构内部 Pass 名称与临时纹理，同时保持 `after_opaque` 或 `final` 不变。重命名 Stage 会改变场景契约，旧 Slot 将保持 Orphan 状态，直到完成 Remap。

下面这条最小高层管线通过当前公开调用暴露四种 Scope：

```python
from Infernux.renderstack import Queue, RenderPipeline


class ScopeProbePipeline(RenderPipeline):
    name = "Scope Probe"

    def define(self, pipeline):
        pipeline.frame(hdr=True)
        with pipeline.opaque() as opaque:
            with opaque.layer("Selected Objects") as selected:
                selected.forward(Queue(1000, 1099)).effects(
                    "route_probe", label="Route Probe"
                )
                selected.forward(Queue(1100, 1199))
                selected.effects("layer_probe", label="Layer Probe")
            opaque.otherwise().forward()
            opaque.effects("opaque_probe", label="Opaque Stage Probe")
        pipeline.effects("scene_probe", label="Scene Composite Probe")
        pipeline.screen_ui()
```

把这份 Python 文件保存到 `Assets`，选择 **Scope Probe**，再让测试物体分别使用 Material Queue `1000`、`1100`、`1200`。每次只把上一章的 Edge Fade 资产挂入一个 Probe 列表：

| 挂载位置 | 可观察的输入与结果 |
| --- | --- |
| `route_probe` | 只处理 Queue `1000..1099` 的 Forward Route，然后把结果送回所属 Layer。Queue `1100` 与 `1200` 保持原样。 |
| `layer_probe` | Queue `1000..1199` 的 Layer 合并后只处理一次。由 `otherwise()` 绘制的 Queue `1200` 保持原样。 |
| `opaque_probe` | 在 Selected Layer 与 `otherwise()` Route 完成后处理不透明 Domain 累计结果，三个 Queue 都会变化。 |
| `scene_probe` | 处理管线运行到该位置时可用的场景 Composite。本例中它位于不透明工作之后、标准 Screen UI 帧尾之前。 |

第一次验收时每次只启用一个 Probe Slot。四个全部启用会执行四份独立 Effect，并叠加视觉结果。

## 合成、Mask 与外溢 {#composition_1}

隔离 Route 的效果经常需要原始几何轮廓之外的像素。高斯模糊、Glow、位移和描边都会扩张贡献。把处理结果裁回原始 Mask 会丢掉外溢；合成时忽略深度，又可能盖住更近的几何体。

挂载 Scope 与 Route Policy 需要共同处理这个问题：

- `MASK_AND_MODIFY` 适合只改变已选像素的效果。
- `ISOLATE_AND_COMPOSITE` 适合可能向外扩张、需要通过深度与 Alpha 合回去的隔离贡献。
- `ADDITIVE_EXTRACT` 适合加到父图像上的光能。
- `INLINE` 直接使用当前贡献。
- Layer Scope 可以先把多条 Route 合成一张图，再统一处理。

两个互相遮挡的隔离效果仍需要管线做出选择：按 Route 顺序处理并合成，先并入 Layer，或移动到更后的 Stage。活动管线拓扑记录这项选择；Slot 顺序只处理已经位于同一 Stage 的 Effect。

可以用 Scope Probe 管线复现外溢与 Policy 冲突：

1. 通过 **Create > Render Effect > Bloom** 创建内置 Bloom，把它挂到 `route_probe`。让一个明亮的 Queue `1000` 物体轮廓与更近的 Queue `1200` 物体重叠。Bloom 使用 `ADDITIVE_EXTRACT`；光晕可以超出隔离轮廓，再通过 Route 的合成 Policy 返回父图。检查 Source Mask 外的光晕，也要检查近处物体的遮挡。
2. 在同一 `route_probe` 中把 Edge Fade 放到 Bloom 旁边。Edge Fade 使用颜色替换型 `MASK_AND_MODIFY`。Route Policy 合并会拒绝 Additive Extract 与颜色替换混用，并在图构建诊断中列出相关 Stage。把其中一个资产移到 `layer_probe` 或更后的 Composite Stage，或将其移除，然后确认图可以重建。
3. 对于自定义 `creates` 资源，可以挂入两个都会用同一语义调用 `bus.set()` 的 Effect。当前 ResourceBus 按顺序替换，后面的 Slot/Group 条目生效，也不会出现同名诊断。两份结果都需要保留时，请重命名其中一个语义。

## 同一资产多次挂载 {#repeat-mounts_1}

同一份 `.effect` 资产可以出现在多个 Slot 与 Scope 中。直接挂载会解析到共享的已加载 Source 文档，因此 Inspector 参数修改会影响每个直接挂载。每次挂载仍会建立独立 Feature 实例、Pass 链、参数块命名空间，并产生独立执行成本。运行时 Binding ID 使用 `<stage_id>/<slot_id>/<source_index>`，所以同一资产挂在 `route_probe` 与 `scene_probe` 时可以分别定位诊断。

Group 条目的 Override 属于 `.effectgroup` 文档。同一 Group 挂载两次时，两处会使用相同的 Group 有效值；修改展开值会更新该条目的 Override，并影响这个 Group 的所有挂载。`entry_id` 只要求在一份 Group 内唯一，不同 Group 可以复用同一 ID。运行时 Stage 诊断使用所属 Stage、Slot 与展开后的 Source Index 定位，不会把 `entry_id` 当成全局唯一地址。

执行顺序始终由管线 Stage 顺序、Slot 顺序、递归 Group 顺序、Feature 生成 Pass 的顺序共同确定。重复引用资产不会合并执行。

## Orphan、失败与保存重载证据 {#verification_1}

修改 RenderStack 后可以完成以下检查：

1. **Orphan：** 在 `route_probe` 挂入 Edge Fade，保存场景，把声明改名为 `route_probe_v2`，再保存 Python 文件。**Missing Effect Stages** 应列出 `route_probe` 及其资产引用。旧 Slot 继续序列化，但不会执行。调用 `stack.remap_orphan_effect_stage("route_probe", "route_probe_v2")`，预期返回 `1`；保存场景并确认警告消失。
2. **单 Effect 失败：** 复制一份 Edge Fade，添加 `requires = {"color", "missing_probe"}`，并在 `setup_passes()` 开头加入 `if bus.get("missing_probe") is None: raise ValueError("missing effect-stage resource: missing_probe")`。把它挂到 `route_probe`。**Effect Compile Errors** 应包含 `<stage>/<slot>` 与 `missing_probe`。该 Effect 生成的 Pass 与 Bus 发布都会移除，其它 Slot 可以继续显示结果。这里需要显式检查，因为声明负责描述契约，自定义实现仍要负责 Bus 校验。
3. **上一份有效拓扑：** 先让有效图显示在画面中，再临时重复声明一个 `scene_probe` 稳定 ID 并保存。Editor 会记录 `Pipeline topology is invalid`，继续显示上一份有效 Inspector 拓扑与渲染图。删除重复声明并再次保存后，失效机制允许重新构建。保留画面只能证明旧图仍在工作；还需确认诊断清空，并故意修改一个可见参数，才能证明新图已经生效。
4. **保存与重载：** 在 `scene_probe` 放入两个名称不同的资产，记下从上到下的顺序与启用状态，保存场景，关闭后重新打开。Inspector 应恢复相同顺序与状态。需要源码管理证据时，可以检查场景序列化字段 `effect_slots`：每项记录 `slot_id`、`stage_id`、GUID/路径引用与启用值。资产参数仍保存在各自的 `.effect` 或 `.effectgroup` 文档中。

## 内置管线的帧尾 {#frame-tail_1}

内置管线采用以下准确顺序：

1. `_ScreenUI_Camera` 把 Camera Overlay Canvas 画入场景颜色。
2. `after_camera_ui` 暴露一个 `EffectStage`。
3. `before_post_process` 标记旧式 injection point。
4. `final` 暴露最终后处理 `EffectStage`。
5. `after_post_process` 标记旧式 injection point。
6. `_DisplayEncode` 把线性 RGB 限制到 `[0,1]`，转换为 sRGB，可选应用 Camera Dithering，然后写入 `_display_encode`；`_DisplayEncode_Commit` 再把编码结果复制回场景颜色。
7. `_ScreenUI_Overlay` 把 Screen Overlay Canvas 画在编码后的图像上。
8. `after_screen_ui` 把完整的显示空间图像暴露为 `EffectStage`。

这些命名位置有各自的颜色与 UI 语义：

- **`after_camera_ui`** 接收 Camera Overlay UI 已经加入的线性场景颜色。挂在这里的 Effect 会处理这部分 UI，后续 `final` 链也会继续处理结果。
- **`final`** 位于显示编码之前。它只是一份有序挂载列表，不含内置 Tone Mapping Pass。内置 Tone Mapping 资产属于可选项；挂到这里后，它会把线性 HDR 压缩成线性 LDR。请通过 Slot 或 Group 顺序把 HDR Effect 放在它前面，把线性 LDR Effect 放在后面。
- **显示编码** 负责全帧唯一一次 linear-to-sRGB 转换。没有挂 Tone Mapping 时，超过 `1.0` 的数值会在这里被裁掉。Tone Mapping 与显示编码是两项独立操作。
- **Screen UI** 是显示编码后的 Draw Pass，不属于 Effect Stage。Screen Overlay 菜单和 HUD 默认不会经过场景 Bloom、调色与 Tone Mapping。
- **`after_screen_ui`** 接收编码后的场景颜色与 Screen UI。它后面没有第二次显示编码。挂在这里的 Effect 必须按显示空间工作，并为 Present 保持编码后的 `[0,1]` 结果。

连续帧尾如下：

```text
Camera UI -> after_camera_ui -> before_post_process -> final
          -> after_post_process -> Display Encode + Commit
          -> Screen UI -> after_screen_ui -> graph output -> presentation
```

只有用户把 Tone Mapping `.effect` 资产挂入 `final` 后，它才会出现在链中：

```text
final: [HDR effects] -> [Tone Mapping .effect, optional] -> [linear-LDR effects]
```

自定义管线遗漏终端段时，RenderStack Finalization 会保证显示编码、Screen UI Overlay 与 `after_screen_ui` 存在。`after_camera_ui` 和 `final` 仍由管线声明；全部内置管线都会声明这两个 Stage。

这也解释了当前 Effect Stages Inspector 中看不到 `before_post_process` 与 `after_post_process` 的原因：它们属于 injection point。资产作者把后处理 Effect 挂进 `final`，再通过 Slot 或 `.effectgroup` 排列执行链。

**证据说明。** 上述 Stage 集合与帧尾来自当前源码中的 `default_forward_pipeline.py`、`default_forward_plus_pipeline.py`、`default_deferred_pipeline.py`、`graph.py::screen_ui_section`、`display_encode.frag` 与 `render_stack.py`。Scope Probe 的调用与 `pipeline_dsl.py` 一致；Orphan 身份和诊断规则对应 `EffectSlot`、`remap_orphan_effect_stage()` 与 `compile_effect_slots()`。
