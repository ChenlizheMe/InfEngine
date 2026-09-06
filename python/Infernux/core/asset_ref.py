"""
Asset reference types for InxComponent serialized fields.

Provides GUID-based asset references that lazily resolve to loaded assets.
They mirror the C++ ``AssetRef<T>`` pattern and integrate with the Inspector.

All asset ref types inherit from ``AssetRefBase`` and override ``_do_resolve``.
"""

from __future__ import annotations

import os
from typing import Any, Optional


def _get_asset_database():
    """Return the C++ AssetDatabase, trying AssetManager first then engine."""
    from Infernux.core.assets import AssetManager

    if AssetManager._asset_database is not None:
        return AssetManager._asset_database
    try:
        # Editor-only module; absent in stripped player runtimes.
        from Infernux.engine.play_mode import PlayModeManager
    except ImportError:
        return None
    pm = PlayModeManager.instance()
    if pm and pm._asset_database is not None:
        return pm._asset_database
    return None


class AssetRefBase:
    """Base class for GUID-based asset references.

    Stores a GUID string and lazily resolves to the loaded asset via
    ``AssetManager``.
    """

    __slots__ = ("_guid", "_cached", "_path_hint")

    def __init__(self, guid: str = "", path_hint: str = ""):
        from .asset_reference_types import canonical_asset_reference_identity

        self._guid, self._path_hint = canonical_asset_reference_identity(guid, path_hint)
        self._cached = None

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
        """Current editor path when known, otherwise the serialized fallback."""
        if self._guid:
            try:
                from Infernux.engine.interaction import AssetMutationService

                mutations = AssetMutationService.instance()
                if mutations is not None:
                    resolved = mutations.resolve_path_hint(self._guid)
                    if resolved:
                        return resolved
            except ImportError:
                pass
            database = _get_asset_database()
            # Duck-typed: headless play mode injects plain-Python database
            # stubs that may not implement the display-path lookup.
            resolve_path = getattr(database, "get_path_from_guid", None)
            if callable(resolve_path):
                resolved = str(resolve_path(self._guid) or "")
                if resolved:
                    return resolved
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
        path_hint = self.path_hint
        if path_hint:
            return os.path.basename(path_hint)
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


class GenericAssetRef(AssetRefBase):
    """Typed reference for registered assets without a specialized wrapper."""

    __slots__ = ("_asset_type",)

    def __init__(self, asset_type: str, guid: str = "", path_hint: str = ""):
        from .asset_reference_types import asset_type_registry

        descriptor = asset_type_registry.require(asset_type)
        super().__init__(guid=guid, path_hint=path_hint)
        self._asset_type = descriptor.type_id

    @property
    def asset_type(self) -> str:
        return self._asset_type

    def _do_resolve(self):
        from Infernux.core.assets import AssetManager

        return AssetManager.load_by_guid(self._guid)


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
    """Reference to an AnimStateMachine (.animfsm) asset. GUID is the only identity."""

    def _do_resolve(self):
        db = _get_asset_database()
        if db is None:
            return None
        from Infernux.core.animation_clip3d import resolve_disk_path_for_guid_string
        path = resolve_disk_path_for_guid_string(db, self._guid) or db.get_path_from_guid(self._guid)
        if not path:
            return None
        from Infernux.core.anim_state_machine import AnimStateMachine
        return AnimStateMachine.load(path)


class ParticleGraphRef(AssetRefBase):
    """Reference to a compiled ParticleGraph or ParticleScript asset. GUID only."""

    def _do_resolve(self):
        db = _get_asset_database()
        path = db.get_path_from_guid(self._guid) if db else ""
        if not path:
            return None
        if path.lower().endswith(".particle.py"):
            from pathlib import Path
            from Infernux.particle.script import ParticleScriptCompiler

            return ParticleScriptCompiler().parse(
                Path(path).read_text(encoding="utf-8"),
                source_name=path,
            )
        from Infernux.particle.asset import ParticleGraphAsset

        return ParticleGraphAsset.load(path)


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
            # GUID is the asset identity; never fall back to a path when the
            # GUID lookup fails.
            return AssetManager.load_by_guid(self._guid, asset_type=RenderEffect)
        if self._path_hint:
            # Code-authored RenderStack references (Python-first API) may name
            # an .effect file directly; without a GUID the explicit path *is*
            # the identity, not a fallback.
            return AssetManager.load(self._path_hint, asset_type=RenderEffect)
        # Runtime-created effects carry no GUID; the live object is the identity.
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
    timeline states); resolved the same way as an .animfsm. GUID only.
    """

    def _do_resolve(self):
        db = _get_asset_database()
        if db is None:
            return None
        from Infernux.core.animation_clip3d import resolve_disk_path_for_guid_string
        path = resolve_disk_path_for_guid_string(db, self._guid) or db.get_path_from_guid(self._guid)
        if not path:
            return None
        from Infernux.core.anim_state_machine import AnimStateMachine
        return AnimStateMachine.load(path)


class AnimationClipRef(AssetRefBase):
    """Reference to an AnimationClip (.animclip2d) asset. GUID only."""

    def _do_resolve(self):
        db = _get_asset_database()
        path = db.get_path_from_guid(self._guid) if db else ""
        if not path:
            return None
        from Infernux.core.animation_clip import AnimationClip
        return AnimationClip.load(path)


class AnimationClip3DRef(AssetRefBase):
    """Reference to an AnimationClip3D (.animclip3d) asset. GUID only."""

    def _do_resolve(self):
        db = _get_asset_database()
        path = db.get_path_from_guid(self._guid) if db else ""
        if not path:
            return None
        from Infernux.core.animation_clip3d import AnimationClip3D
        return AnimationClip3D.load(path)


class AnimationTimelineRef(AssetRefBase):
    """Reference to an AnimationTimeline (.animtimeline) asset. GUID only."""

    def _do_resolve(self):
        db = _get_asset_database()
        path = db.get_path_from_guid(self._guid) if db else ""
        if not path:
            return None
        from Infernux.core.animation_timeline import AnimationTimeline
        return AnimationTimeline.load(path)


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

_ASSET_REF_CLASSES: dict[str, type[AssetRefBase]] = {}


def _ensure_ref_classes():
    """Populate Python reference bindings on first access."""
    if _ASSET_REF_CLASSES:
        return
    _ASSET_REF_CLASSES.update({
        "Material": MaterialRef,
        "Texture": TextureRef,
        "Shader": ShaderRef,
        "AudioClip": AudioClipRef,
        "PhysicMaterial": PhysicMaterialRef,
        "AnimStateMachine": AnimStateMachineRef,
        "ParticleGraph": ParticleGraphRef,
        "RenderEffect": RenderEffectRef,
        "AnimationClip": AnimationClipRef,
        "AnimationClip3D": AnimationClip3DRef,
        "AnimationTimeline": AnimationTimelineRef,
        "TimelineFSM": TimelineFSMRef,
    })


def register_asset_type(asset_type: str, *, ref_class, drag_type: str, extensions: tuple,
                        display: str, prefix: str):
    """Register a new asset type for use with ``FieldType.ASSET``.

    Call this early (e.g. in a plug-in's ``__init__``) to make the type
    available to serialization and the inspector.
    """
    from .asset_reference_types import AssetReferenceType, asset_type_registry

    _ensure_ref_classes()
    normalized_extensions = frozenset(
        str(item).removeprefix("*").casefold() for item in extensions
    )
    asset_type_registry.register(
        AssetReferenceType(
            type_id=asset_type,
            display_name=display,
            extensions=normalized_extensions,
            drag_types=(drag_type,),
            widget_prefix=prefix,
        ),
        replace=asset_type_registry.get(asset_type) is not None,
    )
    _ASSET_REF_CLASSES[asset_type] = ref_class


def get_asset_type_config(asset_type: str) -> Optional[dict]:
    """Return a dynamic view over the single asset-type registry."""
    from .asset_reference_types import asset_type_registry

    _ensure_ref_classes()
    descriptor = asset_type_registry.get(asset_type)
    ref_class = _ASSET_REF_CLASSES.get(
        descriptor.type_id if descriptor is not None else str(asset_type)
    )
    if descriptor is None or ref_class is None:
        return None
    return {
        "ref_class": ref_class,
        "drag_type": descriptor.drag_types[0],
        "extensions": descriptor.patterns,
        "display": descriptor.display_name,
        "prefix": descriptor.widget_prefix,
    }


def get_all_asset_type_configs() -> dict:
    """Return dynamic configs for all registered Python reference classes."""
    _ensure_ref_classes()
    return {
        asset_type: get_asset_type_config(asset_type)
        for asset_type in _ASSET_REF_CLASSES
    }


def create_asset_ref(
    asset_type: str,
    *,
    guid: str = "",
    path_hint: str = "",
) -> AssetRefBase:
    """Create the canonical specialized or generic reference for one type."""
    from .asset_reference_types import asset_type_registry

    _ensure_ref_classes()
    descriptor = asset_type_registry.require(asset_type)
    ref_class = _ASSET_REF_CLASSES.get(descriptor.type_id)
    if ref_class is None:
        return GenericAssetRef(
            descriptor.type_id,
            guid=guid,
            path_hint=path_hint,
        )
    return ref_class(guid=guid, path_hint=path_hint)


def get_asset_type_for_ref(ref) -> Optional[str]:
    """Given an AssetRefBase instance or class, return its asset_type name."""
    _ensure_ref_classes()
    if isinstance(ref, GenericAssetRef):
        return ref.asset_type
    ref_cls = ref if isinstance(ref, type) else type(ref)
    for name, registered_ref_class in _ASSET_REF_CLASSES.items():
        if registered_ref_class is ref_cls:
            return name
    return None


class MaterialRef(AssetRefBase):
    """GUID-based reference to a Material asset.

    Lazily loads the Material through ``AssetManager.load_by_guid`` on first
    access and caches the result. GUID is the only asset identity; a ref
    without a GUID can only be a runtime-created material kept alive through
    the cached object.

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
                g = db.get_guid_from_path(file_path)
                if g:
                    return g
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
        if not self._guid:
            # Runtime-created materials (e.g. via Clone/Instantiate) have no
            # GUID. Keep the cached reference alive instead of discarding
            # it — there is no asset database entry to re-resolve.
            return self._cached
        from Infernux.core.assets import AssetManager
        from Infernux.core.material import Material

        mat = AssetManager.load_by_guid(self._guid, asset_type=Material)
        self._cached = mat
        return mat

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
            return not self._guid and self._cached is None
        if isinstance(other, AssetRefBase):
            return self._guid == other._guid
        return NotImplemented
