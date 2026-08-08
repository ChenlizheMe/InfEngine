from __future__ import annotations

from types import SimpleNamespace

from Infernux.engine.ui.game_view_panel import (
    GameViewPanel,
    _GAME_UI_BUTTON_SEMANTIC_PREFIX,
    _GAME_VIEW_FPS_SEMANTIC_ID,
    _GAME_VIEWPORT_SEMANTIC_ID,
)


class _Engine:
    def __init__(self) -> None:
        self.resizes: list[tuple[int, int]] = []

    @staticmethod
    def get_play_mode_manager():
        return None

    def resize_game_render_target(self, width: int, height: int) -> None:
        self.resizes.append((width, height))

    @staticmethod
    def get_game_texture_id() -> int:
        return 1


class _RenderActivationEngine(_Engine):
    def __init__(self) -> None:
        super().__init__()
        self.game_camera_enabled: list[bool] = []

    def set_game_camera_enabled(self, enabled: bool) -> None:
        self.game_camera_enabled.append(bool(enabled))


class _Context:
    def __init__(self, *, window_hovered: bool = True, mouse_clicked: bool = False) -> None:
        self.semantic_items: list[tuple[str, str, bool, str]] = []
        self.semantic_rects: list[tuple] = []
        self._window_hovered = window_hovered
        self._mouse_clicked = mouse_clicked
        self.invisible_button_calls: list[tuple[str, float, float]] = []

    @staticmethod
    def get_content_region_avail_width() -> float:
        return 640.0

    @staticmethod
    def get_content_region_avail_height() -> float:
        return 360.0

    @staticmethod
    def begin_child(*_args) -> bool:
        return True

    @staticmethod
    def end_child() -> None:
        pass

    @staticmethod
    def get_cursor_pos_x() -> float:
        return 0.0

    @staticmethod
    def get_cursor_pos_y() -> float:
        return 0.0

    @staticmethod
    def set_cursor_pos_x(_value: float) -> None:
        pass

    @staticmethod
    def set_cursor_pos_y(_value: float) -> None:
        pass

    @staticmethod
    def image(*_args) -> None:
        pass

    def invisible_button(self, id: str, width: float, height: float) -> bool:
        self.invisible_button_calls.append((id, width, height))
        return self._mouse_clicked

    def is_item_hovered(self) -> bool:
        return self._window_hovered

    def is_mouse_button_clicked(self, _button: int) -> bool:
        return self._mouse_clicked

    def is_window_hovered(self) -> bool:
        return self._window_hovered

    def record_semantic_item(self, kind: str, label: str, enabled: bool, semantic_id: str) -> None:
        self.semantic_items.append((kind, label, enabled, semantic_id))

    def record_semantic_rect(self, *args) -> None:
        self.semantic_rects.append(args)


def test_game_ui_button_is_exposed_as_a_play_only_semantic_target(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    class _Button:
        label = "START RACE"
        interactable = True
        raycast_target = True
        game_object = SimpleNamespace(id=42, name="StartButton")

    monkeypatch.setattr(module, "UIButton", _Button)
    panel = GameViewPanel(engine=_Engine())
    panel._is_playing = lambda: True
    ctx = _Context()

    panel._record_game_ui_button_semantic(ctx, _Button(), 60.0, 120.0, 150.0, 40.0)

    assert ctx.semantic_rects == [
        ("game_ui_button", "START RACE", 60.0, 120.0, 150.0, 40.0, True, f"{_GAME_UI_BUTTON_SEMANTIC_PREFIX}42"),
    ]


def test_game_viewport_is_exposed_as_a_semantic_click_target(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    monkeypatch.setattr(module, "_SM", SimpleNamespace(instance=lambda: SimpleNamespace(get_active_scene=lambda: None)))
    monkeypatch.setattr(module, "collect_sorted_canvases", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        module,
        "capture_viewport_info",
        lambda _ctx: SimpleNamespace(
            image_min_x=0.0,
            image_min_y=0.0,
            is_hovered=False,
            is_mouse_inside=lambda _ctx: True,
        ),
    )

    panel = GameViewPanel(engine=_Engine())
    panel._fit_mode = False
    panel._display_scale = 1.0
    panel._render_screen_ui = lambda *_args, **_kwargs: None
    route_calls: list[tuple] = []
    panel._route_game_input = lambda *_args: route_calls.append(_args)
    ctx = _Context()

    panel._render_game_viewport(ctx, 320, 180, 1.0)

    assert ctx.semantic_items == [
        ("viewport", "Game Viewport", True, _GAME_VIEWPORT_SEMANTIC_ID),
    ]
    assert ctx.invisible_button_calls == [("##GameViewportInput", 320.0, 180.0)]
    assert route_calls[0][3] is True


def test_game_viewport_does_not_steal_clicks_through_a_floating_window(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    monkeypatch.setattr(module, "_SM", SimpleNamespace(instance=lambda: SimpleNamespace(get_active_scene=lambda: None)))
    monkeypatch.setattr(module, "collect_sorted_canvases", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        module,
        "capture_viewport_info",
        lambda _ctx: SimpleNamespace(
            image_min_x=0.0,
            image_min_y=0.0,
            is_hovered=False,
            is_mouse_inside=lambda _ctx: True,
        ),
    )

    panel = GameViewPanel(engine=_Engine())
    panel._fit_mode = False
    panel._display_scale = 1.0
    panel._render_screen_ui = lambda *_args, **_kwargs: None
    route_calls: list[tuple] = []
    panel._route_game_input = lambda *_args: route_calls.append(_args)
    ctx = _Context(window_hovered=False, mouse_clicked=True)

    panel._render_game_viewport(ctx, 320, 180, 1.0)

    assert ctx.invisible_button_calls == [("##GameViewportInput", 320.0, 180.0)]
    assert route_calls[0][3] is False
    assert route_calls[0][4] is False


def test_game_viewport_activates_on_mouse_down_before_imgui_button_release(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    monkeypatch.setattr(module, "_SM", SimpleNamespace(instance=lambda: SimpleNamespace(get_active_scene=lambda: None)))
    monkeypatch.setattr(module, "collect_sorted_canvases", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        module,
        "capture_viewport_info",
        lambda _ctx: SimpleNamespace(image_min_x=0.0, image_min_y=0.0),
    )

    panel = GameViewPanel(engine=_Engine())
    panel._fit_mode = False
    panel._display_scale = 1.0
    panel._render_screen_ui = lambda *_args, **_kwargs: None
    route_calls: list[tuple] = []
    panel._route_game_input = lambda *_args: route_calls.append(_args)
    ctx = _Context(window_hovered=True, mouse_clicked=True)
    ctx.invisible_button = lambda *_args: False

    panel._render_game_viewport(ctx, 320, 180, 1.0)

    assert route_calls[0][3] is True
    assert route_calls[0][4] is True


def test_visible_fps_counter_has_stable_semantic_target(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    monkeypatch.setattr(
        module,
        "Time",
        SimpleNamespace(unscaled_delta_time=0.0, game_delta_time=0.0),
    )
    panel = GameViewPanel(engine=_Engine())

    class _FpsContext:
        semantic_capture_enabled = True

        def __init__(self):
            self.labels = []
            self.semantic_items = []

        @staticmethod
        def calc_text_size(_text):
            return (140.0, 16.0)

        @staticmethod
        def get_window_width():
            return 720.0

        @staticmethod
        def same_line(_x):
            pass

        def label(self, text):
            self.labels.append(text)

        def record_semantic_item(self, *args):
            self.semantic_items.append(args)

    ctx = _FpsContext()
    panel._render_fps_counter(ctx)

    assert ctx.labels == ["FPS: --"]
    assert ctx.semantic_items == [
        ("performance", "FPS: --", False, _GAME_VIEW_FPS_SEMANTIC_ID),
    ]


def test_hidden_game_view_keeps_rendering_during_runtime_acceptance(monkeypatch):
    from Infernux.acceptance import RuntimeAcceptance

    engine = _RenderActivationEngine()
    panel = GameViewPanel(engine=engine)
    monkeypatch.setattr(RuntimeAcceptance, "is_active", classmethod(lambda _cls: True))

    panel._on_not_visible(None)

    assert engine.game_camera_enabled == [True]
    assert panel._game_camera_was_enabled is True


def test_hidden_game_view_disables_rendering_without_runtime_acceptance(monkeypatch):
    from Infernux.acceptance import RuntimeAcceptance

    engine = _RenderActivationEngine()
    panel = GameViewPanel(engine=engine)
    panel._game_camera_was_enabled = True
    monkeypatch.setattr(RuntimeAcceptance, "is_active", classmethod(lambda _cls: False))

    panel._on_not_visible(None)

    assert engine.game_camera_enabled == [False]
    assert panel._game_camera_was_enabled is False


def test_game_view_retains_canvas_snapshot_until_scene_structure_changes(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    class _Scene:
        structure_version = 7

    scene = _Scene()
    collect_calls = []
    clear_calls = []
    canvases = [SimpleNamespace()]
    monkeypatch.setattr(
        module,
        "_SM",
        SimpleNamespace(instance=lambda: SimpleNamespace(get_active_scene=lambda: scene)),
    )
    monkeypatch.setattr(
        module,
        "collect_sorted_canvases",
        lambda current, **kwargs: collect_calls.append((current, kwargs)) or canvases,
    )
    monkeypatch.setattr(module, "clear_rect_cache", lambda token: clear_calls.append(token))

    panel = GameViewPanel(engine=_Engine())
    first_scene, first_canvases = panel._get_scene_and_canvases()
    second_scene, second_canvases = panel._get_scene_and_canvases()

    assert first_scene is second_scene is scene
    assert first_canvases == second_canvases == tuple(canvases)
    assert len(collect_calls) == 1
    assert clear_calls == [(id(scene), 7)]

    scene.structure_version = 8
    _, refreshed_canvases = panel._get_scene_and_canvases()

    assert refreshed_canvases == tuple(canvases)
    assert len(collect_calls) == 2
    assert clear_calls[-1] == (id(scene), 8)


def test_game_view_invalidates_canvas_snapshot_when_scene_is_cleared(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    class _Scene:
        structure_version = 1

    scene = _Scene()
    active_scene = [scene]
    monkeypatch.setattr(
        module,
        "_SM",
        SimpleNamespace(
            instance=lambda: SimpleNamespace(get_active_scene=lambda: active_scene[0])
        ),
    )
    monkeypatch.setattr(module, "collect_sorted_canvases", lambda *_args, **_kwargs: [object()])
    monkeypatch.setattr(module, "clear_rect_cache", lambda *_args: None)

    panel = GameViewPanel(engine=_Engine())
    panel._get_scene_and_canvases()
    active_scene[0] = None

    assert panel._get_scene_and_canvases() == (None, ())
    assert panel._cached_ui_scene is None
    assert panel._cached_ui_canvases == ()


def test_game_view_retries_empty_canvas_snapshot_for_late_registration(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    class _Scene:
        structure_version = 1

    scene = _Scene()
    late_canvas = SimpleNamespace(sort_order=4)
    discovery_results = [[], [late_canvas]]
    collect_calls = []
    monkeypatch.setattr(
        module,
        "_SM",
        SimpleNamespace(instance=lambda: SimpleNamespace(get_active_scene=lambda: scene)),
    )

    def _collect(_scene, **_kwargs):
        collect_calls.append(_scene)
        return discovery_results.pop(0) if discovery_results else [late_canvas]

    monkeypatch.setattr(module, "collect_sorted_canvases", _collect)
    monkeypatch.setattr(module, "clear_rect_cache", lambda *_args: None)

    panel = GameViewPanel(engine=_Engine())
    assert panel._get_scene_and_canvases() == (scene, ())
    assert panel._get_scene_and_canvases() == (scene, (late_canvas,))
    assert len(collect_calls) == 2


def test_game_view_reorders_retained_canvases_when_sort_order_changes(monkeypatch):
    import Infernux.engine.ui.game_view_panel as module

    class _Scene:
        structure_version = 1

    scene = _Scene()
    first = SimpleNamespace(sort_order=1)
    second = SimpleNamespace(sort_order=2)
    monkeypatch.setattr(
        module,
        "_SM",
        SimpleNamespace(instance=lambda: SimpleNamespace(get_active_scene=lambda: scene)),
    )
    monkeypatch.setattr(
        module,
        "collect_sorted_canvases",
        lambda *_args, **_kwargs: [first, second],
    )
    monkeypatch.setattr(module, "clear_rect_cache", lambda *_args: None)

    panel = GameViewPanel(engine=_Engine())
    assert panel._get_scene_and_canvases()[1] == (first, second)

    first.sort_order = 3
    second.sort_order = 0

    assert panel._get_scene_and_canvases()[1] == (second, first)


def test_game_view_resolution_and_fit_are_non_dirty_undoable_view_actions(monkeypatch):
    from Infernux.engine.interaction import ViewCommandService
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_service = ViewCommandService._instance
    manager = UndoManager()
    service = ViewCommandService()
    panel = GameViewPanel(engine=_Engine())
    persisted = []
    monkeypatch.setattr(
        panel,
        "_save_resolution_settings",
        lambda: persisted.append(panel._capture_view_state()),
    )
    try:
        initial = panel._capture_view_state()
        assert panel._set_resolution_preset(2)
        resolution_state = panel._capture_view_state()
        assert resolution_state[0] == 2
        assert len(persisted) == 1

        first_entry = manager.action_journal.applied_entries()[0]
        assert first_entry.action.description == "Change Game View Resolution"
        assert first_entry.action.marks_dirty is False

        manager.undo()
        assert panel._capture_view_state() == initial
        manager.redo()
        assert panel._capture_view_state() == resolution_state

        panel._fit_mode = False
        panel._display_scale = 1.25
        before_fit = panel._capture_view_state()
        panel._fit_scale()
        fitted = panel._capture_view_state()
        assert fitted[-1] is True
        assert len(manager.action_journal.applied_entries()) == 2
        fit_entry = manager.action_journal.applied_entries()[-1]
        assert fit_entry.action.description == "Fit Game View"
        assert fit_entry.action.marks_dirty is False

        manager.undo()
        assert panel._capture_view_state() == before_fit
        manager.redo()
        assert panel._capture_view_state() == fitted
    finally:
        service.shutdown()
        ViewCommandService._instance = previous_service
        UndoManager._instance = previous_manager


def test_game_view_scale_drag_persists_once_after_gesture(monkeypatch):
    from Infernux.engine.interaction import ViewCommandService
    from Infernux.engine.undo import UndoManager

    class _GestureState:
        def __init__(self):
            self.active = True
            self.deactivated = False

        def is_item_active(self):
            return self.active

        def is_item_deactivated_after_edit(self):
            return self.deactivated

    previous_manager = UndoManager._instance
    previous_service = ViewCommandService._instance
    manager = UndoManager()
    service = ViewCommandService()
    panel = GameViewPanel(engine=_Engine())
    gesture = _GestureState()
    persisted = []
    monkeypatch.setattr(
        panel,
        "_save_resolution_settings",
        lambda: persisted.append(panel._capture_view_state()),
    )
    try:
        initial = panel._capture_view_state()
        panel._display_scale = 0.75
        panel._fit_mode = False
        panel._track_continuous_view_edit(
            gesture,
            "display_scale",
            initial,
            changed=True,
            description="Change Game View Scale",
        )
        panel._display_scale = 1.1
        panel._track_continuous_view_edit(
            gesture,
            "display_scale",
            panel._capture_view_state(),
            changed=True,
            description="Change Game View Scale",
        )

        assert persisted == []
        assert manager.action_journal.applied_entries() == ()

        gesture.active = False
        gesture.deactivated = True
        panel._track_continuous_view_edit(
            gesture,
            "display_scale",
            panel._capture_view_state(),
            changed=False,
            description="Change Game View Scale",
        )

        final = panel._capture_view_state()
        assert len(persisted) == 1
        entries = manager.action_journal.applied_entries()
        assert len(entries) == 1
        assert entries[0].action.description == "Change Game View Scale"
        assert entries[0].action.marks_dirty is False

        manager.undo()
        assert panel._capture_view_state() == initial
        manager.redo()
        assert panel._capture_view_state() == final
    finally:
        service.shutdown()
        ViewCommandService._instance = previous_service
        UndoManager._instance = previous_manager
