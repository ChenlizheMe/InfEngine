"""
SplashPlayer — renders a sequence of splash images / videos at game startup.

Supports two item types:

* **image** — loaded via the C++ texture preview task system, rendered with
  an explicit logo or contain layout and configurable fade timing.
* **video** — pre-extracted at build time into ``.infsplash`` binary blobs
  (JPEG frame sequences).  Frames are decoded on a C++ worker thread via
  ``schedule_texture_preview_from_memory()`` and rendered at the original FPS.

Every package carries one explicit current layout for each item.  The runtime
does not infer layout from filenames or accept an older incomplete record.
"""

from __future__ import annotations

import math
import os
import struct
import time as _time
from typing import Dict, List, Optional
from Infernux.engine.path_utils import resolved_path
from Infernux.engine.texture_task_bridge import texture_stamp, query_or_schedule_texture


class SplashPlayer:
    """Plays a list of splash items sequentially, then reports completion."""

    def __init__(self, splash_items: List[Dict], data_root: str):
        self._items = [self._validate_item(item) for item in splash_items]
        self._data_root = data_root
        self._idx = 0
        self._start: Optional[float] = None
        self._finished = len(self._items) == 0

        # GPU texture currently displayed
        self._tex_id: int = 0
        self._img_w: int = 0
        self._img_h: int = 0
        self._tex_resource_key: str = ""
        self._image_path: str = ""
        self._image_stamp: int = 0

        # Video state
        self._vfile = None
        self._vindex: list = []  # [(offset, size), ...]
        self._vfps: float = 30.0
        self._vdata_offset: int = 0
        self._vlast_frame: int = -1

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_finished(self) -> bool:
        return self._finished

    def update(self, ctx, native_engine, x0: float, y0: float,
               vp_w: float, vp_h: float):
        """Call once per frame to advance & render the splash sequence."""
        if self._finished:
            return

        # The splash is an active Player animation even though gameplay has not
        # entered Play yet. Keep the native window out of its editor-only 10 FPS
        # idle tier for the complete fade rather than relying on input events to
        # wake individual frames.
        native_engine.request_full_speed_frame()
        now = _time.monotonic()

        if self._start is None:
            self._start = now
            self._load_item(native_engine)

        item = self._items[self._idx]
        elapsed = now - self._start
        duration = self._item_duration(item)
        alpha = self._alpha(elapsed, duration,
                            item["fade_in"], item["fade_out"])

        # Video: advance frame
        if item["type"] == "video" and self._vindex:
            target = min(int(elapsed * self._vfps), len(self._vindex) - 1)
            if target != self._vlast_frame:
                self._vlast_frame = target
                self._upload_video_frame(native_engine, target)

        if item["type"] != "video" and not self._tex_id:
            self._poll_image_texture(native_engine)

        # Render using the package's explicit current layout.
        if self._tex_id:
            rx, ry, rw, rh = self._layout_rect(
                item["layout"], vp_w, vp_h, self._img_w, self._img_h)
            ctx.draw_image_rect(
                self._tex_id,
                x0 + rx, y0 + ry, x0 + rx + rw, y0 + ry + rh,
                0.0, 0.0, 1.0, 1.0,        # uv
                1.0, 1.0, 1.0, alpha,       # tint RGBA
            )

        # Advance?
        if elapsed >= duration:
            self._unload_item(native_engine)
            self._idx += 1
            if self._idx >= len(self._items):
                self._finished = True
            else:
                self._start = now
                self._load_item(native_engine)

    def cleanup(self, native_engine):
        """Release any held resources."""
        self._unload_item(native_engine)

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def _item_duration(self, item: Dict) -> float:
        if item["type"] == "video" and self._vindex:
            return len(self._vindex) / max(self._vfps, 1.0)
        return item["duration"]

    @staticmethod
    def _layout_rect(layout: str, vp_w: float, vp_h: float,
                     img_w: int, img_h: int):
        if img_w <= 0 or img_h <= 0 or vp_w <= 0 or vp_h <= 0:
            raise RuntimeError("Splash layout dimensions must be positive")
        if layout == "logo":
            scale = min(vp_w * 0.45 / img_w, vp_h * 0.45 / img_h)
        elif layout == "contain":
            scale = min(vp_w / img_w, vp_h / img_h)
        elif layout == "cover":
            scale = max(vp_w / img_w, vp_h / img_h)
        else:
            raise RuntimeError(f"Unsupported splash layout: {layout!r}")
        w = img_w * scale
        h = img_h * scale
        x = (vp_w - w) * 0.5
        y = (vp_h - h) * 0.5
        return (x, y, w, h)

    @staticmethod
    def _validate_item(source: Dict) -> Dict:
        if not isinstance(source, dict):
            raise TypeError("Player splash item must be an object")
        required = {"type", "path", "layout", "duration", "fade_in", "fade_out"}
        if set(source) != required:
            raise ValueError("Player splash item must use the current exact field set")
        item_type = source["type"]
        layout = source["layout"]
        if item_type not in {"image", "video"}:
            raise ValueError(f"Unsupported splash item type: {item_type!r}")
        if layout not in {"logo", "contain", "cover"}:
            raise ValueError(f"Unsupported splash layout: {layout!r}")
        if item_type == "video" and layout != "cover":
            raise ValueError("Video splash items must use cover layout")
        if not isinstance(source["path"], str) or not source["path"]:
            raise ValueError("Player splash path must be a non-empty string")
        for field in ("duration", "fade_in", "fade_out"):
            if isinstance(source[field], bool) or not isinstance(source[field], (int, float)):
                raise TypeError(f"Player splash {field} must be numeric")
        duration = float(source["duration"])
        fade_in = float(source["fade_in"])
        fade_out = float(source["fade_out"])
        if (
            not math.isfinite(duration)
            or not math.isfinite(fade_in)
            or not math.isfinite(fade_out)
            or duration <= 0.0
            or fade_in < 0.0
            or fade_out < 0.0
        ):
            raise ValueError("Player splash timing is invalid")
        return {
            "type": item_type,
            "path": source["path"],
            "layout": layout,
            "duration": duration,
            "fade_in": fade_in,
            "fade_out": fade_out,
        }

    @staticmethod
    def _alpha(elapsed: float, duration: float,
               fade_in: float, fade_out: float) -> float:
        if elapsed < fade_in:
            return elapsed / fade_in if fade_in > 0 else 1.0
        if elapsed > duration - fade_out:
            remaining = max(0.0, duration - elapsed)
            return remaining / fade_out if fade_out > 0 else 0.0
        return 1.0

    # ------------------------------------------------------------------
    # Loading / unloading
    # ------------------------------------------------------------------

    def _load_item(self, native_engine):
        item = self._items[self._idx]
        path = os.path.join(self._data_root, item["path"])
        if item["type"] == "video":
            self._load_video(native_engine, path)
        else:
            self._load_image(native_engine, path)

    def _load_image(self, native_engine, path: str):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Player splash image is missing: {path}")
        stamp = texture_stamp(path, "splash_image")
        if stamp == 0:
            raise RuntimeError(f"Player splash image has no texture stamp: {path}")
        self._image_path = path
        self._image_stamp = stamp
        self._tex_resource_key = f"splash|{resolved_path(path)}"

    def _poll_image_texture(self, native_engine):
        if not self._image_path or not self._image_stamp or not self._tex_resource_key:
            return
        tex_id, tex_w, tex_h = query_or_schedule_texture(
            native_engine,
            self._tex_resource_key,
            self._image_path,
            self._image_stamp,
            nearest=False,
            srgb=True,
            pump=True,
        )
        self._tex_id = int(tex_id)
        self._img_w = int(tex_w)
        self._img_h = int(tex_h)

    def _load_video(self, native_engine, path: str):
        f = open(path, "rb")
        magic = f.read(8)
        if magic != b"INFSPLSH":
            f.close()
            raise RuntimeError(f"Player video splash header is invalid: {path}")

        hdr = f.read(16)
        count, self._vfps, _w, _h = struct.unpack("<IfII", hdr)
        self._img_w = _w
        self._img_h = _h

        idx_data = f.read(count * 8)
        self._vindex = [
            struct.unpack_from("<II", idx_data, i * 8)
            for i in range(count)
        ]
        self._vdata_offset = f.tell()
        self._vfile = f
        self._vlast_frame = -1

        # Upload first frame immediately
        if self._vindex:
            self._upload_video_frame(native_engine, 0)
            self._vlast_frame = 0

    def _upload_video_frame(self, native_engine, frame_idx: int):
        if not self._vfile or frame_idx >= len(self._vindex):
            raise RuntimeError("Player video splash frame is outside the current stream")
        offset, size = self._vindex[frame_idx]
        self._vfile.seek(self._vdata_offset + offset)
        jpeg = self._vfile.read(size)

        self._tex_resource_key = f"splash_video|{frame_idx}"
        stamp = frame_idx + 1  # simple per-frame stamp
        native_engine.schedule_texture_preview_from_memory(
            self._tex_resource_key, jpeg, stamp, False,
        )
        native_engine.pump_preview_tasks()
        tex_id = int(native_engine.get_texture_preview_texture_id(self._tex_resource_key))
        if tex_id != 0:
            self._tex_id = tex_id

    def _unload_item(self, native_engine):
        if self._tex_id:
            if self._tex_resource_key:
                native_engine.invalidate_texture_preview_task(self._tex_resource_key)
            self._tex_id = 0
        self._tex_resource_key = ""
        self._image_path = ""
        self._image_stamp = 0
        if self._vfile:
            self._vfile.close()
            self._vfile = None
        self._vindex = []
        self._vlast_frame = -1
