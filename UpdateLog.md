# Infernux v0.3.6 · Unified Hierarchy and Hub Updates

Version 0.3.6 unifies the editor Hierarchy and restores automatic update discovery for packaged Infernux Hub builds.

[简体中文更新日志](UpdateLog-zh.md)

**Baseline for comparison:** [`v0.3.5...v0.3.6`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.5...v0.3.6)

### Editor

- Removed the separate Hierarchy UI mode. Scene and UI objects now share one tree, one selection model, and one context-menu path.
- Preserved Canvas root placement and UI Screen subtree constraints in the unified Hierarchy.

### Hub Updates

- Restored startup update checks in packaged Nuitka Hub builds.
- Kept the Install Engine Version list on the selected Hub theme instead of inheriting the desktop system palette.
- Added verified full-package update fallback and elevation for protected installation directories.
- Changed the release pipeline to publish independently installable full Hub packages instead of incremental patch assets.
