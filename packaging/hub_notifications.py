"""Version-scoped, show-once notifications for Infernux Hub."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hub_utils import get_hub_data_dir, is_frozen
from i18n import current_language


_NOTIFICATION_FILE = "hub_notifications.json"
_SEEN_KEY_PREFIX = "hub_notification_seen"


@dataclass(frozen=True)
class HubNotification:
    notification_id: str
    hub_version: str
    level: str
    title: str
    message: str
    action: str
    action_label: str


def _localized_text(value: object, language: str) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    selected = value.get(language) or value.get("en") or ""
    return str(selected).strip()


def default_notification_path() -> Path:
    if is_frozen():
        return Path(get_hub_data_dir()) / _NOTIFICATION_FILE
    return Path(__file__).resolve().parent / "resources" / _NOTIFICATION_FILE


class HubNotificationQueue:
    def __init__(self, database, source_path: str | Path | None = None) -> None:
        self._database = database
        self._source_path = Path(source_path) if source_path else default_notification_path()

    @staticmethod
    def _seen_key(hub_version: str, notification_id: str) -> str:
        return f"{_SEEN_KEY_PREFIX}:{hub_version}:{notification_id}"

    def pending(self, hub_version: str) -> list[HubNotification]:
        try:
            document = json.loads(self._source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if document.get("schema") != 1 or not isinstance(document.get("notifications"), list):
            return []

        language = current_language()
        pending: list[HubNotification] = []
        seen_ids: set[str] = set()
        for raw in document["notifications"]:
            if not isinstance(raw, dict):
                continue
            notification_id = str(raw.get("id", "")).strip()
            versions = raw.get("hub_versions", [])
            if (
                not notification_id
                or notification_id in seen_ids
                or not isinstance(versions, list)
                or hub_version not in versions
            ):
                continue
            seen_ids.add(notification_id)
            if self._database.get_setting(
                self._seen_key(hub_version, notification_id), ""
            ) == "1":
                continue

            title = _localized_text(raw.get("title"), language)
            message = _localized_text(raw.get("message"), language)
            if not title or not message:
                continue
            action_data = raw.get("action", {})
            action = ""
            action_label = ""
            if isinstance(action_data, dict):
                action = str(action_data.get("kind", "")).strip()
                action_label = _localized_text(action_data.get("label"), language)
            pending.append(
                HubNotification(
                    notification_id=notification_id,
                    hub_version=hub_version,
                    level=str(raw.get("level", "information")).strip().lower(),
                    title=title,
                    message=message,
                    action=action,
                    action_label=action_label,
                )
            )
        return pending

    def mark_seen(self, notification: HubNotification) -> None:
        self._database.set_setting(
            self._seen_key(notification.hub_version, notification.notification_id),
            "1",
        )


__all__ = ["HubNotification", "HubNotificationQueue", "default_notification_path"]
