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
import time
from typing import Callable, Dict, List, Optional

import Infernux._jit_kernels as _jit_kernels
from Infernux.debug import Debug
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
        removed = sorted(
            {
                self._normalized_requirement_name(line)
                for line in lines
                if self._is_game_build_excluded_requirement(line)
                and self._normalized_requirement_name(line)
            }
        )
        Debug.log_internal(
            "Filtered non-runtime requirements from requirements.txt: "
            + ", ".join(removed)
        )
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

        The results are de-duplicated, stdlib/engine names are filtered out,
        and only packages actually installed in the current environment are
        returned (to avoid Nuitka errors on typos or conditional imports).
        """
        import ast
        import importlib.util
        import re

        found: set[str] = set()
        uses_infernux_jit = False
        direct_parallel_runtime_imports: set[str] = set()
        _t0 = time.perf_counter()

        # --- Source 1: project requirements.txt -------------------------
        req_path = os.path.join(self.project_path, "requirements.txt")
        if os.path.isfile(req_path):
            Debug.log_internal(f"Found project requirements.txt: {req_path}")
            with open(req_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue
                    # Strip version specifiers: "torch>=2.0" → "torch"
                    pkg = re.split(r"[><=!;\[]", line, maxsplit=1)[0].strip()
                    if pkg:
                        found.add(pkg)
        Debug.log_internal(
            f"  requirements.txt parsed in {time.perf_counter() - _t0:.3f}s"
        )

        # --- Source 2: AST import scanning ------------------------------
        _ast_t0 = time.perf_counter()
        _ast_file_count = 0
        assets_dir = os.path.join(self.project_path, "Assets")
        if os.path.isdir(assets_dir):
            for root, _, files in os.walk(assets_dir):
                for fname in files:
                    if not fname.endswith(".py"):
                        continue
                    fpath = os.path.join(root, fname)
                    _ast_file_count += 1
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
        Debug.log_internal(
            f"  AST scanned {_ast_file_count} .py files in "
            f"{time.perf_counter() - _ast_t0:.3f}s"
        )

        # --- Filter: remove stdlib / engine / excluded ------------------
        found -= self._BUILTIN_MODULES
        found -= self._collect_internal_asset_module_names()
        excluded_imports = self._game_build_excluded_packages()
        skipped = {
            pkg for pkg in found
            if pkg.lower().replace("_", "-") in excluded_imports
        }
        if skipped:
            Debug.log_internal(
                "Skipping game-build-excluded dependencies: "
                + ", ".join(sorted(skipped))
            )
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

        # Only keep packages that are actually importable in the current
        # environment so Nuitka doesn't error on stale or optional imports.
        _verify_t0 = time.perf_counter()
        verified: list[str] = []
        for pkg in sorted(found):
            if importlib.util.find_spec(pkg) is not None:
                verified.append(pkg)
            else:
                Debug.log_warning(
                    f"User script dependency '{pkg}' not installed — skipping"
                )
        Debug.log_internal(
            f"  import verification in {time.perf_counter() - _verify_t0:.3f}s"
        )

        if verified:
            Debug.log_internal(
                f"User dependencies to bundle: {', '.join(verified)}"
            )
        return verified

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

