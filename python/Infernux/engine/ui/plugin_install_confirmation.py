"""Editor-owned confirmation for external plugin and pip sources."""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from Infernux.engine.i18n import t
from Infernux.engine.interaction import ModalService

from .editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    end_editor_modal,
    render_editor_modal_actions,
)


class PluginInstallConfirmationCoordinator:
    MODAL_ID = "editor.plugin_install_confirmation"
    _instance: Optional["PluginInstallConfirmationCoordinator"] = None

    def __init__(self, modal_service: ModalService) -> None:
        self._modals = modal_service
        self._kind = ""
        self._value = ""
        self._callback: Optional[Callable[[], None]] = None
        self._requested = False
        self._modals.register(
            self.MODAL_ID,
            is_active=lambda: self.is_active,
            render=self.render,
            cancel=self.cancel,
        )

    @classmethod
    def instance(cls) -> "PluginInstallConfirmationCoordinator":
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("plugin install confirmation requires EditorInteractionCore")
        if cls._instance is None or cls._instance._modals is not core.modals:
            cls._instance = cls(core.modals)
        return cls._instance

    @property
    def is_active(self) -> bool:
        return bool(self._kind and self._value and self._callback)

    def request(self, kind: str, value: str, callback: Callable[[], None]) -> bool:
        normalized_kind = str(kind or "").strip().casefold()
        normalized_value = str(value or "").strip()
        if self.is_active or normalized_kind not in {"source", "pip"}:
            return False
        if not normalized_value or not callable(callback):
            return False
        if not self._modals.activate(self.MODAL_ID, owner_id="plugins"):
            return False
        self._kind = normalized_kind
        self._value = normalized_value
        self._callback = callback
        self._requested = True
        return True

    def render(self, ctx) -> bool:
        if not self.is_active:
            return False
        title = t("plugins.install_confirm.title")
        request_open = self._requested
        self._requested = False
        if not begin_editor_modal(
            ctx,
            popup_id=f"{title}##plugin_install_confirmation",
            title=title,
            semantic_id=self.MODAL_ID,
            request_open=request_open,
            width=540.0,
            height=230.0,
        ):
            return False
        ctx.text_wrapped(t(f"plugins.install_confirm.{self._kind}"))
        ctx.spacing()
        ctx.text_wrapped(self._value)
        ctx.spacing()
        ctx.text_wrapped(t("plugins.install_confirm.side_effects"))
        render_editor_modal_actions(
            ctx,
            (
                EditorModalAction(
                    t("plugins.install_confirm.install"),
                    "confirm",
                    lambda: self._confirm(ctx),
                ),
                EditorModalAction(
                    t("editor.modal.cancel"),
                    "cancel",
                    lambda: self._cancel(ctx),
                ),
            ),
            semantic_prefix=self.MODAL_ID,
        )
        end_editor_modal(ctx)
        return True

    def _confirm(self, ctx) -> None:
        callback = self._callback
        ctx.close_current_popup()
        self._clear()
        if callback is not None:
            callback()

    def _cancel(self, ctx) -> None:
        ctx.close_current_popup()
        self.cancel()

    def cancel(self) -> None:
        self._clear()

    def _clear(self) -> None:
        self._kind = ""
        self._value = ""
        self._callback = None
        self._requested = False
        self._modals.deactivate(self.MODAL_ID)


__all__ = ["PluginInstallConfirmationCoordinator"]
