# SceneManager

<div class="class-info">
类位于 <b>Infernux.scene</b>
</div>

## 描述

运行时场景加载与卸载管理器。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| active_scene | `Optional[object]` |  *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static SceneManager.get_active_scene() → Optional[object]` | 获取当前活动场景。 |
| `static SceneManager.get_scene_by_name(name: str) → Optional[str]` | Get a scene path by its name. |
| `static SceneManager.get_scene_by_build_index(build_index: int) → Optional[str]` | Get a scene path by its build index. |
| `static SceneManager.get_scene_at(index: int) → Optional[str]` | 按索引获取已加载的场景。 |
| `static SceneManager.load_scene(scene: Union[int, str]) → bool` | 按名称或路径加载场景。 |
| `static SceneManager.wait_for_load_scene(scene: Union[int, str]) → bool` | Prepare a scene asynchronously and switch when it is ready. |
| `static SceneManager.prepare_scene(scene: Union[int, str]) → bool` | Prepare a scene asynchronously without publishing it. |
| `static SceneManager.is_scene_prepared() → bool` | Return whether a held scene is ready to publish. |
| `static SceneManager.activate_prepared_scene() → bool` | Publish the scene previously prepared by prepare_scene. |
| `static SceneManager.process_pending_load() → None` | Process any pending scene load request. |
| `static SceneManager.is_scene_load_pending() → bool` | Return whether a deferred runtime scene load is queued or executing. |
| `static SceneManager.get_scene_count() → int` | 获取已加载的场景数量。 |
| `static SceneManager.get_scene_name(build_index: int) → Optional[str]` | Get a scene name by build index. |
| `static SceneManager.get_scene_path(build_index: int) → Optional[str]` | Get a scene file path by build index. |
| `static SceneManager.get_build_index(name: str) → int` | Get the build index of a scene by name. |
| `static SceneManager.get_all_scene_names() → List[str]` | Get a list of all scene names in the build. |
| `static SceneManager.dont_destroy_on_load(game_object: object) → None` | Mark a game object so it survives scene loads. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.5 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
