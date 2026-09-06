"""Version-scoped, show-once notifications for Infernux Hub."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from i18n import current_language


HUB_NOTIFICATIONS_URL = "https://infernux-engine.com/hub-notifications.json"
HUB_NOTIFICATIONS_SCHEMA = "infernux.hub_notifications"
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


class HubNotificationQueue:
    def __init__(self, database, source_path: str | Path | None = None) -> None:
        self._database = database
        self._source_path = Path(source_path) if source_path else None

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    @staticmethod
    def _seen_key(hub_version: str, notification_id: str) -> str:
        return f"{_SEEN_KEY_PREFIX}:{hub_version}:{notification_id}"

    def pending(self, hub_version: str) -> list[HubNotification]:
        if self._source_path is None:
            raise RuntimeError("The remote notification document has not been supplied")
        return self.pending_bytes(
            hub_version, self._source_path.read_bytes()
        )

    def pending_bytes(self, hub_version: str, payload: bytes) -> list[HubNotification]:
        document = json.loads(payload.decode("utf-8"))
        if (
            not isinstance(document, dict)
            or set(document) != {"$schema", "notifications"}
            or document["$schema"] != HUB_NOTIFICATIONS_SCHEMA
            or not isinstance(document["notifications"], list)
        ):
            raise ValueError("Hub notifications do not match the current contract")

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


__all__ = [
    "HUB_NOTIFICATIONS_SCHEMA",
    "HUB_NOTIFICATIONS_URL",
    "HubNotification",
    "HubNotificationQueue",
]
