# Plugins

An Infernux plugin is an InxPackage. You can ship code, assets, or both.

It works like Unity's Package Manager: drop a `.inxpkg`, point at a local folder, paste a GitHub URL, or install from the official list.

## Folder layout

```
MyPlugin/
  InxPackage.json
  README.md
  README.zh-CN.md
  LICENSE
  requirements.txt
  Runtime/            # ships with the game
  Editor/             # Editor only
  InxPluginPages/     # extra tabs in the Plugins window
  ...                 # everything else is regular assets
```

After install:

| You put | It lands in |
|---|---|
| `Runtime/`, `Editor/`, README, license | `Packages/<name>/` |
| Models, scenes, prefabs, textures | `Assets/Plugins/`, preserving package-relative paths |

`Runtime`, `Editor`, and `InxPluginPages` are case-sensitive. If you just want a regular folder named Runtime, nest it, e.g. `Content/Runtime`.

Names can have a namespace, like `studio/vfx-kit`. Nested folders are not dependencies.

When authoring from Project view, every selected directory name is preserved.
Importing several bare directories expands them directly below `Assets/Plugins/`;
the engine does not invent a plugin-name or `selection` wrapper. Import fails
explicitly if another GUID already owns a destination path.

## InxPackage.json

This is enough:

```json
{
  "reference": "studio/vfx-kit",
  "name": "VFX Kit",
  "version": "1.0.0",
  "engine": ">=0.3.7,<0.4"
}
```

`reference` is the plugin's identity. It does not change after install.

## Pages in the Plugins window

You do not need editor code to show docs:

- `README.md` is the Description tab. `README.zh-CN.md` is the Chinese one.
- `LICENSE` is the License tab.
- Extra markdown or text under `InxPluginPages/` becomes more tabs.

Chinese files only work one way: insert `.zh-CN` before the extension, e.g. `Guide.zh-CN.md`. The editor shows Chinese when the UI language is Chinese, otherwise the default file. `.en`, `Docs/`, and locale folders are ignored.

Images use normal markdown and must live in the package. Remote images are not downloaded.

To control tab order, set `pages` in `InxPackage.json`:

```json
{
  "pages": [
    {"id": "intro", "title": "Description", "path": "README.md"},
    {"id": "guide", "title": "Usage", "path": "InxPluginPages/Guide.md"}
  ]
}
```

If you omit `intro`, the package list uses the first paragraph of the README.

## Code that runs when the Editor starts

Subclass `InxPreload`. You do not register it in the json. The engine scans `Assets` and `Packages`:

```python
from Infernux.lifecycle import InxPreload

class Bootstrap(InxPreload):
    def preload(self, context):
        pass

    def unload(self):
        pass
```

Disabled packages are skipped. If `unload` fails, the engine stops and asks you to restart. It will not pretend the plugin is gone.

Use explicit relative imports for package-local Python code, for example `from .service import Service`. Every installed package has its own deterministic module namespace, so matching filenames in unrelated plugins never share module state. `Runtime/` participates in gameplay component loading and hot refresh; `Editor/` is loaded only by the plugin lifecycle.

## Dependencies

Names in `requirements.txt` are tried as plugins first, then pip. A pip package is just a Python dependency. It does not become an Infernux plugin. If pip fails, Infernux tries to remove what that install added. Local wheels and git installs may not come back exactly.

## Player builds

`Runtime` and regular assets go into the game. `Editor`, README, license, and doc pages stay out. There is no include/exclude list.

## Install, uninstall, moving files

Files are tracked by GUID, so moving them in the project is fine. Uninstall only deletes files this package owns and you have not edited. Removing a parent package does not remove child packages.

A `.inxpkg` sitting inside another package is just a file until you import it yourself or list it in `requirements.txt`.
