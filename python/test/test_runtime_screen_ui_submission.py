from __future__ import annotations

from types import SimpleNamespace


class _Renderer:
    def __init__(self) -> None:
        self.cached_key = None
        self.begin_calls: list[tuple[int, int]] = []
        self.cached_calls: list[tuple[int, int, int]] = []

    def begin_frame(self, width: int, height: int) -> None:
        self.begin_calls.append((width, height))
        self.cached_key = None

    def begin_frame_cached(self, width: int, height: int, revision: int) -> bool:
        key = (width, height, revision)
        self.cached_calls.append(key)
        if key == self.cached_key:
            return True
        self.cached_key = key
        return False


class _Engine:
    def __init__(self, renderer) -> None:
        self.renderer = renderer

    def get_screen_ui_renderer(self):
        return self.renderer


def _install_scene_manager(monkeypatch, scene, persistent_scene=None) -> None:
    import Infernux.lib as lib

    monkeypatch.setattr(
        lib,
        "SceneManager",
        SimpleNamespace(
            instance=lambda: SimpleNamespace(
                get_active_scene=lambda: scene,
                get_runtime_persistent_scene=lambda: persistent_scene,
            )
        ),
    )


def test_runtime_submission_publishes_latest_hud_without_a_game_panel(monkeypatch):
    import Infernux.engine.runtime_screen_ui as module
    from Infernux.engine.runtime_screen_ui import RuntimeScreenUISubmission
    from Infernux.ui.enums import RenderMode

    renderer = _Renderer()
    engine = _Engine(renderer)
    engine._render_submission_frame = 1
    scene = SimpleNamespace(structure_version=4)
    element = SimpleNamespace(
        text="first",
        enabled=True,
        game_object=SimpleNamespace(active_in_hierarchy=True),
        get_rect=lambda *_args: (10.0, 5.0, 20.0, 8.0),
    )
    canvas = SimpleNamespace(
        render_mode=RenderMode.ScreenOverlay,
        reference_width=100.0,
        reference_height=50.0,
        enabled=True,
        game_object=SimpleNamespace(active_in_hierarchy=True),
        compute_scale=lambda *_args: (2.0, 2.0, 2.0),
        compute_logical_size=lambda *_args: (100.0, 50.0),
        _get_elements=lambda: (element,),
    )
    texture_cache = SimpleNamespace(
        has_pending=False,
        generation=3,
        get_bound=lambda _engine: (lambda _path: 71),
    )
    dispatches = []

    _install_scene_manager(monkeypatch, scene)
    monkeypatch.setattr(
        module,
        "collect_sorted_runtime_canvas_snapshot",
        lambda *_args, **_kwargs: [canvas],
    )
    monkeypatch.setattr(module, "clear_rect_cache", lambda *_args: None)
    monkeypatch.setattr(module, "_get_tex_cache", lambda: texture_cache)
    monkeypatch.setattr(
        module,
        "_runtime_ui_revision",
        lambda *_args: 100 if element.text == "first" else 101,
    )
    monkeypatch.setattr(
        module,
        "_ui_dispatch",
        lambda current, backend, **kwargs: dispatches.append(
            (current.text, backend, kwargs)
        ),
    )

    submission = RuntimeScreenUISubmission(engine)
    submission.set_target_size(200, 100)

    assert submission.submit() is True
    assert dispatches[0][0:2] == ("first", "runtime")
    assert dispatches[0][2]["sx"] == 20.0
    assert dispatches[0][2]["sy"] == 10.0

    assert submission.submit() is False
    assert len(dispatches) == 1

    element.text = "latest"
    engine._render_submission_frame = 2
    assert submission.submit() is True
    assert [entry[0] for entry in dispatches] == ["first", "latest"]


def test_runtime_submission_anchors_against_live_logical_canvas(monkeypatch):
    import Infernux.engine.runtime_screen_ui as module
    from Infernux.engine.runtime_screen_ui import RuntimeScreenUISubmission
    from Infernux.ui.enums import RenderMode

    renderer = _Renderer()
    engine = _Engine(renderer)
    engine._render_submission_frame = 1
    scene = SimpleNamespace(structure_version=1)
    logical_sizes = []
    element = SimpleNamespace(
        enabled=True,
        game_object=SimpleNamespace(active_in_hierarchy=True),
        get_rect=lambda width, height: logical_sizes.append((width, height))
        or (32.0, 32.0, 520.0, 52.0),
    )
    canvas = SimpleNamespace(
        render_mode=RenderMode.ScreenOverlay,
        reference_width=1920.0,
        reference_height=1080.0,
        enabled=True,
        game_object=SimpleNamespace(active_in_hierarchy=True),
        compute_scale=lambda *_args: (1.5, 1.5, 1.5),
        compute_logical_size=lambda *_args: (3200.0 / 1.5, 1440.0 / 1.5),
        _get_elements=lambda: (element,),
    )
    texture_cache = SimpleNamespace(
        has_pending=False,
        generation=1,
        get_bound=lambda _engine: (lambda _path: 0),
    )
    dispatches = []
    _install_scene_manager(monkeypatch, scene)
    monkeypatch.setattr(
        module,
        "collect_sorted_runtime_canvas_snapshot",
        lambda *_args, **_kwargs: [canvas],
    )
    monkeypatch.setattr(module, "clear_rect_cache", lambda *_args: None)
    monkeypatch.setattr(module, "_get_tex_cache", lambda: texture_cache)
    monkeypatch.setattr(module, "_runtime_ui_revision", lambda *_args: 1)
    monkeypatch.setattr(
        module,
        "_ui_dispatch",
        lambda _element, _backend, **kwargs: dispatches.append(kwargs),
    )

    submission = RuntimeScreenUISubmission(engine)
    submission.set_target_size(3200, 1440)
    assert submission.submit() is True
    assert logical_sizes == [(3200.0 / 1.5, 1440.0 / 1.5)]
    assert dispatches[0]["sx"] == 48.0
    assert dispatches[0]["sy"] == 48.0
    assert dispatches[0]["ref_w"] == 3200.0 / 1.5
    assert dispatches[0]["ref_h"] == 1440.0 / 1.5


def test_runtime_submission_clears_stale_commands_without_an_active_scene(monkeypatch):
    from Infernux.engine.runtime_screen_ui import RuntimeScreenUISubmission

    renderer = _Renderer()
    engine = _Engine(renderer)
    _install_scene_manager(monkeypatch, None)
    submission = RuntimeScreenUISubmission(engine)

    assert submission.submit() is True
    assert renderer.cached_calls[0][0:2] == (1920, 1080)
    assert submission.submit() is False


def test_runtime_barrier_remains_lifecycle_only():
    from Infernux.engine.engine import Engine
    from Infernux.engine.runtime_change_journal import RuntimeFrameBarrier

    calls = []
    engine = Engine.__new__(Engine)
    engine._runtime_scheduler = SimpleNamespace(
        consume_native_barrier=lambda barrier: calls.append(("barrier", barrier)) or "changes"
    )
    result = engine._consume_runtime_frame_barrier(
        RuntimeFrameBarrier.RENDER_EXTRACTION
    )

    assert result == "changes"
    assert calls == [("barrier", RuntimeFrameBarrier.RENDER_EXTRACTION)]


def test_snapshot_barrier_publishes_native_transform_revision_changes():
    from Infernux.engine.engine import Engine
    from Infernux.engine.runtime_change_journal import (
        RuntimeChangeDomain,
        RuntimeFrameBarrier,
    )

    calls = []
    published = []
    serial = [41]
    engine = Engine.__new__(Engine)
    engine._last_native_transform_serial = 41
    engine._runtime_scene_manager = SimpleNamespace(
        get_global_transform_serial=lambda: serial[0]
    )
    engine._runtime_scheduler = SimpleNamespace(
        change_journal=SimpleNamespace(
            publish=lambda domain, **kwargs: published.append((domain, kwargs))
        ),
        consume_native_barrier=lambda barrier: calls.append(barrier) or "changes",
    )

    assert engine._consume_runtime_frame_barrier(
        RuntimeFrameBarrier.SNAPSHOT_PUBLICATION
    ) == "changes"
    assert published == []

    serial[0] = 42
    engine._consume_runtime_frame_barrier(RuntimeFrameBarrier.SNAPSHOT_PUBLICATION)

    assert published == [
        (RuntimeChangeDomain.TRANSFORM_LOCAL, {"broad": True}),
        (RuntimeChangeDomain.TRANSFORM_WORLD, {"broad": True}),
    ]
    assert calls == [
        RuntimeFrameBarrier.SNAPSHOT_PUBLICATION,
        RuntimeFrameBarrier.SNAPSHOT_PUBLICATION,
    ]


def test_render_pipeline_submits_ui_before_delegating_camera_render(monkeypatch):
    import Infernux.engine.runtime_screen_ui as module

    calls = []
    submission = SimpleNamespace(submit=lambda: calls.append("screen_ui"))
    delegate = SimpleNamespace(
        render=lambda context, camera: calls.append(("render", context, camera)),
        dispose=lambda: calls.append("dispose"),
    )
    pipeline = module.RuntimeScreenUIRenderPipeline(submission, delegate)

    pipeline.render("context", "camera")
    pipeline.dispose()

    assert calls == [
        "screen_ui",
        ("render", "context", "camera"),
        "dispose",
    ]


def test_engine_wraps_custom_python_pipelines_with_runtime_ui_submission():
    from Infernux.engine.engine import Engine
    from Infernux.engine.runtime_screen_ui import RuntimeScreenUIRenderPipeline

    installed = []
    engine = Engine.__new__(Engine)
    engine._engine = SimpleNamespace(
        set_render_pipeline=lambda pipeline: installed.append(pipeline)
    )
    engine._screen_ui_submission = SimpleNamespace(submit=lambda: None)
    engine._render_pipeline = None
    delegate = SimpleNamespace(render=lambda *_args: None, dispose=lambda: None)

    engine.set_render_pipeline(delegate)

    assert isinstance(engine._render_pipeline, RuntimeScreenUIRenderPipeline)
    assert installed == [engine._render_pipeline]
    assert engine._render_pipeline._delegate is delegate


def test_player_gui_does_not_own_gpu_screen_ui_submission():
    from Infernux.engine.player_gui import PlayerGUI

    assert not hasattr(PlayerGUI, "_render_screen_ui")
