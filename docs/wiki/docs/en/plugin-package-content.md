# InxPackage content pages

An Infernux plugin may provide human-facing pages without editor-specific Python code. GitHub sources and `.inxpkg` files use the same convention because source repositories are normalized into an InxPackage before installation.

## Conventional files

The layout is deliberately fixed rather than inferred:

- `README.md` becomes the default **Description** page. `README.zh-CN.md` is its Simplified Chinese variant.
- `LICENSE` becomes the default **License** page. `LICENSE.zh-CN.md` is its Simplified Chinese variant.
- Every `.md`, `.markdown`, or `.txt` file under `InxPluginPages/` becomes an additional tab. Its only localized form inserts `.zh-CN` immediately before the extension: `InxPluginPages/Guide.md` and `InxPluginPages/Guide.zh-CN.md` are one page.

English uses the unsuffixed default file. `.en`, `.zh`, locale directories, `Documentation/`, `Docs/`, and other locale spellings are not localization conventions. When the Editor language is Chinese, Infernux selects the exact `.zh-CN` variant and falls back to the default file when it is absent. Other Editor languages select the default file.

When `intro` is empty, the first useful `README.md` paragraph becomes the default registry summary; `README.zh-CN.md` supplies the Chinese summary automatically.

All page files must be UTF-8 text, live inside the plugin root, and be included in the package file list.

## Markdown images

Markdown pages support standard local image syntax, for example:

```markdown
![Control panel preview](Images/control-panel.png)
```

Relative paths resolve from the Markdown file's directory; paths beginning with `/` resolve from the plugin root. The image must be shipped in the plugin or `.inxpkg`, and paths escaping the plugin root are rejected. Cloned GitHub plugins follow the same rule. Remote HTTP images are not downloaded on the Editor UI thread; unresolved images display their alternative text.

Markdown pages also preserve level-one through level-six headings, paragraphs, dividers, nested ordered/unordered lists, block quotes, and fenced code blocks. Heading hierarchy uses color, separators, and prefixes at the Editor's standard font size.

## Explicit page order

`InxPackage.json` may define `pages` to control IDs, titles, paths, formats, and order:

```json
{
  "pages": [
    {"id": "intro", "title": "Overview", "path": "README.md"},
    {"id": "guide", "title": "Mission Guide", "path": "InxPluginPages/Guide.md"},
    {"id": "license", "title": "License", "path": "LICENSE", "format": "text"}
  ]
}
```

Supported formats are `markdown` and `text`. Explicit entries are shown first and override automatically discovered pages with the same `(id, locale)` pair. The only localized descriptor value is `"locale": "zh-CN"`.

## InxPackage v2 layout

`.inxpkg` is the only standalone plugin/content artifact. The installer recognizes only package-root conventions; manifests cannot map arbitrary project destinations.

| Package path | Project path | Role |
|---|---|---|
| `Runtime/**` | `Packages/<reference>/Runtime/**` | Editor, Headless, and Player runtime code |
| `Editor/**` | `Packages/<reference>/Editor/**` | Editor/Headless only |
| manifest, README, LICENSE, requirements, and information pages | `Packages/<reference>/**` | control plane and documentation |
| everything else | `Assets/Plugins/<reference>/**` | ordinary scenes, prefabs, models, textures, and other assets |

`Runtime`, `Editor`, and `InxPluginPages` require canonical casing. Names such as `Docs` and `Documentation` have no package-control meaning and are imported as ordinary assets. Wrap a reserved name, for example as `Content/Runtime`, when it should be treated as ordinary content. References may contain multiple namespace segments such as `aabbc/physics/jolt`; parent and child references can coexist, and path nesting does not imply a dependency.

`Library` contains download caches, install staging, and engine resource mirrors only. It is not an active plugin root, and installers never scatter files into the project `Assets` root.

## GUID identity, repeated import, and uninstall

Every payload and `.meta` sidecar enters the package inventory with its GUID, SHA-256, role, and path hint. GUID is durable identity; path is only the current location. A user may therefore move or rename an imported asset through AssetDatabase without breaking package ownership or Player build resolution.

- Packages may reuse the same GUID only when the content hash is identical. Exactly one ledger owner is maintained and ownership transfers when needed.
- A same-GUID/different-content conflict, or a destination occupied by another GUID, fails before writing.
- Uninstall removes only unmodified files owned by that package. Removing a parent reference never recursively removes child packages.
- Install and uninstall use staging, atomic replacement, and rollback. Project state lives in `ProjectSettings/InxPlugins.json`; reproducibility evidence lives in `ProjectSettings/InxPackages.lock.json`.

A nested `.inxpkg` is ordinary content unless the user imports it explicitly or `requirements.txt` names it.

## Dependencies, sources, and project Python

Direct artifacts, local source directories, Git, GitHub, other hosting providers, and HTTP artifacts are all normalized to the same InxPackage v2 model before the shared install transaction runs. Every resolved artifact is cached by SHA-256 under project `Library/InxPackageCache` for verification and offline reinstall.

A plain requirement name is resolved against the official InxPackage registry first and falls back to pip only when no plugin matches. URL, VCS, wheel, local-path, and arbitrary legal pip syntax retain pip semantics. A pip distribution remains a Python dependency: it does not gain plugin lifecycle or asset ownership. Pip always targets the project Python environment and its command/evidence is recorded in the lock.

## Generic InxPreload lifecycle

Preload is not a manifest field. Only scripts statically identified as subclasses of `Infernux.lifecycle.InxPreload` are imported:

```python
from Infernux.lifecycle import InxPreload

class Service(InxPreload):
    def preload(self, context):
        pass

    def unload(self):
        pass
```

Discovery covers `Assets` and `Packages`, including aliases and indirect inheritance. Lifecycle identity combines script GUID and type identity. Disabled packages are skipped, dependencies determine load order, and unload runs in reverse order. A failing `unload()` aborts disable/uninstall and reports that a process restart is required.

## Player export and default libraries

Player selection is structural: `Runtime` and ordinary Content ship; `Editor`, control files, and information pages do not. There is no manifest include/exclude or `player` trimming language. Exported Package Python scripts are compiled to bytecode.

The engine wheel carries the official registry, verified `.inxpkg` artifacts, and `default-libraries.json` under `Infernux/resources/official_packages`; the project read-only mirror is `Library/Resources/official_packages`. Defaults are applied only while creating a project or after an explicit repair request. Ordinary startup never reinstalls a plugin that the user removed. In 0.3.7, `infernux/mcp` is the first and currently only official default plugin.
