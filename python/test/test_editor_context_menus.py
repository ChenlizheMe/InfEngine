from types import SimpleNamespace
from pathlib import Path

import pytest

from Infernux.engine.interaction import (
    CommandStatus,
    CommandSource,
    ContextMenuBuilder,
    ContextMenuCommand,
    ContextMenuSubmenu,
    EditorCommand,
    EditorCommandRegistry,
    FocusService,
    SelectionService,
    SelectionTarget,
)


class _MenuContext:
    def __init__(self, click_label: str = "") -> None:
        self.click_label = click_label
        self.items = []
        self.separators = 0
        self.closed = 0
        self.submenus = []
        self.ended_submenus = 0
        self.semantics = []

    def menu_item(self, label, shortcut, selected, enabled):
        self.items.append((label, shortcut, selected, enabled))
        return enabled and label == self.click_label

    def separator(self):
        self.separators += 1

    def close_current_popup(self):
        self.closed += 1

    def begin_menu(self, label, enabled=True, semantic_id=""):
        self.submenus.append((label, enabled, semantic_id))
        return enabled

    def end_menu(self):
        self.ended_submenus += 1

    def record_semantic_item(self, kind, label, enabled, semantic_id):
        self.semantics.append((kind, label, enabled, semantic_id))


def _registry() -> EditorCommandRegistry:
    return EditorCommandRegistry(
        focus=FocusService(),
        selection=SelectionService(),
    )


def test_context_menu_uses_registry_metadata_payload_and_checked_state():
    registry = _registry()
    received = []
    registry.register(
        EditorCommand(
            "edit.toggle",
            lambda context: received.append(dict(context.payload)) or True,
            display_name="Toggle",
            default_shortcut="Ctrl+T",
            is_checked=lambda context: bool(context.payload.get("checked")),
        )
    )
    ctx = _MenuContext("Toggle")

    rendered = ContextMenuBuilder(registry).render(
        ctx,
        (ContextMenuCommand("edit.toggle", payload={"local": 2}),),
        payload={"checked": True, "root": 1},
    )

    assert ctx.items == [("Toggle", "Ctrl+T", True, True)]
    assert received == [{"checked": True, "root": 1, "local": 2}]
    assert rendered is not None
    assert rendered.result.status is CommandStatus.EXECUTED
    assert ctx.closed == 1
    assert ctx.semantics == [("menu_item", "Toggle", True, "edit.toggle")]


def test_context_menu_can_defer_domain_execution_until_widget_scopes_close():
    registry = _registry()
    events = []
    registry.register(
        EditorCommand(
            "asset.clear",
            lambda context: events.append(("execute", context.payload["field"]))
            or True,
            display_name="Clear",
        )
    )
    ctx = _MenuContext("Clear")
    builder = ContextMenuBuilder(registry)

    request = builder.render_deferred(
        ctx,
        (ContextMenuCommand("asset.clear"),),
        payload={"field": "material"},
    )

    assert request is not None
    assert request.spec.command_id == "asset.clear"
    assert events == []
    assert ctx.closed == 1

    result = builder.execute_resolved(request)
    assert result.result.status is CommandStatus.EXECUTED
    assert events == [("execute", "material")]


def test_deferred_context_menu_executes_the_frozen_focus_and_selection():
    focus = FocusService()
    selection = SelectionService()
    registry = EditorCommandRegistry(focus=focus, selection=selection)
    captured = []
    registry.register(
        EditorCommand(
            "asset.delete",
            lambda context: captured.append(context) or True,
            display_name="Delete",
        )
    )
    focus.activate_panel(
        "project",
        view_id="project",
        record_history=False,
    )
    selection.select(
        SelectionTarget.asset("C:/Project/Assets/A.mat"),
        owner_id="project",
        record_history=False,
    )
    builder = ContextMenuBuilder(registry)
    request = builder.render_deferred(
        _MenuContext("Delete"),
        (ContextMenuCommand("asset.delete"),),
        payload={"target_id": "C:/Project/Assets/A.mat"},
    )
    assert request is not None

    focus.activate_panel(
        "hierarchy",
        view_id="hierarchy",
        record_history=False,
    )
    selection.select(
        SelectionTarget.scene_object(42),
        owner_id="hierarchy",
        record_history=False,
    )

    result = builder.execute_resolved(request)

    assert result.result.status is CommandStatus.EXECUTED
    assert len(captured) == 1
    assert captured[0].source is CommandSource.CONTEXT_MENU
    assert captured[0].focus.active_panel_id == "project"
    assert captured[0].selection.primary == SelectionTarget.asset(
        "C:/Project/Assets/A.mat"
    )
    assert captured[0].payload["target_id"].endswith("A.mat")


def test_context_menu_disabled_reason_hidden_items_and_separator_groups():
    registry = _registry()
    registry.register(
        EditorCommand(
            "hidden",
            lambda _context: True,
            can_execute=lambda _context: False,
        )
    )
    registry.register(
        EditorCommand(
            "disabled",
            lambda _context: True,
            display_name="Disabled",
            can_execute=lambda _context: False,
            disabled_reason=lambda context: f"Unavailable for {context.payload['target']}",
        )
    )
    registry.register(EditorCommand("enabled", lambda _context: True, display_name="Enabled"))
    builder = ContextMenuBuilder(registry)

    resolved = builder.resolve(
        (
            ContextMenuCommand("hidden", hide_when_disabled=True),
            ContextMenuCommand("disabled", separator_before=True),
            ContextMenuCommand("enabled", separator_before=True),
        ),
        payload={"target": "selection"},
    )
    ctx = _MenuContext()
    builder.render(
        ctx,
        (
            ContextMenuCommand("hidden", hide_when_disabled=True),
            ContextMenuCommand("disabled", separator_before=True),
            ContextMenuCommand("enabled", separator_before=True),
        ),
        payload={"target": "selection"},
    )

    assert [entry.spec.command_id for entry in resolved] == ["disabled", "enabled"]
    assert resolved[0].disabled_reason == "Unavailable for selection"
    assert ctx.separators == 1


def test_context_menu_rejects_unregistered_commands():
    with pytest.raises(KeyError, match="not registered"):
        ContextMenuBuilder(_registry()).resolve((ContextMenuCommand("missing"),))


def test_context_menu_submenus_keep_leaf_commands_registry_backed():
    registry = _registry()
    received = []
    registry.register(
        EditorCommand(
            "scene.create",
            lambda context: received.append(dict(context.payload)) or True,
            display_name="Create",
        )
    )
    ctx = _MenuContext("Cube")

    result = ContextMenuBuilder(registry).render(
        ctx,
        (
            ContextMenuSubmenu(
                "3D Object",
                (
                    ContextMenuCommand(
                        "scene.create",
                        label="Cube",
                        payload={"kind": "cube"},
                    ),
                ),
                semantic_id="hierarchy.create_3d",
            ),
        ),
        payload={"parent_id": 42},
    )

    assert ctx.submenus == [("3D Object", True, "hierarchy.create_3d")]
    assert ctx.ended_submenus == 1
    assert received == [{"parent_id": 42, "kind": "cube"}]
    assert result is not None
    assert result.command.spec.command_id == "scene.create"


def test_context_menu_hides_an_empty_nested_branch():
    registry = _registry()
    registry.register(
        EditorCommand(
            "hidden",
            lambda _context: True,
            can_execute=lambda _context: False,
        )
    )
    ctx = _MenuContext()

    ContextMenuBuilder(registry).render(
        ctx,
        (
            ContextMenuSubmenu(
                "Empty",
                (ContextMenuCommand("hidden", hide_when_disabled=True),),
                semantic_id="test.empty",
            ),
        ),
    )

    assert ctx.submenus == []


def test_context_menu_submenus_require_stable_semantic_identity():
    with pytest.raises(ValueError, match="semantic_id"):
        ContextMenuSubmenu("Unstable", ())


def _flatten_menu_entries(entries):
    flattened = []
    for entry in entries:
        if isinstance(entry, ContextMenuSubmenu):
            flattened.extend(_flatten_menu_entries(entry.entries))
        else:
            flattened.append(entry)
    return flattened


def test_hierarchy_menu_freezes_target_and_create_parent_payloads():
    from Infernux.engine.ui.core_context_menus import hierarchy_context_menu

    entries = hierarchy_context_menu(
        lambda key: key,
        target_id=42,
        target_is_prefab=True,
    )
    leaves = _flatten_menu_entries(entries)
    create_entries = [entry for entry in leaves if entry.command_id == "scene.create_object"]
    rename = next(entry for entry in leaves if entry.command_id == "edit.rename")
    prefab = next(entry for entry in leaves if entry.command_id == "prefab.apply")

    assert create_entries
    assert {entry.payload["parent_id"] for entry in create_entries} == {42}
    assert rename.payload["target_id"] == "42"
    assert prefab.payload["object_id"] == 42


def test_project_menu_freezes_logical_and_reveal_paths_separately():
    from Infernux.engine.ui.core_context_menus import project_context_menu

    entries = project_context_menu(
        lambda key: key,
        target_path="Assets/model.fbx::submesh:Body",
        reveal_path="C:/Game/Assets/model.fbx",
        current_path="C:/Game/Assets",
    )
    leaves = _flatten_menu_entries(entries)
    reveal = next(
        entry for entry in leaves
        if entry.command_id == "project.reveal_in_explorer"
    )
    rename = next(entry for entry in leaves if entry.command_id == "edit.rename")

    assert reveal.payload == {"path": "C:/Game/Assets/model.fbx"}
    assert rename.payload == {"target_id": "Assets/model.fbx::submesh:Body"}


def test_native_hierarchy_and_project_menus_are_presentation_only():
    root = Path(__file__).resolve().parents[2]
    hierarchy = (
        root / "cpp" / "infernux" / "function" / "editor" / "HierarchyPanel.cpp"
    ).read_text(encoding="utf-8")
    project = (
        root / "cpp" / "infernux" / "function" / "editor" / "ProjectPanel.cpp"
    ).read_text(encoding="utf-8")

    hierarchy_body = hierarchy.split(
        "void HierarchyPanel::RenderItemContextMenu", 1
    )[1].split("bool HierarchyPanel::ExecuteEditorCommand", 1)[0]
    project_body = project.split(
        "void ProjectPanel::RenderContextMenu", 1
    )[1].split("void ProjectPanel::RenderDragDropSource", 1)[0]

    for body in (hierarchy_body, project_body):
        assert "Selectable(" not in body
        assert "CanExecuteEditorCommand" not in body
        assert "ExecuteEditorCommand" not in body
    assert "revealInExplorer(" not in project_body
    assert "ShowStandardCreateMenus" not in hierarchy
