# Time

<div class="class-info">
类位于 <b>Infernux.timing</b>
</div>

## 描述

时间管理器。掌管每一帧的时间节奏——引擎的心跳。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.4

玩法使用缩放时间；暂停后仍继续的 UI 或诊断使用非缩放时间。物理工作放入固定更新。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| time | `float` | 自游戏启动以来的时间（秒）。 |
| delta_time | `float` | 上一帧耗时（秒）。写游戏逻辑离不开它。 |
| unscaled_time | `float` | 不受 time_scale 影响的时间。 |
| unscaled_delta_time | `float` | 不受 time_scale 影响的帧耗时。 |
| fixed_delta_time | `float` | 固定更新时间间隔（秒）。 |
| fixed_time | `float` | The time since the last fixed update. |
| fixed_unscaled_time | `float` | The unscaled time since the last fixed update. |
| time_scale | `float` | 时间缩放。设为 0 暂停，设为 2 双倍速。 |
| frame_count | `int` | 自启动以来的帧数。 |
| realtime_since_startup | `float` | 自启动以来的真实时间（秒）。 |
| maximum_delta_time | `float` | The maximum time a frame can take before delta_time is clamped. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 示例

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

## 另请参阅

<!-- USER CONTENT START --> see_also
- [InxComponent](InxComponent.md)
- [Rigidbody](Rigidbody.md)
<!-- USER CONTENT END -->
