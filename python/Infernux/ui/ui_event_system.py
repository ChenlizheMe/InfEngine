"""Per-frame mouse and multi-touch dispatch for runtime screen UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple, TYPE_CHECKING

from Infernux.engine.runtime_dispatch import (
    current_runtime_epoch,
    resolve_runtime_method,
)

from .ui_event_data import PointerButton, PointerEventData, PointerType

if TYPE_CHECKING:
    from .inx_ui_screen_component import InxUIScreenComponent
    from .ui_canvas import UICanvas


_DRAG_THRESHOLD = 5.0
_DOUBLE_CLICK_TIME = 0.3
_MOUSE_POINTER_ID = -1


@dataclass(frozen=True, slots=True)
class UIPointerFrame:
    """One physical pointer snapshot expressed in every canvas's coordinates."""

    pointer_id: int
    pointer_type: PointerType
    canvas_positions: Tuple[Tuple[float, float], ...]
    down: bool = False
    up: bool = False
    held: bool = False
    canceled: bool = False
    scroll_delta: Tuple[float, float] = (0.0, 0.0)


@dataclass(slots=True)
class _PointerState:
    pointer_type: PointerType
    hover_target: Optional[InxUIScreenComponent] = None
    hover_canvas: Optional[UICanvas] = None
    press_target: Optional[InxUIScreenComponent] = None
    press_canvas: Optional[UICanvas] = None
    press_position: Tuple[float, float] = (0.0, 0.0)
    drag_target: Optional[InxUIScreenComponent] = None
    is_dragging: bool = False
    last_canvas_positions: Tuple[Tuple[float, float], ...] = field(default_factory=tuple)
    last_click_time: float = 0.0
    click_count: int = 0


class UIEventProcessor:
    """Dispatch independent mouse and touch transactions to screen UI elements."""

    def __init__(self):
        self._pointers: dict[tuple[PointerType, int], _PointerState] = {}
        self._time = 0.0
        self._last_pointer_debug: dict = {}

    def process(
        self,
        canvases: List[UICanvas],
        canvas_positions: List[Tuple[float, float]],
        mouse_down: bool,
        mouse_up: bool,
        mouse_held: bool,
        scroll_delta: Tuple[float, float],
        dt: float,
    ) -> None:
        """Dispatch one mouse frame from the Editor Game View."""

        self.process_pointers(
            canvases,
            (
                UIPointerFrame(
                    pointer_id=_MOUSE_POINTER_ID,
                    pointer_type=PointerType.Mouse,
                    canvas_positions=tuple(canvas_positions),
                    down=bool(mouse_down),
                    up=bool(mouse_up),
                    held=bool(mouse_held),
                    scroll_delta=scroll_delta,
                ),
            ),
            dt,
        )

    def process_pointers(
        self,
        canvases: Sequence[UICanvas],
        pointers: Sequence[UIPointerFrame],
        dt: float,
    ) -> None:
        """Dispatch one complete physical pointer snapshot."""

        self._time += float(dt)
        seen: set[tuple[PointerType, int]] = set()
        epoch = current_runtime_epoch()
        for pointer in pointers:
            pointer_id = int(pointer.pointer_id)
            pointer_key = (pointer.pointer_type, pointer_id)
            if pointer_key in seen:
                raise ValueError(
                    f"Duplicate UI {pointer.pointer_type.name} pointer id {pointer_id}"
                )
            if len(pointer.canvas_positions) != len(canvases):
                raise ValueError(
                    "UI pointer canvas position count does not match canvas count"
                )
            seen.add(pointer_key)
            self._process_pointer(canvases, pointer, epoch)

        missing_touches = tuple(
            pointer_key
            for pointer_key, state in self._pointers.items()
            if state.pointer_type is PointerType.Touch and pointer_key not in seen
        )
        for pointer_key in missing_touches:
            self._cancel_pointer(pointer_key, epoch)

    def _process_pointer(self, canvases, pointer: UIPointerFrame, epoch) -> None:
        pointer_id = int(pointer.pointer_id)
        pointer_key = (pointer.pointer_type, pointer_id)
        state = self._pointers.get(pointer_key)
        if state is None:
            state = _PointerState(pointer_type=pointer.pointer_type)
            self._pointers[pointer_key] = state

        current = pointer.canvas_positions[0] if pointer.canvas_positions else (0.0, 0.0)
        previous = state.last_canvas_positions[0] if state.last_canvas_positions else current
        delta = (current[0] - previous[0], current[1] - previous[1])
        active = (
            pointer.down
            or pointer.up
            or pointer.held
            or pointer.canceled
            or pointer.scroll_delta != (0.0, 0.0)
            or current != previous
            or state.press_target is not None
        )
        if not active:
            state.last_canvas_positions = pointer.canvas_positions
            return

        hit_element = None
        hit_canvas = None
        hit_position = current
        for index in range(len(canvases) - 1, -1, -1):
            canvas = canvases[index]
            canvas_object = canvas.game_object
            if canvas_object is not None and not canvas_object.active_in_hierarchy:
                continue
            if not getattr(canvas, "enabled", True):
                continue
            position = pointer.canvas_positions[index]
            candidate = canvas.raycast(position[0], position[1])
            if candidate is not None:
                hit_element = candidate
                hit_canvas = canvas
                hit_position = position
                break

        if pointer.down or pointer.up or pointer.canceled:
            hit_object = getattr(hit_element, "game_object", None) if hit_element is not None else None
            self._last_pointer_debug = {
                "pointer_id": pointer_id,
                "pointer_type": pointer.pointer_type.name,
                "canvas_count": len(canvases),
                "canvas_position": [float(hit_position[0]), float(hit_position[1])],
                "down": bool(pointer.down),
                "up": bool(pointer.up),
                "held": bool(pointer.held),
                "canceled": bool(pointer.canceled),
                "hit_type": type(hit_element).__name__ if hit_element is not None else "",
                "hit_object": str(getattr(hit_object, "name", "") or ""),
                "press_type": type(state.press_target).__name__ if state.press_target is not None else "",
                "press_object": str(
                    getattr(getattr(state.press_target, "game_object", None), "name", "") or ""
                ),
            }

        previous_hover = state.hover_target
        if hit_element is not previous_hover:
            if previous_hover is not None:
                event = self._make_event(
                    pointer, hit_position, delta, state.hover_canvas, previous_hover
                )
                self._dispatch_pointer_callback(previous_hover, "on_pointer_exit", event, epoch)
            state.hover_target = hit_element
            state.hover_canvas = hit_canvas
            if hit_element is not None:
                event = self._make_event(pointer, hit_position, delta, hit_canvas, hit_element)
                self._dispatch_pointer_callback(hit_element, "on_pointer_enter", event, epoch)

        if pointer.down and hit_element is not None:
            state.press_target = hit_element
            state.press_canvas = hit_canvas
            state.press_position = hit_position
            state.drag_target = hit_element
            state.is_dragging = False
            event = self._make_event(pointer, hit_position, delta, hit_canvas, hit_element)
            event.press_position = state.press_position
            self._dispatch_pointer_callback(hit_element, "on_pointer_down", event, epoch)

        if pointer.held and state.drag_target is not None:
            dx = current[0] - state.press_position[0]
            dy = current[1] - state.press_position[1]
            distance_squared = dx * dx + dy * dy
            event = self._make_event(
                pointer, current, delta, state.press_canvas, state.drag_target
            )
            event.press_position = state.press_position
            if not state.is_dragging and distance_squared > _DRAG_THRESHOLD * _DRAG_THRESHOLD:
                state.is_dragging = True
                self._dispatch_pointer_callback(state.drag_target, "on_begin_drag", event, epoch)
            elif state.is_dragging:
                self._dispatch_pointer_callback(state.drag_target, "on_drag", event, epoch)

        if pointer.scroll_delta != (0.0, 0.0) and hit_element is not None:
            event = self._make_event(
                pointer, hit_position, delta, hit_canvas, hit_element
            )
            self._dispatch_pointer_callback(hit_element, "on_scroll", event, epoch)

        if pointer.up or pointer.canceled:
            self._release_pointer(
                pointer,
                state,
                current,
                delta,
                hit_element,
                hit_canvas,
                hit_position,
                epoch,
            )

        state.last_canvas_positions = pointer.canvas_positions
        if pointer.pointer_type is PointerType.Touch and (pointer.up or pointer.canceled):
            if state.hover_target is not None:
                event = self._make_event(
                    pointer, hit_position, delta, state.hover_canvas, state.hover_target
                )
                self._dispatch_pointer_callback(state.hover_target, "on_pointer_exit", event, epoch)
            self._pointers.pop(pointer_key, None)

    def _release_pointer(
        self,
        pointer,
        state,
        current,
        delta,
        hit_element,
        hit_canvas,
        hit_position,
        epoch,
    ) -> None:
        press_target = state.press_target
        if press_target is not None:
            event = self._make_event(
                pointer,
                hit_position if hit_element is not None else current,
                delta,
                state.press_canvas,
                press_target,
            )
            event.press_position = state.press_position
            self._dispatch_pointer_callback(press_target, "on_pointer_up", event, epoch)

            if not pointer.canceled and hit_element is press_target:
                if (self._time - state.last_click_time) < _DOUBLE_CLICK_TIME:
                    state.click_count += 1
                else:
                    state.click_count = 1
                state.last_click_time = self._time
                click = self._make_event(pointer, hit_position, delta, hit_canvas, hit_element)
                click.press_position = state.press_position
                click.click_count = state.click_count
                self._dispatch_pointer_callback(hit_element, "on_pointer_click", click, epoch)
                debug_dispatch = getattr(hit_element, "debug_dispatch_state", None)
                if callable(debug_dispatch):
                    self._last_pointer_debug["persistent_dispatch"] = debug_dispatch()

        if state.is_dragging and state.drag_target is not None:
            event = self._make_event(
                pointer, current, delta, state.press_canvas, state.drag_target
            )
            event.press_position = state.press_position
            self._dispatch_pointer_callback(state.drag_target, "on_end_drag", event, epoch)

        state.press_target = None
        state.press_canvas = None
        state.drag_target = None
        state.is_dragging = False

    def _cancel_pointer(
        self, pointer_key: tuple[PointerType, int], epoch
    ) -> None:
        state = self._pointers.pop(pointer_key)
        pointer_type, pointer_id = pointer_key
        positions = state.last_canvas_positions
        current = positions[0] if positions else (0.0, 0.0)
        pointer = UIPointerFrame(
            pointer_id=pointer_id,
            pointer_type=pointer_type,
            canvas_positions=positions,
            up=True,
            canceled=True,
        )
        self._release_pointer(
            pointer, state, current, (0.0, 0.0), None, None, current, epoch
        )
        if state.hover_target is not None:
            event = self._make_event(
                pointer, current, (0.0, 0.0), state.hover_canvas, state.hover_target
            )
            self._dispatch_pointer_callback(state.hover_target, "on_pointer_exit", event, epoch)

    def reset(self) -> None:
        """Cancel every active pointer transaction."""

        epoch = current_runtime_epoch()
        for pointer_key in tuple(self._pointers):
            self._cancel_pointer(pointer_key, epoch)

    def debug_state(self) -> dict:
        """Return the latest transition without polling input each frame."""

        return dict(self._last_pointer_debug)

    @staticmethod
    def _dispatch_pointer_callback(target, method_name, event, epoch) -> None:
        """Invoke one callback exactly once and propagate user-code failures."""

        callback = resolve_runtime_method(target, method_name, epoch=epoch)
        epoch.require_descriptor(type(target))
        if callback is not None:
            callback(event)

    @staticmethod
    def _make_event(
        pointer: UIPointerFrame,
        position: Tuple[float, float],
        delta: Tuple[float, float],
        canvas: Optional[UICanvas],
        target: Optional[InxUIScreenComponent],
    ) -> PointerEventData:
        event = PointerEventData()
        event.position = position
        event.delta = delta
        event.pointer_id = int(pointer.pointer_id)
        event.pointer_type = pointer.pointer_type
        event.canceled = bool(pointer.canceled)
        event.button = PointerButton.Left
        event.scroll_delta = pointer.scroll_delta
        event.canvas = canvas
        event.target = target
        return event
