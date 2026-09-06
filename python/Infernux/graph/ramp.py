"""Strict authored curve and gradient literals shared by graph frontends."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


MAX_RAMP_KEYS = 16
CURVE_WRAP_MODES = ("clamp", "repeat", "ping_pong")
GRADIENT_MODES = ("linear", "fixed", "perceptual_blend")


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


@dataclass(frozen=True)
class Keyframe:
    time: float
    value: float
    in_tangent: float = 0.0
    out_tangent: float = 0.0

    def __post_init__(self) -> None:
        for name in ("time", "value", "in_tangent", "out_tangent"):
            object.__setattr__(
                self, name, _number(getattr(self, name), f"curve key {name}")
            )

    def to_dict(self) -> dict[str, float]:
        return {
            "time": self.time,
            "value": self.value,
            "in_tangent": self.in_tangent,
            "out_tangent": self.out_tangent,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "Keyframe":
        if type(value) is not dict or set(value) != {
            "time",
            "value",
            "in_tangent",
            "out_tangent",
        }:
            raise ValueError(
                "curve keys require time, value, in_tangent and out_tangent"
            )
        return cls(
            value["time"], value["value"], value["in_tangent"], value["out_tangent"]
        )


@dataclass(frozen=True)
class AnimationCurve:
    keys: tuple[Keyframe, ...] = (
        Keyframe(0.0, 0.0),
        Keyframe(1.0, 1.0),
    )
    pre_wrap: str = "clamp"
    post_wrap: str = "clamp"

    def __post_init__(self) -> None:
        keys = tuple(
            key if isinstance(key, Keyframe) else Keyframe.from_dict(key)
            for key in self.keys
        )
        if not 1 <= len(keys) <= MAX_RAMP_KEYS:
            raise ValueError(f"curve requires between 1 and {MAX_RAMP_KEYS} keys")
        if any(left.time >= right.time for left, right in zip(keys, keys[1:])):
            raise ValueError("curve key times must be strictly increasing")
        if (
            self.pre_wrap not in CURVE_WRAP_MODES
            or self.post_wrap not in CURVE_WRAP_MODES
        ):
            raise ValueError("curve wrap mode must be clamp, repeat or ping_pong")
        object.__setattr__(self, "keys", keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys": [key.to_dict() for key in self.keys],
            "pre_wrap": self.pre_wrap,
            "post_wrap": self.post_wrap,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "AnimationCurve":
        if type(value) is not dict or set(value) != {"keys", "pre_wrap", "post_wrap"}:
            raise ValueError("curve requires keys, pre_wrap and post_wrap")
        if type(value["keys"]) is not list:
            raise ValueError("curve keys must be an array")
        return cls(
            tuple(Keyframe.from_dict(key) for key in value["keys"]),
            value["pre_wrap"],
            value["post_wrap"],
        )


@dataclass(frozen=True)
class GradientKey:
    time: float
    color: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        time = _number(self.time, "gradient key time")
        if not 0.0 <= time <= 1.0:
            raise ValueError("gradient key time must be between 0 and 1")
        if not isinstance(self.color, (list, tuple)) or len(self.color) != 4:
            raise ValueError("gradient key color must contain exactly four numbers")
        color = tuple(
            _number(component, "gradient color component") for component in self.color
        )
        object.__setattr__(self, "time", time)
        object.__setattr__(self, "color", color)

    def to_dict(self) -> dict[str, Any]:
        return {"time": self.time, "color": list(self.color)}

    @classmethod
    def from_dict(cls, value: Any) -> "GradientKey":
        if type(value) is not dict or set(value) != {"time", "color"}:
            raise ValueError("gradient keys require time and color")
        return cls(value["time"], value["color"])


@dataclass(frozen=True)
class Gradient:
    keys: tuple[GradientKey, ...] = (
        GradientKey(0.0, (1.0, 1.0, 1.0, 1.0)),
        GradientKey(1.0, (0.0, 0.0, 0.0, 0.0)),
    )
    mode: str = "linear"

    def __post_init__(self) -> None:
        keys = tuple(
            key if isinstance(key, GradientKey) else GradientKey.from_dict(key)
            for key in self.keys
        )
        if not 1 <= len(keys) <= MAX_RAMP_KEYS:
            raise ValueError(f"gradient requires between 1 and {MAX_RAMP_KEYS} keys")
        if any(left.time >= right.time for left, right in zip(keys, keys[1:])):
            raise ValueError("gradient key times must be strictly increasing")
        if self.mode not in GRADIENT_MODES:
            raise ValueError(
                "gradient mode must be linear, fixed or perceptual_blend"
            )
        object.__setattr__(self, "keys", keys)

    def to_dict(self) -> dict[str, Any]:
        return {"keys": [key.to_dict() for key in self.keys], "mode": self.mode}

    @classmethod
    def from_dict(cls, value: Any) -> "Gradient":
        if type(value) is not dict or set(value) != {"keys", "mode"}:
            raise ValueError("gradient requires keys and mode")
        if type(value["keys"]) is not list:
            raise ValueError("gradient keys must be an array")
        return cls(
            tuple(GradientKey.from_dict(key) for key in value["keys"]), value["mode"]
        )


__all__ = [
    "CURVE_WRAP_MODES",
    "GRADIENT_MODES",
    "MAX_RAMP_KEYS",
    "AnimationCurve",
    "Keyframe",
    "Gradient",
    "GradientKey",
]
