# SceneManager

<div class="class-info">
class in <b>Infernux.scene</b>
</div>

## Description

Manages scene loading, unloading, and queries.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| active_scene | `Optional[object]` |  *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static SceneManager.get_active_scene() → Optional[object]` | Get the currently active scene. |
| `static SceneManager.get_scene_by_name(name: str) → Optional[str]` | Get a scene path by its name. |
| `static SceneManager.get_scene_by_build_index(build_index: int) → Optional[str]` | Get a scene path by its build index. |
| `static SceneManager.get_scene_at(index: int) → Optional[str]` | Get a scene path by its index in the scene list. |
| `static SceneManager.load_scene(scene: Union[int, str]) → bool` | Load a scene by file path or build index. |
| `static SceneManager.wait_for_load_scene(scene: Union[int, str]) → bool` | Prepare a scene asynchronously and switch when it is ready. |
| `static SceneManager.prepare_scene(scene: Union[int, str]) → bool` | Prepare a scene asynchronously without publishing it. |
| `static SceneManager.is_scene_prepared() → bool` | Return whether a held scene is ready to publish. |
| `static SceneManager.activate_prepared_scene() → bool` | Publish the scene previously prepared by prepare_scene. |
| `static SceneManager.process_pending_load() → None` | Process any pending scene load request. |
| `static SceneManager.is_scene_load_pending() → bool` | Return whether a deferred runtime scene load is queued or executing. |
| `static SceneManager.get_scene_count() → int` | Get the total number of scenes in the build. |
| `static SceneManager.get_scene_name(build_index: int) → Optional[str]` | Get a scene name by build index. |
| `static SceneManager.get_scene_path(build_index: int) → Optional[str]` | Get a scene file path by build index. |
| `static SceneManager.get_build_index(name: str) → int` | Get the build index of a scene by name. |
| `static SceneManager.get_all_scene_names() → List[str]` | Get a list of all scene names in the build. |
| `static SceneManager.dont_destroy_on_load(game_object: object) → None` | Mark a game object so it survives scene loads. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.4. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
