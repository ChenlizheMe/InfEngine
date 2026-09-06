# Shader

<div class="class-info">
class in <b>Infernux.core</b>
</div>

## Description

Query published shaders and reimport edited ShaderInfo authoring assets.

<!-- USER CONTENT START --> description

The signatures below describe the 0.4.0 API. Use ShaderInfo assets for shader authoring and the operations below to query publication or reimport edited assets.

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `Shader.is_loaded(name: str, shader_type: str = ...) → bool` | Query GPU publication of a standalone stage or linked material program. |
| `Shader.reload(shader_id: str, shader_type: str | None = ...) → bool` | Reimport by ShaderInfo Name or asset path; failures raise an exception. |

<!-- USER CONTENT START --> static_methods

### 0.4.0 migration

The supported operations are `Shader.is_loaded(name, shader_type="vertex")` and `Shader.reload(shader_id, shader_type=None)`. Querying includes both published standalone modules and linked material programs, performs no loading, and returns false without a live resource host or renderer. Reload accepts a registered ShaderInfo Name or an absolute/project-relative asset path and uses the canonical AssetManager reimport pipeline.

`reload` is an Editor/headless authoring operation; frozen Player content is read-only. Only `vertex` and `fragment` stages are supported. A Name shared across stages reloads both in vertex/fragment order unless a stage is specified. Duplicate Names within one stage require an explicit path. Reimports are sequential, not a multi-file atomic transaction. A successful metadata-only/headless reimport does not imply GPU publication; query `is_loaded` separately. A published last-known-good program can remain loaded after a failed edit, so `is_loaded` is not a last-compilation-success flag.

The obsolete, never-connected `invalidate`, `refresh_materials`, and `load_spirv` wrappers have been removed in 0.4.0. Edit/import a `.vert` or `.frag` with a `ShaderInfo` declaration and use `reload` (the Editor watcher already does this for external edits). Raw SPIR-V bytes are not an alternate public asset/publication route.

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.4.0. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
