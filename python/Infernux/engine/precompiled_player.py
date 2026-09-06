"""Assemble a desktop Player from the selected platform package, never a compiler."""

from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
from pathlib import Path

from Infernux.version import ENGINE_VERSION
from .player_package_native import extract_pack


def inspect_desktop_runtime(root: str) -> dict[str, object]:
    payload = Path(root)
    try:
        manifest = json.loads((payload / "Player.inxmanifest").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            "The platform plugin has no precompiled Player. Install the complete "
            "platform release; game export does not compile the engine."
        ) from exc
    if not isinstance(manifest, dict):
        raise ValueError("Platform Player manifest must be an object")
    machine = platform.machine().casefold().replace("amd64", "x86_64")
    expected = {
        "engine_version": ENGINE_VERSION,
        "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "platform": sys.platform,
    }
    if any(manifest.get(key) != value for key, value in expected.items()) or (
        str(manifest.get("machine", "")).casefold().replace("amd64", "x86_64") != machine
    ):
        raise RuntimeError("The platform Player does not match this engine version, host or Python ABI")
    host = "InfernuxPlayerHost.exe" if sys.platform == "win32" else "InfernuxPlayerHost"
    for filename in ("Runtime.inxrt", host):
        if not (payload / filename).is_file():
            raise FileNotFoundError(f"Platform Player payload is missing: {payload / filename}")
    return manifest


def stage_desktop_runtime(root: str, staging_root: str, *, parallel: bool = False) -> str:
    inspect_desktop_runtime(root)
    payload = Path(root)
    if parallel and not (payload / "Parallel.inxmod").is_file():
        raise FileNotFoundError("The platform package does not carry Parallel.inxmod")
    Path(staging_root).mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="player-", dir=staging_root))
    destination = workspace / "player.dist"
    try:
        destination.mkdir()
        extract_pack(str(payload / "Runtime.inxrt"), str(destination))
        if parallel:
            shutil.copy2(payload / "Parallel.inxmod", destination / "Parallel.inxmod")
        return str(destination)
    except BaseException:
        shutil.rmtree(workspace)
        raise
