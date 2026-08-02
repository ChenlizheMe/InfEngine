from __future__ import annotations

import os
import zlib
from typing import Tuple

from Infernux.engine.path_utils import resolved_path


_U64 = 0xFFFFFFFFFFFFFFFF


def safe_mtime_ns(path: str) -> int:
    try:
        return int(os.stat(path).st_mtime_ns)
    except OSError:
        return 0


def texture_stamp(path: str, *tags: str) -> int:
    image_mtime = safe_mtime_ns(path)
    meta_mtime = safe_mtime_ns(f"{path}.meta")
    stamp = int((image_mtime ^ ((meta_mtime * 2654435761) & _U64)) & _U64)
    if stamp == 0:
        return 0

    if tags:
        seed = 0
        for t in tags:
            seed = int(zlib.crc32(str(t).encode("utf-8"), seed) & 0xFFFFFFFF)
        stamp = int((stamp ^ seed) & _U64)
    return stamp


def query_or_schedule_texture(
    native,
    resource_key: str,
    texture_file_path: str,
    stamp: int,
    *,
    nearest: bool = False,
    srgb: bool = False,
    pump: bool = True,
) -> Tuple[int, int, int]:
    if native is None:
        return 0, 0, 0
    if not resource_key or not texture_file_path or int(stamp) == 0:
        return 0, 0, 0

    tex_id, w, h = native.query_or_schedule_texture_preview(
        resource_key,
        resolved_path(texture_file_path),
        int(stamp),
        nearest=bool(nearest),
        srgb=bool(srgb),
        pump=bool(pump),
    )
    return int(tex_id), int(w), int(h)
