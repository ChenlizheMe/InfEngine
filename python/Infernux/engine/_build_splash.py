"""BuildSplashMixin — extracted from GameBuilder."""
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
from Infernux.engine.i18n import t
from Infernux.engine.nuitka_builder import NuitkaBuilder


class BuildSplashMixin:
    """BuildSplashMixin method group for GameBuilder."""

    def _process_splash_items(self, final_dir: str):
        """Copy/convert splash items into Data/Splash/."""
        if not self.splash_items:
            return

        splash_dir = os.path.join(final_dir, "Data", "Splash")
        os.makedirs(splash_dir, exist_ok=True)
        staged_by_guid: dict[str, str] = {}
        if self._built_icon_path and self.icon_guid:
            staged_by_guid[self.icon_guid] = self._built_icon_path

        for item in self.splash_items:
            asset_guid = item["asset_guid"]
            src_path = self._asset_source_for_guid(asset_guid, "Splash item")
            item_type = item.get("type", "image")
            if item_type not in {"image", "video"}:
                raise ValueError(f"Unsupported splash item type: {item_type}")
            item["_layout"] = (
                "logo"
                if item_type == "image" and asset_guid == self.icon_guid
                else "contain" if item_type == "image" else "cover"
            )
            existing = staged_by_guid.get(asset_guid)
            if existing:
                item["_built_path"] = existing
                continue
            base_name = os.path.splitext(os.path.basename(src_path))[0]

            if item_type == "video":
                out_name = base_name + ".infsplash"
                out_path = os.path.join(splash_dir, out_name)
                if os.path.exists(out_path):
                    raise RuntimeError(f"Splash destination collision: {out_path}")
                self._extract_video_frames(src_path, out_path)
                item["_built_path"] = f"Splash/{out_name}"
            else:
                ext = os.path.splitext(src_path)[1]
                out_name = base_name + ext
                out_path = os.path.join(splash_dir, out_name)
                if os.path.exists(out_path):
                    raise RuntimeError(f"Splash destination collision: {out_path}")
                shutil.copy2(src_path, out_path)
                item["_built_path"] = f"Splash/{out_name}"
            staged_by_guid[asset_guid] = item["_built_path"]

    def _extract_video_frames(self, video_path: str, output_path: str):
        """Extract video frames to .infsplash binary blob."""
        from Infernux.engine.game_builder import _ensure_video_splash_packages
        _ensure_video_splash_packages()
        self._extract_with_imageio(video_path, output_path)

    def _extract_with_imageio(self, video_path: str, output_path: str):
        """Extract video frames using imageio+av."""
        import imageio.v3 as iio

        frames_data: list[bytes] = []
        width = height = 0
        for frame in iio.imiter(video_path, plugin="pyav"):
            height, width = frame.shape[:2]
            jpeg_bytes = iio.imwrite(
                "<bytes>", frame, extension=".jpg", quality=85
            )
            frames_data.append(jpeg_bytes)

        meta = iio.immeta(video_path, plugin="pyav")
        fps = meta.get("fps", 30.0) or 30.0
        self._write_infsplash(output_path, frames_data, fps, width, height)

    @staticmethod
    def _write_infsplash(
        path: str, frames: list, fps: float, width: int, height: int
    ):
        """Write .infsplash binary (magic + header + index + JPEG data)."""
        with open(path, "wb") as f:
            f.write(b"INFSPLSH")
            f.write(struct.pack("<IfII", len(frames), fps, width, height))
            offset = 0
            for data in frames:
                f.write(struct.pack("<II", offset, len(data)))
                offset += len(data)
            for data in frames:
                f.write(data)

