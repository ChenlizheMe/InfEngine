<!-- language:en -->

<span class="mini-tag">Custom Rendering · Chapter 4</span>

# Shading models and GLSL libraries

A `.frag` assembles `SurfaceData`. A `.shadingmodel` defines how that surface responds to light. A `.glsl` file holds reusable functions. Render-path adapters connect those contracts to Forward, Forward+, and eligible Deferred routes.

PBR, unlit color, toon bands, halftone lighting, skin response, and project-specific lighting belong in a ShadingModel. Infernux gives every model the fixed `shading()` entry shown below.

<div class="learn-article-toc"><strong>In this chapter</strong><a href="#complete-example">Build a complete model</a><a href="#discovery-diagnostics">Discovery and diagnostics</a><a href="#pass-variants">Pass variants</a><a href="#requirements">Requirements and Deferred limits</a><a href="#design-boundary">Keep the boundary clean</a></div>

<div class="learn-note"><strong>First-pass finish line.</strong><p>Create the three files in the complete example, select the new fragment stage on one Material, and confirm that a lit Cube changes appearance without Console errors. Stop there on a first reading; importer diagnostics and Deferred boundaries are reference material for when that visible result fails or the model grows.</p></div>

<figure class="learn-figure">
  <img src="../assets/learn/real-render-styles.webp" alt="style reference showing two contrasting rendered character appearances" loading="lazy" decoding="async">
  <figcaption>Style reference only. The workspace contains this WebP, but no matching scene, Material, ShadingModel, RenderStack configuration, source revision, or capture record that can reproduce it.</figcaption>
</figure>

## Build a complete three-file model {#complete-example}

Create this folder under the current project:

```text
Assets/Shaders/LearnBand/
  learn_band_math.glsl
  learn_band.shadingmodel
  learn_band_surface.frag
```

The Project panel currently creates **Fragment Shader (.frag)** and **Vertex Shader (.vert)** assets, but it has no creation command for `.glsl` or `.shadingmodel`. Create those two UTF-8 text files with an external or configured text editor and place them under `Assets`. This is the current authoring limitation; changing their extension or inventing another asset type will not make them discoverable.

Put this reusable function in `learn_band_math.glsl`:

```glsl
ShaderInfo {
    Name "Learn Band Math"
}

float learnBand(float ndl, float threshold) {
    return mix(0.25, 1.0, step(threshold, ndl));
}
```

Put the lighting entry in `learn_band.shadingmodel`:

```glsl
ShadingModelInfo {
    Name "Learn Band"
    Imports ["Lighting", "Learn Band Math"]
    Requires [Lighting]
}

void shading(in SurfaceData s, out vec4 color) {
    ShadingContext ctx = GetShadingContext();
    vec3 N = normalize(s.normalWS);
    Light mainLight = getMainLight(ctx.positionWS, N, ctx.viewDepth);
    float ndl = max(dot(N, mainLight.direction), 0.0);
    float band = learnBand(ndl, clamp(s.shadingParam0, 0.0, 1.0));
    vec3 radiance = mainLight.color
                  * mainLight.attenuation
                  * mainLight.shadow;
    color = vec4(s.albedo * radiance * band + s.emission, s.alpha);
}
```

Put the Material-facing surface in `learn_band_surface.frag`:

```glsl
#version 450

ShaderInfo {
    Name "Learn Band Surface"
    ShadingModel "Learn Band"
    Surface Opaque
    Queue 2000
    Properties {
        Color baseColor = [0.9, 0.3, 0.08, 1.0]
        Float threshold = 0.55 Range(0.0, 1.0)
        Texture2D mainTexture = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texel = sampleAlbedoAlpha(mainTexture);
    s.albedo = texel.rgb * getVertexColor() * material.baseColor.rgb;
    s.alpha = texel.a * material.baseColor.a;
    s.shadingParam0 = material.threshold;
}
```

The dependency graph for these exact files is:

```text
Material -> Standard + Learn Band Surface
Learn Band Surface --ShadingModel--> Learn Band
Learn Band --Imports--> Lighting
Learn Band --Imports--> Learn Band Math
```

To run it end to end:

1. Let the Project panel discover the three files, then select or reimport `learn_band_surface.frag`. The Console must have no shader import, parse, link, or pipeline compile error.
2. Create a **Material (.mat)**. In its Inspector set **Vertex** to `Standard` and **Fragment** to `Learn Band Surface`.
3. Create **3D Object > Cube**, assign the Material to **MeshRenderer > Materials > Element 0**, and keep an enabled Camera and Directional Light in the scene.
4. Change **Threshold** between `0.2` and `0.8`. The hard light band should move while the texture and Base Color remain Material-owned.

This example has a deliberately narrow lighting promise. `getMainLight()` evaluates the first directional light and includes its attenuation/shadow term. Additional directional, point, spot, and area lights, ambient probes, and indirect lighting do not contribute. There is no declaration that automatically upgrades a model to full light support. Use the loops in the built-in `PBR` and `Toon` models when the intended model must cover those lights.

## Placement, discovery, and diagnostics {#discovery-diagnostics}

The loader recursively scans the dependent `.frag` file's parent directory and the built-in shader roots for `.vert`, `.frag`, `.glsl`, and `.shadingmodel` files; `_templates` directories are excluded. `Name` values are exact, case-sensitive IDs. Project declarations in the fragment's directory tree take precedence over built-in fallbacks. Avoid duplicate project IDs: recursive discovery does not issue a dedicated duplicate-name diagnostic, and a later discovered declaration can replace an earlier one.

Runtime reload currently accepts `.vert` and `.frag` assets only. After editing `learn_band_math.glsl` or `learn_band.shadingmodel`, save, touch, or reimport `learn_band_surface.frag`; restarting the Editor also rebuilds discovery. This exact limitation means a dependency save can leave the previous GPU program visible until a dependent root stage is reloaded.

Use this failure matrix before debugging pixels:

| Reproduction | Expected diagnostic and behavior |
| --- | --- |
| Change `Learn Band Math` to a missing ID, then reimport the `.frag` | The generated source contains `shader import not found`; unresolved `learnBand` can follow. The new program is rejected. |
| Remove `Imports ["Lighting", ...]` but keep `Requires [Lighting]` | Lighting declarations/helpers are absent, so symbols such as `Light` or `getMainLight` fail compilation. |
| Keep the import but remove `Requires [Lighting]` | The lighting library is linked, but its camera-local resources are not bound/injected; the dependent variant fails compilation. |
| Introduce invalid GLSL in the library, then reimport the `.frag` | The Console reports the dependent root `.frag` compile failure and the last-known-good program remains active. |

Imports are expanded recursively, with cycle suppression and a maximum nesting depth of 16. Current generated shader diagnostics do not add source-file `#line` mapping for imported text, so a compiler line number may refer to the expanded/root source. Start with the first import error, inspect the named library, and reimport the root `.frag` after the fix. A visible old result proves only that last-known-good fallback worked; a cleared Console plus a deliberate Threshold change proves the new program became active.

## How one model becomes many pass variants {#pass-variants}

One vertex/fragment pair compiles into several programs. The engine enumerates nine material pass targets: `Forward`, `ForwardPlus`, `GBuffer`, `Shadow`, `Depth`, `Picking`, `Motion`, `Normal`, and `BaseColor` (`ShaderTypes.h`). For each target, `InxShaderLoader` regenerates the GLSL with a matching `main()` template:

- Forward and Forward+ call `surface()`, then `shading()` with the per-camera lighting resources (`surface_main.glsl`); the Forward+ target additionally binds the tiled light grid.
- GBuffer writes `packGBuffer()` into the five deferred render targets (`surface_main_gbuffer.glsl`).
- Shadow and Depth run alpha clipping only and write depth.
- Picking writes the stable object identity, Motion writes velocity, and Normal and BaseColor write the geometry-stage buffers that effects such as Motion Blur or TAA can consume.

The shared surface code stays identical; the adapter templates supply the pass-specific output. `ShaderPassVariantPlanner` decides which targets compile for a given pair. GBuffer is enabled for opaque materials whose ShadingModel does not declare `Unsupported [Deferred]` and does not force forward. Alpha clipping is shared by every compatible variant, so a cutout surface clips consistently in color, shadow, and picking passes.

## Requirements and Deferred limits {#requirements}

`Imports ["Lighting"]` links declarations and helper functions. `Requires [Lighting]` asks the compiler and pipeline contract to supply camera-local lighting data. Both are required by this sample, and neither selects a render route.

The compiler makes a valid model eligible for Deferred when it has `void shading(in SurfaceData s, out vec4 color)` and does not declare `Unsupported [Deferred]`. The canonical GBuffer stores `albedo`, encoded world normal, `smoothness`, `metallic`, `occlusion`, `specularHighlights`, `emission`, `alpha`, `shadingParam0`, and `shadingParam1`, plus the object light-layer mask and ShadingModel ID. The packing is concrete: target 0 holds base color and alpha (RGBA16F), target 1 holds the encoded normal plus `smoothness` in alpha (RGBA16F), target 2 holds `metallic`, `occlusion`, `specularHighlights`, and `shadingParam0` (RGBA8), target 3 holds emission plus `shadingParam1` (RGBA16F), and target 4 holds the light-layer mask and ShadingModel ID (RG32_UINT). `deferred_lighting.frag` reconstructs world position from depth, decodes those targets into a fresh `SurfaceData` and `ShadingContext`, and calls `inxDispatchShading(modelId, color)`; the compiler-generated registry appends every Deferred-capable model's `shading()` under its stable ID.

`Learn Band` is eligible because its custom threshold fits in `shadingParam0` and its remaining inputs come from stored Surface fields or reconstructed context. Test the boundary explicitly:

1. With no RenderStack, or with **Default Forward** selected in a RenderStack, confirm the band responds to Threshold and the first Directional Light.
2. Select **Default Deferred** in the RenderStack Inspector. The opaque Material is Deferred-compatible and should be written to the GBuffer, then dispatched by its ShadingModel ID.
3. Add `Unsupported [Deferred]` to `Learn Band`, reimport `learn_band_surface.frag`, and check again. The built-in Deferred pipeline omits that pair's GBuffer variant and draws the opaque object through `DeferredForwardFallbackPass`, which uses Forward+ lighting. Internally the GBuffer pass filters for Deferred-compatible materials (`deferred_compatible`), and the fallback pass filters for the rest (`deferred_unsupported`). Transparent surfaces also use the transparent Forward+ pass.
4. The declarative DSL enforces one more rule: with MSAA above 1, a Deferred route must declare an explicit `Forward` or `Forward+` fallback, and the compiler applies that fallback to the whole route (`pipeline_compiler.py` rejects a Deferred route without one).
5. Remove that declaration only when the model can be reconstructed from the listed fields. The compiler checks syntax and declarations; it cannot infer that a tangent-frame-dependent or extra-data-dependent result is semantically wrong. Such a model may compile and render incorrect Deferred pixels.

A valid `Unsupported [Deferred]` declaration does not produce a warning: fallback is an expected built-in route. The current Material Inspector also has no per-material "Deferred fallback" status. Use the controlled toggle above, a clear Console, and the active RenderStack pipeline as the reproducible evidence. Compile errors are actionable diagnostics; a semantic mismatch that still compiles remains the ShadingModel author's responsibility.

Deferred substitutes the stored shading normal for `geometricNormalWS`, synthesizes a tangent, and reports `frontFacing = true`. It does not preserve the original tangent frame, a separate geometric normal, face orientation, geometry-pass derivative behavior, or model data beyond the two shading scalars. Declare the restriction for models that need any of those inputs:

```glsl
ShadingModelInfo {
    Name "SixWaySmoke"
    Imports ["Lighting"]
    Requires [Lighting]
    Unsupported [Deferred]
}
```

A custom pipeline must provide a compatible fallback for an unsupported model or reject that route. If a selected model is missing, malformed, or fails GLSL compilation, the linked artifact is not published; the renderer retains its last-known-good program when one exists and reports the failure in the Console.

<div class="learn-note"><strong>Current API boundary.</strong><p>`ShadingModelInfo` has no Capabilities list and the compiler does not prove semantic Deferred compatibility. `Unsupported [Deferred]` is the author's explicit opt-out from the canonical GBuffer path.</p></div>

## Keep the boundary clean {#design-boundary}

| Owner | Responsibility |
| --- | --- |
| `.vert` | Vertex position and authored varyings |
| `.frag` | Material properties, texture sampling, and `SurfaceData` assembly |
| `.shadingmodel` | The `SurfaceData` and light interaction exposed through `shading()` |
| `.glsl` | Reusable functions and constants with no pass-routing policy |
| Pipeline adapter | Generated Forward/Forward+ calls, GBuffer packing, Deferred reconstruction and dispatch, requirements, and fallbacks |
| RenderPipeline / RenderEffect | Queue domains, pass order, graph resources, and whole-image effects |

These boundaries keep lighting code readable and leave route decisions with the code that owns the render graph. The next chapter moves from per-object shading to whole-image effects.

**Evidence note.** File discovery and reload behavior above follow the current `InxShaderLoader` and runtime reload path. The GBuffer fields and fallback behavior follow the current Deferred templates, deferred lighting stage, material pass planner, and built-in `default_deferred_pipeline.py`. The WebP at the top remains a visual style reference because its source configuration and revision are absent from this workspace.

<!-- language:zh -->

<span class="mini-tag">自定义渲染 · 第 4 章</span>

# ShadingModel 与 GLSL 函数库

`.frag` 组装 `SurfaceData`，`.shadingmodel` 定义表面对光照的响应，`.glsl` 保存可复用函数。渲染路径适配器把这些契约接到 Forward、Forward+ 与满足条件的 Deferred 路径。

PBR、无光照颜色、色阶卡通、半调、皮肤响应和项目自己的美术光照都属于 ShadingModel。每个模型都使用下文展示的固定 `shading()` 入口。

<div class="learn-article-toc"><strong>本章内容</strong><a href="#complete-example_1">构建完整模型</a><a href="#discovery-diagnostics_1">发现与诊断</a><a href="#pass-variants_1">Pass 变体</a><a href="#requirements_1">依赖与 Deferred 限制</a><a href="#design-boundary_1">保持边界清晰</a></div>

<div class="learn-note"><strong>第一次阅读的完成点。</strong><p>按完整示例创建三份文件，把新的片元阶段选到一个 Material 上，并确认受光 Cube 的外观发生变化且 Console 没有错误。做到这里即可先进入下一章；导入诊断和 Deferred 边界留给结果异常或模型继续扩展时查阅。</p></div>

<figure class="learn-figure">
  <img src="../assets/learn/real-render-styles.webp" alt="展示两种对比鲜明人物渲染外观的风格参考图" loading="lazy" decoding="async">
  <figcaption>本图只作为风格参考。工作区含有这份 WebP，但缺少可复现它的 Scene、Material、ShadingModel、RenderStack 配置、源码版本与捕获记录。</figcaption>
</figure>

## 构建完整的三文件模型 {#complete-example_1}

在当前项目中创建以下目录与文件：

```text
Assets/Shaders/LearnBand/
  learn_band_math.glsl
  learn_band.shadingmodel
  learn_band_surface.frag
```

Project 面板目前可以创建 **片段着色器 (.frag)** 与 **顶点着色器 (.vert)** 资产，没有 `.glsl` 或 `.shadingmodel` 的创建命令。请用外部编辑器或已配置的文本编辑器创建这两个 UTF-8 文本文件，再放入 `Assets`。这是当前编写入口的明确限制；修改扩展名或自造资产类型无法让加载器识别它们。

把以下可复用函数写入 `learn_band_math.glsl`：

```glsl
ShaderInfo {
    Name "Learn Band Math"
}

float learnBand(float ndl, float threshold) {
    return mix(0.25, 1.0, step(threshold, ndl));
}
```

把光照入口写入 `learn_band.shadingmodel`：

```glsl
ShadingModelInfo {
    Name "Learn Band"
    Imports ["Lighting", "Learn Band Math"]
    Requires [Lighting]
}

void shading(in SurfaceData s, out vec4 color) {
    ShadingContext ctx = GetShadingContext();
    vec3 N = normalize(s.normalWS);
    Light mainLight = getMainLight(ctx.positionWS, N, ctx.viewDepth);
    float ndl = max(dot(N, mainLight.direction), 0.0);
    float band = learnBand(ndl, clamp(s.shadingParam0, 0.0, 1.0));
    vec3 radiance = mainLight.color
                  * mainLight.attenuation
                  * mainLight.shadow;
    color = vec4(s.albedo * radiance * band + s.emission, s.alpha);
}
```

把面向 Material 的 Surface 写入 `learn_band_surface.frag`：

```glsl
#version 450

ShaderInfo {
    Name "Learn Band Surface"
    ShadingModel "Learn Band"
    Surface Opaque
    Queue 2000
    Properties {
        Color baseColor = [0.9, 0.3, 0.08, 1.0]
        Float threshold = 0.55 Range(0.0, 1.0)
        Texture2D mainTexture = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texel = sampleAlbedoAlpha(mainTexture);
    s.albedo = texel.rgb * getVertexColor() * material.baseColor.rgb;
    s.alpha = texel.a * material.baseColor.a;
    s.shadingParam0 = material.threshold;
}
```

这三个文件的准确依赖图如下：

```text
Material -> Standard + Learn Band Surface
Learn Band Surface --ShadingModel--> Learn Band
Learn Band --Imports--> Lighting
Learn Band --Imports--> Learn Band Math
```

按以下步骤完成端到端运行：

1. 等 Project 面板发现三个文件，再选择或重新导入 `learn_band_surface.frag`。Console 中不应出现 Shader Import、解析、链接或 Pipeline 编译错误。
2. 创建 **Material (.mat)**，在 Inspector 中把 **Vertex** 设为 `Standard`，把 **Fragment** 设为 `Learn Band Surface`。
3. 创建 **3D Object > Cube**，把 Material 赋给 **MeshRenderer > Materials > Element 0**，并确保场景中有启用的 Camera 与 Directional Light。
4. 在 `0.2` 到 `0.8` 之间调整 **Threshold**。硬边光照色阶应随之移动，纹理与 Base Color 仍由 Material 管理。

这个示例有意限定光源范围。`getMainLight()` 只计算第一盏方向光，并包含它的衰减与阴影项。其它方向光、点光、聚光灯、面光源、环境探针和间接光都不会贡献结果。当前没有能自动升级为完整光源支持的声明。若模型需要覆盖这些光源，请采用内置 `PBR` 与 `Toon` 模型中的遍历方式。

## 放置、发现与诊断 {#discovery-diagnostics_1}

加载器会递归扫描依赖 `.frag` 所在目录与内置 Shader 根目录，识别 `.vert`、`.frag`、`.glsl` 和 `.shadingmodel`，同时排除 `_templates` 目录。`Name` 是区分大小写的精确 ID。Fragment 目录树中的项目声明优先于内置回退。请避免项目内 ID 重复：递归发现过程没有专用的重名诊断，后发现的声明可能覆盖先发现的声明。

运行时重载目前只接受 `.vert` 与 `.frag` 资产。修改 `learn_band_math.glsl` 或 `learn_band.shadingmodel` 后，请保存、触碰或重新导入 `learn_band_surface.frag`；重启 Editor 也会重建发现结果。受此限制，只保存依赖文件时，画面可能继续显示旧 GPU Program，直到依赖它的根阶段发生重载。

调试像素之前，先按此故障矩阵检查：

| 复现方式 | 预期诊断与行为 |
| --- | --- |
| 把 `Learn Band Math` 改成不存在的 ID，再重新导入 `.frag` | 生成源码中出现 `shader import not found`，随后可能报告 `learnBand` 未解析；新 Program 会被拒绝。 |
| 删除 `Imports ["Lighting", ...]`，保留 `Requires [Lighting]` | 光照声明与辅助函数缺失，`Light`、`getMainLight` 等符号编译失败。 |
| 保留 Import，删除 `Requires [Lighting]` | 光照函数库已经链接，相机局部资源没有绑定或注入；相关变体编译失败。 |
| 在函数库中加入非法 GLSL，再重新导入 `.frag` | Console 报告依赖根 `.frag` 的编译失败，并继续使用上一份有效 Program。 |

Import 会递归展开，循环导入会被抑制，最大嵌套深度为 16。当前生成的 Shader 诊断不会为导入文本增加源文件 `#line` 映射，因此编译器行号可能指向展开后的源码或根文件。请先处理第一条 Import 错误，检查其中点名的函数库，修复后重新导入根 `.frag`。画面仍显示旧结果只能证明上一份有效版本继续工作；Console 清空后再故意修改 Threshold，才能证明新 Program 已经生效。

## 一个模型如何变成多套 Pass 变体 {#pass-variants_1}

一对 Vert/Frag 会编译成多套程序。引擎枚举九种材质 Pass 目标：`Forward`、`ForwardPlus`、`GBuffer`、`Shadow`、`Depth`、`Picking`、`Motion`、`Normal` 与 `BaseColor`（`ShaderTypes.h`）。每个目标都由 `InxShaderLoader` 重新生成 GLSL，并配上对应的 `main()` 模板：

- Forward 与 Forward+ 调用 `surface()`，再带每相机光照资源调用 `shading()`（`surface_main.glsl`）；Forward+ 目标额外绑定分块光照网格。
- GBuffer 把 `packGBuffer()` 写进五个 Deferred 渲染目标（`surface_main_gbuffer.glsl`）。
- Shadow 与 Depth 只做 Alpha Clip，随后写深度。
- Picking 写稳定的物体身份，Motion 写速度，Normal 与 BaseColor 写几何阶段缓冲，供 Motion Blur、TAA 等效果消费。

共享的 Surface 代码完全不变，适配器模板负责补上各 Pass 特有的输出。`ShaderPassVariantPlanner` 决定某对阶段要编译哪些目标：材质不透明、ShadingModel 未声明 `Unsupported [Deferred]` 且未强制 Forward 时，GBuffer 才会启用。Alpha Clip 被所有兼容变体共享，Cutout 表面因此在颜色、阴影与拾取 Pass 中的裁剪行为一致。

## 依赖与 Deferred 限制 {#requirements_1}

`Imports ["Lighting"]` 链接声明与辅助函数，`Requires [Lighting]` 要求编译器与管线契约提供相机局部光照数据。本例同时需要两者，这两个字段都不会选择渲染路径。

有效模型包含 `void shading(in SurfaceData s, out vec4 color)` 且没有声明 `Unsupported [Deferred]` 时，编译器会赋予它 Deferred 候选资格。标准 GBuffer 保存 `albedo`、编码后的世界空间法线、`smoothness`、`metallic`、`occlusion`、`specularHighlights`、`emission`、`alpha`、`shadingParam0` 和 `shadingParam1`，同时记录物体光照层掩码与 ShadingModel ID。打包方式很具体：目标 0 保存基础色与 Alpha（RGBA16F），目标 1 保存编码法线、Alpha 通道存 `smoothness`（RGBA16F），目标 2 保存 `metallic`、`occlusion`、`specularHighlights` 与 `shadingParam0`（RGBA8），目标 3 保存自发光与 `shadingParam1`（RGBA16F），目标 4 保存光照层掩码与 ShadingModel ID（RG32_UINT）。`deferred_lighting.frag` 从深度重建世界位置，把各目标解码成新的 `SurfaceData` 与 `ShadingContext`，再调用 `inxDispatchShading(modelId, color)`；编译器生成的注册表会把每个具备 Deferred 资格的模型的 `shading()` 追加到它的稳定 ID 下。

`Learn Band` 满足候选条件，因为自定义阈值可以装入 `shadingParam0`，其余输入来自已保存的 Surface 字段或重建上下文。请显式测试这条边界：

1. 不挂 RenderStack，或者在 RenderStack 中选择 **Default Forward**，确认色阶会响应 Threshold 与第一盏 Directional Light。
2. 在 RenderStack Inspector 中选择 **Default Deferred**。这个不透明 Material 具备 Deferred 兼容性，应写入 GBuffer，再按 ShadingModel ID 分派。
3. 给 `Learn Band` 加上 `Unsupported [Deferred]`，重新导入 `learn_band_surface.frag`，然后再次检查。内置 Deferred 管线会省略这一组合的 GBuffer 变体，并通过使用 Forward+ 光照的 `DeferredForwardFallbackPass` 绘制该不透明物体。内部实现上，GBuffer Pass 按 Deferred 兼容性过滤材质（`deferred_compatible`），回退 Pass 过滤其余材质（`deferred_unsupported`）。透明表面也会进入透明 Forward+ Pass。
4. 声明式 DSL 还有一条规则：MSAA 大于 1 时，Deferred 路由必须声明显式的 `Forward` 或 `Forward+` 回退，编译器会把该回退应用到整条路由（`pipeline_compiler.py` 会拒绝没有回退的 Deferred 路由）。
5. 只有模型确实能从上述字段重建时，才移除该声明。编译器会检查语法与声明，无法推断依赖切线框架或额外数据的结果在语义上有误。这类模型可能编译成功，却产生错误的 Deferred 像素。

有效的 `Unsupported [Deferred]` 声明不会产生 Warning，Fallback 属于内置管线的预期路径。当前 Material Inspector 也没有逐材质的“Deferred Fallback”状态。请用上述受控切换、保持清空的 Console 与活动 RenderStack 管线作为可复现证据。编译错误属于可行动诊断；仍可通过编译的语义不匹配由 ShadingModel 作者负责判断。

Deferred 会用已保存的着色法线填充 `geometricNormalWS`，生成替代 Tangent，并固定报告 `frontFacing = true`。原始切线框架、独立几何法线、正反面信息、几何 Pass 的导数行为，以及两个 Shading Scalar 之外的模型数据都不会保留。依赖这些输入的模型应声明限制：

```glsl
ShadingModelInfo {
    Name "SixWaySmoke"
    Imports ["Lighting"]
    Requires [Lighting]
    Unsupported [Deferred]
}
```

自定义管线需要为不支持的模型提供兼容回退，也可以拒绝该路径。选中的模型缺失、格式错误或 GLSL 编译失败时，链接后的新产物不会发布；若已有上一份有效 Program，渲染器会继续使用它，并在 Console 报告失败。

<div class="learn-note"><strong>当前 API 边界。</strong><p>`ShadingModelInfo` 没有 Capabilities 列表，编译器也不会证明 Deferred 语义兼容性。`Unsupported [Deferred]` 是作者退出标准 GBuffer 路径的显式声明。</p></div>

## 保持边界清晰 {#design-boundary_1}

| 归属 | 职责 |
| --- | --- |
| `.vert` | 顶点位置与自定义 Varying |
| `.frag` | Material 属性、纹理采样与 `SurfaceData` 组装 |
| `.shadingmodel` | 通过 `shading()` 定义 Surface 与光照的交互 |
| `.glsl` | 不携带 Pass 路由策略的可复用函数和常量 |
| 管线适配器 | 生成 Forward/Forward+ 调用、GBuffer 打包、Deferred 重建与分派、依赖满足和回退 |
| RenderPipeline / RenderEffect | Queue 域、Pass 顺序、图资源与整图效果 |

这套边界让光照代码保持易读，也让路由决策留在真正拥有 RenderGraph 的代码里。下一章将从逐物体着色转向整图效果。

**证据说明。** 上述文件发现与重载行为取自当前 `InxShaderLoader` 和运行时重载路径。GBuffer 字段与回退行为取自当前 Deferred 模板、Deferred Lighting 阶段、Material Pass 规划器与内置 `default_deferred_pipeline.py`。页首 WebP 缺少源配置与版本记录，因此只保留为视觉风格参考。
