"""
AnimationClip3D — data model for a 3D skeletal animation clip.

Serialized as ``.animclip3d`` JSON files.  This is the authoring-side
counterpart to 2D :class:`AnimationClip` — it references a source model
(typically ``.fbx``) and names an animation take embedded in that file.

This asset is a *take pointer*, not a keyframe container: runtime sampling,
blending, and GPU skinning are implemented in C++ (``InxSkinnedMesh`` builds
the bone palette; the vertex shader applies 4-influence skinning). Keep this
class simple and stable for Python workflows + AI tooling.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from Infernux.core.animation_event import AnimationEvent, events_from_list
from Infernux.engine.path_utils import resolved_path


def is_asset_guid_string(s: str) -> bool:
    """Return whether *s* is a current 32-character lowercase asset GUID."""
    return (
        isinstance(s, str)
        and len(s) == 32
        and all(character in "0123456789abcdef" for character in s)
    )


def resolve_disk_path_for_guid_string(adb, guid: str) -> Optional[str]:
    """Resolve a current asset GUID to a readable source path."""
    if not adb or not is_asset_guid_string(guid):
        return None
    path = adb.get_path_from_guid(guid)
    return resolved_path(path) if path and os.path.isfile(path) else None


def resolve_model_disk_path_from_virtual_base(base: str) -> Optional[str]:
    """Map virtual clip prefix (asset GUID or absolute model file path) to a readable model file path."""
    b = (base or "").strip()
    if not b:
        return None
    if is_asset_guid_string(b):
        try:
            from Infernux.core.assets import AssetManager
            adb = getattr(AssetManager, "_asset_database", None)
            p = resolve_disk_path_for_guid_string(adb, b)
            return p
        except Exception:
            return None
    p = resolved_path(b)
    return p if os.path.isfile(p) else None


@dataclass
class AnimationClip3D:
    """A single 3D animation clip — references a model + named take."""

    name: str = "New Animation Clip 3D"

    # Source skeletal model (FBX/GLTF/etc.) — GUID is authoritative when present.
    source_model_guid: str = ""
    source_model_path: str = ""

    # Animation take name as reported by Assimp / the importer metadata.
    take_name: str = ""

    # Optional: bind-pose bone names captured at import time (debug / tooling).
    # This is duplicated from the model `.meta` for cheap inspector UX.
    bind_pose_bone_names: List[str] = field(default_factory=list)

    # Optional seconds (authoring or tooling); 0.0 = unknown. Embedded takes may be unknown.
    duration_hint: float = 0.0

    # Animation events keyed by normalized time (0..1); dispatched at runtime.
    events: List[AnimationEvent] = field(default_factory=list)

    file_path: str = field(default="", repr=False, compare=False)

    # ── Serialization ───────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source_model_guid": self.source_model_guid,
            "source_model_path": self.source_model_path,
            "take_name": self.take_name,
            "bind_pose_bone_names": list(self.bind_pose_bone_names),
            "duration_hint": float(self.duration_hint),
            "events": [e.to_dict() for e in self.events],
        }

    def serialize_document(self) -> dict:
        """Return the complete current editable document."""
        return self.to_dict()

    def deserialize_document(self, document: dict) -> bool:
        """Replace authoring state while preserving this asset's file identity."""
        try:
            replacement = type(self).from_dict(document)
        except (KeyError, TypeError, ValueError):
            return False
        self.name = replacement.name
        self.source_model_guid = replacement.source_model_guid
        self.source_model_path = replacement.source_model_path
        self.take_name = replacement.take_name
        self.bind_pose_bone_names = replacement.bind_pose_bone_names
        self.duration_hint = replacement.duration_hint
        self.events = replacement.events
        return True

    @classmethod
    def from_dict(cls, d: dict) -> "AnimationClip3D":
        expected = {
            "name",
            "source_model_guid",
            "source_model_path",
            "take_name",
            "bind_pose_bone_names",
            "duration_hint",
            "events",
        }
        if type(d) is not dict or set(d) != expected:
            actual = set(d) if type(d) is dict else set()
            raise ValueError(
                f"animation clip 3D fields mismatch; "
                f"missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
            )
        string_fields = ("name", "source_model_guid", "source_model_path", "take_name")
        if any(type(d[name]) is not str for name in string_fields):
            raise TypeError("animation clip 3D identity fields must be strings")
        source_model_guid = d["source_model_guid"]
        if source_model_guid and not is_asset_guid_string(source_model_guid):
            raise ValueError("source_model_guid must be a 32-character lowercase asset GUID")
        bones = d["bind_pose_bone_names"]
        if type(bones) is not list or any(type(value) is not str for value in bones):
            raise TypeError("bind_pose_bone_names must be an array of strings")
        if not isinstance(d["duration_hint"], (int, float)) or isinstance(d["duration_hint"], bool):
            raise TypeError("duration_hint must be numeric")
        duration_hint = float(d["duration_hint"])
        if not math.isfinite(duration_hint) or duration_hint < 0.0:
            raise ValueError("duration_hint must be finite and non-negative")
        if type(d["events"]) is not list:
            raise TypeError("events must be an array")
        return cls(
            name=d["name"],
            source_model_guid=source_model_guid,
            source_model_path=d["source_model_path"],
            take_name=d["take_name"],
            bind_pose_bone_names=list(bones),
            duration_hint=duration_hint,
            events=events_from_list(d["events"]),
        )

    def copy(self) -> "AnimationClip3D":
        return AnimationClip3D.from_dict(self.to_dict())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AnimationClip3D):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    @property
    def is_valid_reference(self) -> bool:
        return bool((self.source_model_guid or "").strip() or (self.source_model_path or "").strip())

    # ── File I/O ─────────────────────────────────────────────────────

    def save(self, path: str = "") -> bool:
        target = path or self.file_path
        if not target:
            return False
        type(self).from_dict(self.to_dict())
        try:
            from Infernux.core.document_store import write_document_text
            write_document_text(target, json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n")
            return True
        except (OSError, RuntimeError):
            return False

    @classmethod
    def load(cls, path: str) -> Optional["AnimationClip3D"]:
        if not path:
            return None
        # Project Panel virtual take: model.fbx::subanim:<index> (not a file on disk)
        if "::subanim:" in path:
            return cls.from_embedded_take_virtual_path(path)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            clip = cls.from_dict(data)
            clip.file_path = path
            # Name always derives from filename (matches 2D clip behaviour).
            clip.name = os.path.splitext(os.path.basename(path))[0]
            return clip
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def parse_embedded_take_index(virtual_path: str) -> Optional[int]:
        """Return take index for ``*::subanim:<int>`` or None."""
        token = "::subanim:"
        if token not in virtual_path:
            return None
        _, _, rest = virtual_path.partition(token)
        try:
            idx = int(rest.strip())
        except ValueError:
            return None
        if idx < 0 or idx >= 999999:
            return None
        return idx

    @classmethod
    def from_embedded_take_virtual_path(cls, virtual_path: str) -> Optional["AnimationClip3D"]:
        """Build a read-only clip for ``<guid|path>::subanim:<index>`` (Project Panel embedded takes)."""
        token = "::subanim:"
        if token not in virtual_path:
            return None
        base, _, rest = virtual_path.partition(token)
        base = base.strip()
        if not base:
            return None
        try:
            idx = int(rest.strip())
        except ValueError:
            return None
        # Placeholder / overflow row from the project panel
        if idx < 0 or idx >= 999999:
            return None

        model_disk = resolve_model_disk_path_from_virtual_base(base)
        if not model_disk:
            return None

        from Infernux.core.asset_types import read_meta_file

        meta = read_meta_file(model_disk) or {}
        csv = (meta.get("animation_names_csv") or "")
        if isinstance(csv, str):
            names = [p.strip() for p in csv.split(",") if p.strip()]
        else:
            names = []

        if idx >= len(names):
            return None

        take_name = names[idx]
        meta_guid = _read_asset_guid_from_meta_sidecar(model_disk)
        if is_asset_guid_string(base):
            source_guid = base
        else:
            source_guid = meta_guid
        bind_csv = (meta.get("bone_names_csv") or "")
        if isinstance(bind_csv, str):
            bind_names = [p.strip() for p in bind_csv.split(",") if p.strip()]
        else:
            bind_names = []

        clip = cls(
            name=take_name,
            source_model_guid=source_guid,
            source_model_path=model_disk,
            take_name=take_name,
            bind_pose_bone_names=bind_names,
            duration_hint=0.0,
        )
        clip.file_path = virtual_path
        return clip


def _read_asset_guid_from_meta_sidecar(asset_path: str) -> str:
    """Return the canonical GUID from a ``.meta`` sidecar."""
    from Infernux.core.asset_types import read_meta_guid
    return read_meta_guid(asset_path)
