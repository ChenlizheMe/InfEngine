# Texture

<div class="class-info">
类位于 <b>Infernux.core</b>
</div>

## 描述

纹理资源。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 构造函数

| 签名 | 描述 |
|------|------|
| `Texture.__init__(native: TextureData) → None` | Wrap an existing C++ TextureData. |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| native | `TextureData` | The underlying C++ TextureData object. *(只读)* |
| width | `int` | 纹理宽度（像素）。 *(只读)* |
| height | `int` | 纹理高度（像素）。 *(只读)* |
| channels | `int` | Number of color channels (e.g. *(只读)* |
| name | `str` | 纹理名称。 *(只读)* |
| guid | `str` | 纹理的全局唯一标识符。 *(只读)* |
| source_path | `str` | The file path the texture was loaded from. *(只读)* |
| size | `Tuple[int, int]` | ``(width, height)`` tuple. *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `pixels_as_bytes() → bytes` | Get raw pixel data as bytes (row-major, RGBA or RGB). |
| `pixels_as_list() → list` | Get pixel data as a flat list of integers ``[0-255]``. |
| `to_numpy() → 'numpy.ndarray'` | Convert pixel data to a NumPy array ``(H, W, C)``, dtype ``uint8``. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static Texture.load(file_path: str) → Optional[Texture]` | 从文件路径加载纹理。 |
| `static Texture.decode(data: bytes, name: str = ...) → Optional[Texture]` | Decode PNG/JPEG/BMP/TGA bytes into a texture. |
| `static Texture.solid_color(width: int, height: int, r: int = ..., g: int = ..., b: int = ..., a: int = ...) → Optional[Texture]` | Create a solid color texture. |
| `static Texture.checkerboard(width: int, height: int, cell_size: int = ...) → Optional[Texture]` | Create a checkerboard pattern texture. |
| `static Texture.from_native(native: TextureData) → Texture` | Wrap an existing C++ TextureData. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 运算符

| 方法 | 返回值 |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.4.0 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
