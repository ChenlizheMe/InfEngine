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
