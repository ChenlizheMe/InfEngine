# Light

<div class="class-info">
class in <b>Infernux.components.builtin</b>
</div>

**Inherits from:** [BuiltinComponent](Component.md)

## Description

A Light component that illuminates the scene.

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.3.7

Choose light type, range, intensity, color, and shadow settings for the scene scale. Confirm unshadowed lighting first, then enable shadows and tune bias.
<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| light_type | `int` | The type of light (Directional, Point, or Spot). |
| color | `List[float]` | The color of the light. |
| intensity | `float` | The brightness of the light. |
| range | `float` | The range of the light in world units. |
| spot_angle | `float` | The inner cone angle of the spot light in degrees. |
| outer_spot_angle | `float` | The outer cone angle of the spot light in degrees. |
| shadows | `int` | The shadow casting mode of the light. |
| shadow_strength | `float` | The strength of the shadows cast by this light. |
| shadow_bias | `float` | Bias value to reduce shadow acne artifacts. |
| shadow_normal_bias | `float` |  |
| shadow_softness | `float` |  |
| affect_geometry | `bool` |  |
| affect_particles | `bool` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `serialize() → str` | Serialize the component to a JSON string. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Lifecycle Methods

| Method | Description |
|------|------|
| `on_draw_gizmos_selected() → None` | Draw a type-specific gizmo when the light is selected. |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
from Infernux import GameObject, Light, LightType

light_object = GameObject.find("Key Light")
if light_object is not None:
    light = light_object.get_component(Light)
    if light is not None:
        light.light_type = LightType.Point
        light.intensity = 2.0
        light.range = 12.0
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [LightType](LightType.md)
- [MeshRenderer](MeshRenderer.md)
<!-- USER CONTENT END -->
