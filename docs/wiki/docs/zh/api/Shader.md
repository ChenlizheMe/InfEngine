# Shader

<div class="class-info">
类位于 <b>Infernux.core</b>
</div>

## 描述

着色器程序资源。

<!-- USER CONTENT START --> description

下方生成签名保留已发布的 **0.3.7 快照**，并非 0.4.0 开发版 API。请参阅后面的 0.4.0 迁移说明；旧的裸 Shader 包装接口从未接通原生发布流程。

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `Shader.is_loaded(name: str, shader_type: str = ...) → bool` | Check if a shader is loaded in the cache. |
| `Shader.invalidate(shader_id: str) → None` | Invalidate the shader program cache for hot-reload. |
| `Shader.reload(shader_id: str) → bool` | Invalidate cache and refresh all materials using this shader. |
| `Shader.refresh_materials(shader_id: str, engine: Optional[object] = ...) → bool` | Refresh all material pipelines that use a given shader. |
| `Shader.load_spirv(name: str, spirv_code: bytes, shader_type: str = ...) → None` | Load a SPIR-V shader module into the engine. |

<!-- USER CONTENT START --> static_methods

### 0.4.0 开发版迁移

支持的操作为 `Shader.is_loaded(name, shader_type="vertex")` 和 `Shader.reload(shader_id, shader_type=None)`。查询包含独立模块与材质链接程序，不触发加载；没有活动资源宿主或渲染器时返回 false。重载按已登记的 ShaderInfo Name、绝对路径或项目相对路径，经统一 AssetManager 流程执行，成功返回 true，失败抛出异常。

`reload` 用于 Editor/headless 创作流程，Player 冻结内容只读。只支持 `vertex`、`fragment`；名称横跨两阶段时默认按顶点、片元顺序重导入两者，也可指定阶段。同一阶段有重名时必须给出资产路径。多文件重导入是顺序执行，并非跨文件原子事务。headless 只完成元数据重导入时，不代表 GPU 已发布；应单独查询 `is_loaded`。编译失败后旧的可用程序可能仍在，因此 `is_loaded` 不能当作最近一次编译成功的标志。

0.4.0 移除了从未接通的旧 `invalidate`、`refresh_materials`、`load_spirv` 包装接口。请编写带 `ShaderInfo` 声明的 `.vert`/`.frag` 资产，通过 `reload` 更新；Editor 的文件监听已对外部编辑执行同一流程。裸 SPIR-V 字节不作为另一套公开资产注入通道。

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.7 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
