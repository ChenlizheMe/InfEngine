from __future__ import annotations

import json
import os
import time


INSTALL_MARKER_FILENAME = ".infernux-hub-install.json"


def install_marker_path(install_dir: str) -> str:
    return os.path.join(install_dir, INSTALL_MARKER_FILENAME)


def looks_like_infernux_project(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    return bool(
        os.path.isdir(os.path.join(path, "Assets"))
        and os.path.isdir(os.path.join(path, "ProjectSettings"))
    ) or os.path.isfile(os.path.join(path, ".infernux-version"))


def looks_like_infernux_user_data(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    normalized_name = os.path.basename(os.path.normpath(path)).lower()
    if normalized_name == ".infernux":
        return True
    return bool(
        os.path.isfile(os.path.join(path, "projects.db"))
        or os.path.isdir(os.path.join(path, "versions"))
        or os.path.isdir(os.path.join(path, "runtime"))
    )


def is_dangerous_install_target(path: str) -> bool:
    return looks_like_infernux_project(path) or looks_like_infernux_user_data(path)


def is_recognized_install_dir(path: str) -> bool:
    return is_marked_install_dir(path)


def write_install_marker(install_dir: str) -> None:
    os.makedirs(install_dir, exist_ok=True)
    payload = {
        "tool": "Infernux Hub",
        "kind": "install-directory",
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    marker_path = install_marker_path(install_dir)
    tmp_path = marker_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, marker_path)


def is_marked_install_dir(path: str) -> bool:
    marker_path = install_marker_path(path)
    if not os.path.isfile(marker_path):
        return False
    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("tool") == "Infernux Hub" and payload.get("kind") == "install-directory"


def can_remove_install_dir(path: str) -> bool:
    return bool(path and os.path.isdir(path) and is_recognized_install_dir(path) and not is_dangerous_install_target(path))


def install_target_error(path: str) -> str:
    if looks_like_infernux_project(path):
        return (
            "The selected directory looks like an Infernux project. "
            "Choose a separate application install folder so project files cannot be overwritten or removed."
        )
    if looks_like_infernux_user_data(path):
        return (
            "The selected directory looks like the Infernux user data/cache folder. "
            "It stores project records and downloaded engine versions, so Hub must not be installed there."
        )
    return ""
