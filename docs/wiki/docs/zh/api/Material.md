# Material

<div class="class-info">
类位于 <b>Infernux.core</b>
</div>

## 描述

材质类。控制物体的视觉外观。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 构造函数

| 签名 | 描述 |
|------|------|
| `Material.__init__(native: InxMaterial) → None` | Wrap an existing C++ InxMaterial. |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| native | `InxMaterial` | 底层 C++ InxMaterial 对象。 *(只读)* |
| name | `str` | 材质的显示名称。 |
| guid | `str` | 材质的全局唯一标识符。 *(只读)* |
| render_queue | `int` | 渲染队列优先级，用于绘制排序。 |
| shader_name | `str` | 材质使用的着色器程序名称。 |
| vert_shader_name | `str` | 顶点着色器名称。 |
| frag_shader_name | `str` | 片段着色器名称。 |
| is_builtin | `bool` | 是否为引擎内置材质。 *(只读)* |
| render_state_overrides | `int` | 应用于此材质的渲染状态覆盖位掩码。 |
| cull_mode | `int` | 面剔除模式（0=无，1=正面，2=背面）。 |
| depth_write_enable | `bool` | 是否启用深度缓冲写入。 |
| depth_test_enable | `bool` | 是否启用深度测试。 |
| depth_compare_op | `int` | 深度比较运算符。 |
| blend_enable | `bool` | 是否启用 Alpha 混合。 |
| surface_type | `str` | 表面类型（'opaque' 或 'transparent'）。 |
| alpha_clip_enabled | `bool` | 是否启用 Alpha 裁剪。 |
| alpha_clip_threshold | `float` | Alpha 裁剪阈值。 |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `to_dict() → dict` | 将材质序列化为字典。 |
| `serialize_document() → dict` |  |
| `apply_dict(document: dict) → bool` | Transactionally apply a material document. |
| `deserialize_document(document: dict) → bool` |  |
| `dispose() → None` | 释放底层原生材质资源。 |
| `clone() → Material` | Create a deep copy of this material, including shader state and properties. |
| `set_shader(shader_name: str) → None` | 设置材质使用的着色器。 |
| `set_float(name: str, value: float) → None` | 设置浮点数 uniform 属性。 |
| `set_int(name: str, value: int) → None` | 设置整数 uniform 属性。 |
| `set_color(name: str, r: float, g: float, b: float, a: float = ...) → None` | 设置颜色 uniform 属性。 |
| `set_vector2(name: str, x: float, y: float) → None` | 设置二维向量 uniform 属性。 |
| `set_vector3(name: str, x: float, y: float, z: float) → None` | 设置三维向量 uniform 属性。 |
| `set_vector4(name: str, x: float, y: float, z: float, w: float) → None` | 设置四维向量 uniform 属性。 |
| `set_texture_guid(name: str, texture_guid: str) → None` | 通过 GUID 将纹理分配给采样器槽。 |
| `clear_texture(name: str) → None` | 移除分配给采样器槽的纹理。 |
| `get_float(name: str, default: float = ...) → float` | 获取浮点数属性值。 |
| `get_int(name: str, default: int = ...) → int` | 获取整数属性值。 |
| `get_color(name: str) → Tuple[float, float, float, float]` | 获取颜色属性（返回 RGBA 元组）。 |
| `get_vector2(name: str) → Tuple[float, float]` | 获取二维向量属性。 |
| `get_vector3(name: str) → Tuple[float, float, float]` | 获取三维向量属性。 |
| `get_vector4(name: str) → Tuple[float, float, float, float]` | 获取四维向量属性。 |
| `get_texture(name: str) → Optional[str]` | 获取采样器槽中纹理的 GUID。 |
| `has_property(name: str) → bool` | 检查着色器属性是否存在。 |
| `get_property(name: str) → Any` | 按名称获取着色器属性值。 |
| `get_all_properties() → dict` | 获取所有着色器属性的字典。 |
| `save(file_path: str) → bool` | 将材质保存到文件。 |
| `set_param(name: str, value: Any) → None` | Set a non-texture material property using type/shape dispatch. |
| `set_texture(name: str, value: Any) → None` | Set a texture property from GUID, path, Texture, or None. |
| `flush() → None` | Force-write any pending changes to disk. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static Material.create_lit(name: str = ...) → Material` | 使用默认 PBR 着色器创建新材质。 |
| `static Material.create_unlit(name: str = ...) → Material` | 使用无光照着色器创建新材质。 |
| `static Material.from_native(native: InxMaterial) → Material` | 封装现有的 C++ InxMaterial 实例。 |
| `static Material.load(file_path: str) → Optional[Material]` | 从文件路径加载材质。 |
| `static Material.get(name: str) → Optional[Material]` | 按名称获取缓存的材质。 |
| `static Material.flush_all_pending() → None` | Flush all materials that have throttled pending saves. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 运算符

| 方法 | 返回值 |
|------|------|
| `__repr__() → str` | `str` |
| `__eq__(other: object) → bool` | `bool` |
| `__hash__() → int` | `int` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.6 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
