"""Apply a staged Infernux Hub update after the Hub process exits."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath


def _safe_path(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError("Hub update paths must be strings")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise ValueError(f"Unsafe Hub update path: {value!r}")
    return path


def _load_metadata(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {
        "$schema",
        "product",
        "base_version",
        "target_version",
        "files",
        "delete",
    }:
        raise ValueError("Hub update metadata does not match the current contract")
    if (
        document["$schema"] != "infernux.hub_update"
        or document["product"] != "InfernuxHub"
        or not isinstance(document["files"], list)
        or not isinstance(document["delete"], list)
    ):
        raise ValueError("Hub update metadata does not match the current contract")
    for entry in document["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path"}:
            raise ValueError("Hub update file entry is invalid")
        _safe_path(entry["path"])
    for value in document["delete"]:
        _safe_path(value)
    return document


def _wait_for_exit(process_id: int) -> None:
    while True:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)


def apply_update(
    *,
    parent_pid: int,
    install_dir: Path,
    stage_dir: Path,
    metadata_path: Path,
) -> None:
    metadata = _load_metadata(metadata_path)
    files = [_safe_path(entry["path"]) for entry in metadata["files"]]
    removed = [_safe_path(value) for value in metadata["delete"]]
    for relative in files:
        if not stage_dir.joinpath(*relative.parts).is_file():
            raise FileNotFoundError(f"Staged Hub file is missing: {relative}")

    _wait_for_exit(parent_pid)
    backup = metadata_path.parent / "backup"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    affected = list(dict.fromkeys([*files, *removed]))
    previously_present: set[PurePosixPath] = set()
    applied: list[PurePosixPath] = []
    try:
        for relative in affected:
            live = install_dir.joinpath(*relative.parts)
            if live.is_file():
                saved = backup.joinpath(*relative.parts)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live, saved)
                previously_present.add(relative)
        for relative in files:
            source = stage_dir.joinpath(*relative.parts)
            destination = install_dir.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            applied.append(relative)
        for relative in removed:
            target = install_dir.joinpath(*relative.parts)
            if target.is_file():
                target.unlink()
            applied.append(relative)

        executable = install_dir / "Infernux Hub"
        if not executable.is_file():
            raise FileNotFoundError(f"Updated Hub executable is missing: {executable}")
    except Exception:
        for relative in reversed(applied):
            live = install_dir.joinpath(*relative.parts)
            saved = backup.joinpath(*relative.parts)
            if relative in previously_present:
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, live)
            elif live.is_file():
                live.unlink()
        raise

    subprocess.Popen(
        [os.fspath(executable)],
        cwd=install_dir,
        start_new_session=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    arguments = parser.parse_args()
    apply_update(
        parent_pid=arguments.parent_pid,
        install_dir=arguments.install_dir.resolve(),
        stage_dir=arguments.stage_dir.resolve(),
        metadata_path=arguments.metadata.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
