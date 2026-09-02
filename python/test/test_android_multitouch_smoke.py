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
INSTRUMENTATION_RESULT: committedText=输入测试中文🙂
INSTRUMENTATION_RESULT: height=1440
INSTRUMENTATION_RESULT: imeInset=612
INSTRUMENTATION_RESULT: width=3200
INSTRUMENTATION_CODE: -1
"""
    ) == (3200, 1440, 612, "输入测试中文🙂")


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
        "INFERNUX_PLATFORM_FIXTURE_MULTITOUCH_READY",
        "INFERNUX_PLATFORM_FIXTURE_UNITY_TOUCH_READY",
        "INFERNUX_PLATFORM_FIXTURE_TOUCH_CANCELED",
        "INFERNUX_PLATFORM_FIXTURE_UI_CLICK_READY",
        "INFERNUX_PLATFORM_FIXTURE_IME_VISIBLE",
        "INFERNUX_PLATFORM_FIXTURE_TEXT_COMMITTED value=输入测试中文🙂",
        "INFERNUX_PLATFORM_FIXTURE_IME_HIDDEN",
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
    assert "injectTap(automation, width, height, 0.5f, 0.07f);" in source
    assert "focused.onCreateInputConnection(new EditorInfo())" in source
    assert 'connection.commitText(text, 1)' in source
    assert 'WindowInsets.Type.ime()' in source
