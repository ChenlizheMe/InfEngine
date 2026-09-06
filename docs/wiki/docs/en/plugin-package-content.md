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

Exporting multiple files or folders selected in File Manager preserves their paths relative to their common parent. Ordinary content is imported directly under `Assets/Plugins`, without adding a directory named after the package. Selecting `materials/` and `web/`, for example, produces `Assets/Plugins/materials/` and `Assets/Plugins/web/`.

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

Run `python package.py [destination.inxpkg]` from any working directory. The script depends only on the Python standard library and writes the native InxPack format directly, so neither Infernux nor a C++ toolchain is required on the author's packaging machine. When the destination is omitted, the archive is written beside the script using the repository directory name. Two builds from identical bytes and metadata produce an identical archive.

## Installation routes

| Package path | Project destination | Player |
|---|---|---|
| `runtime/...` | `Packages/<reference>/runtime/...` | included |
| `editor/...` | `Packages/<reference>/editor/...` | excluded |
| `plugin_pages/...` | `Packages/<reference>/plugin_pages/...` | excluded |
| `requirements.txt` | `Packages/<reference>/requirements.txt` | excluded |
| everything else | `Assets/Plugins/...` | included |

Archive names `runtime`, `editor`, and `plugin_pages` must be lowercase. If the project already has an authored `Runtime` or `Editor` directory, import reuses that role's physical spelling without creating a case variant or moving author files. Multiple existing case variants of one role are ambiguous and must be resolved by the author first.

Uninstall follows GUID ownership: moving or editing a file does not prevent removal while the package still owns it. Derived bytecode caches of removed scripts are cleaned in the same transaction. Shared files whose ownership has transferred to another package, newly authored files, and their caches are preserved.

The import panel supports per-file selection. Reopening a package with the same version and full GUID member list lets you add previously unchecked files without uninstalling it. Existing user edits, ownership, and enabled state are preserved; Python dependencies are not reinstalled. A different version or member list remains a package replacement and is not silently applied. Conflicting destination GUIDs are reported, and failed writes roll back only the new additions, preserving the existing installation.

## Explicit version updates

For an installed GitHub package, open **Versions**, choose **Check versions**, then
select a compatible release and **Update to selected version**. Checking and
downloading do not change the installed version. Release notes belong to the
selected version; older compatible versions can also be selected explicitly.

Updates reuse the installation transaction rather than uninstalling the package.
Existing GUIDs, enabled state, user-added files, customized importer settings and
user-moved assets are preserved. A selective import stays selective; newly added
package members are included. Package authors must retain existing asset GUIDs
between releases. Publisher renames move assets that remain at their original
locations; user-moved assets keep their locations.

Local edits to content being replaced or removed require explicit consent. If the
original cached archive has been cleared, the manager cannot prove that existing
content is unmodified and asks before replacement. Shared asset conflicts cannot
be forced. Failed file/registry publication restores the previous installation.
Local author packages continue to use editing and refresh, not remote replacement.

## Plugin pages

Only markdown or text under `plugin_pages/` becomes Plugins-window content. Root README and license files are repository documentation and are not read as plugin pages. Chinese content inserts `.zh-CN` before the extension, such as `guide.zh-CN.md`. Images use relative paths and stay under the package root.

## Hub shared storage

Plugin pip download/build caches default to `Cache/Python/Pip/` under Hub Shared, including source Hub launches. When starting the Editor directly without a Hub Shared location, the cache stays in the project's `Cache/Python/Pip/`. An explicit `PIP_CACHE_DIR` remains effective. The engine neither edits global pip configuration nor moves or deletes existing system caches. Temporary pip build files and generated requirements files belong to the project's `Cache/Plugins/.staging/` and are removed when the operation ends, including failure; the Editor process's own temporary directory is unchanged.

Hub-managed plugins, Android kits, Python runtimes, engine versions, and downloads default to `InfernuxHubData/Shared/` beside the installed Hub. Source runs of `packaging/launcher.py` use `packaging/InfernuxHubData/Shared/` in the repository, independently of the working directory. Set `INFERNUX_SHARED_DATA_ROOT` to choose an explicit shared location; Hub passes it to launched Editors. `INFERNUX_PACKAGE_CACHE_ROOT` remains a separate plugin-cache override.

Small user state, including project records and Editor preferences, still uses `INFERNUX_DATA_ROOT`; project caches stay in the project. Installer replacement and rollback preserve Shared, and application updates do not own its content. On Windows, the installer grants the installing account inherited Modify access to Shared only, not to the application directory.

Existing user-data directories are not moved or deleted automatically. Until migration is complete, existing deployments can explicitly point `INFERNUX_SHARED_DATA_ROOT` at the previous root containing `Library`, `PlatformKits`, `Runtimes`, and `Engines`. This is configuration, not a failure fallback. A standalone Editor launched without Hub's environment can still use its existing user-data root.

Hub Settings offers **Migrate Legacy Resources**. It previews the source, destination, and exact items before moving complete Python runtimes, Android kits, engine versions, plugin packages, and completed Python download archives in a background worker. Close Editors, builds, and downloads first; an open registered project blocks migration. Existing targets are skipped as whole units, with old copies retained—never merged or overwritten. Cross-volume copying finishes before the original is removed. Copy failures retain the original and report the error; completed moves remain at the destination. Projects, user state, unfinished downloads, and update staging are excluded. An explicit plugin-cache override excludes that cache from migration. Relative package-cache references remain unchanged, so project registries are not rewritten.

Uninstall removes Hub application files while preserving Shared and project records. On Windows, a separate system PowerShell process waits for Hub to exit before removal. Errors are reported rather than presented as successful deletion. The retained installation marker lets a reinstall reuse the existing shared resources.

## Code and Player builds

Subclass `InxPreload` for lifecycle work. Use explicit relative imports for package-local Python code. Every installed package receives an isolated deterministic module namespace. `runtime/` participates in gameplay component loading and hot refresh; `editor/` is loaded only by the Editor lifecycle.

A local package authored directly under `Packages/<name>/runtime/` does not need to be installed before building a Player. The build includes its current indexed runtime files and compiled preloads without changing the project's installation ownership records. A simple package needs no manifest; a namespaced author directory such as `Packages/studio/tool/` needs `inx_package.json` to define its boundary. Player module identity keeps the project's directory name, even if the manifest chooses a different reference for future `.inxpkg` distribution.

No include/exclude fallback list exists. A `.pyd` or `.wasm` under `runtime/` is runtime-owned; the same file under `editor/` is Editor-only. Materials, shaders, HTML, and other ordinary assets are imported under `Assets/Plugins` and included in the Player through the normal asset pipeline.

## Read assets by authored path

`inx.Application.asset_path("Assets/Data/message.txt")` and `inx.Application.asset_path("Packages/studio/server/runtime/config.json")` use the same general asset lookup API, without a language-specific or `Resources` directory restriction. The Editor resolves author files under `Assets` or `Packages`. The Player resolves the build-frozen binding between the authored path, GUID, and cooked artifact. A missing binding fails explicitly; loose files are not scanned to fill the gap.

The result is a filesystem path that can be passed to readers such as `Path(...).read_text(encoding="utf-8")`. The Player prepares this asset catalog before calling plugin `preload()`, so preloads can read cooked assets too. The authored path is a lookup key; the author directory need not exist beside the distribution.

## Raw runtime resources

Place content that must remain a raw file for an external runtime or library under `runtime/`, such as JARs, JSON, Wasm, vocabularies, or a complete directory tree with relative `include` statements. Player builds preserve this tree byte-for-byte and keep its relative layout. Do not depend on the process working directory or infer the installed location from a lifecycle script's `__file__`.

Gameplay code resolves a real read-only path with `inx.Application.package_path("studio/server", "runtime/server.jar")`. Inside `InxPreload.preload(context)`, use `context.package_path("runtime/server.jar")` without repeating the package reference. Windows, Linux, and Android prepare a product-private runtime content directory from the sealed archive; Web uses the Emscripten virtual filesystem. The returned path is not guaranteed to live beside the distribution. Libraries that accept filesystem paths can resolve sibling imports against the preserved package layout. Resolving a path does not grant execution support on the target: Web, for example, cannot launch Java or a native exe by its file path.

The final Player distributes project content in binary archives such as `Content.inxpkg`, without loose `Assets`, `Library`, or `Packages` directories beside the executable. This is content packaging, not a promise of irreversible encryption; runtimes that require a filesystem prepare content in a product-private location.

`package_path` resolves only installed package content that was included in the current Player. It rejects absolute paths, drive-qualified paths, and `..` traversal, and fails explicitly when content is missing. Treat the result as immutable release content; write generated or mutable state under `inx.Application.persistent_data_path()`.

In a Player, `package_path` and `asset_path` share the frozen asset catalog. Adding a loose file to the private directory cannot supply a missing binding. `package_path("studio/server", "runtime/data")` can also return a published raw-resource directory: its membership comes from build-registered assets, allowing a JSON file to reference an adjacent TXT by relative filename. Unpublished directories cannot be resolved; the general `asset_path` API continues to return files only.
