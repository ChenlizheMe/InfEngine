# Texture

<div class="class-info">
class in <b>Infernux.core</b>
</div>

## Description

Pythonic wrapper around C++ TextureData.

Example::

    tex = Texture.load("textures/albedo.png")
    print(tex.width, tex.height, tex.channels)
    pixels = tex.pixels_as_bytes()

    import numpy as np
    arr = np.frombuffer(pixels, dtype=np.uint8).reshape(
        tex.height, tex.width, tex.channels
    )

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Constructors

| Signature | Description |
|------|------|
| `Texture.__init__(native: TextureData) → None` | Wrap an existing C++ TextureData. |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| native | `TextureData` | The underlying C++ TextureData object. *(read-only)* |
| width | `int` | Width of the texture in pixels. *(read-only)* |
| height | `int` | Height of the texture in pixels. *(read-only)* |
| channels | `int` | Number of color channels (e.g. *(read-only)* |
| name | `str` | The display name of the texture. *(read-only)* |
| guid | `str` | Asset GUID when this texture originates from AssetManager. *(read-only)* |
| source_path | `str` | The file path the texture was loaded from. *(read-only)* |
| size | `Tuple[int, int]` | ``(width, height)`` tuple. *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `pixels_as_bytes() → bytes` | Get raw pixel data as bytes (row-major, RGBA or RGB). |
| `pixels_as_list() → list` | Get pixel data as a flat list of integers ``[0-255]``. |
| `to_numpy() → 'numpy.ndarray'` | Convert pixel data to a NumPy array ``(H, W, C)``, dtype ``uint8``. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static Texture.load(file_path: str) → Optional[Texture]` | Load a texture from an image file (PNG, JPG, BMP, TGA). |
| `static Texture.decode(data: bytes, name: str = ...) → Optional[Texture]` | Decode PNG/JPEG/BMP/TGA bytes into a texture. |
| `static Texture.solid_color(width: int, height: int, r: int = ..., g: int = ..., b: int = ..., a: int = ...) → Optional[Texture]` | Create a solid color texture. |
| `static Texture.checkerboard(width: int, height: int, cell_size: int = ...) → Optional[Texture]` | Create a checkerboard pattern texture. |
| `static Texture.from_native(native: TextureData) → Texture` | Wrap an existing C++ TextureData. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Operators

| Method | Returns |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.7. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
