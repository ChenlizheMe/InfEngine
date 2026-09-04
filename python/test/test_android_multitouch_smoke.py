from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE = ROOT / "scripts" / "acceptance"
SCRIPT = ACCEPTANCE / "android_multitouch_smoke.py"


def _module():
    sys.path.insert(0, str(ACCEPTANCE))
    try:
        spec = importlib.util.spec_from_file_location(
            "android_multitouch_smoke", SCRIPT
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ACCEPTANCE))


def test_instrumentation_result_requires_success_and_extent():
    module = _module()

    assert module.validate_instrumentation_output(
        """INSTRUMENTATION_RESULT: INFERNUX_MULTITOUCH_INJECTION=passed
INSTRUMENTATION_RESULT: INFERNUX_IME_INJECTION=passed
INSTRUMENTATION_RESULT: INFERNUX_ORIENTATION_INJECTION=passed
INSTRUMENTATION_RESULT: INFERNUX_BACK_INJECTION=passed
INSTRUMENTATION_RESULT: committedText=输入测试中文🙂
INSTRUMENTATION_RESULT: height=1440
INSTRUMENTATION_RESULT: imeInset=612
INSTRUMENTATION_RESULT: landscapeRotation=1
INSTRUMENTATION_RESULT: landscapeSafeInsets=161,0,0,0
INSTRUMENTATION_RESULT: reverseLandscapeRotation=3
INSTRUMENTATION_RESULT: reverseLandscapeSafeInsets=0,0,161,0
INSTRUMENTATION_RESULT: width=3200
INSTRUMENTATION_CODE: -1
"""
    ) == (
        3200,
        1440,
        612,
        "输入测试中文🙂",
        1,
        3,
        (161, 0, 0, 0),
        (0, 0, 161, 0),
    )


def test_screen_state_samples_require_consistent_python_safe_area():
    module = _module()
    log = """
I/python.stdout: INFERNUX_PLATFORM_FIXTURE_SCREEN_STATE revision=4 size=3200x1440 framebuffer=3200x1440 safe=161,161,3039,1219 insets=161,161,0,60 pixel_ratio=3.750000
I/python.stdout: INFERNUX_PLATFORM_FIXTURE_SCREEN_STATE revision=8 size=3200x1440 framebuffer=3200x1440 safe=0,161,3039,1219 insets=0,161,161,60 pixel_ratio=3.750000
I/python.stdout: INFERNUX_PLATFORM_FIXTURE_SCREEN_STATE revision=9 size=3200x1440 framebuffer=3200x1440 safe=0,161,3039,1219 insets=0,161,161,60 pixel_ratio=3.750000
"""

    samples = module.screen_state_samples(log)

    assert len(samples) == 2
    assert samples[0]["safe_insets"] == (161, 161, 0, 60)
    assert samples[1]["safe_insets"] == (0, 161, 161, 60)


def test_screen_state_samples_reject_inconsistent_safe_rect():
    module = _module()
    log = """
INFERNUX_PLATFORM_FIXTURE_SCREEN_STATE revision=4 size=3200x1440 framebuffer=3200x1440 safe=0,0,3200,1440 insets=161,161,0,60 pixel_ratio=3.750000
"""

    with pytest.raises(RuntimeError, match="safe_area and safe_insets disagree"):
        module.screen_state_samples(log)


@pytest.mark.parametrize(
    "output",
    [
        "INSTRUMENTATION_CODE: -1",
        "INSTRUMENTATION_RESULT: INFERNUX_MULTITOUCH_INJECTION=failed\nINSTRUMENTATION_CODE: 0",
        "INSTRUMENTATION_RESULT: INFERNUX_MULTITOUCH_INJECTION=passed\nINSTRUMENTATION_CODE: -1",
    ],
)
def test_instrumentation_result_fails_closed(output: str):
    module = _module()

    with pytest.raises(RuntimeError):
        module.validate_instrumentation_output(output)


def test_parser_uses_current_android_fixture_contract():
    module = _module()

    arguments = module._parser().parse_args(["input.apk"])

    assert arguments.target_package == "com.infernux.bootstrap"
    assert arguments.instrumentation_package == "com.infernux.acceptance.input"
    assert arguments.wait_milliseconds == 7000
    assert module._DEFAULT_REQUIRED_LOGS == (
        "INFERNUX_PLATFORM_FIXTURE_SCREEN_STATE",
        "INFERNUX_PLATFORM_FIXTURE_PACKAGE_RESOURCE_READY "
        "value=Package resource reached UIText on every Player target.",
        "INFERNUX_PLATFORM_FIXTURE_PRELOAD_RESOURCE_READY "
        "value=Package resource reached UIText on every Player target.",
        "INFERNUX_PLATFORM_FIXTURE_MULTITOUCH_READY",
        "INFERNUX_PLATFORM_FIXTURE_UNITY_TOUCH_READY",
        "INFERNUX_PLATFORM_FIXTURE_TOUCH_CANCELED",
        "INFERNUX_PLATFORM_FIXTURE_UI_CLICK_READY",
        "INFERNUX_PLATFORM_FIXTURE_IME_VISIBLE",
        "INFERNUX_PLATFORM_FIXTURE_TEXT_COMMITTED value=输入测试中文🙂",
        "INFERNUX_PLATFORM_FIXTURE_IME_HIDDEN",
        "INFERNUX_PLATFORM_FIXTURE_BACK_READY",
    )


def test_probe_launches_the_target_through_uiautomation_shell():
    source = (
        ROOT
        / "tests"
        / "android"
        / "input_instrumentation"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "infernux"
        / "acceptance"
        / "input"
        / "MultiTouchInstrumentation.java"
    ).read_text(encoding="utf-8")

    assert "automation.executeShellCommand(command)" in source
    assert '"am start -W -n "' in source
    assert "startActivitySync" not in source
    assert "injectCanceledGesture(automation, width, height);" in source
    assert "ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE" in source
    assert "ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE" in source
    assert "Surface.ROTATION_90" in source
    assert "Surface.ROTATION_270" in source
    assert 'shell(automation, "input keyevent KEYCODE_BACK")' in source
    assert "reverseLandscape.width" in source
    assert "reverseLandscape.height" in source
    assert "fixtureButtonCenterX(reverseLandscape.width" in source
    assert "fixtureButtonCenterY(reverseLandscape.width" in source
    assert "Math.sqrt(widthScale * heightScale)" in source
    assert "BUTTON_PRESS_MILLISECONDS = 500L" in source
    assert "0.07f" not in source
    assert "snapshot.windowFocused" in source
    assert "snapshot.attachedToWindow" in source
    assert "INFERNUX_PLATFORM_FIXTURE_GAMEPLAY_READY" in source
    assert 'result.putString("stage", stage)' in source
    assert "focused.onCreateInputConnection(new EditorInfo())" in source
    assert 'connection.commitText(text, 1)' in source
    assert 'WindowInsets.Type.ime()' in source
