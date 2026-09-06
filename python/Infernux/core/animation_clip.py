"""
AnimationClip — data model for a 2D sprite animation clip.

An AnimationClip describes a sequence of sprite frames, playback speed,
and looping behaviour.  Serialized as ``.animclip2d`` JSON files.

Usage::

    clip = AnimationClip.load("Assets/Animations/idle.animclip2d")
    clip.save("Assets/Animations/idle.animclip2d")
"""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from Infernux.core.animation_event import AnimationEvent, events_from_list
from Infernux.engine.path_utils import lexical_path


@dataclass
class AnimationFrame:
    """One stable occurrence in a 2D animation sequence."""

    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sprite_frame_id: str = ""

    def to_dict(self) -> dict:
        return {
            "stable_id": self.stable_id,
            "sprite_frame_id": self.sprite_frame_id,
        }

    @classmethod
    def from_dict(cls, document: dict) -> "AnimationFrame":
        expected = {"stable_id", "sprite_frame_id"}
        if type(document) is not dict or set(document) != expected:
            raise ValueError("animation frame must use the complete current field set")
        stable_id = document["stable_id"]
        sprite_frame_id = document["sprite_frame_id"]
        for field_name, value in (
            ("stable_id", stable_id),
            ("sprite_frame_id", sprite_frame_id),
        ):
            if (
                type(value) is not str
                or len(value) != 32
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise TypeError(
                    f"animation frame {field_name} must be a 32-character lowercase UUID hex string"
                )
        return cls(stable_id=stable_id, sprite_frame_id=sprite_frame_id)


@dataclass
class AnimationClip:
    """A single animation clip — a sequence of sprite frames with timing."""

    name: str = "New Animation Clip"
    authoring_texture_guid: str = ""
    authoring_texture_path: str = ""
    frames: List[AnimationFrame] = field(default_factory=list)
    fps: float = 12.0
    loop: bool = True
    # Animation events keyed by normalized time (0..1); dispatched at runtime.
    events: List[AnimationEvent] = field(default_factory=list)
    file_path: str = field(default="", repr=False, compare=False)

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        frame_documents = [frame.to_dict() for frame in self.frames]
        validated_frames = [
            AnimationFrame.from_dict(document) for document in frame_documents
        ]
        if len({frame.stable_id for frame in validated_frames}) != len(validated_frames):
            raise ValueError("animation clip frame stable_id values must be unique")
        d: dict = {
            "name": self.name,
            "authoring_texture_guid": self.authoring_texture_guid,
            "authoring_texture_path": self.authoring_texture_path,
            "frames": frame_documents,
            "fps": self.fps,
            "loop": self.loop,
            "events": [e.to_dict() for e in self.events],
        }
        return d

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
        self.authoring_texture_guid = replacement.authoring_texture_guid
        self.authoring_texture_path = replacement.authoring_texture_path
        self.frames = replacement.frames
        self.fps = replacement.fps
        self.loop = replacement.loop
        self.events = replacement.events
        return True

    @classmethod
    def from_dict(cls, d: dict) -> AnimationClip:
        expected = {
            "name", "authoring_texture_guid", "authoring_texture_path",
            "frames", "fps", "loop", "events",
        }
        if type(d) is not dict or set(d) != expected:
            raise ValueError("animation clip must use the complete current field set")
        string_fields = ("name", "authoring_texture_guid", "authoring_texture_path")
        if any(type(d[field]) is not str for field in string_fields):
            raise TypeError("animation clip identity fields must be strings")
        if type(d["frames"]) is not list:
            raise TypeError("animation clip frames must be an array")
        frames = [AnimationFrame.from_dict(item) for item in d["frames"]]
        if len({frame.stable_id for frame in frames}) != len(frames):
            raise ValueError("animation clip frame stable_id values must be unique")
        fps = d["fps"]
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0.0:
            raise ValueError("animation clip fps must be a positive finite number")
        if type(d["loop"]) is not bool:
            raise TypeError("animation clip loop must be a bool")
        return cls(
            name=d["name"],
            authoring_texture_guid=d["authoring_texture_guid"],
            authoring_texture_path=d["authoring_texture_path"],
            frames=frames,
            fps=float(fps),
            loop=d["loop"],
            events=events_from_list(d["events"]),
        )

    def copy(self) -> AnimationClip:
        return AnimationClip.from_dict(self.to_dict())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AnimationClip):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    # ── File I/O ──────────────────────────────────────────────────────

    @staticmethod
    def _project_root_for_asset(path: str) -> str:
        absolute = lexical_path(path)
        parts = absolute.replace("\\", "/").split("/")
        for index, part in enumerate(parts):
            if part.casefold() == "assets":
                root = "/".join(parts[:index])
                if not root and absolute.startswith("/"):
                    root = "/"
                return lexical_path(root)
        return ""

    def validate_sprite_frame_references(
        self,
        *,
        project_root: str = "",
        guid_paths: Optional[Dict[str, str]] = None,
    ) -> str:
        """Validate every source-frame ID against the declared Sprite texture.

        Returns the resolved texture path. Empty clips require no texture and
        return an empty path. This check belongs to authoring save/build gates;
        runtime playback consumes the already-validated stable IDs without
        touching source metadata.
        """
        if not self.frames:
            return ""
        guid = str(self.authoring_texture_guid or "").strip()
        if not guid:
            raise ValueError(
                "animation clip frames require an authoring Sprite texture GUID"
            )
        if guid_paths is not None:
            texture_path = str(guid_paths.get(guid) or "").strip()
        else:
            from Infernux.core.assets import AssetManager

            texture_path = str(
                AssetManager.require_asset_database().get_path_from_guid(guid) or ""
            ).strip()
        if texture_path and not os.path.isabs(texture_path) and project_root:
            texture_path = os.path.join(project_root, texture_path)
        texture_path = lexical_path(texture_path) if texture_path else ""
        if not texture_path or not os.path.isfile(texture_path):
            raise ValueError(
                "animation clip frames require an existing authoring Sprite texture"
            )

        from Infernux.core.asset_types import (
            TextureType,
            read_texture_import_settings,
        )

        settings = read_texture_import_settings(texture_path)
        if settings.texture_type is not TextureType.SPRITE:
            raise ValueError(
                f"animation clip texture is not imported as Sprite: {texture_path}"
            )
        available = {frame.stable_id for frame in settings.sprite_frames}
        missing = sorted(
            {
                frame.sprite_frame_id
                for frame in self.frames
                if frame.sprite_frame_id not in available
            }
        )
        if missing:
            preview = ", ".join(missing[:4])
            if len(missing) > 4:
                preview += ", ..."
            raise ValueError(
                "animation clip references SpriteFrame IDs that are missing "
                f"from '{texture_path}': {preview}"
            )
        return texture_path

    def save(self, path: str = "") -> bool:
        target = path or self.file_path
        if not target:
            return False
        try:
            self.validate_sprite_frame_references(
                project_root=self._project_root_for_asset(target),
            )
            from Infernux.core.document_store import write_document_text
            write_document_text(target, json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n")
            return True
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    @classmethod
    def load(cls, path: str) -> Optional[AnimationClip]:
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            clip = cls.from_dict(data)
            clip.file_path = path
            # Name always derives from filename
            clip.name = os.path.splitext(os.path.basename(path))[0]
            return clip
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    # ── Helpers ───────────────────────────────────────────────────────

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def duration(self) -> float:
        if self.fps <= 0 or not self.frames:
            return 0.0
        return len(self.frames) / self.fps
