from __future__ import annotations

from Infernux import resources
from Infernux.engine import headless
from Infernux.engine import library_sync
from Infernux.plugins import PluginManager


def test_headless_prepares_resources_before_plugin_startup(tmp_path, monkeypatch):
    events: list[object] = []

    class _Native:
        exit_requested = False

    class _Engine:
        def __init__(self, *_args):
            events.append("engine.create")

        def init_headless(self, project):
            events.append(("engine.init", project))

        def get_native_engine(self):
            return _Native()

        def exit(self):
            events.append("engine.exit")

    class _Plugins:
        @staticmethod
        def shutdown():
            events.append("plugins.shutdown")

    monkeypatch.setattr(headless, "Engine", _Engine)
    monkeypatch.setattr(
        library_sync,
        "sync_resources",
        lambda project: events.append(("resources.sync", project)),
    )
    monkeypatch.setattr(
        resources,
        "activate_library",
        lambda project: events.append(("resources.activate", project)),
    )
    monkeypatch.setattr(
        "Infernux.engine._acquire_project_lock",
        lambda project, owner: events.append(("lock.acquire", project, owner))
        or (str(tmp_path / "project.lock"), "token"),
    )
    monkeypatch.setattr(
        "Infernux.engine._remove_project_lock",
        lambda path, token: events.append(("lock.remove", path, token)),
    )
    monkeypatch.setattr(
        PluginManager,
        "startup",
        lambda project, engine=None, runtime=False: events.append(
            ("plugins.startup", project, runtime)
        )
        or _Plugins(),
    )

    project = str(tmp_path / "project")
    assert headless.run_headless(project, lambda *_: True, max_frames=0) == 0

    assert events.index(("resources.sync", project)) < events.index(
        ("plugins.startup", project, False)
    )
    assert events.index(("resources.activate", project)) < events.index(
        ("plugins.startup", project, False)
    )
