import os
import sys

import hub_utils
import pytest


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


def test_non_windows_pid_probe_distinguishes_missing_and_inaccessible_processes(
    monkeypatch,
):
    monkeypatch.setattr(hub_utils.sys, "platform", "linux")

    monkeypatch.setattr(
        hub_utils.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert hub_utils.is_pid_running(42) is False

    monkeypatch.setattr(
        hub_utils.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(PermissionError()),
    )
    assert hub_utils.is_pid_running(42) is True


def test_non_windows_pid_probe_propagates_unclassified_os_failure(monkeypatch):
    monkeypatch.setattr(hub_utils.sys, "platform", "linux")
    monkeypatch.setattr(
        hub_utils.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(OSError("probe failed")),
    )

    with pytest.raises(OSError, match="probe failed"):
        hub_utils.is_pid_running(42)


@pytest.mark.parametrize(
    "payload, message",
    (
        ("{", "Expecting property name"),
        ("[]", "must contain a JSON object"),
        ('{"pid": true, "token": "owned"}', "invalid process identity"),
        ('{"pid": 42, "token": ""}', "invalid process identity"),
    ),
)
def test_project_lock_requires_current_process_identity(tmp_path, payload, message):
    lock_path = tmp_path / "ProjectSettings" / ".infernux-engine-lock.json"
    lock_path.parent.mkdir()
    lock_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        hub_utils.read_project_lock(str(tmp_path))

    assert lock_path.is_file()


def test_project_lock_removes_only_a_confirmed_stale_lock(monkeypatch, tmp_path):
    lock_path = hub_utils.write_project_lock(
        str(tmp_path),
        42,
        "owned",
        "editor",
        "running",
    )
    monkeypatch.setattr(hub_utils, "is_pid_running", lambda _pid: False)

    assert hub_utils.read_project_lock(str(tmp_path)) is None
    assert not os.path.exists(lock_path)


def test_project_lock_keeps_a_confirmed_live_lock(monkeypatch, tmp_path):
    lock_path = hub_utils.write_project_lock(
        str(tmp_path),
        42,
        "owned",
        "editor",
        "running",
    )
    monkeypatch.setattr(hub_utils, "is_pid_running", lambda _pid: True)

    lock = hub_utils.read_project_lock(str(tmp_path))

    assert lock is not None
    assert lock["pid"] == 42
    assert os.path.isfile(lock_path)


def test_project_lock_deletion_failure_is_not_hidden(monkeypatch, tmp_path):
    hub_utils.write_project_lock(
        str(tmp_path),
        42,
        "owned",
        "editor",
        "running",
    )
    monkeypatch.setattr(hub_utils, "is_pid_running", lambda _pid: False)
    monkeypatch.setattr(
        hub_utils.os,
        "remove",
        lambda _path: (_ for _ in ()).throw(PermissionError("locked")),
    )

    with pytest.raises(PermissionError, match="locked"):
        hub_utils.read_project_lock(str(tmp_path))


def test_owned_project_lock_removal_requires_a_valid_lock_document(tmp_path):
    lock_path = tmp_path / "ProjectSettings" / ".infernux-engine-lock.json"
    lock_path.parent.mkdir()
    lock_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must contain a JSON object"):
        hub_utils.remove_project_lock(str(tmp_path), "owned")

    assert lock_path.is_file()
