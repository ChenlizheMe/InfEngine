<!-- language:en -->

<span class="mini-tag">Custom Rendering · Chapter 3</span>

# Fragment stages, surfaces, and materials

A material fragment stage assembles textures, Material values, and interpolated geometry into `SurfaceData`. It does not choose a render path or loop over lights. The selected ShadingModel defines how that surface reacts to light; the pipeline adapter decides whether to shade it immediately or store it in the GBuffer.

This split lets one `.frag` serve Forward, Forward+, and any Deferred or custom route whose adapter and ShadingModel can represent the same surface contract.

<div class="learn-article-toc"><strong>In this chapter</strong><a href="#first-surface">A first surface</a><a href="#material-workflow">Shader to visible mesh</a><a href="#ownership">Who owns what</a><a href="#properties">Material properties</a><a href="#render-state">Render state and Queue</a><a href="#surface-contract">The SurfaceData contract</a></div>

<figure class="learn-figure">
  <img src="../assets/learn/real-gold-mountain.webp" alt="gold-coin material style reference" loading="lazy" decoding="async">
  <figcaption>Material-style reference. This workspace contains the WebP, but no matching scene, Material values, pipeline configuration, capture window, or source revision. Use it as a visual target, not as evidence that the settings below produced this frame.</figcaption>
</figure>

## A first surface {#first-surface}

```glsl
#version 450

ShaderInfo {
    Name "Painted Unlit"
    ShadingModel Unlit
    Queue 2000
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D mainTexture = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texel = sampleAlbedoAlpha(mainTexture);
    s.albedo = texel.rgb * getVertexColor() * material.baseColor.rgb;
    s.alpha = texel.a * material.baseColor.a;
}
```

`ShaderInfo` declares the material contract. `surface()` fills that contract for one fragment. `InitSurfaceData()` supplies the defaults; a simple unlit material only writes the fields it owns.

The Material stores parameter values. Compiled shader state stays with the rendering system. Switching a compatible fragment shader changes the property schema and GPU program while preserving the engine's resource lifetime rules. A failed compile keeps the previous valid pipeline active and reports the source failure once.

## From shader to a visible mesh {#material-workflow}

This editor workflow closes the loop with the shader above:

1. In the Project panel, create `Assets/Shaders/Learn`, open its context menu, and choose **Create > Fragment Shader (.frag)**. Name the file `PaintedUnlit.frag`, replace its generated text with the complete `Painted Unlit` source above, and save it. `ShaderInfo Name` is the case-sensitive ID; the filename is only the asset path.
2. Check the Console. A successful import adds no error. A parse, link, or SPIR-V failure appears there after the dependent `.frag` is reimported. Keep the Console entry: the runtime continues drawing the last-known-good linked program when a reload fails.
3. In the same Project folder, choose **Create > Material (.mat)** and name it `PaintedCube.mat`. Select it. In the Material Inspector, assign the built-in `Standard` vertex asset to **Vertex** and `PaintedUnlit.frag` to **Fragment**. The Fragment field resolves the asset to the `Painted Unlit` ID and synchronizes the Material property schema.
4. In the Hierarchy, choose **Create > 3D Object > Cube**. Select the Cube and assign `PaintedCube.mat` to **Materials > Element 0** on its `MeshRenderer`. Set `baseColor` to a saturated color. The default `white` texture makes that color visible without another asset.
5. Save the scene, move the camera or Cube, and confirm that the object remains visible in Scene and Game views. Reopen `PaintedCube.mat`: Vertex must still read `Standard`, Fragment must still identify `Painted Unlit`, and Queue must be `2000`. These three observations distinguish a saved mesh binding from a Material preview alone.

If the Cube uses the fallback/error appearance, first confirm that both shader fields are populated and that `ShaderInfo Name` still matches the imported ID. If changing `Name` during hot reload reports that an asset reimport is required, restore the old ID or reimport and reassign the Fragment asset. A Material property that does not appear usually means the fragment import failed or the assigned Material still points at another fragment ID.

## Who owns what: stages, properties, and bindings {#ownership}

A Material is a small document plus two stage references. The **Vertex** and **Fragment** selectors store `ShaderInfo Name` values, and the fragment's `ShaderInfo` block carries the `ShadingModel` entry that picks the lighting model. When the fragment is imported, the engine links the pair, generates the property schema, and compiles the program variants for each material pass; the Material then owns only the values.

Properties are declared in the fragment's `ShaderInfo` block and become typed Material fields serialized into the `.mat` document. At draw time the engine packs the numeric fields into the material uniform block (`material`, set 0, binding 14) and binds each texture property from binding 2 upward, with `white` and `normal` as built-in defaults. The fragment reads them through the `material.*` members and the `sample*` helpers. A user shader never declares descriptor sets, buffer bindings, or push constants for ordinary material data; the compiler and the engine binding layer own that layout.

ShaderInfo entries affect different things:

| Entry | What it does |
| --- | --- |
| `Name` | The stable, case-sensitive selector ID |
| `ShadingModel` | Which `.shadingmodel` provides `shading()` for the surface |
| `Properties` | Typed Material fields and Inspector controls |
| `Surface` | A defaults bundle (opaque or transparent) for fields left unspecified |
| `Queue` | Which pipeline route consumes the material |
| `Cull`, `DepthWrite`, `DepthTest`, `Blend`, `AlphaClip`, `Stencil` | Render state applied when the pipeline is built |
| `CastShadows`, `ReceiveShadows` | Participation in shadow paths |
| `PassTag` | A shader-authored tag that passes can filter on with `pass_tag` |
| `Capabilities` | Domain and ABI traits such as `Fullscreen` or `BindlessTextures` |
| `Imports`, `Requires` | Linked GLSL libraries and the engine resources they need |

## Material properties {#properties}

Properties become typed Material fields and Inspector controls. This is the property block from the built-in `lit.frag`:

```glsl
Properties {
    Color baseColor = [1.0, 1.0, 1.0, 1.0]
    Float metallic = 0.0
    Float smoothness = 0.5
    Float ambientOcclusion = 1.0
    Color emissionColor = [0.0, 0.0, 0.0, 0.0] HDR
    Float normalScale = 1.0
    Float specularHighlights = 1.0
    Texture2D texSampler = white
    Texture2D metallicMap = white
    Texture2D smoothnessMap = white
    Texture2D aoMap = white
    Texture2D normalMap = normal
}
```

`Range(min, max)` is optional UI metadata for a bounded float; the built-in Lit declaration leaves these floats unannotated. `HDR` allows a color above display white. `Internal` keeps an engine-managed property out of the ordinary Material UI.

Texture defaults such as `white` and `normal` keep the material valid before users assign project assets. The renderer manages texture bindings; users do not declare descriptor sets.

Color space comes from the texture asset import settings and the property type. Select an image in the Project panel to edit **Import Settings**. Use **Texture Type: Default** with **sRGB** enabled for albedo and ordinary color. Use **Texture Type: Data** for metallic, smoothness, AO, height, masks, and packed channels. Use **Texture Type: Normal Map** for tangent-space normals. Selecting Data or Normal Map forces sRGB off and disables the checkbox. With **Compression: Auto**, Normal Map resolves to BC5 and Data resolves to no block compression.

`sampleAlbedoAlpha()` and `sampleGrayscale()` only sample the bound texture view. The imported sRGB GPU format decodes RGB to linear values and leaves alpha unchanged. A `Color` Material property is authored in sRGB and packed as linear RGB. `SurfaceData.albedo` and `SurfaceData.emission` therefore receive linear values.

The current Normal Map importer has a narrow boundary: it changes the semantic, forces linear sampling, and may encode RG as BC5. It does not provide green-channel inversion, channel swizzling, or a DirectX/OpenGL convention selector. `sampleNormal()` reads RG as signed tangent-space XY and reconstructs positive Z. If bumps look recessed, fix the source texture or perform the required channel conversion explicitly in shader code; there is no import checkbox for that conversion today. The mesh also needs valid tangents and `v_Tangent.w`, because the helper constructs a world-space TBN basis.

A reproducible import check uses three simple swatches. Assign a mid-gray color texture to albedo and toggle sRGB: the rendered RGB must change while alpha stays fixed. Assign a mid-gray mask as Data and read it with `sampleGrayscale()`; an encoded value near `0.5` should remain near `0.5`, while an accidental sRGB import decodes it to roughly `0.214`. Assign a flat tangent normal `(0.5, 0.5, 1.0)` as Normal Map; lighting should match the geometric normal. These comparisons diagnose import semantics independently of metallic or smoothness values.

Material values can be edited live. The in-memory object updates immediately, previews observe that state, and persistence is scheduled separately so disk I/O does not become the interaction model.

## Render state and Material Queue {#render-state}

Render state belongs beside the fragment contract:

```glsl
ShaderInfo {
    Name "Transparent Unlit"
    ShadingModel Unlit
    Surface Transparent
    Queue 3000
    Cull Back
    DepthWrite Off
    DepthTest Less_Equal
    Blend Alpha
    CastShadows Off
    ReceiveShadows Off
}
```

These fields affect different problems:

| Field | Responsibility |
| --- | --- |
| `Queue` | Which pipeline route consumes the material and its broad ordering domain |
| `Cull` | Which triangle faces are discarded |
| `DepthWrite` / `DepthTest` | How the material participates in visibility |
| `Blend` | How the shaded source color combines with the current color target |
| `AlphaClip` | The threshold used to discard low-alpha fragments |
| `Stencil` | A shader-authored `compare,reference,pass,fail,depth-fail` state when the pass has a stencil attachment |
| `CastShadows` / `ReceiveShadows` | Participation in compatible shadow paths |

Do not use Queue as a disguised effect parameter. Queue is intentionally structural: a custom pipeline can route `1..100` through one path and `101..200` through another. Material authors choose the queue; pipeline authors decide what that queue means for a project.

`Surface Transparent` supplies transparent defaults for fields left unspecified: queue `3000`, alpha blending, depth writes off, and the transparent pass tag. Explicit Queue, DepthWrite, PassTag, and non-off Blend modes override those defaults. `Off` is also the parser's initial blend value, so this Surface setting normalizes `Blend Off` to `Alpha`. `Blend Alpha` expects straight RGB and uses source alpha for color blending. `Blend Premultiplied` expects `shading()` to return RGB already multiplied by alpha. `Blend Additive` adds source RGB. Blending happens after shading and does not discard a fragment.

Alpha clipping is an earlier, binary decision. `AlphaClip 0.5` stores the threshold in the engine-managed `_AlphaClipThreshold`; `AlphaClip On` uses the same `0.5` default. After `surface()` returns, generated adapters discard when `s.alpha` is below that threshold. The check is shared by Forward, GBuffer, and compatible depth, shadow, motion, normal, base-color, and picking variants. A cutout material normally stays in the opaque queue with depth writes enabled and blending off. A translucent material normally uses the transparent queue, depth writes off, and one of the blend modes.

State has a concrete priority. `Surface Transparent` is normalized first. The resulting `ShaderInfo` metadata supplies defaults to a Material. Inspector edits set per-field override bits for surface type, culling, depth, blend, Queue, and alpha clip, so those values survive shader reloads. Pass construction has the final route-specific word: variants outside Forward and Forward+ disable blending and derive depth write/test from the pass attachment; a read-only depth pass disables depth writes. A pass whose depth format has no stencil component disables stencil testing.

`Stencil` currently has no Material Inspector control or per-material override bit. Its value comes from `ShaderInfo`, for example `Stencil "less_equal,1,replace,keep,keep"`; it applies the same compare/reference/operations to front and back faces with `0xFF` masks. Invalid or underspecified stencil strings are not backed by a dedicated authoring diagnostic, so keep this field in reviewed shader source and verify it in a pipeline with a stencil-capable depth target.

### Transparent sorting: a reproducible diagnosis

The built-in transparent route depth-tests against opaque depth without writing it and requests back-to-front sorting. Sorting uses each draw object's transform origin in camera view space. It does not sort individual triangles or use mesh bounds.

1. Place two transparent Cubes so their projected silhouettes overlap. Give both `Surface Transparent`, Queue `3000`, alpha blending, depth writes off, and `DepthTest Less_Equal`.
2. Move the camera through the line joining their origins. The farther origin should draw first and the overlap should change consistently when the origins exchange depth.
3. If one object always hides the other, inspect the live Material fields for Blend, Depth Write, Depth Test, and Queue. Queue must also fall inside a transparent route owned by the active RenderStack.
4. If the result fails only when one mesh intersects itself, spans a large depth range, or intersects the other mesh, object-origin sorting has reached its limit. Split the geometry into smaller renderers, redesign the overlap, use a cutout where binary coverage is acceptable, or provide a project-specific transparency technique.

Changing `s.alpha` cannot repair a route or depth-state mismatch. Changing Queue only helps when the active pipeline maps the new value to a suitable route or a separately ordered queue interval.

## The `SurfaceData` contract {#surface-contract}

`SurfaceData` is not declared inside each `.frag`. The compiler imports its canonical definition from the built-in `surface.glsl` library before it compiles the material stage:

```glsl
struct SurfaceData {
    vec3 albedo;
    vec3 normalWS;
    float metallic;
    float smoothness;
    float occlusion;
    vec3 emission;
    float alpha;
    float specularHighlights;
    float shadingParam0;
    float shadingParam1;
};
```

This is the complete current shape of the structure. It describes the material state at one rasterized surface point, not the shape of the object. Mesh position, UV, vertex color, normal, and tangent originate in the vertex input and interpolants. Per-fragment geometry and camera values are prepared separately as `ShadingContext`; the ShadingModel reads them with `GetShadingContext()`. Adding a field to a project `.frag` does not extend this engine contract.

`InitSurfaceData()` returns these exact built-in defaults:

| Field | GLSL type | Default | Meaning |
| --- | --- | --- | --- |
| `albedo` | `vec3` | `vec3(1.0)` | Linear RGB base color |
| `normalWS` | `vec3` | `vec3(0.0)` | World-space shading normal, or the sentinel that selects the geometric normal |
| `metallic` | `float` | `0.0` | Metallic factor in `[0, 1]`; zero is dielectric |
| `smoothness` | `float` | `0.5` | Smoothness in `[0, 1]`, equal to `1 - perceptualRoughness` |
| `occlusion` | `float` | `1.0` | Ambient occlusion in `[0, 1]`; one is fully unoccluded |
| `emission` | `vec3` | `vec3(0.0)` | Linear RGB emission with intensity already applied |
| `alpha` | `float` | `1.0` | Opacity or coverage in `[0, 1]` |
| `specularHighlights` | `float` | `1.0` | Specular-highlight multiplier in `[0, 1]` |
| `shadingParam0`, `shadingParam1` | `float` | `0.0` | Two ShadingModel-defined scalars preserved by the canonical GBuffer |

The zero `normalWS` has a specific job. Leave it untouched when the material has no authored normal. The generated pipeline adapter calls `ResolveSurfaceNormal()` after `surface()`, replacing the sentinel with the interpolated geometric normal and normalizing authored world-space normals. Normalizing the zero value inside `surface()` would destroy that signal.

Surface code uses the following spaces: `v_WorldPos`, `v_Normal`, and `v_Tangent` are world-space values; `v_Tangent.w` carries the bitangent sign; `v_TexCoord` is the primary mesh UV; and `v_ViewDepth` is linear eye-space depth. `sampleNormal()` decodes a tangent-space normal map through the world-space TBN basis and returns a world-space normal. Assign world-space data to `s.normalWS`.

The Lit fragment stage in Infernux follows the same shape:

```glsl
void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texColor = sampleAlbedoAlpha(texSampler);
    s.albedo = texColor.rgb * getVertexColor() * material.baseColor.rgb;
    s.metallic = sampleGrayscale(metallicMap) * material.metallic;
    s.smoothness = sampleGrayscale(smoothnessMap) * material.smoothness;
    s.occlusion = sampleGrayscale(aoMap) * material.ambientOcclusion;
    s.normalWS = sampleNormal(normalMap, material.normalScale);
    s.emission = material.emissionColor.rgb * material.emissionColor.a;
    s.alpha = texColor.a * material.baseColor.a;
    s.specularHighlights = material.specularHighlights;
}
```

This function only assembles the surface. The ShadingModel owns the surface-light interaction. Generated pipeline adapters own Forward and Forward+ invocation, GBuffer packing, Deferred dispatch, and alpha clipping.

<div class="learn-warning"><strong>Alpha alone leaves render state unchanged.</strong><p>`s.alpha` supplies coverage to clipping or blending. Queue selects the route, depth state controls visibility, `AlphaClip` can discard, and `Blend` controls composition. Check all four when a surface disappears or stays opaque.</p></div>

**Evidence note.** The workflow and state priority above follow the current Project and Hierarchy context menus, Material and MeshRenderer Inspectors, shader reload path, material metadata application, pass construction, and transparent draw sorting. Texture guidance follows the current Texture Inspector, importer defaults, GPU format selection, and `surface_utils.glsl` (the mesh surface helpers that define `sampleNormal`). The opening WebP remains a style reference because its source configuration and revision are absent from this workspace.

<!-- language:zh -->

<span class="mini-tag">自定义渲染 · 第 3 章</span>

# 片元阶段、Surface 与材质

材质的片元阶段把贴图、Material 参数和插值后的几何数据组装成 `SurfaceData`。它不选择渲染路径，也不遍历光源。ShadingModel 定义表面怎样响应光照，管线适配器决定立即着色，还是先写入 GBuffer。

按照这套分工，同一份 `.frag` 可以服务于 Forward、Forward+，以及能够表示同一份 Surface 契约的 Deferred 或自定义路径。

<div class="learn-article-toc"><strong>本章内容</strong><a href="#first-surface_1">第一份 Surface</a><a href="#material-workflow_1">从 Shader 到可见 Mesh</a><a href="#ownership_1">谁拥有什么</a><a href="#properties_1">材质属性</a><a href="#render-state_1">渲染状态与 Queue</a><a href="#surface-contract_1">SurfaceData 契约</a></div>

<figure class="learn-figure">
  <img src="../assets/learn/real-gold-mountain.webp" alt="金币材质风格参考" loading="lazy" decoding="async">
  <figcaption>材质风格参考。工作区包含这张 WebP，但没有对应场景、Material 数值、管线配置、捕获窗口或源版本记录。它可以充当视觉目标，不能证明下文设置生成了这帧画面。</figcaption>
</figure>

## 第一份 Surface {#first-surface_1}

```glsl
#version 450

ShaderInfo {
    Name "Painted Unlit"
    ShadingModel Unlit
    Queue 2000
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D mainTexture = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texel = sampleAlbedoAlpha(mainTexture);
    s.albedo = texel.rgb * getVertexColor() * material.baseColor.rgb;
    s.alpha = texel.a * material.baseColor.a;
}
```

`ShaderInfo` 声明材质契约，`surface()` 为一个片元填写这份契约。`InitSurfaceData()` 提供默认值；简单的无光照材质只需改动自己拥有的字段。

Material 保存参数值，编译后的 Shader 状态由渲染系统管理。切换兼容的 Frag 会改变属性 Schema 与 GPU 程序，同时继续遵循引擎的资源生命周期。编译失败时，上一份有效 Pipeline 会保留，源文件错误只清晰报告一次。

## 从 Shader 到可见 Mesh {#material-workflow_1}

下面用当前 Editor 控件完成完整闭环：

1. 在 Project 面板创建 `Assets/Shaders/Learn`，打开目录菜单并选择 **创建 > 片段着色器 (.frag)**。把文件命名为 `PaintedUnlit.frag`，用上方完整的 `Painted Unlit` 源码替换模板并保存。`ShaderInfo Name` 是区分大小写的 ID，文件名只表示资产路径。
2. 检查 Console。成功导入不会新增错误。依赖 `.frag` 重新导入后，解析、链接或 SPIR-V 失败会显示在这里。保留这条 Console 记录：重载失败时，运行时会继续绘制上一份有效的链接程序。
3. 在同一 Project 目录选择 **创建 > 材质 (.mat)**，命名为 `PaintedCube.mat`。选中它，在 Material Inspector 的 **Vertex** 字段放入内置 `Standard` 顶点资产，在 **Fragment** 字段放入 `PaintedUnlit.frag`。Fragment 字段会把资产解析成 `Painted Unlit` ID，并同步 Material 属性 Schema。
4. 在 Hierarchy 选择 **创建 > 3D Object > Cube**。选中 Cube，把 `PaintedCube.mat` 分配给 `MeshRenderer` 的 **Materials > Element 0**，再把 `baseColor` 设置为饱和颜色。默认 `white` 贴图足以显示这个颜色。
5. 保存场景，移动相机或 Cube，确认对象在 Scene 与 Game View 都保持可见。重新打开 `PaintedCube.mat`：Vertex 应保持 `Standard`，Fragment 应保持 `Painted Unlit`，Queue 应为 `2000`。这三项观察可以确认场景保存了 Mesh 绑定，验证范围超过单独的 Material 预览。

Cube 显示回退或错误外观时，先确认两个 Shader 字段都有值，并检查 `ShaderInfo Name` 是否仍与导入 ID 一致。热重载期间修改 `Name` 会提示必须重新导入资产；可以恢复旧 ID，也可以重新导入后再次分配 Fragment。Material 属性没有出现时，常见原因是 Frag 导入失败，或当前 Material 仍指向另一个 Fragment ID。

## 谁拥有什么：阶段、属性与绑定 {#ownership_1}

Material 是一份小文档加上两个阶段引用。**Vertex** 与 **Fragment** 选择器保存 `ShaderInfo Name` 值，片元 `ShaderInfo` 块里的 `ShadingModel` 条目选择光照模型。片元导入时，引擎链接这对阶段、生成属性 Schema，并为每种材质 Pass 编译程序变体；Material 此后只拥有参数值。

属性在片元的 `ShaderInfo` 块里声明，会变成有类型的 Material 字段，序列化进 `.mat` 文档。绘制时引擎把数值字段打包进材质 Uniform Block（`material`，set 0、binding 14），并从 binding 2 起绑定每个纹理属性，`white` 与 `normal` 是内置默认值。片元通过 `material.*` 成员与 `sample*` 辅助函数读取它们。用户 Shader 不用为普通材质数据声明描述符集、缓冲绑定或 Push Constant；这份布局由编译器与引擎绑定层持有。

ShaderInfo 各条目影响不同环节：

| 条目 | 作用 |
| --- | --- |
| `Name` | 稳定且区分大小写的选择器 ID |
| `ShadingModel` | 由哪个 `.shadingmodel` 提供表面的 `shading()` |
| `Properties` | 有类型的 Material 字段与 Inspector 控件 |
| `Surface` | 为未指定字段提供的 Opaque 或 Transparent 默认值包 |
| `Queue` | 哪条管线路由消费这个材质 |
| `Cull`、`DepthWrite`、`DepthTest`、`Blend`、`AlphaClip`、`Stencil` | 构建管线时应用的渲染状态 |
| `CastShadows`、`ReceiveShadows` | 是否参与阴影路径 |
| `PassTag` | Shader 声明的标签，Pass 可以用 `pass_tag` 过滤 |
| `Capabilities` | 域与 ABI 特征，例如 `Fullscreen` 或 `BindlessTextures` |
| `Imports`、`Requires` | 链接的 GLSL 函数库与其需要的引擎资源 |

## 材质属性 {#properties_1}

Properties 会成为有类型的 Material 字段和 Inspector 控件。下面就是内置 `lit.frag` 的属性块：

```glsl
Properties {
    Color baseColor = [1.0, 1.0, 1.0, 1.0]
    Float metallic = 0.0
    Float smoothness = 0.5
    Float ambientOcclusion = 1.0
    Color emissionColor = [0.0, 0.0, 0.0, 0.0] HDR
    Float normalScale = 1.0
    Float specularHighlights = 1.0
    Texture2D texSampler = white
    Texture2D metallicMap = white
    Texture2D smoothnessMap = white
    Texture2D aoMap = white
    Texture2D normalMap = normal
}
```

`Range(min, max)` 是可选的浮点数 UI 约束；内置 Lit 的这些 Float 没有添加该标记。`HDR` 允许颜色超过显示白，`Internal` 会隐藏由引擎管理的属性。

`white`、`normal` 等贴图默认值，让材质在用户尚未分配项目资源时仍然有效。纹理由渲染器绑定，用户不需要声明描述符 Set。

颜色空间由贴图资产导入设置和属性类型决定。在 Project 面板选中图片即可编辑 **导入设置**。Albedo 与普通颜色使用 **贴图类型：默认**，并启用 **sRGB**。金属度、光滑度、AO、高度、Mask 和打包通道使用 **贴图类型：数据**。切线空间法线使用 **贴图类型：法线贴图**。选择数据或法线贴图会强制关闭 sRGB 并禁用该复选框。采用 **压缩：自动** 时，法线贴图会解析为 BC5，数据贴图会解析为无块压缩。

`sampleAlbedoAlpha()` 与 `sampleGrayscale()` 只采样已绑定的 Texture View。导入后的 sRGB GPU 格式把 RGB 解码到线性值，Alpha 保持原值。`Color` Material 属性按 sRGB 输入，再以线性 RGB 写入 Shader。`SurfaceData.albedo` 和 `SurfaceData.emission` 因此接收线性值。

当前法线贴图导入器的边界很窄：它设置语义、强制线性采样，并可把 RG 编码成 BC5。导入器没有绿色通道翻转、通道重排或 DirectX/OpenGL 约定选择器。`sampleNormal()` 把 RG 读取为带符号的切线空间 XY，并重建正 Z。凹凸方向相反时，需要修改源贴图，或在 Shader 中显式完成所需通道转换；目前没有对应的导入复选框。Mesh 还需要有效 Tangent 和 `v_Tangent.w`，辅助函数会用它们构建世界空间 TBN 基底。

一组可复现的导入检查只需三个简单色块。把中灰颜色贴图分配给 Albedo 并切换 sRGB：渲染 RGB 应发生变化，Alpha 应保持固定。把中灰 Mask 设为数据贴图并通过 `sampleGrayscale()` 读取；编码值约为 `0.5` 时，结果也应接近 `0.5`，误用 sRGB 导入会把它解码到约 `0.214`。把 `(0.5, 0.5, 1.0)` 的平坦切线法线设为法线贴图；光照应与几何法线一致。这些对照能把导入语义问题与金属度、光滑度参数分开诊断。

Material 参数可以实时编辑：内存对象立即改变，预览直接观察这份状态，落盘则被单独调度，文件 I/O 不会成为交互模型本身。

## 渲染状态与 Material Queue {#render-state_1}

渲染状态和片元契约放在一起：

```glsl
ShaderInfo {
    Name "Transparent Unlit"
    ShadingModel Unlit
    Surface Transparent
    Queue 3000
    Cull Back
    DepthWrite Off
    DepthTest Less_Equal
    Blend Alpha
    CastShadows Off
    ReceiveShadows Off
}
```

这些字段分别解决不同问题：

| 字段 | 职责 |
| --- | --- |
| `Queue` | 材质由哪条管线路由消费，以及所处的大致排序域 |
| `Cull` | 丢弃哪一侧三角面 |
| `DepthWrite` / `DepthTest` | 材质如何参与可见性判断 |
| `Blend` | 着色结果怎样与当前颜色目标合成 |
| `AlphaClip` | 丢弃低 Alpha 片元时使用的阈值 |
| `Stencil` | Pass 带 Stencil Attachment 时采用的 Shader 级 `compare,reference,pass,fail,depth-fail` 状态 |
| `CastShadows` / `ReceiveShadows` | 是否参与兼容的阴影路径 |

不要把 Queue 当成伪装的效果参数。Queue 是有意设计的结构信息：自定义管线可以让 `1..100` 走一条路径、`101..200` 走另一条。材质作者选择 Queue，管线作者决定这些 Queue 在项目里的含义。

`Surface Transparent` 会为尚未显式填写的字段提供透明表面默认值：Queue `3000`、Alpha 混合、关闭深度写入，并使用 transparent Pass Tag。显式 Queue、DepthWrite、PassTag 及非 Off 的 Blend 模式可以覆盖对应默认值。`Off` 同时是解析器的初始 Blend 值，因此该 Surface 设置会把 `Blend Off` 归一为 `Alpha`。`Blend Alpha` 接收未预乘的 RGB，并用源 Alpha 混合颜色。`Blend Premultiplied` 要求 `shading()` 返回已经乘过 Alpha 的 RGB，`Blend Additive` 累加源 RGB。混合发生在着色之后，不会丢弃片元。

Alpha Clip 是更早执行的二值判断。`AlphaClip 0.5` 会把阈值写入引擎管理的 `_AlphaClipThreshold`；`AlphaClip On` 同样使用默认值 `0.5`。`surface()` 返回后，生成的适配代码会丢弃 `s.alpha` 低于阈值的片元。Forward、GBuffer 以及兼容的 Depth、Shadow、Motion、Normal、Base Color、Picking 变体共用这项检查。Cutout 材质通常留在不透明 Queue，开启深度写入并关闭混合；半透明材质通常进入透明 Queue，关闭深度写入并选择一种 Blend 模式。

状态优先级是具体的。系统先归一化 `Surface Transparent`，得到的 `ShaderInfo` 元数据为 Material 提供默认值。Inspector 对 Surface Type、Cull、Depth、Blend、Queue 和 Alpha Clip 的编辑会设置逐字段 Override Bit，因此这些值在 Shader 重载后仍会保留。Pass 构建拥有最终的路径级决定权：Forward 与 Forward+ 以外的变体会关闭混合，并根据 Pass Attachment 设置深度写入与测试；只读深度 Pass 会关闭深度写入。深度格式不含 Stencil 分量时，Pass 会关闭 Stencil Test。

`Stencil` 目前没有 Material Inspector 控件，也没有逐 Material Override Bit。它来自 `ShaderInfo`，例如 `Stencil "less_equal,1,replace,keep,keep"`；正反面共用 Compare、Reference 和 Operation，Mask 固定为 `0xFF`。无效或字段不足的 Stencil 字符串缺少专用创作诊断，因此应把它留在经过审查的 Shader 源码中，并在使用 Stencil Depth Target 的管线里验收。

### 透明排序：可复现排查

内置透明路径会读取不透明深度并进行 Depth Test，同时保持该深度不变，并请求从后向前排序。排序键来自每个 Draw Object 的 Transform Origin 在相机 View Space 中的位置。系统不会对单个三角形排序，也不会用 Mesh Bounds 计算排序键。

1. 放置两个透明 Cube，让它们在屏幕上的轮廓重叠。两者都使用 `Surface Transparent`、Queue `3000`、Alpha Blend、关闭 Depth Write，并设置 `DepthTest Less_Equal`。
2. 让相机沿两个 Origin 的连线移动。较远 Origin 应先绘制；两个 Origin 的深度次序交换后，重叠结果应稳定地随之变化。
3. 某个对象始终遮住另一个对象时，检查 Material 当前值中的 Blend、Depth Write、Depth Test 和 Queue。Queue 还必须落在活动 RenderStack 拥有的透明路径范围内。
4. 问题只在 Mesh 自交、单个 Mesh 跨越很大深度范围，或两个 Mesh 相交时出现，说明基于 Object Origin 的排序已达到能力边界。可以把几何拆成更小的 Renderer、调整重叠设计、在适合二值覆盖时改用 Cutout，或实现项目专用透明方案。

修改 `s.alpha` 无法修复路径或深度状态不匹配。只有活动管线把新 Queue 映射到合适路径或单独排序区间时，修改 Queue 才能改变顺序。

## `SurfaceData` 契约 {#surface-contract_1}

每个 `.frag` 都不需要自行声明 `SurfaceData`。编译材质阶段前，编译器会从内置的 `surface.glsl` 函数库导入这份标准定义：

```glsl
struct SurfaceData {
    vec3 albedo;
    vec3 normalWS;
    float metallic;
    float smoothness;
    float occlusion;
    vec3 emission;
    float alpha;
    float specularHighlights;
    float shadingParam0;
    float shadingParam1;
};
```

这就是当前 `SurfaceData` 的完整结构。它描述的是一个光栅化表面点上的材质状态，不负责描述物体的几何形状。Mesh 的位置、UV、顶点色、法线与切线来自顶点输入和插值变量。每个片元的几何信息与相机信息会被单独整理成 `ShadingContext`，ShadingModel 通过 `GetShadingContext()` 读取。只在项目 `.frag` 中增加字段，不会扩展引擎的这份标准契约。

`InitSurfaceData()` 返回以下内置默认值：

| 字段 | GLSL 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `albedo` | `vec3` | `vec3(1.0)` | 线性 RGB 基础色 |
| `normalWS` | `vec3` | `vec3(0.0)` | 世界空间着色法线；零向量是改用几何法线的哨兵值 |
| `metallic` | `float` | `0.0` | `[0, 1]` 范围的金属度；零表示非金属 |
| `smoothness` | `float` | `0.5` | `[0, 1]` 范围的光滑度，等于 `1 - perceptualRoughness` |
| `occlusion` | `float` | `1.0` | `[0, 1]` 范围的环境遮蔽；一表示完全无遮挡 |
| `emission` | `vec3` | `vec3(0.0)` | 已经乘入强度的线性 RGB 自发光 |
| `alpha` | `float` | `1.0` | `[0, 1]` 范围的不透明度或覆盖率 |
| `specularHighlights` | `float` | `1.0` | `[0, 1]` 范围的镜面高光乘数 |
| `shadingParam0`、`shadingParam1` | `float` | `0.0` | 由 ShadingModel 定义、可被标准 GBuffer 保存的两个标量 |

零向量 `normalWS` 有明确用途。材质没有编写法线时，应保留这个值。管线适配器会在 `surface()` 之后调用 `ResolveSurfaceNormal()`，把哨兵替换为插值后的几何法线，并归一化用户写入的世界空间法线。不要在 `surface()` 内归一化这个零向量，否则哨兵信息会丢失。

Surface 代码使用的空间如下：`v_WorldPos`、`v_Normal`、`v_Tangent` 都在世界空间，`v_Tangent.w` 保存副切线方向符号；`v_TexCoord` 是主 UV；`v_ViewDepth` 是线性眼空间深度。`sampleNormal()` 使用世界空间 TBN 基底解码切线空间法线贴图，返回世界空间法线。写入 `s.normalWS` 的自定义法线也必须处于世界空间。

Infernux 的 Lit Frag 也遵循同样的形状：

```glsl
void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texColor = sampleAlbedoAlpha(texSampler);
    s.albedo = texColor.rgb * getVertexColor() * material.baseColor.rgb;
    s.metallic = sampleGrayscale(metallicMap) * material.metallic;
    s.smoothness = sampleGrayscale(smoothnessMap) * material.smoothness;
    s.occlusion = sampleGrayscale(aoMap) * material.ambientOcclusion;
    s.normalWS = sampleNormal(normalMap, material.normalScale);
    s.emission = material.emissionColor.rgb * material.emissionColor.a;
    s.alpha = texColor.a * material.baseColor.a;
    s.specularHighlights = material.specularHighlights;
}
```

这个函数只组装 Surface。ShadingModel 负责定义表面与光照的交互；生成的管线适配器负责 Forward 和 Forward+ 调用、GBuffer 打包、Deferred 分派与 Alpha Clip。

<div class="learn-warning"><strong>单独填写 Alpha 不会改变渲染状态。</strong><p>`s.alpha` 为裁剪或混合提供覆盖率。Queue 选择路径，深度状态控制可见性，`AlphaClip` 可以丢弃片元，`Blend` 决定合成方式。表面消失或始终不透明时，应同时检查这四项。</p></div>

**证据说明。** 上述工作流与状态优先级取自当前 Project 和 Hierarchy 上下文菜单、Material 与 MeshRenderer Inspector、Shader 重载路径、Material 元数据应用、Pass 构建和透明 Draw 排序。贴图说明取自当前 Texture Inspector、导入默认值、GPU 格式选择与 `surface_utils.glsl`（定义 `sampleNormal` 的 Mesh 表面辅助函数）。页首 WebP 缺少源配置与版本记录，因此只保留为风格参考。
