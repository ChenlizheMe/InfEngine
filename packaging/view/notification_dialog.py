"""Presentation layer for Hub's version-scoped notification queue."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QMessageBox

from hub_notifications import HubNotificationQueue
from hub_updater import current_hub_version
from i18n import tr


class HubNotificationController:
    def __init__(
        self,
        main_window,
        database,
        *,
        open_installs: Callable[[], None],
        queue: HubNotificationQueue | None = None,
    ) -> None:
        self._main_window = main_window
        self._open_installs = open_installs
        self._queue = queue or HubNotificationQueue(database)

    def show_pending(self) -> None:
        for notification in self._queue.pending(current_hub_version()):
            # Record delivery before opening the modal dialog so a crash or forced
            # shutdown cannot turn a show-once notice into a startup loop.
            self._queue.mark_seen(notification)
            box = QMessageBox(self._main_window)
            box.setWindowTitle(notification.title)
            box.setText(notification.message)
            if notification.level == "warning":
                box.setIcon(QMessageBox.Icon.Warning)
            elif notification.level == "critical":
                box.setIcon(QMessageBox.Icon.Critical)
            else:
                box.setIcon(QMessageBox.Icon.Information)

            action_button = None
            if notification.action and notification.action_label:
                action_button = box.addButton(
                    notification.action_label, QMessageBox.ButtonRole.AcceptRole
                )
            box.addButton(tr("Close"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if (
                action_button is not None
                and box.clickedButton() is action_button
                and notification.action == "open_installs"
            ):
                self._open_installs()


__all__ = ["HubNotificationController"]
