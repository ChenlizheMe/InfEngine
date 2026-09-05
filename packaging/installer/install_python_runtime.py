from __future__ import annotations

import argparse
import csv
import ctypes
import logging
import os
import sys
import subprocess
from pathlib import Path


_PACKAGING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGING_DIR not in sys.path:
    sys.path.insert(0, _PACKAGING_DIR)

from embed_runtime_manager import PythonRuntimeManager
from hub_utils import get_hub_shared_data_dir
from python_runtime_catalog import DEFAULT_PYTHON_RUNTIME


def _show_message_box(title: str, message: str, icon: int = 0x40) -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, icon)
    except Exception as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
        pass


def prepare_shared_storage(app_dir: str) -> str:
    """Grant the installing account access to Shared, never to Hub program files."""
    shared = Path(get_hub_shared_data_dir(str(Path(app_dir).resolve())))
    if shared.resolve() != shared:
        raise RuntimeError("Installer shared storage must not redirect to another directory")
    shared.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        system = Path(os.environ["SystemRoot"]) / "System32"
        options = dict(
            check=True, capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=0x08000000,
        )
        identity = subprocess.run(
            [str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"], **options,
        )
        sid = next(csv.reader(identity.stdout.splitlines()))[1]
        subprocess.run(
            [str(system / "icacls.exe"), str(shared), "/grant", f"*{sid}:(OI)(CI)M"],
            **options,
        )
    return str(shared)


def install_runtime_for_app(app_dir: str, progress_callback=None) -> str:
    bundle_runtime_dir = os.path.join(app_dir, "InfernuxHubData", "runtime")
    shared = prepare_shared_storage(app_dir)
    manager = PythonRuntimeManager(
        runtime_dir=os.path.join(shared, "Runtimes"),
        bundle_runtime_dir=bundle_runtime_dir,
        default_version=DEFAULT_PYTHON_RUNTIME,
    )
    return manager.ensure_runtime(
        version=DEFAULT_PYTHON_RUNTIME,
        on_status=progress_callback,
        allow_frozen_repair=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir")
    args = parser.parse_args()

    if not args.app_dir:
        _show_message_box(
            "Infernux Runtime Installer",
            "This program is an internal installer helper for Infernux Hub.\n\n"
            "Please run InfernuxHubInstaller.exe instead of launching this file directly.",
            0x30,
        )
        return 1

    install_runtime_for_app(args.app_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _show_message_box(
            "Infernux Runtime Installer Error",
            str(exc),
            0x10,
        )
        try:
            sys.stderr.write(str(exc) + "\n")
        except Exception as _exc:
            logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
            pass
        raise SystemExit(1)
