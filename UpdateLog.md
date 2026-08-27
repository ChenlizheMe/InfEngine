# Infernux v0.3.7 · Plugins and Skeletal Animation

0.3.7 adds the InxPackage plugin system, moves MCP out of the engine core, and fixes skeletal animation imported from separate FBX files.

[简体中文更新日志](UpdateLog-zh.md)

**Baseline for comparison:** [`v0.3.6...v0.3.7`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.6...v0.3.7)

### Plugins

- Install from a `.inxpkg`, a local folder, Git, or the official list.
- `Runtime/` ships with the game. `Editor/` stays in the Editor.
- Package docs come from `README.md`, `LICENSE`, and `InxPluginPages/`.
- MCP moved to the default official plugin `infernux/mcp`. New projects include it. You can disable or uninstall it.

### Skeletal Animation

- Retarget animation-only FBX files through exact joint identities while accepting Assimp-generated pivot and helper nodes between mapped joints.
- Reject renamed or structurally incompatible rigs instead of guessing from geometry and silently driving the wrong limbs.

### Editor and Authoring

- Use the Plugins window to install, enable, disable, reload, and uninstall packages with visible progress from download through activation.
- Keep Headless and MCP authoring on the same scene, command, permission, and undo paths used by the Editor.

---

# Infernux v0.3.6 · Unified Hierarchy and Hub Updates

Version 0.3.6 unifies the editor Hierarchy and restores automatic update discovery for packaged Infernux Hub builds.

**Baseline for comparison:** [`v0.3.5...v0.3.6`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.5...v0.3.6)

### Editor

- Removed the separate Hierarchy UI mode. Scene and UI objects now share one tree, one selection model, and one context-menu path.
- Preserved Canvas root placement and UI Screen subtree constraints in the unified Hierarchy.

### Hub Updates

- Restored startup update checks in packaged Nuitka Hub builds.
- Kept the Install Engine Version list on the selected Hub theme instead of inheriting the desktop system palette.
- Added verified full-package update fallback and elevation for protected installation directories.
- Changed the release pipeline to publish independently installable full Hub packages instead of incremental patch assets.
