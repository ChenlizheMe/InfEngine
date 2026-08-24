import sys

import hub_utils


def test_is_frozen_detects_pyinstaller(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert hub_utils.is_frozen() is True


def test_is_frozen_detects_nuitka_marker_on_main_module(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delitem(hub_utils.__dict__, "__compiled__", raising=False)
    main_module = sys.modules["__main__"]
    monkeypatch.setattr(main_module, "__compiled__", object(), raising=False)

    assert hub_utils.is_frozen() is True


def test_is_frozen_is_false_for_source_python(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delitem(hub_utils.__dict__, "__compiled__", raising=False)
    main_module = sys.modules["__main__"]
    monkeypatch.delattr(main_module, "__compiled__", raising=False)

    assert hub_utils.is_frozen() is False
