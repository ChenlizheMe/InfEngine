# Critical-workflow regression tests

This is the maintained regression map for contributors, not a claim of complete
coverage. A behavior change in a row below must update its owning regression
tests. Keep this map current when moving tests or changing prerequisites.
Prefer a focused reproduction over another production fallback or a coverage target.

Run commands from the repository root with `conda activate infernux` (Python
3.13). Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development setup.

## Workflow owners

All paths below are pytest modules. Run one with `python -m pytest PATH -q -ra`.

| Critical workflow | Owning regression modules | Execution lane |
| --- | --- | --- |
| Hub project creation, validation and Python binding | `packaging/tests/test_hub_project_workflow.py`, `packaging/tests/test_project_python_runtime.py`, `packaging/tests/test_hub_new_project_python_binding.py` | Portable Hub; full desktop CI |
| Hub launch readiness, errors and repeated launch | `packaging/tests/test_hub_launch_state.py`, `packaging/tests/test_project_runtime_strictness.py` | Portable Hub; full desktop CI |
| Host-specific wheel delivery and refresh | `packaging/tests/test_hub_release.py`, `packaging/tests/test_project_wheel_refresh.py` | Full Hub CI; real download check before release |
| Scene activation, defaults and Play/Stop transitions | `python/test/test_scene_manager_runtime_loading.py`, `python/test/test_scene_manager_defaults.py`, `python/test/test_engine_play_mode.py` | Native Python; visible editor acceptance |
| Script refresh and transactional publication | `python/test/test_play_mode_component_body_reload.py`, `python/test/test_plugin_updates.py` | Native Python |
| Asset loading, persistence and material state | `python/test/test_integration_asset_database.py`, `python/test/test_asset_persistence_races.py`, `python/test/test_material_render_state_authorship.py` | Native Python; visual acceptance |
| Particle compilation and capacity behavior | `python/test/test_particle_graph_hir.py`, `python/test/test_particle_kernel_ir.py`, `python/test/test_particle_gpu_glsl_backend.py`, `python/test/test_particle_spawn_schedule.py` | Native Python; GPU rendering acceptance |
| Game export, dependency closure and sealed content | `python/test/test_game_builder_asset_closure.py`, `python/test/test_player_build_preflight.py`, `python/test/test_desktop_build_exporter.py`, `python/test/test_web_exporter_plugin.py`, `python/test/test_multiplatform_player_fixture.py` | Native Python; four-target Player CI |
| Plugin import, catalog, updates and documentation | `python/test/test_plugin_catalog_refresh.py`, `python/test/test_plugin_updates.py`, `python/test/test_plugin_panel_content.py` | Native Python |

## Portable Hub lane: no compiled engine or Vulkan required

The **Portable Hub regressions** job in `.github/workflows/ci.yml` runs on both
Windows and Linux, before and independently of any native build. Its exact local
equivalent is:

```sh
python -m pip install pytest PySide6 packaging
python -m pytest packaging/tests/test_hub_project_workflow.py packaging/tests/test_project_python_runtime.py packaging/tests/test_hub_new_project_python_binding.py packaging/tests/test_hub_launch_state.py packaging/tests/test_project_runtime_strictness.py packaging/tests/test_regression_guide.py -q -ra
```

For a session without a display, set `QT_QPA_PLATFORM=offscreen` first
(`$env:QT_QPA_PLATFORM = 'offscreen'` in PowerShell,
`export QT_QPA_PLATFORM=offscreen` in Bash). These tests use temporary projects and
controlled launch workers; they do not download an engine or launch a real Editor.
No skips are expected in this selected lane on Windows x64 or Linux x64.
`test_regression_guide.py` checks that mapped modules exist and the documented
portable command stays synchronized with CI.

The broader Hub suite is `python -m pytest packaging/tests -q -ra`.
Platform-specific UI/installer tests may skip on the other OS; `-ra` prints each
reason. A passing mocked download test is not evidence of network availability.

## Native Python and C++ lanes

Every test under `python/test` loads the real backend through `conftest.py` and
initializes SDL, Vulkan, physics and lifecycle state. Even tests with apparently
pure-Python names are **not** a portable/no-GPU subset. Missing native modules,
shader tools, or a Vulkan device are setup failures, not reasons to skip the suite.

Windows:

```powershell
cmake --preset windows-msvc-release
cmake --build --preset windows-msvc-release
cmake --build --preset windows-msvc-player
ctest --preset windows-msvc-release -LE "performance|requires_vulkan_device" --output-on-failure
python -m pytest python/test -q -ra
```

Linux (install dependencies using the setup guide first):

```sh
cmake --preset linux-clang-release
cmake --build --preset linux-clang-release
cmake --build --preset linux-clang-player
ctest --preset linux-clang-release -LE 'performance|requires_vulkan_device' --output-on-failure
xvfb-run --auto-servernum python -m pytest python/test -q -ra
```

The **Desktop Release And Tests** workflow runs these suites and uploads JUnit
results, then builds wheels and Hub distributions. Linux CI uses Mesa Lavapipe
with Xvfb; Windows CI supplies a pinned SwiftShader runtime. These are test-device
configurations, not shipped renderer fallbacks. Local developers can use their
normal Vulkan driver. Do not copy CI software-driver binaries into release wheels.

CTest excludes `performance` and `requires_vulkan_device` in hosted CI. Maintainers
with a suitable Vulkan device run `ctest --preset windows-msvc-release -L
requires_vulkan_device --output-on-failure` (or the Linux preset). Performance
tests run separately with `-L performance` on a stable machine. Record GPU/driver,
source commit and configuration; do not compare timings across unrelated hosts.
Inspect `ctest --preset windows-msvc-release -N` and pytest's `-ra` output when
coverage or skips change. OS-specific skips must match the actual host; newly
skipped changed behavior requires investigation and must be called out in the PR.

## Maintainer acceptance beyond unit tests

The **Platform Player Acceptance** workflow (`.github/workflows/platform-player.yml`)
defines the exact target setup and commands. Its maintained project is
[`tests/fixtures/multiplatform_player`](tests/fixtures/multiplatform_player/README.md),
not a developer's desktop project. Work on a copy under `out/`, never export into
the tracked fixture. Example on Windows:

```powershell
New-Item -ItemType Directory out/ci-projects -Force
Copy-Item tests/fixtures/multiplatform_player out/ci-projects/regression -Recurse
python scripts/acceptance/editor_project_smoke.py out/ci-projects/regression --scene Assets/Scenes/Main.scene
python scripts/acceptance/build_player.py out/ci-projects/regression windows-x64 out/acceptance/regression-player --report out/acceptance/regression-build.json
```

Use a fresh copy name if the destination exists. The editor harness exercises
startup, scene readiness and Play/Stop; additionally inspect the initial Scene
and Game images and gizmo icons before clicking anything. A control-plane-ready
marker alone cannot prove that a camera rendered correctly.

| Target | Maintained runtime harness | Extra prerequisites / acceptance |
| --- | --- | --- |
| Windows | `scripts/acceptance/windows_player_smoke.py` | Built EXE, desktop session and Vulkan; input changes position, rendered capture and clean shutdown |
| Linux | `scripts/acceptance/linux_player_smoke.py` | Built executable, Vulkan and display/Xvfb; same gameplay and render checks |
| Web | `scripts/acceptance/web_mobile_input_smoke.cjs` | Serve the Web build over localhost, install `scripts/acceptance` npm dependencies and Playwright browser; WebGPU, pixels, input and startup markers |
| Android | `scripts/acceptance/android_emulator_ci.sh`, `scripts/acceptance/android_player_smoke.py` | Hub Android support/toolchain, APK and selected ADB device; CI covers an emulator, not every physical GPU |

Run each harness with `--help` for its artifact paths and options; use the workflow
invocations for the fixture's object names and assertions. Android emulator
setup uses `bash scripts/acceptance/android_emulator_ci.sh "$CONDA_PREFIX/bin/python"
build` followed by the workflow's emulator and `smoke` stage. Physical-device
acceptance remains explicit: record model/API/driver, never equate emulator
success with that device passing.

Before release, also download the selected wheel through Hub on each actual host,
install it into a clean runtime, and build with installed platform plugins using
`build_player.py --installed`. Check that package TXT/JSON reads reach UI, sibling
resource references survive export, and the distribution does not expose loose
`Assets` or `Library` directories. Capture report paths, exact asset names and
source commits. Missing devices or unavailable network services are unverified
coverage, not successful acceptance.
