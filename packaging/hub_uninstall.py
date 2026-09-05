"""Remove a marked Hub application, preserving its user-owned Shared tree."""
from __future__ import annotations

import json
from pathlib import Path
import shutil


MARKER = ".infernux-hub-install.json"


def remove_application(install_dir: str | Path) -> None:
    root = Path(install_dir).resolve()
    marker = json.loads((root / MARKER).read_text(encoding="utf-8"))
    if marker.get("tool") != "Infernux Hub" or marker.get("kind") != "install-directory":
        raise ValueError("Uninstaller requires a marked Hub installation")
    if (root / "Assets").is_dir() and (root / "ProjectSettings").is_dir():
        raise ValueError("Uninstaller cannot remove a project directory")
    targets = []
    for path in root.iterdir():
        if path.name == MARKER:
            continue
        if path.name.casefold() == "infernuxhubdata":
            if path.is_symlink() or path.is_junction():
                raise ValueError("Hub data directory must not redirect outside the installation")
            targets.extend(child for child in path.iterdir() if child.name.casefold() != "shared")
        else:
            targets.append(path)
    for path in targets:
        # Unlink redirects themselves; never follow them into another tree.
        if path.is_symlink():
            path.unlink()
        elif path.is_junction():
            path.rmdir()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    # Keep the marker so reinstall can recognize the surviving Shared tree.
