"""Runtime-owned Screen UI command submission.

Screen-space UI is render data, not presentation-panel data.  This service
builds the native ScreenUI command lists at the camera-submission boundary so
Game captures and hidden Game tabs consume the same current-frame HUD.
"""

from __future__ import annotations

import weakref

from Infernux.debug import Debug
from Infernux.lib import RenderPipelineCallback
from Infernux.engine.ui.runtime_canvas_snapshot import (
    collect_sorted_runtime_canvas_snapshot,
)
from Infernux.ui.inx_ui_screen_component import clear_rect_cache
from Infernux.ui.ui_render_dispatch import (
    dispatch as _ui_dispatch,
    runtime_ui_revision as _runtime_ui_revision,
)
from Infernux.ui.ui_texture_cache import get_shared_cache as _get_tex_cache


class RuntimeScreenUISubmission:
    """Own the GPU Screen UI command snapshot for the active game target."""

    DEFAULT_CAPTURE_WIDTH = 1920
    DEFAULT_CAPTURE_HEIGHT = 1080

    def __init__(self, engine) -> None:
        self._engine_ref = weakref.ref(engine)
        self._target_width = self.DEFAULT_CAPTURE_WIDTH
        self._target_height = self.DEFAULT_CAPTURE_HEIGHT
        self._scene = None
        self._scene_structure_version = -1
        self._last_submission_frame = -1

    @property
    def target_size(self) -> tuple[int, int]:
        return self._target_width, self._target_height

    def set_target_size(self, width: int, height: int) -> None:
        width = int(width)
        height = int(height)
        if width < 1 or height < 1:
            return
        self._target_width = width
        self._target_height = height

    def submit(self) -> bool:
        """Publish current-frame UI commands before camera RenderGraph submission.

        Returns ``True`` when commands were rebuilt and ``False`` when the
        native renderer retained an identical cached command snapshot.
        """
        from Infernux.lib import SceneManager, ScreenUIList
        from Infernux.ui.enums import RenderMode

        engine = self._engine_ref()
        if engine is None:
            return False
        frame_token = int(getattr(engine, "_render_submission_frame", -1))
        if frame_token >= 0 and frame_token == self._last_submission_frame:
            return False
        renderer = engine.get_screen_ui_renderer()
        if renderer is None:
            return False

        width, height = self.target_size
        scene_manager = SceneManager.instance()
        scene = scene_manager.get_active_scene()
        persistent_scene = scene_manager.get_runtime_persistent_scene()
        if scene is None:
            self._scene = None
            self._scene_structure_version = -1
            canvases = ()
        else:
            scene_identity = (scene, persistent_scene)
            structure_version = (
                int(getattr(scene, "structure_version", 0)),
                int(getattr(persistent_scene, "structure_version", 0)),
            )
            if scene_identity != self._scene or structure_version != self._scene_structure_version:
                clear_rect_cache((id(scene), id(persistent_scene), structure_version))
                self._scene = scene_identity
                self._scene_structure_version = structure_version
            canvases = tuple(
                collect_sorted_runtime_canvas_snapshot(
                    scene,
                    persistent_scene,
                )
            )

        texture_cache = _get_tex_cache()
        if texture_cache.has_pending:
            renderer.begin_frame(width, height)
        else:
            revision = _runtime_ui_revision(
                scene,
                canvases,
                width,
                height,
                texture_cache.generation,
            )
            if renderer.begin_frame_cached(width, height, revision):
                self._last_submission_frame = frame_token
                return False

        if not canvases:
            self._last_submission_frame = frame_token
            return True

        get_texture_id = texture_cache.get_bound(engine)
        for canvas in canvases:
            self._submit_canvas(
                canvas,
                renderer,
                get_texture_id,
                width,
                height,
                ScreenUIList,
                RenderMode,
            )
        self._last_submission_frame = frame_token
        return True

    @staticmethod
    def _submit_canvas(
        canvas,
        renderer,
        get_texture_id,
        game_width: int,
        game_height: int,
        screen_ui_list,
        render_mode,
    ) -> None:
        canvas_object = getattr(canvas, "game_object", None)
        if canvas_object is not None and not canvas_object.active_in_hierarchy:
            return
        if not getattr(canvas, "enabled", True):
            return

        if canvas.render_mode == render_mode.CameraOverlay:
            ui_list = screen_ui_list.Camera
        elif canvas.render_mode == render_mode.ScreenOverlay:
            ui_list = screen_ui_list.Overlay
        else:
            return

        reference_width = float(canvas.reference_width)
        reference_height = float(canvas.reference_height)
        if reference_width < 1 or reference_height < 1:
            return

        scale_x, scale_y, text_scale = canvas.compute_scale(
            float(game_width), float(game_height)
        )
        offset_x = (float(game_width) - reference_width * scale_x) * 0.5
        offset_y = (float(game_height) - reference_height * scale_y) * 0.5

        for element in canvas._get_elements():
            element_object = getattr(element, "game_object", None)
            if element_object is not None and not element_object.active_in_hierarchy:
                continue
            if not getattr(element, "enabled", True):
                continue

            x, y, width, height = element.get_rect(reference_width, reference_height)
            _ui_dispatch(
                element,
                "runtime",
                renderer=renderer,
                ui_list=ui_list,
                sx=offset_x + x * scale_x,
                sy=offset_y + y * scale_y,
                sw=width * scale_x,
                sh=height * scale_y,
                ref_w=reference_width,
                ref_h=reference_height,
                scale_x=scale_x,
                scale_y=scale_y,
                text_scale=text_scale,
                get_tex_id=get_texture_id,
            )


class RuntimeScreenUIRenderPipeline(RenderPipelineCallback):
    """Submit Screen UI before delegating each engine-owned camera render."""

    def __init__(self, submission: RuntimeScreenUISubmission, delegate) -> None:
        super().__init__()
        self._submission = submission
        self._delegate = delegate

    def render(self, context, camera) -> None:
        try:
            self._submission.submit()
        except Exception as exc:
            Debug.log_suppressed("RenderPipeline.ScreenUI", exc)
        self._delegate.render(context, camera)

    def dispose(self) -> None:
        dispose = getattr(self._delegate, "dispose", None)
        if callable(dispose):
            dispose()
