from __future__ import annotations

import importlib
import importlib.metadata
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from Infernux.plugins import PluginManager
from Infernux.host.commands import MainThreadCommandQueue


@pytest.fixture
def site_install(tmp_path, monkeypatch):
    site_root = tmp_path / "site-packages"
    site_root.mkdir()
    monkeypatch.syspath_prepend(str(site_root))
    counter = SimpleNamespace(new=0, unrelated=0, nested=0)
    monkeypatch.setitem(sys.modules, "site_publication_counter", counter)
    (site_root / "old.pth").write_text("import site_publication_counter; site_publication_counter.unrelated += 1\n")
    manager = PluginManager(str(tmp_path / "project"))
    monkeypatch.setattr(manager, "_project_python_executable", lambda: sys.executable)
    environment = {}
    monkeypatch.setattr(manager, "_python_environment_snapshot", lambda *_args: dict(environment))

    def install(_command, cwd=None):
        package = site_root / "new_site_path"
        package.mkdir(exist_ok=True)
        (package / "inx_site_published_probe.py").write_text("VALUE = 42\n")
        (package / "nested.pth").write_text("import site_publication_counter; site_publication_counter.nested += 1\n")
        (site_root / "new.pth").write_text("new_site_path\nimport site_publication_counter; site_publication_counter.new += 1\n")
        info = site_root / "inx_site_test-1.0.dist-info"
        info.mkdir(exist_ok=True)
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: inx-site-test\nVersion: 1.0\n")
        (info / "RECORD").write_text("new.pth,,\nnew_site_path/nested.pth,,\nnew_site_path/inx_site_published_probe.py,,\ninx_site_test-1.0.dist-info/METADATA,,\n")
        environment['inx-site-test'] = '1.0'
        return SimpleNamespace(stdout="installed fixture wheel")

    monkeypatch.setattr(manager, "_run_process", install)
    yield manager, counter
    sys.modules.pop("inx_site_published_probe", None)


@pytest.mark.parametrize("entrypoint", ["requirements", "pip"])
def test_new_dependency_site_hooks_are_published_in_the_same_process(site_install, entrypoint):
    manager, counter = site_install
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("inx_site_published_probe")
    if entrypoint == "requirements":
        manager._install_pip_lines(["inx-site-test==1.0\n"])
    else:
        manager.install_pip("inx-site-test==1.0")
    assert importlib.import_module("inx_site_published_probe").VALUE == 42
    assert counter.new == 1
    assert counter.unrelated == counter.nested == 0
    # No-op installation must not re-execute any site's startup hooks.
    manager._install_pip_lines(["inx-site-test==1.0\n"])
    assert counter.new == 1


def test_installing_for_another_interpreter_does_not_mutate_live_paths(site_install, monkeypatch):
    manager, counter = site_install
    monkeypatch.setattr(manager, "_project_python_executable", lambda: "another-python")
    paths = list(sys.path)
    manager._install_pip_lines(["inx-site-test==1.0\n"])
    assert sys.path == paths
    assert counter.new == 0


def test_startup_requirement_restoration_publishes_before_preload(site_install, monkeypatch):
    manager, counter = site_install
    monkeypatch.setattr(manager.registry, "installed", lambda: [{
        "reference": "vendor/site-test", "enabled": True,
        "python_requirements": [{"requirement": "inx-site-test==1.0"}],
    }])
    reconciled = []
    monkeypatch.setattr(manager.registry, "record_python_reconciliation", lambda **value: reconciled.append(value))
    assert manager._reconcile_python_requirements_for_startup() == ("vendor/site-test",)
    assert importlib.import_module("inx_site_published_probe").VALUE == 42
    assert counter.new == 1
    assert len(reconciled) == 1


def test_failed_pip_transaction_does_not_publish_site_hooks(site_install, monkeypatch):
    manager, counter = site_install
    install = manager._run_process

    def fail(*args, **kwargs):
        install(*args, **kwargs)
        raise RuntimeError("pip failed")

    monkeypatch.setattr(manager, "_run_process", fail)
    monkeypatch.setattr(manager, "_restore_python_environment", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="pip failed"):
        manager._install_pip_lines(["inx-site-test==1.0\n"])
    assert counter.new == 0


def test_background_dependency_install_publishes_on_editor_owner_thread(site_install, monkeypatch):
    manager, counter = site_install
    manager.engine = object()
    queue = MainThreadCommandQueue()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)
    queue.drain()
    calls = []
    import site
    original = site.addpackage

    def addpackage(*args):
        calls.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(site, "addpackage", addpackage)
    errors = []
    done = threading.Event()

    def install():
        try:
            manager._install_pip_lines(["inx-site-test==1.0\n"])
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=install)
    worker.start()
    try:
        while not done.wait(0.01):
            queue.drain()
        worker.join(1)
        assert not errors
        assert calls == [threading.get_ident()]
        assert counter.new == 1
    finally:
        queue.cancel_pending()
        worker.join(2)
