"""Tests for native InspectorPanel.

The panel handles window management, object header, transform rendering,
component headers, tag/layer controls, the splitter, and Add Component.
Component body rendering and mutation intents are delegated to the shared
editor interaction services.
"""
from pathlib import Path

import pytest
from Infernux.lib import (
    InspectorPanel,
    InspectorComponentInfo,
    InspectorObjectInfo,
    InspectorTransformData,
    InspectorAddComponentEntry,
    InspectorPrefabInfo,
)


# ═══════════════════════════════════════════════════════════════════════
#  Creation & EditorPanel contract
# ═══════════════════════════════════════════════════════════════════════

class TestInspectorPanelCreation:

    def test_native_panel_has_no_frame_scoped_undo_callbacks(self):
        source = Path("cpp/infernux/function/editor/InspectorPanel.cpp").read_text(
            encoding="utf-8"
        )
        header = Path("cpp/infernux/function/editor/InspectorPanel.h").read_text(
            encoding="utf-8"
        )
        combined = source + header
        assert "undoBeginFrame" not in combined
        assert "undoEndFrame" not in combined
        assert "undoInvalidateAll" not in combined

    def test_creation(self):
        ip = InspectorPanel()
        assert ip is not None

    def test_is_editor_panel(self):
        from Infernux.lib import EditorPanel
        ip = InspectorPanel()
        assert isinstance(ip, EditorPanel)

    def test_window_id(self):
        ip = InspectorPanel()
        assert ip.get_window_id() == "inspector"

    def test_default_open(self):
        ip = InspectorPanel()
        assert ip.is_open()


# ═══════════════════════════════════════════════════════════════════════
#  Selection API
# ═══════════════════════════════════════════════════════════════════════

class TestInspectorSelection:

    def test_set_selected_object_id(self):
        ip = InspectorPanel()
        ip.set_selected_object_id(42)
        assert ip.get_selected_object_id() == 42

    def test_clear_selected_object(self):
        ip = InspectorPanel()
        ip.set_selected_object_id(99)
        ip.clear_selected_object()
        assert ip.get_selected_object_id() == 0

    def test_set_selected_file(self):
        ip = InspectorPanel()
        ip.set_selected_file("assets/tex.png", "texture")
        assert ip.get_selected_file() == "assets/tex.png"

    def test_clear_selected_file(self):
        ip = InspectorPanel()
        ip.set_selected_file("a.mat", "material")
        ip.clear_selected_file()
        assert ip.get_selected_file() == ""

    def test_set_selected_file_clears_object(self):
        ip = InspectorPanel()
        ip.set_selected_object_id(42)
        ip.set_selected_file("a.mat", "material")
        assert ip.get_selected_object_id() == 0

    def test_set_detail_file(self):
        ip = InspectorPanel()
        ip.set_selected_object_id(42)
        ip.set_detail_file("a.mat", "material")
        # Detail file does NOT clear object
        assert ip.get_selected_object_id() == 42
        assert ip.get_selected_file() == "a.mat"

    def test_component_selection_projection_api(self):
        ip = InspectorPanel()
        ip.set_selected_component_ids([11, 12])
        ip.clear_selected_components()


# ═══════════════════════════════════════════════════════════════════════
#  Data structs
# ═══════════════════════════════════════════════════════════════════════

class TestInspectorDataStructs:

    def test_component_info(self):
        ci = InspectorComponentInfo()
        ci.type_name = "MeshRenderer"
        ci.component_id = 123
        ci.enabled = True
        ci.is_native = True
        ci.is_script = False
        ci.is_broken = False
        assert ci.type_name == "MeshRenderer"
        assert ci.component_id == 123

    def test_object_info(self):
        oi = InspectorObjectInfo()
        oi.name = "Cube"
        oi.active = True
        oi.tag = "Player"
        oi.layer = 3
        oi.prefab_guid = ""
        oi.hide_transform = False
        oi.transform_component_id = 91
        assert oi.name == "Cube"
        assert oi.layer == 3
        assert oi.transform_component_id == 91

    def test_transform_data(self):
        td = InspectorTransformData()
        td.px = 1.0
        td.py_ = 2.0
        td.pz = 3.0
        td.rx = 0.0
        td.ry = 90.0
        td.rz = 0.0
        td.sx = 1.0
        td.sy = 1.0
        td.sz = 1.0
        assert td.px == 1.0
        assert td.py_ == 2.0
        assert td.ry == 90.0

    def test_add_component_entry(self):
        e = InspectorAddComponentEntry()
        e.display_name = "Light"
        e.category = "Built-in"
        e.is_native = True
        e.script_path = ""
        assert e.display_name == "Light"
        assert e.is_native is True

    def test_prefab_info(self):
        pi = InspectorPrefabInfo()
        pi.override_count = 3
        pi.is_readonly = True
        pi.is_transform_readonly = False
        assert pi.override_count == 3
        assert pi.is_readonly is True


# ═══════════════════════════════════════════════════════════════════════
#  Callbacks
# ═══════════════════════════════════════════════════════════════════════

class TestInspectorCallbacks:

    def test_translate_callback(self):
        ip = InspectorPanel()
        ip.translate = lambda key: f"[{key}]"
        assert ip.translate("Inspector") == "[Inspector]"

    def test_is_multi_selection(self):
        ip = InspectorPanel()
        ip.is_multi_selection = lambda: False
        assert ip.is_multi_selection() is False

    def test_get_selected_ids(self):
        ip = InspectorPanel()
        ip.get_selected_ids = lambda: [10, 20, 30]
        assert ip.get_selected_ids() == [10, 20, 30]

    def test_get_object_info(self):
        ip = InspectorPanel()
        def _make_info(obj_id):
            info = InspectorObjectInfo()
            info.name = f"Obj{obj_id}"
            info.active = True
            return info
        ip.get_object_info = _make_info
        result = ip.get_object_info(5)
        assert result.name == "Obj5"

    def test_get_transform_data(self):
        ip = InspectorPanel()
        def _make_td(obj_id):
            td = InspectorTransformData()
            td.px = float(obj_id)
            return td
        ip.get_transform_data = _make_td
        result = ip.get_transform_data(7)
        assert result.px == 7.0

    def test_object_and_transform_mutations_use_global_commands_only(self):
        source = Path("cpp/infernux/function/editor/InspectorPanel.cpp").read_text(
            encoding="utf-8"
        )
        header = Path("cpp/infernux/function/editor/InspectorPanel.h").read_text(
            encoding="utf-8"
        )
        binding = Path("cpp/infernux/tools/pybinding/BindingGUI.cpp").read_text(
            encoding="utf-8"
        )
        bootstrap = Path(
            "python/Infernux/engine/bootstrap_inspector/_wire.py"
        ).read_text(encoding="utf-8")

        assert 'ExecuteEditorCommand("scene.set_object_property"' in source
        assert 'ExecuteEditorCommand("scene.set_transforms"' in source
        assert "MakeObjectPropertyCommandArgument" in source
        assert "MakeTransformValue" in source
        assert "setObjectProperty" not in header
        assert "setTransformData" not in header
        assert "setMultiTransformData" not in header
        assert 'def_readwrite("set_object_property"' not in binding
        assert 'def_readwrite("set_transform_data"' not in binding
        assert 'def_readwrite("set_multi_transform_data"' not in binding
        assert "reorderComponent" not in header
        assert 'def_readwrite("reorder_component"' not in binding
        assert "ip.set_object_property" not in bootstrap
        assert "ip.set_transform_data" not in bootstrap
        assert "ip.set_multi_transform_data" not in bootstrap
        assert "ip.reorder_component" not in bootstrap

    def test_transform_drag_publishes_one_pointer_gesture_transaction(self):
        source = Path("cpp/infernux/function/editor/InspectorPanel.cpp").read_text(
            encoding="utf-8"
        )
        wiring = Path(
            "python/Infernux/engine/_bootstrap_wiring.py"
        ).read_text(encoding="utf-8")

        assert '"gesture_id"' in source
        assert "UpdateTransformGesture" in source
        assert "creates_user_action=False" in wiring

    def test_get_component_list(self):
        ip = InspectorPanel()
        def _get_comps(obj_id):
            ci = InspectorComponentInfo()
            ci.type_name = "Camera"
            ci.component_id = 1
            return [ci]
        ip.get_component_list = _get_comps
        result = ip.get_component_list(1)
        assert len(result) == 1
        assert result[0].type_name == "Camera"

    def test_get_component_icon_id(self):
        ip = InspectorPanel()
        ip.get_component_icon_id = lambda name, is_script: 42
        assert ip.get_component_icon_id("Light", False) == 42

    def test_render_component_body(self):
        ip = InspectorPanel()
        called = []
        ip.render_component_body = lambda ctx, oid, tn, cid, native: called.append((oid, tn))
        ip.render_component_body(None, 1, "Camera", 100, True)
        assert called == [(1, "Camera")]

    def test_component_selection_callback(self):
        ip = InspectorPanel()
        selected = []
        ip.on_component_selection_changed = (
            lambda object_ids, component_ids, native: selected.append(
                (object_ids, component_ids, native)
            )
        )
        ip.on_component_selection_changed([1, 2], [11, 12], True)
        assert selected == [([1, 2], [11, 12], True)]

    def test_get_all_tags(self):
        ip = InspectorPanel()
        ip.get_all_tags = lambda: ["Untagged", "Player", "Enemy"]
        assert ip.get_all_tags() == ["Untagged", "Player", "Enemy"]

    def test_get_all_layers(self):
        ip = InspectorPanel()
        ip.get_all_layers = lambda: ["Default", "UI", "Water"]
        result = ip.get_all_layers()
        assert "Default" in result

    def test_get_add_component_entries(self):
        ip = InspectorPanel()
        def _get_entries():
            e = InspectorAddComponentEntry()
            e.display_name = "Rigidbody"
            e.category = "Physics"
            e.is_native = True
            return [e]
        ip.get_add_component_entries = _get_entries
        result = ip.get_add_component_entries()
        assert len(result) == 1
        assert result[0].display_name == "Rigidbody"

    def test_unified_command_bridge(self):
        ip = InspectorPanel()
        calls = []
        ip.execute_command = lambda command, source, argument: calls.append(
            (command, source, argument)
        ) or True
        ip.can_execute_command = lambda command, argument: bool(command and argument)

        assert ip.execute_command("component.add", "context_menu", "Light\t1\t")
        assert ip.can_execute_command("prefab.apply", "42")
        assert calls == [("component.add", "context_menu", "Light\t1\t")]

    def test_script_drop_targets_are_validated_before_accepting_payload(self):
        source = Path("cpp/infernux/function/editor/InspectorPanel.cpp").read_text(
            encoding="utf-8"
        )
        header = source[
            source.index("InspectorPanel::ComponentHeaderResult InspectorPanel::RenderComponentHeader") :
            source.index("bool InspectorPanel::RenderInspectorCheckbox")
        ]

        assert header.index("if (targetComponentId != 0)") < header.index(
            'AcceptDragDropPayload("SCRIPT_FILE"'
        )
        assert "const bool canDrag = allowComponentReorder && hasInsertionTarget;" in header
        assert "!allowComponentReorder || ImGui::GetMousePos().y" in header
        assert "const bool insertAtStart = !allowComponentReorder;" in header
        assert '"insert_at_start"' in source
        assert "Inspector rejected script component drop" in header

    def test_component_header_overlay_tracks_scroll_and_restores_layout_cursor(self):
        source = Path("cpp/infernux/function/editor/InspectorPanel.cpp").read_text(
            encoding="utf-8"
        )
        header = source[
            source.index("InspectorPanel::ComponentHeaderResult InspectorPanel::RenderComponentHeader") :
            source.index("bool InspectorPanel::RenderInspectorCheckbox")
        ]

        assert "const float contentCursorY = ImGui::GetCursorPosY();" in header
        assert "const float overlayX = ImGui::GetWindowPos().x + indent;" in header
        assert "ImGui::SetCursorScreenPos(ImVec2(overlayX, headerMin.y));" in header
        assert "headerMin.y + (headerHeight - checkboxRowHeight) * 0.5f" in header
        assert "(cbMin.y + cbMax.y) * 0.5f - labelH * 0.5f" in header
        assert "ImGui::SetCursorPosY(contentCursorY);" in header

    def test_multi_component_enabled_toggle_is_one_batch_command(self):
        source = Path("cpp/infernux/function/editor/InspectorPanel.cpp").read_text(
            encoding="utf-8"
        )
        start = source.index("const auto &commonComponents = GetCommonComponentsForMultiSelection")
        end = source.index("// Add Component", start)
        multi_section = source[start:end]

        assert multi_section.count('ExecuteEditorCommand("component.set_enabled"') == 1
        assert "MakeComponentEnabledCommandArgument(ids, entry.componentIds" in multi_section

    def test_prefab_info(self):
        ip = InspectorPanel()
        def _get_pi(oid):
            pi = InspectorPrefabInfo()
            pi.override_count = 2
            return pi
        ip.get_prefab_info = _get_pi
        result = ip.get_prefab_info(1)
        assert result.override_count == 2

    def test_tag_layer_settings_open_through_global_window_command(self):
        source = Path("cpp/infernux/function/editor/InspectorPanel.cpp").read_text(encoding="utf-8")
        header = Path("cpp/infernux/function/editor/InspectorPanel.h").read_text(encoding="utf-8")
        binding = Path("cpp/infernux/tools/pybinding/BindingGUI.cpp").read_text(encoding="utf-8")
        bootstrap = Path("python/Infernux/engine/bootstrap_inspector/_wire.py").read_text(encoding="utf-8")

        assert source.count(
            'ExecuteEditorCommand("window.open", "tag_layer_settings", "pointer")'
        ) == 2
        assert 'if command_id == "window.open":' in bootstrap
        assert "openWindow" not in header
        assert 'def_readwrite("open_window"' not in binding
        assert "ip.open_window" not in bootstrap
