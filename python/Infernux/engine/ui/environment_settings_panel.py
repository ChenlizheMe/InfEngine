"""
Environment Settings — floating per-scene environment (lighting) window.

Unity URP-style "Lighting > Environment" panel:
  - Skybox material slot (any .mat asset; empty = builtin procedural sky)
  - Inline parameters for procedural sky materials
  - Ambient light source (Skybox / Gradient / Color) and intensity

The settings live on the active C++ Scene (``scene.get_environment()`` /
``scene.set_environment()``) and are serialized into the .scene file, so
every scene keeps its own environment — like Unity's per-scene lighting.

Hosted as a non-dockable utility surface by the global panel lifecycle.
"""

from __future__ import annotations

import os
import copy
from typing import Optional

from Infernux.engine.i18n import t
from Infernux.engine.interaction import PanelInteractionDescriptor
from .editor_panel import FloatingEditorPanel
from .panel_registry import editor_panel
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


@editor_panel(
    "Environment Settings",
    type_id="environment_settings",
    title_key="environment.title",
    menu_path="",
    interaction=PanelInteractionDescriptor(),
)
class EnvironmentSettingsPanel(FloatingEditorPanel):
    """Per-scene Environment utility surface."""

    def __init__(self) -> None:
        super().__init__(
            title="Environment Settings",
            window_id="environment_settings",
            size=(560.0, 640.0),
        )
        # Material wrapper cache: guid -> Infernux.core.material.Material
        self._mat_cache_guid: Optional[str] = None
        self._mat_cache = None

    def request_close(self) -> bool:
        from Infernux.engine.interaction import ContinuousEditService

        ContinuousEditService.instance().commit_owner("environment_settings")
        return super().request_close()

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
        """Commit a partial environment update through the global journal."""
        del scene
        if not changes:
            return
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("Environment edit requires EditorInteractionCore")
        core.scene_objects.set_environment(changes)

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

    def _sky_material_controller(self, guid: str, mat):
        adb = self._asset_database()
        path = str(adb.get_path_from_guid(guid) if adb and guid else "")
        native = getattr(mat, "native", None) if mat is not None else None
        if not path or native is None:
            raise RuntimeError("Asset-backed sky edit requires a material asset")
        from Infernux.engine.interaction import (
            DocumentKind,
            ensure_editable_resource_document,
        )
        from .inspector_material import notify_material_document_restored

        return ensure_editable_resource_document(
            category="material",
            document_kind=DocumentKind.MATERIAL,
            file_path=path,
            resource=native,
            guid=guid,
            title=os.path.basename(path),
            view_id="environment_settings",
            on_restored=notify_material_document_restored,
            autosave_debounce_sec=0.35,
        )

    def _edit_sky_material(self, guid: str, mat, name: str, value) -> None:
        """Live-preview one sky property and commit one gesture transaction."""
        from Infernux.engine.interaction import ContinuousEditService
        from .inspector_material import notify_material_document_restored

        controller = self._sky_material_controller(guid, mat)
        native = mat.native
        old_document = controller.capture_document()
        prop = old_document.get("properties", {}).get(name)
        if not isinstance(prop, dict):
            raise RuntimeError(f"Sky material property is not serialized: {name}")
        next_document = copy.deepcopy(old_document)
        next_document["properties"][name]["value"] = value
        if next_document == old_document:
            return

        if isinstance(value, (list, tuple)):
            native.set_color(name, tuple(float(item) for item in value))
        else:
            native.set_float(name, float(value))
        notify_material_document_restored(native)

        key = f"environment:material:{guid}:{name}"
        edits = ContinuousEditService.instance()
        session = edits.get(key)
        if session is None:
            def _commit(item) -> None:
                if item.initial_value == item.current_value:
                    return
                if not controller.commit_applied_document(
                    item.initial_value,
                    item.current_value,
                    view_id="environment_settings",
                    edit_key=str(item.metadata["edit_key"]),
                    description="Edit Sky Material",
                ):
                    native.deserialize_document(copy.deepcopy(item.initial_value))
                    notify_material_document_restored(native)
                    raise RuntimeError("Sky material document transaction was rejected")

            def _cancel(item) -> None:
                native.deserialize_document(copy.deepcopy(item.initial_value))
                notify_material_document_restored(native)

            session = edits.begin(
                key,
                owner_id="environment_settings",
                description="Edit Sky Material",
                initial_value=old_document,
                metadata={"edit_key": f"property.{name}"},
                on_commit=_commit,
                on_cancel=_cancel,
            )
        edits.update(session.key, next_document)

    @staticmethod
    def _finish_sky_material_edit(guid: str, name: str) -> None:
        from Infernux.engine.interaction import ContinuousEditService

        ContinuousEditService.instance().commit(
            f"environment:material:{guid}:{name}"
        )

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

    def on_render_content(self, ctx) -> None:
        scene = self._active_scene()
        if scene is None:
            ctx.label(t("environment.no_scene"))
        else:
            self._render_body(ctx, scene)

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

        # Asset-backed sky material: every view shares the same Material
        # document, journal and debounced persistence owner.
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
            self._edit_sky_material(guid, mat, _SKY_TOP, tuple(new[:3]) + (1.0,))
        else:
            self._finish_sky_material_edit(guid, _SKY_TOP)

        horizon = mat.get_color(_SKY_HORIZON)
        self._field_label(ctx, t("environment.sky_horizon_color"))
        new = ctx.color_edit("##env_sky_horizon", horizon[0], horizon[1], horizon[2], 1.0)
        if tuple(new[:3]) != tuple(horizon[:3]):
            self._edit_sky_material(guid, mat, _SKY_HORIZON, tuple(new[:3]) + (1.0,))
        else:
            self._finish_sky_material_edit(guid, _SKY_HORIZON)

        ground = mat.get_color(_SKY_GROUND)
        self._field_label(ctx, t("environment.sky_ground_color"))
        new = ctx.color_edit("##env_sky_ground", ground[0], ground[1], ground[2], 1.0)
        if tuple(new[:3]) != tuple(ground[:3]):
            self._edit_sky_material(guid, mat, _SKY_GROUND, tuple(new[:3]) + (1.0,))
        else:
            self._finish_sky_material_edit(guid, _SKY_GROUND)

        exposure = mat.get_float(_SKY_EXPOSURE, 1.0)
        self._field_label(ctx, t("environment.sky_exposure"))
        new_exp = ctx.drag_float("##env_sky_exposure", float(exposure), 0.01, 0.0, 8.0)
        if new_exp != exposure:
            self._edit_sky_material(guid, mat, _SKY_EXPOSURE, float(new_exp))
        else:
            self._finish_sky_material_edit(guid, _SKY_EXPOSURE)

        self._sky_material_controller(guid, mat).flush_autosave()

    def _env_sky_color_row(self, ctx, scene, env: dict, key: str, label: str) -> None:
        value = env.get(key, (0.5, 0.5, 0.5))
        r, g, b = float(value[0]), float(value[1]), float(value[2])
        self._field_label(ctx, label)
        new = ctx.color_edit(f"##env_{key}", r, g, b, 1.0)
        if (new[0], new[1], new[2]) != (r, g, b):
            self._apply(scene, **{key: (float(new[0]), float(new[1]), float(new[2]))})

    def _render_skybox_material_field(self, ctx, scene, guid: str) -> None:
        from Infernux.core.asset_reference_types import (
            AssetReferenceCodec,
            asset_type_registry,
            resolve_asset_reference_path,
        )
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            FieldSchema,
            PropertyTransaction,
            SerializedObjectView,
            SerializedPropertyBinding,
            SerializedPropertyHandle,
        )
        from Infernux.engine.scene_manager import SceneFileManager
        from ._inspector_references import render_asset_reference_field

        descriptor = asset_type_registry.require("Material")

        def _normalize(value) -> str:
            if value in (None, ""):
                return ""
            path = resolve_asset_reference_path(descriptor.type_id, value)
            payload = AssetReferenceCodec.normalize(descriptor.type_id, value)
            new_guid = payload["guid"]
            adb = self._asset_database()
            new_guid = new_guid or (adb.get_guid_from_path(path) if adb else "")
            if not new_guid:
                raise ValueError("Skybox material must be a registered project asset")
            return str(new_guid)

        scene_files = SceneFileManager.instance()
        target_id = (
            str(scene_files.document_id)
            if scene_files is not None and scene_files.document_id
            else f"scene:{id(scene)}"
        )
        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("Environment edit requires EditorInteractionCore")
        transaction = PropertyTransaction(
            SerializedPropertyHandle(
                schema=FieldSchema(
                    "environment.skybox_material_guid",
                    descriptor.type_id,
                ),
                object_view=SerializedObjectView((target_id,)),
                bindings=(SerializedPropertyBinding(
                    target_id=target_id,
                    read=lambda: str(
                        scene.get_environment().get("skybox_material_guid", "")
                    ),
                    normalize=_normalize,
                    command_factory=lambda old, new, description: core.scene_objects.environment_command(
                        {"skybox_material_guid": old},
                        {"skybox_material_guid": new},
                        description,
                    ),
                ),),
            ),
            description="Assign Skybox Material",
            clear_value="",
        )

        adb = self._asset_database()
        sky_path = adb.get_path_from_guid(guid) if adb and guid else ""
        render_asset_reference_field(
            ctx,
            "env_skybox_material",
            self._sky_material_display(guid),
            descriptor.display_name,
            asset_type=descriptor.type_id,
            accept_drag_type=descriptor.drag_types,
            ping_path=sky_path or None,
            has_value=bool(guid),
            reference_value={
                "asset_type": descriptor.type_id,
                "guid": guid,
                "path_hint": sky_path,
            },
            transaction=transaction,
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
