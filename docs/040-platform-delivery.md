# 040 platform delivery closure

Status: active. This is an implementation and delivery gate, not a statement
that the following capabilities have already shipped.

Completion requires all five sections below, updated public release artifacts,
and a green, mergeable engine PR. Publishing exporter-only plugins is not enough.

## 1. Complete platform packages

- [ ] Windows and Linux plugins carry their precompiled Player distributions.
- [ ] Android plugins carry the supported ABI-specific native Player payloads.
- [ ] Web plugins carry the precompiled WASM/JavaScript runtime and the host-side
  asset/shader tools needed for supported build hosts.
- [ ] Normal game exports require no engine source checkout, Git submodules,
  CMake invocation, or engine compilation, including hidden compiler fallback.
- [ ] Editor runtime files remain engine-owned. Target-specific delivery files
  have one declared owner; shared dependencies are not duplicated in Hub and plugins.
- [ ] Release CI builds runtime payloads and publishes matching complete packages.

## 2. Hub Android compatibility

- [ ] Publish channel-installable kits for Windows and Linux build hosts.
- [ ] Hub owns the SDK, NDK, JDK, Gradle and reusable target Python dependencies.
- [ ] Installation prepares paths and official build dependencies without manual
  environment variables, source checkouts, or Conda knowledge.
- [ ] Installation remains asynchronous with the compact queue UI.
- [ ] Editor import availability refreshes after Hub installation finishes.

## 3. Explicit plugin updates

- [x] Discover compatible releases and show installed/available versions and notes.
- [x] Let the user choose a version and update without manual uninstall.
- [x] Keep the project pinned until the user explicitly changes its version.
- [x] Preserve GUID identity, enabled state and user-added files.
- [x] Detect conflicting local edits at update time and require an explicit choice.
- [x] Local author packages retain their live-refresh behavior, not remote replacement.

## 4. Independently refreshed official catalog

- [x] Refresh official repository discovery without reinstalling the engine.
- [x] Separate catalog discovery, available releases and project-installed versions.
- [x] Migrate old official monorepo source descriptors to the independent repositories.
- [x] Refreshing discovery does not upgrade or modify installed package content.
- [x] Already installed packages remain usable without network access.

## 5. Delivery and acceptance

- [ ] Update affected engine/Hub/platform repositories and public release artifacts.
- [ ] Verify source-free exports with the InfernuxMultiPlatform040 project.
- [ ] Verify its button reads packaged TXT content and displays it in game UI.
- [ ] Keep cooked assets in binary packages; do not expose an editable Assets/Library tree.
- [ ] Verify install, update, uninstall and centralized storage ownership.
- [ ] Keep documentation consistent with the actual released payloads.
- [ ] Required PR checks are green and the PR is mergeable.

The user's pause on additional-machine acceptance remains in effect. Record
unavailable device coverage explicitly; do not substitute mocked checks for
completed physical-device acceptance or mark untested work as passed.

## Implementation rules

- Reuse InxPackage, existing runtime archives, registry locks, storage ownership
  and installation transactions. Do not introduce parallel packaging formats.
- Compilation belongs to engine/plugin release engineering, never ordinary
  consumer installation or game export.
- CMake release targets publish final payloads directly into their owning plugin
  subrepositories; build caches stay in `out`. Delivery must not depend on manual
  post-build copying. Local development commands run after `conda activate infernux`.
- Do not wrap or transfer plugin payloads in intermediate ZIP archives. Plugin
  release CI runs CMake in the owning checkout and publishes the final `.inxpkg`.
- Apply necessary binary compatibility, download integrity and user-data protection
  checks at their boundaries, not repeated hashing or speculative fallback chains.
- Keep release versions immutable and never silently overwrite local author work.

## Starting baseline

The four official platform repositories and v0.1.0 exporter releases exist;
their packaging workflows pass and Release acquisition works. They do not yet
provide the complete precompiled platform payloads required by this gate.
The installed editor's official catalog is currently a wheel-bundled snapshot.
Installed plugins have reload/uninstall actions but no version update operation.

## Iteration log

### 2026-09-06: verify Hub kits before channel publication

- Hub kit builds can verify the selected source commit without an existing
  public release. Changes to kit inputs trigger that build; an explicit existing
  release tag remains necessary for channel publication. Both host `.inxkit`
  outputs are retained as CI artifacts, and published release assets are not
  silently overwritten.
- Executed the PowerShell resolver for both verification and publication modes.
  Kit/release regression: 20 passed, 2 skipped. Complete Hub regression before
  this workflow change: 317 passed, 3 skipped. Real dual-host kit builds and
  channel download/install acceptance remain open.
- Cold CI preparation exposed the runner's default JDK 11; selected JDK 17 and
  Python 3.13 before SDK setup. The next cold NumPy build exposed missing Android
  libpython linkage. Added explicit C/C++ `-lpython3.13` cross-link arguments.
  Actual arm64 and x86_64 NDK link probes succeeded with undefined-symbol checks
  enabled and both recorded `DT_NEEDED=libpython3.13.so`. Full cold kit CI is still
  the required validation, not replaced by those probes.

### 2026-09-06: public Windows package consumer acceptance

- Downloaded actual public Windows v0.2.0 through the installed engine's GitHub
  release resolver, updated the isolated MultiPlatform040 project, and retained
  all ten asset GUIDs and its enabled state.
- Installed-only Release export passed in 36.753 seconds. A separate development
  export then passed the authenticated Player control/button test: `UIText.text`
  equaled `BUTTON READ #1: Package resource reached UIText on every Player target.`
  Fatal count was zero. Evidence: `out/acceptance/windows-public020-*.json`.
- Verified uninstall/reinstall of that public package preserves a user-added
  note and restores the same GUIDs. The shared archive remains under the Hub's
  plugin library; project staging and transaction directories are empty.
- Complete engine regression after the cook-host fix: 5,341 passed, 11 skipped.

### 2026-09-06: installed Web export and cook-host ownership

- Completed Windows shader tools through the plugin CMake target: glslang
  2,230,272 bytes and Tint 6,111,744 bytes. Abseil now uses the same static MSVC
  runtime as Tint, fixing the actual Windows linker mismatch.
- Built and installed the complete local Web v0.2.0 `.inxpkg`, with the generic
  browser runtime and both host toolsets. The isolated MultiPlatform040 copy had
  an obsolete development-format v0.1 archive; uninstall/reinstall there does not
  constitute public v0.1-to-v0.2 update acceptance or modify the original project.
- Installed-only Web export passed in 27.685 seconds. Both Python and native
  engine origins are under Conda site-packages; no engine compilation occurred.
  Output contains the immutable runtime plus an 18,325,376-byte project `.inxpkg`,
  not loose Assets/Library directories. Local Edge WebGPU software-adapter
  acceptance subsequently rendered the scene and clicked READ PACKAGE TXT:
  `BUTTON READ #1` displayed the expected resource, with no script/page errors.
  Evidence: `out/acceptance/web-installed040-{build,button}.json` and the button
  screenshot. This is a local browser run, not physical Android acceptance.
- That real export exposed a temporary cook host shutting down the caller's
  plugin manager. Engine exit now unloads only its own manager. Eight focused
  shutdown/preflight tests passed. Rebuilt and installed the updated wheel.
- Full Python regression before that ownership fix: 5,339 passed, 11 skipped.
  Also fixed explicit UTF-8 reads in the CMake workflow tests: Windows CI's
  locale-dependent decoding had failed on workflow comments. Six tests passed.
- Web release CI independently reproduced the MSVC runtime mismatch and caught
  clang-format splitting JavaScript strict-equality tokens inside EM_JS. Protected
  the JavaScript bodies from C++ formatting and rebuilt the native runtime locally.
  The public Web release still awaits the corrected remote build.

### 2026-09-06: complete public Linux payload and Web runtime separation

- Published Linux v0.2.0 with a 152,677,056-byte `.inxpkg` and its compatible-release
  JSON. Downloaded the actual public package through engine release acquisition
  and read its payload. This is publication/acquisition evidence, not a new Linux
  physical-machine run.
- Plugin HTTP operations now use a 30-second socket timeout, without additional
  retries or source fallbacks. Failed downloads retain the previous complete file
  and discard partial output. Focused package/release regression: 150 passed,
  5 skipped; the complete public Linux download also succeeded.
- Complete Windows and Android plugin build workflows passed at `2fa0dae` and
  `5e1e8d6`. Their v0.2.0 public release workflows then passed and uploaded
  complete `.inxpkg` assets: Windows 85,485,120 bytes; Android 883,289,792 bytes.
  Both releases also contain their compatible-release JSON and are not drafts.
- Main PR Android acceptance was blocked before APK assembly by setup-gradle
  validating an unused SDL example wrapper. Exports invoke the pinned Gradle 8.12
  distribution directly; disabled the unrelated wrapper scan, not distribution
  integrity verification. Workflow regression: 20 passed, 2 skipped.
- Moved Web C++/CMake/bootstrap sources out of the installable package. Removed
  compiler, WSL and source-acquisition calls from normal export. A separate content
  `.inxpkg` is loaded into browser memory before Python starts; presentation
  settings no longer require native compile definitions.
- Built the generic Release Web runtime and published it directly through its
  owning CMake target: JS 191,822 bytes, WASM 13,684,652 bytes and CPython data
  3,577,002 bytes. Web/Player bootstrap tests: 45 passed; standalone release tests:
  4 passed. Native shader-tool builds, installed browser acceptance and the
  complete Web public release remain open.
- Linux shader-tool publication completed (glslang 3,145,112 bytes; Tint
  9,086,064 bytes). Windows native compilation is still running. Local Windows
  access to chromium.googlesource.com failed, and the upstream fetch script hid
  the failures; the maintainer helper now refuses to cache incomplete dependencies.
  For local compilation, explicitly selected the already populated WSL source
  directory as the MSVC source input. No consumer WSL path was reintroduced.
- The Windows PR failures exposed a separate staging issue: copying SwiftShader
  beside development modules did not include it in the CMake-installed Python
  payload used by the new precompiled Player publisher. Added an opt-in CI-only
  CMake install hook for that software driver. Public platform releases do not
  select the hook. The resulting Player/desktop CI rerun is still required.

### 2026-09-06: complete Android payload and compiler-free APK assembly

- Removed native source acquisition, consumer CMake configuration, pybind11
  installation and engine compilation from the Android exporter. It now stages
  the plugin's precompiled libraries and SDL Java host, then invokes Gradle.
  The native host source and build entry live outside the distributable package.
- Cross-built Release payloads for arm64-v8a and x86_64. The owning CMake target
  produced the complete 0.2.0 `.inxpkg` (881,401,088 bytes) and release JSON in the
  Android subrepository. Both ELF ABIs and the engine/Python contract were checked;
  no intermediate ZIP archive was used. Public release publication is still pending.
- Source-mode MultiPlatform040 APK assembly passed in 118.8 seconds without any
  CMake or native compilation task. The APK audit found 17 native libraries and
  no forbidden distributions. This is build evidence, not physical-device execution.
- Installed the complete plugin in the isolated acceptance project. Its older
  development-format archive required uninstall/fresh installation in that copy;
  the original desktop project and its cached archive were left unchanged.
- Android/export-release regressions: 67 passed, 1 skipped; standalone Android
  packaging: 5 passed. Build workflow/ownership checks: 25 passed, 2 skipped.
- Fixed the installed-only acceptance entry to bind project paths and synchronize
  installed resources before plugin preloads, matching Editor startup order;
  all 12 CLI tests pass. Installed-only Android export passed in 28.4 seconds:
  both engine origins were under site-packages, the native payload was Release,
  and the resulting APK was 52,276,976 bytes with a passing package audit.
  This run reused the previously validated Gradle cache. A separate cold-cache
  attempt failed on a Maven TLS handshake, so fresh Hub first-build acceptance
  is not yet claimed. The corrected entry's TXT preload probe also passed.
- Pushed Android implementation `d86d173` and engine integration `883b0239`.
  Dispatched the complete Android release build and tagged Linux v0.2.0 after
  its complete workflow passed; public artifact completion remains pending.
- Full local Python regression: 5,331 passed, 11 skipped. Lifecycle-only package
  tests now omit generated Player payloads from their test copies; the focused
  release-assets suite passes in 13.3 seconds (13 passed, 1 skipped), while actual
  complete-package and installed-build acceptance retain their native payloads.
- Android release CI now selects JDK 17 before SDK setup instead of using the
  Ubuntu runner's default JDK 11. Windows runtime installation uses the vendor's
  `/auto` switch (confirmed in the pinned installer's argument parser) with a
  five-minute step limit. These installer checks run
  only in CI; no local Vulkan installation is part of acceptance.
- The complete Linux plugin workflow passed. Main PR Linux desktop/Player,
  Android Player and Web Player/browser checks passed at `bc5cdfd6`; both Windows
  jobs failed because the staged publisher lacked the system Vulkan loader.
  Added an explicit Vulkan Runtime install independent of the SDK cache; rerun
  results and the public Windows release are still pending.

### 2026-09-06: desktop publication and Android native ownership

- Pushed the desktop implementation to the engine and both platform repositories.
  Dispatched complete Windows/Linux plugin builds in their own release workflows;
  their outcome and public release publication remain pending.
- Added the Android plugin's outer `native/CMakeLists.txt` to engine CMake. Its
  `prebuild_android_player` target builds the host against the same SDL target as
  the engine and publishes libraries and SDL Java sources directly into the
  plugin's `package/editor/infernux_android/player/` directory, without ZIP transfer.
- Cross-built the arm64-v8a RelWithDebInfo payload locally using the existing NDK
  cache. The generated host is ELF64 AArch64, linked to SDL3 and CPython 3.13.
  This is native publication evidence, not an installed APK or complete plugin
  release. Android consumer migration and the x86_64 payload remain outstanding.
- Moved Android orientation from a C++ compile-time constant to authored Android
  manifest metadata passed by the Java activity. One precompiled host can now
  apply different project orientation policies. All 54 Android exporter tests pass.
- Installed-only update acceptance found generated file observations being
  mistaken for author edits. Update comparison now ignores the default loaders'
  derived file observations while retaining GUID and authored-setting protection.
  An isolated MultiPlatform040 copy downgraded to the actual public 0.1.0 archive
  and upgraded to the locally built complete 0.2.0 archive. Six shared member GUIDs
  survived the downgrade, all ten survived the upgrade, and disabled state and a
  user-added file were preserved. Exact-tag download did not change the pin.
  The fixture was re-enabled afterward. This covers the current archive format,
  not the development-era archive-layout migration noted below.

### 2026-09-06: CMake-owned desktop plugin payloads (in progress)

- Desktop game assembly now selects its platform plugin's precompiled Player;
  the Editor wheel no longer carries Player archives or the native PlayerHost.
- `windows-msvc-player` and `linux-clang-player` publish directly into their
  respective subrepository's `package/editor/infernux_*/player/`. Release workflow
  presets and desktop acceptance CI invoke these CMake targets. Compiler caches
  remain build-tree-owned; no manual copy is part of publication. The same CMake
  target writes the complete `.inxpkg` and release manifest into the subrepository's
  `dist/`. There is no intermediate ZIP or runtime-archive transfer channel.
- Removed the duplicate source-Editor PlayerHost copy rule and its publisher
  resource fallback. CMake supplies the exact native target to the publisher.
- Windows CMake publication completed in the active `infernux` Conda environment:
  41,252,672-byte base archive and 42,994,432-byte parallel module. The 0.4.0
  Editor wheel is built separately. This is local payload verification, not yet
  a complete public plugin release or source-free game acceptance.
- Android/Web payload publication and public complete releases remain open.

### 2026-09-06: Windows installed-only export acceptance

- Built and inspected the complete 0.2.0 Windows `.inxpkg` (84,742,784 bytes):
  native host, base Player and parallel module are present inside the plugin.
- Fixed the lowercase public `infernux` import being misclassified as a user
  dependency, which had overwritten the selected precompiled engine with the
  builder environment's package. Both public engine names are now engine-owned.
- Source-mode MultiPlatform040 export passed the complete package audit in 13.7
  seconds. Installed-only export of an isolated copy passed in 19.9 seconds;
  both Python and native engine origins were verified under Conda site-packages.
- Launched the installed-only Player and injected a real mouse down/up on the
  package TXT button. UIText returned `BUTTON READ #1: Package resource reached
  UIText on every Player target.`; capture passed and fatal log count was zero.
- The old project's cached development-era archive has an unsupported InxPack
  record layout. The isolated test copy used uninstall/fresh installation; the
  original project was not modified. This does not prove migration of that old
  archive format or replace the current-format update acceptance requirement.
- Full Python regression after the desktop delivery changes: 5,328 passed and
  11 skipped. This is local regression evidence, not a substitute for public
  release publication, remote PR checks or untested platform acceptance.

### 2026-09-06: consumer/compiler boundary and release discovery

- Removed desktop consumer Player compilation on a prebuilt runtime miss;
  explicit release-engineering runtime compilation remains supported.
- Added metadata-only compatible release discovery, release notes, paginated
  version listing and exact tag selection without substitution.
- Preserved GitHub provenance when importing a cached archive. Reading available
  versions leaves the project installation and version pin unchanged.
- Verified real metadata discovery against all four public plugin repositories.
- Full GameBuilder tests: 258 passed, 1 skipped. Public namespace, release discovery,
  plugin management and release packaging tests: 171 passed, 6 skipped.

These are intermediate changes. Platform payload migration, update publication/UI,
catalog refresh, complete release artifacts and final acceptance remain open.

### 2026-09-06: explicit in-place plugin updates

- Added the installed package Versions tab, compatible version selection, release
  notes, background discovery/download and explicit local-edit consent.
- Reused the existing installation transaction for updates; no uninstall/reinstall
  sequence, new archive format or continuous content verification was introduced.
- Preserved GUIDs, user-moved assets, enabled state, selected members, local added
  files and customized importer settings. Publisher renames follow the new layout
  when the user has not moved the corresponding asset.
- Shared assets cannot be forcibly overwritten. Removed shared assets transfer
  ownership to a remaining package; dropped Python requirements relinquish only
  the updating package's ownership.
- Tested failed publication restoration, derived bytecode cleanup, exact-tag
  staging without pin changes, and real binary archives with the shared cache.
- Broad plugin/UI/public-namespace regression: 319 passed, 6 platform skips.
  Follow-up update and dependency-conflict coverage: 160 passed, 5 skips.
  Windows, Android and Web package lifecycle upgrade/downgrade tests passed on
  Windows; the Linux host lifecycle case was skipped on this host.
- Updated English/Chinese package documentation; strict Wiki build passed.

Section 3 implementation is present. Interactive editor acceptance and the final
source-free platform deliveries are still part of section 5; this is not a claim
that the overall goal or public runtime releases are complete.

### 2026-09-06: independently published official discovery

- Added an explicit background Refresh catalog action, reusing the existing
  progress service and catalog JSON format. The shared Hub plugin library retains
  the downloaded catalog; startup remains offline and uses the wheel only to seed
  a library that has never been refreshed.
- Published the initial catalog channel on `codex/plugin-catalog` and added a
  metadata-only publishing workflow. The engine default branch still serves the
  incompatible 0.3.7 catalog until this PR is merged; it is not a runtime fallback.
- Old official platform repository URLs resolve to the independent repositories
  without rewriting installed version locks. Official bundled packages can find
  their publisher; local author/fork catalog overrides remain authoritative.
- Registry-based dependency installation reuses installed versions rather than
  acquiring a newly discovered version implicitly.
- Real channel refresh returned MCP plus the four platform repositories, preserved
  all three existing acceptance-project installations, and reused all five entries
  without the wheel catalog. Eleven focused refresh/migration tests passed.
- Broader plugin, UI, namespace and localization regression: 331 passed, 6 skips.
