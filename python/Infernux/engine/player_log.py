"""Lightweight packaged-player log writer shared by platform hosts."""

from __future__ import annotations

import os
import sys

from Infernux.debug import Debug
from Infernux.engine.path_utils import resolved_path


def write_player_log(message: object) -> None:
    """Append one diagnostic line without importing a graphical Player host."""
    path = os.environ.get("_INFERNUX_PLAYER_LOG")
    if not path:
        executable = getattr(sys, "executable", "") or ""
        logs_dir = os.path.join(
            os.path.dirname(resolved_path(executable)), "Data", "Logs"
        )
        os.makedirs(logs_dir, exist_ok=True)
        path = os.path.join(logs_dir, "player.log")
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{message}\n")
    except OSError as exc:
        Debug.log_suppressed("player_log.write", exc)
