import os

from Infernux.components.registry import (
    get_component_registrations,
    register_component_script,
    unregister_component_script,
)
from Infernux.engine.bootstrap_inspector._helpers import _get_add_component_entries


def test_project_component_source_is_registered_without_execution(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    script = assets / "player.py"
    script.write_text(
        "raise RuntimeError('registry indexing must not execute scripts')\n"
        "class PlayerController(InxComponent):\n"
        "    pass\n",
        encoding="utf-8",
    )
    try:
        assert register_component_script(str(script))
        registrations = get_component_registrations(project_root=str(tmp_path))
        player = next(entry for entry in registrations if entry.type_name == "PlayerController")
        assert player.script_path == os.path.abspath(script)
        assert player.component_type is None
    finally:
        unregister_component_script(str(script))


def test_add_component_menu_reads_registry_without_filesystem_scan(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    assets.mkdir()
    script = assets / "player.py"
    script.write_text("class RegistryOnly(InxComponent):\n    pass\n", encoding="utf-8")
    register_component_script(str(script))
    monkeypatch.setattr(
        "Infernux.engine.project_context.get_project_root",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(os, "walk", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Add Component must not scan the filesystem")
    ))
    try:
        entries = _get_add_component_entries()
        entry = next(item for item in entries if item.display_name == "RegistryOnly")
        assert entry.script_path == os.path.abspath(script)
    finally:
        unregister_component_script(str(script))


def test_deleted_component_script_leaves_registry(tmp_path):
    script = tmp_path / "removed.py"
    script.write_text("class Removed(InxComponent):\n    pass\n", encoding="utf-8")
    assert register_component_script(str(script))
    unregister_component_script(str(script))
    assert all(
        entry.type_name != "Removed"
        for entry in get_component_registrations(project_root=str(tmp_path))
    )
