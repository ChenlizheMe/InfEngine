"""TimelineFSMRuntime — a standalone state machine that plays Timeline states.

A Timeline FSM (``.timelinefsm``) is an :class:`AnimStateMachine` whose states
are all timeline nodes (``kind == 'timeline'``).  This runtime advances the
active state's timeline, drives a target transform (additive or absolute), and
evaluates transitions (exit-time + parameter conditions + triggers) — mirroring
the animator FSM semantics but with no dependency on a renderer.

Used by the :class:`TimelineAction` component; kept renderer-agnostic so it can
be reused elsewhere.
"""

from __future__ import annotations

from typing import Dict, Optional

from Infernux.core.anim_state_machine import (
    AnimStateMachine, AnimState, AnimTransition,
)
from Infernux.core.animation_timeline import AnimationTimeline, sample_sorted_keys
from Infernux.debug import Debug
from Infernux.graph.types import ValueType

_DEFAULT_PERIOD = 1.0  # fallback loop period when a timeline has no duration

def _get_asset_database():
    try:
        from Infernux.core.assets import AssetManager
        if AssetManager._asset_database is not None:
            return AssetManager._asset_database
    except ImportError:
        pass
    try:
        from Infernux.engine.play_mode import PlayModeManager
        pm = PlayModeManager.instance()
        if pm and pm._asset_database is not None:
            return pm._asset_database
    except ImportError:
        pass
    return None


def _resolve_timeline_path(state: AnimState) -> Optional[str]:
    guid = getattr(state, "timeline_guid", "") or ""
    path = (getattr(state, "timeline_path", "") or "").strip()
    if guid:
        db = _get_asset_database()
        if db:
            try:
                p = db.get_path_from_guid(guid)
                if p:
                    return p
            except Exception:
                pass
    return path or None


class TimelineFSMRuntime:
    """Plays a Timeline FSM, driving a supplied transform each frame."""

    def __init__(self):
        self._fsm: Optional[AnimStateMachine] = None
        self._params: Dict[str, object] = {}
        self._timeline_cache: Dict[str, Optional[AnimationTimeline]] = {}
        self._state_name: str = ""
        self._timeline: Optional[AnimationTimeline] = None
        self._elapsed: float = 0.0
        self._playing: bool = False
        self._base = None
        self.playback_speed: float = 1.0
        # ── Per-frame caches (refreshed on state entry) ─────────────────
        self._state: Optional[AnimState] = None          # active AnimState (avoids name scans)
        self._sorted_keys = None                          # timeline keys sorted once per state
        self._apply_additive: bool = True                 # cached apply_mode test
        self._duration: float = _DEFAULT_PERIOD           # cached timeline duration
        # Cache against the native lifetime handle, never the wrapper address.
        self._trs_handle = None
        self._trs_setter = None
        self._last_applied_trs = None

    # ── Setup ──────────────────────────────────────────────────────────
    def set_fsm(self, fsm: Optional[AnimStateMachine]):
        self._fsm = fsm
        self._timeline_cache = {}
        self._params = {}
        self._state_name = ""
        self._timeline = None
        self._elapsed = 0.0
        self._playing = False
        self._base = None
        self._state = None
        self._sorted_keys = None
        self._apply_additive = True
        self._duration = _DEFAULT_PERIOD
        self._trs_handle = None
        self._trs_setter = None
        self._last_applied_trs = None
        if fsm is not None:
            for p in fsm.parameters:
                if p.value_type.value_type is ValueType.BOOL:
                    self._params[p.name] = bool(p.default)
                elif p.value_type.value_type is ValueType.I32:
                    self._params[p.name] = int(p.default)
                else:
                    self._params[p.name] = float(p.default)

    @property
    def fsm(self) -> Optional[AnimStateMachine]:
        return self._fsm

    @property
    def current_state(self) -> str:
        return self._state_name

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def normalized_time(self) -> float:
        if self._timeline is None:
            return 0.0
        dur = max(1e-6, self._duration)
        state = self._state
        if state is not None and state.loop:
            return (self._elapsed % dur) / dur
        return min(self._elapsed / dur, 1.0)

    # ── Parameter API ──────────────────────────────────────────────────
    def set_parameter(self, name: str, value: object):
        self._params[name] = value

    def get_parameter(self, name: str, default: object = None) -> object:
        return self._params.get(name, default)

    def set_bool(self, name: str, value: bool):
        self._params[name] = bool(value)

    def get_bool(self, name: str) -> bool:
        return bool(self._params.get(name, False))

    def set_float(self, name: str, value: float):
        self._params[name] = float(value)

    def get_float(self, name: str) -> float:
        return float(self._params.get(name, 0.0))

    def set_int(self, name: str, value: int):
        self._params[name] = int(value)

    def get_int(self, name: str) -> int:
        return int(self._params.get(name, 0))

    def set_trigger(self, name: str):
        self._params[name] = True

    # ── Playback ───────────────────────────────────────────────────────
    def play(self, state_name: str = "", *, transform=None) -> bool:
        if not self._fsm:
            return False
        name = state_name or self._fsm.default_state
        if not name:
            return False
        return self._enter_state(name, transform)

    def stop(self):
        self._playing = False

    @property
    def needs_update(self) -> bool:
        """Whether this runtime has simulation work for the current frame."""
        return self._playing

    def update(self, delta_time: float, transform=None):
        if self._fsm is None or self._timeline is None or not self._playing:
            return
        tl = self._timeline
        state = self._state
        loop = state.loop if state is not None else True
        if self._playing:
            speed = self.playback_speed * (state.speed if state else 1.0)
            self._elapsed += delta_time * speed
            dur = self._duration
            if self._elapsed >= dur:
                self._apply_timeline(tl, dur, transform)
                self._try_transition(transform)
                if self._timeline is tl and self._playing:
                    if loop:
                        self._elapsed = self._elapsed % dur
                    else:
                        self._elapsed = dur
                        self._playing = False
                    self._apply_timeline(tl, self._elapsed, transform)
                return
        self._apply_timeline(tl, self._elapsed, transform)
        self._try_transition(transform)

    # ── Internals ──────────────────────────────────────────────────────
    def _get_state(self) -> Optional[AnimState]:
        # Fast path: the cached AnimState matches the active state name.
        s = self._state
        if s is not None and s.name == self._state_name:
            return s
        if self._fsm and self._state_name:
            s = self._fsm.get_state(self._state_name)
            self._state = s
            return s
        return None

    def _resolve_timeline(self, state: AnimState) -> Optional[AnimationTimeline]:
        key = state.name
        if key in self._timeline_cache:
            return self._timeline_cache[key]
        tl = None
        path = _resolve_timeline_path(state)
        if path:
            tl = AnimationTimeline.load(path)
            if tl is None:
                Debug.log_warning(f"[TimelineFSM] Failed to load timeline for state '{state.name}': {path}")
        self._timeline_cache[key] = tl
        return tl

    def _enter_state(self, state_name: str, transform) -> bool:
        if not self._fsm:
            return False
        state = self._fsm.get_state(state_name)
        if state is None:
            Debug.log_warning(f"[TimelineFSM] State not found: '{state_name}'")
            return False
        if not getattr(state, "restart_same_clip", False):
            if self._playing and self._state_name == state_name:
                return True
        self._state_name = state_name
        self._state = state
        self._timeline = self._resolve_timeline(state)
        self._elapsed = 0.0
        self._playing = True
        # Refresh per-state caches so the per-frame update path does no scans,
        # re-sorts, getattr lookups, or imports.
        tl = self._timeline
        if tl is not None:
            self._sorted_keys = tl.sorted_keys()
            self._apply_additive = (getattr(tl, "apply_mode", "additive") == "additive")
            self._duration = max(1e-6, float(tl.duration))
        else:
            self._sorted_keys = None
            self._apply_additive = True
            self._duration = _DEFAULT_PERIOD
        self._capture_base(transform)
        self._last_applied_trs = None
        if tl is not None:
            self._apply_timeline(tl, 0.0, transform)
        return True

    def _capture_base(self, transform):
        if transform is None:
            self._base = ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
            return
        try:
            p, r, s = transform.local_position, transform.local_euler_angles, transform.local_scale
            self._base = (
                [float(p.x), float(p.y), float(p.z)],
                [float(r.x), float(r.y), float(r.z)],
                [float(s.x), float(s.y), float(s.z)],
            )
        except Exception:
            self._base = ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0])

    def _apply_timeline(self, tl: AnimationTimeline, t: float, transform):
        if transform is None:
            return
        keys = self._sorted_keys
        sampled = sample_sorted_keys(keys, t) if keys is not None else tl.sample(t)
        if sampled is None:
            return
        pos, rot, scl = sampled

        # Resolve the authoritative combined setter once per transform lifetime.
        transform_identity = getattr(transform, "handle", None) or transform
        if self._trs_handle is None or transform_identity != self._trs_handle:
            self._trs_handle = transform_identity
            self._trs_setter = transform.set_local_trs
        trs = self._trs_setter

        try:
            if self._apply_additive:
                bp, br, bs = self._base or ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
                px, py, pz = bp[0] + pos[0], bp[1] + pos[1], bp[2] + pos[2]
                rx, ry, rz = br[0] + rot[0], br[1] + rot[1], br[2] + rot[2]
                sx, sy, sz = bs[0] * scl[0], bs[1] * scl[1], bs[2] * scl[2]
            else:
                px, py, pz = pos[0], pos[1], pos[2]
                rx, ry, rz = rot[0], rot[1], rot[2]
                sx, sy, sz = scl[0], scl[1], scl[2]

            # Single pybind crossing, no Vector3 objects, one subtree invalidate.
            applied = (px, py, pz, rx, ry, rz, sx, sy, sz)
            if applied == self._last_applied_trs:
                return
            trs(*applied)
            self._last_applied_trs = applied
        except Exception as exc:
            Debug.log_suppressed("TimelineFSMRuntime._apply_timeline", exc)

    def _exit_gate_ok(self, state: AnimState) -> bool:
        if self._timeline is None:
            return True
        dur = self._duration
        thr = max(0.0, min(1.0, float(getattr(state, "exit_time_normalized", 1.0))))
        progress = min(max(self._elapsed / dur, 0.0), 1.0)
        return progress + 1e-7 >= thr

    def _try_transition(self, transform):
        state = self._state if self._state is not None else self._get_state()
        if not state:
            return
        transitions = state.transitions
        if not transitions:
            return
        if not self._exit_gate_ok(state):
            return
        for tr in transitions:
            if self._evaluate_condition(tr, state):
                self._consume_triggers(tr)
                self._enter_state(tr.target_state, transform)
                return

    def _evaluate_condition(self, transition: AnimTransition, state: AnimState) -> bool:
        if not transition.conditions:
            # No explicit condition: advance only when a non-looping timeline ends.
            if self._timeline is None or state.loop:
                return False
            return self._elapsed >= self._duration
        return bool(
            self._fsm
            and self._fsm.evaluate_transition_conditions(
                transition, self._params
            )
        )

    def _consume_triggers(self, transition: AnimTransition):
        names = set(
            self._fsm.transition_parameter_names(transition)
            if self._fsm is not None
            else ()
        )
        for name, val in list(self._params.items()):
            if val is True and name in names:
                self._params[name] = False
