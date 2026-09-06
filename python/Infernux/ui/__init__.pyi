"""Type stubs for Infernux.ui — screen-space UI components."""

from __future__ import annotations

from .enums import (
    RenderMode as RenderMode,
    ScreenAlignH as ScreenAlignH,
    ScreenAlignV as ScreenAlignV,
    TextAlignH as TextAlignH,
    TextAlignV as TextAlignV,
    TextOverflow as TextOverflow,
    TextResizeMode as TextResizeMode,
    UIScaleMode as UIScaleMode,
    ScreenMatchMode as ScreenMatchMode,
    UITransitionType as UITransitionType,
)
from .inx_ui_component import InxUIComponent as InxUIComponent
from .inx_ui_screen_component import InxUIScreenComponent as InxUIScreenComponent
from .ui_canvas import UICanvas as UICanvas
from .ui_text import UIText as UIText
from .ui_image import UIImage as UIImage
from .ui_selectable import UISelectable as UISelectable
from .ui_button import UIButton as UIButton
from .ui_event_data import PointerEventData as PointerEventData, PointerButton as PointerButton, PointerType as PointerType
from .ui_event import UIEvent as UIEvent, UIEvent1 as UIEvent1
from .ui_event_system import UIEventProcessor as UIEventProcessor, UIPointerFrame as UIPointerFrame
from .ui_texture_cache import UITextureCache as UITextureCache, get_shared_cache as get_shared_cache
from .ui_render_dispatch import register_ui_renderer as register_ui_renderer, dispatch as ui_dispatch

__all__ = [
    "RenderMode",
    "ScreenAlignH",
    "ScreenAlignV",
    "TextAlignH",
    "TextAlignV",
    "TextOverflow",
    "TextResizeMode",
    "UIScaleMode",
    "ScreenMatchMode",
    "UITransitionType",
    "InxUIComponent",
    "InxUIScreenComponent",
    "UICanvas",
    "UIText",
    "UIImage",
    "UISelectable",
    "UIButton",
    "PointerEventData",
    "PointerButton",
    "PointerType",
    "UIEvent",
    "UIEvent1",
    "UIEventProcessor",
    "UIPointerFrame",
    "UITextureCache",
    "get_shared_cache",
    "register_ui_renderer",
    "ui_dispatch",
]
