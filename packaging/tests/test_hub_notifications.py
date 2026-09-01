from __future__ import annotations

import json
import sys
from pathlib import Path


PACKAGING_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGING_ROOT))

from database import ProjectDatabase
from hub_notifications import HubNotificationQueue
from i18n import configure_language


def _write_notifications(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "notifications": [
                    {
                        "id": "runtime-repair",
                        "hub_versions": ["0.2.9", "0.3.0"],
                        "level": "warning",
                        "title": {"en": "Repair runtime", "zh": "修复运行环境"},
                        "message": {"en": "Use the new Python.", "zh": "请使用新的 Python。"},
                        "action": {
                            "kind": "open_installs",
                            "label": {"en": "Open Installs", "zh": "打开安装页面"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_notification_is_shown_once_for_each_target_hub_version(tmp_path: Path):
    source = tmp_path / "notifications.json"
    _write_notifications(source)
    database = ProjectDatabase(tmp_path / "projects.db")
    queue = HubNotificationQueue(database, source)
    configure_language("en")

    first = queue.pending("0.2.9")
    assert [notice.notification_id for notice in first] == ["runtime-repair"]
    assert first[0].action == "open_installs"

    queue.mark_seen(first[0])
    assert queue.pending("0.2.9") == []
    assert [notice.notification_id for notice in queue.pending("0.3.0")] == [
        "runtime-repair"
    ]
    database.close()


def test_notification_uses_current_hub_language(tmp_path: Path):
    source = tmp_path / "notifications.json"
    _write_notifications(source)
    database = ProjectDatabase(tmp_path / "projects.db")
    configure_language("zh")

    notice = HubNotificationQueue(database, source).pending("0.2.9")[0]

    assert notice.title == "修复运行环境"
    assert notice.message == "请使用新的 Python。"
    assert notice.action_label == "打开安装页面"
    database.close()
    configure_language("system")


def test_notification_queue_ignores_missing_or_invalid_documents(tmp_path: Path):
    database = ProjectDatabase(tmp_path / "projects.db")
    missing = HubNotificationQueue(database, tmp_path / "missing.json")
    assert missing.pending("0.2.9") == []

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text('{"notifications": {}}', encoding="utf-8")
    assert HubNotificationQueue(database, invalid_path).pending("0.2.9") == []
    database.close()
