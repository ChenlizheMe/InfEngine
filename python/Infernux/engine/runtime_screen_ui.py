"""Runtime-owned Screen UI command submission.

Screen-space UI is render data, not presentation-panel data.  This service
builds the native ScreenUI command lists at the camera-submission boundary so
Game captures and hidden Game tabs consume the same current-frame HUD.
"""

from __future__ import annotations

import weakref
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
        self._reported_ready = False
        self._reported_draw_ready = False

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

        if not self._reported_draw_ready:
            draw_count = getattr(renderer, "last_submitted_draw_count", None)
            index_count = getattr(renderer, "last_submitted_index_count", None)
            if callable(draw_count) and callable(index_count):
                camera_draws = int(draw_count(ScreenUIList.Camera))
                overlay_draws = int(draw_count(ScreenUIList.Overlay))
                if camera_draws or overlay_draws:
                    from Infernux.debug import Debug

                    self._reported_draw_ready = True
                    Debug.log(
                        "INFERNUX_SCREEN_UI_DRAW_READY "
                        f"camera_draws={camera_draws} overlay_draws={overlay_draws} "
                        f"camera_indices={int(index_count(ScreenUIList.Camera))} "
                        f"overlay_indices={int(index_count(ScreenUIList.Overlay))}"
                    )

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
        if not self._reported_ready:
            has_commands = getattr(renderer, "has_commands", None)
            camera_commands = bool(
                callable(has_commands) and has_commands(ScreenUIList.Camera)
            )
            overlay_commands = bool(
                callable(has_commands) and has_commands(ScreenUIList.Overlay)
            )
            if camera_commands or overlay_commands:
                from Infernux.debug import Debug

                self._reported_ready = True
                Debug.log(
                    "INFERNUX_SCREEN_UI_SUBMISSION_READY "
                    f"canvases={len(canvases)} width={width} height={height} "
                    f"camera={int(camera_commands)} overlay={int(overlay_commands)}"
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

        if float(canvas.reference_width) < 1 or float(canvas.reference_height) < 1:
            return

        scale_x, scale_y, text_scale = canvas.compute_scale(
            float(game_width), float(game_height)
        )
        logical_width, logical_height = canvas.compute_logical_size(
            float(game_width), float(game_height)
        )

        for element in canvas._get_elements():
            element_object = getattr(element, "game_object", None)
            if element_object is not None and not element_object.active_in_hierarchy:
                continue
            if not getattr(element, "enabled", True):
                continue

            x, y, width, height = element.get_rect(logical_width, logical_height)
            _ui_dispatch(
                element,
                "runtime",
                renderer=renderer,
                ui_list=ui_list,
                sx=x * scale_x,
                sy=y * scale_y,
                sw=width * scale_x,
                sh=height * scale_y,
                ref_w=logical_width,
                ref_h=logical_height,
                scale_x=scale_x,
                scale_y=scale_y,
                text_scale=text_scale,
                get_tex_id=get_texture_id,
            )


def __getattr__(name: str):
    """Keep the desktop pipeline wrapper lazy for reduced Player bindings.

    Cooked Web Players only need the platform-neutral command submission helpers.
    Importing the native render-pipeline base while loading those helpers would
    unnecessarily require the desktop callback binding in every Player profile.
    """
    if name == "RuntimeScreenUIRenderPipeline":
        from Infernux.engine.runtime_screen_ui_pipeline import (
            RuntimeScreenUIRenderPipeline,
        )

        return RuntimeScreenUIRenderPipeline
    raise AttributeError(name)
