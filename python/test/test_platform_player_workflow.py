from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "platform-player.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_platform_workflow_reuses_repository_build_and_acceptance_entry_points():
    text = _text()

    assert "scripts/acceptance/build_player.py" in text
    assert "scripts/acceptance/web_mobile_input_smoke.cjs" in text
    assert "scripts/acceptance/android_player_smoke.py" in text
    assert "scripts/setup/build_web_toolchain.sh" in text
    assert "scripts/setup/build_android_python_runtime.sh" in text


def test_platform_workflow_keeps_product_graphics_contracts_explicit():
    text = _text().casefold()

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

    assert text.count("timeout-minutes: 120") == 2
    assert text.count("if: always()") == 2
    assert "android-player-build.json" in text
    assert "android-player-smoke.json" in text
    assert "web-player-build.json" in text
    assert "web-player-smoke.json" in text
    assert "trap cleanup EXIT" in text
    assert "set -o pipefail" in text


def test_android_emulator_action_is_immutable_and_app_cleanup_is_default():
    text = _text()
    smoke = (ROOT / "scripts" / "acceptance" / "android_player_smoke.py").read_text(
        encoding="utf-8"
    )

    assert (
        "ReactiveCircus/android-emulator-runner@"
        "a421e43855164a8197daf9d8d40fe71c6996bb0d" in text
    )
    assert '"--keep-running"' in smoke
    assert '"shell", "am", "force-stop", arguments.package' in smoke


def test_web_smoke_can_attach_to_a_physical_mobile_browser():
    smoke = (ROOT / "scripts" / "acceptance" / "web_mobile_input_smoke.cjs").read_text(
        encoding="utf-8"
    )

    assert '"--cdp-endpoint"' in smoke
    assert "chromium.connectOverCDP" in smoke
    assert 'session.send("Input.dispatchTouchEvent"' in smoke
    assert 'process.argv.includes("--movement-touch")' in smoke
    assert 'process.argv.includes("--skip-frame-checks")' in smoke
    assert '"touch:left-zone-forward"' in smoke
    assert 'dataset.infernuxState === "ready"' in smoke
    assert "awaiting-user-activation" not in smoke


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
    assert "frameIsVisible" in smoke
    assert "inputPreservedFrame" in smoke
    assert '"pngjs": "7.0.0"' in package
