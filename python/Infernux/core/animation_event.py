"""
AnimationEvent — named callbacks fired at a normalized time within a clip.

Mirrors Godot's animation "Call Method" track in a lightweight, asset-agnostic
way: each event has a normalized time (0..1) inside its clip, a function name,
and optional string / number arguments.  At runtime the animators dispatch each
crossed event to every Python component on the animated GameObject.

Dispatch contract (per fired event):
  * if a component defines ``on_animation_event(function, string_arg, number_arg)``
    it is called (generic sink), and
  * if a component defines a method named ``function`` it is called with a
    best-effort argument arity (``(string_arg, number_arg)`` → ``(string_arg,)`` → ``()``).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, List


@dataclass
class AnimationEvent:
    """A single animation event keyed by normalized clip time (0..1)."""

    time_normalized: float = 0.0
    function: str = ""
    string_arg: str = ""
    number_arg: float = 0.0

    def to_dict(self) -> dict:
        return {
            "time_normalized": float(self.time_normalized),
            "function": self.function,
            "string_arg": self.string_arg,
            "number_arg": float(self.number_arg),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AnimationEvent":
        expected = {"time_normalized", "function", "string_arg", "number_arg"}
        if type(d) is not dict or set(d) != expected:
            raise ValueError("animation event must use the complete current field set")
        t = d["time_normalized"]
        num = d["number_arg"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
            raise TypeError("animation event time_normalized must be finite numeric data")
        if not 0.0 <= float(t) <= 1.0:
            raise ValueError("animation event time_normalized must be in [0, 1]")
        if isinstance(num, bool) or not isinstance(num, (int, float)) or not math.isfinite(num):
            raise TypeError("animation event number_arg must be finite numeric data")
        if type(d["function"]) is not str or type(d["string_arg"]) is not str:
            raise TypeError("animation event function and string_arg must be strings")
        return cls(
            time_normalized=float(t),
            function=d["function"],
            string_arg=d["string_arg"],
            number_arg=float(num),
        )


def events_from_list(raw: Any) -> List[AnimationEvent]:
    """Build an event list from the current serialized representation."""
    if type(raw) is not list:
        raise TypeError("animation events must be an array")
    return [AnimationEvent.from_dict(item) for item in raw]


def collect_crossed_events(
    events: List[AnimationEvent], prev_norm: float, curr_norm: float, looped: bool
) -> List[AnimationEvent]:
    """Return events whose normalized time falls in the just-played window.

    Non-looping window is ``(prev_norm, curr_norm]``.  When the clip wrapped this
    frame (``looped``) the window is ``(prev_norm, 1] ∪ [0, curr_norm]``.
    """
    if not events:
        return []
    eps = 1e-6
    fired: List[AnimationEvent] = []
    for ev in events:
        t = ev.time_normalized
        if looped:
            if (prev_norm + eps < t <= 1.0 + eps) or (-eps <= t <= curr_norm + eps):
                fired.append(ev)
        else:
            if prev_norm + eps < t <= curr_norm + eps:
                fired.append(ev)
    return fired


def _invoke_event_method(method, ev: AnimationEvent) -> bool:
    """Call *method* with a best-effort argument arity.  Returns True if invoked."""
    for args in ((ev.string_arg, ev.number_arg), (ev.string_arg,), ()):
        try:
            method(*args)
            return True
        except TypeError:
            continue
        except Exception:
            from Infernux.debug import Debug
            Debug.log_warning(f"[AnimationEvent] handler '{ev.function}' raised")
            return True
    return False


def dispatch_animation_events(
    game_object, events: List[AnimationEvent], prev_norm: float, curr_norm: float, looped: bool
) -> None:
    """Fire all events crossed in the current frame's playback window."""
    fired = collect_crossed_events(events, prev_norm, curr_norm, looped)
    if not fired or game_object is None:
        return
    try:
        comps = list(game_object.get_py_components() or [])
    except Exception:
        return
    if not comps:
        return
    from Infernux.debug import Debug
    for ev in fired:
        for comp in comps:
            sink = getattr(comp, "on_animation_event", None)
            if callable(sink):
                try:
                    sink(ev.function, ev.string_arg, ev.number_arg)
                except Exception:
                    Debug.log_warning(
                        f"[AnimationEvent] on_animation_event raised for '{ev.function}'"
                    )
            if ev.function:
                method = getattr(comp, ev.function, None)
                if callable(method) and method is not sink:
                    _invoke_event_method(method, ev)
