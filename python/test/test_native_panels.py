"""Tests for native StatusBarPanel, ToolbarPanel, MenuBarPanel, and HierarchyPanel."""
from pathlib import Path

import pytest
from Infernux.lib import (
    StatusBarPanel,
    ToolbarPanel,
    MenuBarPanel,
    ConsolePanel,
    HierarchyPanel,
    PlayState,
    LogLevel,
    WindowTypeInfo,
)


# ═══════════════════════════════════════════════════════════════════════
#  StatusBarPanel
# ═══════════════════════════════════════════════════════════════════════

class TestStatusBarPanel:

    def test_creation(self):
        sb = StatusBarPanel()
        assert sb is not None

    def test_set_engine_status(self):
        sb = StatusBarPanel()
        sb.set_engine_status("Loading...", 0.5, "progress")
        sb.set_engine_status("Done", 1.0, "success")
        sb.set_engine_status("", -1.0, "idle")

    def test_set_console_panel(self):
        sb = StatusBarPanel()
        console = ConsolePanel()
        sb.set_console_panel(console)


class TestConsolePanel:

    def test_status_snapshot_is_exact_before_panel_render(self):
        console = ConsolePanel()
        console.log_from_python(LogLevel.Info, "first")
        console.log_from_python(LogLevel.Warn, "second")
        console.log_from_python(LogLevel.Error, "latest")

        message, level, info, warning, error, uid = console._get_status_snapshot()
        assert (message, level) == ("latest", "error")
        assert (info, warning, error) == (1, 1, 1)
        assert uid > 0

        console._select_entry(uid)
        assert console._selected_uid == uid

    def test_error_pause_callback_runs_when_error_enters_console(self):
        console = ConsolePanel()
        calls = []
        console.error_pause = True
        console.on_error_pause = lambda: calls.append("pause")

        console.log_from_python(LogLevel.Error, "runtime failure")
        console._get_status_snapshot()
        assert calls == ["pause"]

    def test_clear_resets_authoritative_summary_and_counts(self):
        console = ConsolePanel()
        console.log_from_python(LogLevel.Warn, "warning")
        console._get_status_snapshot()
        revision = console._revision

        console.clear()
        assert console._revision > revision
        assert console._get_status_snapshot() == ("", "info", 0, 0, 0, 0)

    def test_select_requests_window_manager_focus(self):
        console = ConsolePanel()
        calls = []
        console.on_request_focus = lambda: calls.append("focus")
        console.select_latest_entry()
        assert calls == ["focus"]


# ═══════════════════════════════════════════════════════════════════════
#  ToolbarPanel
# ═══════════════════════════════════════════════════════════════════════

class TestToolbarPanel:

    def test_creation(self):
        tb = ToolbarPanel()
        assert tb is not None

    def test_is_editor_panel(self):
        tb = ToolbarPanel()
        assert tb.is_open()
        assert tb.get_window_id() == "toolbar"

    def test_set_open(self):
        tb = ToolbarPanel()
        tb.set_open(False)
        assert not tb.is_open()

    def test_command_callbacks(self):
        tb = ToolbarPanel()
        calls = []
        tb.execute_command = lambda command_id, source, argument: calls.append(
            (command_id, source, argument)
        ) or True
        tb.can_execute_command = lambda command_id, argument: (
            command_id == "play.toggle" and not argument
        )

        assert tb.execute_command("play.toggle", "toolbar", "") is True
        assert tb.can_execute_command("play.toggle", "") is True
        assert calls == [("play.toggle", "toolbar", "")]

    def test_get_play_state_callback(self):
        tb = ToolbarPanel()
        tb.get_play_state = lambda: PlayState.Edit
        assert tb.get_play_state() == PlayState.Edit

        tb.get_play_state = lambda: PlayState.Playing
        assert tb.get_play_state() == PlayState.Playing

        tb.get_play_state = lambda: PlayState.Paused
        assert tb.get_play_state() == PlayState.Paused

    def test_get_play_time_str(self):
        tb = ToolbarPanel()
        tb.get_play_time_str = lambda: "01:23.456"
        assert tb.get_play_time_str() == "01:23.456"

    def test_camera_settings_roundtrip(self):
        tb = ToolbarPanel()
        settings = {
            "orthographic": True,
            "fov": 75.0,
            "orthographic_size": 12.0,
            "rotation_speed": 0.1,
            "pan_speed": 2.0,
            "zoom_speed": 1.5,
            "move_speed": 10.0,
            "move_speed_boost": 5.0,
        }
        tb.set_camera_settings(settings)
        result = tb.get_camera_settings()
        assert result["orthographic"] is True
        assert abs(result["fov"] - 75.0) < 0.01
        assert abs(result["orthographic_size"] - 12.0) < 0.01
        assert abs(result["rotation_speed"] - 0.1) < 0.01
        assert abs(result["pan_speed"] - 2.0) < 0.01
        assert abs(result["zoom_speed"] - 1.5) < 0.01
        assert abs(result["move_speed"] - 10.0) < 0.01
        assert abs(result["move_speed_boost"] - 5.0) < 0.01

    def test_camera_settings_defaults(self):
        tb = ToolbarPanel()
        result = tb.get_camera_settings()
        assert result["orthographic"] is False
        assert abs(result["fov"] - 60.0) < 0.01
        assert abs(result["move_speed"] - 5.0) < 0.01

    def test_translate_callback(self):
        tb = ToolbarPanel()
        tb.translate = lambda key: f"[{key}]"
        # Callback is invoked during render; just verify wiring
        assert tb.translate is not None

    def test_grid_callbacks(self):
        tb = ToolbarPanel()
        grid_state = {"on": True}
        tb.is_show_grid = lambda: grid_state["on"]
        tb.set_show_grid = lambda v: grid_state.__setitem__("on", v)
        assert tb.is_show_grid()
        tb.set_show_grid(False)
        assert not tb.is_show_grid()

    def test_sync_camera_from_engine(self):
        tb = ToolbarPanel()
        tb.sync_camera_from_engine = lambda: {
            "fov": 90.0,
            "rotation_speed": 0.1,
            "pan_speed": 2.0,
            "zoom_speed": 1.0,
            "move_speed": 5.0,
            "move_speed_boost": 3.0,
        }
        # Will be invoked during render when camera popup opens

    def test_apply_camera_to_engine(self):
        applied = {}
        tb = ToolbarPanel()
        tb.apply_camera_to_engine = lambda d: applied.update(d)
        # Setting camera should invoke the apply callback
        tb.set_camera_settings({"fov": 45.0})
        assert abs(applied.get("fov", 0) - 45.0) < 0.01


# ═══════════════════════════════════════════════════════════════════════
#  MenuBarPanel
# ═══════════════════════════════════════════════════════════════════════

class TestMenuBarPanel:

    def test_creation(self):
        mb = MenuBarPanel()
        assert mb is not None

    def test_command_callbacks(self):
        mb = MenuBarPanel()
        calls = []
        mb.execute_command = lambda command_id, source, argument: calls.append(
            (command_id, source, argument)
        ) or True
        mb.can_execute_command = lambda command_id, argument: (
            command_id == "file.save" or argument == "console"
        )
        mb.is_command_checked = lambda command_id, argument: (
            command_id == "window.open" and argument == "console"
        )
        mb.route_shortcut = lambda chord, text_input, modal: calls.append(
            (chord, text_input, modal)
        ) or True
        mb.on_request_close = lambda: calls.append("close")

        assert mb.execute_command("file.save", "menu", "") is True
        assert mb.can_execute_command("file.save", "") is True
        assert mb.can_execute_command("window.open", "console") is True
        assert mb.is_command_checked("window.open", "console") is True
        assert mb.route_shortcut("Ctrl+S", False, False) is True
        mb.on_request_close()
        assert calls == [
            ("file.save", "menu", ""), ("Ctrl+S", False, False), "close"
        ]

    def test_window_management_callbacks(self):
        mb = MenuBarPanel()

        wti = WindowTypeInfo()
        wti.type_id = "console"
        wti.display_name = "Console"
        wti.singleton = True

        mb.get_registered_types = lambda: [wti]

        types = mb.get_registered_types()
        assert len(types) == 1
        assert types[0].type_id == "console"
        assert types[0].display_name == "Console"
        assert types[0].singleton is True

        mb.invalidate_window_type_cache()

    def test_close_requested_callback(self):
        mb = MenuBarPanel()
        mb.is_close_requested = lambda: False
        assert not mb.is_close_requested()


    def test_translate_callback(self):
        mb = MenuBarPanel()
        mb.translate = lambda key: f"<<{key}>>"
        assert mb.translate("menu.project") == "<<menu.project>>"


# ═══════════════════════════════════════════════════════════════════════
#  PlayState enum
# ═══════════════════════════════════════════════════════════════════════

class TestPlayState:

    def test_values(self):
        assert PlayState.Edit is not None
        assert PlayState.Playing is not None
        assert PlayState.Paused is not None

    def test_distinct(self):
        assert PlayState.Edit != PlayState.Playing
        assert PlayState.Playing != PlayState.Paused
        assert PlayState.Edit != PlayState.Paused


# ═══════════════════════════════════════════════════════════════════════
#  WindowTypeInfo
# ═══════════════════════════════════════════════════════════════════════

class TestWindowTypeInfo:

    def test_creation(self):
        wti = WindowTypeInfo()
        assert wti.type_id == ""
        assert wti.display_name == ""
        assert wti.singleton is True

    def test_readwrite(self):
        wti = WindowTypeInfo()
        wti.type_id = "hierarchy"
        wti.display_name = "Hierarchy"
        wti.singleton = False
        assert wti.type_id == "hierarchy"
        assert wti.display_name == "Hierarchy"
        assert wti.singleton is False


# ═══════════════════════════════════════════════════════════════════════
#  HierarchyPanel
# ═══════════════════════════════════════════════════════════════════════

class TestHierarchyPanel:

    def test_inline_rename_survives_context_menu_dismissal(self):
        source = Path("cpp/infernux/function/editor/HierarchyPanel.cpp").read_text(encoding="utf-8")
        header = Path("cpp/infernux/function/editor/HierarchyPanel.h").read_text(encoding="utf-8")

        assert "m_renameSkipDeactivateFrames = 2;" in source
        assert "m_renameSkipDeactivateFrames == 0 && ctx->IsItemDeactivated()" in source
        assert "int m_renameSkipDeactivateFrames = 0;" in header

    def test_creation(self):
        hp = HierarchyPanel()
        assert hp is not None

    def test_is_editor_panel(self):
        from Infernux.lib import EditorPanel
        hp = HierarchyPanel()
        assert isinstance(hp, EditorPanel)

    def test_ui_mode_default_false(self):
        hp = HierarchyPanel()
        assert hp.get_ui_mode() is False

    def test_ui_mode_property(self):
        hp = HierarchyPanel()
        hp.ui_mode = True
        assert hp.ui_mode is True
        hp.ui_mode = False
        assert hp.ui_mode is False

    def test_set_ui_mode(self):
        hp = HierarchyPanel()
        hp.set_ui_mode(True)
        assert hp.get_ui_mode() is True
        hp.set_ui_mode(False)
        assert hp.get_ui_mode() is False

    def test_clear_search_no_crash(self):
        hp = HierarchyPanel()
        hp.clear_search()

    def test_selection_callbacks(self):
        hp = HierarchyPanel()
        selected = set()
        primary = [0]

        hp.is_selected = lambda oid: oid in selected
        hp.select_id = lambda oid: (selected.clear(), selected.add(oid), primary.__setitem__(0, oid))
        hp.clear_selection = lambda: (selected.clear(), primary.__setitem__(0, 0))
        hp.get_primary = lambda: primary[0]
        hp.get_selected_ids = lambda: list(selected)
        hp.selection_count = lambda: len(selected)
        hp.is_selection_empty = lambda: len(selected) == 0

        assert hp.is_selection_empty()
        hp.select_id(42)
        assert hp.is_selected(42)
        assert hp.get_primary() == 42
        assert hp.selection_count() == 1
        hp.clear_selection()
        assert hp.is_selection_empty()

    def test_native_selection_and_runtime_hidden_snapshots(self):
        hp = HierarchyPanel()
        hp.set_selection_snapshot([10, 20], 20)
        hp.set_runtime_hidden_ids({30, 40})
        hp.set_scene_header_snapshot("Sample *", False, "")

    def test_ui_editor_selection_changed_callback(self):
        hp = HierarchyPanel()
        received = []
        hp.on_selection_changed_ui_editor = lambda oid: received.append(oid)

        # Wire minimal selection so ClearSelectionAndNotify works
        hp.is_selection_empty = lambda: True
        hp.clear_selection = lambda: None
        hp.get_primary = lambda: 0
        hp.clear_selection_and_notify()
        assert received == [0]

    def test_notification_callbacks(self):
        hp = HierarchyPanel()
        double_click = []
        hp.on_double_click_focus = lambda oid: double_click.append(oid)
        # Callback is stored; can't trigger from outside render loop
        assert hp.on_double_click_focus is not None

    def test_undo_callbacks(self):
        hp = HierarchyPanel()
        records = []
        hp.undo_record_create = lambda oid, desc: records.append(("create", oid, desc))
        hp.undo_record_delete = lambda oid, desc: records.append(("delete", oid, desc))
        hp.undo_record_rename = lambda oid, old, new: records.append(("rename", oid, old, new))
        hp.undo_record_move = lambda oid, op, np, oi, ni: records.append(("move", oid, op, np, oi, ni))

        hp.undo_record_create(1, "Create")
        hp.undo_record_delete(2, "Delete")
        hp.undo_record_rename(3, "Old", "New")
        hp.undo_record_move(3, 0, 1, 0, 2)
        assert len(records) == 4
        assert records[0] == ("create", 1, "Create")
        assert records[1] == ("delete", 2, "Delete")
        assert records[2] == ("rename", 3, "Old", "New")
        assert records[3] == ("move", 3, 0, 1, 0, 2)

    def test_scene_info_callbacks(self):
        hp = HierarchyPanel()
        hp.get_scene_display_name = lambda: "TestScene"
        hp.is_prefab_mode = lambda: False
        hp.get_prefab_display_name = lambda: "Prefab: TestPrefab"

        assert hp.get_scene_display_name() == "TestScene"
        assert hp.is_prefab_mode() is False
        assert hp.get_prefab_display_name() == "Prefab: TestPrefab"

    def test_translate_callback(self):
        hp = HierarchyPanel()
        hp.translate = lambda key: f"[{key}]"
        assert hp.translate("hierarchy.search_placeholder") == "[hierarchy.search_placeholder]"

    def test_clipboard_callbacks(self):
        hp = HierarchyPanel()
        hp.copy_selected = lambda cut: True
        hp.paste_clipboard = lambda: True
        hp.has_clipboard_data = lambda: False

        assert hp.copy_selected(False) is True
        assert hp.paste_clipboard() is True
        assert hp.has_clipboard_data() is False

    def test_command_callbacks(self):
        hp = HierarchyPanel()
        calls = []
        hp.execute_command = lambda command_id, source, argument: calls.append(
            (command_id, source, argument)
        ) or True
        hp.can_execute_command = lambda command_id, argument: (
            command_id == "edit.rename" and argument == "42"
        )

        assert hp.execute_command("edit.rename", "context_menu", "42") is True
        assert hp.can_execute_command("edit.rename", "42") is True
        hp.begin_rename_object(0)
        assert calls == [("edit.rename", "context_menu", "42")]

    def test_creation_callbacks(self):
        hp = HierarchyPanel()
        created = []
        hp.create_primitive = lambda t, p: created.append(("prim", t, p))
        hp.create_light = lambda t, p: created.append(("light", t, p))
        hp.create_camera = lambda p: created.append(("cam", p))
        hp.create_render_stack = lambda p: created.append(("rs", p))
        hp.create_empty = lambda p: created.append(("empty", p))

        hp.create_primitive(0, 0)
        hp.create_light(1, 42)
        hp.create_camera(0)
        hp.create_render_stack(7)
        hp.create_empty(0)

        assert len(created) == 5
        assert created[0] == ("prim", 0, 0)
        assert created[1] == ("light", 1, 42)

    def test_canvas_query_callbacks(self):
        hp = HierarchyPanel()
        hp.go_has_canvas = lambda oid: oid == 10
        hp.go_has_ui_screen_component = lambda oid: False
        hp.parent_has_canvas_ancestor = lambda oid: oid > 5
        hp.has_canvas_descendant = lambda oid: oid == 1

        assert hp.go_has_canvas(10) is True
        assert hp.go_has_canvas(11) is False
        assert hp.has_canvas_descendant(1) is True

    def test_delete_callback(self):
        hp = HierarchyPanel()
        deleted = []
        hp.delete_selected_objects = lambda: deleted.append(True)
        hp.delete_selected_objects()
        assert deleted == [True]

    def test_external_drop_callbacks(self):
        hp = HierarchyPanel()
        drops = []
        hp.instantiate_prefab = lambda ref, pid, is_guid: drops.append(("prefab", ref, pid, is_guid))
        hp.create_model_object = lambda ref, pid, is_guid: drops.append(("model", ref, pid, is_guid))

        hp.instantiate_prefab("abc-guid", 0, True)
        hp.create_model_object("/path/to/model.fbx", 42, False)
        assert len(drops) == 2

    def test_prefab_action_callbacks(self):
        hp = HierarchyPanel()
        actions = []
        hp.save_as_prefab = lambda oid: actions.append(("save", oid))
        hp.prefab_select_asset = lambda oid: actions.append(("select", oid))
        hp.prefab_open_asset = lambda oid: actions.append(("open", oid))
        hp.prefab_apply_overrides = lambda oid: actions.append(("apply", oid))
        hp.prefab_revert_overrides = lambda oid: actions.append(("revert", oid))
        hp.prefab_unpack = lambda oid: actions.append(("unpack", oid))

        hp.save_as_prefab(1)
        hp.prefab_unpack(2)
        assert ("save", 1) in actions
        assert ("unpack", 2) in actions

    def test_set_pending_expand_id(self):
        hp = HierarchyPanel()
        hp.set_pending_expand_id(42)
        # No assertion needed — verifies API exists without crash

    def test_expand_to_object_no_crash(self):
        hp = HierarchyPanel()
        hp.expand_to_object(0)  # 0 = no-op
        hp.expand_to_object(99999)  # non-existent — no crash

    def test_set_selected_object_by_id(self):
        hp = HierarchyPanel()
        selected = set()
        hp.select_id = lambda oid: selected.add(oid)
        hp.get_primary = lambda: max(selected) if selected else 0
        hp.selection_count = lambda: len(selected)
        hp.get_selected_ids = lambda: list(selected)
        hp.is_selection_empty = lambda: len(selected) == 0
        hp.set_selected_object_by_id(42)
        assert 42 in selected
