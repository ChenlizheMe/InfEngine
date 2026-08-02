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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from Infernux.core.animation_event import AnimationEvent, events_from_list


@dataclass
class AnimationClip:
    """A single animation clip — a sequence of sprite frames with timing."""

    name: str = "New Animation Clip"
    authoring_texture_guid: str = ""
    authoring_texture_path: str = ""
    frame_indices: List[int] = field(default_factory=list)
    fps: float = 12.0
    loop: bool = True
    # Animation events keyed by normalized time (0..1); dispatched at runtime.
    events: List[AnimationEvent] = field(default_factory=list)
    file_path: str = field(default="", repr=False, compare=False)

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "authoring_texture_guid": self.authoring_texture_guid,
            "authoring_texture_path": self.authoring_texture_path,
            "frame_indices": list(self.frame_indices),
            "fps": self.fps,
            "loop": self.loop,
            "events": [e.to_dict() for e in self.events],
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> AnimationClip:
        expected = {
            "name", "authoring_texture_guid", "authoring_texture_path",
            "frame_indices", "fps", "loop", "events",
        }
        if type(d) is not dict or set(d) != expected:
            raise ValueError("animation clip must use the complete current field set")
        string_fields = ("name", "authoring_texture_guid", "authoring_texture_path")
        if any(type(d[field]) is not str for field in string_fields):
            raise TypeError("animation clip identity fields must be strings")
        if type(d["frame_indices"]) is not list or any(
            type(index) is not int or index < 0 for index in d["frame_indices"]
        ):
            raise TypeError("animation clip frame_indices must be non-negative integers")
        fps = d["fps"]
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0.0:
            raise ValueError("animation clip fps must be a positive finite number")
        if type(d["loop"]) is not bool:
            raise TypeError("animation clip loop must be a bool")
        return cls(
            name=d["name"],
            authoring_texture_guid=d["authoring_texture_guid"],
            authoring_texture_path=d["authoring_texture_path"],
            frame_indices=list(d["frame_indices"]),
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

    def save(self, path: str = "") -> bool:
        target = path or self.file_path
        if not target:
            return False
        try:
            from Infernux.core.document_store import write_document_text
            write_document_text(target, json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n")
            return True
        except (OSError, RuntimeError):
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
        return len(self.frame_indices)

    @property
    def duration(self) -> float:
        if self.fps <= 0 or not self.frame_indices:
            return 0.0
        return len(self.frame_indices) / self.fps
