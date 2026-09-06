"""Temporary author paths must not mutate a background import's search list."""

from __future__ import annotations

import importlib.machinery
import sys
import threading

import pytest

from Infernux.engine import project_context
from Infernux.plugins import preload


def _scope(kind, script, project):
    if kind == "preload":
        return preload._temporary_import_paths(str(script), str(project), "")
    return project_context.temporary_script_import_paths(str(script))


@pytest.mark.parametrize("kind", ("preload", "script"))
def test_temporary_import_paths_preserve_the_previous_search_list(
    monkeypatch, tmp_path, kind
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    script = assets / "probe.py"
    script.write_text("", encoding="utf-8")
    previous = list(sys.path)
    monkeypatch.setattr(sys, "path", previous)
    original = previous.copy()
    with project_context.using_project_root(str(tmp_path)):
        with _scope(kind, script, tmp_path):
            assert str(assets) in sys.path
            assert previous == original
            assert sys.path is not previous
        assert sys.path is previous


@pytest.mark.parametrize("kind", ("preload", "script"))
def test_background_pathfinder_keeps_dependency_after_author_scope_exits(
    monkeypatch, tmp_path, kind
):
    installed = tmp_path / "installed"
    installed.mkdir()
    dependency = installed / "threaded_dependency_040.py"
    dependency.write_text("VALUE = 40\n", encoding="utf-8")
    assets = tmp_path / "Assets"
    assets.mkdir()
    script = assets / "probe.py"
    script.write_text("", encoding="utf-8")
    entered = threading.Event()
    resume = threading.Event()
    results, errors = [], []

    class AuthorPathFinder:
        def find_spec(self, fullname, target=None):
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("test did not exit author import scope")
            return None

    def path_hook(path):
        if path == str(assets):
            return AuthorPathFinder()
        raise ImportError

    def background_import():
        try:
            results.append(importlib.machinery.PathFinder.find_spec("threaded_dependency_040"))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(sys, "path", [str(installed)])
    monkeypatch.setattr(sys, "path_hooks", [path_hook, *sys.path_hooks])
    monkeypatch.setattr(sys, "path_importer_cache", {})
    thread = threading.Thread(target=background_import)
    try:
        with project_context.using_project_root(str(tmp_path)):
            with _scope(kind, script, tmp_path):
                thread.start()
                assert entered.wait(5), "background finder never entered author root"
            # The real PathFinder is suspended inside its sys.path iteration.
            # Exiting a scope must not shorten that same list past site-packages.
            resume.set()
            thread.join(5)
            assert not thread.is_alive()
        assert not errors
        assert len(results) == 1
        assert results[0] is not None, "installed dependency vanished during scope exit"
        assert results[0].origin == str(dependency)
    finally:
        resume.set()
        if thread.ident is not None:
            thread.join(5)
