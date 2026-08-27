# Infernux v0.3.7 · Plugins

0.3.7 adds InxPackage plugins. MCP is no longer part of the engine core.

[简体中文更新日志](UpdateLog-zh.md)

**Baseline for comparison:** [`v0.3.6...HEAD`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.6...HEAD)

### Plugins

- Install from a `.inxpkg`, a local folder, Git, or the official list.
- `Runtime/` ships with the game. `Editor/` stays in the Editor.
- Package docs come from `README.md`, `LICENSE`, and `InxPluginPages/`.
- MCP moved to the default official plugin `infernux/mcp`. New projects include it. You can disable or uninstall it.

### Editor

- Plugins window for install, enable, disable, reload, and uninstall.
- Headless authoring keeps the same scene and undo path used by the Editor.

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
