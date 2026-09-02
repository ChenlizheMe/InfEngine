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
from Infernux.components.fields import serialized_field
from Infernux.components.decorators import disallow_multiple, add_component_menu
from Infernux.components.builtin.skinned_mesh_renderer import SkinnedMeshRenderer
from Infernux.core.anim_state_machine import (
    AnimStateMachine, AnimState, AnimTransition,
)
from Infernux.core.animation_clip3d import AnimationClip3D, resolve_disk_path_for_guid_string
from Infernux.core.asset_ref import AnimStateMachineRef
from Infernux.debug import Debug
from Infernux.graph.types import ValueType


def _animation_source_guid(clip: Optional[AnimationClip3D]) -> str:
    """Return the runtime model GUID that owns *clip*'s animation take."""
    if clip is None:
        return ""
    return str(clip.source_model_guid or "").strip()


def _get_asset_database():
    from Infernux.core.assets import AssetManager

    return AssetManager.require_asset_database()


def _resolve_clip_path_from(guid: str, path_hint: str) -> Optional[str]:
    """Resolve a clip GUID / path-hint to a usable disk path (or embedded take id)."""
    if guid:
        db = _get_asset_database()
        return resolve_disk_path_for_guid_string(db, guid) or None
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
        return db.get_path_from_guid(guid) or None
    return path or None


def _clip_duration_hint(clip: Optional[AnimationClip3D]) -> float:
    if clip is None:
        return 0.0
    return max(float(clip.duration_hint), 0.0)


# When importer/meta leaves duration unknown (e.g. embedded FBX takes), use this for
# normalized_time and looping so native/runtime hooks see monotonic [0,1) progress.
_DEFAULT_PLAYBACK_SEC_WHEN_UNKNOWN_DURATION = 1.0


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
    _skinned_renderers: list[SkinnedMeshRenderer] = []
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
    _last_native_pose_key = None
    _duration_cache: Dict[tuple[str, str], float] = {}
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
        self._last_native_pose_key = None
        self._current_state_name = ""
        self._current_clip = None
        self._elapsed = 0.0
        self._playing = False
        self._skinned_renderer = None
        self._skinned_renderers = []
        self._clear_blend_state()

    def start(self):
        # Play-mode scene replacement can invalidate an edit-scene wrapper after
        # the Python component has already been restored.  Resolve through the
        # live GameObject instead of trusting a serialized/cached wrapper.
        self._skinned_renderer = None
        self._skinned_renderers = []
        renderers = self._resolve_skinned_renderers(force=True)
        if not renderers:
            Debug.log_warning(
                "[SkeletalAnimator] No SkinnedMeshRenderer found on this GameObject or its descendants."
            )
            return

        self._load_controller()

        if self.auto_play and self._fsm and self._fsm.default_state:
            self.play(self._fsm.default_state)

    def update(self, delta_time: float):
        if self._current_timeline is not None:
            self._update_timeline(delta_time)
            return
        if not self._playing or not self._current_clip:
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

    def cross_fade(
        self,
        state_name: str,
        duration: float,
        *,
        preserve_phase: bool = False,
    ) -> bool:
        """Blend into *state_name*, optionally preserving a looping gait phase.

        ``preserve_phase`` is intended for related cyclic states such as Walk
        and Run.  It maps the outgoing normalized time onto the incoming clip
        instead of restarting that clip at frame zero, preventing planted feet
        from snapping during locomotion transitions.
        """
        return self._enter_state(
            state_name,
            fade_duration=max(float(duration), 0.0),
            preserve_phase=bool(preserve_phase),
        )

    def stop(self):
        self._playing = False
        self._clear_blend_state()
        self._last_native_pose_key = None
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
        self._last_native_pose_key = None
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
            if p.value_type.value_type is ValueType.BOOL:
                self._parameters[p.name] = bool(p.default)
            elif p.value_type.value_type is ValueType.I32:
                self._parameters[p.name] = int(p.default)
            else:
                self._parameters[p.name] = float(p.default)

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
            from Infernux.core.animation_timeline import AnimationTimeline
            tl = AnimationTimeline.load(path)
            if tl is None:
                raise RuntimeError(
                    f"SkeletalAnimator could not load timeline for state {state.name!r}: {path}"
                )
        self._timeline_cache[key] = tl
        return tl

    def _update_timeline(self, delta_time: float):
        """Advance + apply a timeline state, driving the owner GameObject transform."""
        tl = self._current_timeline
        if tl is None or not self._playing:
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
        transform = self.game_object.transform
        position = transform.local_position
        rotation = transform.local_euler_angles
        scale = transform.local_scale
        self._timeline_base = (
            [float(position.x), float(position.y), float(position.z)],
            [float(rotation.x), float(rotation.y), float(rotation.z)],
            [float(scale.x), float(scale.y), float(scale.z)],
        )

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
        transform = self.game_object.transform
        pose = tuple(float(value) for value in (*pos, *rot, *scl))
        if pose == getattr(self, "_last_timeline_pose", None):
            return
        transform.set_local_trs(*pose)
        self._last_timeline_pose = pose

    def _blend_state_lerp(self, state: AnimState) -> float:
        """Per-node Lerp (authored ``blend_value``), overridable via param ``<state>/Lerp``."""
        lerp = float(getattr(state, "blend_value", 0.5) or 0.0)
        pkey = f"{state.name}/Lerp"
        if pkey in self._parameters:
            lerp = float(self._parameters[pkey])
        return max(0.0, min(1.0, lerp))

    def _submit_blend_state(self, native_renderers, state: AnimState) -> bool:
        """Submit a blend-state pose (clip A lerp clip B). Returns True if handled."""
        clip_a = self._current_clip
        take_a = str(getattr(clip_a, "take_name", "") or "") if clip_a is not None else ""
        source_a = _animation_source_guid(clip_a)
        clip_b = self._resolve_clip_b(state)
        take_b = str(getattr(clip_b, "take_name", "") or "") if clip_b is not None else ""
        source_b = _animation_source_guid(clip_b)
        if not take_a and not take_b:
            return False
        lerp = self._blend_state_lerp(state)
        loop = bool(getattr(state, "loop", True))
        t = float(self._elapsed)
        normalized = float(self.normalized_time)

        native_renderers = [cpp for cpp in native_renderers if cpp is not None]
        if not native_renderers:
            return False
        if take_a and take_b:
            pose_key = ("stack", self._playing, take_a, source_a, take_b, source_b, t, lerp, loop)
            if pose_key == self._last_native_pose_key:
                return True
            layers = [
                {"take_name": take_a, "source_model_guid": source_a,
                 "time": t, "weight": 1.0 - lerp, "loop": loop},
                {"take_name": take_b, "source_model_guid": source_b,
                 "time": t, "weight": lerp, "loop": loop},
            ]
            for cpp in native_renderers:
                cpp.submit_pose_stack(layers)
            self._last_native_take_name = take_a
            self._last_native_pose_key = pose_key
            return True

        pose_key = (
            "blend", self._playing, take_a or take_b, t, normalized,
            "", 0.0, 0.0, loop, source_a if take_a else source_b, "",
        )
        if pose_key == self._last_native_pose_key:
            return True
        for cpp in native_renderers:
            cpp.submit_animation_pose(
                take_a or take_b,
                t,
                normalized,
                "",
                0.0,
                0.0,
                loop,
                source_a if take_a else source_b,
                "",
            )
        self._last_native_take_name = take_a or take_b
        self._last_native_pose_key = pose_key
        return True

    def _enter_state(
        self,
        state_name: str,
        fade_duration: Optional[float] = None,
        *,
        preserve_phase: bool = False,
    ) -> bool:
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
            self._last_timeline_pose = None
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
        next_elapsed = 0.0
        if preserve_phase and previous_clip is not None and clip is not None:
            previous_duration = self._clip_duration(previous_clip)
            next_duration = self._clip_duration(clip)
            if previous_duration > 0.0 and next_duration > 0.0:
                phase = (max(float(previous_elapsed), 0.0) % previous_duration) / previous_duration
                next_elapsed = phase * next_duration
        self._start_blend_if_needed(previous_clip, previous_elapsed, previous_speed, clip,
                                    fade_duration=fade_duration)
        self._current_state_name = state_name
        self._current_clip = clip
        self._elapsed = next_elapsed
        self._prev_event_norm = (next_elapsed / self._clip_duration(clip)) if next_elapsed > 0.0 else 0.0
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
        r = self._resolve_skinned_renderer()
        cpp = r._get_bound_native_component() if r is not None else None
        if cpp is None:
            return 0.0
        take_name = str(getattr(clip, "take_name", "") or "")
        if not take_name:
            return 0.0
        source_guid = _animation_source_guid(clip)
        cache_key = (source_guid, take_name)
        if cache_key in self._duration_cache:
            return self._duration_cache[cache_key]
        duration = max(float(cpp.get_animation_duration_seconds(take_name, source_guid)), 0.0)
        self._duration_cache[cache_key] = duration
        return duration

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

    @staticmethod
    def _renderer_is_live(renderer) -> bool:
        try:
            return renderer is not None and renderer._get_bound_native_component() is not None
        except (AttributeError, ReferenceError):
            return False

    def _resolve_skinned_renderers(self, *, force: bool = False):
        cached = [] if force else list(getattr(self, "_skinned_renderers", []) or [])
        if cached and all(self._renderer_is_live(renderer) for renderer in cached):
            return cached

        try:
            game_object = self.game_object
        except (AttributeError, ReferenceError, RuntimeError):
            game_object = None
        if game_object is None:
            self._skinned_renderer = None
            self._skinned_renderers = []
            return []

        local_renderer = None
        try:
            local_renderer = game_object.get_component(SkinnedMeshRenderer)
        except (AttributeError, ReferenceError):
            pass

        descendants = []
        try:
            pending = list(game_object.get_children() or [])
        except (AttributeError, ReferenceError, RuntimeError):
            pending = []
        while pending:
            child = pending.pop(0)
            try:
                renderer = child.get_component(SkinnedMeshRenderer)
            except (AttributeError, ReferenceError):
                renderer = None
            if self._renderer_is_live(renderer):
                descendants.append(renderer)
            try:
                pending.extend(list(child.get_children() or []))
            except (AttributeError, ReferenceError, RuntimeError):
                pass

        renderers = []
        if self._renderer_is_live(local_renderer):
            renderers.append(local_renderer)
        renderers.extend(renderer for renderer in descendants if renderer not in renderers)
        self._skinned_renderers = renderers
        self._skinned_renderer = renderers[0] if renderers else None
        return renderers

    def _resolve_skinned_renderer(self, *, force: bool = False):
        renderers = self._resolve_skinned_renderers(force=force)
        return renderers[0] if renderers else None

    def _apply_active_take(self):
        # The source model and take form one atomic native submission. Setting
        # active_take_name first would briefly look the take up on the render
        # model and recreate the old mesh/animation coupling. The following
        # _sync_native_runtime_playback call performs the complete update.
        return

    def _sync_native_runtime_playback(self) -> None:
        renderers = self._resolve_skinned_renderers()
        if not renderers:
            return
        native_renderers = []
        for renderer in renderers:
            try:
                cpp = renderer._get_bound_native_component()
            except (AttributeError, ReferenceError):
                cpp = None
            if cpp is not None:
                native_renderers.append(cpp)
        if not native_renderers:
            return
        # Blend states output a continuous A↔B lerp via their own Lerp value,
        # independent of transition crossfades.
        state = self._get_current_state()
        if state is not None and getattr(state, "kind", "clip") == "blend":
            if self._submit_blend_state(native_renderers, state):
                return
        # Continuous playback has one current native submission path. Explicit
        # runtime_animation_time assignment is reserved for discontinuous seek.
        has_clip = self._current_clip is not None and bool(self.current_take_name)
        take_name = self.current_take_name if has_clip else ""
        source_guid = _animation_source_guid(self._current_clip) if take_name else ""
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
        blend_source_guid = _animation_source_guid(self._blend_from_clip) if blend_take else ""
        pose_key = (
            "clip", self._playing, take_name, float(self._elapsed), normalized,
            blend_take, blend_time, blend_weight, loop, source_guid, blend_source_guid,
        )
        if pose_key == self._last_native_pose_key:
            return
        for cpp in native_renderers:
            cpp.submit_animation_pose(
                take_name,
                float(self._elapsed) if take_name else 0.0,
                normalized,
                blend_take,
                blend_time,
                blend_weight,
                loop,
                source_guid,
                blend_source_guid,
            )
        self._last_native_take_name = take_name
        self._last_native_pose_key = pose_key

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
                self._consume_triggers(tr)
                # AnimTransition.duration (authored in the FSM editor) drives
                # this specific fade; <= 0 falls back to cross_fade_duration.
                tr_duration = float(getattr(tr, "duration", 0.0) or 0.0)
                self._enter_state(tr.target_state,
                                  fade_duration=tr_duration if tr_duration > 0.0 else None,
                                  preserve_phase=bool(getattr(
                                      tr, "synchronize_normalized_time", False,
                                  )))
                return

    def _evaluate_condition(self, transition: AnimTransition) -> bool:
        if not transition.conditions:
            duration = self._clip_duration(self._current_clip)
            if duration <= 0.0:
                return False
            state = self._get_current_state()
            should_loop = state.loop if state else True
            if self._current_clip and not should_loop:
                return self._elapsed >= duration
            return False
        return bool(
            self._fsm
            and self._fsm.evaluate_transition_conditions(
                transition, self._parameters
            )
        )

    def _consume_triggers(self, transition: AnimTransition):
        names = set(
            self._fsm.transition_parameter_names(transition)
            if self._fsm is not None
            else ()
        )
        for name, val in list(self._parameters.items()):
            if val is True and name in names:
                self._parameters[name] = False
