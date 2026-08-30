import os
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


def test_child_environment_owns_the_shared_package_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(hub_utils, "get_hub_data_dir", lambda: str(tmp_path / "HubData"))
    monkeypatch.delenv("INFERNUX_PACKAGE_CACHE_ROOT", raising=False)

    merged = hub_utils.merge_child_env_utf8()

    assert merged["INFERNUX_PACKAGE_CACHE_ROOT"] == os.path.join(
        str(tmp_path / "HubData"), "packages"
    )


def test_explicit_package_cache_override_survives_hub_launch(monkeypatch, tmp_path):
    explicit = str(tmp_path / "ManagedPackages")
    monkeypatch.setenv("INFERNUX_PACKAGE_CACHE_ROOT", explicit)

    merged = hub_utils.merge_child_env_utf8()

    assert merged["INFERNUX_PACKAGE_CACHE_ROOT"] == explicit
