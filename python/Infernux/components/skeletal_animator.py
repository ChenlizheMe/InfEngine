"""
SkeletalAnimator — runtime 3D animation state machine controller.

Mirrors :class:`SpiritAnimator` (2D) for skeletal assets: bridge from
``.animfsm`` / ``.animclip3d`` to :class:`SkinnedMeshRenderer`, advancing FSM
state and pushing playback time to native code for an upcoming skinning path.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from Infernux.components.component import InxComponent
from Infernux.components.serialized_field import serialized_field
from Infernux.components.decorators import require_component, disallow_multiple, add_component_menu
from Infernux.components.builtin.skinned_mesh_renderer import SkinnedMeshRenderer
from Infernux.core.anim_state_machine import (
    AnimStateMachine, AnimState, AnimTransition, evaluate_anim_condition,
)
from Infernux.core.animation_clip3d import AnimationClip3D, resolve_disk_path_for_guid_string
from Infernux.core.asset_ref import AnimStateMachineRef
from Infernux.debug import Debug
from Infernux.engine.path_utils import lexical_path, portable_path


def _normalize_guid_key(s: str) -> str:
    """Case-insensitive, hyphen-insensitive key for comparing asset GUIDs."""
    return (s or "").replace("-", "").strip().lower()


def _try_guid_for_model_path(db, path: str) -> str:
    """Resolve a model file path to its asset GUID via the database, if available."""
    if not db or not (path or "").strip():
        return ""
    p0 = str(path).strip()
    if not p0:
        return ""
    seen = set()
    cands: list = []
    for p in (p0, portable_path(p0), lexical_path(p0), portable_path(lexical_path(p0))):
        if p and p not in seen:
            seen.add(p)
            cands.append(p)
    for p in cands:
        try:
            g = db.get_guid_from_path(p)
            if g and str(g).strip():
                return str(g).strip()
        except Exception:
            pass
    return ""


def _model_keys_for_source(db, source_guid: str, source_path: str) -> tuple:
    """
    Return (key, had_explicit_guid) where key is the normalized 32-hex id when known.
    Uses serialized GUID first, then path→GUID from the database.
    """
    g = (source_guid or "").strip()
    k = _normalize_guid_key(g)
    if k:
        return (k, True)
    g2 = _try_guid_for_model_path(db, source_path) if db else ""
    k2 = _normalize_guid_key(g2)
    if k2:
        return (k2, False)
    return ("", False)


def _skinned_mismatch_message(
    db,
    clip: AnimationClip3D,
    source_model_guid: str,
) -> str:
    """
    If clip and renderer are definitely different model assets, return a warning string.
    Prefers GUID and path→GUID; does not use raw path comparison when both GUID keys match.
    """
    p_clip = (getattr(clip, "source_model_path", "") or "").strip()
    g_clip = (getattr(clip, "source_model_guid", "") or "").strip()
    g_rend = (source_model_guid or "").strip()

    k_clip, _ = _model_keys_for_source(db, g_clip, p_clip)
    k_rend = _normalize_guid_key(g_rend)

    if k_clip and k_rend and k_clip != k_rend:
        return (
            f"[SkeletalAnimator] Clip source model does not match SkinnedMeshRenderer "
            f"(clip guid≈{g_clip!r} path='{p_clip}' vs renderer guid≈{g_rend!r})."
        )
    if k_clip and k_rend:
        return ""

    if k_clip or k_rend:
        return ""

    # SkinnedMeshRenderer intentionally exposes only the source-model GUID. If
    # neither side can provide a GUID, there is no renderer path to compare and
    # therefore no definite mismatch to report.
    return ""


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


def _resolve_clip_path_from(guid: str, path_hint: str) -> Optional[str]:
    """Resolve a clip GUID / path-hint to a usable disk path (or embedded take id)."""
    if guid:
        db = _get_asset_database()
        if db:
            try:
                p = resolve_disk_path_for_guid_string(db, guid)
                if p:
                    return p
            except Exception:
                pass
    raw = (path_hint or "").strip()
    # Project panel: embedded FBX take as "<guid>::subanim:<index>" (not a file path).
    if raw and "::subanim:" in raw:
        return raw
    if raw and os.path.isfile(raw):
        return raw
    return None


def _resolve_clip_path(state: AnimState) -> Optional[str]:
    return _resolve_clip_path_from(state.clip_guid, state.clip_path)


def _resolve_clip_b_path(state: AnimState) -> Optional[str]:
    return _resolve_clip_path_from(getattr(state, "clip_b_guid", ""), getattr(state, "clip_b_path", ""))


def _resolve_timeline_path(state: AnimState) -> Optional[str]:
    """Resolve a timeline state's ``.animtimeline`` asset to a disk path."""
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


def _clip_duration_hint(clip: Optional[AnimationClip3D]) -> float:
    if clip is None:
        return 0.0
    try:
        return max(float(getattr(clip, "duration_hint", 0.0) or 0.0), 0.0)
    except Exception:
        return 0.0


# When importer/meta leaves duration unknown (e.g. embedded FBX takes), use this for
# normalized_time and looping so native/runtime hooks see monotonic [0,1) progress.
_DEFAULT_PLAYBACK_SEC_WHEN_UNKNOWN_DURATION = 1.0


@require_component(SkinnedMeshRenderer)
@disallow_multiple
@add_component_menu("Animation/Skeletal Animator")
class SkeletalAnimator(InxComponent):
    """Drives a SkinnedMeshRenderer from a 3D AnimStateMachine (``.animfsm``)."""

    controller: AnimStateMachineRef = serialized_field(
        default=None,
        asset_type="AnimStateMachine",
        tooltip="3D AnimStateMachine controller (.animfsm)",
    )

    playback_speed: float = serialized_field(
        default=1.0,
        range=(0.0, 10.0),
        tooltip="Global playback speed multiplier",
    )

    auto_play: bool = serialized_field(
        default=True,
        tooltip="Start playing the default state on start",
    )

    cross_fade_duration: float = serialized_field(
        default=0.15,
        range=(0.0, 2.0),
        tooltip="Seconds used to blend between 3D animation states",
    )

    _parameters: Dict[str, object] = {}

    _fsm: Optional[AnimStateMachine] = None
    _skinned_renderer: Optional[SkinnedMeshRenderer] = None
    _clip_cache: Dict[str, Optional[AnimationClip3D]] = {}

    _current_state_name: str = ""
    _current_clip: Optional[AnimationClip3D] = None
    _elapsed: float = 0.0
    _playing: bool = False
    _blend_from_clip: Optional[AnimationClip3D] = None
    _blend_from_take_name: str = ""
    _blend_from_elapsed: float = 0.0
    _blend_from_speed: float = 1.0
    _blend_elapsed: float = 0.0
    _blend_duration: float = 0.0
    _last_native_take_name: str = ""
    _duration_cache: Dict[str, float] = {}
    _current_timeline = None
    _timeline_cache: Dict[str, object] = {}
    _timeline_base = None

    def awake(self):
        self._parameters = {}
        self._clip_cache = {}
        self._duration_cache = {}
        self._timeline_cache = {}
        self._current_timeline = None
        self._timeline_base = None
        self._last_native_take_name = ""
        self._current_state_name = ""
        self._current_clip = None
        self._elapsed = 0.0
        self._playing = False
        self._clear_blend_state()

    def start(self):
        self._skinned_renderer = self.game_object.get_component(SkinnedMeshRenderer)
        if not self._skinned_renderer:
            Debug.log_warning("[SkeletalAnimator] No SkinnedMeshRenderer found on this GameObject.")
            return

        self._load_controller()

        if self.auto_play and self._fsm and self._fsm.default_state:
            self.play(self._fsm.default_state)

    def update(self, delta_time: float):
        if self._current_timeline is not None:
            self._update_timeline(delta_time)
            return
        if not self._playing or not self._current_clip:
            self._sync_native_runtime_playback()
            return

        state = self._get_current_state()
        clip = self._current_clip
        speed = self.playback_speed * (state.speed if state else 1.0)
        self._elapsed += delta_time * speed
        self._advance_blend(delta_time)

        prev_norm = getattr(self, "_prev_event_norm", 0.0)
        duration = self._clip_duration(clip)
        if duration > 0.0 and self._elapsed >= duration:
            should_loop = state.loop if state else True
            if should_loop:
                post = self._elapsed % duration
                post_norm = post / duration
                self._dispatch_clip_events(clip, prev_norm, post_norm, True)
                self._prev_event_norm = post_norm
                self._try_auto_transition()
                if self._current_clip is clip and self._playing:
                    self._elapsed = post
            else:
                self._elapsed = duration
                self._dispatch_clip_events(clip, prev_norm, 1.0, False)
                self._prev_event_norm = 1.0
                self._playing = False
                self._try_auto_transition()
                self._sync_native_runtime_playback()
                return
        else:
            curr_norm = (self._elapsed / duration) if duration > 0.0 else 0.0
            self._dispatch_clip_events(clip, prev_norm, curr_norm, False)
            self._prev_event_norm = curr_norm

        self._apply_active_take()
        self._try_auto_transition()
        self._sync_native_runtime_playback()

    def _dispatch_clip_events(self, clip, prev_norm: float, curr_norm: float, looped: bool):
        """Fire any animation events on *clip* crossed this frame."""
        events = getattr(clip, "events", None)
        if not events:
            return
        try:
            from Infernux.core.animation_event import dispatch_animation_events
            dispatch_animation_events(self.game_object, events, prev_norm, curr_norm, looped)
        except Exception as exc:
            Debug.log_warning(f"[SkeletalAnimator] event dispatch error: {exc}")

    @property
    def current_state(self) -> str:
        return self._current_state_name

    @property
    def current_take_name(self) -> str:
        if self._current_clip is None:
            return ""
        return str(getattr(self._current_clip, "take_name", "") or "")

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def normalized_time(self) -> float:
        if self._current_timeline is not None:
            dur = max(1e-6, float(self._current_timeline.duration))
            if bool(getattr(self._current_timeline, "loop", True)):
                return (self._elapsed % dur) / dur
            return min(self._elapsed / dur, 1.0)
        duration = self._clip_duration(self._current_clip)
        if duration > 0.0:
            return min(self._elapsed / duration, 1.0)
        # No duration in asset — assume a neutral loop period so time/normalized are not stuck.
        t = _DEFAULT_PLAYBACK_SEC_WHEN_UNKNOWN_DURATION
        return (self._elapsed % t) / t

    def play(self, state_name: str = "") -> bool:
        if not self._fsm:
            return False
        name = state_name or self._fsm.default_state
        if not name:
            return False
        return self._enter_state(name)

    def cross_fade(self, state_name: str, duration: float) -> bool:
        """Enter *state_name* blending over *duration* seconds (Unity-style)."""
        return self._enter_state(state_name, fade_duration=max(float(duration), 0.0))

    def stop(self):
        self._playing = False
        self._clear_blend_state()
        self._sync_native_runtime_playback()

    def set_parameter(self, name: str, value: object):
        self._parameters[name] = value

    def get_parameter(self, name: str, default: object = None) -> object:
        return self._parameters.get(name, default)

    def get_bool(self, name: str) -> bool:
        return bool(self._parameters.get(name, False))

    def set_bool(self, name: str, value: bool):
        self._parameters[name] = bool(value)

    def get_float(self, name: str) -> float:
        return float(self._parameters.get(name, 0.0))

    def set_float(self, name: str, value: float):
        self._parameters[name] = float(value)

    def get_int(self, name: str) -> int:
        return int(self._parameters.get(name, 0))

    def set_int(self, name: str, value: int):
        self._parameters[name] = int(value)

    def set_trigger(self, name: str):
        self._parameters[name] = True

    def reload_controller(self):
        self._load_controller()
        if self._fsm and self._fsm.default_state:
            self.play(self._fsm.default_state)

    def on_after_deserialize(self):
        self._clip_cache = {}
        self._duration_cache = {}
        self._timeline_cache = {}
        self._current_timeline = None
        self._timeline_base = None
        self._last_native_take_name = ""
        self._parameters = {}
        self._clear_blend_state()

    def _load_controller(self):
        self._fsm = None
        self._clip_cache = {}
        self._duration_cache = {}
        self._timeline_cache = {}

        fsm = self.controller
        if fsm is None:
            return

        if fsm.mode != "3d":
            Debug.log_warning(f"[SkeletalAnimator] Controller is mode='{fsm.mode}', expected '3d'.")
        self._fsm = fsm
        self._seed_parameters_from_fsm(fsm)
        for state in fsm.states:
            self._resolve_clip(state)

    def _seed_parameters_from_fsm(self, fsm: AnimStateMachine) -> None:
        self._parameters = {}
        for p in fsm.parameters:
            if p.kind == "bool":
                self._parameters[p.name] = bool(p.default_bool)
            elif p.kind == "int":
                self._parameters[p.name] = int(p.default_int)
            else:
                self._parameters[p.name] = float(p.default_float)

    def _resolve_clip(self, state: AnimState) -> Optional[AnimationClip3D]:
        key = state.name
        if key in self._clip_cache:
            return self._clip_cache[key]

        clip_path = _resolve_clip_path(state)
        clip = None
        if clip_path:
            clip = AnimationClip3D.load(clip_path)
            if clip is None:
                Debug.log_warning(f"[SkeletalAnimator] Failed to load clip for state '{state.name}': {clip_path}")
        else:
            if state.clip_guid or state.clip_path:
                Debug.log_warning(
                    f"[SkeletalAnimator] Clip not found for state '{state.name}' "
                    f"(guid='{state.clip_guid}', path='{state.clip_path}')"
                )
        self._clip_cache[key] = clip
        return clip

    def _resolve_clip_b(self, state: AnimState) -> Optional[AnimationClip3D]:
        """Resolve the second clip (B) of a blend state."""
        key = state.name + "::B"
        if key in self._clip_cache:
            return self._clip_cache[key]
        clip_path = _resolve_clip_b_path(state)
        clip = AnimationClip3D.load(clip_path) if clip_path else None
        self._clip_cache[key] = clip
        return clip

    def _resolve_timeline(self, state: AnimState):
        """Resolve and cache the ``.animtimeline`` asset for a timeline state."""
        key = state.name
        if key in self._timeline_cache:
            return self._timeline_cache[key]
        tl = None
        path = _resolve_timeline_path(state)
        if path:
            try:
                from Infernux.core.animation_timeline import AnimationTimeline
                tl = AnimationTimeline.load(path)
            except Exception as exc:
                Debug.log_suppressed("SkeletalAnimator._resolve_timeline", exc)
            if tl is None:
                Debug.log_warning(f"[SkeletalAnimator] Failed to load timeline for state '{state.name}': {path}")
        self._timeline_cache[key] = tl
        return tl

    def _update_timeline(self, delta_time: float):
        """Advance + apply a timeline state, driving the owner GameObject transform."""
        tl = self._current_timeline
        if tl is None:
            return
        state = self._get_current_state()
        # Looping is decided by the owning FSM state, not the timeline asset.
        loop = bool(state.loop) if state is not None else True
        if self._playing:
            speed = self.playback_speed * (state.speed if state else 1.0)
            self._elapsed += delta_time * speed
            dur = max(1e-6, float(tl.duration))
            if self._elapsed >= dur:
                # Reached the end: hold the final pose and evaluate transitions
                # while progress == 1.0 *before* wrapping, so a looping timeline
                # can still leave via exit-time (fixes timeline->next stalls).
                self._apply_timeline(tl, dur)
                self._try_auto_transition()
                if self._current_timeline is tl and self._playing:
                    if loop:
                        self._elapsed = self._elapsed % dur
                    else:
                        self._elapsed = dur
                        self._playing = False
                    self._apply_timeline(tl, self._elapsed)
                return
        self._apply_timeline(tl, self._elapsed)
        self._try_auto_transition()

    def _capture_timeline_base(self):
        """Snapshot the owner's local transform as the additive base for a timeline."""
        tr = getattr(self.game_object, "transform", None)
        if tr is None:
            self._timeline_base = ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
            return
        try:
            p, r, s = tr.local_position, tr.local_euler_angles, tr.local_scale
            self._timeline_base = (
                [float(p.x), float(p.y), float(p.z)],
                [float(r.x), float(r.y), float(r.z)],
                [float(s.x), float(s.y), float(s.z)],
            )
        except Exception:
            self._timeline_base = ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0])

    def _apply_timeline(self, tl, t: float):
        """Sample *tl* at time *t* and write the local transform of the owner."""
        sampled = tl.sample(t)
        if sampled is None:
            return
        pos, rot, scl = sampled
        if getattr(tl, "apply_mode", "additive") == "additive":
            bp, br, bs = self._timeline_base or ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
            pos = [bp[0] + pos[0], bp[1] + pos[1], bp[2] + pos[2]]
            rot = [br[0] + rot[0], br[1] + rot[1], br[2] + rot[2]]
            scl = [bs[0] * scl[0], bs[1] * scl[1], bs[2] * scl[2]]
        tr = getattr(self.game_object, "transform", None)
        if tr is None:
            return
        try:
            trs = getattr(tr, "set_local_trs", None)
            if trs is not None:
                # Single boundary crossing + one subtree invalidate (no Vector3 allocs).
                trs(float(pos[0]), float(pos[1]), float(pos[2]),
                    float(rot[0]), float(rot[1]), float(rot[2]),
                    float(scl[0]), float(scl[1]), float(scl[2]))
                return
            from Infernux.lib import Vector3
            tr.local_position = Vector3(float(pos[0]), float(pos[1]), float(pos[2]))
            tr.local_euler_angles = Vector3(float(rot[0]), float(rot[1]), float(rot[2]))
            tr.local_scale = Vector3(float(scl[0]), float(scl[1]), float(scl[2]))
        except Exception as exc:
            Debug.log_suppressed("SkeletalAnimator._apply_timeline", exc)

    def _blend_state_lerp(self, state: AnimState) -> float:
        """Per-node Lerp (authored ``blend_value``), overridable via param ``<state>/Lerp``."""
        lerp = float(getattr(state, "blend_value", 0.5) or 0.0)
        pkey = f"{state.name}/Lerp"
        if pkey in self._parameters:
            try:
                lerp = float(self._parameters[pkey])
            except (TypeError, ValueError):
                pass
        return max(0.0, min(1.0, lerp))

    def _submit_blend_state(self, cpp, state: AnimState) -> bool:
        """Submit a blend-state pose (clip A lerp clip B). Returns True if handled."""
        clip_a = self._current_clip
        take_a = str(getattr(clip_a, "take_name", "") or "") if clip_a is not None else ""
        clip_b = self._resolve_clip_b(state)
        take_b = str(getattr(clip_b, "take_name", "") or "") if clip_b is not None else ""
        if not take_a and not take_b:
            return False
        lerp = self._blend_state_lerp(state)
        loop = bool(getattr(state, "loop", True))
        t = float(self._elapsed)
        normalized = float(self.normalized_time)

        # Preferred: a 2-layer pose stack (correct per-bone N-way blend, needs the
        # native pose-stack API); otherwise fall back to the 2-clip crossfade slot.
        submit_stack = getattr(cpp, "submit_pose_stack", None)
        if callable(submit_stack) and take_a and take_b:
            try:
                submit_stack([
                    {"take_name": take_a, "time": t, "weight": 1.0 - lerp, "loop": loop},
                    {"take_name": take_b, "time": t, "weight": lerp, "loop": loop},
                ])
                self._last_native_take_name = take_a
                return True
            except Exception as exc:
                Debug.log_suppressed("SkeletalAnimator._submit_blend_state.pose_stack", exc)

        submit_pose = getattr(cpp, "submit_animation_pose", None)
        if callable(submit_pose):
            submit_pose(take_a or take_b, t, normalized,
                        take_b if take_a else "", t, lerp if take_a else 0.0, loop)
            self._last_native_take_name = take_a or take_b
            return True
        return False

    def _enter_state(self, state_name: str, fade_duration: Optional[float] = None) -> bool:
        if not self._fsm:
            return False
        state = self._fsm.get_state(state_name)
        if state is None:
            Debug.log_warning(f"[SkeletalAnimator] State not found: '{state_name}'")
            return False

        if not getattr(state, "restart_same_clip", False):
            if self._playing and self._current_state_name == state_name:
                return True

        # Timeline state: drives the owner transform instead of a skeletal clip.
        if getattr(state, "kind", "clip") == "timeline":
            self._clear_blend_state()
            self._current_state_name = state_name
            self._current_clip = None
            self._current_timeline = self._resolve_timeline(state)
            self._elapsed = 0.0
            self._prev_event_norm = 0.0
            self._playing = True
            self._capture_timeline_base()  # additive deltas apply on top of this
            self._apply_active_take()  # clear skeletal take → bind pose
            if self._current_timeline is not None:
                self._apply_timeline(self._current_timeline, 0.0)
            self._sync_native_runtime_playback()
            return True

        self._current_timeline = None
        previous_state = self._get_current_state()
        previous_clip = self._current_clip if self._playing else None
        previous_elapsed = self._elapsed
        previous_speed = self._clip_effective_speed(previous_state, previous_clip)

        clip = self._resolve_clip(state)
        self._start_blend_if_needed(previous_clip, previous_elapsed, previous_speed, clip,
                                    fade_duration=fade_duration)
        self._current_state_name = state_name
        self._current_clip = clip
        self._elapsed = 0.0
        self._prev_event_norm = 0.0
        self._playing = True
        self._apply_active_take()
        self._sync_native_runtime_playback()
        return True

    def _clip_effective_speed(self, state: Optional[AnimState], clip: Optional[AnimationClip3D]) -> float:
        if clip is None:
            return 1.0
        return self.playback_speed * (state.speed if state else 1.0)

    def _clip_duration(self, clip: Optional[AnimationClip3D]) -> float:
        duration = _clip_duration_hint(clip)
        if duration > 0.0 or clip is None:
            return duration
        r = self._skinned_renderer
        cpp = getattr(r, "_cpp_component", None) if r else None
        if cpp is None:
            return 0.0
        take_name = str(getattr(clip, "take_name", "") or "")
        if not take_name:
            return 0.0
        if take_name in self._duration_cache:
            return self._duration_cache[take_name]
        try:
            get_duration = getattr(cpp, "get_animation_duration_seconds", None)
            if callable(get_duration):
                duration = max(float(get_duration(take_name) or 0.0), 0.0)
                self._duration_cache[take_name] = duration
                return duration
        except Exception:
            return 0.0
        return 0.0

    def _start_blend_if_needed(
        self,
        previous_clip: Optional[AnimationClip3D],
        previous_elapsed: float,
        previous_speed: float,
        next_clip: Optional[AnimationClip3D],
        fade_duration: Optional[float] = None,
    ) -> None:
        self._clear_blend_state()
        if previous_clip is None or next_clip is None:
            return
        prev_take = str(getattr(previous_clip, "take_name", "") or "")
        next_take = str(getattr(next_clip, "take_name", "") or "")
        # Per-transition duration (AnimTransition.duration / cross_fade()) wins;
        # the component-level cross_fade_duration is only the fallback.
        if fade_duration is not None:
            duration = max(float(fade_duration), 0.0)
        else:
            duration = max(float(getattr(self, "cross_fade_duration", 0.0) or 0.0), 0.0)
        # Same-take fades are supported natively (different sample times), so
        # only an actually-missing take disables blending.
        if duration <= 0.0 or not prev_take or not next_take:
            return
        self._blend_from_clip = previous_clip
        self._blend_from_take_name = prev_take
        self._blend_from_elapsed = max(float(previous_elapsed), 0.0)
        self._blend_from_speed = float(previous_speed)
        self._blend_elapsed = 0.0
        self._blend_duration = duration

    def _clear_blend_state(self) -> None:
        self._blend_from_clip = None
        self._blend_from_take_name = ""
        self._blend_from_elapsed = 0.0
        self._blend_from_speed = 1.0
        self._blend_elapsed = 0.0
        self._blend_duration = 0.0

    def _advance_blend(self, delta_time: float) -> None:
        if self._blend_from_clip is None or self._blend_duration <= 0.0:
            return
        self._blend_elapsed += max(float(delta_time), 0.0)
        self._blend_from_elapsed += max(float(delta_time), 0.0) * self._blend_from_speed
        prev_duration = self._clip_duration(self._blend_from_clip)
        if prev_duration > 0.0 and self._blend_from_elapsed >= prev_duration:
            self._blend_from_elapsed %= prev_duration
        if self._blend_elapsed >= self._blend_duration:
            self._clear_blend_state()

    def _apply_active_take(self):
        if not self._skinned_renderer:
            return
        take_name = ""
        clip = self._current_clip
        if clip is not None:
            take_name = str(getattr(clip, "take_name", "") or "")
            if take_name == self._last_native_take_name:
                return

            r = self._skinned_renderer
            renderer_guid = str(getattr(r, "source_model_guid", "") or "")
            db = _get_asset_database()
            msg = _skinned_mismatch_message(db, clip, renderer_guid)
            if msg:
                Debug.log_warning(msg)

        self._skinned_renderer.active_take_name = take_name
        self._last_native_take_name = take_name

    def _sync_native_runtime_playback(self) -> None:
        r = self._skinned_renderer
        if not r:
            return
        cpp = getattr(r, "_cpp_component", None)
        if cpp is None:
            return
        # Blend states output a continuous A↔B lerp via their own Lerp value,
        # independent of transition crossfades.
        state = self._get_current_state()
        if state is not None and getattr(state, "kind", "clip") == "blend":
            if self._submit_blend_state(cpp, state):
                return
        submit_pose = getattr(cpp, "submit_animation_pose", None)
        if callable(submit_pose):
            # A finished non-looping clip keeps submitting its take with
            # loop=False so the native sampler holds the END pose; an empty
            # take renders the bind pose (mesh always stays visible).
            has_clip = self._current_clip is not None and bool(self.current_take_name)
            take_name = self.current_take_name if has_clip else ""
            state = self._get_current_state()
            loop = bool(state.loop) if state is not None else True
            normalized = float(self.normalized_time) if take_name else 0.0
            blend_take = ""
            blend_time = 0.0
            blend_weight = 0.0
            if self._blend_from_clip is not None and self._blend_duration > 0.0:
                progress = min(max(self._blend_elapsed / self._blend_duration, 0.0), 1.0)
                blend_take = self._blend_from_take_name
                blend_time = float(self._blend_from_elapsed)
                blend_weight = float(1.0 - progress)
            submit_pose(
                take_name,
                float(self._elapsed) if take_name else 0.0,
                normalized,
                blend_take,
                blend_time,
                blend_weight,
                loop,
            )
            self._last_native_take_name = take_name
            return

        if self._playing and self._current_clip is not None:
            cpp.runtime_animation_time = float(self._elapsed)
            cpp.runtime_animation_normalized_time = float(self.normalized_time)
            if self._blend_from_clip is not None and self._blend_duration > 0.0:
                progress = min(max(self._blend_elapsed / self._blend_duration, 0.0), 1.0)
                cpp.blend_take_name = self._blend_from_take_name
                cpp.blend_animation_time = float(self._blend_from_elapsed)
                cpp.blend_weight = float(1.0 - progress)
            else:
                clear = getattr(cpp, "clear_animation_blend", None)
                if callable(clear):
                    clear()
        else:
            cpp.runtime_animation_time = 0.0
            cpp.runtime_animation_normalized_time = 0.0
            clear = getattr(cpp, "clear_animation_blend", None)
            if callable(clear):
                clear()

    def _get_current_state(self) -> Optional[AnimState]:
        if self._fsm and self._current_state_name:
            return self._fsm.get_state(self._current_state_name)
        return None

    def _exit_time_gate_ok(self, state: AnimState) -> bool:
        if self._current_timeline is not None:
            dur = max(1e-6, float(self._current_timeline.duration))
            thr = max(0.0, min(1.0, float(getattr(state, "exit_time_normalized", 1.0))))
            progress = min(max(self._elapsed / dur, 0.0), 1.0)
            return progress + 1e-7 >= thr
        duration = self._clip_duration(self._current_clip)
        if duration <= 0.0:
            return True
        thr = float(getattr(state, "exit_time_normalized", 1.0))
        thr = max(0.0, min(1.0, thr))
        progress = min(max(self._elapsed / duration, 0.0), 1.0)
        return progress + 1e-7 >= thr

    def _try_auto_transition(self):
        state = self._get_current_state()
        if not state:
            return
        if not self._exit_time_gate_ok(state):
            return
        for tr in state.transitions:
            if self._evaluate_condition(tr):
                self._consume_triggers(tr.condition)
                # AnimTransition.duration (authored in the FSM editor) drives
                # this specific fade; <= 0 falls back to cross_fade_duration.
                tr_duration = float(getattr(tr, "duration", 0.0) or 0.0)
                self._enter_state(tr.target_state,
                                  fade_duration=tr_duration if tr_duration > 0.0 else None)
                return

    def _evaluate_condition(self, transition: AnimTransition) -> bool:
        cond = transition.condition.strip()

        if not cond:
            duration = self._clip_duration(self._current_clip)
            if duration <= 0.0:
                return False
            state = self._get_current_state()
            should_loop = state.loop if state else True
            if self._current_clip and not should_loop:
                return self._elapsed >= duration
            return False

        ctx = dict(self._parameters)
        ctx["time"] = self._elapsed
        ctx["normalized_time"] = self.normalized_time
        ctx["state"] = self._current_state_name

        # Safe structured evaluation — no eval()/builtins/attribute access.
        try:
            return evaluate_anim_condition(cond, ctx)
        except Exception as exc:
            Debug.log_warning(
                f"[SkeletalAnimator] Condition error in '{self._current_state_name}': "
                f"'{cond}' -> {exc}"
            )
            return False

    def _consume_triggers(self, condition: str):
        # Identifier-boundary matching: a trigger named "attack" must NOT be
        # consumed by a condition that references "is_attacking".
        import re
        identifiers = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", condition or ""))
        for name, val in list(self._parameters.items()):
            if val is True and name in identifiers:
                self._parameters[name] = False
