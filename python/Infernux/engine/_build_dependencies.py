"""BuildDependencyMixin — extracted from GameBuilder."""
from __future__ import annotations

"""
GameBuilder — packages a standalone native game from an Infernux project.

Uses **Nuitka** to compile the Python entry script into a native EXE.
All engine code, dependencies, and the CPython runtime are bundled into
a self-contained directory.  User scripts (.py in Assets/) are compiled
to .pyc with ``py_compile`` for source protection.

Output layout::

    <OutputDir>/
        <GameName>.exe          ← Nuitka-compiled native executable
        python313.dll           ← CPython runtime (required by Nuitka)
        SDL3.dll, imgui.dll … ← engine native DLLs (also in Infernux/lib/)
        Infernux/              ← engine package
            lib/
                _Infernux.*.pyd ← pybind11 extension module
                SDL3.dll …       ← DLLs (for os.add_dll_directory)
        Data/
            Assets/             ← game scenes, scripts(.pyc), textures, models
            ProjectSettings/    ← build & tag-layer settings
            materials/
            Splash/             ← splash images + .infsplash video data
            BuildManifest.json  ← display mode, window size, splash config
"""


import json
import os
import py_compile
import re
import shutil
import struct
import subprocess
import sys
import threading
from typing import Callable, Dict, List, Optional

import Infernux._jit_kernels as _jit_kernels
from Infernux.engine.i18n import t
from Infernux.engine.nuitka_builder import NuitkaBuilder


class BuildDependencyMixin:
    """BuildDependencyMixin method group for GameBuilder."""

    @staticmethod
    def _normalized_requirement_name(line: str) -> str:
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("-"):
            return ""
        name = re.split(r"[><=!;\[\s]", text, maxsplit=1)[0].strip()
        return name.lower().replace("_", "-")

    def _game_build_excluded_packages(self) -> set[str]:
        packages = getattr(self, "_GAME_BUILD_EXCLUDED_PACKAGES", frozenset())
        return {str(pkg).lower().replace("_", "-") for pkg in packages}

    def _is_game_build_excluded_requirement(self, line: str) -> bool:
        name = self._normalized_requirement_name(line)
        if not name:
            return False
        if name in self._game_build_excluded_packages():
            return True
        return not bool(getattr(self, "enable_jit", False)) and name in {
            "numba",
            "llvmlite",
        }

    def _write_filtered_game_requirements(self, req_path: str) -> str:
        with open(req_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        filtered = [
            line for line in lines
            if not self._is_game_build_excluded_requirement(line)
        ]
        if len(filtered) == len(lines):
            return req_path

        temp_dir = os.path.join(self.output_dir, "_build_temp")
        os.makedirs(temp_dir, exist_ok=True)
        filtered_path = os.path.join(temp_dir, "requirements.game.txt")
        with open(filtered_path, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(filtered)
        return filtered_path

    def _project_requirement_files(self) -> List[str]:
        req_path = os.path.join(self.project_path, "requirements.txt")
        if os.path.isfile(req_path):
            return [self._write_filtered_game_requirements(req_path)]
        return []

    def _collect_user_dependencies(self) -> List[str]:
        """Scan user scripts for third-party imports and return package names.

        Detection sources (in order of priority):
        1. ``requirements.txt`` in the project root — explicit user list.
           Lines starting with ``#`` or empty lines are ignored.
           Version specifiers are stripped (``torch>=2.0`` → ``torch``).
        2. AST-based import scanning of all ``.py`` files under ``Assets/``.
           Only top-level package names are collected (``import a.b`` → ``a``).

        The results are de-duplicated and stdlib/engine names are filtered out.
        Every remaining package must be installed in the build environment.
        """
        import ast
        import importlib.util
        import re

        found: set[str] = set()
        uses_infernux_jit = False
        direct_parallel_runtime_imports: set[str] = set()

        # --- Source 1: project requirements.txt -------------------------
        req_path = os.path.join(self.project_path, "requirements.txt")
        if os.path.isfile(req_path):
            with open(req_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue
                    # Strip version specifiers: "torch>=2.0" → "torch"
                    pkg = re.split(r"[><=!;\[]", line, maxsplit=1)[0].strip()
                    if pkg:
                        found.add(pkg)

        # --- Source 2: AST import scanning ------------------------------
        assets_dir = os.path.join(self.project_path, "Assets")
        if os.path.isdir(assets_dir):
            for root, _, files in os.walk(assets_dir):
                for fname in files:
                    if not fname.endswith(".py"):
                        continue
                    fpath = os.path.join(root, fname)
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        tree = ast.parse(f.read(), filename=fpath)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                root_name = alias.name.split(".")[0]
                                found.add(root_name)
                                if root_name in {"numba", "llvmlite"}:
                                    direct_parallel_runtime_imports.add(root_name)
                                if alias.name in {"Infernux.jit", "Infernux._jit_kernels"}:
                                    uses_infernux_jit = True
                        elif isinstance(node, ast.ImportFrom):
                            if node.module and node.level == 0:
                                root_name = node.module.split(".")[0]
                                found.add(root_name)
                                if root_name in {"numba", "llvmlite"}:
                                    direct_parallel_runtime_imports.add(root_name)
                                if node.module in {"Infernux.jit", "Infernux._jit_kernels"}:
                                    uses_infernux_jit = True
                                elif node.module == "Infernux":
                                    imported_names = {alias.name for alias in node.names}
                                    if imported_names & {
                                        "jit", "njit", "warmup",
                                        "JIT_AVAILABLE",
                                    }:
                                        uses_infernux_jit = True
        # --- Filter: remove stdlib / engine / excluded ------------------
        found -= self._BUILTIN_MODULES
        found -= self._collect_internal_asset_module_names()
        excluded_imports = self._game_build_excluded_packages()
        skipped = {
            pkg for pkg in found
            if pkg.lower().replace("_", "-") in excluded_imports
        }
        found -= skipped

        enable_jit = bool(getattr(self, "enable_jit", False))
        if direct_parallel_runtime_imports and not enable_jit:
            names = ", ".join(sorted(direct_parallel_runtime_imports))
            raise RuntimeError(
                "Auto Parallel is disabled, but project scripts directly import "
                f"{names}. Enable Auto Parallel or use the public Infernux.jit "
                "API so the build can provide its serial fallback."
            )

        # The public JIT API only pulls in the native parallel runtime when the
        # product explicitly enables it. With JIT disabled, Infernux.jit stays
        # importable and its decorator resolves to the source-level serial
        # fallback without shipping Numba/LLVM. NumPy remains independent: a
        # project that imports it directly still receives its normal package.
        if enable_jit and (uses_infernux_jit or "numba" in found or "llvmlite" in found):
            found.add("numba")
            found.add("llvmlite")
            found.add("numpy")
        elif not enable_jit:
            found.discard("numba")
            found.discard("llvmlite")

        dependencies = sorted(found)
        missing = [
            package
            for package in dependencies
            if importlib.util.find_spec(package) is None
        ]
        if missing:
            raise RuntimeError(
                "Player build dependencies are not installed: "
                + ", ".join(missing)
            )
        return dependencies

    def _collect_internal_asset_module_names(self) -> set[str]:
        """Return top-level module names that belong to the project's Assets tree."""
        names: set[str] = {"Assets"}
        assets_dir = os.path.join(self.project_path, "Assets")
        if not os.path.isdir(assets_dir):
            return names

        for entry in os.scandir(assets_dir):
            name = entry.name
            if name.startswith(".") or name in {"__pycache__"}:
                continue
            if entry.is_dir():
                names.add(name)
                continue
            stem, ext = os.path.splitext(name)
            if ext in {".py", ".pyc"} and stem and not stem.startswith("_"):
                names.add(stem)
        return names

