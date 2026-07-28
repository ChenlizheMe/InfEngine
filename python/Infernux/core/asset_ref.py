"""
Asset reference types for InxComponent serialized fields.

Provides GUID-based asset references that lazily resolve to loaded assets.
They mirror the C++ ``AssetRef<T>`` pattern and integrate with the Inspector.

All asset ref types inherit from ``AssetRefBase`` and override ``_do_resolve``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from Infernux.debug import Debug

_log = logging.getLogger("Infernux.ref")


def _get_asset_database():
    """Return the C++ AssetDatabase, trying AssetManager first then engine."""
    try:
        from Infernux.core.assets import AssetManager
        if AssetManager._asset_database is not None:
            return AssetManager._asset_database
    except ImportError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        pass
    try:
        from Infernux.engine.play_mode import PlayModeManager
        pm = PlayModeManager.instance()
        if pm and pm._asset_database is not None:
            return pm._asset_database
    except ImportError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        pass
    return None


class AssetRefBase:
    """Base class for GUID-based asset references.

    Stores a GUID string and lazily resolves to the loaded asset via
    ``AssetManager``.
    """

    __slots__ = ("_guid", "_cached", "_path_hint")

    def __init__(self, guid: str = "", path_hint: str = ""):
        self._guid = guid
        self._cached = None
        self._path_hint = path_hint  # optional human-readable path for display

    # ── GUID ───────────────────────────────────────────────────────────

    @property
    def guid(self) -> str:
        return self._guid

    @guid.setter
    def guid(self, value: str):
        if value != self._guid:
            self._guid = value
            self._cached = None

    @property
    def path_hint(self) -> str:
        """Best-effort human-readable path (may be stale)."""
        return self._path_hint

    @path_hint.setter
    def path_hint(self, value: str):
        self._path_hint = value

    # ── Resolution ─────────────────────────────────────────────────────

    def resolve(self):
        """Attempt to resolve the GUID to a loaded asset.

        Returns the asset object, or ``None`` if not found.
        Subclasses override ``_do_resolve``.
        """
        if self._cached is not None:
            return self._cached
        if not self._guid:
            return None
        self._cached = self._do_resolve()
        return self._cached

    def _do_resolve(self):
        """Override in subclass to call the appropriate AssetManager loader."""
        return None

    def invalidate(self):
        """Clear the cached resolved object (GUID is kept)."""
        self._cached = None

    # ── Serialization ──────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"guid": self._guid, "path_hint": self._path_hint}

    @classmethod
    def from_dict(cls, d: dict) -> "AssetRefBase":
        if type(d) is not dict or set(d) != {"guid", "path_hint"}:
            raise ValueError("asset reference must use the complete current field set")
        if type(d["guid"]) is not str or type(d["path_hint"]) is not str:
            raise TypeError("asset reference values must be strings")
        return cls(guid=d["guid"], path_hint=d["path_hint"])

    # ── Display ────────────────────────────────────────────────────────

    @property
    def display_name(self) -> str:
        if self._path_hint:
            return os.path.basename(self._path_hint)
        if self._guid:
            return f"GUID:{self._guid[:8]}…"
        return "None"

    @property
    def is_missing(self) -> bool:
        """True if we have a GUID but resolution failed."""
        if not self._guid:
            return False
        return self.resolve() is None

    def __bool__(self):
        return bool(self._guid)

    def __eq__(self, other):
        if isinstance(other, AssetRefBase):
            return self._guid == other._guid
        return NotImplemented

    def __hash__(self):
        return hash(self._guid)

    def __repr__(self):
        cls_name = type(self).__name__
        return f"{cls_name}(guid='{self._guid}', path_hint='{self._path_hint}')"


class TextureRef(AssetRefBase):
    """Reference to a Texture asset."""

    def _do_resolve(self):
        from Infernux.core.assets import AssetManager
        from Infernux.core.texture import Texture
        return AssetManager.load_by_guid(self._guid, asset_type=Texture)


class ShaderRef(AssetRefBase):
    """Reference to a Shader asset (resolves to ShaderAssetInfo)."""

    def _do_resolve(self):
        from Infernux.core.assets import AssetManager
        from Infernux.core.shader import Shader
        return AssetManager.load_by_guid(self._guid, asset_type=Shader)


class AudioClipRef(AssetRefBase):
    """Reference to an AudioClip asset."""

    def _do_resolve(self):
        from Infernux.core.assets import AssetManager
        from Infernux.core.audio_clip import AudioClip
        return AssetManager.load_by_guid(self._guid, asset_type=AudioClip)


class PhysicMaterialRef(AssetRefBase):
    """Reference to a native PhysicMaterial asset."""

    def _do_resolve(self):
        from Infernux.core.physic_material import PhysicMaterial
        return PhysicMaterial.load_by_guid(self._guid)


class AnimStateMachineRef(AssetRefBase):
    """Reference to an AnimStateMachine (.animfsm) asset."""

    def _do_resolve(self):
        if not self._guid and self._path_hint:
            from Infernux.core.anim_state_machine import AnimStateMachine
            return AnimStateMachine.load(self._path_hint)
        db = _get_asset_database()
        if db and self._guid:
            try:
                from Infernux.core.animation_clip3d import resolve_disk_path_for_guid_string
                path = resolve_disk_path_for_guid_string(db, self._guid) or db.get_path_from_guid(self._guid)
                if path:
                    from Infernux.core.anim_state_machine import AnimStateMachine
                    return AnimStateMachine.load(path)
            except Exception:
                pass
        return None


class ParticleGraphRef(AssetRefBase):
    """Reference to a compiled ParticleGraph or ParticleScript asset."""

    def _do_resolve(self):
        path = self._path_hint
        db = _get_asset_database()
        if self._guid:
            path = (db.get_path_from_guid(self._guid) if db else "") or path
        if not path:
            return None
        try:
            if path.lower().endswith(".particle.py"):
                from pathlib import Path
                from Infernux.particle.script import ParticleScriptCompiler

                return ParticleScriptCompiler().parse(
                    Path(path).read_text(encoding="utf-8"),
                    source_name=path,
                )
            from Infernux.particle.asset import ParticleGraphAsset

            return ParticleGraphAsset.load(path)
        except (OSError, TypeError, ValueError):
            return None


class RenderEffectRef(AssetRefBase):
    """Reference to a ``.effect`` asset or reusable effect group source."""

    def __init__(self, effect=None, *, guid: str = "", path_hint: str = ""):
        if effect is None:
            super().__init__(guid=guid, path_hint=path_hint)
            return
        effect_guid = str(getattr(effect, "guid", "") or guid)
        effect_path = str(getattr(effect, "file_path", "") or path_hint)
        super().__init__(guid=effect_guid, path_hint=effect_path)
        self._cached = effect

    def resolve(self):
        if self._cached is not None:
            return self._cached
        self._cached = self._do_resolve()
        return self._cached

    def _do_resolve(self):
        from Infernux.core.assets import AssetManager
        from Infernux.renderstack.render_effect import RenderEffect

        if self._guid:
            effect = AssetManager.load_by_guid(self._guid, asset_type=RenderEffect)
            if effect is not None:
                return effect
        if self._path_hint:
            return AssetManager.load(self._path_hint, asset_type=RenderEffect)
        return self._cached

    def __bool__(self):
        return bool(self._guid or self._path_hint or self._cached is not None)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        effect = self.resolve()
        if effect is None:
            raise AttributeError(name)
        return getattr(effect, name)

    def __copy__(self):
        copied = RenderEffectRef(guid=self._guid, path_hint=self._path_hint)
        copied._cached = self._cached
        return copied

    def __deepcopy__(self, memo):
        return self.__copy__()


class TimelineFSMRef(AssetRefBase):
    """Reference to a Timeline state machine (.timelinefsm) asset.

    Stored as an AnimStateMachine with ``mode == 'timeline'`` (nodes are all
    timeline states); resolved the same way as an .animfsm.
    """

    def _do_resolve(self):
        if not self._guid and self._path_hint:
            from Infernux.core.anim_state_machine import AnimStateMachine
            return AnimStateMachine.load(self._path_hint)
        db = _get_asset_database()
        if db and self._guid:
            try:
                from Infernux.core.animation_clip3d import resolve_disk_path_for_guid_string
                path = resolve_disk_path_for_guid_string(db, self._guid) or db.get_path_from_guid(self._guid)
                if path:
                    from Infernux.core.anim_state_machine import AnimStateMachine
                    return AnimStateMachine.load(path)
            except Exception:
                pass
        return None


class AnimationClipRef(AssetRefBase):
    """Reference to an AnimationClip (.animclip2d) asset."""

    def _do_resolve(self):
        if not self._guid and self._path_hint:
            from Infernux.core.animation_clip import AnimationClip
            return AnimationClip.load(self._path_hint)
        db = _get_asset_database()
        if db and self._guid:
            try:
                path = db.get_path_from_guid(self._guid)
                if path:
                    from Infernux.core.animation_clip import AnimationClip
                    return AnimationClip.load(path)
            except Exception:
                pass
        return None


class AnimationClip3DRef(AssetRefBase):
    """Reference to an AnimationClip3D (.animclip3d) asset."""

    def _do_resolve(self):
        if not self._guid and self._path_hint:
            from Infernux.core.animation_clip3d import AnimationClip3D
            return AnimationClip3D.load(self._path_hint)
        db = _get_asset_database()
        if db and self._guid:
            try:
                path = db.get_path_from_guid(self._guid)
                if path:
                    from Infernux.core.animation_clip3d import AnimationClip3D
                    return AnimationClip3D.load(path)
            except Exception:
                pass
        return None


class AnimationTimelineRef(AssetRefBase):
    """Reference to an AnimationTimeline (.animtimeline) asset."""

    def _do_resolve(self):
        if not self._guid and self._path_hint:
            from Infernux.core.animation_timeline import AnimationTimeline
            return AnimationTimeline.load(self._path_hint)
        db = _get_asset_database()
        if db and self._guid:
            try:
                path = db.get_path_from_guid(self._guid)
                if path:
                    from Infernux.core.animation_timeline import AnimationTimeline
                    return AnimationTimeline.load(path)
            except Exception:
                pass
        return None


# ---------------------------------------------------------------------------
# Asset type registry — central config for all ASSET-typed serialized fields.
#
# Each entry maps an *asset_type* name (used in ``serialized_field()``) to:
#   ref_class   — AssetRefBase subclass for wrapping/resolving the GUID
#   drag_type   — ImGui drag-drop payload type emitted by the Project panel
#   extensions  — file globs for the asset picker
#   display     — human-readable type label for the inspector object field
#   prefix      — short id prefix for the inspector widget
# ---------------------------------------------------------------------------

_ASSET_TYPE_REGISTRY: dict = {}


def _ensure_registry():
    """Populate the registry on first access (avoids import-time cycles)."""
    if _ASSET_TYPE_REGISTRY:
        return
    _ASSET_TYPE_REGISTRY.update({
        "AudioClip": {
            "ref_class":  AudioClipRef,
            "drag_type":  "AUDIO_FILE",
            "extensions": ("*.wav", "*.ogg"),
            "display":    "AudioClip",
            "prefix":     "aud",
        },
        "PhysicMaterial": {
            "ref_class":  PhysicMaterialRef,
            "drag_type":  "PHYSIC_MATERIAL_FILE",
            "extensions": ("*.physicMaterial",),
            "display":    "PhysicMaterial",
            "prefix":     "pmat",
        },
        "AnimStateMachine": {
            "ref_class":  AnimStateMachineRef,
            "drag_type":  "ANIMFSM_FILE",
            "extensions": ("*.animfsm",),
            "display":    "AnimFSM",
            "prefix":     "fsm",
        },
        "ParticleGraph": {
            "ref_class":  ParticleGraphRef,
            "drag_type":  "PARTICLE_GRAPH_FILE",
            "extensions": ("*.particlegraph", "*.particle.py"),
            "display":    "Particle Graph",
            "prefix":     "particle",
        },
        "RenderEffect": {
            "ref_class":  RenderEffectRef,
            "drag_type":  "RENDER_EFFECT_FILE",
            "extensions": ("*.effect", "*.effectgroup"),
            "display":    "Render Effect / Group",
            "prefix":     "effect",
        },
        "AnimationClip": {
            "ref_class":  AnimationClipRef,
            "drag_type":  "ANIMCLIP_FILE",
            "extensions": ("*.animclip2d",),
            "display":    "AnimClip2D",
            "prefix":     "aclip",
        },
        "AnimationClip3D": {
            "ref_class":  AnimationClip3DRef,
            "drag_type":  "ANIMCLIP3D_FILE",
            "extensions": ("*.animclip3d",),
            "display":    "AnimClip3D",
            "prefix":     "aclip3d",
        },
        "AnimationTimeline": {
            "ref_class":  AnimationTimelineRef,
            "drag_type":  "ANIMTIMELINE_FILE",
            "extensions": ("*.animtimeline",),
            "display":    "Timeline",
            "prefix":     "atl",
        },
        "TimelineFSM": {
            "ref_class":  TimelineFSMRef,
            "drag_type":  "TIMELINEFSM_FILE",
            "extensions": ("*.timelinefsm",),
            "display":    "TimelineFSM",
            "prefix":     "tlfsm",
        },
    })


def register_asset_type(asset_type: str, *, ref_class, drag_type: str, extensions: tuple,
                        display: str, prefix: str):
    """Register a new asset type for use with ``FieldType.ASSET``.

    Call this early (e.g. in a plug-in's ``__init__``) to make the type
    available to serialization and the inspector.
    """
    _ensure_registry()
    _ASSET_TYPE_REGISTRY[asset_type] = {
        "ref_class":  ref_class,
        "drag_type":  drag_type,
        "extensions": extensions,
        "display":    display,
        "prefix":     prefix,
    }


def get_asset_type_config(asset_type: str) -> Optional[dict]:
    """Return the registry entry for *asset_type*, or ``None``."""
    _ensure_registry()
    return _ASSET_TYPE_REGISTRY.get(asset_type)


def get_all_asset_type_configs() -> dict:
    """Return the full registry dict (asset_type → config)."""
    _ensure_registry()
    return _ASSET_TYPE_REGISTRY


def get_asset_type_for_ref(ref) -> Optional[str]:
    """Given an AssetRefBase instance, return its asset_type name."""
    _ensure_registry()
    ref_cls = type(ref)
    for name, cfg in _ASSET_TYPE_REGISTRY.items():
        if cfg["ref_class"] is ref_cls:
            return name
    return None


class MaterialRef(AssetRefBase):
    """GUID-based reference to a Material asset.

    Lazily loads the Material through ``AssetManager.load_by_guid`` on
    first access and caches the result.  Falls back to path-hint resolution
    and direct AssetDatabase lookup when the primary GUID path fails.

    Supports attribute forwarding to the underlying Material::

        mat_ref = MaterialRef(guid="abc123")
        mat_ref.set_color("_BaseColor", (1, 0, 0, 1))  # forwards to Material
    """

    def __init__(self, material=None, *, guid: str = "", path_hint: str = ""):
        if material is not None:
            extracted_guid = self._extract_guid(material)
            native = getattr(material, "native", material)
            hint = path_hint or getattr(native, "file_path", "") or ""
            super().__init__(guid=extracted_guid, path_hint=hint)
            self._cached = material
        else:
            super().__init__(guid=guid, path_hint=path_hint)

    @staticmethod
    def _extract_guid(material) -> str:
        """Extract the GUID for a Material wrapper or native InxMaterial."""
        if hasattr(material, "guid") and material.guid:
            return material.guid
        native = getattr(material, "native", material)
        if hasattr(native, "guid") and native.guid:
            return native.guid
        file_path = getattr(native, "file_path", "") or ""
        if file_path:
            db = _get_asset_database()
            if db:
                try:
                    g = db.get_guid_from_path(file_path)
                    if g:
                        return g
                except Exception as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    pass
        return ""

    def resolve(self):
        """Return the loaded Material, or ``None`` if missing."""
        mat = self._cached
        if mat is not None:
            try:
                native = getattr(mat, "native", mat)
                _ = native.name
                return mat
            except (RuntimeError, AttributeError):
                self._cached = None
        return self._do_resolve()

    def _do_resolve(self):
        if not self._guid and not self._path_hint:
            # Runtime-created materials (e.g. via Clone/Instantiate) have no
            # GUID or path.  Keep the cached reference alive instead of
            # discarding it — there is no asset database entry to re-resolve.
            return self._cached
        # Primary: load by GUID through AssetManager
        if self._guid:
            try:
                from Infernux.core.assets import AssetManager
                from Infernux.core.material import Material
                mat = AssetManager.load_by_guid(self._guid, asset_type=Material)
                if mat is not None:
                    self._cached = mat
                    return mat
            except Exception as exc:
                _log.warning("MaterialRef.resolve by GUID failed: %s", exc)
            # Fallback: resolve GUID → path via C++ AssetDatabase
            db = _get_asset_database()
            if db:
                try:
                    path = db.get_path_from_guid(self._guid)
                    if path:
                        from Infernux.core.material import Material
                        mat = Material.load(path)
                        if mat is not None:
                            self._cached = mat
                            return mat
                except Exception as exc:
                    _log.warning("MaterialRef.resolve by path failed: %s", exc)
        # Last resort: load by path_hint
        if self._path_hint:
            try:
                from Infernux.core.material import Material
                mat = Material.load(self._path_hint)
                if mat is not None:
                    self._cached = mat
                    if not self._guid:
                        g = self._extract_guid(mat)
                        if g:
                            self._guid = g
                    return mat
            except Exception as exc:
                _log.warning("MaterialRef.resolve by path_hint failed: %s", exc)
        self._cached = None
        return None

    def __bool__(self):
        """True if the ref points to a valid material (asset or runtime)."""
        return bool(self._guid) or self._cached is not None

    @property
    def display_name(self) -> str:
        if self._path_hint:
            return os.path.basename(self._path_hint)
        if self._guid:
            return f"GUID:{self._guid[:8]}\u2026"
        # Runtime material — use its name directly
        mat = self._cached
        if mat is not None:
            try:
                native = getattr(mat, "native", mat)
                return native.name or "(Runtime Material)"
            except Exception:
                pass
        return "None"

    def __getattr__(self, name: str) -> Any:
        """Forward attribute access to the underlying Material."""
        if name.startswith("_"):
            raise AttributeError(name)
        mat = self.resolve()
        if mat is None:
            return None
        return getattr(mat, name)

    def __copy__(self):
        return type(self)(guid=self._guid, path_hint=self._path_hint)

    def __deepcopy__(self, memo):
        copied = type(self)(guid=self._guid, path_hint=self._path_hint)
        memo[id(self)] = copied
        return copied

    def __eq__(self, other):
        if other is None:
            return not self._guid and not self._path_hint
        if isinstance(other, AssetRefBase):
            return self._guid == other._guid
        return NotImplemented
