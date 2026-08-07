from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from Infernux.engine.interaction import (
    ActionOrigin,
    DocumentKind,
    DocumentOpenResult,
    DocumentOpenStatus,
    PrefabCommandService,
    SelectionDomain,
)
from Infernux.engine.path_utils import resolved_path, same_path


class _NavigationProbe:
    def __init__(self) -> None:
        self.calls = []

    def locate(self, target, **kwargs):
        self.calls.append((target, kwargs))
        return True


class _SelectionProbe:
    def __init__(self) -> None:
        self.calls = []

    def select(self, target, **kwargs):
        self.calls.append((target, kwargs))
        return True


class _DocumentOpenProbe:
    def __init__(self) -> None:
        self.calls = []

    def open_resource(self, kind, path):
        self.calls.append((kind, path))
        return DocumentOpenResult(DocumentOpenStatus.READY)


class _ProjectAssetProbe:
    def __init__(self, project_root, asset_database=None) -> None:
        self.project_root = str(project_root)
        self.asset_database = asset_database
        self.create_calls = []

    def create_prefab(
        self,
        destination,
        creator,
        capture_linkage,
        restore_linkage,
        *,
        origin,
    ):
        before = capture_linkage()
        result = creator()
        self.create_calls.append(
            {
                "destination": destination,
                "before": before,
                "restore_linkage": restore_linkage,
                "origin": origin,
                "result": result,
            }
        )
        return result


class _UndoManagerProbe:
    enabled = True
    is_executing = False

    def __init__(self) -> None:
        self.calls = []

    def execute(self, command, *, origin):
        self.calls.append((command, origin))
        return True


class _CommandProbe:
    def __init__(self, kind, *arguments, **options) -> None:
        self.kind = kind
        self.arguments = arguments
        self.options = options
        self.description = f"Prefab {kind}"

    def dispose(self) -> None:
        raise AssertionError("an accepted command must not be disposed")


@pytest.fixture()
def prefab_service(tmp_path):
    previous = PrefabCommandService._instance
    selection = _SelectionProbe()
    navigation = _NavigationProbe()
    document_open = _DocumentOpenProbe()
    project_assets = _ProjectAssetProbe(tmp_path)
    service = PrefabCommandService(
        selection,
        navigation,
        document_open,
        project_assets,
    )
    try:
        yield service, selection, navigation, document_open, project_assets
    finally:
        service.shutdown()
        PrefabCommandService._instance = previous


def test_can_execute_resolves_each_prefab_action(prefab_service, tmp_path, monkeypatch):
    service, _selection, _navigation, _documents, project_assets = prefab_service
    prefab_path = tmp_path / "Assets" / "Probe.prefab"
    prefab_path.parent.mkdir()
    prefab_path.write_text("{}", encoding="utf-8")
    source = object()
    root = SimpleNamespace(id=17, prefab_guid="prefab-guid")
    project_assets.asset_database = SimpleNamespace(
        get_path_from_guid=lambda guid: str(prefab_path) if guid == "prefab-guid" else ""
    )
    monkeypatch.setattr(
        service,
        "_scene_object",
        lambda object_id: source if int(object_id) == 7 else None,
    )
    monkeypatch.setattr(
        service,
        "_instance_root",
        lambda object_id: root if int(object_id) == 17 else None,
    )

    assert service.can_execute("create", object_id=7)
    assert not service.can_execute("create", object_id=8)
    assert service.can_execute("locate", path=str(prefab_path))
    assert service.can_execute("open", path=str(prefab_path))
    assert service.can_execute("apply", object_id=17)
    assert service.can_execute("revert", object_id=17)
    assert service.can_execute("unpack", object_id=17)
    assert not service.can_execute("apply", object_id=99)
    assert not service.can_execute("unknown", object_id=17)


def test_locate_delegates_to_navigation_service(prefab_service, tmp_path):
    service, _selection, navigation, _documents, _assets = prefab_service
    prefab_path = tmp_path / "Located.prefab"
    prefab_path.write_text("{}", encoding="utf-8")

    assert service.locate(path=str(prefab_path), record_history=False)
    assert len(navigation.calls) == 1
    target, options = navigation.calls[0]
    assert target.domain is SelectionDomain.ASSET
    assert same_path(target.target_id, str(prefab_path))
    assert options == {
        "owner_id": "prefab",
        "reason": "prefab_locate",
        "record_history": False,
    }


def test_create_from_object_delegates_to_project_asset_service(
    prefab_service,
    tmp_path,
    monkeypatch,
):
    from Infernux.engine.ui import project_file_ops

    service, _selection, _navigation, _documents, project_assets = prefab_service
    assets = tmp_path / "Assets"
    assets.mkdir()
    created_path = assets / "Created.prefab"

    class _Scene:
        def find_by_id(self, object_id):
            return source if int(object_id) == source.id else None

    source = SimpleNamespace(
        id=31,
        name="Source",
        prefab_guid="",
        prefab_root=False,
        scene=_Scene(),
        get_children=lambda: [],
        get_parent=lambda: None,
    )
    project_assets.asset_database = object()
    monkeypatch.setattr(service, "_scene_object", lambda object_id: source)
    creator_calls = []

    def _create(game_object, destination, asset_database, *, source_canvas_name):
        creator_calls.append(
            (game_object, destination, asset_database, source_canvas_name)
        )
        return True, str(created_path)

    monkeypatch.setattr(project_file_ops, "create_prefab_from_gameobject", _create)

    result = service.create_from_object(
        source.id,
        str(assets),
        origin=ActionOrigin.AUTOMATION,
    )

    assert result == resolved_path(str(created_path))
    assert creator_calls == [(source, resolved_path(str(assets)), project_assets.asset_database, "")]
    assert len(project_assets.create_calls) == 1
    call = project_assets.create_calls[0]
    assert call["destination"] == resolved_path(str(assets))
    assert call["before"] == ((source.id, "", False),)
    assert call["origin"] is ActionOrigin.AUTOMATION


def test_apply_revert_and_unpack_each_execute_one_undo_command(
    prefab_service,
    tmp_path,
    monkeypatch,
):
    import Infernux.engine.prefab_overrides as prefab_overrides
    import Infernux.engine.undo as undo_module
    from Infernux.engine.undo import UndoManager

    service, selection, _navigation, _documents, project_assets = prefab_service
    prefab_path = tmp_path / "Source.prefab"
    prefab_path.write_text("{}", encoding="utf-8")
    root = SimpleNamespace(id=41, name="Instance", prefab_guid="prefab-guid")
    project_assets.asset_database = SimpleNamespace(
        get_path_from_guid=lambda guid: str(prefab_path) if guid == "prefab-guid" else ""
    )
    monkeypatch.setattr(service, "_instance_root", lambda _object_id: root)
    monkeypatch.setattr(
        prefab_overrides,
        "build_prefab_apply_command",
        lambda *args: _CommandProbe("apply", *args),
    )
    monkeypatch.setattr(
        prefab_overrides,
        "build_prefab_revert_command",
        lambda *args: _CommandProbe("revert", *args),
    )
    monkeypatch.setattr(
        undo_module,
        "PrefabUnpackCommand",
        lambda object_id: _CommandProbe("unpack", object_id),
    )
    manager = _UndoManagerProbe()
    monkeypatch.setattr(UndoManager, "_instance", manager)

    operations = (
        ("apply", lambda: service.apply(root.id)),
        ("revert", lambda: service.revert(root.id)),
        ("unpack", lambda: service.unpack(root.id)),
    )
    for expected_kind, operation in operations:
        before = len(manager.calls)
        assert operation()
        assert len(manager.calls) == before + 1
        command, origin = manager.calls[-1]
        assert command.kind == expected_kind
        assert origin is ActionOrigin.USER

    assert len(manager.calls) == 3
    assert len(selection.calls) == 1
    target, options = selection.calls[0]
    assert target.domain is SelectionDomain.SCENE_OBJECT
    assert target.target_id == str(root.id)
    assert options["reason"] == "prefab_revert"
    assert options["record_history"] is False


def test_open_enters_prefab_mode_and_opens_prefab_document(
    prefab_service,
    tmp_path,
    monkeypatch,
):
    import Infernux.engine.undo as undo_module
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.undo import UndoManager

    service, _selection, _navigation, documents, _assets = prefab_service
    prefab_path = tmp_path / "Opened.prefab"
    prefab_path.write_text("{}", encoding="utf-8")
    scene_files = SimpleNamespace(is_prefab_mode=False, prefab_mode_path="")
    monkeypatch.setattr(SceneFileManager, "instance", staticmethod(lambda: scene_files))
    monkeypatch.setattr(
        undo_module,
        "PrefabModeCommand",
        lambda path, *, enter_mode: _CommandProbe(
            "open",
            path,
            enter_mode=enter_mode,
        ),
    )
    manager = _UndoManagerProbe()
    monkeypatch.setattr(UndoManager, "_instance", manager)

    assert service.open(path=str(prefab_path), origin=ActionOrigin.AUTOMATION)
    assert len(manager.calls) == 1
    command, origin = manager.calls[0]
    assert command.kind == "open"
    assert same_path(command.arguments[0], str(prefab_path))
    assert command.options == {"enter_mode": True}
    assert origin is ActionOrigin.AUTOMATION
    assert len(documents.calls) == 1
    kind, opened_path = documents.calls[0]
    assert kind is DocumentKind.PREFAB
    assert same_path(opened_path, str(prefab_path))


def test_prefab_commands_have_no_panel_or_event_bus_authority():
    source_root = Path(__file__).resolve().parents[1] / "Infernux" / "engine"
    forbidden = (
        "prefab_actions_getter",
        "apply_overrides_to_prefab_with_undo",
        "revert_overrides_with_undo",
        "open_prefab_mode_with_undo",
        'emit("select_asset"',
        'emit("open_asset"',
    )
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in source_root.rglob("*.py")
    )
    for token in forbidden:
        assert token not in production
