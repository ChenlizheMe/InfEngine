# AudioListener

<div class="class-info">
class in <b>Infernux.components.builtin</b>
</div>

**Inherits from:** [BuiltinComponent](Component.md)

## Description

Represents the listener/ears for 3D audio in the scene.

Attach one AudioListener to the main camera in most games. The engine keeps
one active listener; additional enabled listeners remain registered but can
be standby instead of immediately replacing the active listener.

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.3.4

Place one intended active listener at the scene's listening position, normally on the active Camera or player head. Source distance is measured relative to it.
<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| game_object_id | `int` | The ID of the GameObject this component is attached to. *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `serialize() → str` | Serialize the component to a JSON string. |
| `deserialize(json_str: str) → bool` | Deserialize the component from a JSON string. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
from Infernux import AudioListener, GameObject

camera_object = GameObject.find("Main Camera")
if camera_object is not None:
    listener = camera_object.get_component(AudioListener)
    if listener is None:
        listener = camera_object.add_component(AudioListener)
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [AudioSource](AudioSource.md)
- [Camera](Camera.md)
<!-- USER CONTENT END -->
