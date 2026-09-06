# Infernux v0.4.0 · Multiplatform Builds and Distribution

0.4.0 adds Windows, Linux, Android, and Web Player builds, expands InxPackage authoring and runtime asset delivery, and puts shared build environments under Hub management.

[简体中文更新日志](UpdateLog-zh.md)

**Baseline for comparison:** [`v0.3.7...v0.4.0`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.7...v0.4.0)

### Multiplatform

- Build Windows and Linux Editors and Players, Android APK/AAB packages, and Web Players from the shared build service.
- Deliver precompiled Players, runtimes, and target tools in the four platform plugins; normal exports require no engine checkout, submodules, CMake, or native engine compilation.
- Run native targets on Vulkan and browser targets on WebGPU, including Python gameplay, rendering, input, UI, audio, and particles.
- Exercise the same MultiPlatform040 project across targets, including button-triggered reads of packaged text assets.
- Add Windows/Linux desktop and platform Player CI builds, plus Web browser acceptance.

### Plugins and Assets

- Package only a repository's `package/` directory using a standalone `package.py`; keep README and language-specific build configuration outside the archive.
- Preserve direct local-folder authoring and generate default package metadata from the output filename when no manifest is supplied.
- Include scripts, materials, shaders, and arbitrary runtime files in plugins, with explicit asset metadata and live refresh under `Packages/`.
- Keep Player assets in `Content.inxpkg` instead of expanding the project's authoring directory tree. Resolve engine asset paths through cooked GUID identities and provide filesystem access for payloads that need it.
- Separate editor-only content from Player payloads and distribute platform build support as optional packages.
- Check compatible GitHub releases and explicitly select plugin updates while preserving GUIDs, enabled state, and user-added files; require consent before replacing local edits.
- Refresh the independent official catalog without upgrading project-pinned packages, and resolve former platform sources to their independent repositories.

### Hub and Build Environments

- Provide Windows and Linux Hub distributions and managed Python 3.13 environments.
- Install Android support from the Hub channel on Windows and Linux, sharing SDK, NDK, JDK, Gradle, and target Python dependencies; supply toolchain paths automatically and require installed support before enabling Android plugin import.
- Reuse downloads through the Hub Library while keeping project build caches inside the project.
- Group engine, Python, and Android installations into tabs under Installs, with background jobs, a compact progress strip, an expandable queue, and system-tray support.
- Keep interrupted Android kit downloads in Hub's shared cache so an explicitly restarted installation resumes the download; remove the download cache after successful installation.

---

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
