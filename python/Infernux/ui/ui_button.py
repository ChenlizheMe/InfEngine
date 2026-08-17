"""UIButton — a clickable button UI element.

Hierarchy:
    InxComponent → InxUIComponent → InxUIScreenComponent → UISelectable → UIButton

Usage in a user script::

    class MyUI(InxComponent):
        def start(self):
            start_btn = GameObject.find("StartBtn")
            if start_btn is None:
                return
            btn = start_btn.get_component(UIButton)
            btn.on_click.add_listener(self.on_start)

        def on_start(self):
            print("Start clicked!")
"""

from __future__ import annotations

from Infernux.components import serialized_field, list_field, add_component_menu
from Infernux.components.fields import FieldType
from Infernux.debug import Debug
from .enums import TextAlignH, TextAlignV
from .ui_selectable import UISelectable
from .ui_event import UIEvent
from .ui_event_entry import (
    UIEventEntry,
    _UIEventRuntimeBinding,
    _get_serializable_raw_field,
)


@add_component_menu("UI/Button")
class UIButton(UISelectable):
    """A clickable button with visual state feedback.

    Combines **Image** (background) and **Text** (label) capabilities:

    * Background can be a solid ``background_color`` or a ``texture_path`` image.
    * Label text supports full typography: alignment, line-height, letter-spacing.
    * Fires ``on_click`` when the user performs a full click (down + up).
    """

    # ── Content ──
    label: str = serialized_field(
        default="Button", tooltip="Button label text",
        group="Content",
    )
    font_size: float = serialized_field(
        default=18.0, tooltip="Label font size",
        group="Content", range=(4.0, 256.0), drag_speed=0.5,
    )
    font_path: str = serialized_field(
        default="", tooltip="Optional font asset path",
        group="Content",
    )
    label_color: list = serialized_field(
        default=[1.0, 1.0, 1.0, 1.0], field_type=FieldType.COLOR,
        hdr=True, tooltip="Label text colour", group="Content",
    )
    text_align_h: TextAlignH = serialized_field(
        default=TextAlignH.Center,
        tooltip="Horizontal text alignment",
        group="Content",
    )
    text_align_v: TextAlignV = serialized_field(
        default=TextAlignV.Center,
        tooltip="Vertical text alignment",
        group="Content",
    )
    line_height: float = serialized_field(
        default=1.2, tooltip="Line height multiplier",
        group="Content", range=(0.5, 5.0), drag_speed=0.01,
    )
    letter_spacing: float = serialized_field(
        default=0.0, tooltip="Extra letter spacing in pixels",
        group="Content", range=(-20.0, 100.0), drag_speed=0.1,
    )

    # ── Fill ──
    texture_path: str = serialized_field(
        default="", tooltip="Background image texture path",
        group="Fill",
    )
    background_color: list = serialized_field(
        default=[0.922, 0.341, 0.341, 1.0], field_type=FieldType.COLOR,
        hdr=True, tooltip="Background fill colour (RGBA)", group="Fill",
    )

    # ── Events ──
    on_click_entries: list = list_field(
        element_type=FieldType.SERIALIZABLE_OBJECT,
        element_class=UIEventEntry,
        tooltip="Persistent click handlers (GO → component → method)",
        group="Events",
    )

    def awake(self):
        super().awake()
        self._init_button_state()

    def _init_button_state(self):
        if not hasattr(self, "_on_click"):
            self._on_click: UIEvent = UIEvent()
        if not hasattr(self, "_persistent_click_bindings"):
            # Runtime-only cache.  It is intentionally aligned with the
            # serialized entry list and is discarded whenever authoring state
            # changes or an owner becomes invalid.
            self._persistent_click_bindings: list = []

    @property
    def on_click(self) -> UIEvent:
        self._init_button_state()
        return self._on_click

    # ------------------------------------------------------------------
    # Pointer hooks
    # ------------------------------------------------------------------

    def on_pointer_click(self, event_data):
        if not self.interactable:
            return
        self._init_button_state()
        self._on_click.invoke()
        self._dispatch_persistent_entries()

    # ------------------------------------------------------------------
    # Persistent event dispatch
    # ------------------------------------------------------------------

    def _dispatch_persistent_entries(self):
        """Resolve and invoke each on_click_entries binding."""
        self._init_button_state()
        entries = list(self.on_click_entries or [])
        results = []
        self._last_persistent_dispatch = results
        if len(self._persistent_click_bindings) > len(entries):
            del self._persistent_click_bindings[len(entries):]
        while len(self._persistent_click_bindings) < len(entries):
            self._persistent_click_bindings.append(None)
        if not entries:
            return
        for index, entry in enumerate(entries):
            result = {
                "index": index,
                "component_name": str(getattr(entry, "component_name", "") or ""),
                "method_name": str(getattr(entry, "method_name", "") or ""),
                "status": "pending",
            }
            results.append(result)
            target_ref = _get_serializable_raw_field(entry, "target")
            if target_ref is None:
                self._persistent_click_bindings[index] = None
                result["status"] = "missing_target"
                self._log_dispatch_failure(result)
                continue
            go = target_ref.resolve() if hasattr(target_ref, "resolve") else target_ref
            if go is None:
                self._persistent_click_bindings[index] = None
                result["status"] = "unresolved_target"
                self._log_dispatch_failure(result)
                continue
            result["target_id"] = int(getattr(go, "id", 0) or 0)
            result["target_name"] = str(getattr(go, "name", "") or "")
            comp_name = getattr(entry, "component_name", "") or ""
            method_name = getattr(entry, "method_name", "") or ""
            if not comp_name or not method_name:
                self._persistent_click_bindings[index] = None
                result["status"] = "incomplete_binding"
                self._log_dispatch_failure(result)
                continue
            comp = self._resolve_component(go, comp_name)
            if comp is None:
                self._persistent_click_bindings[index] = None
                result["status"] = "missing_component"
                self._log_dispatch_failure(result)
                continue
            binding = self._persistent_click_bindings[index]
            if not isinstance(binding, _UIEventRuntimeBinding) or not binding.matches(
                entry,
                go,
                comp,
            ):
                try:
                    binding = _UIEventRuntimeBinding.create(entry, comp, go)
                except Exception as exc:
                    self._persistent_click_bindings[index] = None
                    result["status"] = (
                        "missing_method"
                        if isinstance(exc, ValueError)
                        and "method" in str(exc).lower()
                        else "invalid_binding"
                    )
                    result["error"] = f"{type(exc).__name__}: {exc}"
                    self._log_dispatch_failure(result)
                    continue
                self._persistent_click_bindings[index] = binding
            try:
                invocation = binding.invoke(entry)
                if invocation.status in {
                    "owner_unavailable",
                    "owner_invalid",
                    "method_missing",
                    "signature_mismatch",
                }:
                    self._persistent_click_bindings[index] = None
                    result["status"] = invocation.status
                    result["error"] = invocation.message
                    self._log_dispatch_failure(result)
                    continue
                if invocation.status not in {"resolved", "legacy_pinned"}:
                    result["status"] = invocation.status
                    result["error"] = invocation.message
                    self._log_dispatch_failure(result)
                    continue
                result["status"] = "invoked"
            except Exception as exc:
                result["status"] = "exception"
                result["error"] = f"{type(exc).__name__}: {exc}"
                Debug.log_error(
                    f"UIButton persistent event failed for "
                    f"{result['target_name']}.{comp_name}.{method_name}: {exc}"
                )

    def on_validate(self):
        """Invalidate transient event bindings after serialized edits."""
        self._persistent_click_bindings = []
        parent_validate = getattr(super(), "on_validate", None)
        if callable(parent_validate):
            parent_validate()

    @staticmethod
    def _log_dispatch_failure(result: dict) -> None:
        target = result.get("target_name") or f"object {result.get('target_id', 0)}"
        component = result.get("component_name") or "<component>"
        method = result.get("method_name") or "<method>"
        Debug.log_error(
            f"UIButton persistent event could not invoke "
            f"{target}.{component}.{method}: {result['status']}"
        )

    def debug_dispatch_state(self) -> list[dict]:
        """Return the most recent click dispatch result for on-demand diagnostics."""
        return [dict(item) for item in getattr(self, "_last_persistent_dispatch", [])]

    @staticmethod
    def _resolve_component(go, comp_name: str):
        """Find a Python component by class name on *go*."""
        for py_comp in go.get_py_components():
            if type(py_comp).__name__ == comp_name:
                return py_comp
        return None
