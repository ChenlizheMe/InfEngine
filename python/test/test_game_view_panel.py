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
