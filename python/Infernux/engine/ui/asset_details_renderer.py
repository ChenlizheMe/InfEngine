"""
Unified Asset Inspector — data-driven inspector for all asset types.

One loader, one state machine, one renderer entry point.  Categories register
via ``AssetCategoryDef``, each specifying how to load data, which editable
fields to expose, and optional custom sections (preview, shader editing, etc.).

Read-only assets (texture, audio, shader) share an Apply / Revert bar.
Read-write assets (material) use automatic debounced save.

Public API::

    render_asset_inspector(ctx, panel, file_path, category)
    invalidate()
"""

from __future__ import annotations

import copy
import json
import os
import uuid
import wave
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import t
from Infernux.core.asset_types import (
    AudioCompressionFormat,
    FilterMode,
    SpriteFrame,
    TextureCompression,
    TextureCompressionQuality,
    TextureFormat,
    TextureType,
    TextureImportSettings,
    WrapMode,
    ShaderAssetInfo,
    FontAssetInfo,
    read_meta_file,
    read_texture_import_settings,
    read_audio_import_settings,
    read_mesh_import_settings,
)
from .inspector_utils import max_label_w, field_label, render_apply_revert
from .theme import Theme, ImGuiCol
from .asset_execution_layer import AssetAccessMode, get_asset_execution_layer
from .asset_resource_preview import render_resource_preview_rect
from .imgui_keys import KEY_LEFT_CTRL, KEY_RIGHT_CTRL
from Infernux.engine.texture_task_bridge import texture_stamp, query_or_schedule_texture
from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.debug import Debug


_U64 = 0xFFFFFFFFFFFFFFFF


# ═══════════════════════════════════════════════════════════════════════════
# Field descriptor
# ═══════════════════════════════════════════════════════════════════════════


class WidgetType(Enum):
    """Widget type for an editable import-settings field."""
    CHECKBOX = "checkbox"
    COMBO = "combo"
    FLOAT = "float"


@dataclass
class FieldDef:
    """Describes one editable field on an import-settings object.

    * *key* — attribute name on the settings dataclass.
    * *label* — display text in the Inspector.
    * *field_type* — which ImGui widget to render.
    * *combo_entries* — ``[(display_label, value), ...]`` for COMBO fields.
    * *float_speed* — drag speed for FLOAT fields (default 0.001).
    * *float_range* — ``(min, max)`` clamp for FLOAT fields (default None = unclamped).
    """
    key: str
    label: str
    field_type: WidgetType
    combo_entries: List[Tuple[str, Any]] = field(default_factory=list)
    float_speed: float = 0.001
    float_range: Optional[Tuple[float, float]] = None


# ═══════════════════════════════════════════════════════════════════════════
# Category definition
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class AssetCategoryDef:
    """Registration for one asset category.

    * *load_fn* returns ``(settings_obj, extra_dict)`` or ``None`` on failure.
      For read-only assets the settings object must implement ``.copy()``
      and ``__eq__`` for dirty tracking.
    * *refresh_fn* is called every frame when the asset is already loaded
      (e.g. material re-serializes native data).
    * *custom_header_fn(ctx, panel, state)* renders after the standard header
      (e.g. texture preview).
    * *custom_body_fn(ctx, panel, state)* replaces the auto-generated
      import-settings field list (e.g. material properties, shader path editing).
    """
    display_name: str
    access_mode: AssetAccessMode
    load_fn: Callable[[str], Optional[Tuple[Any, dict]]]
    refresh_fn: Optional[Callable] = None
    editable_fields: List[FieldDef] = field(default_factory=list)
    extra_meta_keys: List[str] = field(default_factory=list)
    custom_header_fn: Optional[Callable] = None
    custom_body_fn: Optional[Callable] = None
    autosave_debounce: float = 0.35
    show_header: bool = True


# ═══════════════════════════════════════════════════════════════════════════
# Virtual assets (model ::submat / ::subanim) — map to real disk path for .meta
# ═══════════════════════════════════════════════════════════════════════════


def _meta_host_path_for_virtual_asset(file_path: str) -> str:
    if not file_path:
        return file_path
    for tok in ("::submat:", "::subanim:", "::subbone:"):
        pos = file_path.find(tok)
        if pos != -1:
            base = file_path[:pos]
            from Infernux.core.animation_clip3d import resolve_model_disk_path_from_virtual_base
            resolved = resolve_model_disk_path_from_virtual_base(base)
            if resolved:
                return resolved
            return base
    return file_path


# ═══════════════════════════════════════════════════════════════════════════
# Unified state
# ═══════════════════════════════════════════════════════════════════════════


class _ImportSettingsController:
    """Own one GUID/path-stable import-settings draft document."""

    def __init__(self, category: str, file_path: str, settings: Any) -> None:
        self.category = str(category)
        self.file_path = str(file_path)
        self.settings = copy.deepcopy(settings)
        self.disk_settings = copy.deepcopy(settings)
        self.exec_layer = None
        self.document_id = ""
        self.state: Optional[_State] = None

    def bind(self, *, file_path: str, exec_layer: Any, state: "_State") -> None:
        self.file_path = str(file_path)
        self.exec_layer = exec_layer
        self.state = state
        if self.exec_layer is not None:
            self.exec_layer.refresh_binding(self.category, self.file_path)

    def refresh_clean_source(self, settings: Any) -> None:
        """Accept an external reload only while no local draft is pending."""
        from Infernux.engine.interaction import DocumentRegistry

        document = DocumentRegistry.instance().get(self.document_id)
        if document is not None and document.is_dirty:
            return
        if settings == self.settings and settings == self.disk_settings:
            return
        self.settings = copy.deepcopy(settings)
        self.disk_settings = copy.deepcopy(settings)

    def restore_draft(self, settings: Any) -> None:
        """Restore only the draft model; the document service owns revisions."""
        self.settings = copy.deepcopy(settings)
        if self.state is not None and self.state.import_controller is self:
            self.state.settings = self.settings
            self.state.disk_settings = self.disk_settings

    def apply_mutation(
        self,
        mutator: Callable[[Any], None],
        *,
        view_id: str,
        edit_key: str,
        description: str,
        selection_after: Optional[Callable[[Any, Any, Any], Any]] = None,
    ) -> bool:
        """Commit one import draft mutation through its document authority."""
        from Infernux.engine.interaction import AuthoringMutationService
        from Infernux.engine.undo import ImportSettingsDraftCommand

        old_settings = copy.deepcopy(self.settings)
        new_settings = copy.deepcopy(self.settings)
        mutator(new_settings)
        if new_settings == old_settings:
            return False
        before_selection = None
        after_selection = None
        if selection_after is not None:
            if not callable(selection_after):
                raise TypeError("import-settings selection projection must be callable")
            from Infernux.engine.interaction import SelectionService

            before_selection = SelectionService.instance().snapshot
            after_selection = selection_after(
                old_settings,
                new_settings,
                before_selection,
            )
        return AuthoringMutationService.require().execute_command(
            self.document_id,
            lambda _before_revision, _after_revision: ImportSettingsDraftCommand(
                self,
                old_settings,
                new_settings,
                edit_key=edit_key,
                description=description,
            ),
            view_id=view_id,
            before_selection=before_selection,
            after_selection=after_selection,
        )

    @staticmethod
    def _content_document(settings: Any) -> dict:
        serializer = getattr(settings, "to_dict", None)
        if callable(serializer):
            document = serializer()
        elif isinstance(settings, dict):
            document = settings
        else:
            raise TypeError("import settings must expose to_dict()")
        if not isinstance(document, dict):
            raise TypeError("import settings serialization must return an object")
        return copy.deepcopy(document)

    def save(self, *, ticket, save_as: bool = False):
        del save_as
        if self.exec_layer is None:
            return False
        from Infernux.engine.interaction import (
            DocumentRegistry,
            document_content_token,
        )

        registry = DocumentRegistry.instance()
        content = self._content_document(self.settings)
        registry.capture_save_revision(
            ticket.ticket_id,
            content_token=document_content_token(content),
        )
        if self.category == "texture":
            from Infernux.engine.interaction import (
                DocumentActionResult,
                DocumentActionStatus,
            )
            from Infernux.engine.ui.asset_import_progress import (
                AssetImportProgressService,
            )

            settings_snapshot = copy.deepcopy(self.settings)
            content_token = document_content_token(content)

            def _work() -> bool:
                if not self.exec_layer.apply_import_settings(settings_snapshot):
                    return False
                from Infernux.core.assets import AssetManager

                AssetManager.flush_pending_gpu_texture_reloads(
                    paths=[self.file_path],
                    max_items=None,
                )
                return True

            def _is_published() -> bool:
                from Infernux.engine.ui.asset_resource_preview import (
                    ensure_imported_texture_preview,
                )

                return ensure_imported_texture_preview(self.file_path)

            def _complete(success: bool, message: str) -> None:
                if success:
                    self.disk_settings = copy.deepcopy(settings_snapshot)
                    if self.state is not None and self.state.import_controller is self:
                        self.state.disk_settings = self.disk_settings
                registry.complete_save(
                    ticket.ticket_id,
                    success=success,
                    message=message,
                    content_token=content_token if success else None,
                )

            started = AssetImportProgressService.instance().begin(
                title=t("asset.import_progress.texture_title"),
                path=self.file_path,
                work=_work,
                is_published=_is_published,
                complete=_complete,
            )
            if not started:
                registry.complete_save(
                    ticket.ticket_id,
                    success=False,
                    message="another asset import is already in progress",
                )
                return False
            return DocumentActionResult(DocumentActionStatus.PENDING)

        if not self.exec_layer.apply_import_settings(self.settings):
            return False
        self.disk_settings = copy.deepcopy(self.settings)
        if self.state is not None and self.state.import_controller is self:
            self.state.disk_settings = self.disk_settings
        registry.complete_save(
            ticket.ticket_id,
            success=True,
            content_token=document_content_token(content),
        )
        return True

    def discard(self, *, document_id: str):
        from Infernux.engine.interaction import DocumentRegistry

        if str(document_id) != self.document_id:
            return False
        self.settings = copy.deepcopy(self.disk_settings)
        if self.state is not None and self.state.import_controller is self:
            self.state.settings = self.settings
            self.state.disk_settings = self.disk_settings
        return True

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ) -> None:
        del source_path, guid
        if str(document_id) != self.document_id:
            return
        self.file_path = str(destination_path)
        if self.exec_layer is not None:
            self.exec_layer.refresh_binding(self.category, self.file_path)


class _State:
    """Per-asset inspector state (only one asset is inspected at a time)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.file_path: str = ""
        self.category: str = ""
        self.meta: Optional[dict] = None
        self.settings: Any = None
        self.disk_settings: Any = None   # snapshot for dirty check (read-only)
        self.document_id: str = ""
        self.import_controller: Optional[_ImportSettingsController] = None
        self.resource_controller = None
        self.exec_layer = None
        self.extra: dict = {}

    def load(self, file_path: str, category: str,
             cat_def: AssetCategoryDef) -> bool:
        # Already loaded — just refresh.
        if (self.file_path == file_path
                and self.category == category
                and self.settings is not None):
            if cat_def.refresh_fn:
                cat_def.refresh_fn(self)
            return True
        # Fresh load. Return live preview ownership before replacing state.
        if self.resource_controller is not None:
            self.resource_controller.flush_autosave(force=True)
        if self.file_path and self.category in {"texture", "material"}:
            from .asset_resource_preview import (
                invalidate_live_material_preview,
                invalidate_live_texture_preview,
            )
            if self.category == "texture":
                invalidate_live_texture_preview(self.file_path)
            else:
                from Infernux.core.assets import AssetManager

                if not AssetManager.has_pending_local_revision(self.file_path):
                    invalidate_live_material_preview(self.file_path)
        self.reset()
        self.file_path = file_path
        self.category = category
        self.meta = read_meta_file(_meta_host_path_for_virtual_asset(file_path))
        result = cat_def.load_fn(file_path)
        if result is None:
            return False
        settings, extra = result
        if settings is None:
            return False
        self.settings = settings
        self.extra = extra
        if (cat_def.access_mode == AssetAccessMode.READ_ONLY_RESOURCE
                and hasattr(settings, "copy")):
            self.disk_settings = settings.copy()
        return True

    def is_dirty(self) -> bool:
        if self.document_id:
            from Infernux.engine.interaction import DocumentRegistry

            document = DocumentRegistry.instance().get(self.document_id)
            if document is not None:
                return document.is_dirty
        if self.disk_settings is None:
            return False
        return self.settings != self.disk_settings


_state = _State()


def _bind_import_settings_document(
    state: _State,
    cat_def: AssetCategoryDef,
) -> None:
    """Project one editable source asset into DocumentRegistry."""
    from Infernux.engine.interaction import (
        DocumentCapability,
        DocumentKey,
        DocumentKind,
        DocumentRegistry,
    )

    registry = DocumentRegistry.instance()
    if (
        cat_def.access_mode is not AssetAccessMode.READ_ONLY_RESOURCE
        or not cat_def.editable_fields
        or state.settings is None
    ):
        registry.detach_view("inspector")
        state.document_id = ""
        state.import_controller = None
        return

    guid = str((state.meta or {}).get("guid", "") or "").strip()
    key = (
        DocumentKey.asset(DocumentKind.IMPORT_SETTINGS, guid)
        if guid
        else DocumentKey.resource(DocumentKind.IMPORT_SETTINGS, state.file_path)
    )
    title = f"{os.path.basename(state.file_path)} Import Settings"
    existing = registry.get_by_key(key)
    if existing is not None:
        controller = existing.controller
        if not isinstance(controller, _ImportSettingsController):
            raise RuntimeError("import-settings document has an incompatible controller")
        controller.refresh_clean_source(state.settings)
        registry.update_metadata(
            existing.document_id,
            title=title,
            resource_path=state.file_path,
            capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
            controller=controller,
        )
        document = existing
    else:
        controller = _ImportSettingsController(
            state.category,
            state.file_path,
            state.settings,
        )
        document = registry.create(
            DocumentKind.IMPORT_SETTINGS,
            title,
            key=key,
            resource_path=state.file_path,
            capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
            controller=controller,
        )
        controller.document_id = document.document_id

    controller.bind(file_path=state.file_path, exec_layer=state.exec_layer, state=state)
    state.document_id = document.document_id
    state.import_controller = controller
    state.settings = controller.settings
    state.disk_settings = controller.disk_settings
    registry.attach_view(document.document_id, "inspector")


def _bind_editable_resource_document(
    state: _State,
    cat_def: AssetCategoryDef,
) -> None:
    """Bind supported directly editable assets to one stable document."""
    from Infernux.engine.interaction import (
        DocumentKind,
        ensure_editable_resource_document,
    )

    kind_by_category = {
        "material": DocumentKind.MATERIAL,
        "physic_material": DocumentKind.PHYSIC_MATERIAL,
        "render_effect": DocumentKind.RENDER_EFFECT,
        "animclip": DocumentKind.ANIMATION_CLIP,
        "animclip3d": DocumentKind.ANIMATION_CLIP,
    }
    document_kind = kind_by_category.get(state.category)
    if (
        cat_def.access_mode is AssetAccessMode.READ_WRITE_RESOURCE
        and document_kind is None
    ):
        raise RuntimeError(
            f"read-write asset category '{state.category}' has no document contract"
        )
    if (
        cat_def.access_mode is not AssetAccessMode.READ_WRITE_RESOURCE
        or state.settings is None
        or (
            state.category == "animclip3d"
            and bool(state.extra.get("embedded_model_take"))
        )
        or not callable(getattr(state.settings, "serialize_document", None))
        or not callable(getattr(state.settings, "deserialize_document", None))
    ):
        return

    resource = state.settings
    controller_state = state
    on_restored = None
    if state.category == "material":
        resource = state.extra.get("native_mat")
        controller_state = None
        from .inspector_material import notify_material_document_restored

        on_restored = notify_material_document_restored

    controller = ensure_editable_resource_document(
        category=state.category,
        document_kind=document_kind,
        file_path=state.file_path,
        resource=resource,
        guid=str((state.meta or {}).get("guid", "") or ""),
        title=os.path.basename(state.file_path),
        view_id="inspector",
        exec_layer=state.exec_layer,
        state=controller_state,
        autosave_debounce_sec=cat_def.autosave_debounce,
        on_restored=on_restored,
    )
    state.document_id = controller.document_id
    state.resource_controller = controller
    if state.category != "material":
        state.settings = controller.resource


def _apply_editable_resource_document(
    state: _State,
    document: dict,
    *,
    edit_key: str,
    description: str,
) -> bool:
    controller = state.resource_controller
    if controller is None or not state.document_id:
        raise RuntimeError(
            f"{state.category} edit requires an editable resource document"
        )
    return bool(
        controller.apply_document(
            document,
            view_id="inspector",
            edit_key=edit_key,
            description=description,
        )
    )


def _bind_asset_document(state: _State, cat_def: AssetCategoryDef) -> None:
    _bind_import_settings_document(state, cat_def)
    if not state.document_id:
        _bind_editable_resource_document(state, cat_def)


# ═══════════════════════════════════════════════════════════════════════════
# Category registry
# ═══════════════════════════════════════════════════════════════════════════

_categories: Dict[str, AssetCategoryDef] = {}
_initialized = False


def _ensure_categories():
    global _initialized
    if _initialized:
        return
    _initialized = True

    # ── Texture ────────────────────────────────────────────────────────
    _categories["texture"] = AssetCategoryDef(
        display_name="asset.display_texture",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_texture,
        editable_fields=[
            FieldDef("texture_type", "asset.texture_type", WidgetType.COMBO,
                     [("asset.tex_default", TextureType.DEFAULT),
                      ("asset.tex_normalmap", TextureType.NORMAL_MAP),
                      ("asset.tex_ui", TextureType.UI),
                      ("asset.tex_sprite", TextureType.SPRITE),
                      ("asset.tex_data", TextureType.DATA)]),
            FieldDef("srgb", "asset.srgb", WidgetType.CHECKBOX),
            FieldDef("filter_mode", "asset.filter_mode", WidgetType.COMBO,
                     [("asset.filter_point", FilterMode.POINT),
                      ("asset.filter_bilinear", FilterMode.BILINEAR),
                      ("asset.filter_trilinear", FilterMode.TRILINEAR)]),
            FieldDef("wrap_mode", "asset.wrap_mode", WidgetType.COMBO,
                     [("asset.wrap_repeat", WrapMode.REPEAT),
                      ("asset.wrap_clamp", WrapMode.CLAMP),
                      ("asset.wrap_mirror", WrapMode.MIRROR)]),
            FieldDef("generate_mipmaps", "asset.generate_mipmaps", WidgetType.CHECKBOX),
            FieldDef("aniso_level", "asset.aniso_level", WidgetType.COMBO,
                     [("asset.aniso_device_max", -1), ("asset.aniso_off", 0)]
                     + [(str(level), level) for level in (2, 4, 8, 16)]),
            FieldDef("format", "asset.texture_format", WidgetType.COMBO,
                     [("asset.format_auto", TextureFormat.AUTO),
                      ("RGBA8 (32-bit)", TextureFormat.RGBA8),
                      ("RGBA4444 (16-bit)", TextureFormat.RGBA4444),
                      ("RGBA16 UNorm (64-bit)", TextureFormat.RGBA16_UNORM),
                      ("RGBA16 Float (64-bit)", TextureFormat.RGBA16_FLOAT),
                      ("RGBA32 Float (128-bit)", TextureFormat.RGBA32_FLOAT)]),
            FieldDef("compression", "asset.texture_compression", WidgetType.COMBO,
                     [("asset.compression_auto", TextureCompression.AUTO),
                      ("asset.compression_none", TextureCompression.NONE),
                      ("BC1", TextureCompression.BC1),
                      ("BC3", TextureCompression.BC3),
                      ("BC4", TextureCompression.BC4),
                      ("BC5", TextureCompression.BC5)]),
            FieldDef("compression_quality", "asset.texture_compression_quality", WidgetType.COMBO,
                     [("asset.quality_fast", TextureCompressionQuality.FAST),
                      ("asset.quality_normal", TextureCompressionQuality.NORMAL),
                      ("asset.quality_high", TextureCompressionQuality.HIGH)]),
            FieldDef("max_size", "asset.max_size", WidgetType.COMBO,
                     [(str(s), s) for s in
                      (32, 64, 128, 256, 512, 1024, 2048, 4096, 8192)]),
        ],
        custom_header_fn=_render_texture_preview,
        custom_body_fn=_render_sprite_body,
    )

    # ── Audio ──────────────────────────────────────────────────────────
    _categories["audio"] = AssetCategoryDef(
        display_name="asset.display_audio",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_audio,
        editable_fields=[
            FieldDef("force_mono", "asset.force_mono", WidgetType.CHECKBOX),
            FieldDef("load_in_background", "asset.audio_load_in_background", WidgetType.CHECKBOX),
            FieldDef(
                "compression_format", "asset.audio_compression_format", WidgetType.COMBO,
                [("asset.audio_compression_pcm", AudioCompressionFormat.PCM),
                 ("asset.audio_compression_vorbis", AudioCompressionFormat.VORBIS),
                 ("asset.audio_compression_adpcm", AudioCompressionFormat.ADPCM)],
            ),
            FieldDef(
                "quality", "asset.audio_quality", WidgetType.FLOAT,
                float_speed=0.01, float_range=(0.0, 1.0),
            ),
        ],
        extra_meta_keys=["file_size", "extension"],
        custom_header_fn=_render_audio_header,
    )

    # ── Shader ─────────────────────────────────────────────────────────
    _categories["shader"] = AssetCategoryDef(
        display_name="asset.display_shader",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_shader,
        custom_body_fn=_render_shader_body,
    )

    _categories["font"] = AssetCategoryDef(
        display_name="asset.display_font",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_font,
        custom_body_fn=_render_font_body,
        extra_meta_keys=["file_size", "extension"],
    )

    # ── Mesh ─────────────────────────────────────────────────────────────────
    _categories["mesh"] = AssetCategoryDef(
        display_name="asset.display_mesh",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_mesh,
        editable_fields=[
            FieldDef("scale_factor", "asset.scale_factor", WidgetType.FLOAT,
                     float_speed=0.001, float_range=(0.0001, 1000.0)),
            FieldDef("generate_normals", "asset.generate_normals", WidgetType.CHECKBOX),
            FieldDef("generate_tangents", "asset.generate_tangents", WidgetType.CHECKBOX),
            FieldDef("swap_uv_channels", "asset.swap_uv_channels", WidgetType.CHECKBOX),
            FieldDef("optimize_mesh", "asset.optimize_mesh", WidgetType.CHECKBOX),
        ],
        custom_header_fn=_render_mesh_header,
        extra_meta_keys=[
            "mesh_count",
            "vertex_count",
            "index_count",
            "material_slot_count",
            "bone_count",
            "bone_names_csv",
            "animation_count",
            "animation_names_csv",
        ],
        show_header=False,
    )

    # ── Material ───────────────────────────────────────────────────────
    _categories["material"] = AssetCategoryDef(
        display_name="asset.display_material",
        access_mode=AssetAccessMode.READ_WRITE_RESOURCE,
        load_fn=_load_material,
        refresh_fn=_refresh_material,
        custom_body_fn=_render_material_body,
        autosave_debounce=0.35,
    )

    _categories["render_effect"] = AssetCategoryDef(
        display_name="asset.display_render_effect",
        access_mode=AssetAccessMode.READ_WRITE_RESOURCE,
        load_fn=_load_render_effect,
        custom_body_fn=_render_render_effect_body,
        autosave_debounce=0.5,
    )

    _categories["physic_material"] = AssetCategoryDef(
        display_name="asset.display_physic_material",
        access_mode=AssetAccessMode.READ_WRITE_RESOURCE,
        load_fn=_load_physic_material,
        custom_body_fn=_render_physic_material_body,
        autosave_debounce=0.2,
    )

    # ── Prefab ─────────────────────────────────────────────────────────
    _categories["prefab"] = AssetCategoryDef(
        display_name="asset.display_prefab",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_prefab,
        custom_header_fn=_render_prefab_preview,
        custom_body_fn=_render_prefab_body,
        autosave_debounce=0.5,
    )

    # ── Animation Clip ─────────────────────────────────────────────────
    _categories["animclip"] = AssetCategoryDef(
        display_name="asset.display_animclip",
        access_mode=AssetAccessMode.READ_WRITE_RESOURCE,
        load_fn=_load_animclip,
        custom_body_fn=_render_animclip_body,
        autosave_debounce=0.5,
    )

    # ── 3D Animation Clip ──────────────────────────────────────────────
    _categories["animclip3d"] = AssetCategoryDef(
        display_name="asset.display_animclip3d",
        access_mode=AssetAccessMode.READ_WRITE_RESOURCE,
        load_fn=_load_animclip3d,
        custom_body_fn=_render_animclip3d_body,
        autosave_debounce=0.5,
    )

    # ── Animation State Machine ────────────────────────────────────────
    _categories["animfsm"] = AssetCategoryDef(
        display_name="asset.display_animfsm",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_animfsm,
        custom_body_fn=_render_animfsm_body,
        autosave_debounce=0.5,
    )

    _categories["animtimeline"] = AssetCategoryDef(
        display_name="asset.display_animtimeline",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_identity_asset,
    )

    # ── Timeline State Machine (.timelinefsm) ──────────────────────────
    _categories["timelinefsm"] = AssetCategoryDef(
        display_name="asset.display_timelinefsm",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_identity_asset,
    )

    _categories["particle_graph"] = AssetCategoryDef(
        display_name="asset.display_particlegraph",
        access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
        load_fn=_load_particlegraph,
        custom_body_fn=_render_particlegraph_body,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Per-category loaders
# ═══════════════════════════════════════════════════════════════════════════


def _load_texture(path: str):
    return read_texture_import_settings(path), {"preview_height": 200.0}


def _load_audio(path: str):
    extra = {"duration": 0.0, "sample_rate": 0, "channels": 0, "sample_count": 0}
    try:
        with wave.open(path, "rb") as stream:
            sample_rate = int(stream.getframerate())
            sample_count = int(stream.getnframes())
            extra.update(
                duration=(float(sample_count) / float(sample_rate)) if sample_rate > 0 else 0.0,
                sample_rate=sample_rate,
                channels=int(stream.getnchannels()),
                sample_count=sample_count,
            )
    except (OSError, EOFError, wave.Error):
        pass
    return read_audio_import_settings(path), extra


def _load_identity_asset(path: str):
    return {"path": path}, {}


def _load_shader(path: str):
    meta = read_meta_file(path)
    guid = (meta or {}).get("guid", "")
    return ShaderAssetInfo.from_path(path, guid=guid), {}


def _load_font(path: str):
    meta = read_meta_file(path)
    guid = (meta or {}).get("guid", "")
    return FontAssetInfo.from_path(path, guid=guid), {}


def _load_mesh(path: str):
    return read_mesh_import_settings(path), {}


def _load_material(path: str):
    from Infernux.core.material import Material
    mat = Material.load(path)
    if mat is None:
        return None
    native = mat.native
    cached = native.serialize_document()
    old_prop_names = set(cached.get("properties", {}).keys())
    _sync_material_shader_metadata(cached)
    new_prop_names = set(cached.get("properties", {}).keys())
    if new_prop_names != old_prop_names:
        # Vertex/fragment shader sync added new properties — push them to the
        # native C++ material so the UBO picks up the correct default values.
        native.deserialize_document(cached)
    return mat, {
        "native_mat": native,
        "cached_data": cached,
        "cached_json": json.dumps(cached),
        "shader_cache": {".vert": None, ".frag": None},
        "shader_sync_key": "",
        "_applied_version": native.get_version(),
    }


def _load_render_effect(path: str):
    from Infernux.renderstack.render_effect import (
        EditableRenderEffectGroup,
        RenderEffect,
    )

    if path.lower().endswith(".effect"):
        from Infernux.core.assets import AssetManager

        effect = AssetManager.load(path, asset_type=RenderEffect)
        return (effect, {"document_kind": "effect"}) if effect is not None else None

    from pathlib import Path
    from Infernux.renderstack.render_effect_asset import (
        RenderEffectGroupAsset,
        parse_render_effect_document,
    )

    document = parse_render_effect_document(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, RenderEffectGroupAsset):
        return None
    guid = str((read_meta_file(path) or {}).get("guid", "") or "")
    return EditableRenderEffectGroup(
        document,
        file_path=path,
        guid=guid,
    ), {"document_kind": "group"}


def _load_physic_material(path: str):
    from Infernux.core.physic_material import PhysicMaterial
    material = PhysicMaterial.load(path)
    return (material, {}) if material is not None else None


def _apply_physic_material_edit(state: _State, field_name: str, value) -> bool:
    material = state.settings
    old_document = material.serialize_document()
    if old_document[field_name] == value:
        return False
    new_document = dict(old_document)
    new_document[field_name] = value
    controller = getattr(state, "resource_controller", None)
    document_id = str(getattr(state, "document_id", "") or "")
    if controller is None or not document_id:
        return False
    return controller.apply_document(
        new_document,
        view_id="inspector",
        edit_key=field_name,
        description=f"Set PhysicMaterial {field_name}",
    )


def _render_physic_material_body(ctx: InxGUIContext, panel, state: _State):
    del panel
    from .inspector_utils import render_compact_section_header

    material = state.settings
    if material is None:
        return
    if not render_compact_section_header(ctx, t("asset.physic_material_properties"), level="secondary"):
        return

    labels = [
        t("asset.friction"), t("asset.bounciness"),
        t("asset.friction_combine"), t("asset.bounce_combine"),
    ]
    label_width = max_label_w(ctx, labels)
    field_label(ctx, labels[0], label_width)
    friction = ctx.drag_float("##physic_friction", float(material.friction), 0.01, 0.0, 1.0)
    if friction != material.friction:
        _apply_physic_material_edit(state, "friction", friction)

    field_label(ctx, labels[1], label_width)
    bounciness = ctx.drag_float("##physic_bounciness", float(material.bounciness), 0.01, 0.0, 1.0)
    if bounciness != material.bounciness:
        _apply_physic_material_edit(state, "bounciness", bounciness)

    combine_labels = [
        t("asset.combine_average"), t("asset.combine_minimum"),
        t("asset.combine_multiply"), t("asset.combine_maximum"),
    ]
    field_label(ctx, labels[2], label_width)
    friction_combine = ctx.combo("##physic_friction_combine", int(material.friction_combine), combine_labels)
    if friction_combine != material.friction_combine:
        _apply_physic_material_edit(state, "friction_combine", friction_combine)

    field_label(ctx, labels[3], label_width)
    bounce_combine = ctx.combo("##physic_bounce_combine", int(material.bounce_combine), combine_labels)
    if bounce_combine != material.bounce_combine:
        _apply_physic_material_edit(state, "bounce_combine", bounce_combine)


def _load_prefab(path: str):
    """Load a .prefab file into its data-only inspector representation.

    The previous implementation instantiated a hidden preview scene and then
    routed the prefab through the full object inspector. That path re-used
    editor-only object rendering on non-active temporary scene objects and
    allocated a new native scene for each selection, which is not safe with
    the current SceneManager API surface.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("prefab document must contain a JSON object")

    root_json = data.get("root_object")
    if not isinstance(root_json, dict):
        raise ValueError("prefab document must contain a root_object")

    root_copy = copy.deepcopy(root_json)
    return root_copy, {
        "prefab_path": path,
        "prefab_envelope": data,
        "root_name": root_copy.get("name", "GameObject"),
        "node_count": _count_prefab_nodes(root_copy),
        "component_count": _count_prefab_components(root_copy),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Prefab body — exact same components rendering as hierarchy instances
# ═══════════════════════════════════════════════════════════════════════════

def _render_prefab_body(ctx: InxGUIContext, panel, state: _State):
    """Render a safe prefab summary plus Prefab Mode entry point.

    Inline full-object rendering is intentionally disabled here for stability.
    Prefab editing remains available through Prefab Mode.
    """
    from .inspector_utils import render_info_text, render_compact_section_header

    prefab_root = state.settings
    if not isinstance(prefab_root, dict):
        ctx.label(t("asset.invalid_prefab"))
        return

    def _open_prefab_mode():
        from Infernux.engine.interaction import EditorInteractionCore

        interaction = EditorInteractionCore.instance()
        if interaction is not None:
            interaction.prefabs.open(path=state.extra["prefab_path"])

    ctx.dummy(0, 4)
    ctx.push_style_color(ImGuiCol.Button, *Theme.PREFAB_BTN_NORMAL)
    ctx.push_style_color(ImGuiCol.ButtonHovered, *Theme.PREFAB_BTN_HOVERED)
    ctx.push_style_color(ImGuiCol.ButtonActive, *Theme.PREFAB_BTN_ACTIVE)
    try:
        ctx.button(t("asset.open_prefab_mode"), _open_prefab_mode, -1, 32)
    finally:
        ctx.pop_style_color(3)

    ctx.dummy(0, 8)
    render_info_text(ctx, t("asset.prefab_safe_mode"))
    ctx.separator()
    ctx.dummy(0, 4)

    labels = [
        t("asset.prefab_root"),
        t("asset.prefab_nodes"),
        t("asset.prefab_components_count"),
        t("asset.prefab_scripts_count"),
        t("asset.prefab_path"),
    ]
    lw = max_label_w(ctx, labels)

    root_name = state.extra.get("root_name", prefab_root.get("name", "GameObject"))
    component_count = state.extra.get("component_count", 0)
    script_count = _count_prefab_script_components(prefab_root)
    node_count = state.extra.get("node_count", 1)

    field_label(ctx, t("asset.prefab_root"), lw)
    ctx.label(str(root_name))
    field_label(ctx, t("asset.prefab_nodes"), lw)
    ctx.label(str(node_count))
    field_label(ctx, t("asset.prefab_components_count"), lw)
    ctx.label(str(component_count))
    field_label(ctx, t("asset.prefab_scripts_count"), lw)
    ctx.label(str(script_count))
    field_label(ctx, t("asset.prefab_path"), lw)
    ctx.label(state.extra.get("prefab_path", state.file_path))

    ctx.dummy(0, 6)
    ctx.separator()

    if render_compact_section_header(ctx, t("asset.prefab_root_object"), level="secondary"):
        _render_prefab_root_summary(ctx, prefab_root)

    if render_compact_section_header(ctx, t("asset.prefab_raw_json_preview"), default_open=False, level="secondary"):
        preview = json.dumps(prefab_root, indent=2, ensure_ascii=False)
        if len(preview) > 8000:
            preview = preview[:8000] + "\n..."
        ctx.label(preview)


def _count_prefab_nodes(node: dict) -> int:
    total = 1
    for child in node.get("children", []):
        if isinstance(child, dict):
            total += _count_prefab_nodes(child)
    return total


def _count_prefab_components(node: dict) -> int:
    total = len(node.get("components", []))
    for child in node.get("children", []):
        if isinstance(child, dict):
            total += _count_prefab_components(child)
    return total


def _count_prefab_script_components(node: dict) -> int:
    total = sum(
        1 for component in node.get("components", [])
        if isinstance(component, dict) and str(component.get("type_id", "")).startswith("python:")
    )
    for child in node.get("children", []):
        if isinstance(child, dict):
            total += _count_prefab_script_components(child)
    return total


def _render_prefab_root_summary(ctx: InxGUIContext, root: dict):
    transform = root.get("transform", {}) if isinstance(root.get("transform"), dict) else {}
    position = transform.get("position", [0.0, 0.0, 0.0])
    rotation = transform.get("rotation", [0.0, 0.0, 0.0])
    scale = transform.get("scale", [1.0, 1.0, 1.0])
    ctx.label(f"{t('asset.prefab_name')}: {root.get('name', 'GameObject')}")
    ctx.label(f"{t('asset.prefab_active')}: {bool(root.get('active', True))}")
    ctx.label(f"{t('asset.prefab_tag')}: {root.get('tag', 'Untagged')}")
    ctx.label(f"{t('asset.prefab_layer')}: {root.get('layer', 0)}")
    ctx.label(f"{t('asset.prefab_position')}: {position}")
    ctx.label(f"{t('asset.prefab_rotation')}: {rotation}")
    ctx.label(f"{t('asset.prefab_scale')}: {scale}")
    components = root.get("components", [])
    script_count = sum(
        1 for component in components
        if isinstance(component, dict) and str(component.get("type_id", "")).startswith("python:")
    )
    ctx.label(f"{t('asset.prefab_native_components')}: {len(components) - script_count}")
    ctx.label(f"{t('asset.prefab_script_components')}: {script_count}")
    ctx.label(f"{t('asset.prefab_children_count')}: {len(root.get('children', []))}")


# ═══════════════════════════════════════════════════════════════════════════
# Animation Clip loader & body
# ═══════════════════════════════════════════════════════════════════════════

def _load_animclip(path: str):
    from Infernux.core.animation_clip import AnimationClip
    clip = AnimationClip.load(path)
    if clip is None:
        return None
    return clip, {"clip_path": path}


def _render_animclip_body(ctx: InxGUIContext, panel, state: _State):
    from Infernux.core.animation_clip import AnimationClip
    from .inspector_utils import render_compact_section_header, render_info_text
    from .inspector_components import render_asset_reference_field

    clip: AnimationClip = state.settings
    if not isinstance(clip, AnimationClip):
        ctx.label(t("asset.invalid_animclip"))
        return

    labels = [
        t("asset.animclip_name"),
        t("asset.animclip_texture"),
        t("asset.animclip_preview_texture"),
        t("asset.animclip_fps"),
        t("asset.animclip_frames"),
    ]
    lw = max_label_w(ctx, labels)

    ctx.dummy(0, 4)

    # ── Clip name (read-only, derived from filename) ───────────
    clip_display_name = os.path.splitext(os.path.basename(state.file_path))[0] if state.file_path else clip.name
    field_label(ctx, t("asset.animclip_name"), lw)
    ctx.begin_disabled(True)
    ctx.text_input("##animclip_name", clip_display_name, 256)
    ctx.end_disabled()

    # ── Authoring texture reference (read-only) ────────────────
    authoring_path = _resolve_authoring_texture_path(clip)
    if authoring_path:
        display = os.path.basename(authoring_path)
    elif clip.authoring_texture_guid:
        display = "(missing) " + clip.authoring_texture_guid[:8] + "…"
    elif clip.authoring_texture_path:
        display = "(missing) " + os.path.basename(clip.authoring_texture_path)
    else:
        display = "None (Texture)"

    field_label(ctx, t("asset.animclip_texture"), lw)
    render_asset_reference_field(
        ctx,
        "##animclip_texture",
        display,
        "Texture",
        asset_type="Texture",
        ping_path=authoring_path or None,
        has_value=bool(
            clip.authoring_texture_guid or clip.authoring_texture_path
        ),
        reference_value={
            "asset_type": "Texture",
            "guid": str(clip.authoring_texture_guid or ""),
            "path_hint": str(authoring_path or clip.authoring_texture_path or ""),
        },
        read_only=True,
    )

    # ── Preview texture override (drag-droppable) ──────────────
    preview_override = state.extra.get("_animclip_preview_override_path", "")
    if preview_override:
        pv_display = os.path.basename(preview_override)
    else:
        pv_display = "None (use authoring)"

    def _on_preview_texture_drop(payload):
        if isinstance(payload, dict):
            tex_path = str(payload.get("path_hint") or "").strip()
            if not tex_path and payload.get("guid"):
                try:
                    from Infernux.core.assets import AssetManager

                    adb = getattr(AssetManager, "_asset_database", None)
                    tex_path = (
                        str(adb.get_path_from_guid(payload["guid"]) or "")
                        if adb else ""
                    )
                except (AttributeError, RuntimeError):
                    tex_path = ""
        else:
            tex_path = str(payload or "")
        if tex_path and os.path.isfile(tex_path):
            state.extra["_animclip_preview_override_path"] = tex_path
            # Clear cached preview texture so it reloads
            state.extra.pop("_animclip_pv", None)

    def _clear_preview():
        state.extra.pop("_animclip_preview_override_path", None)
        state.extra.pop("_animclip_pv", None)

    field_label(ctx, t("asset.animclip_preview_texture"), lw)
    render_asset_reference_field(
        ctx,
        "##animclip_pv_tex",
        pv_display,
        "Texture",
        asset_type="Texture",
        accept_drag_type=("TEXTURE_GUID", "TEXTURE_FILE"),
        on_assign=_on_preview_texture_drop,
        on_clear=_clear_preview,
        ping_path=preview_override or None,
        has_value=bool(preview_override),
        reference_value=(
            {"asset_type": "Texture", "path_hint": preview_override}
            if preview_override else None
        ),
    )

    # ── FPS (editable) ─────────────────────────────────────────
    field_label(ctx, t("asset.animclip_fps"), lw)
    new_fps = ctx.drag_float("##animclip_fps", clip.fps, 0.1, 0.1, 120.0)
    if new_fps != clip.fps:
        document = clip.serialize_document()
        document["fps"] = max(0.1, new_fps)
        _apply_editable_resource_document(
            state,
            document,
            edit_key="fps",
            description="Set Animation Clip FPS",
        )

    ctx.separator()

    # ── Preview ────────────────────────────────────────────────
    _render_animclip_preview(ctx, clip, state)

    ctx.separator()

    # ── Stable source-frame IDs (read-only) ────────────────────
    if render_compact_section_header(ctx, t("asset.animclip_frames")):
        frame_count = clip.frame_count
        duration = clip.duration
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label(t("asset.animclip_frame_count").format(count=frame_count))
        ctx.label(t("asset.animclip_duration").format(duration=f"{duration:.3f}"))
        ctx.pop_style_color(1)

        ctx.dummy(0, 4)

        frame_str = ", ".join(
            frame.sprite_frame_id for frame in clip.frames
        )
        field_label(ctx, t("asset.animclip_sequence"), lw)
        ctx.begin_disabled(True)
        ctx.text_input("##animclip_frame_seq", frame_str, 2048)
        ctx.end_disabled()

    ctx.separator()

def _load_animclip3d(path: str):
    from Infernux.core.animation_clip3d import AnimationClip3D

    if "::subanim:" in path:
        clip = AnimationClip3D.from_embedded_take_virtual_path(path)
        if clip is None:
            return None
        return clip, {"clip_path": path, "embedded_model_take": True}
    clip = AnimationClip3D.load(path)
    if clip is None:
        return None
    return clip, {"clip_path": path, "embedded_model_take": False}


def _render_animclip3d_body(ctx: InxGUIContext, panel, state: _State):
    from Infernux.core.animation_clip3d import AnimationClip3D
    from Infernux.core.assets import AssetManager
    from .inspector_components import render_asset_reference_field

    clip: AnimationClip3D = state.settings
    if not isinstance(clip, AnimationClip3D):
        ctx.label(t("asset.invalid_animclip3d"))
        return

    labels = [
        t("asset.animclip3d_name"),
        t("asset.animclip3d_source_model"),
        t("asset.animclip3d_take"),
    ]
    lw = max_label_w(ctx, labels)

    ctx.dummy(0, 4)

    embedded = bool(state.extra.get("embedded_model_take"))
    if embedded:
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label(t("asset.animclip3d_embedded_hint"))
        ctx.pop_style_color(1)
        ctx.dummy(0, 4)

    clip_display_name = os.path.splitext(os.path.basename(state.file_path))[0] if state.file_path else clip.name
    field_label(ctx, t("asset.animclip3d_name"), lw)
    ctx.begin_disabled(True)
    ctx.text_input("##animclip3d_name", clip_display_name, 256)
    ctx.end_disabled()

    # ── Source model reference ────────────────────────────────────
    model_path = (clip.source_model_path or "").strip()
    if not model_path and (clip.source_model_guid or "").strip():
        try:
            adb = getattr(AssetManager, "_asset_database", None)
            if adb:
                model_path = adb.get_path_from_guid(clip.source_model_guid) or ""
        except Exception:
            model_path = ""

    if model_path and os.path.isfile(model_path):
        model_display = os.path.basename(model_path)
    elif clip.source_model_guid:
        model_display = "(missing) " + clip.source_model_guid[:8] + "…"
    elif clip.source_model_path:
        model_display = "(missing) " + os.path.basename(clip.source_model_path)
    else:
        model_display = "None (Model)"

    if embedded:
        ctx.begin_disabled(True)

    field_label(ctx, t("asset.animclip3d_source_model"), lw)

    def _on_model_pick(path, _clip=clip):
        supplied_guid = ""
        if isinstance(path, dict):
            supplied_guid = str(path.get("guid") or "").strip()
            p = str(path.get("path_hint") or "").strip()
            if supplied_guid and not p:
                try:
                    adb = getattr(AssetManager, "_asset_database", None)
                    p = str(adb.get_path_from_guid(supplied_guid) or "") if adb else ""
                except (AttributeError, RuntimeError):
                    p = ""
        else:
            p = str(path or "").strip()
        if not p:
            return
        model_guid = supplied_guid
        try:
            model_guid = model_guid or AssetManager._get_guid_from_path(p) or ""
        except Exception:
            pass
        bone_names = []
        # Pull bind pose bone names from import metadata (cheap UX).
        meta = read_meta_file(p) or {}
        csv = str(meta.get("bone_names_csv", "") or "")
        if csv:
            bone_names = [x.strip() for x in csv.split(",") if x.strip()]
        document = _clip.serialize_document()
        document["source_model_path"] = p
        document["source_model_guid"] = model_guid
        document["bind_pose_bone_names"] = bone_names
        _apply_editable_resource_document(
            state,
            document,
            edit_key="source_model",
            description="Set Animation Clip 3D Source Model",
        )

    def _on_model_clear(_clip=clip):
        document = _clip.serialize_document()
        document["source_model_guid"] = ""
        document["source_model_path"] = ""
        document["bind_pose_bone_names"] = []
        _apply_editable_resource_document(
            state,
            document,
            edit_key="source_model",
            description="Clear Animation Clip 3D Source Model",
        )

    render_asset_reference_field(
        ctx,
        "##animclip3d_model",
        model_display,
        "Model",
        asset_type="Mesh",
        accept_drag_type=("MODEL_FILE", "MODEL_GUID"),
        on_assign=_on_model_pick,
        on_clear=_on_model_clear,
        ping_path=model_path or None,
        has_value=bool(clip.source_model_guid or clip.source_model_path),
        reference_value={
            "asset_type": "Mesh",
            "guid": str(clip.source_model_guid or ""),
            "path_hint": str(model_path or clip.source_model_path or ""),
        },
    )

    # ── Take name ─────────────────────────────────────────────────
    field_label(ctx, t("asset.animclip3d_take"), lw)
    take_buf = clip.take_name or ""
    new_take = ctx.text_input("##animclip3d_take", take_buf, 256)
    if not embedded and new_take != take_buf:
        document = clip.serialize_document()
        document["take_name"] = new_take
        _apply_editable_resource_document(
            state,
            document,
            edit_key="take_name",
            description="Set Animation Clip 3D Take",
        )

    if embedded:
        ctx.end_disabled()

    # ── Imported bone summary (read-only) ─────────────────────────
    if clip.bind_pose_bone_names:
        ctx.separator()
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label(t("asset.animclip3d_bind_pose_bones").format(count=len(clip.bind_pose_bone_names)))
        preview = ", ".join(clip.bind_pose_bone_names[:24])
        if len(clip.bind_pose_bone_names) > 24:
            preview += ", …"
        ctx.label(preview)
        ctx.pop_style_color(1)

def _resolve_authoring_texture_path(clip) -> str:
    """Resolve the actual file path for the clip's authoring texture."""
    if clip.authoring_texture_path and os.path.isfile(clip.authoring_texture_path):
        return clip.authoring_texture_path
    if clip.authoring_texture_guid:
        try:
            from Infernux.engine.bootstrap import EditorBootstrap
            adb = EditorBootstrap.instance().engine.get_asset_database()
            if adb:
                p = adb.get_path_from_guid(clip.authoring_texture_guid)
                if p and os.path.isfile(p):
                    return p
        except Exception:
            pass
    return ""


def _safe_mtime_ns(path: str) -> int:
    try:
        return int(os.stat(path).st_mtime_ns)
    except OSError:
        return 0


def _animclip_texture_stamp(path: str, nearest: bool, srgb: bool) -> int:
    image_mtime = _safe_mtime_ns(path)
    meta_mtime = _safe_mtime_ns(f"{path}.meta")
    base = int((image_mtime ^ ((meta_mtime * 2654435761) & _U64)) & _U64)
    setting_hash = int(zlib.crc32(f"{int(nearest)}|{int(srgb)}".encode("utf-8")) & 0xFFFFFFFF)
    return int((base ^ setting_hash) & _U64)


def _read_animclip_preview_settings(tex_file: str):
    use_nearest = False
    use_srgb = False
    frames = []
    source_w = 0
    source_h = 0
    try:
        settings = read_texture_import_settings(tex_file)
        if settings:
            use_nearest = getattr(settings, "filter_mode", None) == FilterMode.POINT
            use_srgb = bool(getattr(settings, "srgb", False))
            if settings.sprite_frames:
                frames = list(settings.sprite_frames)
    except Exception:
        pass
    try:
        meta = read_meta_file(tex_file) or {}
        source_w = int(meta.get("width", 0) or 0)
        source_h = int(meta.get("height", 0) or 0)
    except Exception:
        pass
    if (source_w <= 0 or source_h <= 0) and frames:
        source_w = max((int(f.x) + int(f.w) for f in frames), default=0)
        source_h = max((int(f.y) + int(f.h) for f in frames), default=0)
    return use_nearest, use_srgb, frames, source_w, source_h


def _sprite_frame_imgui_uv(frame: SpriteFrame, tex_w: int, tex_h: int) -> Tuple[float, float, float, float]:
    """Map sprite-frame coordinates to the existing ImGui preview UV convention."""
    inv_w = float(max(tex_w, 1))
    inv_h = float(max(tex_h, 1))
    uv0_x = float(frame.x) / inv_w
    uv0_y = float(frame.y) / inv_h
    uv1_x = float(frame.x + frame.w) / inv_w
    uv1_y = float(frame.y + frame.h) / inv_h
    return uv0_x, uv0_y, uv1_x, uv1_y


def _ensure_animclip_preview_texture(state: _State, tex_file: str) -> bool:
    """Queue/fetch sprite-sheet preview via native C++ task system."""

    if not tex_file:
        return False

    try:
        from Infernux.engine.ui.editor_services import EditorServices

        svc = EditorServices.instance()
        native = svc.native_engine if svc else None
        if not native:
            return False

        use_nearest, use_srgb, frames, source_w, source_h = _read_animclip_preview_settings(tex_file)
        stamp = _animclip_texture_stamp(tex_file, use_nearest, use_srgb)
        if stamp == 0:
            return False

        norm_path = resolved_path(tex_file)
        resource_key = f"animclip_insp|{norm_path}|{int(use_nearest)}|{int(use_srgb)}"

        pv = state.extra.get("_animclip_pv")
        if (
            pv
            and pv.get("resource_key") == resource_key
            and int(pv.get("stamp", 0)) == int(stamp)
            and int(pv.get("tex_id", 0)) != 0
        ):
            # Re-resolve the descriptor every frame: C++ replaces/evicts the
            # underlying ImGui texture, so a handle cached across frames may
            # point at a freed VkDescriptorSet (validation errors + crashes).
            live_id = 0
            if hasattr(native, "get_texture_preview_texture_id"):
                live_id = int(native.get_texture_preview_texture_id(resource_key) or 0)
            if live_id:
                pv["tex_id"] = live_id
                return True
            # Texture gone (replaced mid-flight or evicted) — fall through to
            # a full re-query so a fresh render gets scheduled.

        native.pump_preview_tasks()
        tex_id, tex_w, tex_h = native.query_or_schedule_texture_preview(
            resource_key, norm_path, int(stamp), nearest=bool(use_nearest),
            srgb=bool(use_srgb), pump=False)
        tex_id = int(tex_id)
        tex_w = int(tex_w)
        tex_h = int(tex_h)

        if tex_id == 0:
            return False

        if tex_w <= 0 or tex_h <= 0:
            return False

        if source_w <= 0:
            source_w = tex_w
        if source_h <= 0:
            source_h = tex_h

        state.extra["_animclip_pv"] = {
            "file": norm_path,
            "resource_key": resource_key,
            "stamp": int(stamp),
            "tex_id": tex_id,
            "tex_w": source_w,
            "tex_h": source_h,
            "frames": frames,
        }
        return True
    except Exception as exc:
        Debug.log_warning(f"[AnimClipPreview] {exc}")
        return False


def _render_animclip_preview(ctx: InxGUIContext, clip, state: _State):
    """Render an animated preview of the animation clip in the inspector."""
    import time as _time

    # Use preview override if set, otherwise fall back to authoring texture
    preview_override = state.extra.get("_animclip_preview_override_path", "")
    if preview_override and os.path.isfile(preview_override):
        tex_file = preview_override
    else:
        tex_file = _resolve_authoring_texture_path(clip)

    if not tex_file:
        from .inspector_utils import render_info_text
        if clip.authoring_texture_guid or clip.authoring_texture_path:
            render_info_text(ctx, t("asset.animclip_texture_missing"))
        return

    if not clip.frames:
        return

    if not _ensure_animclip_preview_texture(state, tex_file):
        return

    pv = state.extra.get("_animclip_pv")
    if not pv:
        return

    tex_id = pv["tex_id"]
    tex_w = pv["tex_w"]
    tex_h = pv["tex_h"]
    frames = pv["frames"]
    fc = len(clip.frames)

    # Playback state in state.extra
    pb = state.extra.get("_animclip_pb")
    if pb is None:
        pb = {"playing": False, "frame_idx": 0, "last_time": 0.0}
        state.extra["_animclip_pb"] = pb

    # Transport: Play/Pause + frame counter
    is_playing = pb["playing"]
    if is_playing:
        if ctx.button(t("animclip_editor.pause") + "##insp_transport"):
            pb["playing"] = False
    else:
        if ctx.button(t("animclip_editor.play") + "##insp_transport"):
            pb["playing"] = True
            pb["last_time"] = _time.perf_counter()
            if pb["frame_idx"] >= fc:
                pb["frame_idx"] = 0

    ctx.same_line(0, 8)
    ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
    ctx.label(f"{pb['frame_idx'] + 1}/{fc}")
    ctx.pop_style_color(1)

    # Advance frame
    if pb["playing"] and clip.fps > 0:
        now = _time.perf_counter()
        elapsed = now - pb["last_time"]
        interval = 1.0 / clip.fps
        if elapsed >= interval:
            steps = int(elapsed / interval)
            pb["frame_idx"] += steps
            pb["last_time"] = now
            if pb["frame_idx"] >= fc:
                pb["frame_idx"] = pb["frame_idx"] % fc

    fi = max(0, min(pb["frame_idx"], fc - 1))
    source_id = clip.frames[fi].sprite_frame_id
    frame = next(
        (candidate for candidate in frames if candidate.stable_id == source_id),
        None,
    )
    if frame is None:
        return

    uv0_x, uv0_y, uv1_x, uv1_y = _sprite_frame_imgui_uv(frame, tex_w, tex_h)

    # Fit into available width, max 200px
    avail_w = ctx.get_content_region_avail_width()
    max_dim = min(200.0, avail_w - 16.0)
    aspect = frame.w / max(frame.h, 1)
    if aspect >= 1.0:
        pw = max_dim
        ph = max_dim / aspect
    else:
        ph = max_dim
        pw = max_dim * aspect

    # Center horizontally
    pad_x = (avail_w - pw) * 0.5
    if pad_x > 0:
        ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + pad_x)

    ctx.image(tex_id, pw, ph, uv0_x, uv0_y, uv1_x, uv1_y)


def _get_sprite_frames(texture_guid: str, texture_path: str = "") -> list[SpriteFrame]:
    path = ""
    if texture_guid:
        try:
            from Infernux.engine.bootstrap import EditorBootstrap
            adb = EditorBootstrap.instance().engine.get_asset_database()
            if adb:
                path = adb.get_path_from_guid(texture_guid) or ""
        except Exception:
            pass
    if not path and texture_path:
        path = texture_path
    if not path:
        return []
    try:
        from Infernux.core.asset_types import read_texture_import_settings
        settings = read_texture_import_settings(path)
        return list(settings.sprite_frames)
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════
# Animation State Machine inspector
# ═══════════════════════════════════════════════════════════════════════════

def _load_animfsm(path: str):
    from Infernux.core.anim_state_machine import AnimStateMachine
    fsm = AnimStateMachine.load(path)
    if fsm is None:
        return None
    return fsm, {"fsm_path": path}


def _load_particlegraph(path: str):
    from Infernux.particle.asset import ParticleGraphAsset

    graph = ParticleGraphAsset.load(path)
    return graph, {"particle_graph_path": path}


def _render_particlegraph_body(ctx: InxGUIContext, panel, state: _State):
    from Infernux.particle.asset import ParticleGraphAsset
    from .inspector_utils import field_label

    graph = state.settings
    if not isinstance(graph, ParticleGraphAsset):
        ctx.label(t("asset.failed_load").format(name=t("asset.display_particlegraph")))
        return

    label_width = 120.0
    field_label(ctx, t("asset.display_particlegraph"), label_width)
    ctx.same_line()
    ctx.label(graph.name or os.path.basename(state.file_path))
    field_label(ctx, t("asset.particle_emitters"), label_width)
    ctx.same_line()
    ctx.label(str(len(graph.emitters)))

    ctx.dummy(0, 8)
    if not ctx.button(t("asset.particle_open_editor")):
        return
    open_fn = getattr(panel, "open_particle_graph", None) if panel is not None else None
    if callable(open_fn):
        open_fn(state.file_path)
        return
    from Infernux.engine.interaction import (
        DocumentKind,
        DocumentOpenStatus,
        EditorInteractionCore,
    )

    core = EditorInteractionCore.instance()
    if core is None:
        raise RuntimeError("document open requires EditorInteractionCore")
    result = core.document_open.open_resource(
        DocumentKind.PARTICLE_GRAPH,
        state.file_path,
    )
    if result.status is DocumentOpenStatus.FAILED:
        raise RuntimeError(result.message or "Particle Graph open failed")


def _render_animfsm_body(ctx: InxGUIContext, panel, state: _State):
    from Infernux.core.anim_state_machine import AnimStateMachine
    from .inspector_utils import render_compact_section_header, field_label

    fsm: AnimStateMachine = state.settings
    if not isinstance(fsm, AnimStateMachine):
        ctx.label(t("asset.invalid_animfsm"))
        return

    lw = 120.0

    # Name (read-only)
    field_label(ctx, t("animfsm_editor.name"), lw)
    ctx.same_line()
    ctx.label(fsm.name)

    # Default state
    field_label(ctx, t("asset.animfsm_default_state"), lw)
    ctx.same_line()
    ctx.label(fsm.default_state or "—")

    # States section
    if render_compact_section_header(ctx, t("animfsm_editor.states").format(count=fsm.state_count)):
        for s in fsm.states:
            is_default = (s.name == fsm.default_state)
            prefix = "► " if is_default else "  "
            ctx.label(f"{prefix}{s.name}")
            clip_path = ""
            if s.clip_guid:
                try:
                    from Infernux.engine.bootstrap import EditorBootstrap
                    bs = EditorBootstrap.instance()
                    adb = bs.engine.get_asset_database() if bs and bs.engine else None
                    if adb:
                        clip_path = adb.get_path_from_guid(s.clip_guid) or ""
                except Exception:
                    pass
            if not clip_path:
                clip_path = s.clip_path
            if clip_path:
                ctx.same_line()
                ctx.label(f"  [{os.path.basename(clip_path)}]")
            for tr in s.transitions:
                ctx.label(f"      → {tr.target_state}")
                if tr.conditions:
                    labels = []
                    for condition in tr.conditions:
                        parameter = fsm.parameter_by_id(condition.parameter_id)
                        name = parameter.name if parameter is not None else "Missing"
                        labels.append(
                            f"{name} {condition.operator} {condition.threshold:g}"
                        )
                    ctx.same_line()
                    ctx.label(f"  ({' AND '.join(labels)})")


# ═══════════════════════════════════════════════════════════════════════════
# Material inspector
# ═══════════════════════════════════════════════════════════════════════════

def _refresh_material(state: _State):
    native = state.extra.get("native_mat")
    if not native:
        return
    current_version = native.get_version()
    # Fast-path: when the only mutations since the last refresh came from
    # the Python-side property editor (sliders, combos, etc.), cached_data
    # is already in sync with the native material.  Skip the expensive
    # native document -> merge -> preview-cache encoding round-trip (~1-7 ms).
    applied_version = state.extra.get("_applied_version", -2)
    if current_version == applied_version:
        return
    state.extra["_applied_version"] = current_version
    document = native.serialize_document()
    _sync_material_shader_metadata(document)
    state.extra["cached_data"] = document
    state.extra["cached_json"] = json.dumps(document)


def _sync_material_shader_metadata(mat_data: dict):
    shaders = mat_data.get("shaders") if isinstance(mat_data.get("shaders"), dict) else {}
    from . import inspector_shader_utils as shader_utils

    vert_shader_id = shader_utils.shader_ref_id(shaders.get("vertex", ""))
    frag_shader_id = shader_utils.shader_ref_id(shaders.get("fragment", ""))
    if vert_shader_id or frag_shader_id:
        shader_utils.sync_all_shader_properties(mat_data, vert_shader_id, frag_shader_id, remove_unknown=True)


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def render_asset_inspector(ctx: InxGUIContext, panel,
                           file_path: str, category: str):
    """Single entry point for all asset inspectors."""
    _ensure_categories()
    cat_def = _categories.get(category)
    if cat_def is None:
        display_name = str(category or "asset").replace("_", " ").strip().title()
        if not display_name:
            display_name = os.path.splitext(os.path.basename(file_path))[1].lstrip(".").upper() or "Asset"
        cat_def = AssetCategoryDef(
            display_name=display_name,
            access_mode=AssetAccessMode.READ_ONLY_RESOURCE,
            load_fn=_load_identity_asset,
        )
        _categories[category] = cat_def

    if not _state.load(file_path, category, cat_def):
        ctx.label(t("asset.failed_load").format(name=t(cat_def.display_name)))
        ctx.label(file_path)
        return

    _state.exec_layer = get_asset_execution_layer(
        _state.exec_layer, category, file_path, cat_def.access_mode,
        autosave_debounce_sec=cat_def.autosave_debounce,
    )
    _bind_asset_document(_state, cat_def)

    # ── Header (shared for all categories) ─────────────────────────────
    if cat_def.show_header:
        _render_header(ctx, cat_def, _state)

    # ── Custom header additions (e.g. texture preview) ─────────────────
    if cat_def.custom_header_fn:
        cat_def.custom_header_fn(ctx, panel, _state)

    # ── Body (auto-generated fields or custom) ─────────────────────────
    if cat_def.custom_body_fn:
        cat_def.custom_body_fn(ctx, panel, _state)
    elif cat_def.editable_fields:
        _render_import_fields(ctx, cat_def, _state)

    # ── Footer ─────────────────────────────────────────────────────────
    if (cat_def.access_mode == AssetAccessMode.READ_ONLY_RESOURCE
            and cat_def.editable_fields):
        render_apply_revert(
            ctx, _state.is_dirty(),
            on_apply=lambda: _on_apply(),
            on_revert=_on_revert,
            semantic_prefix=f"asset.{category}.import",
        )
    elif cat_def.access_mode == AssetAccessMode.READ_WRITE_RESOURCE:
        if _state.resource_controller is not None:
            _state.resource_controller.flush_autosave()
        elif _state.exec_layer:
            _state.exec_layer.flush_rw_autosave()


def invalidate():
    """Reset all inspector state (called on selection change)."""
    from Infernux.engine.interaction import ContinuousEditService, DocumentRegistry

    ContinuousEditService.instance().commit_owner("inspector")
    if _state.resource_controller is not None:
        _state.resource_controller.flush_autosave(force=True)
    for controller in tuple(_state.extra.get("linked_resource_controllers", {}).values()):
        controller.flush_autosave(force=True)
    DocumentRegistry.instance().detach_view("inspector")
    if _state.category == "texture" and _state.file_path:
        from .asset_resource_preview import invalidate_live_texture_preview
        invalidate_live_texture_preview(_state.file_path)
    if _state.category == "material" and _state.file_path:
        from .asset_resource_preview import invalidate_live_material_preview
        from Infernux.core.assets import AssetManager

        if not AssetManager.has_pending_local_revision(_state.file_path):
            invalidate_live_material_preview(_state.file_path)
    if _state.category == "audio":
        _stop_audio_preview()
    _state.reset()
    _sprite_state.reset()


def invalidate_asset(path: str):
    """Clear inspector cache if *path* is the currently inspected asset.

    Call this when an asset file is deleted so that re-creating a file with
    the same name performs a fresh load instead of reusing stale cached data.
    """
    if not _state.file_path or not path:
        return
    if same_path(_state.file_path, path):
        from Infernux.engine.interaction import ContinuousEditService

        ContinuousEditService.instance().commit_owner("inspector")
        for controller in tuple(
            _state.extra.get("linked_resource_controllers", {}).values()
        ):
            controller.flush_autosave(force=True)
        if _state.category in {"texture", "material"}:
            from .asset_resource_preview import (
                invalidate_live_material_preview,
                invalidate_live_texture_preview,
            )
            if _state.category == "texture":
                invalidate_live_texture_preview(_state.file_path)
            else:
                invalidate_live_material_preview(_state.file_path)
        _state.reset()


# ═══════════════════════════════════════════════════════════════════════════
# Shared rendering helpers
# ═══════════════════════════════════════════════════════════════════════════


def _render_header(ctx: InxGUIContext, cat_def: AssetCategoryDef,
                   state: _State):
    """Render the standard asset header: name, GUID, path, extra meta."""
    filename = os.path.basename(state.file_path)
    ctx.label(f"{t(cat_def.display_name)}: {filename}")

    # GUID — try .meta first, then serialized data (material stores it inside)
    guid = (state.meta or {}).get("guid", "")
    if not guid:
        cached = state.extra.get("cached_data")
        if cached:
            guid = cached.get("guid", "")
    if guid:
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label(t("asset.guid_label").format(guid=guid))
        ctx.pop_style_color(1)

    # Path
    ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
    ctx.label(t("asset.path_label").format(path=state.file_path))
    ctx.pop_style_color(1)

    # Extra metadata from .meta (e.g. file_size, extension for audio)
    if cat_def.extra_meta_keys and state.meta:
        for key in cat_def.extra_meta_keys:
            val = state.meta.get(key, "")
            if not val:
                continue
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            if key == "file_size":
                _render_file_size(ctx, val)
            else:
                ctx.label(f"{key.replace('_', ' ').title()}: {val}")
            ctx.pop_style_color(1)

    ctx.separator()


def _render_file_size(ctx: InxGUIContext, val):
    try:
        size = int(val)
        if size >= 1048576:
            ctx.label(t("asset.size_mb").format(size=f"{size / 1048576:.2f}"))
        elif size >= 1024:
            ctx.label(t("asset.size_kb").format(size=f"{size / 1024:.1f}"))
        else:
            ctx.label(t("asset.size_bytes").format(size=size))
    except (ValueError, TypeError):
        ctx.label(t("asset.size_bytes").format(size=val))


def _audio_engine():
    try:
        from Infernux.lib import AudioEngine

        engine = AudioEngine.instance()
        return engine if engine is not None and engine.is_initialized else None
    except (ImportError, RuntimeError, AttributeError):
        return None


def _stop_audio_preview() -> None:
    engine = _audio_engine()
    if engine is not None and hasattr(engine, "stop_preview"):
        engine.stop_preview()


def _render_audio_header(ctx: InxGUIContext, panel, state: _State) -> None:
    """Render shared-device playback controls and source metadata."""
    del panel
    engine = _audio_engine()
    can_preview = engine is not None and hasattr(engine, "play_preview")
    playing = bool(
        can_preview
        and hasattr(engine, "is_preview_playing")
        and engine.is_preview_playing(state.file_path)
    )

    if playing:
        label = t("asset.audio_stop_preview")
        callback = engine.stop_preview
    else:
        label = t("asset.audio_play_preview")
        callback = (lambda: engine.play_preview(state.file_path)) if can_preview else (lambda: None)
    if not can_preview:
        ctx.begin_disabled(True)
    ctx.button(f"{label}##audio_asset_preview", callback, width=132.0, height=32.0)
    ctx.record_semantic_item("button", label, can_preview, "asset.audio.preview")
    if not can_preview:
        ctx.end_disabled()

    duration = float(state.extra.get("duration", 0.0) or 0.0)
    sample_rate = int(state.extra.get("sample_rate", 0) or 0)
    channels = int(state.extra.get("channels", 0) or 0)
    sample_count = int(state.extra.get("sample_count", 0) or 0)
    if sample_rate > 0:
        ctx.text_wrapped(
            t("asset.audio_summary").format(
                duration=f"{duration:.2f}",
                sample_rate=sample_rate,
                channels=channels,
                samples=sample_count,
            )
        )
    ctx.separator()


def _render_import_fields(ctx: InxGUIContext, cat_def: AssetCategoryDef,
                          state: _State):
    """Auto-render editable import-settings fields from descriptors."""
    from .inspector_utils import render_compact_section_header, render_inspector_checkbox

    if render_compact_section_header(ctx, t("asset.import_settings"), level="secondary"):
        labels = [t(f.label) for f in cat_def.editable_fields]
        lw = max_label_w(ctx, labels)

        for fdef in cat_def.editable_fields:
            cur = getattr(state.settings, fdef.key)
            wid = f"##{fdef.key}"
            semantic_id = f"asset.{state.category}.import.{fdef.key}"

            if fdef.field_type == WidgetType.CHECKBOX:
                # Disable sRGB when texture_type is NORMAL_MAP
                disabled = (fdef.key == "srgb"
                            and hasattr(state.settings, "texture_type")
                            and state.settings.texture_type in {
                                TextureType.NORMAL_MAP, TextureType.DATA, TextureType.VECTOR_FIELD, TextureType.SDF,
                            })
                if disabled:
                    ctx.begin_disabled(True)
                new_val = render_inspector_checkbox(ctx, t(fdef.label), cur)
                ctx.record_semantic_item(
                    "checkbox", t(fdef.label), not disabled, semantic_id, bool(new_val),
                )
                if new_val != cur:
                    _edit_import_settings(
                        state,
                        fdef.key,
                        lambda settings, key=fdef.key, value=new_val: setattr(settings, key, value),
                        f"Set {t(fdef.label)}",
                    )
                if disabled:
                    ctx.end_disabled()

            elif fdef.field_type == WidgetType.COMBO:
                field_label(ctx, t(fdef.label), lw)
                display_labels = [t(e[0]) if e[0].startswith("asset.") else e[0] for e in fdef.combo_entries]
                values = [e[1] for e in fdef.combo_entries]
                try:
                    idx = values.index(cur)
                except ValueError:
                    idx = 0
                new_idx = ctx.combo(wid, idx, display_labels)
                display_value = display_labels[new_idx] if 0 <= new_idx < len(display_labels) else ""
                ctx.record_semantic_item(
                    "combo", f"{t(fdef.label)}: {display_value}", True, semantic_id,
                )
                if new_idx != idx:
                    selected_value = values[new_idx]

                    def _apply_combo(settings, key=fdef.key, value=selected_value):
                        setattr(settings, key, value)
                        if key == "texture_type" and hasattr(settings, "_sync_derived_fields"):
                            settings._sync_derived_fields()
                            if value is TextureType.SPRITE and not settings.sprite_frames:
                                metadata = state.meta or read_meta_file(state.file_path) or {}
                                width = int(metadata.get("width", 0) or 0)
                                height = int(metadata.get("height", 0) or 0)
                                if width <= 0 or height <= 0:
                                    raise ValueError(
                                        "sprite texture dimensions are unavailable; reimport the texture before changing its type"
                                    )
                                settings.sprite_frames = [
                                    SpriteFrame(
                                        name="frame_0",
                                        x=0,
                                        y=0,
                                        w=width,
                                        h=height,
                                    )
                                ]
                        elif key == "format" and value != TextureFormat.AUTO:
                            settings.compression = TextureCompression.NONE
                        elif key == "compression" and value != TextureCompression.NONE:
                            settings.format = TextureFormat.AUTO

                    _edit_import_settings(
                        state,
                        fdef.key,
                        _apply_combo,
                        f"Set {t(fdef.label)}",
                    )

            elif fdef.field_type == WidgetType.FLOAT:
                field_label(ctx, t(fdef.label), lw)
                speed = fdef.float_speed
                v_min = fdef.float_range[0] if fdef.float_range else 0.0
                v_max = fdef.float_range[1] if fdef.float_range else 0.0
                new_val = ctx.drag_float(wid, float(cur), speed, v_min, v_max)
                ctx.record_semantic_item(
                    "drag_float", f"{t(fdef.label)}: {new_val:g}", True, semantic_id,
                )
                if new_val != cur:
                    _edit_import_settings(
                        state,
                        fdef.key,
                        lambda settings, key=fdef.key, value=new_val: setattr(settings, key, value),
                        f"Set {t(fdef.label)}",
                    )


# ── Apply / Revert actions ─────────────────────────────────────────────


def _edit_import_settings(
    state: _State,
    edit_key: str,
    mutator: Callable[[Any], None],
    description: str,
    *,
    selection_after: Optional[Callable[[Any, Any, Any], Any]] = None,
) -> bool:
    """Apply one import draft edit through the global Action Journal."""
    controller = state.import_controller
    if controller is None or not state.document_id:
        return False

    return controller.apply_mutation(
        mutator,
        view_id="inspector",
        edit_key=edit_key,
        description=description,
        selection_after=selection_after,
    )


def _on_apply():
    if _state.settings is None or _state.exec_layer is None:
        return
    if not _state.document_id:
        return
    from Infernux.engine.interaction import DocumentRegistry

    DocumentRegistry.instance().request_save(_state.document_id)


def _on_revert():
    if _state.document_id:
        from Infernux.engine.interaction import DocumentRegistry

        DocumentRegistry.instance().request_discard(_state.document_id)
        return
    if _state.category == "texture" and _state.file_path:
        from .asset_resource_preview import invalidate_live_texture_preview
        invalidate_live_texture_preview(_state.file_path)
    _state.file_path = ""  # force full reload next frame


# ═══════════════════════════════════════════════════════════════════════════
# Mesh — info section (custom_header_fn)
# ═══════════════════════════════════════════════════════════════════════════


def _render_mesh_header(ctx: InxGUIContext, panel, state: _State):
    """Render mesh preview + mesh metadata in inspector header."""
    avail_w = max(32.0, ctx.get_content_region_avail_width() - 8.0)
    draw_h = min(max(avail_w, 120.0), 320.0)

    if not render_resource_preview_rect(ctx, panel, state.file_path, avail_w, draw_h,
                                        preview_size=int(draw_h),
                                        preserve_aspect=True,
                                        center=True):
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label("Model preview unavailable.")
        ctx.pop_style_color(1)
        ctx.separator()

    _render_mesh_info(ctx, panel, state)


def _render_prefab_preview(ctx: InxGUIContext, panel, state: _State):
    """Render prefab preview via standalone resource preview module."""
    avail_w = max(32.0, ctx.get_content_region_avail_width() - 8.0)
    draw_h = min(max(avail_w, 140.0), 360.0)

    if not render_resource_preview_rect(ctx, panel, state.file_path, avail_w, draw_h,
                                        preview_size=int(draw_h),
                                        preserve_aspect=True,
                                        center=True):
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label("Prefab preview unavailable.")
        ctx.pop_style_color(1)
    ctx.separator()


def _render_mesh_info(ctx: InxGUIContext, panel, state: _State):
    """Render mesh metadata summary (vertex count, submesh count, etc.)."""
    meta = state.meta
    if not meta:
        return

    from .inspector_utils import render_compact_section_header

    if render_compact_section_header(ctx, t("asset.mesh_info"), level="secondary"):
        labels = [
            t("asset.mesh_file"),
            t("asset.mesh_meshes"),
            t("asset.mesh_vertices"),
            t("asset.mesh_indices"),
            t("asset.mesh_material_slots"),
            t("asset.mesh_bones"),
            t("asset.mesh_anims"),
        ]
        lw = max_label_w(ctx, labels)

        filename = os.path.basename(state.file_path)
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        field_label(ctx, t("asset.mesh_file"), lw)
        ctx.label(filename)
        ctx.pop_style_color(1)

        mesh_count = meta.get("mesh_count", "?")
        vertex_count = meta.get("vertex_count", "?")
        index_count = meta.get("index_count", "?")
        mat_slots = meta.get("material_slot_count", "?")
        mat_names = meta.get("material_slots", "")
        bone_count = meta.get("bone_count", "?")
        bone_names = meta.get("bone_names_csv", "")
        anim_count = meta.get("animation_count", "?")
        anim_names = meta.get("animation_names_csv", "")

        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        field_label(ctx, t("asset.mesh_meshes"), lw)
        ctx.label(str(mesh_count))
        field_label(ctx, t("asset.mesh_vertices"), lw)
        ctx.label(str(vertex_count))
        field_label(ctx, t("asset.mesh_indices"), lw)
        ctx.label(str(index_count))
        field_label(ctx, t("asset.mesh_material_slots"), lw)
        ctx.label(str(mat_slots))
        if mat_names:
            field_label(ctx, t("asset.mesh_materials"), lw)
            ctx.label(str(mat_names))

        field_label(ctx, t("asset.mesh_bones"), lw)
        ctx.label(str(bone_count))
        if bone_names:
            field_label(ctx, t("asset.mesh_bone_names"), lw)
            ctx.label(str(bone_names))

        field_label(ctx, t("asset.mesh_anims"), lw)
        ctx.label(str(anim_count))
        if anim_names:
            field_label(ctx, t("asset.mesh_anim_names"), lw)
            ctx.label(str(anim_names))
        ctx.pop_style_color(1)

    ctx.separator()


# ═══════════════════════════════════════════════════════════════════════════
# Sprite slice editor (SPRITE-mode custom body)
# ═══════════════════════════════════════════════════════════════════════════

class _SpriteEditorState:
    """Persistent state for the sprite slice editor (one at a time)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.file_path: str = ""
        self.tex_w: int = 0
        self.tex_h: int = 0
        self.texture_id: int = 0
        self.slice_rows: int = 1
        self.slice_cols: int = 1
        self.drag_edge: str = ""  # "", "left", "right", "top", "bottom"
        self.drag_frame_id: str = ""
        self.zoom: float = 0.0
        self.pan_x: float = 0.0
        self.pan_y: float = 0.0
        self.view_initialized: bool = False
        self.pan_drag_button: int = -1
        self.drag_left: float = 0.0
        self.drag_right: float = 0.0
        self.drag_top: float = 0.0
        self.drag_bottom: float = 0.0


_sprite_state = _SpriteEditorState()


def _selected_sprite_frame_index(
    state: _State,
    settings: TextureImportSettings,
) -> int:
    from Infernux.engine.interaction import (
        SelectionDomain,
        SelectionService,
    )

    primary = SelectionService.instance().snapshot.primary
    if (
        primary is None
        or primary.domain is not SelectionDomain.ASSET_SUBRESOURCE
        or primary.sub_kind != "sprite_frame"
        or not same_path(primary.document_id, state.file_path)
    ):
        return -1
    index = next(
        (
            index
            for index, frame in enumerate(settings.sprite_frames)
            if frame.stable_id == primary.target_id
        ),
        -1,
    )
    if index < 0:
        selection = SelectionService.instance()
        selection.apply_snapshot(
            _sprite_selection_after(
                state,
                settings,
                selection.snapshot,
            ),
            reason="sprite_frame_removed",
            record_history=False,
        )
    return index


def _sprite_selection_after(
    state: _State,
    settings: TextureImportSettings,
    before,
):
    from Infernux.engine.interaction import (
        SelectionDomain,
        SelectionService,
        SelectionTarget,
    )

    valid_ids = {frame.stable_id for frame in settings.sprite_frames}

    def is_valid(target) -> bool:
        return not (
            target.domain is SelectionDomain.ASSET_SUBRESOURCE
            and target.sub_kind == "sprite_frame"
            and same_path(target.document_id, state.file_path)
            and target.target_id not in valid_ids
        )

    return SelectionService.reconciled_snapshot(
        before,
        is_valid,
        fallback=SelectionTarget.asset(state.file_path),
        fallback_owner_id="inspector",
    )


def _select_sprite_frame(state: _State, frame: Optional[SpriteFrame]) -> None:
    from Infernux.engine.interaction import SelectionService, SelectionTarget

    selection = SelectionService.instance()
    if frame is None:
        target = SelectionTarget.asset(state.file_path)
        reason = "sprite_frame_deselect"
    else:
        target = SelectionTarget.asset_subresource(
            state.file_path,
            frame.stable_id,
            sub_kind="sprite_frame",
        )
        reason = "sprite_frame_select"
    selection.select(
        target,
        owner_id="inspector",
        reason=reason,
        record_history=True,
    )


def _live_sprite_texture_id(ss: "_SpriteEditorState", cur_srgb, cur_filter) -> int:
    """Cheap per-frame lookup of the currently-published sprite preview descriptor."""
    try:
        from Infernux.engine.ui.editor_services import EditorServices
        svc = EditorServices.instance()
        native = svc.native_engine if svc else None
        if not native or not hasattr(native, "get_texture_preview_texture_id"):
            return 0
        filter_tag = cur_filter.name if cur_filter else "default"
        srgb_tag = "srgb" if cur_srgb else "linear"
        norm_path = resolved_path(ss.file_path)
        resource_key = f"spriteedit|sprite_preview|{srgb_tag}_{filter_tag}|{norm_path}"
        return int(native.get_texture_preview_texture_id(resource_key) or 0)
    except Exception as exc:
        Debug.log_warning(f"[SpriteEditor] live texture lookup failed: {exc}")
        return 0


def _ensure_sprite_texture(state: _State) -> bool:
    """Load the texture dimensions + ImGui texture ID for the sprite editor."""
    ss = _sprite_state
    # Track sRGB + filter_mode so we re-upload when either changes
    cur_srgb = getattr(state.settings, 'srgb', False)
    cur_filter = getattr(state.settings, 'filter_mode', None)
    meta = state.meta or read_meta_file(state.file_path) or {}
    expected_w = int(meta.get("width", 0) or 0)
    expected_h = int(meta.get("height", 0) or 0)
    dims_match = ((expected_w <= 0 or ss.tex_w == expected_w)
                  and (expected_h <= 0 or ss.tex_h == expected_h))
    if (ss.file_path == state.file_path and ss.tex_w > 0 and ss.texture_id != 0
            and getattr(ss, '_srgb', None) == cur_srgb
            and getattr(ss, '_filter', None) == cur_filter
            and dims_match):
        # Re-resolve the descriptor every frame — the C++ side may replace or
        # evict the ImGui texture, and binding a cached freed VkDescriptorSet
        # triggers validation errors and intermittent crashes.
        live_id = _live_sprite_texture_id(ss, cur_srgb, cur_filter)
        if live_id:
            ss.texture_id = live_id
            return True
        # Texture gone — fall through to a full re-query/reschedule.
    # Preserve slice grid when only sRGB/filter changed for the same file
    same_file = (ss.file_path == state.file_path)
    saved_rows = ss.slice_rows if same_file else 1
    saved_cols = ss.slice_cols if same_file else 1
    saved_zoom = ss.zoom if same_file else 0.0
    saved_pan_x = ss.pan_x if same_file else 0.0
    saved_pan_y = ss.pan_y if same_file else 0.0
    saved_view_initialized = ss.view_initialized if same_file else False
    ss.reset()
    ss.slice_rows = saved_rows
    ss.slice_cols = saved_cols
    ss.zoom = saved_zoom
    ss.pan_x = saved_pan_x
    ss.pan_y = saved_pan_y
    ss.view_initialized = saved_view_initialized
    ss.file_path = state.file_path
    ss._srgb = cur_srgb
    ss._filter = cur_filter

    try:
        from Infernux.engine.ui.editor_services import EditorServices
        svc = EditorServices.instance()
        native = svc.native_engine if svc else None
        if not native:
            Debug.log_warning("[SpriteEditor] No native engine via EditorServices")
            return False

        filter_tag = cur_filter.name if cur_filter else "default"
        srgb_tag = "srgb" if cur_srgb else "linear"
        norm_path = resolved_path(state.file_path)
        stamp = texture_stamp(norm_path, "sprite_edit_preview", filter_tag, srgb_tag)
        if stamp == 0:
            return False

        resource_key = f"spriteedit|sprite_preview|{srgb_tag}_{filter_tag}|{norm_path}"
        tex_id, tex_w, tex_h = query_or_schedule_texture(
            native,
            resource_key,
            norm_path,
            int(stamp),
            nearest=(cur_filter == FilterMode.POINT),
            srgb=bool(cur_srgb),
            pump=True,
        )

        source_w = int(meta.get("width", 0) or 0)
        source_h = int(meta.get("height", 0) or 0)
        if source_w <= 0:
            source_w = max(int(tex_w), 0)
        if source_h <= 0:
            source_h = max(int(tex_h), 0)

        ss.texture_id = int(tex_id)
        ss.tex_w = source_w
        ss.tex_h = source_h
    except Exception as exc:
        Debug.log_warning(f"[SpriteEditor] Preview task exception: {exc}")
        ss.texture_id = 0
    return ss.tex_w > 0


def _render_sprite_body(ctx: InxGUIContext, panel, state: _State):
    """Render the full SPRITE-mode inspector: import fields + slice editor."""
    from .inspector_utils import render_compact_section_header

    settings: TextureImportSettings = state.settings

    # ── Standard import fields (same as generic texture) ────────────────
    cat_def = _categories.get("texture")
    if cat_def and cat_def.editable_fields:
        _render_import_fields(ctx, cat_def, state)

    # ── Sprite slice editor ─────────────────────────────────────────────
    if not isinstance(settings, TextureImportSettings):
        return
    if settings.texture_type != TextureType.SPRITE:
        return

    ctx.separator()
    if not render_compact_section_header(ctx, t("sprite.slice_editor")):
        return

    if not _ensure_sprite_texture(state):
        ctx.push_style_color(ImGuiCol.Text, *Theme.WARNING_TEXT)
        ctx.label(t("sprite.cannot_load"))
        ctx.pop_style_color(1)
        return

    ss = _sprite_state

    # Derive rows/cols from existing sprite_frames when the UI state is at
    # the default 1×1 (e.g. first load of a previously-sliced texture).
    if ss.slice_rows == 1 and ss.slice_cols == 1 and settings.sprite_frames:
        frames = settings.sprite_frames
        tex_w = ss.tex_w if ss.tex_w > 0 else max((f.x + f.w for f in frames), default=0)
        tex_h = ss.tex_h if ss.tex_h > 0 else max((f.y + f.h for f in frames), default=0)
        if tex_w > 0 and tex_h > 0:
            v_divs, h_divs = _collect_dividers(settings, tex_w, tex_h)
            ss.slice_rows = max(1, len(h_divs) + 1)
            ss.slice_cols = max(1, len(v_divs) + 1)

    labels = [t("sprite.image_size"), t("sprite.rows"), t("sprite.cols")]
    lw = max_label_w(ctx, labels)

    # Image dimensions (read-only)
    field_label(ctx, t("sprite.image_size"), lw)
    ctx.label(f"{ss.tex_w} x {ss.tex_h}")

    # Auto-slice controls
    field_label(ctx, t("sprite.rows"), lw)
    ctx.set_next_item_width(120)
    ss.slice_rows = max(1, ctx.input_int("##sprite_rows", ss.slice_rows, 1, 1))
    ctx.record_semantic_item(
        "int_input", f"{t('sprite.rows')}: {ss.slice_rows}", True,
        "asset.texture.sprite.rows",
    )

    field_label(ctx, t("sprite.cols"), lw)
    ctx.set_next_item_width(120)
    ss.slice_cols = max(1, ctx.input_int("##sprite_cols", ss.slice_cols, 1, 1))
    ctx.record_semantic_item(
        "int_input", f"{t('sprite.cols')}: {ss.slice_cols}", True,
        "asset.texture.sprite.columns",
    )

    ctx.button(t("sprite.auto_slice"), lambda: _auto_slice_and_save(state, ss))
    ctx.record_semantic_item(
        "button", t("sprite.auto_slice"), True, "asset.texture.sprite.auto_slice",
    )
    ctx.dummy(0, 4)

    # ── Visual preview with divider lines ────────────────────────────────
    _render_sprite_preview(ctx, settings, ss, state)


def _auto_slice(settings: TextureImportSettings, ss: _SpriteEditorState):
    """Generate uniform sprite_frames from rows × cols grid."""
    rows, cols = ss.slice_rows, ss.slice_cols
    if rows < 1 or cols < 1 or ss.tex_w < 1 or ss.tex_h < 1:
        return
    fw = ss.tex_w // cols
    fh = ss.tex_h // rows
    existing = {
        (frame.x, frame.y, frame.w, frame.h): frame
        for frame in settings.sprite_frames
    }
    frames = []
    idx = 0
    for r in range(rows):
        for c in range(cols):
            rect = (c * fw, r * fh, fw, fh)
            previous = existing.get(rect)
            frames.append(
                SpriteFrame(
                    stable_id=(previous.stable_id if previous else uuid.uuid4().hex),
                    name=(previous.name if previous else f"frame_{idx}"),
                    x=rect[0],
                    y=rect[1],
                    w=rect[2],
                    h=rect[3],
                    pivot_x=(previous.pivot_x if previous else 0.5),
                    pivot_y=(previous.pivot_y if previous else 0.5),
                )
            )
            idx += 1
    settings.sprite_frames = frames


def _auto_slice_and_save(state: _State, ss: _SpriteEditorState):
    settings = state.settings
    if not isinstance(settings, TextureImportSettings):
        return
    if _edit_import_settings(
        state,
        "sprite_frames.auto_slice",
        lambda draft: _auto_slice(draft, ss),
        "Auto Slice Sprite",
        selection_after=lambda _old, new, before: _sprite_selection_after(
            state,
            new,
            before,
        ),
    ):
        _auto_save_sprite(state)


def _auto_save_sprite(state: _State):
    """Persist the current sprite import-settings document."""
    if not state.document_id:
        return False
    from Infernux.engine.interaction import DocumentRegistry

    return DocumentRegistry.instance().request_save(state.document_id).accepted


def _collect_dividers(settings: TextureImportSettings,
                      tex_w: int, tex_h: int):
    """Extract unique vertical (X) and horizontal (Y) divider positions
    from sprite_frames, excluding image edges (0 and max)."""
    v_set: set[int] = set()
    h_set: set[int] = set()
    for f in settings.sprite_frames:
        v_set.add(f.x)
        v_set.add(f.x + f.w)
        h_set.add(f.y)
        h_set.add(f.y + f.h)
    # Remove image boundary values — they are implicit
    v_set.discard(0)
    v_set.discard(tex_w)
    h_set.discard(0)
    h_set.discard(tex_h)
    return sorted(v_set), sorted(h_set)


def _fit_sprite_zoom(tex_w: int, tex_h: int, canvas_w: float, canvas_h: float) -> float:
    if tex_w <= 0 or tex_h <= 0 or canvas_w <= 0.0 or canvas_h <= 0.0:
        return 1.0
    return max(min(canvas_w / float(tex_w), canvas_h / float(tex_h)), 0.01)


def _clamp_sprite_pan(ss: _SpriteEditorState, canvas_w: float, canvas_h: float) -> None:
    image_w = ss.tex_w * ss.zoom
    image_h = ss.tex_h * ss.zoom

    if image_w <= canvas_w:
        ss.pan_x = (canvas_w - image_w) * 0.5
    else:
        ss.pan_x = max(canvas_w - image_w, min(0.0, ss.pan_x))

    if image_h <= canvas_h:
        ss.pan_y = (canvas_h - image_h) * 0.5
    else:
        ss.pan_y = max(canvas_h - image_h, min(0.0, ss.pan_y))


def _hit_test_sprite_frame_edge(frame: SpriteFrame, tex_x: float, tex_y: float,
                                threshold_tex: float) -> str:
    left = float(frame.x)
    right = float(frame.x + frame.w)
    top = float(frame.y)
    bottom = float(frame.y + frame.h)

    candidates: list[tuple[str, float]] = []
    if top - threshold_tex <= tex_y <= bottom + threshold_tex:
        dl = abs(tex_x - left)
        dr = abs(tex_x - right)
        if dl <= threshold_tex:
            candidates.append(("left", dl))
        if dr <= threshold_tex:
            candidates.append(("right", dr))
    if left - threshold_tex <= tex_x <= right + threshold_tex:
        dt = abs(tex_y - top)
        db = abs(tex_y - bottom)
        if dt <= threshold_tex:
            candidates.append(("top", dt))
        if db <= threshold_tex:
            candidates.append(("bottom", db))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[1])
    return candidates[0][0]


def _frame_contains_point(frame: SpriteFrame, tex_x: float, tex_y: float) -> bool:
    return (frame.x <= tex_x <= frame.x + frame.w
            and frame.y <= tex_y <= frame.y + frame.h)


def _begin_frame_edge_drag(ss: _SpriteEditorState,
                           frame: SpriteFrame, edge: str) -> None:
    ss.drag_frame_id = frame.stable_id
    ss.drag_edge = edge
    ss.drag_left = float(frame.x)
    ss.drag_right = float(frame.x + frame.w)
    ss.drag_top = float(frame.y)
    ss.drag_bottom = float(frame.y + frame.h)


def _commit_frame_edge_drag(ss: _SpriteEditorState, frame: SpriteFrame,
                            tex_w: int, tex_h: int) -> None:
    min_size = 1
    left = max(0, min(int(round(ss.drag_left)), tex_w - min_size))
    right = min(tex_w, max(int(round(ss.drag_right)), left + min_size))
    top = max(0, min(int(round(ss.drag_top)), tex_h - min_size))
    bottom = min(tex_h, max(int(round(ss.drag_bottom)), top + min_size))
    frame.x = left
    frame.y = top
    frame.w = max(min_size, right - left)
    frame.h = max(min_size, bottom - top)


def _apply_frame_edge_drag_preview(ss: _SpriteEditorState, tex_x: float, tex_y: float,
                                   tex_w: int, tex_h: int) -> None:
    min_size = 1.0
    snapped_x = float(max(0, min(tex_w, int(round(tex_x)))))
    snapped_y = float(max(0, min(tex_h, int(round(tex_y)))))
    if ss.drag_edge == "left":
        ss.drag_left = max(0.0, min(snapped_x, ss.drag_right - min_size))
    elif ss.drag_edge == "right":
        ss.drag_right = min(float(tex_w), max(snapped_x, ss.drag_left + min_size))
    elif ss.drag_edge == "top":
        ss.drag_top = max(0.0, min(snapped_y, ss.drag_bottom - min_size))
    elif ss.drag_edge == "bottom":
        ss.drag_bottom = min(float(tex_h), max(snapped_y, ss.drag_top + min_size))


def _render_sprite_preview(ctx: InxGUIContext, settings: TextureImportSettings,
                           ss: _SpriteEditorState, state: _State):
    """Draw a zoomable sprite viewport with per-frame edge editing."""
    avail_w = max(ctx.get_content_region_avail_width() - 8.0, 32.0)
    if avail_w < 32.0 or ss.tex_w <= 0 or ss.tex_h <= 0:
        return

    selected_frame = _selected_sprite_frame_index(state, settings)

    canvas_h = min(max(320.0, avail_w * 0.6), 720.0)
    fit_zoom_hint = _fit_sprite_zoom(ss.tex_w, ss.tex_h, avail_w, canvas_h)

    if ctx.button(f"{t('sprite.fit_view')}##sprite_fit_view"):
        ss.view_initialized = False
    ctx.same_line(0, 10)
    selected_label = t("sprite.preview_none")
    if 0 <= selected_frame < len(settings.sprite_frames):
        selected_label = f"#{selected_frame}"
    shown_zoom = ss.zoom if ss.zoom > 0.0 else fit_zoom_hint
    ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
    ctx.label(t("sprite.preview_status").format(
        zoom=f"{shown_zoom:.2f}",
        selected=selected_label,
    ))
    ctx.pop_style_color(1)

    if not ctx.begin_child("##sprite_zoom_canvas", 0, canvas_h, True):
        return
    try:
        canvas_w = max(ctx.get_content_region_avail_width(), 8.0)
        canvas_h_inner = max(ctx.get_content_region_avail_height(), 8.0)
        ctx.invisible_button("##sprite_canvas_hit", canvas_w, canvas_h_inner)
        view_min_x = ctx.get_item_rect_min_x()
        view_min_y = ctx.get_item_rect_min_y()
        view_max_x = ctx.get_item_rect_max_x()
        view_max_y = ctx.get_item_rect_max_y()
        hovered = ctx.is_item_hovered()

        fit_zoom = _fit_sprite_zoom(ss.tex_w, ss.tex_h, canvas_w, canvas_h_inner)
        if not ss.view_initialized or ss.zoom <= 0.0:
            ss.zoom = fit_zoom
            ss.pan_x = (canvas_w - ss.tex_w * ss.zoom) * 0.5
            ss.pan_y = (canvas_h_inner - ss.tex_h * ss.zoom) * 0.5
            ss.view_initialized = True

        ss.zoom = max(ss.zoom, fit_zoom)
        _clamp_sprite_pan(ss, canvas_w, canvas_h_inner)

        if ss.pan_drag_button >= 0:
            if ctx.is_mouse_button_down(ss.pan_drag_button):
                dx = ctx.get_mouse_drag_delta_x(ss.pan_drag_button)
                dy = ctx.get_mouse_drag_delta_y(ss.pan_drag_button)
                ss.pan_x += dx
                ss.pan_y += dy
                _clamp_sprite_pan(ss, canvas_w, canvas_h_inner)
                ctx.reset_mouse_drag_delta(ss.pan_drag_button)
            else:
                ss.pan_drag_button = -1

        ctx.draw_filled_rect(view_min_x, view_min_y, view_max_x, view_max_y,
                             0.035, 0.035, 0.04, 1.0)

        img_min_x = view_min_x + ss.pan_x
        img_min_y = view_min_y + ss.pan_y
        img_max_x = img_min_x + ss.tex_w * ss.zoom
        img_max_y = img_min_y + ss.tex_h * ss.zoom

        ctx.draw_rect(view_min_x, view_min_y, view_max_x, view_max_y,
                      1.0, 1.0, 1.0, 0.18, 1.0)

        hovered_frame = -1
        hovered_edge = ""
        tex_x = 0.0
        tex_y = 0.0
        threshold_tex = 4.0 / max(ss.zoom, 0.001)

        if hovered:
            local_mx = ctx.get_mouse_pos_x() - view_min_x
            local_my = ctx.get_mouse_pos_y() - view_min_y
            ctrl_down = (ctx.is_key_down(KEY_LEFT_CTRL)
                         or ctx.is_key_down(KEY_RIGHT_CTRL))

            if ctrl_down and abs(ctx.get_mouse_wheel_delta()) > 0.01:
                old_zoom = ss.zoom
                anchor_x = (local_mx - ss.pan_x) / max(old_zoom, 0.001)
                anchor_y = (local_my - ss.pan_y) / max(old_zoom, 0.001)
                new_zoom = ss.zoom * pow(1.08, ctx.get_mouse_wheel_delta())
                ss.zoom = max(fit_zoom, min(new_zoom, max(fit_zoom * 128.0, 128.0)))
                ss.pan_x = local_mx - anchor_x * ss.zoom
                ss.pan_y = local_my - anchor_y * ss.zoom
                _clamp_sprite_pan(ss, canvas_w, canvas_h_inner)
                img_min_x = view_min_x + ss.pan_x
                img_min_y = view_min_y + ss.pan_y
                img_max_x = img_min_x + ss.tex_w * ss.zoom
                img_max_y = img_min_y + ss.tex_h * ss.zoom

            tex_x = (ctx.get_mouse_pos_x() - img_min_x) / max(ss.zoom, 0.001)
            tex_y = (ctx.get_mouse_pos_y() - img_min_y) / max(ss.zoom, 0.001)

            if 0.0 <= tex_x <= ss.tex_w and 0.0 <= tex_y <= ss.tex_h:
                indices = list(range(len(settings.sprite_frames) - 1, -1, -1))
                if 0 <= selected_frame < len(settings.sprite_frames):
                    indices = [selected_frame] + [i for i in indices if i != selected_frame]

                for idx in indices:
                    edge = _hit_test_sprite_frame_edge(settings.sprite_frames[idx], tex_x, tex_y, threshold_tex)
                    if edge:
                        hovered_frame = idx
                        hovered_edge = edge
                        break

                if hovered_frame < 0:
                    for idx in indices:
                        if _frame_contains_point(settings.sprite_frames[idx], tex_x, tex_y):
                            hovered_frame = idx
                            break

            if hovered_edge in {"left", "right"}:
                ctx.set_mouse_cursor(4)
            elif hovered_edge in {"top", "bottom"}:
                ctx.set_mouse_cursor(3)
            elif ss.pan_drag_button >= 0:
                ctx.set_mouse_cursor(7)

            if ctx.is_mouse_button_clicked(1):
                ss.pan_drag_button = 1
            elif ctx.is_mouse_button_clicked(2):
                ss.pan_drag_button = 2

            if ctx.is_mouse_button_clicked(0):
                _select_sprite_frame(
                    state,
                    settings.sprite_frames[hovered_frame]
                    if hovered_frame >= 0
                    else None,
                )
                selected_frame = hovered_frame
                ss.drag_frame_id = ""
                ss.drag_edge = ""
                if hovered_frame >= 0 and hovered_edge:
                    _begin_frame_edge_drag(
                        ss,
                        settings.sprite_frames[hovered_frame],
                        hovered_edge,
                    )

        ctx.push_draw_list_clip_rect(view_min_x + 1.0, view_min_y + 1.0,
                                     view_max_x - 1.0, view_max_y - 1.0, True)
        try:
            if ss.texture_id:
                ctx.draw_image_rect(ss.texture_id, img_min_x, img_min_y, img_max_x, img_max_y)

            if ss.drag_frame_id and ss.drag_edge:
                if ctx.is_mouse_button_down(0):
                    drag_tex_x = (ctx.get_mouse_pos_x() - img_min_x) / max(ss.zoom, 0.001)
                    drag_tex_y = (ctx.get_mouse_pos_y() - img_min_y) / max(ss.zoom, 0.001)
                    _apply_frame_edge_drag_preview(ss, drag_tex_x, drag_tex_y, ss.tex_w, ss.tex_h)
                else:
                    frame_id = ss.drag_frame_id

                    def _commit_edge(draft, stable_id=frame_id):
                        frame = next(
                            (
                                value
                                for value in draft.sprite_frames
                                if value.stable_id == stable_id
                            ),
                            None,
                        )
                        if frame is None:
                            raise RuntimeError(
                                "sprite frame disappeared during edge drag"
                            )
                        _commit_frame_edge_drag(
                            ss,
                            frame,
                            ss.tex_w,
                            ss.tex_h,
                        )

                    changed = _edit_import_settings(
                        state,
                        f"sprite_frames.{frame_id}",
                        _commit_edge,
                        "Resize Sprite Frame",
                    )
                    ss.drag_frame_id = ""
                    ss.drag_edge = ""
                    if changed:
                        _auto_save_sprite(state)

            for idx, frame in enumerate(settings.sprite_frames):
                if frame.stable_id == ss.drag_frame_id and ss.drag_edge:
                    frame_left = ss.drag_left
                    frame_right = ss.drag_right
                    frame_top = ss.drag_top
                    frame_bottom = ss.drag_bottom
                else:
                    frame_left = float(frame.x)
                    frame_right = float(frame.x + frame.w)
                    frame_top = float(frame.y)
                    frame_bottom = float(frame.y + frame.h)

                fx0 = img_min_x + frame_left * ss.zoom
                fy0 = img_min_y + frame_top * ss.zoom
                fx1 = img_min_x + frame_right * ss.zoom
                fy1 = img_min_y + frame_bottom * ss.zoom

                is_selected = idx == selected_frame
                is_hovered = idx == hovered_frame

                if is_selected:
                    rgba = (0.22, 0.62, 0.98, 1.0)
                    thickness = 2.0
                elif is_hovered:
                    rgba = (0.97, 0.76, 0.24, 1.0)
                    thickness = 1.5
                else:
                    rgba = (1.0, 1.0, 1.0, 0.78)
                    thickness = 1.0

                ctx.draw_rect(fx0, fy0, fx1, fy1, rgba[0], rgba[1], rgba[2], rgba[3], thickness)

                if min((frame_right - frame_left) * ss.zoom,
                       (frame_bottom - frame_top) * ss.zoom) >= 18.0:
                    ctx.draw_text(fx0 + 3.0, fy0 + 2.0, str(idx), 1.0, 1.0, 1.0, 0.92)

                if is_selected:
                    handle_r = 2.5
                    cx = (fx0 + fx1) * 0.5
                    cy = (fy0 + fy1) * 0.5
                    ctx.draw_filled_circle(fx0, cy, handle_r, rgba[0], rgba[1], rgba[2], 0.95)
                    ctx.draw_filled_circle(fx1, cy, handle_r, rgba[0], rgba[1], rgba[2], 0.95)
                    ctx.draw_filled_circle(cx, fy0, handle_r, rgba[0], rgba[1], rgba[2], 0.95)
                    ctx.draw_filled_circle(cx, fy1, handle_r, rgba[0], rgba[1], rgba[2], 0.95)
        finally:
            ctx.pop_draw_list_clip_rect()

    finally:
        ctx.end_child()


# _render_frame_table removed — divider lines replace per-frame editing


# ═══════════════════════════════════════════════════════════════════════════
# Texture — preview section (custom_header_fn)
# ═══════════════════════════════════════════════════════════════════════════

_PREVIEW_MIN_H = 60.0
_PREVIEW_MAX_H = 800.0
_SPLITTER_H = 14.0


def _render_texture_preview(ctx: InxGUIContext, panel, state: _State):
    """Render texture preview via standalone resource preview module."""
    avail_w = max(32.0, ctx.get_content_region_avail_width() - 8.0)
    draw_h = min(max(float(state.extra.get("preview_height", 200.0)), _PREVIEW_MIN_H), _PREVIEW_MAX_H)

    if not render_resource_preview_rect(ctx, panel, state.file_path, avail_w, draw_h,
                                        preview_size=int(max(avail_w, draw_h)),
                                        texture_settings=state.settings,
                                        preserve_aspect=True,
                                        center=True):
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label("Texture preview unavailable.")
        ctx.pop_style_color(1)

    ctx.separator()


# ═══════════════════════════════════════════════════════════════════════════
# Shader — custom body (path editing + source preview)
# ═══════════════════════════════════════════════════════════════════════════


def _render_shader_body(ctx: InxGUIContext, panel, state: _State):
    info = state.settings  # ShaderAssetInfo

    # Shader type (read-only)
    lw = max_label_w(ctx, [t("asset.shader_type")])
    field_label(ctx, t("asset.shader_type"), lw)
    ctx.label(info.shader_type.capitalize() if info.shader_type else t("asset.shader_unknown"))
    ctx.separator()

    # ── Path editing ───────────────────────────────────────────────────
    from .inspector_utils import render_compact_section_header

    if render_compact_section_header(ctx, t("asset.shader_path"), level="secondary"):
        plw = max_label_w(ctx, [t("asset.shader_source_path")])
        field_label(ctx, t("asset.shader_source_path"), plw)
        new_path = ctx.text_input("##shader_src_path", info.source_path, 512)

        if new_path != info.source_path:
            ext = os.path.splitext(new_path)[1].lower()
            valid = {".vert", ".frag"}
            if ext not in valid:
                ctx.push_style_color(ImGuiCol.Text, *Theme.ERROR_TEXT)
                ctx.label(t("asset.shader_invalid_ext").format(ext=ext))
                ctx.pop_style_color(1)
            else:
                if not os.path.isfile(new_path):
                    ctx.push_style_color(ImGuiCol.Text, *Theme.WARNING_TEXT)
                    ctx.label(t("asset.file_not_exist_warning"))
                    ctx.pop_style_color(1)
                ctx.button(t("asset.apply_path_change"),
                           lambda np=new_path: _apply_shader_path(
                               state, np))

    ctx.separator()

    # ── Source preview ─────────────────────────────────────────────────
    if render_compact_section_header(ctx, t("asset.shader_source_preview"), default_open=False, level="secondary"):
        _render_shader_source(ctx, state.file_path)


def _render_font_body(ctx: InxGUIContext, panel, state: _State):
    info = state.settings
    lw = max_label_w(ctx, [t("asset.font_format"), t("asset.font_source_path")])
    field_label(ctx, t("asset.font_format"), lw)
    ctx.label(info.font_type.capitalize() if info.font_type else t("asset.font_unknown"))
    field_label(ctx, t("asset.font_source_path"), lw)
    ctx.label(info.source_path)


def _apply_shader_path(state: _State, new_path: str):
    info = state.settings
    old_path = info.source_path
    if state.exec_layer is None or not state.exec_layer.move_asset_path(new_path):
        from Infernux.debug import Debug

        Debug.log_error(f"Shader path change was rejected: {old_path} -> {new_path}")
        return False
    from Infernux.core.shader import Shader
    shader_id = os.path.splitext(os.path.basename(old_path))[0]
    Shader.invalidate(shader_id)
    info.source_path = new_path
    info.shader_type = ShaderAssetInfo.from_path(new_path).shader_type
    return True


def _render_shader_source(ctx: InxGUIContext, file_path: str):
    if not os.path.isfile(file_path):
        ctx.label(t("asset.file_not_found"))
        return
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[:40]
        text = "".join(lines)
        if len(lines) == 40:
            text += "\n" + t("asset.shader_truncated")
        ctx.push_style_color(ImGuiCol.Text, *Theme.SUCCESS_TEXT)
        ctx.label(text)
        ctx.pop_style_color(1)
    except OSError:
        ctx.label(t("asset.failed_read_source"))


# ═══════════════════════════════════════════════════════════════════════════
# Material — custom body (delegates to inspector_material)
# ═══════════════════════════════════════════════════════════════════════════


def _render_material_body(ctx: InxGUIContext, panel, state: _State):
    from . import inspector_material as mat_ui
    mat_ui.render_material_body(ctx, panel, state)


def _render_render_effect_body(ctx: InxGUIContext, panel, state: _State):
    del panel
    from .inspector_utils import render_compact_section_header
    from .render_effect_inspector import render_render_effect_parameters
    from Infernux.renderstack.render_effect import RenderEffect

    def linked_effect_controller(effect):
        from Infernux.engine.interaction import (
            DocumentKind,
            ensure_editable_resource_document,
        )

        file_path = str(getattr(effect, "file_path", "") or "")
        guid = str(getattr(effect, "guid", "") or "")
        if not file_path and not guid:
            return None
        controller = ensure_editable_resource_document(
            category="render_effect",
            document_kind=DocumentKind.RENDER_EFFECT,
            file_path=file_path,
            resource=effect,
            guid=guid,
            title=os.path.basename(file_path) or getattr(effect, "name", "Render Effect"),
            autosave_debounce_sec=0.5,
        )
        controllers = state.extra.setdefault("linked_resource_controllers", {})
        controllers[controller.document_id] = controller
        return controller

    resource = state.settings
    if isinstance(resource, RenderEffect):
        field_label(ctx, "Feature", max_label_w(ctx, ["Feature"]))
        ctx.label(resource.feature_type)
        ctx.separator()
        render_render_effect_parameters(
            ctx,
            resource,
            widget_prefix="asset_effect",
            resource_controller=state.resource_controller,
        )
        return

    # Effect groups are first-class editable assets. Their ordered references
    # are authored here, while each referenced effect keeps its own shared
    # material-like parameter document.
    from Infernux.core.asset_ref import RenderEffectRef
    from Infernux.engine.ui._inspector_references import (
        _resolve_guid_and_path,
        render_asset_reference_field,
    )

    def apply_group_document(document, edit_key: str, description: str) -> bool:
        return _apply_editable_resource_document(
            state,
            document,
            edit_key=edit_key,
            description=description,
        )

    def replace_entry(index: int, **changes) -> bool:
        document = resource.serialize_document()
        entry_document = dict(document["entries"][index])
        entry_document.update(changes)
        document["entries"][index] = entry_document
        return apply_group_document(
            document,
            f"entries.{index}",
            f"Edit Render Effect Group entry {index + 1}",
        )

    def assign_entry(index: int, payload) -> None:
        guid, path_hint = _resolve_guid_and_path(payload)
        if guid or path_hint:
            replace_entry(index, asset={"guid": guid, "path_hint": path_hint})

    def append_entry(payload) -> None:
        guid, path_hint = _resolve_guid_and_path(payload)
        if not guid and not path_hint:
            return
        document = resource.serialize_document()
        used_ids = {str(entry.get("entry_id", "")) for entry in document["entries"]}
        stem = os.path.splitext(os.path.basename(path_hint))[0].strip() or "Effect"
        entry_id = stem
        suffix = 2
        while entry_id in used_ids:
            entry_id = f"{stem} {suffix}"
            suffix += 1
        document["entries"].append(
            {
                "entry_id": entry_id,
                "asset": {"guid": guid, "path_hint": path_hint},
                "enabled": True,
                "overrides": {},
            }
        )
        apply_group_document(document, "entries.add", "Add Render Effect Group entry")

    if not render_compact_section_header(
        ctx,
        t("asset.render_effect_group_entries"),
        level="primary",
    ):
        return
    if not resource.entries:
        ctx.label(t("asset.render_effect_group_empty"))

    for index, entry in enumerate(resource.entries):
        label = entry.entry_id or f"Effect {index + 1}"
        if not render_compact_section_header(
            ctx,
            f"{label}##effect_group_{index}",
            level="secondary",
        ):
            continue
        labels = [
            t("asset.render_effect_group_enabled"),
            t("asset.render_effect_group_name"),
            t("asset.render_effect_group_asset"),
        ]
        label_width = max_label_w(ctx, labels)

        field_label(ctx, labels[0], label_width)
        enabled = bool(ctx.checkbox(f"##effect_group_enabled_{index}", entry.enabled))
        if enabled != entry.enabled:
            replace_entry(index, enabled=enabled)

        field_label(ctx, labels[1], label_width)
        entry_id = ctx.text_input(
            f"##effect_group_name_{index}",
            entry.entry_id,
            256,
        ).strip()
        other_ids = {
            other.entry_id
            for other_index, other in enumerate(resource.entries)
            if other_index != index
        }
        if entry_id and entry_id != entry.entry_id and entry_id not in other_ids:
            replace_entry(index, entry_id=entry_id)

        field_label(ctx, labels[2], label_width)
        render_asset_reference_field(
            ctx,
            f"##effect_group_asset_{index}",
            os.path.basename(entry.asset.path_hint) or entry.asset.guid or t("asset.none"),
            "RenderEffect",
            asset_type="RenderEffect",
            on_assign=lambda payload, _index=index: assign_entry(_index, payload),
            ping_path=entry.asset.path_hint or None,
            has_value=True,
            reference_value={
                "asset_type": "RenderEffect",
                "guid": entry.asset.guid,
                "path_hint": entry.asset.path_hint,
            },
            semantic_id=f"render_effect_group.entry.{index}.asset",
        )

        if index > 0 and ctx.button(f"{t('asset.move_up')}##effect_group_up_{index}"):
            document = resource.serialize_document()
            document["entries"][index - 1], document["entries"][index] = (
                document["entries"][index],
                document["entries"][index - 1],
            )
            apply_group_document(document, "entries.order", "Reorder Render Effect Group")
        if index > 0:
            ctx.same_line()
        if index + 1 < len(resource.entries) and ctx.button(
            f"{t('asset.move_down')}##effect_group_down_{index}"
        ):
            document = resource.serialize_document()
            document["entries"][index], document["entries"][index + 1] = (
                document["entries"][index + 1],
                document["entries"][index],
            )
            apply_group_document(document, "entries.order", "Reorder Render Effect Group")
        if index + 1 < len(resource.entries):
            ctx.same_line()
        if ctx.button(f"{t('asset.remove')}##effect_group_remove_{index}"):
            document = resource.serialize_document()
            del document["entries"][index]
            apply_group_document(document, f"entries.{index}.remove", "Remove Render Effect Group entry")
            return

        reference = RenderEffectRef(
            guid=entry.asset.guid,
            path_hint=entry.asset.path_hint,
        )
        effect = reference.resolve()
        if effect is None:
            ctx.label(entry.asset.path_hint or entry.asset.guid)
            continue
        if not entry.enabled:
            ctx.begin_disabled(True)
        controller = linked_effect_controller(effect)
        try:
            render_render_effect_parameters(
                ctx,
                effect,
                widget_prefix=f"asset_effect_group_{index}",
                resource_controller=controller,
            )
        finally:
            if not entry.enabled:
                ctx.end_disabled()
        if controller is not None:
            controller.flush_autosave()

    ctx.separator()
    field_label(
        ctx,
        t("asset.render_effect_group_add"),
        max_label_w(ctx, [t("asset.render_effect_group_add")]),
    )
    render_asset_reference_field(
        ctx,
        "##effect_group_add",
        t("asset.none"),
        "RenderEffect",
        asset_type="RenderEffect",
        on_assign=append_entry,
        has_value=False,
        reference_value=None,
        semantic_id="render_effect_group.add",
    )
