"""Platform-independent window, framebuffer, safe-area, and keyboard metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from Infernux.lib import InputManager as _NativeInputManager


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Insets:
    left: int
    top: int
    right: int
    bottom: int


class _ScreenMeta(type):
    @staticmethod
    def _state():
        return _NativeInputManager.instance().screen_state

    @property
    def revision(cls) -> int:
        return int(cls._state().revision)

    @property
    def size(cls) -> Tuple[int, int]:
        state = cls._state()
        return (int(state.logical_width), int(state.logical_height))

    @property
    def framebuffer_size(cls) -> Tuple[int, int]:
        state = cls._state()
        return (int(state.framebuffer_width), int(state.framebuffer_height))

    @property
    def pixel_ratio(cls) -> float:
        return float(cls._state().pixel_ratio)

    @property
    def safe_area(cls) -> Rect:
        state = cls._state()
        return Rect(
            int(state.safe_area_x),
            int(state.safe_area_y),
            int(state.safe_area_width),
            int(state.safe_area_height),
        )

    @property
    def safe_insets(cls) -> Insets:
        state = cls._state()
        width = int(state.logical_width)
        height = int(state.logical_height)
        x = int(state.safe_area_x)
        y = int(state.safe_area_y)
        safe_width = int(state.safe_area_width)
        safe_height = int(state.safe_area_height)
        return Insets(
            x,
            y,
            max(0, width - x - safe_width),
            max(0, height - y - safe_height),
        )

    @property
    def keyboard_inset(cls) -> Optional[int]:
        state = cls._state()
        if not bool(state.keyboard_inset_known):
            return None
        return int(state.keyboard_inset)

    @property
    def focused(cls) -> bool:
        return bool(cls._state().focused)

    @property
    def occluded(cls) -> bool:
        return bool(cls._state().occluded)


class Screen(metaclass=_ScreenMeta):
    """Live game-window metrics in logical units.

    ``safe_area`` is derived from SDL WindowInsets on Android and CSS
    ``safe-area-inset-*`` on Web. ``keyboard_inset`` is ``None`` when the
    platform cannot report it reliably; no fixed keyboard height is guessed.
    """


__all__ = ["Insets", "Rect", "Screen"]
