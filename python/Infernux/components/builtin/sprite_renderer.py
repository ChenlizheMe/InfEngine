"""
SpriteRenderer — renders a single frame from a sprite-sheet texture.

Wraps the C++ ``SpriteRenderer`` component (which inherits from
``MeshRenderer`` for rendering pipeline compatibility) and manages the
``sprite_unlit`` material, UV rect, and texture binding from Python.

This component is completely independent of the Python ``MeshRenderer``
wrapper — the two are parallel, same-level renderer types.
"""

from __future__ import annotations

from typing import List, Optional

from Infernux.components.builtin_component import BuiltinComponent, CppProperty
from Infernux.components.fields import FieldType
from Infernux.debug import Debug
from Infernux.engine.path_utils import lexical_path, portable_path, same_path


def _to_native_material(value):
    """Unwrap a Python Material wrapper to native InxMaterial."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    native = getattr(value, "_native", None) or getattr(value, "native", None)
    return native if native is not None else value


def _sprite_color_to_list(t):
    """C++ sprite_color tuple → [r,g,b,a] list for COLOR field."""
    return [t[0], t[1], t[2], t[3]]


def _list_to_sprite_color(lst):
    """[r,g,b,a] list → tuple for C++ sprite_color."""
    c = lst if lst and len(lst) >= 4 else [1, 1, 1, 1]
    return (c[0], c[1], c[2], c[3])


def _get_asset_database():
    """Match the Material Inspector's asset database lookup order."""
    try:
        from Infernux.engine.ui.editor_services import EditorServices
        adb = EditorServices.instance()._asset_database
        if adb:
            return adb
    except Exception:
        pass
    try:
        from Infernux.lib import AssetRegistry
        return AssetRegistry.instance().get_asset_database()
    except Exception:
        return None


class SpriteRenderer(BuiltinComponent):
    """Renders one frame of a sprite-sheet texture on a Quad mesh.

    Wraps the C++ ``SpriteRenderer`` component.  Properties delegate to C++
    for serialization; material management (shader, texture, UV) is handled
    in this Python wrapper.
    """

    _cpp_type_name = "SpriteRenderer"
    _component_category_ = "Rendering"
    _component_menu_path_ = "Rendering/Sprite Renderer"

    # ── CppProperty descriptors (scalar fields → C++) ───────────────
    #
    # IMPORTANT: Python descriptor names MUST match ``cpp_attr`` so that
    # ``_record_builtin_property(comp, cpp_attr, ...)`` → ``setattr``
    # goes through the CppProperty descriptor and reaches C++.

    sprite_guid = CppProperty(
        "sprite_guid",
        FieldType.STRING,
        default="",
    )

    frame_id = CppProperty(
        "frame_id",
        FieldType.STRING,
        default="",
        tooltip="Stable ID of the frame to display",
        visible_when=lambda comp: not comp._is_driven_by_animator(),
    )

    sprite_color = CppProperty(
        "sprite_color",
        FieldType.COLOR,
        default=None,
        tooltip="Tint color (RGBA)",
        get_converter=_sprite_color_to_list,
        set_converter=_list_to_sprite_color,
    )

    flip_x = CppProperty(
        "flip_x",
        FieldType.BOOL,
        default=False,
        tooltip="Flip sprite horizontally",
    )

    flip_y = CppProperty(
        "flip_y",
        FieldType.BOOL,
        default=False,
        tooltip="Flip sprite vertically",
    )

    casts_shadows = CppProperty(
        "casts_shadows",
        FieldType.BOOL,
        default=False,
        tooltip="Whether this renderer casts shadows",
    )

    receives_shadows = CppProperty(
        "receives_shadows",
        FieldType.BOOL,
        default=True,
        tooltip="Whether this renderer receives shadows",
    )

    # ── Private runtime state ───────────────────────────────────────

    _sprite_material = None
    _sprite_frames: list = []
    _sprite_frames_by_id: dict = {}
    _tex_w: int = 0
    _tex_h: int = 0
    _last_frame_id: str = ""
    _reported_missing_frame_id: str = ""
    _last_flip_x: bool = False
    _last_flip_y: bool = False
    _last_color: tuple = None
    _last_sprite: str = ""
    _material_ready: bool = False
    _instance_counter: int = 0  # fallback counter before a stable component_id exists

    # ── Binding hook ────────────────────────────────────────────────

    def _bind_cpp(self, cpp_component, game_object):
        super()._bind_cpp(cpp_component, game_object)
        # Reset per-instance state (class-level defaults are shared).
        self._sprite_frames = []
        self._sprite_frames_by_id = {}
        self._tex_w = 0
        self._tex_h = 0
        self._last_frame_id = ""
        self._reported_missing_frame_id = ""
        self._last_flip_x = False
        self._last_flip_y = False
        self._last_color = None
        self._last_sprite = ""
        self._material_ready = False
        self._sprite_material = None
        self._ensure_material()
        self._subscribe_asset_events()

    # ── Asset-change notification ───────────────────────────────────

    def _subscribe_asset_events(self):
        """Subscribe to typed asset mutations so texture reimport refreshes this renderer."""
        try:
            from Infernux.engine.interaction import AssetMutationService

            previous = getattr(self, "_asset_mutation_service", None)
            if previous is not None:
                previous.remove_component_listener(self._on_asset_changed)
            service = AssetMutationService.instance()
            self._asset_mutation_service = service
            if service is not None:
                service.add_component_listener(self._on_asset_changed)
        except (AttributeError, ImportError, RuntimeError, TypeError):
            pass

    def _unsubscribe_asset_events(self):
        """Release the mutation-service reference while this wrapper is live."""
        try:
            service = getattr(self, "_asset_mutation_service", None)
            if service is not None:
                service.remove_component_listener(self._on_asset_changed)
            self._asset_mutation_service = None
        except (AttributeError, ImportError, RuntimeError, TypeError):
            pass

    def _invalidate_native_binding(self):
        """Release wrapper-owned resources before a scene rebuild invalidates C++."""
        self._unsubscribe_asset_events()
        self._sprite_material = None
        self._material_ready = False
        self._sprite_frames = []
        self._sprite_frames_by_id = {}
        super()._invalidate_native_binding()

    def _on_asset_changed(self, change):
        """Called when any asset file is modified/deleted on disk."""
        from Infernux.engine.interaction import iter_asset_mutations

        guid = self.sprite
        if not guid:
            return
        try:
            adb = _get_asset_database()
            if not adb:
                return
            asset_path = adb.get_path_from_guid(guid)
            if not asset_path:
                return
            for mutation in iter_asset_mutations(change):
                file_path = mutation.path
                if same_path(file_path, asset_path) or same_path(file_path, asset_path + ".meta"):
                    Debug.log_internal("SpriteRenderer: asset changed, refreshing texture")
                    self._load_sprite_data()
                    self._apply_uv_rect()
                    self._apply_color()
                    break
        except Exception:
            pass

    # ── Scene-wide initialization ───────────────────────────────────

    @staticmethod
    def init_all_in_scene(scene=None):
        """Force wrapper creation for all SpriteRenderers in the scene.

        This ensures each SpriteRenderer gets its own material with the
        correct texture binding *before* the first render frame, avoiding
        the white-quad-until-clicked problem.
        """
        try:
            if scene is None:
                from Infernux.lib import SceneManager
                scene = SceneManager.instance().get_active_scene()
            if scene is None:
                return
            find_objects = getattr(scene, "find_objects_with_component", None)
            all_objects = (
                find_objects("SpriteRenderer")
                if callable(find_objects)
                else scene.get_all_objects()
            )
            count = 0
            for obj in all_objects:
                try:
                    cpp_comp = obj.get_component("SpriteRenderer")
                    if cpp_comp is not None:
                        # SpriteRenderer._get_or_create_wrapper(cpp_comp, obj)
                        w = SpriteRenderer._get_or_create_wrapper(cpp_comp, obj)
                        w._ensure_material()
                        count += 1
                except Exception:
                    pass
            if count > 0:
                Debug.log_internal(f"SpriteRenderer: initialized {count} instance(s)")
        except Exception as e:
            Debug.log_warning(f"SpriteRenderer.init_all_in_scene failed: {e}")

    # ── Sprite GUID (wraps C++ string, exposes as TEXTURE for Inspector) ──

    @property
    def sprite(self) -> str:
        """Asset GUID of the sprite texture."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.sprite_guid or ""
        return ""

    @sprite.setter
    def sprite(self, value):
        assignment = self._sprite_assignment_snapshot(value)
        self._restore_sprite_assignment(assignment)

    def _resolve_sprite_reference_candidate(self, candidate) -> str:
        if candidate is None or candidate == "":
            return ""
        supplied_guid = ""
        path = ""
        if isinstance(candidate, dict):
            supplied_guid = str(candidate.get("guid") or "").strip()
            path = str(
                candidate.get("path_hint") or candidate.get("path") or ""
            ).strip()
        else:
            supplied_guid = self._extract_guid(candidate)
            path = str(candidate or "").strip()
        guid = supplied_guid or self._resolve_texture_guid(path)
        if not guid:
            raise ValueError("sprite must reference an imported Texture asset")
        return guid

    def _sprite_assignment_snapshot(self, candidate) -> dict:
        guid = self._resolve_sprite_reference_candidate(candidate)
        if not guid:
            return {"sprite_guid": "", "frame_id": ""}
        requested_frame_id = (
            str(
                candidate.get("frame_id")
                or candidate.get("subresource_id")
                or ""
            ).strip()
            if isinstance(candidate, dict)
            else ""
        )
        adb = _get_asset_database()
        asset_path = adb.get_path_from_guid(guid) if adb else ""
        if not asset_path:
            raise ValueError("sprite texture is missing from the Asset Database")
        from Infernux.core.asset_types import (
            TextureType,
            read_texture_import_settings,
        )

        settings = read_texture_import_settings(asset_path)
        if settings.texture_type is not TextureType.SPRITE:
            raise ValueError("SpriteRenderer requires a texture imported as Sprite")
        if not settings.sprite_frames:
            raise ValueError("SpriteRenderer requires at least one persisted SpriteFrame")
        frame_ids = {frame.stable_id for frame in settings.sprite_frames}
        if requested_frame_id and requested_frame_id not in frame_ids:
            raise ValueError(
                f"SpriteFrame '{requested_frame_id}' does not belong to the selected texture"
            )
        current_frame_id = self.frame_id if self.sprite_guid == guid else ""
        frame_id = (
            requested_frame_id
            or (
                current_frame_id
                if current_frame_id in frame_ids
                else settings.sprite_frames[0].stable_id
            )
        )
        return {"sprite_guid": guid, "frame_id": frame_id}

    def _frame_assignment_snapshot(self, candidate) -> dict:
        frame_id = str(candidate or "").strip()
        guid = self.sprite_guid
        if not guid:
            raise ValueError("a SpriteFrame cannot be selected without a Sprite texture")
        return self._sprite_assignment_snapshot(
            {"guid": guid, "frame_id": frame_id}
        )

    def _restore_sprite_assignment(self, assignment: dict) -> None:
        if type(assignment) is not dict or set(assignment) != {
            "sprite_guid",
            "frame_id",
        }:
            raise ValueError("sprite assignment must use the complete current field set")
        guid = assignment["sprite_guid"]
        frame_id = assignment["frame_id"]
        if type(guid) is not str or type(frame_id) is not str:
            raise TypeError("sprite assignment identity fields must be strings")
        cpp = self._cpp_component
        if cpp is not None:
            cpp.sprite_guid = guid
            cpp.frame_id = frame_id
        self._load_sprite_data()
        self._apply_uv_rect()

    # ── Material access (direct to C++ SpriteRenderer) ──────────────

    @property
    def material(self):
        """The material on slot 0."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_material(0)
        return self._sprite_material

    @material.setter
    def material(self, value):
        cpp = self._cpp_component
        if cpp is not None:
            cpp.set_material(0, _to_native_material(value))

    @property
    def shared_material(self):
        return self.material

    @shared_material.setter
    def shared_material(self, value):
        self.material = value

    # ── Public API ──────────────────────────────────────────────────

    @property
    def sprite_frames(self) -> list:
        """Currently loaded sprite frames (read-only at runtime)."""
        return list(self._sprite_frames)

    @property
    def frame_count(self) -> int:
        return len(self._sprite_frames)

    # ── Custom Inspector rendering ──────────────────────────────────

    def render_inspector(self, ctx):
        """Custom Inspector: texture picker + material + color bar + CppProperty fields."""
        from Infernux.engine.ui.inspector_components import (
            render_builtin_via_setters, field_label, max_label_w,
            render_asset_reference_field, _record_builtin_property,
        )
        from Infernux.engine.ui.inspector_utils import _render_color_bar

        labels = ["Sprite", "Material", "Color", "Frame", "Flip X", "Flip Y"]
        lw = max_label_w(ctx, labels)

        # ── Sprite texture picker ──────────────────────────────
        guid = self.sprite
        display = "None (Texture)"
        sprite_path = ""
        if guid:
            try:
                adb = _get_asset_database()
                path = adb.get_path_from_guid(guid) if adb else ""
                if path:
                    import os
                    sprite_path = str(path)
                    display = os.path.basename(path)
            except Exception:
                display = guid[:8] + "…" if len(guid) > 8 else guid

        field_label(ctx, "Sprite", lw)
        from Infernux.engine.interaction import SnapshotPropertyTransaction

        game_object_id = int(getattr(self.game_object, "id", 0) or 0)
        component_id = int(getattr(self, "component_id", 0) or 0)
        sprite_transaction = SnapshotPropertyTransaction(
            f"SpriteRenderer:{game_object_id}:{component_id}:sprite",
            lambda: {
                "sprite_guid": self.sprite_guid,
                "frame_id": self.frame_id,
            },
            self._restore_sprite_assignment,
            description="Set Sprite",
            value_type="Texture",
            normalize=self._sprite_assignment_snapshot,
            clear_value="",
            mergeable=False,
        )
        render_asset_reference_field(
            ctx,
            "##sprite_texture",
            display,
            "Texture",
            ping_path=sprite_path or None,
            has_value=bool(guid),
            asset_type="Texture",
            reference_value={"guid": guid, "path_hint": sprite_path},
            transaction=sprite_transaction,
        )

        # ── Stable SpriteFrame picker ──────────────────────────
        if not self._is_driven_by_animator() and self._sprite_frames:
            frame_ids = [frame.stable_id for frame in self._sprite_frames]
            frame_labels = [
                str(frame.name or f"Frame {index}")
                for index, frame in enumerate(self._sprite_frames)
            ]
            current_frame_id = self.frame_id
            current_index = next(
                (
                    index
                    for index, frame_id in enumerate(frame_ids)
                    if frame_id == current_frame_id
                ),
                -1,
            )
            labels = frame_labels
            combo_index = current_index
            if current_index < 0:
                missing_label = (
                    f"Missing ({current_frame_id[:8]})"
                    if current_frame_id
                    else "None"
                )
                labels = [missing_label, *frame_labels]
                combo_index = 0

            field_label(ctx, "Frame", lw)
            selected_index = ctx.combo(
                "##sprite_frame",
                combo_index,
                labels,
                len(labels),
            )
            if selected_index != combo_index:
                source_index = (
                    selected_index
                    if current_index >= 0
                    else selected_index - 1
                )
                if 0 <= source_index < len(frame_ids):
                    SnapshotPropertyTransaction(
                        f"SpriteRenderer:{game_object_id}:{component_id}:frame",
                        lambda: {
                            "sprite_guid": self.sprite_guid,
                            "frame_id": self.frame_id,
                        },
                        self._restore_sprite_assignment,
                        description="Set Sprite Frame",
                        normalize=self._frame_assignment_snapshot,
                        mergeable=False,
                    ).commit_or_raise(frame_ids[source_index])

        # ── Material slot (supports custom materials) ──────────
        mat = self._get_material()
        mat_display = "Default (sprite_unlit)"
        if mat is not None and not self._is_default_material(mat):
            mat_name = getattr(mat, 'name', None) or getattr(mat, 'path', None)
            if mat_name:
                import os
                mat_display = os.path.basename(str(mat_name))
            else:
                mat_display = "Custom Material"
        field_label(ctx, "Material", lw)
        material_path = str(
            getattr(mat, "file_path", "")
            or getattr(mat, "path", "")
            or ""
        )
        has_custom_material = bool(
            mat is not None and not self._is_default_material(mat)
        )
        render_asset_reference_field(
            ctx,
            "##sprite_material",
            mat_display,
            "Material",
            asset_type="Material",
            on_assign=self._on_material_drop,
            on_clear=self._on_material_clear,
            ping_path=material_path or None,
            has_value=has_custom_material,
            reference_value=mat if has_custom_material else None,
        )

        # ── Color (Unity-style color bar, same as Material Inspector) ──
        c = self.sprite_color
        if c is None or len(c) < 4:
            c = [1.0, 1.0, 1.0, 1.0]
        field_label(ctx, "Color", lw)
        nr, ng, nb, na = _render_color_bar(
            ctx, "##sprite_color", c[0], c[1], c[2], c[3])
        if (nr, ng, nb, na) != (c[0], c[1], c[2], c[3]):
            _record_builtin_property(
                self, "sprite_color", c, [nr, ng, nb, na], "Set color")
            self._apply_color()

        # ── Remaining CppProperty fields (frame_id, flip_x, flip_y) ──
        render_builtin_via_setters(
            ctx, self, type(self),
            skip_fields={'sprite_guid', 'sprite_color', 'frame_id'})

        # ── Sync material state after Inspector edits ──────────
        self._sync_material_if_dirty()

    def _on_material_drop(self, payload):
        mat_path = str(payload)
        from Infernux.core.material import Material
        from Infernux.engine.ui._inspector_undo import _record_generic_component

        mat = Material.load(mat_path)
        cpp = self._cpp_component
        if mat is None or cpp is None:
            raise RuntimeError(f"SpriteRenderer material is unavailable: {mat_path}")
        old_document = self.serialize_document()
        cpp.set_material(0, mat._native)
        new_document = self.serialize_document()
        _record_generic_component(self, old_document, new_document)
        self._sprite_material = cpp.get_material(0)
        self._material_ready = True
        self._apply_texture_to_material()
        self._apply_uv_rect()
        self._apply_color()

    def _on_material_clear(self):
        """Reset to default sprite_unlit material."""
        cpp = self._cpp_component
        if cpp is None:
            return
        from Infernux.engine.ui._inspector_undo import _record_generic_component

        old_document = self.serialize_document()
        cpp.set_material(0, None)
        self._sprite_material = None
        self._material_ready = False
        self._ensure_material()
        new_document = self.serialize_document()
        _record_generic_component(self, old_document, new_document)
        self._sprite_material = cpp.get_material(0)
        self._material_ready = self._sprite_material is not None

    def _is_default_material(self, mat):
        """Check if a material is the auto-created sprite_unlit default."""
        try:
            frag = getattr(mat, 'frag_shader_name', None)
            # Material wrapper has no 'path' property — check native file_path
            native = getattr(mat, '_native', None) or getattr(mat, 'native', mat)
            path = getattr(native, 'file_path', '') or ''
            # Default material has no saved path and uses sprite_unlit
            return frag == 'Sprite Unlit' and not path
        except Exception:
            return False

    # ── Internals ───────────────────────────────────────────────────

    @staticmethod
    def _extract_guid(value) -> str:
        """Extract a GUID string from various input types."""
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        guid = getattr(value, "guid", None)
        if guid:
            return guid
        return str(value)

    @staticmethod
    def _resolve_texture_guid(path_str: str) -> str:
        """Resolve a file path to an asset GUID using the editor-aware asset DB."""
        if not path_str:
            return ""
        try:
            adb = _get_asset_database()
            if not adb:
                return ""

            existing_path = str(adb.get_path_from_guid(path_str) or "").strip()
            if existing_path:
                return path_str

            candidates = [path_str]
            normalized = portable_path(path_str)
            if normalized not in candidates:
                candidates.append(normalized)

            try:
                normpath = lexical_path(path_str)
                if normpath not in candidates:
                    candidates.append(normpath)
                slash_norm = portable_path(normpath)
                if slash_norm not in candidates:
                    candidates.append(slash_norm)
            except Exception:
                pass

            for candidate in candidates:
                guid = adb.get_guid_from_path(candidate)
                if guid:
                    return guid
        except Exception:
            pass
        return ""

    def sync_visual(self):
        """Public API: push the current C++ properties (frame, flip, color)
        to the material.  Called by external drivers like SpiritAnimator
        after they update ``frame_id`` from Python."""
        self._sync_material_if_dirty()

    def _is_driven_by_animator(self) -> bool:
        """Return True if a SpiritAnimator is attached to this GameObject."""
        try:
            go = self.game_object
            if go is None:
                return False
            from Infernux.components.spirit_animator import SpiritAnimator
            return go.get_component(SpiritAnimator) is not None
        except Exception:
            return False

    def _sync_material_if_dirty(self):
        """Push changed CppProperty values to the material (called per Inspector frame)."""
        cpp = self._cpp_component
        if cpp is None:
            return

        guid = self.sprite
        frame_id = cpp.frame_id
        fx = cpp.flip_x
        fy = cpp.flip_y
        try:
            c = tuple(cpp.sprite_color)
        except Exception:
            c = (1, 1, 1, 1)

        uv_dirty = (
            frame_id != self._last_frame_id
            or fx != self._last_flip_x
            or fy != self._last_flip_y
            or guid != self._last_sprite
        )
        color_dirty = c != self._last_color

        if guid != self._last_sprite:
            self._load_sprite_data()

        if uv_dirty:
            self._apply_uv_rect()
        if color_dirty:
            self._apply_color()

    def _get_material(self):
        """Get the Python Material wrapper for slot 0."""
        cpp = self._cpp_component
        if cpp is None:
            return None
        native = cpp.get_material(0)
        if native is None:
            return None
        from Infernux.core.material import Material
        if isinstance(native, Material):
            return native
        return Material.from_native(native)

    def _stable_default_material_name(self) -> str:
        """Return a stable runtime material key for this SpriteRenderer."""
        comp_id = int(getattr(self, "component_id", 0) or 0)
        if comp_id > 0:
            return f"SpriteUnlit_Default_{comp_id}"
        return ""

    @classmethod
    def _next_temp_material_name(cls) -> str:
        cls._instance_counter += 1
        return f"SpriteUnlit_Temp{cls._instance_counter}"

    def _stabilize_default_material_name(self, native_mat) -> None:
        """Keep auto-created default sprite materials stable across scene reloads."""
        desired_name = self._stable_default_material_name()
        if not desired_name or native_mat is None:
            return
        try:
            frag = getattr(native_mat, "frag_shader_name", None)
            path = getattr(native_mat, "file_path", "") or ""
            if frag == "Sprite Unlit" and not path and getattr(native_mat, "name", "") != desired_name:
                native_mat.name = desired_name
        except Exception:
            pass

    def _ensure_material(self):
        """Create the default sprite_unlit material if none is assigned."""
        cpp = self._cpp_component
        if cpp is None:
            return
        existing = cpp.get_material(0)
        if existing is not None:
            self._sprite_material = existing
            self._stabilize_default_material_name(existing)
            self._material_ready = True
            # Reload sprite data in case we're restoring from a scene
            self._load_sprite_data()
            self._apply_uv_rect()
            self._apply_color()
            return
        try:
            from Infernux.core.material import Material
            mat = Material.create_unlit()
            mat.frag_shader_name = "Sprite Unlit"
            # Opaque + alpha clipping: sprites are rendered in the opaque
            # queue with hard-edge alpha test (no blending artefacts).
            mat.surface_type = "opaque"
            mat.alpha_clip_enabled = True
            mat.alpha_clip_threshold = 0.5
            # Runtime sprite materials still need a unique renderer key, but it
            # must stay stable across Play/Stop for the same component.
            mat._native.name = self._stable_default_material_name() or self._next_temp_material_name()
            mat.set_color("baseColor", 1.0, 1.0, 1.0, 1.0)
            mat.set_vector4("uvRect", 0.0, 0.0, 1.0, 1.0)
            self._sprite_material = mat._native
            self._material_ready = True
            cpp.set_material(0, mat._native)
            # _load_sprite_data / _apply_uv_rect / _apply_color all use _get_material(),
            # which reads cpp.get_material(0). Running them *before* set_material is a
            # no-op (mat stays None), which caused invisible sprites until Inspector
            # selection ran _sync_material_if_dirty(). Apply after the slot is assigned.
            self._load_sprite_data()
            self._apply_uv_rect()
            self._apply_color()
        except Exception as e:
            Debug.log_warning(f"SpriteRenderer: failed to create material: {e}")

    def _load_sprite_data(self):
        """Load sprite frame list and texture dimensions from the asset .meta."""
        self._sprite_frames = []
        self._sprite_frames_by_id = {}
        self._tex_w = 0
        self._tex_h = 0
        self._reported_missing_frame_id = ""

        guid = self.sprite
        self._last_sprite = guid
        if not guid:
            self._apply_texture_to_material()
            return

        try:
            adb = _get_asset_database()
            if not adb:
                self._apply_texture_to_material()
                return
            asset_path = adb.get_path_from_guid(guid)
            if not asset_path:
                self._apply_texture_to_material()
                return

            # Runtime packages publish authoring metadata through the native
            # AssetDatabase and deliberately contain no .meta files. Prefer
            # that immutable snapshot; the sidecar remains an editor fallback.
            from Infernux.core.asset_types import read_asset_metadata
            meta = read_asset_metadata(asset_path, guid=guid)
            if meta is None:
                self._apply_texture_to_material()
                return

            self._tex_w = int(meta.get("width", 0))
            self._tex_h = int(meta.get("height", 0))

            # Only load sprite frames if texture_type is "sprite"
            tex_type = meta.get("texture_type", "default")
            if tex_type == "sprite":
                raw_frames = meta.get("sprite_frames", [])
                if type(raw_frames) is not list:
                    raise TypeError("texture sprite_frames must be an array")

                from Infernux.core.asset_types import SpriteFrame
                self._sprite_frames = [
                    SpriteFrame.from_dict(f)
                    for f in raw_frames
                ]
                self._sprite_frames_by_id = {
                    frame.stable_id: frame for frame in self._sprite_frames
                }
                if len(self._sprite_frames_by_id) != len(self._sprite_frames):
                    raise ValueError("texture sprite frame stable_id values must be unique")

            # Assign the texture to the material (if it supports texSampler)
            self._apply_texture_to_material()
        except Exception as e:
            Debug.log_warning(f"SpriteRenderer: failed to load sprite data: {e}")

    def _apply_texture_to_material(self):
        """Pass the sprite texture to texSampler (sprite_unlit shader slot)."""
        guid = self.sprite
        mat = self._get_material()
        if mat is None:
            return
        try:
            # sprite_unlit.frag uses "texSampler" — set it directly without
            # has_property() since programmatic materials may not have the
            # property registered in m_properties until first set_texture call.
            if guid:
                mat.set_texture("texSampler", guid)
            else:
                mat.clear_texture("texSampler")
        except Exception as e:
            Debug.log_warning(f"SpriteRenderer: _apply_texture_to_material failed: {e}")

    def _apply_uv_rect(self):
        """Compute and apply UV rect and display scale from the current frame."""
        cpp = self._cpp_component
        if cpp is None:
            return

        frame_id = cpp.frame_id
        fx = cpp.flip_x
        fy = cpp.flip_y
        self._last_frame_id = frame_id
        self._last_flip_x = fx
        self._last_flip_y = fy
        self._last_sprite = self.sprite

        mat = self._get_material()
        if mat is None:
            return

        # Default: full texture
        u, v, su, sv = 0.0, 0.0, 1.0, 1.0
        ds_x, ds_y = 1.0, 1.0  # displayScale for aspect-fit centering

        if self._sprite_frames and self._tex_w > 0 and self._tex_h > 0:
            frame = (
                self._sprite_frames[0]
                if not frame_id
                else self._sprite_frames_by_id.get(frame_id)
            )
            if frame is None:
                if self._reported_missing_frame_id != frame_id:
                    Debug.log_error(
                        "SpriteRenderer: frame ID "
                        f"'{frame_id}' is missing from texture '{self.sprite}'"
                    )
                    self._reported_missing_frame_id = frame_id
                u, v, su, sv = 0.0, 0.0, 0.0, 0.0
                ds_x, ds_y = 0.0, 0.0
                frame = None
            else:
                self._reported_missing_frame_id = ""
        else:
            frame = None

        if frame is not None:
            tw, th = float(self._tex_w), float(self._tex_h)
            u = frame.x / tw
            v = frame.y / th
            su = frame.w / tw
            sv = frame.h / th
            fw = float(frame.w) if frame.w > 0 else 1.0
            fh = float(frame.h) if frame.h > 0 else 1.0
            max_dim = max(fw, fh)
            ds_x = fw / max_dim
            ds_y = fh / max_dim

        if fx:
            u = u + su
            su = -su
        if not fy:
            # Default (flip_y=False): invert V to correct Vulkan UV orientation
            v = v + sv
            sv = -sv

        try:
            mat.set_vector4("uvRect", u, v, su, sv)
        except Exception:
            pass

        # displayScale tells the shader what fraction of the quad the sprite
        # occupies.  The shader centers the image and discards outside pixels.
        try:
            mat.set_vector4("displayScale", ds_x, ds_y, 0.0, 0.0)
        except Exception:
            pass

    def _apply_color(self):
        """Apply tint color to the material."""
        cpp = self._cpp_component
        if cpp is None:
            return

        try:
            c = cpp.sprite_color
            c = (c[0], c[1], c[2], c[3])
        except Exception:
            c = (1, 1, 1, 1)

        self._last_color = c
        mat = self._get_material()
        if mat is None:
            return

        try:
            mat.set_color("baseColor", c[0], c[1], c[2], c[3])
        except Exception:
            pass
