# Material

<div class="class-info">
class in <b>Infernux.core</b>
</div>

## Description

Pythonic wrapper around C++ InxMaterial.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Constructors

| Signature | Description |
|------|------|
| `Material.__init__(native: InxMaterial) → None` | Wrap an existing C++ InxMaterial. |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| native | `InxMaterial` | The underlying C++ InxMaterial object. *(read-only)* |
| name | `str` | The display name of the material. |
| guid | `str` | The globally unique identifier for this material. *(read-only)* |
| render_queue | `int` | The render queue priority for draw order sorting. |
| shader_name | `str` | The name of the shader program used by this material. |
| vert_shader_name | `str` | The vertex shader name override. |
| frag_shader_name | `str` | The fragment shader name override. |
| is_builtin | `bool` | Whether this is a built-in engine material. *(read-only)* |
| render_state_overrides | `int` | Bitmask of render state overrides applied to this material. |
| cull_mode | `int` | The face culling mode (0=None, 1=Front, 2=Back). |
| depth_write_enable | `bool` | Whether depth buffer writing is enabled. |
| depth_test_enable | `bool` | Whether depth testing is enabled. |
| depth_compare_op | `int` | The depth comparison operator. |
| blend_enable | `bool` | Whether alpha blending is enabled. |
| surface_type | `str` | The surface type ('opaque' or 'transparent'). |
| alpha_clip_enabled | `bool` | Whether alpha clipping (cutout) is enabled. |
| alpha_clip_threshold | `float` | The alpha value threshold for clipping. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `to_dict() → dict` | Return the material document without encoding JSON text. |
| `serialize_document() → dict` |  |
| `apply_dict(document: dict) → bool` | Transactionally apply a material document. |
| `deserialize_document(document: dict) → bool` |  |
| `dispose() → None` | Release the underlying native material resources. |
| `clone() → Material` | Create a deep copy of this material, including shader state and properties. |
| `set_shader(shader_name: str) → None` | Set the shader used by this material. |
| `set_float(name: str, value: float) → None` | Set a float uniform property on the material. |
| `set_int(name: str, value: int) → None` | Set an integer uniform property on the material. |
| `set_color(name: str, r: float, g: float, b: float, a: float = ...) → None` | Set a color uniform property on the material. |
| `set_vector2(name: str, x: float, y: float) → None` | Set a 2D vector uniform property on the material. |
| `set_vector3(name: str, x: float, y: float, z: float) → None` | Set a 3D vector uniform property on the material. |
| `set_vector4(name: str, x: float, y: float, z: float, w: float) → None` | Set a 4D vector uniform property on the material. |
| `set_texture_guid(name: str, texture_guid: str) → None` | Assign a texture to a sampler slot by GUID. |
| `clear_texture(name: str) → None` | Remove the texture assigned to a sampler slot. |
| `get_float(name: str, default: float = ...) → float` | Get a float uniform property value. |
| `get_int(name: str, default: int = ...) → int` | Get an integer uniform property value. |
| `get_color(name: str) → Tuple[float, float, float, float]` | Get a color property as an (R, G, B, A) tuple. |
| `get_vector2(name: str) → Tuple[float, float]` | Get a 2D vector property as an (X, Y) tuple. |
| `get_vector3(name: str) → Tuple[float, float, float]` | Get a 3D vector property as an (X, Y, Z) tuple. |
| `get_vector4(name: str) → Tuple[float, float, float, float]` | Get a 4D vector property as an (X, Y, Z, W) tuple. |
| `get_texture(name: str) → Optional[str]` | Get the GUID of the texture assigned to a sampler slot. |
| `has_property(name: str) → bool` | Check whether a shader property exists on this material. |
| `get_property(name: str) → Any` | Get a shader property value by name. |
| `get_all_properties() → dict` | Get all shader properties as a dictionary. |
| `save(file_path: str) → bool` | Save the material to a file. |
| `set_param(name: str, value: Any) → None` | Set a non-texture material property using type/shape dispatch. |
| `set_texture(name: str, value: Any) → None` | Set a texture property from GUID, path, Texture, or None. |
| `flush() → None` | Force-write any pending changes to disk. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static Material.create_lit(name: str = ...) → Material` | Create a new material with the default lit (PBR) shader. |
| `static Material.create_unlit(name: str = ...) → Material` | Create a new material with the unlit shader. |
| `static Material.from_native(native: InxMaterial) → Material` | Wrap an existing C++ InxMaterial instance. |
| `static Material.load(file_path: str) → Optional[Material]` | Load a material from a file path. |
| `static Material.get(name: str) → Optional[Material]` | Get a cached material by name. |
| `static Material.flush_all_pending() → None` | Flush all materials that have throttled pending saves. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Operators

| Method | Returns |
|------|------|
| `__repr__() → str` | `str` |
| `__eq__(other: object) → bool` | `bool` |
| `__hash__() → int` | `int` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.4. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
