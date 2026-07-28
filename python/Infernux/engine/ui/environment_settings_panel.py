"""
Environment Settings — floating per-scene environment (lighting) window.

Unity URP-style "Lighting > Environment" panel:
  - Skybox material slot (any .mat asset; empty = builtin procedural sky)
  - Inline parameters for procedural sky materials
  - Ambient light source (Skybox / Gradient / Color) and intensity

The settings live on the active C++ Scene (``scene.get_environment()`` /
``scene.set_environment()``) and are serialized into the .scene file, so
every scene keeps its own environment — like Unity's per-scene lighting.

Rendered by MenuBarPanel each frame when visible (Project menu, next to
the physics layer matrix).
"""

from __future__ import annotations

import os
from typing import Optional

from Infernux.engine.i18n import t
from .theme import Theme, ImGuiCol

_LABEL_W = 190.0

# Procedural sky material property names (Skybox Procedural)
_SKY_TOP = "skyTopColor"
_SKY_HORIZON = "skyHorizonColor"
_SKY_GROUND = "groundColor"
_SKY_EXPOSURE = "exposure"

# Ambient sources — order must match SceneEnvironmentSettings::AmbientSource
_SOURCE_KEYS = (
    "environment.source_skybox",
    "environment.source_gradient",
    "environment.source_color",
)


class EnvironmentSettingsPanel:
    """Standalone floating Environment Settings window."""

    def __init__(self) -> None:
        self._visible: bool = False
        # Material wrapper cache: guid -> Infernux.core.material.Material
        self._mat_cache_guid: Optional[str] = None
        self._mat_cache = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open(self) -> None:
        self._visible = True

    def close(self) -> None:
        self._visible = False

    @property
    def is_open(self) -> bool:
        return self._visible

    # ------------------------------------------------------------------
    # Scene helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _active_scene():
        try:
            from Infernux.lib import SceneManager
            return SceneManager.instance().get_active_scene()
        except Exception:
            return None

    @staticmethod
    def _asset_database():
        try:
            from Infernux.lib import AssetRegistry
            return AssetRegistry.instance().get_asset_database()
        except Exception:
            return None

    def _apply(self, scene, **changes) -> None:
        """Push a partial environment update to the scene and mark it dirty."""
        scene.set_environment(changes)
        try:
            from Infernux.engine.scene_manager import SceneFileManager
            sfm = SceneFileManager.instance()
            if sfm:
                sfm.mark_dirty()
        except Exception:
            pass

    def _resolve_sky_material(self, guid: str):
        """Return the material edited by the sky-parameter section.

        Asset materials come back as throttled auto-saving ``Material``
        wrappers; the builtin procedural sky is wrapped too (its wrapper
        simply never persists to disk).
        """
        if self._mat_cache is not None and self._mat_cache_guid == guid:
            return self._mat_cache
        self._mat_cache = None
        self._mat_cache_guid = guid
        try:
            from Infernux.core.material import Material
            from Infernux.lib import AssetRegistry
            if guid:
                native = AssetRegistry.instance().load_material_by_guid(guid)
            else:
                native = AssetRegistry.instance().get_builtin_material("SkyboxProcedural")
            if native is not None:
                self._mat_cache = Material(native)
        except Exception:
            self._mat_cache = None
        return self._mat_cache

    def _sky_material_display(self, guid: str) -> str:
        if not guid:
            return t("environment.builtin_sky")
        adb = self._asset_database()
        path = adb.get_path_from_guid(guid) if adb else ""
        if path:
            return os.path.splitext(os.path.basename(path))[0]
        return t("environment.missing_material")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, ctx) -> None:
        if not self._visible:
            return

        x0, y0, dw, dh = ctx.get_main_viewport_bounds()
        w, h = 560.0, 640.0
        ctx.set_next_window_pos(x0 + (dw - w) * 0.5, y0 + (dh - h) * 0.5, Theme.COND_FIRST_USE_EVER, 0.0, 0.0)
        ctx.set_next_window_size(w, h, Theme.COND_FIRST_USE_EVER)

        visible, still_open = ctx.begin_window_closable(
            t("environment.title") + "###environment_settings", self._visible, Theme.WINDOW_FLAGS_DIALOG
        )
        if not still_open:
            self._visible = False
            ctx.end_window()
            return

        if visible:
            scene = self._active_scene()
            if scene is None:
                ctx.label(t("environment.no_scene"))
            else:
                self._render_body(ctx, scene)

        ctx.end_window()

    def _render_body(self, ctx, scene) -> None:
        env = scene.get_environment()

        self._render_skybox_section(ctx, scene, env)
        ctx.spacing()
        ctx.separator()
        ctx.spacing()
        self._render_ambient_section(ctx, scene, env)

    # ── Skybox ────────────────────────────────────────────────────────

    def _render_skybox_section(self, ctx, scene, env: dict) -> None:
        ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
        ctx.label(t("environment.skybox_section"))
        ctx.pop_style_color(1)
        ctx.spacing()

        guid = str(env.get("skybox_material_guid", ""))

        self._field_label(ctx, t("environment.skybox_material"))
        self._render_skybox_material_field(ctx, scene, guid)

        if not guid:
            # Builtin procedural sky: its parameters are scene data (the
            # builtin material has no backing asset). Edit the environment
            # settings; the renderer syncs them onto the material each frame.
            ctx.spacing()
            self._env_sky_color_row(ctx, scene, env, "sky_top_color", t("environment.sky_top_color"))
            self._env_sky_color_row(ctx, scene, env, "sky_horizon_color", t("environment.sky_horizon_color"))
            self._env_sky_color_row(ctx, scene, env, "sky_ground_color", t("environment.sky_ground_color"))

            exposure = float(env.get("sky_exposure", 1.0))
            self._field_label(ctx, t("environment.sky_exposure"))
            new_exp = ctx.drag_float("##env_sky_exposure", exposure, 0.01, 0.0, 8.0)
            if new_exp != exposure:
                self._apply(scene, sky_exposure=float(new_exp))
            return

        # Asset-backed sky material: edit the material itself (persisted to
        # its .mat file via the wrapper's throttled auto-save).
        mat = self._resolve_sky_material(guid)
        native = getattr(mat, "native", None) if mat else None
        if native is None or not native.has_property(_SKY_TOP):
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.label(t("environment.custom_sky_hint"))
            ctx.pop_style_color(1)
            return

        ctx.spacing()

        top = mat.get_color(_SKY_TOP)
        self._field_label(ctx, t("environment.sky_top_color"))
        new = ctx.color_edit("##env_sky_top", top[0], top[1], top[2], 1.0)
        if tuple(new[:3]) != tuple(top[:3]):
            mat.set_color(_SKY_TOP, new[0], new[1], new[2], 1.0)

        horizon = mat.get_color(_SKY_HORIZON)
        self._field_label(ctx, t("environment.sky_horizon_color"))
        new = ctx.color_edit("##env_sky_horizon", horizon[0], horizon[1], horizon[2], 1.0)
        if tuple(new[:3]) != tuple(horizon[:3]):
            mat.set_color(_SKY_HORIZON, new[0], new[1], new[2], 1.0)

        ground = mat.get_color(_SKY_GROUND)
        self._field_label(ctx, t("environment.sky_ground_color"))
        new = ctx.color_edit("##env_sky_ground", ground[0], ground[1], ground[2], 1.0)
        if tuple(new[:3]) != tuple(ground[:3]):
            mat.set_color(_SKY_GROUND, new[0], new[1], new[2], 1.0)

        exposure = mat.get_float(_SKY_EXPOSURE, 1.0)
        self._field_label(ctx, t("environment.sky_exposure"))
        new_exp = ctx.drag_float("##env_sky_exposure", float(exposure), 0.01, 0.0, 8.0)
        if new_exp != exposure:
            mat.set_float(_SKY_EXPOSURE, float(new_exp))

    def _env_sky_color_row(self, ctx, scene, env: dict, key: str, label: str) -> None:
        value = env.get(key, (0.5, 0.5, 0.5))
        r, g, b = float(value[0]), float(value[1]), float(value[2])
        self._field_label(ctx, label)
        new = ctx.color_edit(f"##env_{key}", r, g, b, 1.0)
        if (new[0], new[1], new[2]) != (r, g, b):
            self._apply(scene, **{key: (float(new[0]), float(new[1]), float(new[2]))})

    def _render_skybox_material_field(self, ctx, scene, guid: str) -> None:
        from ._inspector_references import render_object_field, _picker_assets

        def _assign_from_path(path) -> None:
            adb = self._asset_database()
            new_guid = adb.get_guid_from_path(str(path)) if adb else ""
            if new_guid:
                self._apply(scene, skybox_material_guid=new_guid)

        def _clear() -> None:
            self._apply(scene, skybox_material_guid="")

        adb = self._asset_database()
        sky_path = adb.get_path_from_guid(guid) if adb and guid else ""
        render_object_field(
            ctx,
            "env_skybox_material",
            self._sky_material_display(guid),
            "Material",
            clickable=False,
            accept_drag_type="MATERIAL_FILE",
            on_drop_callback=_assign_from_path,
            picker_asset_items=lambda filt: _picker_assets(filt, "*.mat"),
            on_pick=_assign_from_path,
            on_clear=_clear,
            ping_path=sky_path or None,
            semantic_id="environment.skybox_material",
        )

    # ── Ambient light ─────────────────────────────────────────────────

    def _render_ambient_section(self, ctx, scene, env: dict) -> None:
        ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
        ctx.label(t("environment.ambient_section"))
        ctx.pop_style_color(1)
        ctx.spacing()

        source = int(env.get("ambient_source", 0))
        self._field_label(ctx, t("environment.ambient_source"))
        labels = [t(k) for k in _SOURCE_KEYS]
        new_source = ctx.combo("##env_ambient_source", source, labels)
        if new_source != source:
            self._apply(scene, ambient_source=int(new_source))
            source = int(new_source)

        intensity = float(env.get("ambient_intensity", 1.0))
        self._field_label(ctx, t("environment.ambient_intensity"))
        new_intensity = ctx.drag_float("##env_ambient_intensity", intensity, 0.01, 0.0, 8.0)
        if new_intensity != intensity:
            self._apply(scene, ambient_intensity=float(new_intensity))

        if source == 1:  # Gradient
            self._ambient_color_row(ctx, scene, env, "ambient_sky_color", t("environment.ambient_sky"))
            self._ambient_color_row(ctx, scene, env, "ambient_equator_color", t("environment.ambient_equator"))
            self._ambient_color_row(ctx, scene, env, "ambient_ground_color", t("environment.ambient_ground"))
        elif source == 2:  # Color
            self._ambient_color_row(ctx, scene, env, "ambient_color", t("environment.ambient_color"))
        else:
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.label(t("environment.ambient_skybox_hint"))
            ctx.pop_style_color(1)

    def _ambient_color_row(self, ctx, scene, env: dict, key: str, label: str) -> None:
        value = env.get(key, (0.5, 0.5, 0.5))
        r, g, b = float(value[0]), float(value[1]), float(value[2])
        self._field_label(ctx, label)
        new = ctx.color_edit(f"##env_{key}", r, g, b, 1.0)
        if (new[0], new[1], new[2]) != (r, g, b):
            self._apply(scene, **{key: (float(new[0]), float(new[1]), float(new[2]))})

    # ── Layout helper ─────────────────────────────────────────────────

    @staticmethod
    def _field_label(ctx, text: str) -> None:
        ctx.label(text)
        ctx.same_line(_LABEL_W)
        ctx.set_next_item_width(ctx.get_content_region_avail_width())
