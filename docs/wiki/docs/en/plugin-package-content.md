# Plugins

An Infernux plugin is an InxPackage. It may carry Python, native libraries, Wasm, Java resources, materials, shaders, web pages, or arbitrary files.

## Local authoring

When you export a local folder, that folder is already the package root:

```text
abc/
  runtime/          # available in Editor and Player
  editor/           # Editor only
  plugin_pages/     # pages in the Plugins window
  materials/
  shaders/
  web/
```

`inx_package.json` is optional. If it is absent, export generates the package metadata and uses the output `.inxpkg` filename as the default `name` and `reference`. For example, exporting `abc/` as `physics_tools.inxpkg` creates the identity `physics_tools`. Add the manifest only when you need explicit metadata:

```json
{
  "reference": "studio/vfx-kit",
  "name": "VFX Kit",
  "version": "1.0.0",
  "engine": ">=0.4,<0.5",
  "intro": "Reusable visual effects."
}
```

The manifest does not currently declare `requirements` or `dependencies`. An optional `requirements.txt` is recognized by its fixed filename.

## Git repository layout

A repository adds exactly one wrapper:

```text
vfx-kit/
  README.md          # GitHub documentation, never packaged
  package.py         # standalone standard-library packer
  CMakeLists.txt     # optional author build, never packaged
  package/           # the only directory entering .inxpkg
    inx_package.json
    runtime/
    editor/
    plugin_pages/
    native/backend.pyd
    web/module.wasm
```

The outer repository is unrestricted. CMake, Cargo, Gradle, npm, or another build may place its final outputs into `package/`. The packer treats known and unknown extensions as bytes; location, not extension guessing, defines ownership and Player export.

## Installation routes

| Package path | Project destination | Player |
|---|---|---|
| `runtime/...` | `Packages/<reference>/runtime/...` | included |
| `editor/...` | `Packages/<reference>/editor/...` | excluded |
| `plugin_pages/...` | `Packages/<reference>/plugin_pages/...` | excluded |
| `requirements.txt` | `Packages/<reference>/requirements.txt` | excluded |
| everything else | `Assets/Plugins/...` | included |

The lowercase names `runtime`, `editor`, and `plugin_pages` are exact. Files are tracked by GUID, so uninstall removes only unedited files owned by that package.

## Plugin pages

Only markdown or text under `plugin_pages/` becomes Plugins-window content. Root README and license files are repository documentation and are not read as plugin pages. Chinese content inserts `.zh-CN` before the extension, such as `guide.zh-CN.md`. Images use relative paths and stay under the package root.

## Code and Player builds

Subclass `InxPreload` for lifecycle work. Use explicit relative imports for package-local Python code. Every installed package receives an isolated deterministic module namespace. `runtime/` participates in gameplay component loading and hot refresh; `editor/` is loaded only by the Editor lifecycle.

No include/exclude fallback list exists. A `.pyd` or `.wasm` under `runtime/` is runtime-owned; the same file under `editor/` is Editor-only. Materials, shaders, HTML, and other ordinary assets are imported under `Assets/Plugins` and included in the Player through the normal asset pipeline.
