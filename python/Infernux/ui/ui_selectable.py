"""UISelectable — visual state machine for interactive UI elements.

Provides Normal / Highlighted / Pressed / Disabled visual states
with ColorTint transitions.  ``UIButton`` inherits this and adds
an ``on_click`` event.

Hierarchy:
    InxComponent → InxUIComponent → InxUIScreenComponent → UISelectable
"""

from __future__ import annotations

from Infernux.components import serialized_field
from Infernux.components.fields import FieldType
from .inx_ui_screen_component import InxUIScreenComponent
from .enums import UITransitionType
from .ui_render_revision import mark_runtime_ui_dirty


class SelectionState:
    """Visual state indices — mirrors Unity's ``SelectionState``."""
    Normal = 0
    Highlighted = 1
    Pressed = 2
    Disabled = 3


class UISelectable(InxUIScreenComponent):
    """Base class for interactive UI elements with visual feedback.

    Subclass and override pointer hooks to build concrete widgets
    (see ``UIButton``).
    """

    interactable: bool = serialized_field(
        default=True, tooltip="Allow user interaction",
        group="Interaction",
    )
    transition: UITransitionType = serialized_field(
        default=UITransitionType.ColorTint,
        tooltip="How visual states are displayed",
        group="Interaction",
    )

    # ── Color Tint ──
    normal_color: list = serialized_field(
        default=[1.0, 1.0, 1.0, 1.0], field_type=FieldType.COLOR,
        hdr=True, tooltip="Tint when idle", group="ColorTint",
    )
    highlighted_color: list = serialized_field(
        default=[0.96, 0.96, 0.96, 1.0], field_type=FieldType.COLOR,
        hdr=True, tooltip="Tint when hovered", group="ColorTint",
    )
    pressed_color: list = serialized_field(
        default=[0.78, 0.78, 0.78, 1.0], field_type=FieldType.COLOR,
        hdr=True, tooltip="Tint when pressed", group="ColorTint",
    )
    disabled_color: list = serialized_field(
        default=[0.78, 0.78, 0.78, 0.5], field_type=FieldType.COLOR,
        hdr=True, tooltip="Tint when disabled", group="ColorTint",
    )

    def awake(self):
        self._init_selectable_state()

    def _init_selectable_state(self):
        """Initialize transient state (safe to call multiple times)."""
        if not hasattr(self, "_current_state"):
            self._current_state: int = SelectionState.Normal
            self._is_pointer_inside: bool = False
            self._is_pointer_down: bool = False

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def current_selection_state(self) -> int:
        self._init_selectable_state()
        return self._current_state

    def get_current_tint(self) -> list:
        """Return the RGBA tint for the current visual state."""
        self._init_selectable_state()
        if self.transition != UITransitionType.ColorTint:
            return [1.0, 1.0, 1.0, 1.0]
        if not self.interactable:
            return list(self.disabled_color)
        if self._current_state == SelectionState.Pressed:
            return list(self.pressed_color)
        if self._current_state == SelectionState.Highlighted:
            return list(self.highlighted_color)
        return list(self.normal_color)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _evaluate_state(self):
        self._init_selectable_state()
        old_state = self._current_state
        if not self.interactable:
            self._current_state = SelectionState.Disabled
        elif self._is_pointer_down:
            self._current_state = SelectionState.Pressed
        elif self._is_pointer_inside:
            self._current_state = SelectionState.Highlighted
        else:
            self._current_state = SelectionState.Normal
        if self._current_state != old_state:
            mark_runtime_ui_dirty()

    # ------------------------------------------------------------------
    # Pointer hooks — override in further subclasses, call super()
    # ------------------------------------------------------------------

    def on_pointer_enter(self, event_data):
        if not self.interactable:
            return
        self._is_pointer_inside = True
        self._evaluate_state()

    def on_pointer_exit(self, event_data):
        self._is_pointer_inside = False
        self._is_pointer_down = False
        self._evaluate_state()

    def on_pointer_down(self, event_data):
        if not self.interactable:
            return
        self._is_pointer_down = True
        self._evaluate_state()

    def on_pointer_up(self, event_data):
        self._is_pointer_down = False
        self._evaluate_state()
