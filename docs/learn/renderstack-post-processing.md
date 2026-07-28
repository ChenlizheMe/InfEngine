# RenderStack and reusable post-processing

RenderStack separates three decisions that are often tangled together:

- **RenderPipeline** defines the frame topology: shadows, geometry routes, sky, effect stages, and screen UI.
- **RenderStack** is the scene component that selects a pipeline and mounts ordered effects at its declared stages.
- **RenderEffect** is a reusable, material-like asset containing one effect type and its live parameters.

The pipeline owns structure. Effect assets own reusable settings. The scene only binds the two together.

## Use RenderStack in a scene

Create one GameObject with a `RenderStack` component. Select a pipeline in the Inspector, then add `.effect` or `.effectgroup` assets to the stages exposed by that pipeline.

Each stage is an ordered list. Reordering slots changes effect order; disabling a slot keeps the reference but skips the effect. A pipeline may expose stages such as `after_opaque`, `after_sky`, `after_transparent`, or `final`, but custom pipelines should choose stable IDs that describe their own intent.

An `EffectStage` is a mount point, not the effect itself. Changing an effect parameter does not reconstruct the pipeline topology. Changing the selected pipeline, a route, or a topology-affecting parameter does.

## Effect assets

A `.effect` file is strict, readable JSON. It resembles a material asset: `feature_type` selects the implementation and `parameters` stores editable values.

```json
{
  "$schema": "infernux.render_effect",
  "dependencies": [],
  "feature_type": "infernux.post.bloom",
  "parameters": {
    "clamp": 65472.0,
    "intensity": 0.8,
    "max_iterations": 5,
    "scatter": 0.7,
    "threshold": 1.0,
    "tint": [1.0, 1.0, 1.0, 1.0]
  }
}
```

Editing the asset in its Inspector updates every stack that references the shared instance. Editing it inline under a RenderStack slot updates the same asset. The runtime revision system uploads changed values without rebuilding the graph unless the registered feature marks that value as topology-changing.

An `.effectgroup` stores a reusable ordered chain:

```json
{
  "$schema": "infernux.render_effect_group",
  "entries": [
    {
      "entry_id": "bloom",
      "asset": {
        "guid": "",
        "path_hint": "Assets/Rendering/Bloom.effect"
      },
      "enabled": true,
      "overrides": {}
    },
    {
      "entry_id": "tone_mapping",
      "asset": {
        "guid": "",
        "path_hint": "Assets/Rendering/ACES Tone Mapping.effect"
      },
      "enabled": true,
      "overrides": {}
    }
  ]
}
```

References are GUID-first with a readable `path_hint`, so moves remain recoverable while source control and AI tools can still understand the document.

## Change an effect at runtime

`RenderEffect` exposes material-style typed accessors:

```python
from Infernux.renderstack import RenderStack

stack = RenderStack.instance()
if stack is not None:
    bloom = stack.get_effect("final", 0)
    if bloom is not None:
        bloom.set_float("intensity", 1.2)
        current = bloom.get_float("intensity")
```

There are matching `set_` and `get_` methods for float, int, bool, vector2, vector3, vector4, and color values, plus generic `set_param()` and `get_param()`. Loaded effect assets are shared. Clone an effect when a temporary runtime change must not affect other references or its source asset.

## Write a custom pipeline

Python is the public entry point for custom pipelines. New code should override `define()` and use the declarative builder. The compiler validates queue ownership and effect scope before creating RenderGraph resources.

```python
from Infernux.renderstack import Path, Queue, RenderPipeline


class MixedPipeline(RenderPipeline):
    name = "Mixed Pipeline"

    def define(self, pipeline):
        pipeline.frame(hdr=True, msaa=4)
        pipeline.shadows(resolution=4096)
        pipeline.lighting(clustered=True)

        with pipeline.opaque() as opaque:
            with opaque.layer("Stylized Objects") as layer:
                # Materials in Queue 1-100 use Forward and can receive
                # effects mounted at the low_queue stage.
                layer.forward(Queue(1, 100)).effects("low_queue")

                # Materials in Queue 101-200 prefer Deferred. If a material
                # cannot use that path, it falls back to Forward+.
                layer.deferred(
                    Queue(101, 200),
                    fallback=Path.FORWARD_PLUS,
                ).effects("middle_queue")

                # This stage receives the combined result of both routes.
                layer.effects("stylized_combined")

            # Consume all remaining opaque queues.
            opaque.otherwise().forward_plus()
            opaque.effects("opaque_only")

        # Composite stages see everything accumulated so far.
        pipeline.effects("after_opaque")
        pipeline.sky()
        pipeline.effects("after_sky")

        with pipeline.transparent() as transparent:
            transparent.otherwise().forward_plus()
            transparent.effects("transparent_only")

        pipeline.effects("final")
        pipeline.screen_ui()
```

Queue intervals are inclusive. The conventional opaque range is `0-2500`, transparent is `2501-5000`, and the complete supported range is `0-9999`. A queue can be claimed by only one route in the same domain. `otherwise()` consumes the unclaimed remainder.

The location of `.effects()` determines what it sees:

| Declaration | Scope | Input it receives |
| --- | --- | --- |
| `route.effects(...)` | Route | One queue route only |
| `layer.effects(...)` | Layer | Routes combined inside that layer |
| `opaque.effects(...)` | Stage/domain | The complete opaque stage at that point |
| `pipeline.effects(...)` | Composite | Everything accumulated so far |

This is how the same effect can process a few selected materials, a group of routes, or the final camera image without changing the effect implementation.

## Write a general post-processing effect

For a new fullscreen feature, subclass `FullScreenEffect`, expose parameters with `serialized_field`, and register a stable namespaced feature ID.

```python
from Infernux.components.serialized_field import serialized_field
from Infernux.renderstack import (
    FullScreenEffect,
    RoutePolicy,
    register_render_effect_feature,
)


class EdgeFadeEffect(FullScreenEffect):
    name = "Edge Fade"
    intensity: float = serialized_field(default=0.35, range=(0.0, 1.0))

    def get_shader_list(self):
        return ["Fullscreen Triangle", "Edge Fade"]

    def setup_passes(self, graph, bus):
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_edge_fade_out",
            pass_name="EdgeFade_Apply",
            shader_name="Edge Fade",
            format=Format.RGBA16_SFLOAT,
            params={"intensity": self.intensity},
        )


register_render_effect_feature(
    "game.post.edge_fade",
    EdgeFadeEffect,
    route_policy=RoutePolicy.MASK_AND_MODIFY,
)
```

The matching asset uses `"feature_type": "game.post.edge_fade"`. Keep the feature ID stable after assets begin referencing it.

## Route policies

Effects mounted on a queue route need an image ownership policy:

- `MASK_AND_MODIFY` modifies selected pixels while preserving the existing composite around them.
- `ISOLATE_AND_COMPOSITE` renders the route into an isolated image and composites the result back.
- `ADDITIVE_EXTRACT` contributes an additive signal such as bloom extraction.
- `INLINE` needs no isolated route image.
- `CUSTOM_FEATURE` is reserved for a feature that owns specialized composition.

Choose this based on image semantics, not convenience. Mixing incompatible color-replacement and additive policies on one route is rejected rather than producing an ambiguous composite.

## Practical rules

- Put reusable tuning in `.effect`; put frame structure in `RenderPipeline`.
- Use a route stage for selected material queues and a composite stage for the whole image-so-far.
- Keep stage IDs stable. Renaming one can orphan serialized slots until they are remapped.
- Parameter-only edits should advance an effect revision, not call `invalidate_graph()` every frame.
- Put tone mapping after HDR effects such as bloom unless the visual design explicitly requires display-space processing.
- `screen_ui()` must be the final pipeline operation.
- Use the low-level `define_topology(graph)` API only when the declarative pipeline cannot express the required resources or pass dependencies.

---

# RenderStack 与通用后处理

RenderStack 把经常混在一起的三类职责分开：

- **RenderPipeline** 定义一帧的结构，包括阴影、几何 Route、天空、EffectStage 与屏幕 UI。
- **RenderStack** 是场景组件，用于选择管线，并把有序效果挂到管线声明的阶段上。
- **RenderEffect** 是类似材质的可复用资产，保存一种效果类型及其实时参数。

管线负责结构，Effect 资产负责可复用设置，场景只负责把二者绑定起来。

## 在场景中使用 RenderStack

创建一个带 `RenderStack` 组件的 GameObject。先在 Inspector 选择管线，再把 `.effect` 或 `.effectgroup` 资产加入该管线公开的阶段。

每个阶段都是有序列表。调整 Slot 顺序会改变效果顺序；禁用 Slot 会保留引用但跳过效果。默认管线可能公开 `after_opaque`、`after_sky`、`after_transparent` 和 `final`，自定义管线则应使用能稳定表达意图的 ID。

`EffectStage` 是挂载点，不是效果本身。调整效果参数不应重建管线拓扑；切换管线、改变 Route 或修改影响拓扑的参数才需要重建。

## Effect 资产

`.effect` 是严格且可读的 JSON。它和材质资产很像：`feature_type` 选择实现，`parameters` 保存可编辑参数。

```json
{
  "$schema": "infernux.render_effect",
  "dependencies": [],
  "feature_type": "infernux.post.bloom",
  "parameters": {
    "clamp": 65472.0,
    "intensity": 0.8,
    "max_iterations": 5,
    "scatter": 0.7,
    "threshold": 1.0,
    "tint": [1.0, 1.0, 1.0, 1.0]
  }
}
```

在资产 Inspector 里修改它，会更新所有引用同一个实例的 RenderStack；在 RenderStack Slot 下内联修改，也会反映到同一份资产。运行时通过 Revision 上传新值，只有被 Feature 标记为影响拓扑的参数才会触发 Graph 重建。

`.effectgroup` 用来保存可复用的有序效果链：

```json
{
  "$schema": "infernux.render_effect_group",
  "entries": [
    {
      "entry_id": "bloom",
      "asset": {
        "guid": "",
        "path_hint": "Assets/Rendering/Bloom.effect"
      },
      "enabled": true,
      "overrides": {}
    },
    {
      "entry_id": "tone_mapping",
      "asset": {
        "guid": "",
        "path_hint": "Assets/Rendering/ACES Tone Mapping.effect"
      },
      "enabled": true,
      "overrides": {}
    }
  ]
}
```

资产引用以 GUID 为主，并保留可读的 `path_hint`。移动资产后仍可恢复引用，源码管理和 AI 工具也能看懂文件。

## 在运行时修改效果

`RenderEffect` 提供和材质类似的类型化接口：

```python
from Infernux.renderstack import RenderStack

stack = RenderStack.instance()
if stack is not None:
    bloom = stack.get_effect("final", 0)
    if bloom is not None:
        bloom.set_float("intensity", 1.2)
        current = bloom.get_float("intensity")
```

Float、Int、Bool、Vector2、Vector3、Vector4 和 Color 都有对应的 `set_` / `get_` 方法，也可以使用通用的 `set_param()` 与 `get_param()`。载入的 Effect 资产默认共享；临时运行时变化不应影响其它引用或源文件时，应先 Clone。

## 编写自定义管线

Python 是自定义 RenderPipeline 的公开入口。新管线覆写 `define()`，通过声明式 Builder 编写。编译器会先验证 Queue 所有权与 Effect Scope，再创建 RenderGraph 资源。

```python
from Infernux.renderstack import Path, Queue, RenderPipeline


class MixedPipeline(RenderPipeline):
    name = "Mixed Pipeline"

    def define(self, pipeline):
        pipeline.frame(hdr=True, msaa=4)
        pipeline.shadows(resolution=4096)
        pipeline.lighting(clustered=True)

        with pipeline.opaque() as opaque:
            with opaque.layer("Stylized Objects") as layer:
                # Queue 1-100 的材质走 Forward，并可在 low_queue
                # 挂载点处理这一条 Route 的结果。
                layer.forward(Queue(1, 100)).effects("low_queue")

                # Queue 101-200 优先走 Deferred；不兼容时回退 Forward+。
                layer.deferred(
                    Queue(101, 200),
                    fallback=Path.FORWARD_PLUS,
                ).effects("middle_queue")

                # 同时处理上面两条 Route 合并后的结果。
                layer.effects("stylized_combined")

            # 消费还没有被领取的不透明 Queue。
            opaque.otherwise().forward_plus()
            opaque.effects("opaque_only")

        # Composite 阶段接收目前为止已经积累的画面。
        pipeline.effects("after_opaque")
        pipeline.sky()
        pipeline.effects("after_sky")

        with pipeline.transparent() as transparent:
            transparent.otherwise().forward_plus()
            transparent.effects("transparent_only")

        pipeline.effects("final")
        pipeline.screen_ui()
```

Queue 区间包含两端。约定的不透明范围是 `0-2500`，透明范围是 `2501-5000`，完整支持范围是 `0-9999`。同一个 Domain 中，一段 Queue 只能被一条 Route 领取；`otherwise()` 负责消费剩余部分。

`.effects()` 写在哪里，决定了它会看到什么：

| 声明位置 | Scope | 接收到的内容 |
| --- | --- | --- |
| `route.effects(...)` | Route | 单独一条 Queue Route |
| `layer.effects(...)` | Layer | 该 Layer 内多条 Route 的合并结果 |
| `opaque.effects(...)` | Stage/Domain | 此时完整的不透明阶段 |
| `pipeline.effects(...)` | Composite | 截至当前位置累计的全部画面 |

因此同一个效果既可以只处理几个特定材质，也可以处理一组 Route，或作用于最终相机画面，而无需重写效果实现。

## 编写通用后处理

新建全屏效果时，继承 `FullScreenEffect`，用 `serialized_field` 暴露参数，再注册稳定的命名空间 Feature ID。

```python
from Infernux.components.serialized_field import serialized_field
from Infernux.renderstack import (
    FullScreenEffect,
    RoutePolicy,
    register_render_effect_feature,
)


class EdgeFadeEffect(FullScreenEffect):
    name = "Edge Fade"
    intensity: float = serialized_field(default=0.35, range=(0.0, 1.0))

    def get_shader_list(self):
        return ["Fullscreen Triangle", "Edge Fade"]

    def setup_passes(self, graph, bus):
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_edge_fade_out",
            pass_name="EdgeFade_Apply",
            shader_name="Edge Fade",
            format=Format.RGBA16_SFLOAT,
            params={"intensity": self.intensity},
        )


register_render_effect_feature(
    "game.post.edge_fade",
    EdgeFadeEffect,
    route_policy=RoutePolicy.MASK_AND_MODIFY,
)
```

对应资产使用 `"feature_type": "game.post.edge_fade"`。一旦有资产引用这个 ID，就应保持稳定。

## Route Policy

挂在 Queue Route 上的效果需要声明图像所有权策略：

- `MASK_AND_MODIFY` 修改选中像素，同时保留周围已有 Composite。
- `ISOLATE_AND_COMPOSITE` 把 Route 渲染到独立图像，再合成回来。
- `ADDITIVE_EXTRACT` 提供 Bloom 提取一类的加法信号。
- `INLINE` 不需要独立 Route 图像。
- `CUSTOM_FEATURE` 留给自己管理特殊合成的高级 Feature。

这里应按图像语义选择，而不是按写起来是否省事。互不兼容的颜色替换与加法策略挂在同一 Route 时，系统会拒绝它们，而不是生成语义不清的合成结果。

## 实用规则

- 可复用参数放进 `.effect`，一帧的结构放进 `RenderPipeline`。
- 处理指定材质 Queue 时使用 Route Stage，处理当前整张画面时使用 Composite Stage。
- 保持 Stage ID 稳定。改名会让已序列化 Slot 暂时成为孤立项，直到完成重映射。
- 仅修改参数时推进 Effect Revision，不要每帧调用 `invalidate_graph()`。
- Bloom 等 HDR 效果通常放在 Tone Mapping 之前，除非美术设计明确要求显示空间处理。
- `screen_ui()` 必须是管线最后一个操作。
- 只有声明式管线无法表达资源或 Pass 依赖时，才使用底层 `define_topology(graph)`。
