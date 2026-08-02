# Writing shaders in Infernux

Infernux keeps shader code close to GLSL while generating the repetitive interface declarations for you. A shader file starts with a structured `ShaderInfo` block, then contains ordinary GLSL functions. You do not write descriptor bindings or vertex attribute `layout` declarations in project shaders.

This chapter follows three levels:

1. **Beginner:** author a material with a vertex stage and a surface fragment stage.
2. **Intermediate:** define a reusable shading model that decides how a surface reacts to light.
3. **Advanced:** take full control of a standalone geometry or fullscreen stage.

## Beginner: vertex and fragment shaders

### Start with the standard vertex path

The smallest mesh vertex shader is deliberately small:

```glsl
#version 450

ShaderInfo {
    Name "Standard"
}
```

With no `vertex()` function, Infernux uses the built-in object-to-world-to-view-to-clip transform. To deform the mesh, add the fixed vertex hook:

```glsl
#version 450

ShaderInfo {
    Name "Wave"
}

void vertex(inout VertexInput v) {
    float wave = sin(v.position.x * 4.0 + _Globals._Time.x * 2.0) * 0.1;
    v.position.y += wave;
}
```

`VertexInput` and `_Globals` are supplied by the engine. The hook changes object-space input before the standard transform, so the shader remains compatible with instancing, cameras, and the normal material pipeline.

### Describe a surface in the fragment stage

A regular material fragment shader writes `SurfaceData`; its `ShadingModel` decides how that data becomes a final color.

```glsl
#version 450

ShaderInfo {
    Name "Tinted Unlit"
    ShadingModel Unlit
    Queue 2000
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D mainTexture = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texel = texture(mainTexture, v_TexCoord);
    s.albedo = texel.rgb * v_Color * material.baseColor.rgb;
    s.alpha = texel.a * material.baseColor.a;
}
```

The `Properties` block creates material properties and typed access through `material`. Texture resources are sampled by their declared names. Common property types are `Float`, `Float2`, `Float3`, `Float4`, `Color`, `Int`, `Mat4`, and `Texture2D`.

Use `Range(min, max)` for an Inspector range and `HDR` for an HDR color:

```glsl
Properties {
    Float roughness = 0.5 Range(0.0, 1.0)
    Color emission = [0.0, 0.0, 0.0, 1.0] HDR
}
```

`SurfaceData` contains the material-facing values used by the built-in lighting paths: `albedo`, `normalWS`, `metallic`, `smoothness`, `occlusion`, `emission`, `alpha`, and `specularHighlights`.

### Material state belongs in ShaderInfo

The same header controls queue and fixed-function state:

```glsl
ShaderInfo {
    Name "Transparent Unlit"
    ShadingModel Unlit
    Queue 3000
    Cull Back
    DepthWrite Off
    DepthTest LessEqual
    Blend SrcAlpha OneMinusSrcAlpha
    CastShadows Off
    ReceiveShadows Off
}
```

Use the exact `Name` as the shader ID selected by a material. Shader IDs are case-sensitive. `Queue` also decides which RenderPipeline route consumes the material.

## Intermediate: reusable shading models

A surface shader answers “what is this surface?” A shading model answers “how does this surface interact with light?” Keeping those jobs separate lets many materials share PBR, unlit, toon, halftone, or a project-specific lighting model.

```glsl
ShadingModelInfo {
    Name "FlatTint"
    Entry Forward evaluateForward
}

void evaluateForward(in SurfaceData s, out vec4 color) {
    vec3 base = clamp(s.albedo, 0.0, 1.0);
    color = vec4(base + s.emission, s.alpha);
}
```

Reference it from a fragment shader with `ShadingModel FlatTint`. The fixed forward entry is `void evaluateForward(in SurfaceData s, out vec4 color)`, although the function name may be changed by the `Entry Forward` declaration.

For a custom deferred encoding, also declare a GBuffer entry:

```glsl
ShadingModelInfo {
    Name "MyLighting"
    Imports ["Lighting", "PBR"]
    Entry Forward evaluateForward
    Entry GBuffer evaluateGBuffer
}
```

If a conventional surface model omits `Entry GBuffer`, Infernux can use its standard PBR-oriented GBuffer encoding. Define the GBuffer entry only when the model needs a different representation. This keeps ordinary material code portable between Forward, Forward+, and Deferred routes.

## Advanced: fully custom stages

Use a custom stage only when `surface()` and the standard vertex hook cannot express the job. Typical examples are procedural geometry, editor utilities, fullscreen effects, and specialized domain renderers.

### Fullscreen shader

```glsl
#version 450

ShaderInfo {
    Name "Color Invert"
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
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
    outColor = vec4(vec3(1.0) - source.rgb, source.a);
}
```

### Standalone geometry shader

Use `Capabilities [Standalone]` when the stage owns an explicit geometry `main()`. Declare inter-stage values with `Inputs` and `Outputs`, plus textures in `Resources` and small per-draw data in `PushConstants`. Infernux generates the matching GLSL interfaces and bindings.

Do not add authored `layout(location=...)`, `layout(set=..., binding=...)`, uniform blocks, or push-constant layouts around those declarations. The generated interface is part of the RHI contract and keeps the same source portable to later backends.

## Choosing the right level

| Goal | Start here |
| --- | --- |
| Change mesh shape or animate vertices | Standard vertex shader with `vertex()` |
| Define texture, color, metallic, emission, or transparency | Fragment shader with `surface()` |
| Reuse a new lighting style across materials | `.shadingmodel` |
| Build a fullscreen effect | `Capabilities [Fullscreen]` |
| Own a specialized geometry stage | `Capabilities [Standalone]` |

Prefer the lowest level that expresses the effect. It preserves engine-managed instancing, shadows, material previews, render-route compatibility, and future RHI portability.

## Common errors

- **Missing fixed entry point:** a structured vertex stage needs `vertex()` when it declares custom vertex work; a surface fragment stage needs `surface()`.
- **Material disappears after changing a shader:** verify that the selected vertex and fragment shaders belong to the geometry/material domain and that their exact IDs match.
- **A transparent object is rendered as opaque:** check `Queue`, `DepthWrite`, and `Blend` together.
- **A linker reports duplicate layouts:** remove authored `layout` declarations and describe the interface in `ShaderInfo`.
- **A deferred route looks wrong:** provide an explicit GBuffer entry only if the standard surface encoding is insufficient.

---

# 在 Infernux 中编写 Shader

Infernux 保留了 GLSL 的主体写法，同时替你生成重复的接口声明。每个 Shader 文件先写结构化的 `ShaderInfo`，后面继续使用普通 GLSL 函数。项目 Shader 不需要手写描述符绑定和顶点属性的 `layout`。

这篇文档分成三个层级：

1. **入门：**编写普通顶点 Shader 和 Surface 片元 Shader。
2. **进阶：**定义可被多个材质复用的着色模型。
3. **高级：**完整接管独立几何阶段或全屏阶段。

## 入门：普通 Vert 与 Frag

### 从标准顶点路径开始

最小的网格顶点 Shader 只有这些内容：

```glsl
#version 450

ShaderInfo {
    Name "Standard"
}
```

不提供 `vertex()` 时，引擎自动完成物体空间到世界、观察和裁剪空间的变换。需要做顶点变形时，再加入固定入口：

```glsl
#version 450

ShaderInfo {
    Name "Wave"
}

void vertex(inout VertexInput v) {
    float wave = sin(v.position.x * 4.0 + _Globals._Time.x * 2.0) * 0.1;
    v.position.y += wave;
}
```

`VertexInput` 和 `_Globals` 由引擎注入。这个函数修改标准变换前的物体空间数据，因此仍然兼容实例化、相机和普通材质管线。

### 在片元阶段描述表面

普通材质的 Frag 负责填写 `SurfaceData`，最终如何结算颜色则由 `ShadingModel` 决定。

```glsl
#version 450

ShaderInfo {
    Name "Tinted Unlit"
    ShadingModel Unlit
    Queue 2000
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D mainTexture = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texel = texture(mainTexture, v_TexCoord);
    s.albedo = texel.rgb * v_Color * material.baseColor.rgb;
    s.alpha = texel.a * material.baseColor.a;
}
```

`Properties` 会生成材质属性，并通过 `material` 提供类型化访问。常用类型包括 `Float`、`Float2`、`Float3`、`Float4`、`Color`、`Int`、`Mat4` 和 `Texture2D`。

范围与 HDR 颜色可以直接写在属性后面：

```glsl
Properties {
    Float roughness = 0.5 Range(0.0, 1.0)
    Color emission = [0.0, 0.0, 0.0, 1.0] HDR
}
```

`SurfaceData` 提供 `albedo`、`normalWS`、`metallic`、`smoothness`、`occlusion`、`emission`、`alpha` 与 `specularHighlights` 等标准表面数据。

### 把材质状态写进 ShaderInfo

渲染队列和固定管线状态也在同一个头部声明：

```glsl
ShaderInfo {
    Name "Transparent Unlit"
    ShadingModel Unlit
    Queue 3000
    Cull Back
    DepthWrite Off
    DepthTest LessEqual
    Blend SrcAlpha OneMinusSrcAlpha
    CastShadows Off
    ReceiveShadows Off
}
```

材质选择 Shader 时使用的 ID 就是精确的 `Name`，并区分大小写。`Queue` 还决定该材质会被 RenderPipeline 的哪条 Route 消费。

## 进阶：可复用的着色模型

Surface Shader 回答“表面是什么”，着色模型回答“表面如何与光照交互”。这样多个材质可以共享 PBR、Unlit、卡通、半调或项目自己的光照模型。

```glsl
ShadingModelInfo {
    Name "FlatTint"
    Entry Forward evaluateForward
}

void evaluateForward(in SurfaceData s, out vec4 color) {
    vec3 base = clamp(s.albedo, 0.0, 1.0);
    color = vec4(base + s.emission, s.alpha);
}
```

Frag 中用 `ShadingModel FlatTint` 引用它。Forward 入口的固定签名是 `void evaluateForward(in SurfaceData s, out vec4 color)`，函数名可以通过 `Entry Forward` 改写。

需要自定义延迟渲染编码时，再声明 GBuffer 入口：

```glsl
ShadingModelInfo {
    Name "MyLighting"
    Imports ["Lighting", "PBR"]
    Entry Forward evaluateForward
    Entry GBuffer evaluateGBuffer
}
```

常规 Surface 模型没有提供 `Entry GBuffer` 时，引擎可以采用标准的 PBR 型 GBuffer 编码。只有表现确实需要不同的数据布局时才自定义它。这样普通材质无需改代码，就能在 Forward、Forward+ 与 Deferred Route 之间移动。

## 高级：完全自定义流程

只有 `surface()` 和标准顶点 Hook 无法表达需求时，才进入完全自定义阶段。典型用途包括程序化几何、编辑器工具、全屏效果与专用域渲染器。

### 全屏 Shader

```glsl
#version 450

ShaderInfo {
    Name "Color Invert"
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
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
    outColor = vec4(vec3(1.0) - source.rgb, source.a);
}
```

### 独立几何 Shader

当阶段需要自己提供几何 `main()` 时，使用 `Capabilities [Standalone]`。跨阶段数据放在 `Inputs` 和 `Outputs`，纹理写入 `Resources`，少量逐次绘制数据放在 `PushConstants`。Infernux 会生成对应的 GLSL 接口与绑定。

不要在这些声明外继续手写 `layout(location=...)`、`layout(set=..., binding=...)`、Uniform Block 或 Push Constant 布局。生成接口属于 RHI 契约，也是同一份源码未来迁移到其它后端的基础。

## 如何选择

| 目标 | 使用方式 |
| --- | --- |
| 改变网格形状或做顶点动画 | 带 `vertex()` 的标准 Vert |
| 定义贴图、颜色、金属度、自发光与透明度 | 带 `surface()` 的 Frag |
| 让多个材质共享一种新光照风格 | `.shadingmodel` |
| 编写全屏效果 | `Capabilities [Fullscreen]` |
| 接管专用几何阶段 | `Capabilities [Standalone]` |

优先选择能完成效果的最低层级。这样可以保留引擎管理的实例化、阴影、材质预览、渲染 Route 兼容性与后续 RHI 可迁移性。

## 常见错误

- **缺少固定入口：**结构化顶点阶段的自定义工作写在 `vertex()`；Surface Frag 必须提供 `surface()`。
- **切换 Shader 后物体消失：**检查 Vert/Frag 是否属于普通几何材质域，以及材质引用的 ID 大小写是否完全一致。
- **透明物体仍按不透明渲染：**一起检查 `Queue`、`DepthWrite` 和 `Blend`。
- **链接器提示布局重复：**移除手写 `layout`，改由 `ShaderInfo` 描述接口。
- **Deferred Route 表现错误：**只有标准 Surface 编码不够用时才提供显式 GBuffer 入口。
