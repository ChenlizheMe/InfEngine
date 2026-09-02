"""
SpiritAnimator — runtime 2D animation state machine controller.

Drives a :class:`SpriteRenderer` by evaluating an :class:`AnimStateMachine`
every frame.  Loads the FSM from a ``.animfsm`` file, resolves each state's
animation clip, and advances the current clip's frame index on the
SpriteRenderer.

Usage::

    animator = game_object.add_component(SpiritAnimator)
    animator.controller = AnimStateMachineRef(path_hint="Assets/Animations/player.animfsm")
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from Infernux.components.component import InxComponent
from Infernux.components.fields import serialized_field, FieldType
from Infernux.components.decorators import require_component, disallow_multiple, add_component_menu
from Infernux.components.builtin.sprite_renderer import SpriteRenderer
from Infernux.core.anim_state_machine import (
    AnimStateMachine, AnimState, AnimTransition,
)
from Infernux.core.animation_clip import AnimationClip
from Infernux.core.asset_ref import AnimStateMachineRef
from Infernux.debug import Debug
from Infernux.engine.path_utils import same_path
from Infernux.graph.types import ValueType


def _get_asset_database():
    from Infernux.core.assets import AssetManager

    return AssetManager.require_asset_database()


def _resolve_clip_path(state: AnimState) -> Optional[str]:
    """Resolve an AnimState's clip reference to an absolute file path."""
    # Try GUID first
    if state.clip_guid:
        db = _get_asset_database()
        path = db.get_path_from_guid(state.clip_guid)
        return path if path and os.path.isfile(path) else None
    if state.clip_path and os.path.isfile(state.clip_path):
        return state.clip_path
    return None


def _resolve_timeline_path(state: AnimState) -> Optional[str]:
    """Resolve a timeline state's ``.animtimeline`` asset to a disk path."""
    guid = getattr(state, "timeline_guid", "") or ""
    path = (getattr(state, "timeline_path", "") or "").strip()
    if guid:
        db = _get_asset_database()
        return db.get_path_from_guid(guid) or None
    return path or None


@require_component(SpriteRenderer)
@disallow_multiple
@add_component_menu("Animation/Spirit Animator")
class SpiritAnimator(InxComponent):
    """Runtime controller that drives a SpriteRenderer from a 2D AnimStateMachine."""

    # ── Serialized fields (shown in Inspector) ──────────────────────

    controller: AnimStateMachineRef = serialized_field(
        default=None,
        asset_type="AnimStateMachine",
        tooltip="2D AnimStateMachine controller (.animfsm)",
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

    # ── Runtime parameters (user-settable, used in conditions) ──────

    _parameters: Dict[str, object] = {}

    # ── Private runtime state ───────────────────────────────────────

    _fsm: Optional[AnimStateMachine] = None
    _sprite_renderer: Optional[SpriteRenderer] = None
    _clip_cache: Dict[str, Optional[AnimationClip]] = {}

    _current_state_name: str = ""
    _current_clip: Optional[AnimationClip] = None
    _elapsed: float = 0.0
    _playing: bool = False
    _current_timeline = None
    _timeline_cache: Dict[str, object] = {}
    _timeline_base = None
    _last_timeline_pose = None

    # ── Lifecycle ───────────────────────────────────────────────────

    def awake(self):
        self._parameters = {}
        self._clip_cache = {}
        self._timeline_cache = {}
        self._current_timeline = None
        self._timeline_base = None
        self._last_timeline_pose = None
        self._current_state_name = ""
        self._current_clip = None
        self._elapsed = 0.0
        self._playing = False
        self._subscribe_asset_events()

    def on_destroy(self):
        self._unsubscribe_asset_events()

    def start(self):
        self._sprite_renderer = self.game_object.get_component(SpriteRenderer)
        if not self._sprite_renderer:
            Debug.log_warning("[SpiritAnimator] No SpriteRenderer found on this GameObject.")
            return

        self._load_controller()

        if self.auto_play and self._fsm and self._fsm.default_state:
            self.play(self._fsm.default_state)

    def update(self, delta_time: float):
        if self._current_timeline is not None:
            self._update_timeline(delta_time)
            return
        if not self._playing or not self._current_clip or not self._sprite_renderer:
            return

        state = self._get_current_state()
        speed = self.playback_speed * (state.speed if state else 1.0)

        # Advance elapsed time
        self._elapsed += delta_time * speed

        clip = self._current_clip
        if clip.fps <= 0 or clip.frame_count == 0:
            return

        duration = clip.duration
        prev_norm = getattr(self, "_prev_event_norm", 0.0)

        # Handle looping / clip end
        if self._elapsed >= duration:
            state = self._get_current_state()
            should_loop = state.loop if state else clip.loop
            if should_loop:
                post = (self._elapsed % duration) if duration > 0 else 0.0
                post_norm = (post / duration) if duration > 0 else 0.0
                # Fire events crossed in (prev, 1] ∪ [0, post] for the looped clip.
                self._dispatch_clip_events(clip, prev_norm, post_norm, True)
                self._prev_event_norm = post_norm
                # Evaluate transitions at loop boundary while progress == 1.0,
                # BEFORE wrapping elapsed (preserves exit-time gating).
                self._try_auto_transition()
                if self._current_clip is clip and self._playing:
                    self._elapsed = post
            else:
                self._elapsed = duration
                self._dispatch_clip_events(clip, prev_norm, 1.0, False)
                self._prev_event_norm = 1.0
                self._playing = False
                self._try_auto_transition()
                return
        else:
            curr_norm = (self._elapsed / duration) if duration > 0 else 0.0
            self._dispatch_clip_events(clip, prev_norm, curr_norm, False)
            self._prev_event_norm = curr_norm

        # Compute and apply the stable source-frame ID. Only touch the renderer when the
        # frame actually changed — sync_visual() walks the material-sync
        # chain and is wasteful to run every tick for unchanged frames.
        clip = self._current_clip
        if not self._playing or clip is None or clip.fps <= 0 or clip.frame_count == 0:
            return
        raw_frame = int(self._elapsed * clip.fps)
        raw_frame = min(raw_frame, clip.frame_count - 1)
        sprite_frame = clip.frames[raw_frame].sprite_frame_id
        if sprite_frame != getattr(self, "_last_applied_frame", None):
            self._sprite_renderer.frame_id = sprite_frame
            self._sprite_renderer.sync_visual()
            self._last_applied_frame = sprite_frame

        # Check transitions every frame (for condition-driven transitions)
        self._try_auto_transition()

    def _dispatch_clip_events(self, clip, prev_norm: float, curr_norm: float, looped: bool):
        """Fire any animation events on *clip* crossed this frame."""
        events = getattr(clip, "events", None)
        if not events:
            return
        try:
            from Infernux.core.animation_event import dispatch_animation_events
            dispatch_animation_events(self.game_object, events, prev_norm, curr_norm, looped)
        except Exception as exc:
            Debug.log_warning(f"[SpiritAnimator] event dispatch error: {exc}")

    # ── Public API ──────────────────────────────────────────────────

    @property
    def current_state(self) -> str:
        """Name of the active FSM state."""
        return self._current_state_name

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def normalized_time(self) -> float:
        """Current playback position in [0, 1]."""
        if self._current_timeline is not None:
            dur = max(1e-6, float(self._current_timeline.duration))
            if bool(getattr(self._current_timeline, "loop", True)):
                return (self._elapsed % dur) / dur
            return min(self._elapsed / dur, 1.0)
        if self._current_clip and self._current_clip.duration > 0:
            return min(self._elapsed / self._current_clip.duration, 1.0)
        return 0.0

    def play(self, state_name: str = "") -> bool:
        """Transition immediately to *state_name* (or default state)."""
        if not self._fsm:
            return False
        name = state_name or self._fsm.default_state
        if not name:
            return False
        return self._enter_state(name)

    def stop(self):
        """Stop playback.  The current frame stays on screen."""
        self._playing = False

    def set_parameter(self, name: str, value: object):
        """Set a named parameter that transition conditions can reference."""
        self._parameters[name] = value

    def get_parameter(self, name: str, default: object = None) -> object:
        """Get a named parameter value."""
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
        """Set a trigger parameter (auto-clears after consumed by a transition)."""
        self._parameters[name] = True

    def reload_controller(self):
        """Force-reload the FSM from disk."""
        self._load_controller()
        if self._fsm and self._fsm.default_state:
            self.play(self._fsm.default_state)

    # ── Serialization hooks ─────────────────────────────────────────

    def on_after_deserialize(self):
        self._clip_cache = {}
        self._timeline_cache = {}
        self._current_timeline = None
        self._timeline_base = None
        self._last_timeline_pose = None
        self._parameters = {}

    # ── Internals ───────────────────────────────────────────────────

    def _subscribe_asset_events(self) -> None:
        try:
            from Infernux.engine.interaction import AssetMutationService

            previous = getattr(self, "_asset_mutation_service", None)
            if previous is not None:
                previous.remove_component_listener(self._on_asset_changed)
            service = AssetMutationService.instance()
            self._asset_mutation_service = service
            if service is not None:
                service.add_component_listener(self._on_asset_changed)
        except (AttributeError, ImportError, RuntimeError, TypeError):
            pass

    def _unsubscribe_asset_events(self) -> None:
        try:
            service = getattr(self, "_asset_mutation_service", None)
            if service is not None:
                service.remove_component_listener(self._on_asset_changed)
            self._asset_mutation_service = None
        except (AttributeError, ImportError, RuntimeError, TypeError):
            pass

    @staticmethod
    def _event_asset_path(file_path: str) -> str:
        path = str(file_path or "").strip()
        return path[:-5] if path.casefold().endswith(".meta") else path

    @staticmethod
    def _state_clip_reference_path(state: AnimState) -> str:
        if state.clip_guid:
            database = _get_asset_database()
            if database is not None:
                try:
                    path = str(
                        database.get_path_from_guid(state.clip_guid) or ""
                    ).strip()
                    if path:
                        return path
                except (KeyError, RuntimeError, TypeError, ValueError):
                    pass
        return str(state.clip_path or "").strip()

    def _apply_current_clip_frame(self) -> None:
        clip = self._current_clip
        renderer = self._sprite_renderer
        if clip is None or renderer is None or clip.frame_count <= 0 or clip.fps <= 0:
            return
        frame_index = min(
            max(0, int(self._elapsed * clip.fps)),
            clip.frame_count - 1,
        )
        frame_id = clip.frames[frame_index].sprite_frame_id
        renderer.frame_id = frame_id
        renderer.sync_visual()
        self._last_applied_frame = frame_id

    def _reload_clip_asset(self, asset_path: str) -> bool:
        fsm = self._fsm
        if fsm is None:
            return False
        affected = tuple(
            state
            for state in fsm.states
            if getattr(state, "kind", "clip") != "timeline"
            and same_path(self._state_clip_reference_path(state), asset_path)
        )
        if not affected:
            return False
        replacement = AnimationClip.load(asset_path)
        if replacement is None:
            Debug.log_error(
                f"[SpiritAnimator] AnimationClip hot reload rejected; "
                f"the previous runtime clip remains active: {asset_path}"
            )
            return True

        normalized = self.normalized_time
        playing = self._playing
        current_name = self._current_state_name
        for state in affected:
            self._clip_cache[state.name] = replacement
        if any(state.name == current_name for state in affected):
            self._current_clip = replacement
            duration = replacement.duration
            self._elapsed = min(max(0.0, normalized) * duration, duration)
            self._prev_event_norm = min(max(0.0, normalized), 1.0)
            self._playing = playing
            self._last_applied_frame = None
            self._apply_current_clip_frame()
        return True

    def _reload_controller_asset(self, asset_path: str) -> bool:
        fsm = self._fsm
        if fsm is None or not same_path(getattr(fsm, "file_path", ""), asset_path):
            return False
        replacement = AnimStateMachine.load(asset_path)
        if replacement is None:
            Debug.log_error(
                f"[SpiritAnimator] controller hot reload rejected; "
                f"the previous runtime controller remains active: {asset_path}"
            )
            return True

        state_name = self._current_state_name
        normalized = self.normalized_time
        playing = self._playing
        parameters = dict(self._parameters)
        self._fsm = replacement
        self._clip_cache = {}
        self._timeline_cache = {}
        self._seed_parameters_from_fsm(replacement)
        self._parameters.update(
            (name, value)
            for name, value in parameters.items()
            if name in self._parameters
        )
        for candidate in replacement.states:
            self._precache_state_asset(candidate)

        state = replacement.get_state(state_name) if state_name else None
        preserved_state = state is not None
        if state is None:
            state_name = replacement.default_state
            state = replacement.get_state(state_name) if state_name else None
            normalized = 0.0
        self._current_state_name = state_name or ""
        self._current_clip = None
        self._current_timeline = None
        if state is not None and getattr(state, "kind", "clip") == "timeline":
            self._current_timeline = self._resolve_timeline(state)
            duration = (
                float(self._current_timeline.duration)
                if self._current_timeline is not None
                else 0.0
            )
            if not preserved_state:
                self._capture_timeline_base()
        else:
            self._current_clip = self._resolve_clip(state) if state is not None else None
            duration = (
                self._current_clip.duration
                if self._current_clip is not None
                else 0.0
            )
        self._elapsed = min(max(0.0, normalized) * duration, duration)
        self._prev_event_norm = min(max(0.0, normalized), 1.0)
        self._playing = bool(playing and state is not None)
        self._last_applied_frame = None
        if self._current_timeline is not None:
            self._apply_timeline(self._current_timeline, self._elapsed)
        else:
            self._apply_current_clip_frame()
        return True

    def _on_asset_changed(self, change) -> None:
        from Infernux.engine.interaction import AssetMutationKind, iter_asset_mutations

        for mutation in iter_asset_mutations(change):
            if mutation.kind is AssetMutationKind.DELETED:
                continue
            asset_path = self._event_asset_path(mutation.path)
            if not asset_path:
                continue
            try:
                if self._reload_controller_asset(asset_path):
                    continue
                self._reload_clip_asset(asset_path)
            except Exception as exc:
                Debug.log_error(
                    f"[SpiritAnimator] asset hot reload failed for '{asset_path}': {exc}"
                )

    def _load_controller(self):
        """Load the AnimStateMachine from the *controller* asset reference."""
        self._fsm = None
        self._clip_cache = {}
        self._timeline_cache = {}

        # self.controller auto-resolves the AnimStateMachineRef via the
        # descriptor, so *fsm* is already the loaded AnimStateMachine (or None).
        fsm = self.controller
        if fsm is None:
            return

        if fsm.mode != "2d":
            Debug.log_warning(
                f"[SpiritAnimator] Controller is mode='{fsm.mode}', expected '2d'."
            )
        self._fsm = fsm
        self._seed_parameters_from_fsm(fsm)
        # Pre-cache the state-owned asset without treating Timeline states as clips.
        for state in fsm.states:
            self._precache_state_asset(state)

    def _precache_state_asset(self, state: AnimState) -> None:
        if getattr(state, "kind", "clip") == "timeline":
            self._resolve_timeline(state)
        else:
            self._resolve_clip(state)

    def _seed_parameters_from_fsm(self, fsm: AnimStateMachine) -> None:
        """Expose FSM parameter defaults in condition eval (``eval`` ctx)."""
        self._parameters = {}
        for p in fsm.parameters:
            if p.value_type.value_type is ValueType.BOOL:
                self._parameters[p.name] = bool(p.default)
            elif p.value_type.value_type is ValueType.I32:
                self._parameters[p.name] = int(p.default)
            else:
                self._parameters[p.name] = float(p.default)

    def _resolve_clip(self, state: AnimState) -> Optional[AnimationClip]:
        """Resolve and cache the AnimationClip for an FSM state."""
        key = state.name
        if key in self._clip_cache:
            return self._clip_cache[key]

        clip_path = _resolve_clip_path(state)
        clip = None
        if clip_path:
            clip = AnimationClip.load(clip_path)
            if clip is None:
                Debug.log_warning(
                    f"[SpiritAnimator] Failed to load clip for state '{state.name}': {clip_path}"
                )
        else:
            if state.clip_guid or state.clip_path:
                Debug.log_warning(
                    f"[SpiritAnimator] Clip not found for state '{state.name}' "
                    f"(guid='{state.clip_guid}', path='{state.clip_path}')"
                )
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
            except Exception:
                tl = None
            if tl is None:
                Debug.log_warning(f"[SpiritAnimator] Failed to load timeline for state '{state.name}': {path}")
        self._timeline_cache[key] = tl
        return tl

    def _update_timeline(self, delta_time: float):
        """Advance + apply a timeline state, driving the owner GameObject transform."""
        tl = self._current_timeline
        if tl is None or not self._playing:
            return
        state = self._get_current_state()
        loop = bool(state.loop) if state is not None else True
        if self._playing:
            speed = self.playback_speed * (state.speed if state else 1.0)
            self._elapsed += delta_time * speed
            dur = max(1e-6, float(tl.duration))
            if self._elapsed >= dur:
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
        pose = tuple(float(value) for value in (*pos, *rot, *scl))
        if pose == self._last_timeline_pose:
            return
        try:
            from Infernux.lib import Vector3
            tr.local_position = Vector3(*pose[0:3])
            tr.local_euler_angles = Vector3(*pose[3:6])
            tr.local_scale = Vector3(*pose[6:9])
            self._last_timeline_pose = pose
        except Exception:
            pass

    def _enter_state(self, state_name: str) -> bool:
        """Enter a state: load its clip and reset playback."""
        if not self._fsm:
            return False
        state = self._fsm.get_state(state_name)
        if state is None:
            Debug.log_warning(f"[SpiritAnimator] State not found: '{state_name}'")
            return False

        if not getattr(state, "restart_same_clip", False):
            if self._playing and self._current_state_name == state_name:
                return True

        if getattr(state, "kind", "clip") == "timeline":
            self._current_state_name = state_name
            self._current_clip = None
            self._current_timeline = self._resolve_timeline(state)
            self._elapsed = 0.0
            self._prev_event_norm = 0.0
            self._playing = True
            self._capture_timeline_base()
            self._last_timeline_pose = None
            if self._current_timeline is not None:
                self._apply_timeline(self._current_timeline, 0.0)
            return True

        self._current_timeline = None
        clip = self._resolve_clip(state)
        self._current_state_name = state_name
        self._current_clip = clip
        self._elapsed = 0.0
        self._prev_event_norm = 0.0
        self._playing = True

        # Apply first frame immediately
        if clip and clip.frame_count > 0 and self._sprite_renderer:
            self._sprite_renderer.frame_id = clip.frames[0].sprite_frame_id
            self._sprite_renderer.sync_visual()

        return True

    def _get_current_state(self) -> Optional[AnimState]:
        if self._fsm and self._current_state_name:
            return self._fsm.get_state(self._current_state_name)
        return None

    def _exit_time_gate_ok(self, state: AnimState) -> bool:
        """Require normalized clip progress >= state's exit_time before any outgoing transition."""
        if self._current_timeline is not None:
            dur = max(1e-6, float(self._current_timeline.duration))
            thr = max(0.0, min(1.0, float(getattr(state, "exit_time_normalized", 1.0))))
            progress = min(max(self._elapsed / dur, 0.0), 1.0)
            return progress + 1e-7 >= thr
        if not self._current_clip or self._current_clip.duration <= 0:
            return True
        thr = float(getattr(state, "exit_time_normalized", 1.0))
        thr = max(0.0, min(1.0, thr))
        d = self._current_clip.duration
        progress = min(max(self._elapsed / d, 0.0), 1.0)
        return progress + 1e-7 >= thr

    def _try_auto_transition(self):
        """Evaluate outgoing transitions from the current state."""
        state = self._get_current_state()
        if not state:
            return
        if not self._exit_time_gate_ok(state):
            return
        for tr in state.transitions:
            if self._evaluate_condition(tr):
                self._consume_triggers(tr)
                self._enter_state(tr.target_state)
                return

    def _evaluate_condition(self, transition: AnimTransition) -> bool:
        """Evaluate a transition's stable parameter predicates.

        No conditions means "transition when clip finishes" (only fires
        when the clip is non-looping and has reached its end).
        """
        if not transition.conditions:
            state = self._get_current_state()
            should_loop = state.loop if state else (
                self._current_clip.loop if self._current_clip else False)
            if self._current_clip and not should_loop:
                return self._elapsed >= self._current_clip.duration
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
