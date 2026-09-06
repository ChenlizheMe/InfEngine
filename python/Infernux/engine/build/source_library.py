"""Hub-owned source snapshots shared by platform build plugins."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from Infernux.engine.build.contracts import BuildRequest
from Infernux.plugins.cache import package_cache_root


_SOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class GitSource:
    name: str
    repository: str
    revision: str
    required_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SOURCE_SEGMENT.fullmatch(self.name):
            raise ValueError(f"Invalid shared source name: {self.name!r}")
        if not self.repository.strip():
            raise ValueError("Shared Git source repository is required")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", self.revision):
            raise ValueError("Shared Git source revision must be one full commit ID")
        if not self.required_paths:
            raise ValueError("Shared Git source must declare required paths")


def source_library_root() -> Path:
    """Return the Hub Library directory reserved for immutable source trees."""

    return Path(package_cache_root()).parent / "Sources"


def _repository_path(source: GitSource) -> tuple[str, str]:
    parsed = urlparse(source.repository)
    host = parsed.hostname or "local"
    parts = [part for part in parsed.path.replace("\\", "/").split("/") if part]
    owner = parts[-2] if len(parts) >= 2 else "source"
    for value in (host, owner):
        if not _SOURCE_SEGMENT.fullmatch(value):
            raise ValueError(f"Invalid shared source repository path: {source.repository}")
    return host.casefold(), owner


def _source_destination(source: GitSource) -> Path:
    host, owner = _repository_path(source)
    return source_library_root() / host / owner / source.name / source.revision.casefold()


def _validate_source_tree(destination: Path, source: GitSource) -> None:
    metadata_path = destination / ".infernux-source.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Shared source metadata is unavailable: {metadata_path}") from exc
    if metadata != {
        "repository": source.repository,
        "revision": source.revision.casefold(),
    }:
        raise RuntimeError(f"Shared source identity does not match its Hub Library path: {destination}")
    missing = [path for path in source.required_paths if not (destination / path).is_file()]
    if missing:
        raise RuntimeError(
            f"Shared source tree is incomplete ({', '.join(missing)}): {destination}"
        )


def _remove_source_tree(path: Path) -> None:
    """Remove a Git worktree whose object files are read-only on Windows."""

    if not path.exists():
        return

    def _remove_read_only(function, entry, _error) -> None:
        os.chmod(entry, stat.S_IWRITE)
        function(entry)

    shutil.rmtree(path, onexc=_remove_read_only)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        process.terminate()
    if process.poll() is None:
        process.kill()
    process.wait()


def _run_git(request: BuildRequest, command: list[str], cwd: Path) -> tuple[str, ...]:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output: queue.Queue[str | None] = queue.Queue()

    def _read_output() -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    output.put(line.rstrip())
        finally:
            output.put(None)

    reader = threading.Thread(target=_read_output, name="InfernuxGitSource", daemon=True)
    reader.start()
    lines: list[str] = []
    try:
        while True:
            request.cancellation.raise_if_cancelled()
            try:
                line = output.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                break
            if not line:
                continue
            lines.append(line)
            request.report("dependencies", 0, 0, line[:500], source="git")
        return_code = process.wait()
    except BaseException:
        _terminate_process_tree(process)
        reader.join(timeout=2.0)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    reader.join(timeout=2.0)
    if return_code != 0:
        raise RuntimeError(
            "Unable to acquire shared platform source:\n" + "\n".join(lines[-30:])
        )
    return tuple(lines)


def acquire_git_source(request: BuildRequest, source: GitSource) -> Path:
    """Return one exact Git snapshot from the shared Hub Library."""

    destination = _source_destination(source)
    if destination.is_dir():
        _validate_source_tree(destination, source)
        request.report("dependencies", 1, 1, f"Using shared {source.name} source")
        return destination
    if destination.exists():
        raise RuntimeError(f"Shared source path is not a directory: {destination}")

    staging_parent = source_library_root() / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{source.name}-", dir=staging_parent))
    try:
        request.report("dependencies", 0, 1, f"Downloading {source.name} source")
        _run_git(request, ["git", "init", "--quiet"], staging)
        _run_git(request, ["git", "remote", "add", "origin", source.repository], staging)
        _run_git(
            request,
            [
                "git",
                "fetch",
                "--depth=1",
                "--progress",
                "origin",
                source.revision,
            ],
            staging,
        )
        _run_git(request, ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], staging)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=staging,
            text=True,
            encoding="ascii",
        ).strip().casefold()
        if revision != source.revision.casefold():
            raise RuntimeError(
                f"Downloaded {source.name} revision is {revision}, expected {source.revision}"
            )
        (staging / ".infernux-source.json").write_text(
            json.dumps(
                {
                    "repository": source.repository,
                    "revision": source.revision.casefold(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _validate_source_tree(staging, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
    except BaseException:
        _remove_source_tree(staging)
        raise
    request.report("dependencies", 1, 1, f"Downloaded {source.name} source")
    return destination


__all__ = ["GitSource", "acquire_git_source", "source_library_root"]
