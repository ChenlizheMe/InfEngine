from __future__ import annotations

import threading

from Infernux.mcp import session
from Infernux.mcp.threading import MainThreadCommandQueue
from Infernux.mcp.tools import editor_ui, input as input_tools
from Infernux.mcp.tools.common import ok


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


def _install_main_queue(monkeypatch) -> None:
    queue = MainThreadCommandQueue()
    queue._main_thread_id = threading.get_ident()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)
    monkeypatch.setattr(editor_ui, "_request_fresh_semantic_snapshot", lambda *_args, **_kwargs: True)


def _snapshot(frame: int = 42) -> dict:
    return {
        "capture_enabled": True,
        "frame": frame,
        "snapshot_id": str(frame),
        "mouse": (0.0, 0.0),
        "wants_text_input": False,
        "focused_window": "Hierarchy",
        "focused_window_id": "hierarchy",
        "targets": [
            {
                "id": "tree_node:hierarchy:hierarchy.object.7:101",
                "semantic_id": "hierarchy.object.7",
                "label": "Player",
                "kind": "hierarchy_object",
                "window": "Hierarchy",
                "window_id": "hierarchy",
                "item_id": 101,
                "rect": (16.0, 72.0, 180.0, 24.0),
                "enabled": True,
                "visible": True,
                "active": False,
                "focused": False,
            },
            {
                "id": "text_input:hierarchy:hierarchy.search:102",
                "semantic_id": "hierarchy.search",
                "label": "Search",
                "kind": "hierarchy_search",
                "window": "Hierarchy",
                "window_id": "hierarchy",
                "item_id": 102,
                "rect": (8.0, 34.0, 240.0, 22.0),
                "enabled": True,
                "visible": True,
                "active": False,
                "focused": False,
            },
        ],
    }


def _focused_snapshot(frame: int = 42) -> dict:
    snapshot = _snapshot(frame)
    snapshot["targets"][1]["focused"] = True
    return snapshot


def _focused_numeric_snapshot(frame: int = 42) -> dict:
    snapshot = _snapshot(frame)
    snapshot["targets"][1] = {
        "id": "vector_axis:inspector:inspector.object.7.transform.position.y:202",
        "semantic_id": "inspector.object.7.transform.position.y",
        "label": "Y",
        "kind": "vector_axis",
        "window": "Inspector",
        "window_id": "inspector",
        "item_id": 202,
        "rect": (128.0, 34.0, 76.0, 22.0),
        "enabled": True,
        "visible": True,
        "active": True,
        "focused": True,
    }
    return snapshot


def test_register_editor_ui_tools_leaves_continuous_capture_disabled(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    capture_states = []
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", capture_states.append)

    editor_ui.register_editor_ui_tools(_FakeMcp())

    assert capture_states == [False]


def test_fresh_semantic_snapshot_requests_one_new_rendered_frame(monkeypatch):
    snapshots = iter([_snapshot(42), _snapshot(42), _snapshot(43)])
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(editor_ui, "request_semantic_snapshot", lambda: True)

    assert editor_ui._request_fresh_semantic_snapshot(timeout_seconds=0.1) is True


def test_fresh_semantic_snapshot_waits_for_its_requested_frame(monkeypatch):
    snapshots = iter(
        [
            {**_snapshot(42), "request_sequence": 4},
            {**_snapshot(43), "request_sequence": 5},
            {**_snapshot(44), "request_sequence": 6},
        ]
    )
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(editor_ui, "request_semantic_snapshot", lambda: 6)

    assert editor_ui._request_fresh_semantic_snapshot(timeout_seconds=0.1) is True


def test_editor_ui_snapshot_normalizes_rendered_targets(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot())

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_snapshot"](label="Player")

    assert response["ok"] is True
    target = response["data"]["targets"][0]
    assert target["target_id"] == "tree_node:hierarchy:hierarchy.object.7:101"
    assert target["semantic_id"] == "hierarchy.object.7"
    assert target["rect"] == [16.0, 72.0, 180.0, 24.0]
    assert target["click_point"] == [106.0, 84.0]
    assert target["actions"] == ["click"]


def test_editor_ui_snapshot_explains_native_window_occlusion(tmp_path, monkeypatch):
    session.configure(
        str(tmp_path),
        {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}},
    )
    _install_main_queue(monkeypatch)
    snapshot = _snapshot()
    snapshot["targets"] = [
        {
            "id": "button:animtimeline_editor:animtimeline.toolbar.new:101",
            "semantic_id": "animtimeline.toolbar.new",
            "label": "New",
            "kind": "button",
            "window": "Timeline Editor",
            "window_id": "animtimeline_editor",
            "occluded_by_window": "VFX Graph Editor",
            "occluded_by_window_id": "particle_graph_editor",
            "item_id": 101,
            "rect": (299.0, 92.0, 38.0, 21.0),
            "enabled": False,
            "visible": False,
        }
    ]
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: snapshot)

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_snapshot"](
        semantic_id="animtimeline.toolbar.new",
        visible_only=False,
    )

    assert response["ok"] is True
    target = response["data"]["targets"][0]
    assert target["occluded_by_window"] == "VFX Graph Editor"
    assert target["occluded_by_window_id"] == "particle_graph_editor"
    assert target["actions"] == []


def test_editor_ui_snapshot_exposes_read_only_string_status(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    snapshot = _snapshot()
    snapshot["targets"] = [
        {
            "id": "combo:animfsm_editor:animfsm.toolbar.mode:103",
            "semantic_id": "animfsm.toolbar.mode",
            "label": "Mode",
            "kind": "combo",
            "window": "Animation State Machine Editor",
            "window_id": "animfsm_editor",
            "item_id": 103,
            "rect": (8.0, 34.0, 240.0, 22.0),
            "enabled": True,
            "visible": True,
            "active": False,
            "focused": False,
            "value": "3d",
        },
        {
            "id": "status:animfsm_editor:animfsm.document.path:103",
            "semantic_id": "animfsm.document.path",
            "label": "Asset Path",
            "kind": "status",
            "window": "Animation State Machine Editor",
            "window_id": "animfsm_editor",
            "item_id": 103,
            "rect": (8.0, 34.0, 240.0, 22.0),
            "enabled": True,
            "visible": True,
            "active": False,
            "focused": False,
            "value": "Assets/Locomotion.animfsm",
        },
        {
            "id": "status:animfsm_editor:animfsm.document.dirty:103",
            "semantic_id": "animfsm.document.dirty",
            "label": "Dirty",
            "kind": "status",
            "window": "Animation State Machine Editor",
            "window_id": "animfsm_editor",
            "item_id": 103,
            "rect": (8.0, 34.0, 240.0, 22.0),
            "enabled": True,
            "visible": True,
            "active": False,
            "focused": False,
            "value": False,
        },
    ]
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: snapshot)

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_snapshot"](semantic_id="animfsm.document.path")

    target = response["data"]["targets"][0]
    assert target["value_available"] is True
    assert target["value"] == "Assets/Locomotion.animfsm"
    assert target["actions"] == []
    all_targets = editor_ui._coalesce_targets(
        [editor_ui._normalize_target(value) for value in snapshot["targets"]]
    )
    assert {target["semantic_id"] for target in all_targets} == {
        "animfsm.toolbar.mode",
        "animfsm.document.path",
        "animfsm.document.dirty",
    }


def test_editor_ui_snapshot_does_not_expose_or_depend_on_window_presentation(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot())

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    assert "editor_ui_restore_window" not in fake.tools
    response = fake.tools["editor_ui_snapshot"]()

    assert response["ok"] is True
    assert response["data"]["ready"] is True
    assert "window_state" not in response["data"]
    assert len(response["data"]["targets"]) == 2
    assert response["data"]["rendered_target_count"] == 2


def test_editor_ui_click_routes_through_internal_input_without_window_state(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot())
    clicks = []
    monkeypatch.setattr(
        input_tools,
        "perform_pointer_click",
        lambda *args, **kwargs: clicks.append((args, kwargs)) or ok({"delivered": True}),
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_click"](
        "tree_node:hierarchy:hierarchy.object.7:101",
        "42",
    )

    assert response["ok"] is True
    assert len(clicks) == 1


def test_editor_ui_snapshot_explains_when_optional_filters_remove_every_target(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot())

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_snapshot"](label="initial editor state")

    assert response["ok"] is True
    assert response["data"]["rendered_target_count"] == 2
    assert response["data"]["matching_target_count"] == 0
    assert response["data"]["filters"]["label_contains"] == "initial editor state"
    assert "not a snapshot title" in response["data"]["empty_match_hint"]


def test_editor_ui_snapshot_without_optional_filters_returns_every_target(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot())

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_snapshot"]()

    assert response["ok"] is True
    assert response["data"]["matching_target_count"] == 2
    assert len(response["data"]["targets"]) == 2
    assert "empty_match_hint" not in response["data"]


def test_editor_ui_snapshot_filters_by_exact_semantic_id(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot())

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_snapshot"](semantic_id="hierarchy.search")

    assert response["ok"] is True
    assert [target["semantic_id"] for target in response["data"]["targets"]] == ["hierarchy.search"]


def test_editor_ui_drag_resolves_both_semantic_targets(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    resolved = {
        "source": {"found": True, "target_id": "source", "center_x": 10.0, "center_y": 20.0},
        "destination": {"found": True, "target_id": "destination", "center_x": 110.0, "center_y": 220.0},
    }
    monkeypatch.setattr(editor_ui, "_resolve_target", lambda target_id, _snapshot_id: resolved[target_id])
    monkeypatch.setattr(editor_ui, "_read_interaction_observation", lambda: {})
    monkeypatch.setattr(editor_ui, "_wait_for_post_action_observation", lambda *_args, **_kwargs: {"frame": 44})
    calls = []
    monkeypatch.setattr(
        input_tools,
        "perform_pointer_drag",
        lambda *args, **kwargs: calls.append((args, kwargs)) or ok({"delivered": True}),
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_drag"](
        "source", "42", destination_target_id="destination", destination_snapshot_id="42", steps=6,
        button="middle",
    )

    assert response["ok"] is True
    assert calls == [
        ((10.0, 20.0, 110.0, 220.0), {
            "button": 2,
            "steps": 6,
            "timeout_seconds": 5.0,
            "trace_name": "editor_ui_drag",
            "expected_target_id": "source",
        })
    ]
    assert response["data"]["button"] == 2
    assert response["data"]["action_path"] == "synthetic_sdl_semantic_pointer_drag"


def test_editor_ui_wait_for_target_accepts_snapshot_visibility_filter(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    calls: list[bool] = []

    def _payload(*, visible_only, **_kwargs):
        calls.append(visible_only)
        return {"snapshot_id": "43", "targets": [{"semantic_id": "hidden.target"}]}

    monkeypatch.setattr(editor_ui, "_snapshot_payload", _payload)
    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_wait_for_target"](label="hidden", visible_only=False)

    assert response["ok"] is True
    assert calls == [False]


def test_editor_ui_wait_for_window_focus_accepts_a_focused_child_window(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    snapshots = iter(
        [
            {
                "capture_enabled": True,
                "frame": 42,
                "snapshot_id": "42",
                "focused_window": "VFX Graph Editor",
                "focused_window_id": "particle_graph_editor",
            },
            {
                "capture_enabled": True,
                "frame": 43,
                "snapshot_id": "43",
                "focused_window": "Animation State Machine Editor",
                "focused_window_id": "animfsm_editor/##fsm_graph_region",
            },
        ]
    )
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: next(snapshots))

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_wait_for_window_focus"](
        "animfsm_editor",
        timeout_seconds=0.5,
        poll_interval=0.001,
    )

    assert response["ok"] is True
    assert response["data"] == {
        "window_id": "animfsm_editor",
        "ready": True,
        "capture_enabled": True,
        "snapshot_id": "43",
        "frame": 43,
        "focused_window": "Animation State Machine Editor",
        "focused_window_id": "animfsm_editor/##fsm_graph_region",
    }


def test_editor_ui_open_menu_keeps_an_already_open_menu_visible(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    menu_item = {
        "target_id": "menu_item:menu.project:menu.project.save_scene:302",
        "semantic_id": "menu.project.save_scene",
        "label": "Save Scene",
        "kind": "menu_item",
        "rect": [12.0, 42.0, 160.0, 20.0],
        "visible": True,
        "enabled": True,
    }
    calls: list[tuple] = []

    monkeypatch.setattr(
        editor_ui,
        "_snapshot_payload",
        lambda *, semantic_id, **_kwargs: {"snapshot_id": "42", "targets": [menu_item] if semantic_id == menu_item["semantic_id"] else []},
    )
    monkeypatch.setattr(
        input_tools,
        "perform_pointer_click",
        lambda *args, **kwargs: calls.append((args, kwargs)) or ok({"delivered": True}),
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_open_menu"]("menu.project", "menu.project.save_scene")

    assert response["ok"] is True
    assert response["data"]["already_open"] is True
    assert response["data"]["item"] == menu_item
    assert response["data"]["snapshot_id"] == "42"
    assert calls == []


def test_editor_ui_open_menu_clicks_once_then_waits_for_item(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    menu_target = {
        "target_id": "menu:main:menu.project:301",
        "semantic_id": "menu.project",
        "label": "Project",
        "kind": "menu",
        "rect": [80.0, 0.0, 60.0, 24.0],
        "visible": True,
        "enabled": True,
    }
    menu_item = {
        "target_id": "menu_item:menu.project:menu.project.save_scene:302",
        "semantic_id": "menu.project.save_scene",
        "label": "Save Scene",
        "kind": "menu_item",
        "rect": [80.0, 24.0, 160.0, 20.0],
        "visible": True,
        "enabled": True,
    }
    snapshot_calls: list[str] = []
    click_calls: list[tuple] = []

    def _payload(*, semantic_id, **_kwargs):
        snapshot_calls.append(semantic_id)
        if semantic_id == "menu.project":
            return {"snapshot_id": "43", "targets": [menu_target]}
        if snapshot_calls.count("menu.project.save_scene") == 1:
            return {"snapshot_id": "42", "targets": []}
        return {"snapshot_id": "44", "targets": [menu_item]}

    monkeypatch.setattr(editor_ui, "_snapshot_payload", _payload)
    monkeypatch.setattr(
        editor_ui,
        "_resolve_target",
        lambda target_id, snapshot_id: {
            "found": True,
            "target_id": target_id,
            "snapshot_id": snapshot_id,
            "center_x": 110.0,
            "center_y": 12.0,
        },
    )
    monkeypatch.setattr(
        input_tools,
        "perform_pointer_click",
        lambda x, y, **kwargs: click_calls.append((x, y, kwargs)) or ok({"delivered": True}),
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_open_menu"]("menu.project", "menu.project.save_scene")

    assert response["ok"] is True
    assert response["data"]["already_open"] is False
    assert response["data"]["item"] == menu_item
    assert response["data"]["snapshot_id"] == "44"
    assert snapshot_calls == ["menu.project.save_scene", "menu.project", "menu.project.save_scene"]
    assert click_calls == [
        (
            110.0,
            12.0,
            {
                "button": 0,
                "timeout_seconds": 3.0,
                "trace_name": "editor_ui_open_menu",
                "expected_target_id": "menu:main:menu.project:301",
            },
        ),
    ]


def test_editor_ui_open_menu_does_not_confuse_window_with_menu_item(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    menu_target = {
        "target_id": "menu:main:menu.Animation:101",
        "semantic_id": "menu.Animation",
        "label": "Animation",
        "kind": "menu",
        "rect": [40.0, 0.0, 80.0, 24.0],
        "visible": True,
        "enabled": True,
    }
    menu_item = {
        "target_id": "menu_item:menu.Animation:animfsm_editor:102",
        "semantic_id": "animfsm_editor",
        "label": "Animation State Machine Editor",
        "kind": "menu_item",
        "rect": [40.0, 24.0, 220.0, 20.0],
        "visible": True,
        "enabled": True,
    }
    snapshot_calls: list[tuple[str, str]] = []
    click_calls: list[tuple] = []

    def _payload(*, semantic_id, kind, **_kwargs):
        snapshot_calls.append((semantic_id, kind))
        if semantic_id == "menu.Animation":
            return {"snapshot_id": "51", "targets": [menu_target]}
        if len(snapshot_calls) == 1:
            return {"snapshot_id": "50", "targets": []}
        return {"snapshot_id": "52", "targets": [menu_item]}

    monkeypatch.setattr(editor_ui, "_snapshot_payload", _payload)
    monkeypatch.setattr(
        editor_ui,
        "_resolve_target",
        lambda target_id, snapshot_id: {
            "found": True,
            "target_id": target_id,
            "snapshot_id": snapshot_id,
            "center_x": 80.0,
            "center_y": 12.0,
        },
    )
    monkeypatch.setattr(
        input_tools,
        "perform_pointer_click",
        lambda x, y, **kwargs: click_calls.append((x, y, kwargs)) or ok({"delivered": True}),
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_open_menu"]("menu.Animation", "animfsm_editor")

    assert response["ok"] is True
    assert response["data"]["already_open"] is False
    assert response["data"]["item"] == menu_item
    assert snapshot_calls == [
        ("animfsm_editor", "menu_item"),
        ("menu.Animation", "menu"),
        ("animfsm_editor", "menu_item"),
    ]
    assert click_calls


def test_editor_ui_click_refreshes_target_and_uses_sdl_click_path(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot(frame=43))
    calls: list[tuple] = []

    def _click(x, y, *, button, timeout_seconds, trace_name, expected_target_id):
        calls.append((x, y, button, timeout_seconds, trace_name, expected_target_id))
        return ok({"release_sequence": 3, "delivered": True})

    monkeypatch.setattr(input_tools, "perform_pointer_click", _click)
    monkeypatch.setattr(
        editor_ui,
        "_wait_for_post_action_observation",
        lambda before, **_kwargs: {"snapshot_id": "44", "focused_window_id": "scene_save_as", "ui_changed": True},
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_click"]("tree_node:hierarchy:hierarchy.object.7:101", "42")

    assert response["ok"] is True
    assert response["data"]["target"]["snapshot_refreshed"] is True
    assert response["data"]["post_action"]["focused_window_id"] == "scene_save_as"
    assert response["data"]["post_action"]["ui_changed"] is True
    assert calls == [(106.0, 84.0, 0, 3.0, "editor_ui_click", "tree_node:hierarchy:hierarchy.object.7:101")]


def test_editor_ui_click_uses_native_reachability_checked_point(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    snapshot = _snapshot(frame=43)
    snapshot["targets"][0]["has_click_point"] = True
    snapshot["targets"][0]["click_point"] = (32.0, 91.0)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: snapshot)
    calls: list[tuple] = []

    monkeypatch.setattr(
        input_tools,
        "perform_pointer_click",
        lambda x, y, *, button, timeout_seconds, trace_name, expected_target_id: calls.append(
            (x, y, button, timeout_seconds, trace_name, expected_target_id)
        )
        or ok({"release_sequence": 3, "delivered": True}),
    )
    monkeypatch.setattr(editor_ui, "_wait_for_post_action_observation", lambda before, **_kwargs: {"ui_changed": False})

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_click"]("tree_node:hierarchy:hierarchy.object.7:101", "42")

    assert response["ok"] is True
    assert response["data"]["target"]["click_point"] == [32.0, 91.0]
    assert response["data"]["target"]["click_x"] == 32.0
    assert response["data"]["target"]["click_y"] == 91.0
    assert calls == [(32.0, 91.0, 0, 3.0, "editor_ui_click", "tree_node:hierarchy:hierarchy.object.7:101")]


def test_post_action_observation_reports_delayed_modal_and_semantics(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(editor_ui.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(editor_ui.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))

    modal_target = {
        "id": "text_input:scene_save_as:scene.save_as.name:1",
        "semantic_id": "scene.save_as.name",
        "label": "Name",
        "kind": "text_input",
        "window": "Save Scene As",
        "window_id": "scene_save_as",
        "item_id": 1,
        "rect": (100.0, 100.0, 200.0, 20.0),
        "visible": True,
        "enabled": True,
    }
    monkeypatch.setattr(
        editor_ui,
        "_read_native_snapshot",
        lambda: {
            "capture_enabled": True,
            "snapshot_id": "44",
            "frame": 44,
            "focused_window": "Save Scene As",
            "focused_window_id": "scene_save_as",
            "wants_text_input": True,
            "targets": [modal_target],
        },
    )
    before = {
        "snapshot_id": "43",
        "frame": 43,
        "focused_window_id": "menu_project",
        "semantic_ids": ["menu.project.save_scene_as"],
        "target_ids": ["menu_item:menu_project:menu.project.save_scene_as:2"],
    }

    result = editor_ui._wait_for_post_action_observation(
        before,
        source_target_id="menu_item:menu_project:menu.project.save_scene_as:2",
        timeout_seconds=0.05,
    )

    assert result["snapshot_id"] == "44"
    assert result["focused_window_id"] == "scene_save_as"
    assert result["wants_text_input"] is True
    assert result["ui_changed"] is True
    assert result["source_target_still_rendered"] is False
    assert result["new_semantic_ids"] == ["scene.save_as.name"]
    assert result["removed_semantic_ids"] == ["menu.project.save_scene_as"]
    assert result["focused_window_targets"] == [
        {
            "target_id": "text_input:scene_save_as:scene.save_as.name:1",
            "semantic_id": "scene.save_as.name",
            "label": "Name",
            "kind": "text_input",
        }
    ]
    assert result["effect_completion"] is False


def test_editor_ui_double_click_waits_for_a_new_frame_and_retargets_before_second_click(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    calls: list[tuple] = []
    target = {
        "target_id": "tree_node:hierarchy:hierarchy.object.7:101",
        "semantic_id": "hierarchy.object.7",
        "label": "Player",
        "kind": "hierarchy_object",
        "window": "Hierarchy",
        "window_id": "hierarchy",
    }
    snapshots = iter([
        {"snapshot_id": "43", "targets": [target]},
        {"snapshot_id": "44", "targets": [target]},
    ])
    monkeypatch.setattr(editor_ui, "_snapshot_payload", lambda **_kwargs: next(snapshots))

    def _click(x, y, *, button, timeout_seconds, trace_name, expected_target_id):
        calls.append((x, y, button, timeout_seconds, trace_name, expected_target_id))
        return ok({"release_sequence": len(calls) * 3, "delivered": True})

    monkeypatch.setattr(input_tools, "perform_pointer_click", _click)
    resolve_calls: list[tuple[str, str]] = []

    def _resolve(target_id, snapshot_id):
        resolve_calls.append((target_id, snapshot_id))
        current_snapshot_id = "43" if len(resolve_calls) == 1 else snapshot_id
        return {
            "found": True,
            "target_id": target_id,
            "snapshot_id": current_snapshot_id,
            "semantic_id": "hierarchy.object.7",
            "label": "Player",
            "kind": "hierarchy_object",
            "window": "Hierarchy",
            "window_id": "hierarchy",
            "center_x": 106.0,
            "center_y": 84.0,
            "snapshot_refreshed": True,
        }

    monkeypatch.setattr(
        editor_ui,
        "_resolve_target",
        _resolve,
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_double_click"]("tree_node:hierarchy:hierarchy.object.7:101", "42")

    assert response["ok"] is True
    assert response["data"]["target"]["snapshot_refreshed"] is True
    assert response["data"]["action_path"] == "synthetic_sdl_pointer_double_click"
    assert response["data"]["frame_barrier"]["snapshot_id"] == "44"
    assert resolve_calls == [
        ("tree_node:hierarchy:hierarchy.object.7:101", "42"),
        ("tree_node:hierarchy:hierarchy.object.7:101", "44"),
    ]
    assert calls == [
        (106.0, 84.0, 0, 3.0, "editor_ui_double_click.first", "tree_node:hierarchy:hierarchy.object.7:101"),
        (106.0, 84.0, 0, 3.0, "editor_ui_double_click.second", "tree_node:hierarchy:hierarchy.object.7:101"),
    ]


def test_editor_ui_double_click_does_not_send_second_click_without_a_new_frame(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    target = {
        "target_id": "tree_node:hierarchy:hierarchy.object.7:101",
        "semantic_id": "hierarchy.object.7",
        "label": "Player",
        "kind": "hierarchy_object",
        "window": "Hierarchy",
        "window_id": "hierarchy",
    }
    monkeypatch.setattr(editor_ui, "_snapshot_payload", lambda **_kwargs: {"snapshot_id": "43", "targets": [target]})
    calls: list[tuple] = []
    monkeypatch.setattr(
        editor_ui,
        "_resolve_target",
        lambda target_id, snapshot_id: {
            "found": True,
            "target_id": target_id,
            "snapshot_id": "43",
            "semantic_id": "hierarchy.object.7",
            "label": "Player",
            "kind": "hierarchy_object",
            "window": "Hierarchy",
            "window_id": "hierarchy",
            "center_x": 106.0,
            "center_y": 84.0,
        },
    )
    monkeypatch.setattr(
        input_tools,
        "perform_pointer_click",
        lambda x, y, **kwargs: calls.append((x, y, kwargs)) or ok({"delivered": True}),
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_double_click"](
        "tree_node:hierarchy:hierarchy.object.7:101",
        "42",
        timeout_seconds=0.01,
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "error.double_click_frame_barrier"
    assert len(calls) == 1


def test_normalized_checkbox_exposes_value_and_idempotent_action():
    target = editor_ui._normalize_target({
        "id": "checkbox:inspector:audio.play_on_awake:1",
        "semantic_id": "audio.play_on_awake",
        "label": "Play On Awake",
        "kind": "checkbox",
        "window": "Inspector",
        "window_id": "inspector",
        "item_id": 1,
        "rect": (10.0, 20.0, 100.0, 20.0),
        "enabled": True,
        "visible": True,
        "value": False,
    })

    assert target["value_available"] is True
    assert target["value"] is False
    assert target["actions"] == ["click", "set_checked"]


def test_normalized_numeric_axis_exposes_read_only_value():
    target = editor_ui._normalize_target({
        "id": "vector_axis:inspector:inspector.object.41.transform.position.x:2",
        "semantic_id": "inspector.object.41.transform.position.x",
        "label": "X",
        "kind": "vector_axis",
        "window": "Inspector",
        "window_id": "inspector",
        "item_id": 2,
        "rect": (10.0, 20.0, 100.0, 20.0),
        "enabled": True,
        "visible": True,
        "value": 3.25,
    })

    assert target["value_available"] is True
    assert target["value"] == 3.25
    assert target["actions"] == ["click", "focus", "input_text"]


def test_editor_ui_set_checkbox_only_clicks_when_value_differs(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    target = {
        "found": True,
        "target_id": "checkbox:inspector:audio.play_on_awake:1",
        "snapshot_id": "42",
        "semantic_id": "audio.play_on_awake",
        "label": "Play On Awake",
        "kind": "checkbox",
        "window": "Inspector",
        "window_id": "inspector",
        "center_x": 60.0,
        "center_y": 30.0,
        "value_available": True,
        "value": False,
    }
    monkeypatch.setattr(editor_ui, "_resolve_target", lambda *_args: dict(target))
    clicks = []
    monkeypatch.setattr(
        input_tools,
        "perform_pointer_click",
        lambda *args, **kwargs: clicks.append((args, kwargs)) or ok({"delivered": True}),
    )
    monkeypatch.setattr(
        editor_ui,
        "_wait_for_checkbox_state",
        lambda _original, desired, **_kwargs: {"value_available": True, "value": desired},
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    no_op = fake.tools["editor_ui_set_checkbox"](target["target_id"], "42", False)
    changed = fake.tools["editor_ui_set_checkbox"](target["target_id"], "42", True)

    assert no_op["ok"] is True
    assert no_op["data"]["changed"] is False
    assert changed["ok"] is True
    assert changed["data"]["changed"] is True
    assert len(clicks) == 1


def test_editor_ui_focus_rejects_non_text_target(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot())

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_focus"]("tree_node:hierarchy:hierarchy.object.7:101", "42")

    assert response["ok"] is False
    assert response["error"]["code"] == "error.invalid_target"


def test_editor_ui_hover_resolves_target_without_clicking(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _snapshot())
    calls: list[tuple] = []

    def _move(x, y, *, timeout_seconds, trace_name):
        calls.append((x, y, timeout_seconds, trace_name))
        return ok({"sequence": 5, "delivered": True})

    monkeypatch.setattr(input_tools, "perform_pointer_move", _move)

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_hover"]("tree_node:hierarchy:hierarchy.object.7:101", "42")

    assert response["ok"] is True
    assert response["data"]["action_path"] == "synthetic_sdl_pointer_move"
    assert calls == [(106.0, 84.0, 3.0, "editor_ui_hover")]


def test_editor_ui_replace_text_uses_focus_select_all_and_sdl_text(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _focused_snapshot())
    calls: list[tuple] = []

    monkeypatch.setattr(
        input_tools,
        "perform_pointer_click",
        lambda x, y, **kwargs: calls.append(("focus", x, y, kwargs)) or ok({"delivered": True}),
    )
    monkeypatch.setattr(
        input_tools,
        "perform_key_chord",
        lambda keys, **kwargs: calls.append(("chord", keys, kwargs)) or ok({"delivered": True}),
    )
    monkeypatch.setattr(
        input_tools,
        "perform_text_input",
        lambda text, **kwargs: calls.append(("text", text, kwargs)) or ok({"delivered": True}),
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_replace_text"]("text_input:hierarchy:hierarchy.search:102", "42", "Racer")

    assert response["ok"] is True
    assert response["data"]["action_path"] == "synthetic_sdl_pointer_and_keyboard"
    assert calls == [
        (
            "focus",
            128.0,
            45.0,
            {
                "button": 0,
                "timeout_seconds": 3.0,
                "trace_name": "editor_ui_replace_text.focus",
                "expected_target_id": "text_input:hierarchy:hierarchy.search:102",
            },
        ),
        ("chord", ["Left Ctrl", "A"], {"timeout_seconds": 3.0, "trace_name": "editor_ui_replace_text.select_all"}),
        ("text", "Racer", {"timeout_seconds": 3.0, "trace_name": "editor_ui_replace_text.type"}),
    ]


def test_editor_ui_replace_text_ctrl_clicks_numeric_vector_axis(tmp_path, monkeypatch):
    session.configure(str(tmp_path), {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}})
    _install_main_queue(monkeypatch)
    monkeypatch.setattr(editor_ui, "set_semantic_capture_enabled", lambda enabled: True)
    monkeypatch.setattr(editor_ui, "_read_native_snapshot", lambda: _focused_numeric_snapshot())
    calls: list[tuple] = []

    monkeypatch.setattr(
        input_tools,
        "perform_modifier_pointer_click",
        lambda modifier, x, y, **kwargs: calls.append(("focus", modifier, x, y, kwargs)) or ok({"delivered": True}),
    )
    monkeypatch.setattr(
        input_tools,
        "perform_key_transition",
        lambda key, pressed, **kwargs: calls.append(("release", key, pressed, kwargs)) or ok({"delivered": True}),
    )
    monkeypatch.setattr(
        input_tools,
        "perform_key_chord",
        lambda keys, **kwargs: calls.append(("chord", keys, kwargs)) or ok({"delivered": True}),
    )
    monkeypatch.setattr(
        input_tools,
        "perform_text_input",
        lambda text, **kwargs: calls.append(("text", text, kwargs)) or ok({"delivered": True}),
    )

    fake = _FakeMcp()
    editor_ui.register_editor_ui_tools(fake)
    response = fake.tools["editor_ui_replace_text"](
        "vector_axis:inspector:inspector.object.7.transform.position.y:202",
        "42",
        "3.5",
    )

    assert response["ok"] is True
    assert response["data"]["action_path"] == "synthetic_sdl_modifier_pointer_and_keyboard"
    assert calls == [
        (
            "focus",
            "Left Ctrl",
            166.0,
            45.0,
            {
                "button": 0,
                "timeout_seconds": 3.0,
                    "trace_name": "editor_ui_replace_text.focus",
                    "keep_modifier_pressed": True,
                    "expected_target_id": "vector_axis:inspector:inspector.object.7.transform.position.y:202",
                },
        ),
        (
            "release",
            "Left Ctrl",
            False,
            {"timeout_seconds": 3.0, "trace_name": "editor_ui_replace_text.focus.modifier_release"},
        ),
        ("chord", ["Left Ctrl", "A"], {"timeout_seconds": 3.0, "trace_name": "editor_ui_replace_text.select_all"}),
        ("text", "3.5", {"timeout_seconds": 3.0, "trace_name": "editor_ui_replace_text.type"}),
    ]


def test_editor_ui_snapshot_prefers_semantic_target_when_item_id_is_zero():
    generic = {
        "target_id": "vector:inspector:Position:0",
        "semantic_id": "",
        "window_id": "inspector",
        "item_id": 0,
        "rect": [20.0, 30.0, 100.0, 20.0],
    }
    semantic = {
        "target_id": "inspector_transform:inspector:position:0",
        "semantic_id": "inspector.object.7.transform.position",
        "window_id": "inspector",
        "item_id": 0,
        "rect": [20.0, 30.0, 100.0, 20.0],
    }

    targets = editor_ui._coalesce_targets([generic, semantic])

    assert targets == [semantic]


def test_editor_ui_snapshot_prefers_later_domain_alias_for_same_item():
    generic = {
        "target_id": "combo:inspector:texture_type:41",
        "semantic_id": "texture_type",
        "window_id": "inspector",
        "item_id": 41,
        "rect": [20.0, 30.0, 100.0, 20.0],
    }
    domain_alias = {
        "target_id": "combo:inspector:asset.texture.import.texture_type:41",
        "semantic_id": "asset.texture.import.texture_type",
        "window_id": "inspector",
        "item_id": 41,
        "rect": [20.0, 30.0, 100.0, 20.0],
    }

    targets = editor_ui._coalesce_targets([generic, domain_alias])

    assert targets == [domain_alias]
