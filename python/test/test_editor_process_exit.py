from __future__ import annotations

import pytest

from Infernux.engine.engine import Engine
from Infernux.plugins import PluginManager


class _PluginManagerProbe:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error
        self.engine = None

    def shutdown(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def _engine_without_native_runtime(*, process_owned: bool) -> Engine:
    engine = Engine.__new__(Engine)
    engine._process_owned_exit = process_owned
    return engine


def test_standalone_process_exit_does_not_unload_process_owned_plugins(monkeypatch):
    manager = _PluginManagerProbe()
    monkeypatch.setattr(PluginManager, "_instance", manager)
    engine = _engine_without_native_runtime(process_owned=True)

    engine._shutdown_plugins_for_exit()

    assert manager.calls == 0


def test_embedded_engine_exit_preserves_plugin_unload_contract(monkeypatch):
    manager = _PluginManagerProbe()
    monkeypatch.setattr(PluginManager, "_instance", manager)
    engine = _engine_without_native_runtime(process_owned=False)
    manager.engine = engine

    engine._shutdown_plugins_for_exit()

    assert manager.calls == 1


def test_embedded_plugin_unload_failure_is_not_suppressed(monkeypatch):
    manager = _PluginManagerProbe(error=RuntimeError("unload failed"))
    monkeypatch.setattr(PluginManager, "_instance", manager)
    engine = _engine_without_native_runtime(process_owned=False)
    manager.engine = engine

    with pytest.raises(RuntimeError, match="unload failed"):
        engine._shutdown_plugins_for_exit()


@pytest.mark.parametrize("owner", [None, object()])
def test_temporary_cook_host_does_not_unload_callers_plugins(monkeypatch, owner):
    manager = _PluginManagerProbe()
    manager.engine = owner
    monkeypatch.setattr(PluginManager, "_instance", manager)
    engine = _engine_without_native_runtime(process_owned=False)

    engine._shutdown_plugins_for_exit()

    assert manager.calls == 0
    assert PluginManager.instance() is manager

