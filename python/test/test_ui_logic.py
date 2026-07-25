"""Pure-logic tests for the editor UI layer (no ImGui frame required).

Covers: theme color math + C++ SSOT override, viewport math, window manager
state machine, panel state persistence, scene-view math helpers, UI-editor
geometry, and selection manager behavior.
"""
from __future__ import annotations

import math
import os

import pytest

from Infernux.engine.ui.theme import Theme, srgb_to_linear, srgb3, hex_to_linear


# ── theme color utilities ────────────────────────────────────────────────

def test_default_editor_and_game_ui_font_size_is_18px():
    import inspect

    from Infernux.engine.engine import Engine
    from Infernux.ui import UIButton, UIText

    assert Theme.UI_DEFAULT_FONT_SIZE == 18.0
    assert UIText().font_size == 18.0
    assert UIButton().font_size == 18.0
    assert inspect.signature(Engine.set_gui_font).parameters["font_size"].default == 18


def test_engine_gui_registration_forwards_overlay_priority():
    from types import SimpleNamespace

    from Infernux.engine.engine import Engine

    calls = []
    engine = Engine.__new__(Engine)
    engine._engine = SimpleNamespace(
        register_gui_renderable=lambda name, renderable, priority: calls.append(
            (name, renderable, priority)
        )
    )
    engine._gui_objects = {}
    renderable = object()

    engine.register_gui("global_overlay", renderable, priority=1000)

    assert calls == [("global_overlay", renderable, 1000)]
    assert engine._gui_objects["global_overlay"] is renderable


class TestThemeColorMath:
    def test_srgb_to_linear_low_segment(self):
        assert srgb_to_linear(0.0) == 0.0
        assert srgb_to_linear(0.04045) == pytest.approx(0.04045 / 12.92)

    def test_srgb_to_linear_power_segment(self):
        assert srgb_to_linear(1.0) == pytest.approx(1.0)
        assert srgb_to_linear(0.5) == pytest.approx(((0.5 + 0.055) / 1.055) ** 2.4)

    def test_srgb_to_linear_monotonic(self):
        samples = [srgb_to_linear(i / 20.0) for i in range(21)]
        assert all(b >= a for a, b in zip(samples, samples[1:]))

    def test_srgb3_keeps_alpha(self):
        r, g, b, a = srgb3(1.0, 0.5, 0.25, 0.7)
        assert a == 0.7
        assert r == pytest.approx(1.0)

    def test_hex_to_linear(self):
        r, g, b, a = hex_to_linear(255, 0, 255)
        assert r == pytest.approx(1.0)
        assert g == pytest.approx(0.0)
        assert b == pytest.approx(1.0)
        assert a == 1.0


class TestThemeNativeSSOT:
    """The C++ EditorThemeRegistry is the single source of truth."""

    def test_native_registry_exposed(self):
        from Infernux.lib import (
            get_editor_theme_colors,
            get_editor_theme_floats,
            get_editor_theme_vec2s,
        )
        colors = get_editor_theme_colors()
        floats = get_editor_theme_floats()
        assert len(colors) >= 90, "expected the full migrated color table"
        assert len(floats) >= 50
        assert isinstance(get_editor_theme_vec2s(), dict)

    def test_python_theme_overridden_from_native(self):
        from Infernux.lib import get_editor_theme_colors
        native = get_editor_theme_colors()
        assert getattr(Theme, "_NATIVE_OVERRIDES_APPLIED", 0) > 0
        # Every overlapping constant must match the native value exactly.
        mismatches = []
        for name, value in native.items():
            if hasattr(Theme, name):
                if tuple(getattr(Theme, name)) != tuple(value):
                    mismatches.append(name)
        assert mismatches == []

    def test_native_values_are_rgba(self):
        from Infernux.lib import get_editor_theme_colors
        for name, value in get_editor_theme_colors().items():
            assert len(value) == 4, name
            assert all(isinstance(c, float) for c in value), name

    def test_play_border_color_logic(self):
        playing = Theme.get_play_border_color(False)
        paused = Theme.get_play_border_color(True)
        assert playing == tuple(Theme.BORDER_PLAY)
        assert paused == tuple(Theme.BORDER_PAUSE)
        assert playing != paused


# ── viewport math ────────────────────────────────────────────────────────

class _FakeCtx:
    """Minimal stand-in exposing the mouse-position API ViewportInfo uses."""

    def __init__(self, x: float, y: float):
        self._x, self._y = x, y

    def get_mouse_pos_x(self) -> float:
        return self._x

    def get_mouse_pos_y(self) -> float:
        return self._y


class TestViewportInfo:
    def _vp(self):
        from Infernux.engine.ui.viewport_utils import ViewportInfo
        return ViewportInfo(image_min_x=100, image_min_y=50,
                            image_max_x=300, image_max_y=250)

    def test_dimensions(self):
        vp = self._vp()
        assert vp.width == 200 and vp.height == 200

    def test_mouse_local(self):
        vp = self._vp()
        assert vp.mouse_local(_FakeCtx(150, 75)) == (50, 25)

    def test_mouse_inside_boundaries(self):
        vp = self._vp()
        assert vp.is_mouse_inside(_FakeCtx(100, 50))      # top-left corner
        assert vp.is_mouse_inside(_FakeCtx(300, 250))     # bottom-right corner
        assert not vp.is_mouse_inside(_FakeCtx(99, 50))
        assert not vp.is_mouse_inside(_FakeCtx(301, 250))


# ── window manager state machine ─────────────────────────────────────────

class TestWindowManager:
    def _fresh_manager(self):
        from Infernux.engine.ui.window_manager import WindowManager
        mgr = WindowManager.instance()
        return mgr

    def test_singleton(self):
        from Infernux.engine.ui.window_manager import WindowManager
        assert WindowManager.instance() is WindowManager.instance()

    def test_window_type_listener_fires_only_on_registration(self):
        from Infernux.engine.ui.window_manager import WindowManager

        previous = WindowManager._instance
        try:
            manager = WindowManager(object())
            calls = []
            listener = lambda: calls.append(tuple(manager.get_registered_types()))
            manager.add_type_change_listener(listener)
            manager.register_window_type("sample", object, "Sample")
            manager.remove_type_change_listener(listener)
            manager.register_window_type("second", object, "Second")
            assert calls == [("sample",)]
        finally:
            WindowManager._instance = previous

    def test_explicit_dynamic_window_state_machine(self):
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.registered = {}
                self.focused = []

            def register_gui(self, window_id, instance):
                self.registered[window_id] = instance

            def unregister_gui(self, window_id):
                self.registered.pop(window_id)

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        class Panel:
            def __init__(self):
                self._is_open = False

            @property
            def is_open(self):
                return self._is_open

            def set_window_manager(self, manager):
                self.manager = manager

            def open(self):
                self._is_open = True

        previous = WindowManager._instance
        try:
            engine = Engine()
            manager = WindowManager(engine)
            manager.register_window_type("dynamic", Panel, "Dynamic", factory=Panel)
            panel = manager.open_window("dynamic")
            assert manager.get_window_state("dynamic") is WindowState.OPENING
            manager.process_pending_actions()
            assert manager.get_window_state("dynamic") is WindowState.OPEN
            assert engine.registered["dynamic"] is panel

            assert manager.open_window("dynamic") is panel
            assert manager.get_window_state("dynamic") is WindowState.FOCUS_REQUESTED
            manager.process_pending_actions()
            assert manager.get_window_state("dynamic") is WindowState.FOCUSED
            assert engine.focused == ["dynamic"]

            manager.close_window("dynamic")
            assert manager.get_window_state("dynamic") is WindowState.CLOSING
            manager.process_pending_actions()
            assert manager.get_window_state("dynamic") is WindowState.CLOSED
            assert "dynamic" not in engine.registered
        finally:
            WindowManager._instance = previous

    def test_builtin_window_closes_without_unregistering(self):
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.registered = {}
                self.focused = []

            def register_gui(self, window_id, instance):
                self.registered[window_id] = instance

            def unregister_gui(self, window_id):
                raise AssertionError("builtin window must remain registered")

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        class Panel:
            def __init__(self):
                self.is_open = True

            def set_open(self, value):
                self.is_open = value

        previous = WindowManager._instance
        try:
            engine = Engine()
            manager = WindowManager(engine)
            panel = Panel()
            engine.registered["builtin"] = panel
            manager.register_window_type("builtin", Panel, "Builtin", factory=Panel)
            manager.register_existing_window("builtin", panel, "builtin")

            manager.close_window("builtin")
            assert manager.get_window_state("builtin") is WindowState.CLOSED
            assert engine.registered["builtin"] is panel
            assert panel.is_open is False

            assert manager.open_window("builtin") is panel
            manager.process_pending_actions()
            assert manager.get_window_state("builtin") is WindowState.FOCUSED
            assert panel.is_open is True
            assert engine.focused == ["builtin"]
        finally:
            WindowManager._instance = previous

    def test_window_menu_close_respects_panel_close_deferral(self):
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            @staticmethod
            def unregister_gui(_window_id):
                raise AssertionError("deferred window must remain registered")

        class Panel:
            def __init__(self):
                self._is_open = True
                self.close_requests = 0

            @property
            def is_open(self):
                return self._is_open

            def request_close(self):
                self.close_requests += 1
                return False

        previous = WindowManager._instance
        try:
            manager = WindowManager(Engine())
            panel = Panel()
            manager._window_states["dirty"] = WindowState.OPEN
            manager._window_instances["dirty"] = panel
            manager._registered_instance_ids.add("dirty")

            manager.close_window("dirty")

            assert panel.close_requests == 1
            assert panel.is_open is True
            assert manager.get_window_state("dirty") is WindowState.OPEN
        finally:
            WindowManager._instance = previous


# ── scene view math helpers ──────────────────────────────────────────────

class TestSceneViewMath:
    def test_dot_and_cross(self):
        from Infernux.engine.ui import _scene_view_math as m
        if not hasattr(m, "_dot3"):
            pytest.skip("helper not present")
        assert m._dot3((1, 0, 0), (0, 1, 0)) == 0
        assert m._dot3((1, 2, 3), (4, 5, 6)) == 32
        cx, cy, cz = m._cross3((1, 0, 0), (0, 1, 0))
        assert (cx, cy, cz) == (0, 0, 1)


# ── IGUI non-drawing logic ───────────────────────────────────────────────

class TestIGUIFilters:
    def test_searchable_combo_filter_semantics(self):
        labels = ["MeshRenderer", "SkinnedMeshRenderer", "Rigidbody", "BoxCollider"]
        filt = "mesh"
        filtered = [l for l in labels if filt.lower() in l.lower()]
        assert filtered == ["MeshRenderer", "SkinnedMeshRenderer"]


class TestUICanvasRaycast:
    class _GameObject:
        def __init__(self, active):
            self.active_in_hierarchy = active

    class _Element:
        def __init__(self, active):
            self.game_object = TestUICanvasRaycast._GameObject(active)
            self.raycast_target = True
            self.enabled = True

        @staticmethod
        def get_visual_rect(_ref_w, _ref_h):
            return 0.0, 0.0, 100.0, 100.0

        @staticmethod
        def contains_point(_x, _y, _ref_w, _ref_h, _tolerance):
            return True

    def test_inactive_hierarchy_does_not_receive_raycast(self):
        from Infernux.ui import UICanvas

        canvas = UICanvas()
        visible = self._Element(True)
        hidden_on_top = self._Element(False)
        canvas._get_elements = lambda: [visible, hidden_on_top]

        assert canvas.raycast(50.0, 50.0) is visible
        assert canvas.raycast_all(50.0, 50.0) == [visible]


class TestUICanvasCollectionCache:
    class _Root:
        def __init__(self, canvas):
            self._canvas = canvas

        def get_py_components(self):
            return [self._canvas]

        @staticmethod
        def get_children():
            return []

    class _Scene:
        name = "SameName"
        structure_version = 7

        def __init__(self, canvas):
            self._root = TestUICanvasCollectionCache._Root(canvas)

        def get_root_objects(self):
            return [self._root]

    def test_same_name_and_version_do_not_alias_distinct_scenes(self):
        from Infernux.ui import UICanvas
        from Infernux.ui.ui_canvas_utils import collect_canvases, invalidate_canvas_cache

        first_canvas = UICanvas()
        second_canvas = UICanvas()
        first_scene = self._Scene(first_canvas)
        second_scene = self._Scene(second_canvas)

        invalidate_canvas_cache()
        assert collect_canvases(first_scene) == [first_canvas]
        assert collect_canvases(second_scene) == [second_canvas]


def test_screen_ui_rect_cache_survives_frames_and_invalidates_on_geometry_change(monkeypatch):
    from Infernux.ui import UIText
    from Infernux.ui.inx_ui_screen_component import clear_rect_cache

    element = UIText()
    calls = 0
    def counted_parent_rect(width, height):
        nonlocal calls
        calls += 1
        return (0.0, 0.0, width, height)

    monkeypatch.setattr(element, "_get_parent_world_rect", counted_parent_rect)
    clear_rect_cache(("scene", 1))

    first = element.get_rect(1920.0, 1080.0)
    assert element.get_rect(1920.0, 1080.0) == first
    assert calls == 1

    element.width = 320.0
    assert element.get_rect(1920.0, 1080.0)[2] == 320.0
    assert calls == 2


def test_runtime_ui_packet_cache_reuses_static_text_and_tracks_mutation(monkeypatch):
    import Infernux.ui.ui_render_dispatch as dispatch_module
    from Infernux.ui import UIText

    class Renderer:
        def __init__(self):
            self.text_calls = []

        def add_text(self, *args):
            self.text_calls.append(args)

    element = UIText()
    renderer = Renderer()
    extract_calls = 0
    original_extract = dispatch_module.extract_common

    def counted_extract(value):
        nonlocal extract_calls
        extract_calls += 1
        return original_extract(value)

    monkeypatch.setattr(dispatch_module, "extract_common", counted_extract)
    kwargs = {
        "renderer": renderer,
        "ui_list": 0,
        "sx": 10.0,
        "sy": 20.0,
        "sw": 160.0,
        "sh": 40.0,
        "ref_w": 1920.0,
        "ref_h": 1080.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "text_scale": 1.0,
        "get_tex_id": lambda _path: 0,
    }

    assert dispatch_module.dispatch(element, "runtime", **kwargs)
    assert dispatch_module.dispatch(element, "runtime", **kwargs)
    assert extract_calls == 1
    assert len(renderer.text_calls) == 2

    element.text = "Updated"
    assert dispatch_module.dispatch(element, "runtime", **kwargs)
    assert extract_calls == 2
    assert renderer.text_calls[-1][5] == "Updated"


def test_runtime_ui_revision_is_stable_and_tracks_visual_state():
    from Infernux.ui.ui_render_dispatch import runtime_ui_revision
    from Infernux.ui.ui_render_revision import mark_runtime_ui_dirty

    class GameObject:
        active_in_hierarchy = True

    class Element:
        game_object = GameObject()
        enabled = True
        _ui_render_revision = 3
        _current_state = "normal"

    class Canvas:
        game_object = GameObject()
        enabled = True
        render_mode = 1
        sort_order = 0
        reference_width = 1920
        reference_height = 1080
        ui_scale_mode = 1
        screen_match_mode = 0
        match_width_or_height = 0.5
        pixel_perfect = False

        def __init__(self, element):
            self._element = element

        def _get_elements(self):
            return [self._element]

    class Scene:
        structure_version = 7

    element = Element()
    scene = Scene()
    canvases = [Canvas(element)]

    first = runtime_ui_revision(scene, canvases, 1280, 720, 2)
    assert runtime_ui_revision(scene, canvases, 1280, 720, 2) == first

    element._current_state = "hovered"
    mark_runtime_ui_dirty()
    assert runtime_ui_revision(scene, canvases, 1280, 720, 2) != first

    element._current_state = "normal"
    element._ui_render_revision += 1
    mark_runtime_ui_dirty()
    assert runtime_ui_revision(scene, canvases, 1280, 720, 2) != first


def test_ui_scalar_reassignment_does_not_invalidate_runtime_commands():
    from Infernux.ui import UIText
    from Infernux.ui.ui_render_revision import get_runtime_ui_revision

    text = UIText()
    initial_global = get_runtime_ui_revision()
    initial_local = getattr(text, "_ui_render_revision", 0)

    text.text = text.text
    assert get_runtime_ui_revision() == initial_global
    assert getattr(text, "_ui_render_revision", 0) == initial_local

    text.text = "Changed"
    assert get_runtime_ui_revision() == initial_global + 1
    assert text._ui_render_revision == initial_local + 1


def test_persistent_event_combo_preserves_temporarily_unresolved_method():
    from Infernux.engine.ui.inspector_ui_components import _persistent_event_combo_options

    labels, values = _persistent_event_combo_options(
        "toggle_settings", [], "None"
    )

    assert labels == ["None", "toggle_settings"]
    assert values == ["", "toggle_settings"]


class TestUIButtonPersistentDispatch:
    class _TargetRef:
        def __init__(self, target):
            self._target = target

        def resolve(self):
            return self._target

    class _GameObject:
        id = 31
        name = "Menu Controller"

        def __init__(self, component):
            self._component = component

        def get_py_components(self):
            return [self._component]

    @staticmethod
    def _entry(target, method_name):
        from Infernux.ui.ui_event_entry import UIEventEntry

        entry = UIEventEntry(
            component_name="MenuController",
            method_name=method_name,
            arguments=[],
        )
        entry.__dict__["target"] = TestUIButtonPersistentDispatch._TargetRef(target)
        return entry

    def test_invokes_bound_component_method_and_records_result(self):
        from Infernux.ui import UIButton

        class MenuController:
            def __init__(self):
                self.called = False

            def toggle_settings(self):
                self.called = True

        component = MenuController()
        target = self._GameObject(component)
        button = UIButton()
        button.on_click_entries = [self._entry(target, "toggle_settings")]

        button._dispatch_persistent_entries()

        assert component.called is True
        assert button.debug_dispatch_state() == [{
            "index": 0,
            "component_name": "MenuController",
            "method_name": "toggle_settings",
            "status": "invoked",
            "target_id": 31,
            "target_name": "Menu Controller",
        }]

    def test_missing_method_is_reported_instead_of_silently_ignored(self, monkeypatch):
        from Infernux.debug import Debug
        from Infernux.ui import UIButton

        class MenuController:
            pass

        errors = []
        monkeypatch.setattr(Debug, "log_error", errors.append)
        target = self._GameObject(MenuController())
        button = UIButton()
        button.on_click_entries = [self._entry(target, "toggle_settings")]

        button._dispatch_persistent_entries()

        assert button.debug_dispatch_state()[0]["status"] == "missing_method"
        assert errors == [
            "UIButton persistent event could not invoke "
            "Menu Controller.MenuController.toggle_settings: missing_method"
        ]


def test_focused_save_routes_to_document_then_falls_back_to_scene():
    from Infernux.engine._bootstrap_wiring import BootstrapWiringMixin
    from Infernux.engine.ui.closable_panel import ClosablePanel

    calls = []

    class Panel:
        @staticmethod
        def handle_save_command(save_as=False):
            calls.append(("document", save_as))
            return True

    class WindowManager:
        panel = Panel()

        @classmethod
        def get_window_instance(cls, panel_id):
            return cls.panel if panel_id == "timeline" else None

    class SceneFiles:
        @staticmethod
        def save_current_scene():
            calls.append(("scene", False))

        @staticmethod
        def save_scene_as():
            calls.append(("scene", True))

    previous = ClosablePanel._active_panel_id
    try:
        ClosablePanel._active_panel_id = "timeline"
        BootstrapWiringMixin._save_focused_document(WindowManager, SceneFiles)
        BootstrapWiringMixin._save_focused_document(
            WindowManager, SceneFiles, save_as=True
        )
        ClosablePanel._active_panel_id = "game"
        BootstrapWiringMixin._save_focused_document(WindowManager, SceneFiles)
    finally:
        ClosablePanel._active_panel_id = previous

    assert calls == [
        ("document", False),
        ("document", True),
        ("scene", False),
    ]


class TestPanelFocusEvents:
    def test_closable_panel_emits_single_canonical_focus_event(self):
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.event_bus import EditorEvent, EditorEventBus

        class FocusContext:
            def set_window_focus(self):
                pass

        bus = EditorEventBus.instance()
        received = []
        handler = received.append
        previous_active = ClosablePanel._active_panel_id
        bus.subscribe(EditorEvent.PANEL_FOCUSED, handler)
        try:
            panel = ClosablePanel("Focus Test", "focus_test")
            ClosablePanel._active_panel_id = None
            panel._activate_panel(FocusContext(), focus_window=True)
            panel._activate_panel(FocusContext(), focus_window=True)
            assert received == ["focus_test"]
            assert not hasattr(ClosablePanel, "set_on_panel_focus_changed")
        finally:
            bus.unsubscribe(EditorEvent.PANEL_FOCUSED, handler)
            ClosablePanel._active_panel_id = previous_active

    def test_closable_panel_keeps_child_window_focus_as_panel_focus(self):
        from Infernux.engine.ui.closable_panel import ClosablePanel

        class FocusContext:
            def __init__(self):
                self.focus_flags = []

            @staticmethod
            def begin_window_closable(_title, _open, _flags):
                return True, True

            @staticmethod
            def is_window_hovered(_flags):
                return False

            @staticmethod
            def is_mouse_button_clicked(_button):
                return False

            def is_window_focused(self, flags):
                self.focus_flags.append(flags)
                return flags == 3

        panel = ClosablePanel("Child Focus Test", "child_focus_test")
        ctx = FocusContext()
        previous_active = ClosablePanel._active_panel_id
        try:
            ClosablePanel._active_panel_id = panel.window_id
            panel._panel_was_focused = True

            assert panel._begin_closable_window(ctx) is True
            assert ClosablePanel.get_active_panel_id() == panel.window_id
            assert ctx.focus_flags == [3]
        finally:
            ClosablePanel._active_panel_id = previous_active

    def test_dirty_registry_sync_is_change_driven(self, monkeypatch):
        from Infernux.engine import project_context
        from Infernux.engine.ui.closable_panel import ClosablePanel

        calls = []
        monkeypatch.setattr(
            project_context,
            "set_panel_dirty",
            lambda panel_id, dirty, **kwargs: calls.append(
                (panel_id, dirty, kwargs["title"])
            ),
        )

        panel = ClosablePanel("Probe", "dirty_probe")
        panel._dirty = False
        panel._sync_dirty_registry()
        panel._sync_dirty_registry()
        panel._dirty = True
        panel._sync_dirty_registry()
        panel._sync_dirty_registry()

        assert calls == [
            ("dirty_probe", False, "Probe"),
            ("dirty_probe", True, "Probe"),
        ]


class TestSceneViewPicking:
    def test_scene_click_keeps_immediate_cpu_pick_and_queues_gpu_refinement(self):
        from Infernux.engine.ui._scene_view_picking import SceneViewPickingMixin

        class Context:
            @staticmethod
            def is_mouse_button_clicked(_button):
                return True

            @staticmethod
            def is_key_down(_key):
                return False

        class Viewport:
            width = 100.0
            height = 80.0

            @staticmethod
            def mouse_local(_ctx):
                return 10.0, 12.0

        class Engine:
            def __init__(self):
                self.requests = []

            def request_scene_object_pick(self, x, y, width, height):
                self.requests.append((x, y, width, height))
                return 7

            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "pending"}

        class PickingProbe(SceneViewPickingMixin):
            def __init__(self):
                self._engine = Engine()
                self._box_select_active = False
                self._pending_scene_pick = None
                self._pick_cycle_candidates = [42]
                self._pick_cycle_index = 0
                self._pick_cycle_last_mouse = (10.0, 12.0)
                self._pick_cycle_last_viewport = (100, 80)
                self.picked = []
                self._on_object_picked = lambda object_id, ctrl: self.picked.append((object_id, ctrl))

            @staticmethod
            def _pick_scene_object(_ctx, _viewport):
                return 42

        probe = PickingProbe()
        probe._handle_picking_and_selection(
            Context(), Viewport(), gizmo_consumed=False, overlay_hovered=False,
            is_scene_hovered=True, play_border_clr=None,
        )

        assert probe.picked == [(42, False)]
        assert probe._engine.requests == [(10.0, 12.0, 100.0, 80.0)]
        assert probe._pending_scene_pick["cpu_id"] == 42
        assert probe._pending_scene_pick["cpu_candidates"] == [42]

    def test_particle_refinement_keeps_icon_selection_but_joins_cycle(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.ui.selection_manager import SelectionManager

        class Engine:
            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "completed", "object_id": 99}

            @staticmethod
            def screen_to_world_ray(*_args):
                return (0.0, 0.0, 0.0, 0.0, 0.0, 1.0)

        class PickingProbe(picking.SceneViewPickingMixin):
            def __init__(self):
                self._engine = Engine()
                self._pending_scene_pick = {
                    "request_id": 1,
                    "x": 10.0,
                    "y": 12.0,
                    "width": 100.0,
                    "height": 80.0,
                    "cpu_id": 42,
                    "cpu_candidates": [42, 7],
                }
                self._pick_cycle_candidates = [42, 7]
                self._pick_cycle_index = 0
                self._pick_cycle_last_mouse = (-1.0, -1.0)
                self._pick_cycle_last_viewport = (0, 0)
                self.picked = []
                self._on_object_picked = lambda object_id, ctrl: self.picked.append((object_id, ctrl))

        monkeypatch.setattr(picking, "_owns_particle_system", lambda object_id: object_id == 99)
        monkeypatch.setattr(picking, "_is_icon_only_pick_target", lambda object_id: object_id == 42)
        monkeypatch.setattr(picking, "_object_ray_depth", lambda object_id, *_args: {
            42: 1.0,
            99: 2.0,
            7: 3.0,
        }[object_id])

        sel = SelectionManager.instance()
        previous = list(sel.get_ids())
        sel.select(42)
        try:
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == []
            assert probe._pick_cycle_candidates == [42, 99, 7]
            assert probe._pick_cycle_index == 0
        finally:
            if previous:
                sel.set_ids(previous)
            else:
                sel.clear()

    def test_particle_refinement_corrects_mesh_behind_spray(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.ui.selection_manager import SelectionManager

        class Engine:
            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "completed", "object_id": 99}

            @staticmethod
            def screen_to_world_ray(*_args):
                return (0.0, 0.0, 0.0, 0.0, 0.0, 1.0)

        class PickingProbe(picking.SceneViewPickingMixin):
            def __init__(self):
                self._engine = Engine()
                self._pending_scene_pick = {
                    "request_id": 1,
                    "x": 10.0,
                    "y": 12.0,
                    "width": 100.0,
                    "height": 80.0,
                    "cpu_id": 7,
                    "cpu_candidates": [7],
                }
                self._pick_cycle_candidates = [7]
                self._pick_cycle_index = 0
                self._pick_cycle_last_mouse = (-1.0, -1.0)
                self._pick_cycle_last_viewport = (0, 0)
                self.picked = []
                self._on_object_picked = lambda object_id, ctrl: self.picked.append((object_id, ctrl))

        monkeypatch.setattr(picking, "_owns_particle_system", lambda object_id: object_id == 99)
        monkeypatch.setattr(picking, "_is_icon_only_pick_target", lambda object_id: False)
        monkeypatch.setattr(picking, "_object_ray_depth", lambda object_id, *_args: {
            99: 1.0,
            7: 3.0,
        }[object_id])

        sel = SelectionManager.instance()
        previous = list(sel.get_ids())
        sel.select(7)
        try:
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == [(99, False)]
            assert probe._pick_cycle_candidates == [99, 7]
            assert probe._pick_cycle_index == 0
        finally:
            if previous:
                sel.set_ids(previous)
            else:
                sel.clear()

    def test_same_spot_click_keeps_particle_in_depth_cycle(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking

        class Engine:
            @staticmethod
            def pick_scene_object_ids(*_args):
                return [42, 7]

            @staticmethod
            def screen_to_world_ray(*_args):
                return (0.0, 0.0, 0.0, 0.0, 0.0, 1.0)

        class Context:
            @staticmethod
            def mouse_local(_ctx=None):
                return 10.0, 12.0

        class Viewport:
            width = 100.0
            height = 80.0

            @staticmethod
            def mouse_local(_ctx):
                return 10.0, 12.0

        class PickingProbe(picking.SceneViewPickingMixin):
            def __init__(self):
                self._engine = Engine()
                self._pick_cycle_candidates = [42, 99, 7]
                self._pick_cycle_index = 0
                self._pick_cycle_last_mouse = (10.0, 12.0)
                self._pick_cycle_last_viewport = (100, 80)

        monkeypatch.setattr(picking, "_owns_particle_system", lambda object_id: object_id == 99)
        monkeypatch.setattr(picking, "_object_ray_depth", lambda object_id, *_args: {
            42: 1.0,
            99: 2.0,
            7: 3.0,
        }[object_id])

        probe = PickingProbe()
        assert probe._pick_scene_object(Context(), Viewport()) == 99
        assert probe._pick_cycle_candidates == [42, 99, 7]
        assert probe._pick_scene_object(Context(), Viewport()) == 7


class TestEditorPanelVisibilityLifecycle:
    def test_hidden_hook_runs_only_on_visibility_transitions(self):
        from Infernux.engine.ui.editor_panel import EditorPanel

        class Context:
            @staticmethod
            def end_window():
                pass

        class ProbePanel(EditorPanel):
            def __init__(self):
                super().__init__("Probe", "probe")
                self.visibility = iter([False, False, True, False, False])
                self.hidden_calls = 0
                self.visible_calls = 0
                self.content_calls = 0

            def _begin_closable_window(self, _ctx, _flags=0):
                return next(self.visibility)

            def _on_not_visible(self, _ctx):
                self.hidden_calls += 1

            def _on_visible_pre(self, _ctx):
                self.visible_calls += 1

            def on_render_content(self, _ctx):
                self.content_calls += 1

        panel = ProbePanel()
        ctx = Context()
        for _ in range(5):
            panel.on_render(ctx)

        assert panel.hidden_calls == 2
        assert panel.visible_calls == 1
        assert panel.content_calls == 1
