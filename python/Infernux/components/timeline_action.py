"""TimelineAction — runtime component that plays a Timeline FSM (.timelinefsm).

A Timeline FSM is a state machine whose states are all timeline nodes; this
component drives the owner GameObject's transform from it, handling transitions
(exit-time, parameter conditions, triggers) between timelines.  It is the
Timeline-mode counterpart to SpiritAnimator (2D) / SkeletalAnimator (3D).
"""

from __future__ import annotations

from typing import Optional

from Infernux.components.component import InxComponent
from Infernux.components.serialized_field import serialized_field
from Infernux.components.decorators import disallow_multiple, add_component_menu
from Infernux.core.asset_ref import TimelineFSMRef
from Infernux.core.timeline_fsm_runtime import TimelineFSMRuntime
from Infernux.debug import Debug


@disallow_multiple
@add_component_menu("Animation/Timeline Action")
class TimelineAction(InxComponent):
    """Plays a Timeline FSM (``.timelinefsm``), driving this GameObject's transform."""

    # ── Serialized fields (Inspector) ───────────────────────────────────
    controller: TimelineFSMRef = serialized_field(
        default=None,
        asset_type="TimelineFSM",
        tooltip="Timeline state machine (.timelinefsm) to play",
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

    # ── Private runtime state ───────────────────────────────────────────
    _runtime: Optional[TimelineFSMRuntime] = None
    _cached_transform_handle = None

    # ── Lifecycle ───────────────────────────────────────────────────────
    def awake(self):
        self._runtime = TimelineFSMRuntime()
        self._cached_transform_handle = None

    def start(self):
        self._load_controller()
        rt = self._runtime
        if self.auto_play and rt is not None and rt.fsm is not None and rt.fsm.default_state:
            self.play(rt.fsm.default_state)

    def on_after_deserialize(self):
        if getattr(self, "_runtime", None) is None:
            self._runtime = TimelineFSMRuntime()
        self._cached_transform_handle = None
        self._load_controller()

    def update(self, delta_time: float):
        rt = self._runtime
        if rt is None:
            return
        # A stopped timeline is already holding its last pose. Avoid resolving
        # the native transform and crossing the binding boundary until playback
        # is explicitly resumed.
        if not getattr(rt, "needs_update", rt.is_playing):
            return
        rt.playback_speed = self.playback_speed
        rt.update(delta_time, self._transform())

    # ── Public API ──────────────────────────────────────────────────────
    def reload_controller(self):
        self._load_controller()
        rt = self._runtime
        if rt is not None and rt.fsm is not None and rt.fsm.default_state:
            self.play(rt.fsm.default_state)

    def play(self, state_name: str = "") -> bool:
        rt = getattr(self, "_runtime", None)
        if rt is None:
            return False
        return rt.play(state_name, transform=self._transform())

    def stop(self):
        rt = getattr(self, "_runtime", None)
        if rt is not None:
            rt.stop()

    @property
    def current_state(self) -> str:
        rt = getattr(self, "_runtime", None)
        return rt.current_state if rt is not None else ""

    @property
    def is_playing(self) -> bool:
        rt = getattr(self, "_runtime", None)
        return bool(rt.is_playing) if rt is not None else False

    @property
    def normalized_time(self) -> float:
        rt = getattr(self, "_runtime", None)
        return float(rt.normalized_time) if rt is not None else 0.0

    # Parameter API (delegates to the runtime)
    def set_bool(self, name: str, value: bool):
        rt = getattr(self, "_runtime", None)
        if rt is not None:
            rt.set_bool(name, value)

    def get_bool(self, name: str) -> bool:
        rt = getattr(self, "_runtime", None)
        return rt.get_bool(name) if rt is not None else False

    def set_float(self, name: str, value: float):
        rt = getattr(self, "_runtime", None)
        if rt is not None:
            rt.set_float(name, value)

    def get_float(self, name: str) -> float:
        rt = getattr(self, "_runtime", None)
        return rt.get_float(name) if rt is not None else 0.0

    def set_int(self, name: str, value: int):
        rt = getattr(self, "_runtime", None)
        if rt is not None:
            rt.set_int(name, value)

    def get_int(self, name: str) -> int:
        rt = getattr(self, "_runtime", None)
        return rt.get_int(name) if rt is not None else 0

    def set_trigger(self, name: str):
        rt = getattr(self, "_runtime", None)
        if rt is not None:
            rt.set_trigger(name)

    # ── Internals ───────────────────────────────────────────────────────
    def _transform(self):
        handle = self._cached_transform_handle
        scene = getattr(self, "_native_scene", None)
        if handle is not None and scene is not None:
            try:
                transform = scene.resolve_component(handle)
            except (RuntimeError, AttributeError):
                transform = None
            if transform is not None:
                return transform
            self._cached_transform_handle = None

        tr = self._try_get_transform()
        self._cached_transform_handle = getattr(tr, "handle", None) if tr is not None else None
        return tr

    def _load_controller(self):
        rt = getattr(self, "_runtime", None)
        if rt is None:
            self._runtime = rt = TimelineFSMRuntime()
        fsm = self.controller  # TimelineFSMRef auto-resolves to an AnimStateMachine
        if fsm is not None and getattr(fsm, "mode", "") != "timeline":
            Debug.log_warning(
                f"[TimelineAction] Controller mode='{getattr(fsm, 'mode', '')}', expected 'timeline'."
            )
        rt.set_fsm(fsm)
