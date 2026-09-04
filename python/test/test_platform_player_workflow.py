from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "platform-player.yml"
ANDROID_DRIVER = ROOT / "scripts" / "acceptance" / "android_emulator_ci.sh"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _android_driver_text() -> str:
    return ANDROID_DRIVER.read_text(encoding="utf-8")


def test_platform_workflow_reuses_repository_build_and_acceptance_entry_points():
    text = _text() + "\n" + _android_driver_text()

    assert "scripts/acceptance/build_player.py" in text
    assert "scripts\\acceptance\\windows_player_smoke.py" in text
    assert "scripts/acceptance/linux_player_smoke.py" in text
    assert "scripts/acceptance/web_mobile_input_smoke.cjs" in text
    assert "scripts/acceptance/android_player_smoke.py" in text
    assert "scripts/acceptance/android_multitouch_smoke.py" in text
    assert "scripts/setup/build_web_toolchain.sh" in text
    assert "scripts/setup/build_android_python_runtime.sh" in text


def test_android_runtime_builder_creates_build_python_before_cross_build():
    script = (
        ROOT / "scripts" / "setup" / "build_android_python_runtime.sh"
    ).read_text(encoding="utf-8")

    build_python = '\"$python_source/Android/android.py\" build build'
    cross_python = '\"$python_source/Android/android.py\" build \"$host\"'
    assert script.index(build_python) < script.index(cross_python)


def test_linux_setup_installs_required_shader_reflection_dependency():
    script = (
        ROOT / "scripts" / "setup" / "install_linux_dependencies.sh"
    ).read_text(encoding="utf-8")
    native_targets = (ROOT / "cmake" / "InfernuxNativeTargets.cmake").read_text(
        encoding="utf-8"
    )

    assert "libspirv-cross-c-shared-dev" in script
    assert "SPIRV-Cross libraries are required" in native_targets


def test_windows_native_build_can_load_the_vulkan_linked_module():
    text = _text()
    loader_step = text.index(
        "- name: Provide pinned software Vulkan to the build and Player closure"
    )
    build_step = text.index("- name: Build Windows Player runtime")

    assert loader_step < build_step
    assert 'Copy-Item -LiteralPath $softwareVulkan -Destination "python\\Infernux\\lib\\vulkan-1.dll"' in text[loader_step:build_step]
    assert (
        'Copy-Item -LiteralPath $softwareVulkan -Destination '
        '"out\\build\\windows-msvc-release\\Release\\vulkan-1.dll"'
        in text[loader_step:build_step]
    )
    assert "runtime\\vk_swiftshader_icd.json" in text[loader_step:build_step]
    assert "VK_DRIVER_FILES=$swiftShaderManifest" in text[loader_step:build_step]


def test_platform_workflow_keeps_product_graphics_contracts_explicit():
    text = (_text() + "\n" + _android_driver_text()).casefold()

    assert "web-wasm32" in text
    assert "android-x64-emulator" in text
    assert "api-level: 36" in text
    assert "arch: x86_64" in text
    assert '"ndk;27.3.13750724"' in text
    assert '"ndk;29.0.14206865"' in text
    assert "opengl" not in text
    assert "gles" not in text


def test_platform_workflow_is_bounded_and_collects_evidence():
    text = _text()
    product_commands = text + "\n" + _android_driver_text()

    assert text.count("timeout-minutes: 120") == 4
    assert text.count("if: always()") == 4
    assert "windows-player-build.json" in text
    assert "windows-player-smoke.json" in text
    assert "linux-player-build.json" in text
    assert "linux-player-smoke.json" in text
    assert "android-player-build.json" in product_commands
    assert "android-player-smoke.json" in product_commands
    assert "android-multitouch-smoke.json" in product_commands
    assert "web-player-build.json" in text
    assert "web-player-smoke.json" in text
    assert "trap cleanup EXIT" in text
    assert "set -o pipefail" in text


def test_desktop_player_jobs_use_real_input_physics_and_line_renderer_smoke():
    text = _text()

    assert "windows-player:" in text
    assert "linux-player:" in text
    assert '--object "Render Probe"' in text
    assert text.count("minimum-axis-delta 0.5") == 2
    assert text.count("minimum-final-y 0.0") == 2
    assert text.count('"component_type":"LineRenderer"') == 2
    assert "lvp_icd.x86_64.json" in text
    assert "--validation" in text
    assert "opengl" not in text.casefold()
    assert "gles" not in text.casefold()


def test_android_emulator_action_is_immutable_and_app_cleanup_is_default():
    text = _text()
    driver = _android_driver_text()
    smoke = (ROOT / "scripts" / "acceptance" / "android_player_smoke.py").read_text(
        encoding="utf-8"
    )

    assert (
        "ReactiveCircus/android-emulator-runner@"
        "a421e43855164a8197daf9d8d40fe71c6996bb0d" in text
    )
    emulator_step = text.index("ReactiveCircus/android-emulator-runner@")
    emulator_script = text.index("script: |", emulator_step)
    emulator_body = text[emulator_script : text.index("- name: Upload Android Player evidence", emulator_script)]
    assert emulator_body.count("bash scripts/acceptance/android_emulator_ci.sh") == 1
    assert '"$CONDA/envs/infernux/bin/python"' in emulator_body
    assert "build_player.py" not in emulator_body
    assert '"--keep-running"' in smoke
    assert '"--require-log"' in smoke
    assert '"shell", "am", "force-stop", arguments.package' in smoke
    assert "tests/android/input_instrumentation" in driver


def test_android_emulator_driver_owns_the_full_single_shell_workflow():
    driver = _android_driver_text()

    assert "set -euo pipefail" in driver
    assert '"$python_executable" scripts/acceptance/build_player.py' in driver
    assert "android-x64-emulator" in driver
    assert "gradle -p tests/android/input_instrumentation" in driver
    smoke = driver.index('"$python_executable" scripts/acceptance/android_player_smoke.py')
    assert "locksettings set-disabled true" in driver
    assert "svc power stayon true" in driver
    assert driver.index("KEYCODE_WAKEUP") < smoke
    assert driver.index("wm dismiss-keyguard") < smoke
    assert driver.index("KEYCODE_HOME") < smoke
    assert '"$python_executable" scripts/acceptance/android_player_smoke.py' in driver
    assert "--startup-timeout 240" in driver
    assert '"$python_executable" scripts/acceptance/android_multitouch_smoke.py' in driver
    assert "-PinfernuxTargetPackage=com.infernux.bootstrap" in driver
    assert "--wait-milliseconds 20000" in driver


def test_android_builds_host_modules_before_cross_platform_packaging():
    workflow = _text()
    android_job = workflow[workflow.index("  android-player:") :]

    install_host = android_job.index("- name: Install Linux host dependencies")
    build_host = android_job.index("- name: Build Linux host modules")
    launch_emulator = android_job.index("ReactiveCircus/android-emulator-runner@")
    assert install_host < build_host < launch_emulator
    assert "scripts/setup/install_linux_dependencies.sh" in android_job
    assert "cmake --preset linux-clang-headless" in android_job
    assert "cmake --build --preset linux-clang-headless" in android_job
    assert "out/build/linux-clang-headless" in android_job


def test_headless_audio_device_absence_is_a_nonfatal_compatibility_boundary():
    audio_engine = (
        ROOT / "cpp" / "infernux" / "function" / "audio" / "AudioEngine.cpp"
    ).read_text(encoding="utf-8")
    open_failure = audio_engine[
        audio_engine.index("if (m_deviceId == 0)") : audio_engine.index(
            "SDL_AudioSpec actualSpec"
        )
    ]
    assert "INXLOG_WARN" in open_failure
    assert "continuing silently" in open_failure
    assert "INXLOG_ERROR" not in open_failure


def test_web_smoke_can_attach_to_a_physical_mobile_browser():
    smoke = (ROOT / "scripts" / "acceptance" / "web_mobile_input_smoke.cjs").read_text(
        encoding="utf-8"
    )
    workflow = _text()

    assert '"--cdp-endpoint"' in smoke
    assert "chromium.connectOverCDP" in smoke
    assert 'session.send("Input.dispatchTouchEvent"' in smoke
    assert 'process.argv.includes("--movement-touch")' in smoke
    assert 'process.argv.includes("--verify-native-multitouch")' in smoke
    assert 'process.argv.includes("--verify-mobile-ime")' in smoke
    assert "--verify-mobile-ime requires a physical browser" in smoke
    assert 'type: "touchStart"' in smoke
    assert 'type: "touchMove"' in smoke
    assert 'type: "touchCancel"' in smoke
    assert "started.count === 2" in smoke
    assert "movedDistances.every" in smoke
    assert 'item.includes("phase=canceled")' in smoke
    assert "const canceledIdsObserved = startedIds.every" in smoke
    assert "cleared.count === 0" in smoke
    assert "const before = await waitForNoUnityTouches()" in smoke
    assert 'imeSession.send("Input.insertText"' in smoke
    assert 'item.includes("BALANCE // TEXT COMMIT")' in smoke
    assert "visible.visualViewportHeight < visible.innerHeight - 1" in smoke
    assert "const movementTouchGeometry = movementTouch" in smoke
    assert "viewportLeft + pixels(safe.paddingLeft) - rect.left" in smoke
    assert "result.nativeMultitouch" in smoke
    assert "result.mobileIme" in smoke
    assert 'process.argv.includes("--skip-frame-checks")' in smoke
    assert 'argumentValues("--require-diagnostic")' in smoke
    assert 'argumentValue("--report")' in smoke
    assert "writeJsonAtomic(reportPath" in smoke
    assert 'argumentValue("--capture-frame-output")' in smoke
    assert "result.captureFramePath" in smoke
    assert "requiredDiagnostics: Object.fromEntries" in smoke
    assert "Object.values(result.requiredDiagnostics)" in smoke
    assert '"AudioSource::StartVoice: AudioEngine not initialized"' in smoke
    assert "Object.values(result.forbiddenDiagnostics)" in smoke
    assert '"touch:left-zone-forward"' in smoke
    assert 'dataset.infernuxState === "ready"' in smoke
    assert "awaiting-user-activation" not in smoke
    assert "--verify-native-multitouch" in workflow


def test_web_smoke_does_not_force_an_unavailable_linux_vulkan_adapter():
    smoke = (ROOT / "scripts" / "acceptance" / "web_mobile_input_smoke.cjs").read_text(
        encoding="utf-8"
    )

    assert '"--enable-unsafe-webgpu"' in smoke
    assert '"--use-angle=vulkan"' not in smoke
    assert '"--enable-features=Vulkan"' not in smoke
    assert '"--disable-vulkan-surface"' not in smoke


def test_windows_smoke_can_capture_the_engine_game_render_target():
    smoke = (ROOT / "scripts" / "acceptance" / "windows_player_smoke.py").read_text(
        encoding="utf-8"
    )

    assert '"--capture-file"' in smoke
    assert 'control.call(\n                "capture"' in smoke
    assert 'capture.get("output_path"' in smoke


def test_web_smoke_rejects_black_or_flat_frames_after_input():
    acceptance = ROOT / "scripts" / "acceptance"
    smoke = (acceptance / "web_mobile_input_smoke.cjs").read_text(encoding="utf-8")
    package = (acceptance / "package.json").read_text(encoding="utf-8")

    assert 'const { PNG } = require("pngjs")' in smoke
    assert "frameAfterActivation" in smoke
    assert "frameAfterInput" in smoke
    assert "shadowDifference" in smoke
    assert "skyDifference" in smoke
    assert "InfernuxWebSetRenderDiagnostic" in smoke
    assert 'process.argv.includes("--verify-particle-bloom")' in smoke
    assert "withBloomContribution" in smoke
    assert "withoutBloomContribution" in smoke
    assert "haloPixelRatio" in smoke
    assert "particle-with-bloom" in smoke
    assert "no-particle-no-bloom" in smoke
    assert "diagnosticTail" in smoke
    assert "requiredDiagnosticMatches" in smoke
    assert "requiredDiagnosticOrderResults" in smoke
    assert "forbiddenDiagnosticMatches" in smoke
    assert "Object.values(forbiddenDiagnosticMatches)" in smoke
    assert "frameIsVisible" in smoke
    assert "inputPreservedFrame" in smoke
    assert '"pngjs": "7.0.0"' in package


def test_web_deterministic_capture_rejects_browser_resampling():
    smoke = (ROOT / "scripts" / "acceptance" / "web_mobile_input_smoke.cjs").read_text(
        encoding="utf-8"
    )

    assert 'deterministicCapture ? "1" : "2"' in smoke
    assert "deterministic capture viewport must exactly match" in smoke
    assert "deterministic capture requires --device-scale-factor 1" in smoke
    assert 'phase: "deterministic-capture-layout"' in smoke
    assert 'phase: "deterministic-capture-pixels"' in smoke
    assert "frame.width !== expectedRenderWidth" in smoke
