from __future__ import annotations

from typing import Optional, Tuple

class Rect:
    x: int
    y: int
    width: int
    height: int

class Insets:
    left: int
    top: int
    right: int
    bottom: int

class Screen:
    revision: int
    size: Tuple[int, int]
    framebuffer_size: Tuple[int, int]
    pixel_ratio: float
    safe_area: Rect
    safe_insets: Insets
    keyboard_inset: Optional[int]
    focused: bool
    occluded: bool

__all__: list[str]
