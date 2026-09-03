from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


HUB_EXECUTABLE = "Infernux Hub.exe" if sys.platform == "win32" else "Infernux Hub"
HUB_PAYLOAD_ARCHIVE = "infernux-hub-payload.zip"


def create_payload_archive(
    source_dir: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> Path:
    source = Path(source_dir).resolve()
    output = Path(destination).resolve()
    if not source.is_dir():
        raise RuntimeError(f"Hub payload directory not found: {source}")
    if not (source / HUB_EXECUTABLE).is_file():
        raise RuntimeError(f"Hub payload is missing {HUB_EXECUTABLE}: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for source_file in sorted(
                path for path in source.rglob("*") if path.is_file()
            ):
                archive.write(source_file, source_file.relative_to(source).as_posix())
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _safe_archive_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or relative.is_absolute()
        or any(part in ("", ".", "..") for part in relative.parts)
        or (relative.parts and ":" in relative.parts[0])
    ):
        raise RuntimeError(f"Unsafe path in Hub payload archive: {name!r}")
    return relative


def extract_payload_archive(
    archive_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> None:
    archive_file = Path(archive_path).resolve()
    output = Path(destination).resolve()
    if not archive_file.is_file():
        raise RuntimeError(f"Hub payload archive not found: {archive_file}")
    output.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive_file, mode="r") as archive:
            for entry in archive.infolist():
                relative = _safe_archive_path(entry.filename)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise RuntimeError(
                        f"Links are not allowed in the Hub payload archive: {entry.filename!r}"
                    )
                target = output.joinpath(*relative.parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with (
                    archive.open(entry, mode="r") as source,
                    target.open("wb") as destination_file,
                ):
                    shutil.copyfileobj(source, destination_file, length=1024 * 1024)
                archived_mode = mode & 0o777
                if archived_mode:
                    target.chmod(archived_mode)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Invalid Hub payload archive: {archive_file}") from exc

    if not (output / HUB_EXECUTABLE).is_file():
        raise RuntimeError(f"The Hub payload archive is missing {HUB_EXECUTABLE}.")
