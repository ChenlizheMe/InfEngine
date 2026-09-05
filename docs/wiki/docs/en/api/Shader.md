# Shader

<div class="class-info">
class in <b>Infernux.core</b>
</div>

## Description

Static utility class for shader management.

Example::

    Shader.reload("pbr_lit")
    if Shader.is_loaded("pbr_lit", "vertex"):
        print("Ready")

<!-- USER CONTENT START --> description

The generated signatures below preserve the published **0.3.7 snapshot**, not the 0.4.0 development API. See the 0.4.0 migration note below; the old raw shader wrappers were never connected to native publication.

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `Shader.is_loaded(name: str, shader_type: str = ...) → bool` | Check if a shader is loaded in the cache. |
| `Shader.invalidate(shader_id: str) → None` | Invalidate the shader program cache for hot-reload. |
| `Shader.reload(shader_id: str) → bool` | Invalidate cache and refresh all materials using this shader. |
| `Shader.refresh_materials(shader_id: str, engine: Optional[object] = ...) → bool` | Refresh all material pipelines that use a given shader. |
| `Shader.load_spirv(name: str, spirv_code: bytes, shader_type: str = ...) → None` | Load a SPIR-V shader module into the engine. |

<!-- USER CONTENT START --> static_methods

### 0.4.0 development migration

The supported operations are `Shader.is_loaded(name, shader_type="vertex")` and `Shader.reload(shader_id, shader_type=None)`. Querying includes both published standalone modules and linked material programs, performs no loading, and returns false without a live resource host or renderer. Reload accepts a registered ShaderInfo Name or an absolute/project-relative asset path and uses the canonical AssetManager reimport pipeline.

`reload` is an Editor/headless authoring operation; frozen Player content is read-only. Only `vertex` and `fragment` stages are supported. A Name shared across stages reloads both in vertex/fragment order unless a stage is specified. Duplicate Names within one stage require an explicit path. Reimports are sequential, not a multi-file atomic transaction. A successful metadata-only/headless reimport does not imply GPU publication; query `is_loaded` separately. A published last-known-good program can remain loaded after a failed edit, so `is_loaded` is not a last-compilation-success flag.

The obsolete, never-connected `invalidate`, `refresh_materials`, and `load_spirv` wrappers have been removed in 0.4.0. Edit/import a `.vert` or `.frag` with a `ShaderInfo` declaration and use `reload` (the Editor watcher already does this for external edits). Raw SPIR-V bytes are not an alternate public asset/publication route.

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.7. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
