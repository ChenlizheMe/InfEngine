# AudioListener

<div class="class-info">
类位于 <b>Infernux.components.builtin</b>
</div>

**继承自:** [BuiltinComponent](Component.md)

## 描述

音频监听器组件。场景中的耳朵——通常挂在主摄像机上。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.4

在场景收听位置保留一个预期活动 Listener，通常位于活动 Camera 或玩家头部。Source 距离以它为基准。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| game_object_id | `int` | The ID of the GameObject this component is attached to. *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `serialize() → str` | Serialize the component to a JSON string. |
| `deserialize(json_str: str) → bool` | Deserialize the component from a JSON string. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

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

## 另请参阅

<!-- USER CONTENT START --> see_also
- [AudioSource](AudioSource.md)
- [Camera](Camera.md)
<!-- USER CONTENT END -->
