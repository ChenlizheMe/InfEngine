# Time

<div class="class-info">
class in <b>Infernux.timing</b>
</div>

## Description

Provides access to time information for the current frame.

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.3.5

Use scaled time for gameplay and unscaled time for UI or diagnostics that continue while paused. Physics work belongs to fixed updates.
<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| time | `float` | The time in seconds since the start of the game. |
| delta_time | `float` | The time in seconds since the last frame. |
| unscaled_time | `float` | The unscaled time in seconds since the start of the game. |
| unscaled_delta_time | `float` | The unscaled time in seconds since the last frame. |
| fixed_delta_time | `float` | The interval in seconds at which physics and fixed updates are performed. |
| fixed_time | `float` | The time since the last fixed update. |
| fixed_unscaled_time | `float` | The unscaled time since the last fixed update. |
| time_scale | `float` | The scale at which time passes (1.0 = normal speed). |
| frame_count | `int` | The total number of frames rendered since the start of the game. |
| realtime_since_startup | `float` | The real time in seconds since the application started. |
| maximum_delta_time | `float` | The maximum time a frame can take before delta_time is clamped. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
from Infernux import InxComponent, Time


class PauseClock(InxComponent):
    def update(self, delta_time: float) -> None:
        gameplay_seconds = Time.time
        menu_step = Time.unscaled_delta_time
        print(gameplay_seconds, menu_step)
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [InxComponent](InxComponent.md)
- [Rigidbody](Rigidbody.md)
<!-- USER CONTENT END -->
