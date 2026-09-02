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


class _WindowManagerPanelInteractions:
    """Explicit interaction boundary for WindowManager-only tests."""

    def bind_view(self, _view_id, _type_id, _instance):
        pass

    def unbind_view(self, _view_id):
        return True

    def records_focus_history(self, **_identity):
        return True

    def is_document_backed(self, **_identity):
        return False


def _window_manager(engine, panel_interactions=None):
    from Infernux.engine.ui.window_manager import WindowManager

    registrar = getattr(engine, "register_gui", None)
    if registrar is None:
        registrar = lambda _window_id, _instance: None
    return WindowManager(
        engine,
        panel_interactions or _WindowManagerPanelInteractions(),
        registrar,
    )


def test_window_manager_requires_explicit_interaction_and_gui_boundaries():
    from Infernux.engine.ui.window_manager import WindowManager

    with pytest.raises(ValueError, match="PanelInteractionRegistry"):
        WindowManager(object(), None, lambda _window_id, _instance: None)
    with pytest.raises(TypeError, match="panel GUI registrar"):
        WindowManager(object(), _WindowManagerPanelInteractions(), None)


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


def test_engine_gui_registration_rejects_duplicate_renderable_identity():
    from types import SimpleNamespace

    from Infernux.engine.engine import Engine

    calls = []
    engine = Engine.__new__(Engine)
    engine._engine = SimpleNamespace(
        register_gui_renderable=lambda name, renderable, priority: calls.append(
            (name, renderable, priority)
        )
    )
    existing = object()
    engine._gui_objects = {"scene_view": existing}

    with pytest.raises(RuntimeError, match="already registered"):
        engine.register_gui("scene_view", object())

    assert calls == []
    assert engine._gui_objects["scene_view"] is existing


def test_existing_window_registration_is_atomic_when_native_registration_fails():
    from Infernux.engine.interaction import (
        EditorInteractionCore,
        PanelInteractionDescriptor,
    )
    from Infernux.engine.ui.window_manager import WindowManager

    class Engine:
        @staticmethod
        def register_gui(_window_id, _instance):
            raise RuntimeError("native registration failed")

    class Panel:
        is_open = True

    previous_manager = WindowManager._instance
    core = EditorInteractionCore()
    try:
        manager = _window_manager(Engine(), core.panels)
        core.panels.register_type("native", PanelInteractionDescriptor())

        with pytest.raises(RuntimeError, match="native registration failed"):
            manager.register_existing_window("native", Panel(), "native")

        with pytest.raises(KeyError, match="Unknown window id"):
            manager.get_window_state("native")
        assert manager.get_window_instance("native") is None
        assert core.panels.instance_for_view("native") is None
        assert "native" not in manager._registered_instance_ids
    finally:
        core.shutdown()
        WindowManager._instance = previous_manager


def test_engine_docked_window_selection_forwards_modal_flag():
    from types import SimpleNamespace

    from Infernux.engine.engine import Engine

    calls = []
    engine = Engine.__new__(Engine)
    engine._engine = SimpleNamespace(
        select_docked_window=lambda window_id, allow_during_modal: calls.append(
            (window_id, allow_during_modal)
        )
    )

    engine.select_docked_window("particle_graph_editor", True)

    assert calls == [("particle_graph_editor", True)]


def test_engine_docked_window_selection_rejects_stale_native_binding():
    from Infernux.engine.engine import Engine

    class LegacyNativeEngine:
        def __init__(self):
            self.focus_requests = []

        def select_docked_window(self, window_id):
            self.focus_requests.append(window_id)

    engine = Engine.__new__(Engine)
    engine._engine = LegacyNativeEngine()

    with pytest.raises(TypeError):
        engine.select_docked_window("particle_graph_editor", True)

    assert engine._engine.focus_requests == []


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
    @staticmethod
    def _layout_reset_fixture():
        from Infernux.engine.interaction import (
            DocumentCapability,
            DocumentKind,
            EditorInteractionCore,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.editor_panel import EditorPanel
        from Infernux.engine.ui.window_manager import WindowManager

        class Engine:
            def __init__(self):
                self.registered = {}
                self.reset_count = 0
                self.focused = []

            def register_gui(self, window_id, instance):
                self.registered[window_id] = instance

            def unregister_gui(self, window_id):
                self.registered.pop(window_id, None)

            def reset_imgui_layout(self):
                self.reset_count += 1

            def select_docked_window(self, window_id, *, allow_during_modal=False):
                self.focused.append((window_id, bool(allow_during_modal)))

        class Controller:
            def __init__(self):
                self.discard_calls = 0

            def discard(self, *, document_id):
                assert document_id == "graph-document"
                self.discard_calls += 1
                return True

        core = EditorInteractionCore()
        engine = Engine()
        manager = _window_manager(engine, core.panels)
        for type_id in ("scene_view", "graph"):
            core.panels.register_type(type_id, PanelInteractionDescriptor())

        scene = EditorPanel("Scene", "scene_view")
        manager.register_existing_window("scene_view", scene, "scene_view")
        manager.register_window_type(
            "graph",
            ClosablePanel,
            "Graph",
            factory=lambda: ClosablePanel("Graph", "graph"),
        )
        graph = manager.open_window("graph")
        manager.process_pending_actions()

        controller = Controller()
        document = core.documents.create(
            DocumentKind.GENERIC,
            "Graph",
            document_id="graph-document",
            revision=1,
            saved_revision=0,
            capabilities=DocumentCapability.DISCARD,
            controller=controller,
        )
        graph.bind_document(document.document_id)
        return core, engine, manager, scene, graph, document, controller

    def test_native_titlebar_close_intent_uses_window_manager_lifecycle(self):
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def select_docked_window(self, *_args, **_kwargs):
                pass

        class NativePanel:
            def __init__(self):
                self.open = True
                self.on_request_close = None

            def is_open(self):
                return self.open

            def set_open(self, value):
                self.open = bool(value)

        previous = WindowManager._instance
        core = None
        try:
            core = EditorInteractionCore()
            manager = _window_manager(Engine(), core.panels)
            core.panels.register_type(
                "native",
                PanelInteractionDescriptor(),
            )
            panel = NativePanel()
            manager.register_existing_window("native", panel, "native")
            state_changes = []
            manager.set_on_state_changed(lambda: state_changes.append(True))

            core.focus.activate_panel(
                "native",
                view_id="native",
                reason="test",
                record_history=False,
            )
            core.modals.register(
                "native.modal",
                is_active=lambda: True,
                render=lambda _ctx: None,
                cancel=lambda: None,
            )
            assert core.modals.activate("native.modal", owner_id="native")
            assert callable(panel.on_request_close)

            assert panel.on_request_close()

            assert manager.get_window_state("native") is WindowState.CLOSED
            assert not panel.open
            assert core.focus.snapshot.active_view_id == ""
            assert core.modals.active_modal_id == ""
            assert state_changes
        finally:
            if core is not None:
                core.shutdown()
            WindowManager._instance = previous

    def test_reset_layout_cancel_preserves_dirty_dynamic_view(self):
        from Infernux.engine.interaction import CloseState
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        previous = WindowManager._instance
        core = None
        try:
            core, engine, manager, _scene, graph, document, controller = (
                self._layout_reset_fixture()
            )

            assert manager.reset_layout()
            assert core.close_coordinator.state is CloseState.AWAITING_DECISION
            assert core.close_coordinator.active_document is document
            assert core.modals.active_modal_id == "editor.unsaved_changes"
            assert engine.reset_count == 0

            core.close_coordinator.cancel()
            manager.process_pending_actions()

            assert manager.get_window_state("graph") in {
                WindowState.OPEN,
                WindowState.FOCUSED,
            }
            assert manager.get_window_instance("graph") is graph
            assert core.documents.document_for_view("graph") is document
            assert controller.discard_calls == 0
            assert engine.reset_count == 0
            assert core.modals.active_modal_id == ""
        finally:
            if core is not None:
                core.shutdown()
            WindowManager._instance = previous

    def test_reset_layout_discards_then_formally_retires_dynamic_view(self):
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        previous = WindowManager._instance
        core = None
        try:
            core, engine, manager, _scene, graph, _document, controller = (
                self._layout_reset_fixture()
            )

            assert manager.reset_layout()
            core.close_coordinator.decide_discard()
            manager.process_pending_actions()

            assert controller.discard_calls == 1
            assert manager.get_window_state("graph") is WindowState.CLOSED
            assert "graph" not in manager._window_instances
            assert core.documents.document_for_view("graph") is None
            assert graph.document_id == ""
            assert "graph" not in engine.registered
            assert engine.reset_count == 1
            assert engine.focused[-1] == ("scene_view", False)
        finally:
            if core is not None:
                core.shutdown()
            WindowManager._instance = previous

    def test_reset_layout_does_not_prompt_for_document_with_retained_view(self):
        from Infernux.engine.interaction import CloseState
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        previous = WindowManager._instance
        core = None
        try:
            core, engine, manager, _scene, _graph, document, controller = (
                self._layout_reset_fixture()
            )
            core.documents.attach_view(document.document_id, "scene_view")

            assert manager.reset_layout()
            assert core.close_coordinator.state is CloseState.IDLE
            manager.process_pending_actions()

            assert controller.discard_calls == 0
            assert document.is_dirty
            assert document.view_ids == {"scene_view"}
            assert manager.get_window_state("graph") is WindowState.CLOSED
            assert engine.reset_count == 1
        finally:
            if core is not None:
                core.shutdown()
            WindowManager._instance = previous

    def test_document_panel_payload_without_formal_snapshot_is_pruned(self, monkeypatch):
        from Infernux.engine.ui import panel_state

        monkeypatch.setattr(
            panel_state,
            "_state",
            {
                "panel:animclip2d_editor": {"dirty": True, "clips": ["draft"]},
                "panel:particle_graph_editor": {"schema": "view"},
                "panel:console": {"filter": "warning"},
                "window_manager": {"open_windows": {}},
            },
        )

        removed = panel_state.prune_document_view_states(
            is_document_backed=lambda view_id: view_id
            in {"animclip2d_editor", "particle_graph_editor"},
            has_restorable_document=lambda view_id: view_id
            == "particle_graph_editor",
        )

        assert removed == ("animclip2d_editor",)
        assert "panel:animclip2d_editor" not in panel_state.keys()
        assert panel_state.get("panel:particle_graph_editor") == {"schema": "view"}
        assert panel_state.get("panel:console") == {"filter": "warning"}

    def test_window_state_retains_type_identity_for_closed_dynamic_views(
        self,
        _reset_editor_interaction_state,
    ):
        from Infernux.engine.interaction import (
            PanelInteractionDescriptor,
            PanelInteractionRegistry,
        )
        from Infernux.engine.ui.window_manager import WindowManager

        previous = WindowManager._instance
        try:
            panels = PanelInteractionRegistry()
            panels.register_type(
                "particle_graph_editor",
                PanelInteractionDescriptor(document_backed=True),
            )
            manager = _window_manager(object(), panels)
            manager.load_state(
                {
                    "open_windows": {"graph-instance": False},
                    "window_types": {
                        "graph-instance": "particle_graph_editor",
                    },
                }
            )

            assert manager.window_type_id("graph-instance") == "particle_graph_editor"
            assert manager.is_document_backed_view(
                "graph-instance",
                manager.window_type_id("graph-instance"),
            )
        finally:
            WindowManager._instance = previous

    def test_terminal_discard_closes_document_backed_view_in_saved_state(
        self,
        _reset_editor_interaction_state,
    ):
        from Infernux.engine.interaction import (
            DocumentKind,
            PanelInteractionDescriptor,
            PanelInteractionRegistry,
        )
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        previous = WindowManager._instance
        try:
            panels = PanelInteractionRegistry()
            panels.register_type(
                "particle_graph_editor",
                PanelInteractionDescriptor(document_backed=True),
            )
            manager = _window_manager(object(), panels)
            manager._window_states["particle_graph_editor"] = WindowState.OPEN
            manager._window_type_ids["particle_graph_editor"] = (
                "particle_graph_editor"
            )

            document = _reset_editor_interaction_state.create(
                DocumentKind.PARTICLE_GRAPH,
                "Unsaved Graph",
                revision=1,
                saved_revision=0,
            )
            _reset_editor_interaction_state.attach_view(
                document.document_id,
                "particle_graph_editor",
            )
            _reset_editor_interaction_state.abandon_session_changes(
                document.document_id
            )

            state = manager.save_state()
            assert state["open_windows"]["particle_graph_editor"] is False
        finally:
            WindowManager._instance = previous

    def test_deleted_asset_closes_its_authoring_view_without_dormant_draft(
        self,
        _reset_editor_interaction_state,
        tmp_path,
    ):
        from Infernux.engine.interaction import (
            AssetContentChange,
            AssetMutation,
            AssetMutationKind,
            DocumentKey,
            DocumentKind,
            PanelInteractionDescriptor,
            PanelInteractionRegistry,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            @staticmethod
            def register_gui(_window_id, _instance):
                pass

            @staticmethod
            def unregister_gui(_window_id):
                pass

        previous = WindowManager._instance
        try:
            panels = PanelInteractionRegistry()
            panels.register_type(
                "particle_graph_editor",
                PanelInteractionDescriptor(document_backed=True),
            )
            manager = _window_manager(Engine(), panels)
            manager.register_window_type(
                "particle_graph_editor",
                ClosablePanel,
                "Particle Graph",
                factory=lambda: ClosablePanel(
                    "Particle Graph",
                    "particle_graph_editor",
                ),
            )
            panel = manager.open_window("particle_graph_editor")
            manager.process_pending_actions()

            path = tmp_path / "Smoke.particlegraph"
            path.write_text("{}", encoding="utf-8")
            document = _reset_editor_interaction_state.create(
                DocumentKind.PARTICLE_GRAPH,
                "Smoke",
                key=DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(path)),
                resource_path=str(path),
                revision=1,
                saved_revision=0,
            )
            panel.bind_document(document.document_id)
            locator = _reset_editor_interaction_state.locate(document.document_id)
            path.unlink()

            manager.on_asset_mutation(
                AssetContentChange(
                    AssetMutation(AssetMutationKind.DELETED, str(path)),
                    1,
                )
            )
            manager.process_pending_actions()

            assert manager.get_window_state(
                "particle_graph_editor"
            ) is WindowState.CLOSED
            assert panel.document_id == ""
            assert _reset_editor_interaction_state.get(document.document_id) is None
            assert _reset_editor_interaction_state.resolve_locator(locator) is None
        finally:
            WindowManager._instance = previous

    def test_startup_does_not_recreate_document_panel_without_snapshot(
        self,
        _reset_editor_interaction_state,
    ):
        from Infernux.engine.interaction import (
            PanelInteractionDescriptor,
            PanelInteractionRegistry,
        )
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Panel:
            def __init__(self):
                self.is_open = True

            def set_open(self, value):
                self.is_open = bool(value)

        previous = WindowManager._instance
        try:
            panels = PanelInteractionRegistry()
            panels.register_type(
                "particle_graph_editor",
                PanelInteractionDescriptor(document_backed=True),
            )
            manager = _window_manager(object(), panels)
            manager.register_window_type(
                "particle_graph_editor",
                Panel,
                "Particle Graph",
                factory=Panel,
            )

            manager.load_state(
                {
                    "open_windows": {"particle_graph_editor": True},
                    "window_types": {
                        "particle_graph_editor": "particle_graph_editor"
                    },
                }
            )

            assert manager.get_window_state(
                "particle_graph_editor"
            ) is WindowState.CLOSED
            assert manager.get_window_instance("particle_graph_editor") is None
        finally:
            WindowManager._instance = previous

    def test_startup_discards_pending_draft_for_closed_document_view(
        self,
        _reset_editor_interaction_state,
    ):
        from Infernux.engine.interaction import (
            DocumentKind,
            PanelInteractionDescriptor,
            PanelInteractionRegistry,
        )
        from Infernux.engine.ui.window_manager import WindowManager

        class Controller:
            @staticmethod
            def capture_document_restore_state(_document_id):
                return {"draft": True}

        document = _reset_editor_interaction_state.create(
            DocumentKind.PARTICLE_GRAPH,
            "Closed Draft",
            revision=1,
            saved_revision=0,
            controller=Controller(),
        )
        _reset_editor_interaction_state.attach_view(
            document.document_id,
            "particle_graph_editor",
        )
        session = _reset_editor_interaction_state.capture_session_state()
        _reset_editor_interaction_state.clear()
        assert _reset_editor_interaction_state.queue_session_restore(session) == 1

        previous = WindowManager._instance
        try:
            panels = PanelInteractionRegistry()
            panels.register_type(
                "particle_graph_editor",
                PanelInteractionDescriptor(document_backed=True),
            )
            manager = _window_manager(object(), panels)
            manager.load_state(
                {
                    "open_windows": {"particle_graph_editor": False},
                    "window_types": {
                        "particle_graph_editor": "particle_graph_editor"
                    },
                }
            )

            assert not _reset_editor_interaction_state.has_pending_session_document(
                "particle_graph_editor"
            )
            assert _reset_editor_interaction_state.capture_session_state()[
                "documents"
            ] == []
        finally:
            WindowManager._instance = previous

    def test_startup_restores_document_panel_when_snapshot_exists(
        self,
        _reset_editor_interaction_state,
    ):
        from Infernux.engine.interaction import (
            DocumentKind,
            PanelInteractionDescriptor,
            PanelInteractionRegistry,
        )
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Panel:
            def __init__(self):
                self.is_open = True

            def set_open(self, value):
                self.is_open = bool(value)

        class Controller:
            @staticmethod
            def capture_document_restore_state(_document_id):
                return {"asset": "Assets/Smoke.particlegraph"}

        document = _reset_editor_interaction_state.create(
            DocumentKind.PARTICLE_GRAPH,
            "Smoke",
            controller=Controller(),
        )
        _reset_editor_interaction_state.attach_view(
            document.document_id,
            "particle_graph_editor",
        )
        session = _reset_editor_interaction_state.capture_session_state()
        _reset_editor_interaction_state.clear()
        assert _reset_editor_interaction_state.queue_session_restore(session) == 1

        previous = WindowManager._instance
        try:
            panels = PanelInteractionRegistry()
            panels.register_type(
                "particle_graph_editor",
                PanelInteractionDescriptor(document_backed=True),
            )
            manager = _window_manager(object(), panels)
            manager.register_window_type(
                "particle_graph_editor",
                Panel,
                "Particle Graph",
                factory=Panel,
            )

            manager.load_state(
                {
                    "open_windows": {"particle_graph_editor": True},
                    "window_types": {
                        "particle_graph_editor": "particle_graph_editor"
                    },
                }
            )

            assert manager.get_window_state(
                "particle_graph_editor"
            ) is WindowState.OPENING
            assert manager.get_window_instance("particle_graph_editor") is not None
        finally:
            WindowManager._instance = previous

    def test_dynamic_panel_menu_close_finalizes_lifecycle_before_unregister(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.editor_panel import EditorPanel
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.registered = {}

            def register_gui(self, window_id, instance):
                self.registered[window_id] = instance

            def unregister_gui(self, window_id):
                self.registered.pop(window_id)

            @staticmethod
            def select_docked_window(_window_id):
                pass

        class Panel(EditorPanel):
            def __init__(self):
                super().__init__("Utility", "utility")
                self.disable_count = 0

            def on_disable(self):
                self.disable_count += 1

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            FocusService()
            engine = Engine()
            manager = _window_manager(engine)
            manager.register_window_type(
                "utility",
                Panel,
                "Utility",
                factory=Panel,
                singleton=True,
            )
            panel = manager.open_window("utility")
            manager.process_pending_actions()
            panel._enable_called = True

            manager.close_window("utility")
            assert panel.disable_count == 0
            assert manager.get_window_state("utility") is WindowState.CLOSING

            manager.process_pending_actions()
            assert panel.disable_count == 1
            assert manager.get_window_state("utility") is WindowState.CLOSED
            assert "utility" not in engine.registered

            panel._finalize_close_lifecycle()
            assert panel.disable_count == 1
        finally:
            FocusService._instance = previous_focus
            WindowManager._instance = previous_manager

    def test_utility_settings_use_the_regular_dockable_panel_contract(self):
        from Infernux.engine.interaction import PanelInteractionDescriptor
        from Infernux.engine.ui import (
            BuildSettingsPanel,
            EditorPanel,
            EnvironmentSettingsPanel,
            FloatingEditorPanel,
            InxPackageImportPanel,
            PhysicsLayerMatrixPanel,
            PreferencesPanel,
        )

        expected_ids = {
            BuildSettingsPanel: "build_settings",
            PreferencesPanel: "preferences",
            PhysicsLayerMatrixPanel: "physics_settings",
            EnvironmentSettingsPanel: "environment_settings",
            InxPackageImportPanel: "inxpackage_import",
        }
        for panel_class, expected_id in expected_ids.items():
            assert issubclass(panel_class, EditorPanel)
            assert not issubclass(panel_class, FloatingEditorPanel)
            assert panel_class.WINDOW_TYPE_ID == expected_id
            assert panel_class._panel_menu_path == ""
            assert isinstance(panel_class.PANEL_INTERACTION, PanelInteractionDescriptor)
            assert "render" not in panel_class.__dict__

    def test_close_confirmation_restores_source_during_modal(self):
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focus_requests = []

            def select_docked_window(
                self, window_id, *, allow_during_modal=False
            ):
                self.focus_requests.append((window_id, allow_during_modal))

        previous = WindowManager._instance
        try:
            engine = Engine()
            manager = _window_manager(engine)
            manager._window_states["particle_graph_editor"] = WindowState.OPEN

            manager.restore_close_confirmation_source("particle_graph_editor")

            assert engine.focus_requests == [("particle_graph_editor", True)]
        finally:
            WindowManager._instance = previous

    def test_close_confirmation_requires_canonical_engine_boundary(self):
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focus_requests = []

            def select_docked_window(self, window_id):
                self.focus_requests.append(window_id)

        previous = WindowManager._instance
        try:
            engine = Engine()
            manager = _window_manager(engine)
            manager._window_states["particle_graph_editor"] = WindowState.OPEN

            with pytest.raises(TypeError):
                manager.restore_close_confirmation_source("particle_graph_editor")

            assert engine.focus_requests == []
        finally:
            WindowManager._instance = previous

    def test_dynamic_views_keep_panel_type_separate_from_instance_identity(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.editor_panel import EditorPanel
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            @staticmethod
            def register_gui(_window_id, _instance):
                pass

            @staticmethod
            def unregister_gui(_window_id):
                pass

            @staticmethod
            def select_docked_window(_window_id):
                pass

        class FocusContext:
            @staticmethod
            def set_window_focus():
                pass

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            manager = _window_manager(Engine())
            manager.register_window_type(
                "graph",
                EditorPanel,
                "Graph",
                factory=lambda: EditorPanel("Graph", "factory-default"),
                singleton=False,
            )
            left = manager.open_window("graph", instance_id="graph/left")
            right = manager.open_window("graph", instance_id="graph/right")
            manager.process_pending_actions()

            assert left.panel_type_id == right.panel_type_id == "graph"
            assert left.window_id == "graph/left"
            assert right.window_id == "graph/right"

            right._activate_panel(FocusContext())
            assert focus.snapshot.active_panel_id == "graph"
            assert focus.snapshot.active_view_id == "graph/right"

            manager.close_window("graph/left")
            assert focus.snapshot.active_panel_id == "graph"
            assert focus.snapshot.active_view_id == "graph/right"
        finally:
            FocusService._instance = previous_focus
            WindowManager._instance = previous_manager

    def test_panel_identity_transfer_preserves_an_early_document_binding(self):
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry
        from Infernux.engine.ui.closable_panel import ClosablePanel

        previous_registry = DocumentRegistry._instance
        try:
            registry = DocumentRegistry()
            document = registry.create(DocumentKind.GENERIC, "Graph")
            panel = ClosablePanel("Graph", "factory-default")
            panel.bind_document(document.document_id)

            panel.set_panel_identity("graph", "graph/asset-a")

            assert registry.document_for_view("factory-default") is None
            assert registry.document_for_view("graph/asset-a") is document
            assert registry.get(document.document_id) is document
        finally:
            DocumentRegistry._instance = previous_registry

    def test_failed_native_registration_rolls_back_panel_interaction_binding(self):
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            @staticmethod
            def register_gui(_window_id, _instance):
                raise RuntimeError("native registration failed")

        previous_manager = WindowManager._instance
        core = EditorInteractionCore()
        try:
            manager = _window_manager(Engine(), core.panels)
            core.panels.register_type("graph", PanelInteractionDescriptor())
            manager.register_window_type(
                "graph",
                ClosablePanel,
                "Graph",
                factory=lambda: ClosablePanel("Graph"),
                singleton=False,
            )
            manager.open_window("graph", instance_id="graph/broken")

            with pytest.raises(RuntimeError, match="native registration failed"):
                manager.process_pending_actions()

            assert manager.get_window_state("graph/broken") is WindowState.CLOSED
            assert "graph/broken" not in core.panels._views
        finally:
            core.shutdown()
            WindowManager._instance = previous_manager

    def test_native_pointer_activation_is_distinct_user_focus_history(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager

        class Panel:
            is_open = True

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            changes = []
            focus.add_change_listener(changes.append)
            focus.activate_panel(
                "scene_view",
                view_id="scene_view",
                record_history=False,
            )
            changes.clear()

            manager = _window_manager(object())
            manager.register_existing_window("project", Panel())
            callback = manager.native_panel_focus_callback("project")
            callback(True, True)

            assert focus.snapshot.active_panel_id == "project"
            assert len(changes) == 1
            assert changes[0].record_history is True
            assert changes[0].reason == "pointer_panel_activation"
        finally:
            WindowManager._instance = previous_manager
            FocusService._instance = previous_focus

    def test_user_window_command_publishes_reveal_before_native_focus(self):
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.window_manager import WindowManager

        class Engine:
            @staticmethod
            def select_docked_window(_window_id):
                pass

        previous_manager = WindowManager._instance
        core = EditorInteractionCore()
        changes = []
        core.focus.add_change_listener(changes.append)
        try:
            manager = _window_manager(Engine(), core.panels)
            core.panels.register_type("graph", PanelInteractionDescriptor())
            manager.register_window_type(
                "graph",
                ClosablePanel,
                "Graph",
                factory=lambda: ClosablePanel("Graph", "graph"),
            )
            panel = ClosablePanel("Graph", "graph")
            manager.register_existing_window("graph", panel, type_id="graph")
            core.focus.activate_panel(
                "scene_view",
                view_id="scene_view",
                record_history=False,
            )
            changes.clear()

            assert manager.open_window_from_user("graph") is panel

            assert core.focus.snapshot.active_view_id == "graph"
            assert len(changes) == 1
            assert changes[0].reason == "window_open_command"
            assert changes[0].record_history is True
        finally:
            core.shutdown()
            WindowManager._instance = previous_manager

    def test_user_window_reveal_survives_early_focus_projection(self):
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focused = []

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        previous_manager = WindowManager._instance
        core = EditorInteractionCore()
        try:
            engine = Engine()
            manager = _window_manager(engine, core.panels)
            core.panels.register_type("graph", PanelInteractionDescriptor())
            manager.register_window_type(
                "graph",
                ClosablePanel,
                "Graph",
                factory=lambda: ClosablePanel("Graph", "graph"),
            )
            panel = ClosablePanel("Graph", "graph")
            manager.register_existing_window("graph", panel, type_id="graph")
            core.focus.add_listener(manager.project_interaction_focus)
            core.focus.activate_panel(
                "scene_view",
                view_id="scene_view",
                record_history=False,
            )

            assert manager.open_window_from_user("graph") is panel
            assert manager.get_window_state("graph") is WindowState.FOCUSED

            manager.process_pending_actions()

            assert engine.focused == ["graph"]
            assert manager.get_window_state("graph") is WindowState.FOCUSED
        finally:
            core.shutdown()
            WindowManager._instance = previous_manager

    def test_new_user_window_registers_before_focus_projection(self):
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.registered = {}
                self.focused = []

            def register_gui(self, window_id, instance):
                self.registered[window_id] = instance

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        previous_manager = WindowManager._instance
        core = EditorInteractionCore()
        changes = []
        core.focus.add_change_listener(changes.append)
        try:
            engine = Engine()
            manager = _window_manager(engine, core.panels)
            core.panels.register_type("graph", PanelInteractionDescriptor())
            manager.register_window_type(
                "graph",
                ClosablePanel,
                "Graph",
                factory=lambda: ClosablePanel("Graph", "graph"),
            )
            core.focus.add_listener(manager.project_interaction_focus)
            core.focus.activate_panel(
                "project",
                view_id="project",
                record_history=False,
            )
            changes.clear()

            panel = manager.open_window_from_user("graph")

            assert manager.get_window_state("graph") is WindowState.OPENING
            assert core.focus.snapshot.active_view_id == "project"
            assert "graph" not in engine.registered

            manager.process_pending_actions()

            assert engine.registered["graph"] is panel
            assert engine.focused == ["graph"]
            assert manager.get_window_state("graph") is WindowState.FOCUSED
            assert core.focus.snapshot.active_view_id == "graph"
            recorded = [change for change in changes if change.record_history]
            assert len(recorded) == 1
            assert recorded[0].reason == "window_open_command"
        finally:
            core.shutdown()
            WindowManager._instance = previous_manager

    def test_user_window_command_does_not_publish_visible_focus(self):
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.editor_panel import EditorPanel
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focused = []

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        previous_manager = WindowManager._instance
        core = EditorInteractionCore()
        changes = []
        core.focus.add_change_listener(changes.append)
        try:
            engine = Engine()
            manager = _window_manager(engine, core.panels)
            core.panels.register_type("graph", PanelInteractionDescriptor())
            manager.register_window_type(
                "graph",
                EditorPanel,
                "Graph",
                factory=lambda: EditorPanel("Graph", "graph"),
            )
            panel = EditorPanel("Graph", "graph")
            panel.open()
            panel._content_was_visible = True
            panel._content_visible_previous_frame = True
            manager.register_existing_window("graph", panel, type_id="graph")
            core.focus.activate_panel(
                "scene_view",
                view_id="scene_view",
                record_history=False,
            )
            changes.clear()

            assert manager.open_window_from_user("graph") is panel

            assert manager.get_window_state("graph") is WindowState.FOCUS_REQUESTED
            assert engine.focused == []
            assert core.focus.snapshot.active_view_id == "scene_view"
            assert changes == []
        finally:
            core.shutdown()
            WindowManager._instance = previous_manager

    def test_visible_native_panel_activation_is_not_focus_history(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager

        class Panel:
            is_open = True

            @staticmethod
            def is_content_visible():
                return True

            @staticmethod
            def was_content_visible():
                return True

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            changes = []
            focus.add_change_listener(changes.append)
            focus.activate_panel(
                "scene_view",
                view_id="scene_view",
                record_history=False,
            )
            changes.clear()

            manager = _window_manager(object())
            manager.register_existing_window("inspector", Panel())
            manager.native_panel_focus_callback("inspector")(True, True)

            assert focus.snapshot.active_panel_id == "inspector"
            assert len(changes) == 1
            assert changes[0].record_history is False
            assert changes[0].reason == "pointer_panel_activation"
        finally:
            WindowManager._instance = previous_manager
            FocusService._instance = previous_focus

    def test_revealed_native_dock_tab_activation_remains_focus_history(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager

        class Panel:
            is_open = True

            @staticmethod
            def is_content_visible():
                # Native focus is published before the newly selected dock
                # tab commits its current-frame presentation state.
                return False

            @staticmethod
            def was_content_visible():
                return False

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            changes = []
            focus.add_change_listener(changes.append)
            focus.activate_panel(
                "scene_view",
                view_id="scene_view",
                record_history=False,
            )
            changes.clear()

            manager = _window_manager(object())
            manager.register_existing_window("inspector", Panel())
            manager.native_panel_focus_callback("inspector")(True, True)

            assert focus.snapshot.active_panel_id == "inspector"
            assert len(changes) == 1
            assert changes[0].record_history is True
            assert changes[0].reason == "pointer_panel_activation"
        finally:
            WindowManager._instance = previous_manager
            FocusService._instance = previous_focus

    def test_revealed_dock_tab_reports_the_tab_it_replaced(self, monkeypatch):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager

        class Panel:
            is_open = True

            @staticmethod
            def is_content_visible():
                return False

            @staticmethod
            def was_content_visible():
                return False

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            focus.activate_panel("console", view_id="console", record_history=False)
            changes = []
            focus.add_change_listener(changes.append)

            manager = _window_manager(object())
            manager.register_existing_window("scene_view", Panel())
            manager.register_existing_window("particle_graph_editor", Panel())
            monkeypatch.setattr(
                manager,
                "_native_window_presented_dock_peer",
                lambda window_id: "scene_view"
                if window_id == "particle_graph_editor"
                else "",
            )

            manager.native_panel_focus_callback("particle_graph_editor")(True, True)

            assert len(changes) == 1
            assert changes[0].before.active_view_id == "console"
            assert changes[0].after.active_view_id == "particle_graph_editor"
            assert changes[0].presentation_before_view_id == "scene_view"
        finally:
            WindowManager._instance = previous_manager
            FocusService._instance = previous_focus

    def test_user_window_command_captures_peer_before_dock_selection(
        self, monkeypatch
    ):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager

        class Engine:
            pass

        class Panel:
            is_open = True

            @staticmethod
            def is_content_visible():
                return True

            @staticmethod
            def was_content_visible():
                return False

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            focus.activate_panel("console", view_id="console", record_history=False)
            changes = []
            focus.add_change_listener(changes.append)

            manager = _window_manager(Engine())
            manager.register_window_type(
                "particle_graph_editor",
                Panel,
                "Particle Graph",
            )
            manager.register_existing_window("scene_view", Panel())
            manager.register_existing_window("particle_graph_editor", Panel())
            monkeypatch.setattr(
                manager,
                "_native_window_presented_dock_peer",
                lambda window_id: "scene_view"
                if window_id == "particle_graph_editor"
                else "",
            )
            monkeypatch.setattr(
                manager,
                "is_window_content_visible",
                lambda _window_id: False,
            )

            manager.open_window_from_user("particle_graph_editor")

            assert len(changes) == 1
            assert changes[0].before.active_view_id == "console"
            assert changes[0].presentation_before_view_id == "scene_view"
        finally:
            WindowManager._instance = previous_manager
            FocusService._instance = previous_focus

    def test_revealed_native_dock_tab_uses_previous_frame_visibility(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager

        class Panel:
            is_open = True

            @staticmethod
            def is_content_visible():
                # Some native callbacks arrive after the selected tab has
                # already submitted its current-frame contents.
                return True

            @staticmethod
            def was_content_visible():
                return False

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            changes = []
            focus.add_change_listener(changes.append)
            focus.activate_panel(
                "particle_graph_editor",
                view_id="particle_graph_editor",
                record_history=False,
            )
            changes.clear()

            manager = _window_manager(object())
            manager.register_existing_window("scene_view", Panel())
            manager.native_panel_focus_callback("scene_view")(True, True)

            assert focus.snapshot.active_panel_id == "scene_view"
            assert len(changes) == 1
            assert changes[0].record_history is True
            assert changes[0].reason == "pointer_panel_activation"
        finally:
            WindowManager._instance = previous_manager
            FocusService._instance = previous_focus

    def test_native_focus_visibility_comes_from_the_event_source_instance(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager

        class StaleRegisteredPanel:
            is_open = True

            @staticmethod
            def is_content_visible():
                return False

            @staticmethod
            def was_content_visible():
                return False

        class RenderingSourcePanel:
            is_open = True

            @staticmethod
            def is_content_visible():
                return True

            @staticmethod
            def was_content_visible():
                return True

        previous_manager = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            changes = []
            focus.add_change_listener(changes.append)
            focus.activate_panel(
                "inspector",
                view_id="inspector",
                record_history=False,
            )
            changes.clear()

            manager = _window_manager(object())
            manager.register_existing_window("hierarchy", StaleRegisteredPanel())
            source = RenderingSourcePanel()
            callback = manager.native_panel_focus_callback(
                "hierarchy",
                source_instance=source,
            )
            callback(True, True)

            assert focus.snapshot.active_panel_id == "hierarchy"
            assert len(changes) == 1
            assert changes[0].record_history is False
        finally:
            WindowManager._instance = previous_manager
            FocusService._instance = previous_focus

    def test_panel_child_context_can_be_restored_by_its_owner(self):
        from Infernux.engine.ui.window_manager import WindowManager

        class Panel:
            is_open = True

            def __init__(self):
                self.restored = []

            def restore_child_context(self, context_id):
                self.restored.append(context_id)
                return context_id in {"", "particle_graph.workspace.parameters"}

        previous = WindowManager._instance
        try:
            manager = _window_manager(object())
            panel = Panel()
            manager.register_existing_window("particle_graph_editor", panel)

            assert manager.restore_panel_child_context(
                "particle_graph_editor",
                "particle_graph.workspace.parameters",
            )
            assert panel.restored == ["particle_graph.workspace.parameters"]
            assert not manager.restore_panel_child_context(
                "particle_graph_editor",
                "particle_graph.workspace.missing",
            )
        finally:
            WindowManager._instance = previous

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
            manager = _window_manager(object())
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
        from Infernux.engine.interaction import FocusService
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
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            engine = Engine()
            manager = _window_manager(engine)
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
            focus.activate_panel("dynamic", view_id="dynamic", record_history=False)

            manager.close_window("dynamic")
            assert manager.get_window_state("dynamic") is WindowState.CLOSING
            assert focus.snapshot.active_panel_id == ""
            manager.observe_native_panel_focus("dynamic", True, view_id="dynamic")
            assert focus.snapshot.active_panel_id == ""
            manager.process_pending_actions()
            assert manager.get_window_state("dynamic") is WindowState.CLOSED
            assert "dynamic" not in engine.registered
        finally:
            FocusService._instance = previous_focus
            WindowManager._instance = previous

    def test_focus_window_requires_known_open_panel(self):
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focused = []

            def register_gui(self, _window_id, _instance):
                pass

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        class Panel:
            def __init__(self):
                self.is_open = True

            def set_open(self, value):
                self.is_open = bool(value)

        previous = WindowManager._instance
        try:
            engine = Engine()
            manager = _window_manager(engine)
            panel = Panel()
            manager.register_window_type("game_view", Panel, "Game", factory=Panel)
            manager.register_existing_window("game_view", panel, "game_view")

            manager.focus_window("game_view")
            assert manager.get_window_state("game_view") is WindowState.FOCUS_REQUESTED
            manager.process_pending_actions()
            assert manager.get_window_state("game_view") is WindowState.FOCUSED
            assert engine.focused == ["game_view"]

            manager.close_window("game_view")
            assert manager.get_window_state("game_view") is WindowState.CLOSED
            with pytest.raises(RuntimeError, match="not open"):
                manager.focus_window("game_view")
            with pytest.raises(KeyError, match="Unknown window"):
                manager.focus_window("missing")
        finally:
            WindowManager._instance = previous

    def test_load_state_projects_restored_front_tab_into_focus_service(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focused = []

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        class Panel:
            def __init__(self):
                self.is_open = True

            def set_open(self, value):
                self.is_open = bool(value)

        previous = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            focus.activate_panel(
                "particle_graph_editor",
                view_id="particle_graph_editor",
                record_history=False,
            )
            engine = Engine()
            manager = _window_manager(engine)
            manager.register_window_type("console", Panel, "Console", factory=Panel)
            manager.register_existing_window("console", Panel(), "console")

            manager.load_state(
                {
                    "open_windows": {"console": True},
                    "active_panel_id": "particle_graph_editor",
                    "project_console_front_id": "console",
                }
            )

            assert engine.focused == ["console"]
            assert manager.get_window_state("console") is WindowState.FOCUSED
            assert focus.snapshot.active_panel_id == "console"
            assert focus.snapshot.active_view_id == "console"
        finally:
            WindowManager._instance = previous
            FocusService._instance = previous_focus

    def test_completed_imgui_focus_reconciles_child_window_to_owning_panel(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Panel:
            def __init__(self):
                self.is_open = True

        previous = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            focus.activate_panel(
                "particle_graph_editor",
                view_id="particle_graph_editor",
                record_history=False,
            )
            manager = _window_manager(object())
            manager.register_existing_window("particle_graph_editor", Panel())
            manager.register_existing_window("project", Panel())

            child_id = "project/##project_browser/##file_grid"
            assert manager.resolve_native_gui_panel_id(child_id) == "project"
            assert manager.observe_native_gui_window_focus(child_id)
            assert focus.snapshot.active_panel_id == "project"
            assert manager.get_window_state("project") is WindowState.FOCUSED

            assert not manager.observe_native_gui_window_focus("##SaveResourceModal")
            assert focus.snapshot.active_panel_id == "project"
            assert not manager.observe_native_gui_window_focus(child_id)
        finally:
            WindowManager._instance = previous
            FocusService._instance = previous_focus

    def test_completed_imgui_focus_rebinds_document_for_already_focused_panel(self):
        from Infernux.engine.interaction import (
            DocumentKind,
            DocumentRegistry,
            FocusService,
        )
        from Infernux.engine.ui.window_manager import WindowManager

        class Panel:
            def __init__(self):
                self.is_open = True

        previous = WindowManager._instance
        previous_focus = FocusService._instance
        previous_registry = DocumentRegistry._instance
        try:
            focus = FocusService()
            registry = DocumentRegistry()
            manager = _window_manager(object())
            manager.register_existing_window("inspector", Panel())

            child_id = "inspector/##asset_inspector"
            assert manager.observe_native_gui_window_focus(child_id)
            assert focus.snapshot.active_panel_id == "inspector"
            assert focus.snapshot.active_document_id == ""

            document = registry.create(DocumentKind.MATERIAL, "Material")
            registry.attach_view(document.document_id, "inspector")

            # The native panel did not change, but its active document did.
            assert manager.observe_native_gui_window_focus(child_id)
            assert focus.snapshot.active_panel_id == "inspector"
            assert focus.snapshot.active_document_id == document.document_id
            assert not manager.observe_native_gui_window_focus(child_id)
        finally:
            WindowManager._instance = previous
            DocumentRegistry._instance = previous_registry
            FocusService._instance = previous_focus

    def test_dynamic_window_locator_restores_instance_and_focuses_after_register(self):
        from Infernux.engine.interaction import ContextRestoreStatus, FocusService
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

            def set_open(self, value):
                self._is_open = bool(value)

            def open(self):
                self._is_open = True

            def set_window_manager(self, manager):
                self.manager = manager

        previous = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            focus_changes = []
            focus.add_change_listener(focus_changes.append)
            engine = Engine()
            manager = _window_manager(engine)
            manager.register_window_type("graph", Panel, "Graph", factory=Panel)
            manager.open_window("graph", instance_id="graph/asset-a")
            manager.process_pending_actions()
            locator = manager.locate_window("graph/asset-a")
            assert locator.window_id == "graph/asset-a"
            assert locator.type_id == "graph"

            manager.close_window("graph/asset-a")
            manager.process_pending_actions()
            assert manager.get_window_state("graph/asset-a") is WindowState.CLOSED

            assert manager.restore_window(locator) is ContextRestoreStatus.PENDING
            assert manager.get_window_state("graph/asset-a") is WindowState.OPENING
            manager.process_pending_actions()

            assert manager.get_window_state("graph/asset-a") is WindowState.FOCUSED
            assert "graph/asset-a" in engine.registered
            assert engine.focused == ["graph/asset-a"]
            assert manager.restore_window(locator) is ContextRestoreStatus.PENDING

            manager.observe_native_panel_focus(
                "graph/asset-a", True, view_id="graph/asset-a"
            )
            assert manager.restore_window(locator) is ContextRestoreStatus.READY
            assert [change.record_history for change in focus_changes] == [False]
        finally:
            WindowManager._instance = previous
            FocusService._instance = previous_focus

    def test_visible_window_locator_does_not_steal_focus(self):
        from Infernux.engine.interaction import ContextRestoreStatus, FocusService
        from Infernux.engine.ui.window_manager import WindowManager

        class Engine:
            def __init__(self):
                self.focused = []

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        class Panel:
            is_open = True

            @staticmethod
            def is_content_visible():
                return True

            @staticmethod
            def was_content_visible():
                return True

        previous = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            focus.activate_panel(
                "scene_view",
                view_id="scene_view",
                record_history=False,
            )
            engine = Engine()
            manager = _window_manager(engine)
            manager.register_existing_window("inspector", Panel())

            locator = manager.locate_window("inspector")
            assert manager.is_window_content_visible("inspector")
            assert manager.was_window_content_visible("inspector")
            assert manager.restore_window(locator) is ContextRestoreStatus.READY
            assert focus.snapshot.active_view_id == "scene_view"
            assert engine.focused == []
        finally:
            WindowManager._instance = previous
            FocusService._instance = previous_focus

    def test_hidden_dock_tab_still_requests_focus(self):
        from Infernux.engine.interaction import ContextRestoreStatus, FocusService
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focused = []

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        class Panel:
            is_open = True

            @staticmethod
            def is_content_visible():
                return False

            @staticmethod
            def was_content_visible():
                return False

        previous = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            FocusService()
            engine = Engine()
            manager = _window_manager(engine)
            manager.register_existing_window("console", Panel())
            locator = manager.locate_window("console")

            assert manager.restore_window(locator) is ContextRestoreStatus.PENDING
            assert manager.get_window_state("console") is WindowState.FOCUS_REQUESTED
            manager.process_pending_actions()
            assert engine.focused == ["console"]
        finally:
            WindowManager._instance = previous
            FocusService._instance = previous_focus

    def test_hidden_dock_tab_does_not_restore_from_logical_focus_alone(self):
        from Infernux.engine.interaction import ContextRestoreStatus, FocusService
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focused = []

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        class Panel:
            is_open = True
            presented = False

            def is_content_visible(self):
                return self.presented

            def was_content_visible(self):
                return self.presented

        previous = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            engine = Engine()
            panel = Panel()
            manager = _window_manager(engine)
            manager.register_existing_window("scene_view", panel)
            locator = manager.locate_window("scene_view")

            # Context restore applies its logical snapshot before asking the
            # WindowManager to reveal the recorded dock tab.
            focus.activate_panel(
                "scene_view",
                view_id="scene_view",
                record_history=False,
            )
            manager._window_states["scene_view"] = WindowState.FOCUSED

            assert manager.restore_window(locator) is ContextRestoreStatus.PENDING
            assert manager.get_window_state("scene_view") is WindowState.FOCUS_REQUESTED
            manager.process_pending_actions()
            assert engine.focused == ["scene_view"]

            assert manager.restore_window(locator) is ContextRestoreStatus.READY
        finally:
            WindowManager._instance = previous
            FocusService._instance = previous_focus

    def test_builtin_window_locator_restores_without_dynamic_type_registration(self):
        from Infernux.engine.interaction import ContextRestoreStatus, FocusService
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.focused = []

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        class Panel:
            def __init__(self):
                self._is_open = True

            @property
            def is_open(self):
                return self._is_open

            def set_open(self, value):
                self._is_open = bool(value)

        previous = WindowManager._instance
        previous_focus = FocusService._instance
        try:
            focus = FocusService()
            engine = Engine()
            panel = Panel()
            manager = _window_manager(engine)
            manager.register_existing_window("hierarchy", panel)
            locator = manager.locate_window("hierarchy")

            manager.close_window("hierarchy")
            assert manager.get_window_state("hierarchy") is WindowState.CLOSED
            assert panel.is_open is False

            assert manager.restore_window(locator) is ContextRestoreStatus.PENDING
            assert manager.get_window_state("hierarchy") is WindowState.FOCUS_REQUESTED
            assert panel.is_open is True
            manager.process_pending_actions()
            manager.observe_native_panel_focus(
                "hierarchy", True, view_id="hierarchy"
            )

            assert manager.restore_window(locator) is ContextRestoreStatus.READY
            assert engine.focused == ["hierarchy"]
        finally:
            WindowManager._instance = previous
            FocusService._instance = previous_focus

    def test_builtin_window_closes_without_unregistering(self):
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry
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
        previous_registry = DocumentRegistry._instance
        try:
            registry = DocumentRegistry()
            engine = Engine()
            manager = _window_manager(engine)
            panel = Panel()
            engine.registered["builtin"] = panel
            manager.register_window_type("builtin", Panel, "Builtin", factory=Panel)
            manager.register_existing_window("builtin", panel, "builtin")
            document = registry.create(DocumentKind.SCENE, "Scene")
            registry.attach_view(document.document_id, "builtin")

            manager.close_window("builtin")
            assert manager.get_window_state("builtin") is WindowState.CLOSED
            assert engine.registered["builtin"] is panel
            assert panel.is_open is False
            assert registry.document_for_view("builtin") is document

            assert manager.open_window("builtin") is panel
            manager.process_pending_actions()
            assert manager.get_window_state("builtin") is WindowState.FOCUSED
            assert panel.is_open is True
            assert engine.focused == ["builtin"]
        finally:
            DocumentRegistry._instance = previous_registry
            WindowManager._instance = previous

    def test_destroyed_dynamic_window_releases_its_document_view(self):
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.window_manager import WindowManager

        class Engine:
            @staticmethod
            def register_gui(_window_id, _instance):
                pass

            @staticmethod
            def unregister_gui(_window_id):
                pass

        previous_manager = WindowManager._instance
        previous_registry = DocumentRegistry._instance
        try:
            registry = DocumentRegistry()
            manager = _window_manager(Engine())
            manager.register_window_type(
                "graph", ClosablePanel, "Graph", factory=lambda: ClosablePanel("Graph", "graph")
            )
            panel = manager.open_window("graph")
            manager.process_pending_actions()
            document = registry.create(DocumentKind.PARTICLE_GRAPH, "Graph")
            panel.bind_document(document.document_id)

            manager.close_window("graph")

            assert panel.document_id == ""
            assert registry.document_for_view("graph") is None
            assert registry.get(document.document_id) is None
        finally:
            DocumentRegistry._instance = previous_registry
            WindowManager._instance = previous_manager

    def test_reopened_dynamic_singleton_restores_its_document_binding(self):
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.window_manager import WindowManager

        class Engine:
            @staticmethod
            def register_gui(_window_id, _instance):
                pass

            @staticmethod
            def unregister_gui(_window_id):
                pass

        previous_manager = WindowManager._instance
        previous_registry = DocumentRegistry._instance
        try:
            registry = DocumentRegistry()
            manager = _window_manager(Engine())
            manager.register_window_type(
                "graph",
                ClosablePanel,
                "Graph",
                factory=lambda: ClosablePanel("Graph", "graph"),
            )
            panel = manager.open_window("graph")
            manager.process_pending_actions()
            document = registry.create(DocumentKind.PARTICLE_GRAPH, "Graph")
            panel.bind_document(document.document_id)
            original_stable_id = document.stable_id

            manager.close_window("graph")
            manager.process_pending_actions()
            assert panel.document_id == ""

            reopened = manager.open_window("graph")
            manager.process_pending_actions()

            assert reopened is panel
            assert reopened.document_id == document.document_id
            restored = registry.require(reopened.document_id)
            assert restored.stable_id == original_stable_id
            assert registry.document_for_view("graph") is restored
        finally:
            DocumentRegistry._instance = previous_registry
            WindowManager._instance = previous_manager

    def test_dynamic_window_reopen_waits_for_native_close_transaction(self):
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.registered = {}
                self.events = []

            def register_gui(self, window_id, instance):
                assert window_id not in self.registered
                self.registered[window_id] = instance
                self.events.append(("register", window_id))

            def unregister_gui(self, window_id):
                self.registered.pop(window_id)
                self.events.append(("unregister", window_id))

        class Panel:
            def __init__(self):
                self.is_open = True

            def open(self):
                self.is_open = True

            def set_open(self, value):
                self.is_open = bool(value)

            def request_close(self):
                return True

        previous = WindowManager._instance
        try:
            engine = Engine()
            manager = _window_manager(engine)
            manager.register_window_type("graph", Panel, "Graph", factory=Panel)
            panel = manager.open_window("graph")
            manager.process_pending_actions()
            assert manager.get_window_state("graph") is WindowState.OPEN

            manager.close_window("graph")
            assert manager.get_window_state("graph") is WindowState.CLOSING
            assert manager.open_window("graph") is panel
            assert manager.get_window_state("graph") is WindowState.CLOSING

            manager.process_pending_actions()

            assert engine.events == [
                ("register", "graph"),
                ("unregister", "graph"),
                ("register", "graph"),
            ]
            assert manager.get_window_state("graph") is WindowState.OPEN
            assert manager.get_window_instance("graph") is panel
            assert panel.is_open is True
        finally:
            WindowManager._instance = previous

    def test_user_reopen_during_close_publishes_focus_after_registration(self, monkeypatch):
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel
        from Infernux.engine.ui.window_manager import WindowManager, WindowState

        class Engine:
            def __init__(self):
                self.registered = {}
                self.focused = []

            def register_gui(self, window_id, instance):
                assert window_id not in self.registered
                self.registered[window_id] = instance

            def unregister_gui(self, window_id):
                self.registered.pop(window_id)

            def select_docked_window(self, window_id):
                self.focused.append(window_id)

        previous_manager = WindowManager._instance
        core = EditorInteractionCore()
        changes = []
        core.focus.add_change_listener(changes.append)
        try:
            monkeypatch.setattr(
                WindowManager,
                "_native_window_content_visible",
                staticmethod(lambda _window_id: False),
            )
            monkeypatch.setattr(
                WindowManager,
                "_native_window_presented_dock_peer",
                staticmethod(lambda _window_id: "project"),
            )
            engine = Engine()
            manager = _window_manager(engine, core.panels)
            core.panels.register_type("graph", PanelInteractionDescriptor())
            manager.register_window_type(
                "graph",
                ClosablePanel,
                "Graph",
                factory=lambda: ClosablePanel("Graph", "graph"),
            )
            panel = manager.open_window("graph")
            manager.process_pending_actions()
            core.focus.activate_panel(
                "project",
                view_id="project",
                record_history=False,
            )
            changes.clear()

            manager.close_window("graph")
            assert manager.get_window_state("graph") is WindowState.CLOSING
            assert manager.open_window_from_user("graph") is panel
            assert core.focus.snapshot.active_view_id == "project"

            manager.process_pending_actions()

            assert manager.get_window_state("graph") is WindowState.FOCUSED
            assert engine.registered["graph"] is panel
            assert engine.focused == ["graph"]
            assert core.focus.snapshot.active_view_id == "graph"
            recorded = [change for change in changes if change.record_history]
            assert len(recorded) == 1
            assert recorded[0].reason == "window_open_command"
        finally:
            core.shutdown()
            WindowManager._instance = previous_manager

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
            manager = _window_manager(Engine())
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
            self.id = id(self)
            self._components = [] if canvas is None else [canvas]

        def get_py_components(self):
            return list(self._components)

        @staticmethod
        def get_children():
            return []

    class _Scene:
        name = "SameName"
        structure_version = 7

        def __init__(self, canvas):
            self._root = TestUICanvasCollectionCache._Root(canvas)
            self.world_id = id(self)
            self.temporal_discontinuity_revision = 0
            self.root_queries = 0

        def get_root_objects(self):
            self.root_queries += 1
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

    def test_temporal_discontinuity_rebuilds_same_scene_canvas_cache(self):
        from Infernux.ui import UICanvas
        from Infernux.ui.ui_canvas_utils import collect_canvases, invalidate_canvas_cache

        first_canvas = UICanvas()
        second_canvas = UICanvas()
        scene = self._Scene(first_canvas)

        invalidate_canvas_cache()
        assert collect_canvases(scene) == [first_canvas]

        scene._root = self._Root(second_canvas)
        scene.temporal_discontinuity_revision += 1
        assert collect_canvases(scene) == [second_canvas]

    def test_runtime_collection_includes_persistent_scene_canvases(self):
        from Infernux.ui import UICanvas
        from Infernux.ui.ui_canvas_utils import (
            collect_sorted_runtime_canvases,
            invalidate_canvas_cache,
        )

        active_canvas = UICanvas()
        active_canvas.sort_order = 10
        persistent_canvas = UICanvas()
        persistent_canvas.sort_order = 5

        invalidate_canvas_cache()
        canvases = collect_sorted_runtime_canvases(
            self._Scene(active_canvas),
            self._Scene(persistent_canvas),
        )

        assert canvases == [persistent_canvas, active_canvas]

    def test_runtime_collection_with_owners_includes_persistent_scene(self):
        from Infernux.ui import UICanvas
        from Infernux.ui.ui_canvas_utils import (
            collect_runtime_canvases_with_go,
            invalidate_canvas_cache,
        )

        active_canvas = UICanvas()
        persistent_canvas = UICanvas()
        active_scene = self._Scene(active_canvas)
        persistent_scene = self._Scene(persistent_canvas)

        invalidate_canvas_cache()
        pairs = collect_runtime_canvases_with_go(active_scene, persistent_scene)

        assert [canvas for _, canvas in pairs] == [active_canvas, persistent_canvas]
        assert [owner for owner, _ in pairs] == [
            active_scene._root,
            persistent_scene._root,
        ]

    def test_runtime_collection_does_not_duplicate_same_scene(self):
        from Infernux.ui import UICanvas
        from Infernux.ui.ui_canvas_utils import (
            collect_runtime_canvases_with_go,
            invalidate_canvas_cache,
        )

        canvas = UICanvas()
        scene = self._Scene(canvas)

        invalidate_canvas_cache()
        assert collect_runtime_canvases_with_go(scene, scene) == [
            (scene._root, canvas),
        ]

    def test_runtime_collection_tracks_dynamic_canvas_attach_and_detach(self):
        from Infernux.ui import UICanvas
        import Infernux.ui.ui_canvas_utils as canvas_utils

        scene = self._Scene(None)
        canvas_utils.invalidate_canvas_cache()
        assert canvas_utils.collect_runtime_canvases_with_go(scene) == []

        canvas = UICanvas()
        scene._root._components.append(canvas)
        canvas._set_game_object(scene._root)
        assert canvas_utils.collect_runtime_canvases_with_go(scene) == [
            (scene._root, canvas),
        ]

        scene._root._components.remove(canvas)
        canvas._set_game_object(None)
        assert canvas_utils.collect_runtime_canvases_with_go(scene) == []

    def test_runtime_collection_ignores_unrelated_scene_structure_changes(self):
        from Infernux.ui import UICanvas
        from Infernux.ui.ui_canvas_utils import (
            collect_sorted_runtime_canvases,
            invalidate_canvas_cache,
        )

        canvas = UICanvas()
        scene = self._Scene(canvas)
        invalidate_canvas_cache()

        assert collect_sorted_runtime_canvases(scene) == [canvas]
        assert scene.root_queries == 1

        scene.structure_version += 100_000
        assert collect_sorted_runtime_canvases(scene) == [canvas]
        assert scene.root_queries == 1


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


def test_canvas_reference_resolution_scales_without_center_crop():
    from Infernux.ui import UICanvas

    canvas = UICanvas()
    canvas.reference_width = 1920
    canvas.reference_height = 1080
    canvas.match_width_or_height = 0.5

    wide_scale_x, wide_scale_y, _ = canvas.compute_scale(3200, 1440)
    wide_width, wide_height = canvas.compute_logical_size(3200, 1440)
    assert wide_scale_x == pytest.approx(wide_scale_y)
    assert wide_width * wide_scale_x == pytest.approx(3200.0)
    assert wide_height * wide_scale_y == pytest.approx(1440.0)

    portrait_scale_x, portrait_scale_y, _ = canvas.compute_scale(824, 1830)
    portrait_width, portrait_height = canvas.compute_logical_size(824, 1830)
    assert portrait_scale_x == pytest.approx(portrait_scale_y)
    assert portrait_width * portrait_scale_x == pytest.approx(824.0)
    assert portrait_height * portrait_scale_y == pytest.approx(1830.0)

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


def test_focused_save_rejects_stale_document_instead_of_saving_scene():
    from Infernux.engine.interaction import (
        DocumentCapability,
        DocumentKind,
        DocumentRegistry,
        EditorSaveService,
        FocusService,
    )
    from Infernux.engine.scene_manager import SceneFileManager

    class Controller:
        calls = 0

        def save(self, *, ticket, save_as=False):
            self.calls += 1
            DocumentRegistry.instance().complete_save(ticket.ticket_id, success=True)

    registry = DocumentRegistry()
    scene_controller = Controller()
    scene = registry.create(
        DocumentKind.SCENE,
        "Scene",
        document_id="scene-document",
        capabilities=DocumentCapability.SAVE | DocumentCapability.SAVE_AS,
        controller=scene_controller,
    )
    scene.revision = 1
    previous_scene = SceneFileManager._instance
    previous_focus = FocusService._instance
    previous_saving = EditorSaveService._instance
    SceneFileManager._instance = type("SceneFiles", (), {"document_id": scene.document_id})()
    focus = FocusService()
    saving = EditorSaveService(registry)
    try:
        focus.activate_panel(
            "timeline",
            view_id="timeline",
            document_id="missing-document",
        )
        result = saving.save_focused()
    finally:
        EditorSaveService._instance = previous_saving
        FocusService._instance = previous_focus
        SceneFileManager._instance = previous_scene

    assert result.accepted is False
    assert "no longer registered" in result.result.message
    assert scene_controller.calls == 0


class TestPanelFocusEvents:
    def test_scene_change_binds_every_scene_backed_view_to_one_document(self):
        from Infernux.engine.bootstrap import EditorBootstrap
        from Infernux.engine.interaction import SelectionService

        class SceneFiles:
            document_id = "scene-document"
            callback = None

            def set_on_scene_changed(self, callback):
                self.callback = callback

        class View:
            def __init__(self):
                self.bound = []

            def bind_document(self, document_id):
                self.bound.append(document_id)

        bootstrap = EditorBootstrap.__new__(EditorBootstrap)
        bootstrap.scene_file_manager = SceneFiles()
        bootstrap.scene_view = View()
        bootstrap.scene_view._fly_to_active = True
        bootstrap.scene_view._fly_to_last_obj_id = 17
        bootstrap.scene_view._fly_to_close = True
        bootstrap.game_view = View()
        bootstrap.ui_editor = View()

        previous_selection = SelectionService._instance
        SelectionService()
        try:
            bootstrap._setup_scene_change_cleanup()
            bootstrap.scene_file_manager.callback()
        finally:
            SelectionService._instance = previous_selection

        assert bootstrap.scene_view.bound == ["scene-document"]
        assert bootstrap.game_view.bound == ["scene-document"]
        assert bootstrap.ui_editor.bound == ["scene-document"]

    def test_document_binding_projects_focus_without_user_history(self):
        from Infernux.engine.interaction import (
            DocumentKind,
            DocumentRegistry,
            FocusService,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel

        previous_focus = FocusService._instance
        previous_registry = DocumentRegistry._instance
        focus = FocusService()
        registry = DocumentRegistry()
        changes = []
        focus.add_change_listener(changes.append)
        try:
            panel = ClosablePanel("Graph", "graph")
            document = registry.create(DocumentKind.GENERIC, "Graph")
            focus.activate_panel("graph", view_id="graph", record_history=False)
            changes.clear()

            panel.bind_document(document.document_id)
            assert focus.snapshot.active_document_id == document.document_id
            assert [change.record_history for change in changes] == [False]

            changes.clear()
            panel.unbind_document()
            assert focus.snapshot.active_document_id == ""
            assert [change.record_history for change in changes] == [False]
        finally:
            DocumentRegistry._instance = previous_registry
            FocusService._instance = previous_focus

    def test_closable_panel_publishes_focus_only_through_focus_service(self):
        from Infernux.engine.ui.closable_panel import ClosablePanel

        class FocusContext:
            def set_window_focus(self):
                pass

        received = []
        from Infernux.engine.interaction import FocusService

        previous_focus = FocusService._instance
        focus = FocusService()
        focus.add_listener(received.append)
        try:
            panel = ClosablePanel("Focus Test", "focus_test")
            panel._activate_panel(FocusContext(), focus_window=True)
            panel._activate_panel(FocusContext(), focus_window=True)
            assert [snapshot.active_view_id for snapshot in received] == [
                "focus_test"
            ]
            assert not hasattr(ClosablePanel, "set_on_panel_focus_changed")
        finally:
            FocusService._instance = previous_focus

    def test_untyped_editor_event_bus_cannot_be_reintroduced(self):
        from pathlib import Path

        root = Path("python/Infernux/engine")
        assert not (root / "ui" / "event_bus.py").exists()
        assert all(
            "EditorEventBus" not in path.read_text(encoding="utf-8")
            for path in root.rglob("*.py")
        )

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
                return flags == 1

        panel = ClosablePanel("Child Focus Test", "child_focus_test")
        ctx = FocusContext()
        from Infernux.engine.interaction import FocusService

        previous_focus = FocusService._instance
        focus = FocusService()
        try:
            focus.activate_panel(panel.window_id)
            panel._panel_was_focused = True

            assert panel._begin_closable_window(ctx) is True
            assert ClosablePanel.get_active_panel_id() == panel.window_id
            assert ctx.focus_flags == [1]
        finally:
            FocusService._instance = previous_focus

    def test_closable_panel_records_pointer_driven_dock_tab_focus(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.closable_panel import ClosablePanel

        class FocusContext:
            @staticmethod
            def begin_window_closable(_title, _open, _flags):
                return True, True

            @staticmethod
            def is_window_hovered(_flags):
                return False

            @staticmethod
            def is_mouse_button_clicked(button):
                return button == 0

            @staticmethod
            def is_window_focused(_flags):
                return True

        previous_focus = FocusService._instance
        focus = FocusService()
        changes = []
        focus.add_change_listener(changes.append)
        try:
            focus.activate_panel("scene", record_history=False)
            changes.clear()
            panel = ClosablePanel("Docked", "docked")

            assert panel._begin_closable_window(FocusContext()) is True
            assert focus.snapshot.active_panel_id == "docked"
            assert len(changes) == 1
            assert changes[0].record_history is True
        finally:
            FocusService._instance = previous_focus

    def test_closable_panel_does_not_claim_click_consumed_by_popup(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.closable_panel import ClosablePanel

        class FocusContext:
            @staticmethod
            def begin_window_closable(_title, _open, _flags):
                return True, True

            @staticmethod
            def is_window_hovered(_flags):
                return True

            @staticmethod
            def is_mouse_button_clicked(button):
                return button == 0

            @staticmethod
            def is_window_focused(_flags):
                return True

            @staticmethod
            def is_pointer_activation_blocked_by_popup():
                return True

        previous_focus = FocusService._instance
        focus = FocusService()
        changes = []
        focus.add_change_listener(changes.append)
        try:
            focus.activate_panel("scene", record_history=False)
            changes.clear()
            panel = ClosablePanel("History", "history")

            assert panel._begin_closable_window(FocusContext()) is True
            assert focus.snapshot.active_panel_id == "scene"
            assert changes == []
            assert panel._last_pointer_press_at == 0.0
        finally:
            FocusService._instance = previous_focus

    def test_closable_panel_does_not_record_focus_when_already_visible(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.closable_panel import ClosablePanel

        class FocusContext:
            @staticmethod
            def begin_window_closable(_title, _open, _flags):
                return True, True

            @staticmethod
            def is_window_hovered(_flags):
                return True

            @staticmethod
            def is_mouse_button_clicked(button):
                return button == 0

            @staticmethod
            def is_window_focused(_flags):
                return True

            @staticmethod
            def set_window_focus():
                pass

        previous_focus = FocusService._instance
        focus = FocusService()
        changes = []
        focus.add_change_listener(changes.append)
        try:
            focus.activate_panel("scene", record_history=False)
            changes.clear()
            panel = ClosablePanel("Docked", "docked")
            panel._content_visible_previous_frame = True

            assert panel._begin_closable_window(FocusContext()) is True
            assert focus.snapshot.active_panel_id == "docked"
            assert len(changes) == 1
            assert changes[0].record_history is False
        finally:
            FocusService._instance = previous_focus

    def test_closable_panel_carries_dock_press_to_next_focus_frame(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.closable_panel import ClosablePanel

        class FocusContext:
            def __init__(self):
                self.visible = False
                self.clicked = True

            def begin_window_closable(self, _title, _open, _flags):
                return self.visible, True

            @staticmethod
            def is_window_hovered(_flags):
                return False

            def is_mouse_button_clicked(self, button):
                return self.clicked and button == 0

            def is_window_focused(self, _flags):
                return self.visible

        previous_focus = FocusService._instance
        focus = FocusService()
        changes = []
        focus.add_change_listener(changes.append)
        try:
            focus.activate_panel("scene", record_history=False)
            changes.clear()
            panel = ClosablePanel("Docked", "docked")
            ctx = FocusContext()

            assert panel._begin_closable_window(ctx) is False
            ctx.visible = True
            ctx.clicked = False
            assert panel._begin_closable_window(ctx) is True

            assert focus.snapshot.active_panel_id == "docked"
            assert len(changes) == 1
            assert changes[0].record_history is True
        finally:
            FocusService._instance = previous_focus

    def test_hidden_dock_tab_clears_stale_focus_latch_before_reveal(self):
        from Infernux.engine.interaction import FocusService
        from Infernux.engine.ui.closable_panel import ClosablePanel

        class FocusContext:
            def __init__(self):
                self.visible = False
                self.clicked = False

            def begin_window_closable(self, _title, _open, _flags):
                return self.visible, True

            @staticmethod
            def is_window_hovered(_flags):
                return False

            def is_mouse_button_clicked(self, button):
                return self.clicked and button == 0

            def is_window_focused(self, _flags):
                return self.visible

        previous_focus = FocusService._instance
        focus = FocusService()
        changes = []
        focus.add_change_listener(changes.append)
        try:
            panel = ClosablePanel("Scene", "scene_view")
            panel._panel_was_focused = True
            focus.activate_panel(
                "particle_graph_editor",
                view_id="particle_graph_editor",
                record_history=False,
            )
            changes.clear()
            ctx = FocusContext()

            # The Scene tab is still submitted by the dock host, but its
            # content is hidden behind Particle Graph.
            assert panel._begin_closable_window(ctx) is False
            assert panel._panel_was_focused is False
            assert focus.snapshot.active_view_id == "particle_graph_editor"

            # Revealing Scene must now create an independent navigation item.
            ctx.visible = True
            ctx.clicked = True
            assert panel._begin_closable_window(ctx) is True

            assert focus.snapshot.active_view_id == "scene_view"
            assert len(changes) == 1
            assert changes[0].record_history is True
        finally:
            FocusService._instance = previous_focus

    def test_non_authoring_panel_does_not_create_a_legacy_document(self):
        from Infernux.engine.interaction import DocumentRegistry
        from Infernux.engine.ui.closable_panel import ClosablePanel

        panel = ClosablePanel("Probe", "dirty_probe")
        panel._sync_dirty_registry()
        panel._sync_dirty_registry()

        assert DocumentRegistry.instance().document_for_view(panel.window_id) is None

    def test_bound_document_dirty_state_is_a_per_view_panel_title_capability(self):
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry
        from Infernux.engine.ui.closable_panel import ClosablePanel

        previous_registry = DocumentRegistry._instance
        registry = DocumentRegistry()
        try:
            document = registry.create(DocumentKind.PROJECT_SETTINGS, "Settings")
            panel = ClosablePanel("Settings", "settings")
            panel.bind_document(document.document_id)

            assert panel._window_title_suffix() == ""
            registry.mark_changed(document.document_id)
            assert panel._window_title_suffix() == " *"
            registry.mark_saved(document.document_id)
            assert panel._window_title_suffix() == ""
        finally:
            DocumentRegistry._instance = previous_registry

    def test_shared_scene_document_does_not_broadcast_dirty_titles_to_every_view(self):
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry
        from Infernux.engine.ui.closable_panel import ClosablePanel

        previous_registry = DocumentRegistry._instance
        registry = DocumentRegistry()
        try:
            document = registry.create(DocumentKind.SCENE, "Main")
            scene = ClosablePanel("Scene", "scene_view")
            game = ClosablePanel("Game", "game_view")
            ui = ClosablePanel("UI Editor", "ui_editor")
            for panel in (scene, game, ui):
                panel.bind_document(document.document_id)

            registry.mark_changed(document.document_id, view_id="ui_editor")
            assert scene._window_title_suffix() == ""
            assert game._window_title_suffix() == ""
            assert ui._window_title_suffix() == " *"

            registry.mark_changed(document.document_id, view_id="scene_view")
            assert scene._window_title_suffix() == " *"
            assert game._window_title_suffix() == ""
            assert ui._window_title_suffix() == " *"

            registry.mark_saved(document.document_id)
            assert all(panel._window_title_suffix() == "" for panel in (scene, game, ui))
        finally:
            DocumentRegistry._instance = previous_registry

    def test_legacy_shared_document_without_owner_chooses_one_view_instead_of_broadcasting(self):
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry
        from Infernux.engine.ui.closable_panel import ClosablePanel

        previous_registry = DocumentRegistry._instance
        registry = DocumentRegistry()
        try:
            document = registry.create(
                DocumentKind.GENERIC,
                "Shared",
                revision=1,
                saved_revision=0,
            )
            first = ClosablePanel("First", "first")
            second = ClosablePanel("Second", "second")
            first.bind_document(document.document_id)
            second.bind_document(document.document_id)

            assert first._window_title_suffix() == " *"
            assert second._window_title_suffix() == ""
        finally:
            DocumentRegistry._instance = previous_registry

    def test_unbound_authoring_panel_reports_once_without_creating_state(
        self, monkeypatch
    ):
        from Infernux.debug import Debug
        from Infernux.engine.interaction import (
            DocumentRegistry,
            PanelInteractionDescriptor,
        )
        from Infernux.engine.ui.closable_panel import ClosablePanel

        class _AuthoringProbe(ClosablePanel):
            PANEL_INTERACTION = PanelInteractionDescriptor(document_backed=True)

        errors = []
        monkeypatch.setattr(Debug, "log_error", errors.append)
        panel = _AuthoringProbe("Probe", "dirty_probe")
        panel._sync_dirty_registry()
        panel._sync_dirty_registry()

        assert DocumentRegistry.instance().document_for_view(panel.window_id) is None
        assert len(errors) == 1
        assert "has no formal DocumentRegistry binding" in errors[0]


class TestSceneViewPicking:
    def test_particle_query_does_not_hide_scene_lifetime_failure(self, monkeypatch):
        import Infernux.lib as infernux_lib
        from Infernux.engine.ui import _scene_view_picking as picking

        class SceneManager:
            @staticmethod
            def instance():
                raise RuntimeError("scene lifetime failure")

        monkeypatch.setattr(infernux_lib, "SceneManager", SceneManager)

        with pytest.raises(RuntimeError, match="scene lifetime failure"):
            picking._owns_particle_system(42)

    def test_pick_ray_rebuild_failure_is_not_reordered_as_a_valid_hit(self):
        from Infernux.engine.ui._scene_view_picking import SceneViewPickingMixin

        class Engine:
            @staticmethod
            def screen_to_world_ray(*_args):
                raise RuntimeError("camera ray unavailable")

        probe = SceneViewPickingMixin()
        probe._engine = Engine()

        with pytest.raises(RuntimeError, match="camera ray unavailable"):
            probe._insert_ids_by_depth([7], [9], 10.0, 12.0, 100.0, 80.0)

    def test_skinned_renderer_counts_as_mesh_pick_geometry(self, monkeypatch):
        import Infernux.lib as infernux_lib
        from Infernux.components.builtin import MeshRenderer, SkinnedMeshRenderer
        from Infernux.engine.ui import _scene_view_picking as picking

        class Object:
            requested_types = []

            @classmethod
            def get_component(cls, component_type):
                cls.requested_types.append(component_type)
                return object() if component_type is SkinnedMeshRenderer else None

        class Scene:
            @staticmethod
            def find_by_id(object_id):
                return Object if object_id == 42 else None

        class SceneManager:
            @staticmethod
            def instance():
                return SceneManager()

            @staticmethod
            def get_active_scene():
                return Scene()

        monkeypatch.setattr(infernux_lib, "SceneManager", SceneManager)

        assert picking._has_mesh_pick_geometry(42) is True
        assert Object.requested_types == [MeshRenderer, SkinnedMeshRenderer]

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

    def test_scene_click_defers_mesh_selection_until_gpu_refinement(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

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
            @staticmethod
            def request_scene_object_pick(*_args):
                return 7

            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "completed", "object_id": 42}

        class PickingProbe(picking.SceneViewPickingMixin):
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

        monkeypatch.setattr(picking, "_has_mesh_pick_geometry", lambda object_id: object_id == 42)

        selection = SelectionService.instance()
        previous = selection.snapshot
        selection.clear(reason="mesh_pick_test", record_history=False)
        try:
            probe = PickingProbe()
            probe._handle_picking_and_selection(
                Context(), Viewport(), gizmo_consumed=False, overlay_hovered=False,
                is_scene_hovered=True, play_border_clr=None,
            )
            assert probe.picked == []
            assert probe._pending_scene_pick["selection_deferred"] is True

            probe._poll_scene_object_pick()
            assert probe.picked == [(42, False)]
        finally:
            selection.apply_snapshot(previous, record_history=False)

    def test_deferred_mesh_selection_falls_back_when_gpu_pick_fails(self):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

        class Engine:
            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "failed", "error": "readback unavailable"}

        class PickingProbe(picking.SceneViewPickingMixin):
            def __init__(self, selection):
                self._engine = Engine()
                self._pending_scene_pick = {
                    "request_id": 1,
                    "cpu_id": 42,
                    "cpu_candidates": [42],
                    "selection_deferred": True,
                    "selection_primary": selection.primary_scene_object_id(),
                    "selection_revision": selection.revision,
                    "document_id": "",
                }
                self.document_id = ""
                self.picked = []
                self._on_object_picked = lambda object_id, ctrl: self.picked.append((object_id, ctrl))

        selection = SelectionService.instance()
        previous = selection.snapshot
        selection.clear(reason="mesh_pick_failure_test", record_history=False)
        try:
            probe = PickingProbe(selection)
            probe._poll_scene_object_pick()
            assert probe.picked == [(42, False)]
        finally:
            selection.apply_snapshot(previous, record_history=False)

    def test_particle_refinement_keeps_icon_selection_but_joins_cycle(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

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

        sel = SelectionService.instance()
        previous = sel.snapshot
        sel.select_scene_object(42, owner_id="scene_view", record_history=False)
        try:
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == []
            assert probe._pick_cycle_candidates == [42, 99, 7]
            assert probe._pick_cycle_index == 0
        finally:
            sel.apply_snapshot(previous, record_history=False)

    def test_particle_refinement_corrects_mesh_behind_spray(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

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

        sel = SelectionService.instance()
        previous = sel.snapshot
        sel.select_scene_object(7, owner_id="scene_view", record_history=False)
        try:
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == [(99, False)]
            assert probe._pick_cycle_candidates == [99, 7]
            assert probe._pick_cycle_index == 0
        finally:
            sel.apply_snapshot(previous, record_history=False)

    def test_particle_without_mesh_is_an_icon_only_pick_target(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking

        monkeypatch.setattr(picking, "_has_mesh_pick_geometry", lambda object_id: object_id == 7)
        monkeypatch.setattr(picking, "_owns_particle_system", lambda object_id: object_id == 99)
        assert picking._is_icon_only_pick_target(99) is True
        assert picking._is_icon_only_pick_target(7) is False
        assert picking._is_icon_only_pick_target(0) is False

    def test_gpu_refinement_does_not_replace_particle_with_mesh_behind(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

        class Engine:
            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "completed", "object_id": 7}

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
                    "cpu_id": 99,
                    "cpu_candidates": [99],
                }
                self._pick_cycle_candidates = [99]
                self._pick_cycle_index = 0
                self._pick_cycle_last_mouse = (-1.0, -1.0)
                self._pick_cycle_last_viewport = (0, 0)
                self.picked = []
                self._on_object_picked = lambda object_id, ctrl: self.picked.append((object_id, ctrl))

        monkeypatch.setattr(picking, "_owns_particle_system", lambda object_id: object_id == 99)
        monkeypatch.setattr(picking, "_has_mesh_pick_geometry", lambda object_id: object_id == 7)
        monkeypatch.setattr(picking, "_is_icon_only_pick_target", lambda object_id: object_id == 99)
        monkeypatch.setattr(picking, "_object_ray_depth", lambda object_id, *_args: {
            99: 1.0,
            7: 3.0,
        }[object_id])
        assert picking._is_icon_only_pick_target(99)

        sel = SelectionService.instance()
        previous = sel.snapshot
        sel.select_scene_object(99, owner_id="scene_view", record_history=False)
        try:
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == []
            assert probe._pick_cycle_candidates == [99, 7]
            assert probe._pick_cycle_index == 0
        finally:
            sel.apply_snapshot(previous, record_history=False)

    def test_first_click_keeps_selected_particle_among_mesh_candidates(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

        class Engine:
            @staticmethod
            def pick_scene_object_ids(*_args):
                return [7, 99]

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
                self._pick_cycle_candidates = []
                self._pick_cycle_index = -1
                self._pick_cycle_last_mouse = (-1.0, -1.0)
                self._pick_cycle_last_viewport = (0, 0)

        monkeypatch.setattr(picking, "_owns_particle_system", lambda object_id: object_id == 99)
        monkeypatch.setattr(
            picking.SceneViewPickingMixin,
            "_current_scene_pick_id",
            staticmethod(lambda: 99),
        )

        sel = SelectionService.instance()
        previous = sel.snapshot
        sel.select_scene_object(99, owner_id="hierarchy", record_history=False)
        try:
            probe = PickingProbe()
            assert probe._current_scene_pick_id() == 99
            assert probe._pick_scene_object(Context(), Viewport()) == 99
            assert probe._pick_cycle_candidates == [7, 99]
            assert probe._pick_scene_object(Context(), Viewport()) == 7
        finally:
            sel.apply_snapshot(previous, record_history=False)

    def test_gpu_refinement_clears_mesh_aabb_false_positive(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

        class Engine:
            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "completed", "object_id": 0}

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
                self.picked = []
                self._on_object_picked = lambda object_id, ctrl: self.picked.append((object_id, ctrl))

        monkeypatch.setattr(picking, "_has_mesh_pick_geometry", lambda object_id: object_id == 7)

        selection = SelectionService.instance()
        previous = selection.snapshot
        selection.select_scene_object(7, owner_id="scene_view", record_history=False)
        try:
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == [(0, False)]
            assert probe._pick_cycle_candidates == []
            assert probe._pick_cycle_index == -1
        finally:
            selection.apply_snapshot(previous, record_history=False)

    def test_gpu_refinement_corrects_one_mesh_aabb_to_visible_mesh(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

        class Engine:
            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "completed", "object_id": 9}

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

        monkeypatch.setattr(picking, "_has_mesh_pick_geometry", lambda object_id: object_id in (7, 9))
        monkeypatch.setattr(picking, "_is_icon_only_pick_target", lambda _object_id: False)
        monkeypatch.setattr(picking, "_object_ray_depth", lambda object_id, *_args: {9: 1.0, 7: 2.0}[object_id])

        selection = SelectionService.instance()
        previous = selection.snapshot
        selection.select_scene_object(7, owner_id="scene_view", record_history=False)
        try:
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == [(9, False)]
            assert probe._pick_cycle_candidates == [9, 7]
            assert probe._pick_cycle_index == 0
        finally:
            selection.apply_snapshot(previous, record_history=False)

    def test_gpu_refinement_preserves_intentional_overlap_cycle(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

        class Engine:
            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "completed", "object_id": 7}

        class PickingProbe(picking.SceneViewPickingMixin):
            def __init__(self):
                self._engine = Engine()
                self._pending_scene_pick = {
                    "request_id": 1,
                    "x": 10.0,
                    "y": 12.0,
                    "width": 100.0,
                    "height": 80.0,
                    "cpu_id": 9,
                    "cpu_candidates": [7, 9],
                    "cpu_cycle_index": 1,
                }
                self._pick_cycle_candidates = [7, 9]
                self._pick_cycle_index = 1
                self._pick_cycle_last_mouse = (10.0, 12.0)
                self._pick_cycle_last_viewport = (100, 80)
                self.picked = []
                self._on_object_picked = lambda object_id, ctrl: self.picked.append((object_id, ctrl))

        monkeypatch.setattr(picking, "_is_icon_only_pick_target", lambda _object_id: False)

        selection = SelectionService.instance()
        previous = selection.snapshot
        selection.select_scene_object(9, owner_id="scene_view", record_history=False)
        try:
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == []
            assert probe._pick_cycle_candidates == [7, 9]
            assert probe._pick_cycle_index == 1
        finally:
            selection.apply_snapshot(previous, record_history=False)

    def test_particle_refinement_cannot_overwrite_a_newer_selection(self, monkeypatch):
        from Infernux.engine.ui import _scene_view_picking as picking
        from Infernux.engine.interaction import SelectionService

        class Engine:
            @staticmethod
            def query_scene_object_pick(_request_id):
                return {"status": "completed", "object_id": 99}

            @staticmethod
            def screen_to_world_ray(*_args):
                return (0.0, 0.0, 0.0, 0.0, 0.0, 1.0)

        selection = SelectionService.instance()
        previous = selection.snapshot
        selection.select_scene_object(7, owner_id="scene_view", record_history=False)
        request_revision = selection.revision

        class PickingProbe(picking.SceneViewPickingMixin):
            def __init__(self):
                self._engine = Engine()
                self._document_id = "scene-a"
                self._pending_scene_pick = {
                    "request_id": 1,
                    "x": 10.0,
                    "y": 12.0,
                    "width": 100.0,
                    "height": 80.0,
                    "cpu_id": 7,
                    "cpu_candidates": [7],
                    "selection_revision": request_revision,
                    "document_id": "scene-a",
                }
                self._pick_cycle_candidates = [7]
                self._pick_cycle_index = 0
                self._pick_cycle_last_mouse = (-1.0, -1.0)
                self._pick_cycle_last_viewport = (0, 0)
                self.picked = []
                self._on_object_picked = lambda object_id, ctrl: self.picked.append((object_id, ctrl))

            @property
            def document_id(self):
                return self._document_id

        monkeypatch.setattr(picking, "_owns_particle_system", lambda object_id: object_id == 99)
        monkeypatch.setattr(picking, "_is_icon_only_pick_target", lambda _object_id: False)
        try:
            selection.select_scene_object(42, owner_id="hierarchy", record_history=False)
            selection.select_scene_object(7, owner_id="scene_view", record_history=False)
            probe = PickingProbe()
            probe._poll_scene_object_pick()
            assert probe.picked == []
        finally:
            selection.apply_snapshot(previous, record_history=False)

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
    def test_panel_content_failure_is_isolated_and_deduplicated(self, monkeypatch):
        from Infernux.debug import Debug
        from Infernux.engine.ui.editor_panel import EditorPanel

        class Context:
            @staticmethod
            def end_window():
                pass

        class ProbePanel(EditorPanel):
            def __init__(self):
                super().__init__("Probe", "probe")
                self.should_fail = True

            def _begin_closable_window(self, _ctx, _flags=0):
                return True

            def on_render_content(self, _ctx):
                if self.should_fail:
                    raise AttributeError("missing widget API")

        errors = []
        monkeypatch.setattr(Debug, "log_error", errors.append)
        panel = ProbePanel()

        panel.on_render(Context())
        panel.on_render(Context())
        assert len(errors) == 1
        assert "missing widget API" in errors[0]

        panel.should_fail = False
        panel.on_render(Context())
        panel.should_fail = True
        panel.on_render(Context())
        assert len(errors) == 2

    def test_dock_presentation_survives_begin_window_false(self):
        from Infernux.engine.ui.editor_panel import EditorPanel

        class Context:
            @staticmethod
            def begin_window_closable(_title, _open, _flags):
                # ImGui may skip content submission on a throttled editor
                # frame even though this remains the selected dock tab.
                return False, True

            @staticmethod
            def is_current_window_content_presented():
                return True

            @staticmethod
            def is_mouse_button_clicked(_button):
                return False

            @staticmethod
            def is_window_focused(_flags):
                return False

            @staticmethod
            def end_window():
                pass

        panel = EditorPanel("Probe", "probe")
        panel.on_render(Context())

        assert panel.is_content_visible() is True
        assert panel._content_was_visible is True

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


def test_ui_editor_nudge_executes_before_recording_and_replays_through_history():
    from types import SimpleNamespace

    from Infernux.engine.ui.ui_editor_panel import UIEditorPanel
    from Infernux.engine.undo import UndoManager

    class ProbePanel(UIEditorPanel):
        def __init__(self, element):
            super().__init__()
            self.element = element

        @property
        def _selected_element_comp(self):
            return self.element

    previous_manager = UndoManager.instance()
    manager = UndoManager()
    panel = ProbePanel(SimpleNamespace(x=12.0, y=24.0))
    try:
        assert panel.command_nudge_selected(-1, 10)
        assert (panel.element.x, panel.element.y) == (11.0, 34.0)
        assert manager.undo_description == "Nudge UI Element"

        manager.undo()
        assert (panel.element.x, panel.element.y) == (12.0, 24.0)
        manager.redo()
        assert (panel.element.x, panel.element.y) == (11.0, 34.0)
    finally:
        UndoManager._instance = previous_manager


def test_ui_editor_selection_identity_uses_game_object_id_not_wrapper_identity():
    from types import SimpleNamespace

    from Infernux.engine.ui.ui_editor_panel import UIEditorPanel

    first_wrapper = SimpleNamespace(game_object=SimpleNamespace(id=42))
    refreshed_wrapper = SimpleNamespace(game_object=SimpleNamespace(id=42))

    assert first_wrapper is not refreshed_wrapper
    assert UIEditorPanel._element_object_id(first_wrapper) == UIEditorPanel._element_object_id(
        refreshed_wrapper
    )


def test_runtime_image_packet_refreshes_when_async_texture_becomes_ready():
    from types import SimpleNamespace

    from Infernux.ui.ui_render_dispatch import _runtime_render_image

    class Renderer:
        def __init__(self):
            self.rects = []
            self.images = []

        def add_filled_rect(self, *arguments):
            self.rects.append(arguments)

        def add_image(self, *arguments):
            self.images.append(arguments)

    renderer = Renderer()
    element = SimpleNamespace(
        texture_path="Assets/Textures/hud.png",
        color=[1.0, 1.0, 1.0, 1.0],
        opacity=1.0,
        rotation=0.0,
        mirror_x=False,
        mirror_y=False,
        corner_radius=0.0,
        _ui_render_revision=7,
    )
    texture_id = [0]

    def render():
        _runtime_render_image(
            element,
            renderer,
            0,
            10.0,
            20.0,
            100.0,
            50.0,
            1.0,
            1.0,
            lambda _path: texture_id[0],
        )

    render()
    assert len(renderer.rects) == 1
    assert renderer.images == []

    texture_id[0] = 73
    render()
    assert len(renderer.rects) == 1
    assert len(renderer.images) == 1
    assert renderer.images[0][1] == 73


def test_ui_editor_continuous_manipulation_is_core_owned_and_fail_closed():
    from types import SimpleNamespace

    from Infernux.engine.interaction import (
        ContinuousEditService,
        EditorContextSnapshot,
        EditorInteractionCore,
        FocusService,
    )
    from Infernux.engine.ui.ui_editor_panel import UIEditorPanel
    from Infernux.engine.undo import UndoManager

    class ProbePanel(UIEditorPanel):
        def __init__(self, element):
            super().__init__()
            self.element = element

        @property
        def _selected_element_comp(self):
            return self.element

    previous_manager = UndoManager.instance()
    previous_core = EditorInteractionCore._instance
    previous_edits = ContinuousEditService._instance
    previous_focus = FocusService._instance
    manager = UndoManager()
    edits = ContinuousEditService()
    focus = FocusService()
    core = SimpleNamespace(
        continuous_edits=edits,
        focus=focus,
        capture_context=lambda: EditorContextSnapshot(focus=focus.snapshot),
    )
    EditorInteractionCore._instance = core
    element = SimpleNamespace(x=12.0, y=24.0, rotation=0.0)
    panel = ProbePanel(element)
    try:
        assert panel._begin_element_manipulation("drag", element)
        assert focus.snapshot.capture_owner_id == "ui_editor.manipulation"
        assert edits.active_count == 1
        assert panel._mutate_element_manipulation(
            lambda: (setattr(element, "x", 32.0), setattr(element, "y", 44.0))
        )
        assert panel._finish_element_manipulation(commit=True)
        assert edits.active_count == 0
        assert focus.snapshot.capture_owner_id == ""
        assert manager.undo_description == "Move UI Element"
        entry = manager.action_journal.peek_undo()
        assert entry is not None
        assert entry.after_context.focus.capture_owner_id == ""

        manager.undo()
        assert (element.x, element.y) == (12.0, 24.0)
        manager.redo()
        assert (element.x, element.y) == (32.0, 44.0)

        assert panel._begin_element_manipulation("rotate", element)
        assert panel._mutate_element_manipulation(
            lambda: setattr(element, "rotation", 90.0)
        )
        assert panel._finish_element_manipulation(commit=False)
        assert element.rotation == 0.0
        assert manager.undo_description == "Move UI Element"

        manager.enabled = False
        before = (element.x, element.y)
        assert not panel._begin_element_manipulation("drag", element)
        assert not panel._apply_drag_suppressed(100.0, 200.0, 800.0, 600.0)
        assert (element.x, element.y) == before
    finally:
        UndoManager._instance = previous_manager
        EditorInteractionCore._instance = previous_core
        ContinuousEditService._instance = previous_edits
        FocusService._instance = previous_focus
