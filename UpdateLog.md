# Infernux v0.3.4 · Full Engine Update

Version 0.3.4 is the largest Infernux release so far. It rebuilds the rendering, GPU VFX, editor interaction, asset, runtime, physics, and Player distribution paths around explicit ownership and production-facing workflows while keeping Python as the public gameplay and tooling layer.

[简体中文更新日志](UpdateLog-zh.md)

**Baseline for comparison:** [`v0.2.9...v0.3.4`](https://github.com/ChenlizheMe/Infernux/compare/v0.2.9...v0.3.4)

### Highlights

- A programmable RenderStack and RenderGraph foundation now supports Forward, Forward+, and Deferred routes, reusable Effect assets, explicit stage mount points, custom pipelines, and per-camera render state.
- GPU Particle Graph now covers typed graph authoring, AOT graph-to-IR compilation, sprite, mesh, and ribbon outputs, events, continuations, sorting, culling, soft particles, and six-way lit smoke foundations.
- The editor now shares one interaction core for commands, shortcuts, selection, focus, documents, save/close transactions, dirty state, and undo/redo across Scene, Project, Inspector, Timeline, animation, FSM, and graph workflows.
- The asset system now treats GUID identity and Library artifacts as the durable contract, preserving references across moves and renames and separating authoring data from Player content.
- Standalone games use a native launcher, a private Python runtime, compressed `Content.inxpkg`, packaged runtime shader sources and engine assets, and no editor services.

### Rendering and shaders

- Added programmable Python RenderStack pipelines with ordered Effect and EffectGroup assets, explicit render stages, runtime parameter updates, and failure-safe fallback behavior.
- Unified Forward, Forward+, and Deferred geometry submission around the same material, lighting, shadow, and camera contracts.
- Added typed RenderGraph resources and passes, structural compilation caching, transient resource aliasing, explicit synchronization intents, renderer-list resources, and frame-safe retirement.
- Added structured GLSL stage metadata and interface linking so materials can author vertex, fragment, and shading-model responsibilities without manually maintaining descriptor layouts.
- Added reusable geometry-buffer requests for depth, normal, base color, motion, and project-defined buffers, including named sampling from specific passes.
- Isolated camera culling, shadows, render targets, and lighting resources so Scene, Game, preview, and runtime cameras no longer share mutable per-frame state.
- Added Forward+ tiled light lists, canonical GPU light data, light masks, particle lighting, and linked material surfaces.
- Improved shadow stability, cascade behavior, resource lifetime, MSAA routing, display encoding, fullscreen effects, outline, picking, gizmos, previews, and packaged-player render parity.
- Added engine-native capture for the editor, individual views, and game cameras.

### GPU Particle Graph

- Replaced the public CPU/GPU split with a GPU-first particle workflow and retained one authoring model for emitter settings, Init, Update, events, and Rendering stages.
- Added a typed common graph core, Particle Graph editor, node defaults, typed attributes, curves, gradients, noise, conditionals, normalized age, rotation and size over life, and runtime-exposed parameters.
- Added sprite, static-mesh, and GPU-resident ribbon outputs with live materials, textures, alignment, UV controls, flipbook foundations, sorting, indirect drawing, and per-view culling.
- Added typed GPU event payloads, event routing, indirect spawning, continuations, waits, and graph-owned lifecycle state.
- Added plane, sphere, surface, vector-field, signed-distance-volume, skin-pose, and scene-depth interaction foundations.
- Added soft particles, six-way lit smoke support, emitter lifecycle controls, Scene/Game preview, selection, gizmos, bounds, diagnostics, and scheduling telemetry.

### Assets, documents, and serialization

- Added revisioned Scene and Component documents with stable object and component identities, validation, transactional publication, rollback, and missing-script recovery.
- Added indexed asset records, dependency tracking, durable GUID references, asynchronous refresh arbitration, conflict-aware atomic writes, and current-disk reload semantics.
- Asset moves and renames now preserve references without requiring path-based runtime resolution.
- Expanded model, texture, shader, audio, material, prefab, animation, VFX, and physical-material import and preview paths.
- Added artifact-backed texture formats, mip generation, compression settings, GPU-correct previews, and broader common image, model, and audio format handling.
- Removed legacy serialization and shader syntax paths in favor of current schemas and explicit migration errors.

### Editor authoring and interaction

- Added a global command and shortcut router with panel-aware focus and Play-mode editing support.
- Added authoritative global selection across Hierarchy, Scene, Project, Inspector, UI, Timeline, Console, FSM, and node-based editors.
- Added a global action journal with transaction-aware undo/redo for scene edits, asset operations, component operations, document navigation, Timeline keys, graph edits, and UI authoring.
- Unified Save, Save As, Discard, Cancel, close, exit, external-change arbitration, and dirty-state ownership across authoring windows.
- Rebuilt common Node Graph foundations for connection handling, dynamic node rebuilding, selection, parameters, contextual commands, and undo integration used by Particle Graph and animation FSM authoring.
- Improved Hierarchy expansion, persistent objects, component reordering, serialized resource fields, Project search and navigation, Console filtering, Inspector refresh, and authoring-panel docking.

### Runtime, scripting, physics, and animation

- Improved Python component hot reload so method bodies and serialized fields can update during Play without replaying lifecycle entry points.
- Added safer runtime component proxies, lifecycle dispatch, coroutine waits, `WaitForEndOfFrame`, deferred scene loading, and scene-request generation control.
- Added batched native Transform and GameObject creation while preserving the standard scene model and public `instantiate` workflow.
- Improved Jolt rigidbody synchronization, physical materials, collider shapes, fixed-step behavior, interpolation, collision callbacks, and coherent shadow/visible-transform submission.
- Expanded 2D clips, sprite animation, Timeline, skeletal animation, skinned meshes, FBX takes, animation state machines, and shared graph authoring.
- Added `DontDestroyOnLoad` scene ownership and Hierarchy presentation for persistent runtime objects.

### Player build and distribution

- Exported games now use one native launcher and a private runtime instead of installing into or modifying the user's system Python environment.
- Added the compressed `Content.inxpkg` content container and Library-backed runtime assets while excluding editor panels, authoring metadata, caches, tests, and source-only services.
- Packaged built-in and project shader sources, Effects, custom pipelines, display encoding, fullscreen passes, and other runtime-only render dependencies required for editor/Player parity.
- Added the wheel-distributed Player Runtime Pack, portable Hub archive, installer, incremental Hub manifest, and SHA-256 release manifest.
- Moved lengthy build, asset scan, content packing, and finalization work away from the editor frame loop and exposed progress and cancellation through the build UI.
- Release builds suppress native debug/info chatter while preserving Python logs and native errors.

### MCP and validation

- Added an editor-side MCP Harness for deterministic project creation, scene and asset authoring, semantic UI inspection, bounded input, checkpoints, capture, and blocker reporting.
- MCP is an editor development and validation service; it is not included in exported games.
- Expanded native Vulkan, shader-linking, renderer, physics, headless, Python, packaging, website, and installed-wheel regression coverage.

### Upgrade notes

- Back up or branch existing projects before opening them in 0.3.4. This release intentionally removes several legacy data and shader compatibility paths.
- Reimport project assets after upgrading so current Library artifacts and dependency records are available.
- Custom shaders must use the current structured shader schema and exact shader identifiers. Legacy `@` syntax and hand-maintained engine descriptor layouts are no longer supported.
- Particle authoring is GPU-first. Existing CPU-particle-only content is not part of the supported 0.3.4 workflow.
- Windows 10/11 x64 with Python 3.12 remains the primary supported development and distribution target.

### Community

- Documentation: <https://infernux-engine.com/wiki.html>
- Community forum: <https://infernux-engine.discourse.group/>
- Issues: <https://github.com/ChenlizheMe/Infernux/issues>
