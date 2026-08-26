"""Generic project-level dependency checker.

On every engine startup (editor or player) this module reads
``<project>/ProjectSettings/requirements.txt`` and ensures that every
listed package is importable.  In editor mode missing packages are
installed automatically; in player mode the check is informational only.

Users can customise their project environment by editing the file.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import subprocess
import sys

from Infernux.engine.path_utils import resolved_path

_log = logging.getLogger("Infernux.project_requirements")
_CHECK_ENV = "_INFERNUX_PROJECT_REQS_CHECKED"

# Packages whose importable name differs from the pip name.
_IMPORT_NAME_MAP: dict[str, str] = {
    "pillow": "PIL",
    "opencv-python": "cv2",
    "pyyaml": "yaml",
    "scikit-learn": "sklearn",
    "ordered-set": "ordered_set",
}


def _bundled_requirements_path() -> str:
    """Return the path to the default requirements.txt shipped inside the engine wheel."""
    # Resolve relative to this file's package location to avoid triggering
    # the full Infernux import chain (which needs the native C++ module).
    _engine_dir = os.path.dirname(resolved_path(__file__))
    _infernux_dir = os.path.dirname(_engine_dir)
    return os.path.join(
        _infernux_dir, "resources", "project_templates", "requirements.txt"
    )


# ── Helpers ──────────────────────────────────────────────────────────

def _run_python(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run([sys.executable, *args], **kwargs)


def _has_module(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _ensure_pip() -> bool:
    completed = _run_python(["-m", "pip", "--version"], timeout=60)
    if completed.returncode == 0:
        return True
    completed = _run_python(["-m", "ensurepip", "--upgrade"], timeout=600)
    if completed.returncode != 0:
        return False
    completed = _run_python(["-m", "pip", "--version"], timeout=60)
    return completed.returncode == 0


def _pip_name_to_import(pip_name: str) -> str:
    """Best-effort conversion of a pip package name to an importable module."""
    key = pip_name.lower()
    if key in _IMPORT_NAME_MAP:
        return _IMPORT_NAME_MAP[key]
    # Common convention: dashes → underscores
    return key.replace("-", "_")


def _parse_requirements(path: str) -> list[tuple[str, str]]:
    """Return ``[(pip_spec, import_name), ...]`` from a requirements file."""
    entries: list[tuple[str, str]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Extract bare package name for import check
            pkg = re.split(r"[><=!;\[\s]", line, maxsplit=1)[0].strip()
            if pkg:
                entries.append((line, _pip_name_to_import(pkg)))
    return entries


def _install_packages(specs: list[str]) -> bool:
    """pip-install a list of requirement specifiers."""
    completed = _run_python(
        [
            "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-input",
            "--prefer-binary",
            "--upgrade",
            *specs,
        ],
        timeout=1800,
    )
    return completed.returncode == 0


# ── Public API ───────────────────────────────────────────────────────

def requirements_path(project_path: str) -> str:
    """Return the canonical path to the project requirements file."""
    return os.path.join(project_path, "ProjectSettings", "requirements.txt")


def ensure_project_requirements(
    project_path: str,
    *,
    auto_install: bool = True,
) -> bool:
    """Check (and optionally install) packages listed in ProjectSettings/requirements.txt.

    Returns ``True`` when all listed packages are importable after the check.
    The check runs at most once per process (guarded by an env-var flag).
    """
    req_file = requirements_path(project_path)
    if not os.path.isfile(req_file):
        # Copy the default requirements from the engine resources
        bundled = _bundled_requirements_path()
        if os.path.isfile(bundled):
            import shutil
            os.makedirs(os.path.dirname(req_file), exist_ok=True)
            shutil.copy2(bundled, req_file)
            _log.info("Copied default requirements to %s", req_file)
        else:
            return True  # no bundled file either — nothing to check

    entries = _parse_requirements(req_file)
    if not entries:
        return True

    # Find missing packages
    missing: list[tuple[str, str]] = []  # (pip_spec, import_name)
    for pip_spec, import_name in entries:
        if not _has_module(import_name):
            missing.append((pip_spec, import_name))

    if not missing:
        return True

    names = ", ".join(m for _, m in missing)

    if not auto_install:
        _log.warning("Project requirements check: missing packages: %s", names)
        return False

    # Only attempt auto-install once per process
    if os.environ.get(_CHECK_ENV) == "1":
        return False
    os.environ[_CHECK_ENV] = "1"

    _log.info("Auto-installing missing project requirements: %s", names)

    if not _ensure_pip():
        _log.warning("Project requirements check failed: pip is unavailable.")
        return False

    specs = [s for s, _ in missing]
    if not _install_packages(specs):
        _log.warning("Project requirements check failed: pip install returned an error.")
        return False

    # Verify that all packages are now importable
    importlib.invalidate_caches()
    still_missing = [m for _, m in missing if not _has_module(m)]
    if still_missing:
        _log.warning(
            "Project requirements check: still missing after install: %s",
            ", ".join(still_missing),
        )
        return False

    return True
