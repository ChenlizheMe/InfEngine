"""
ToneMappingEffect — HDR-to-LDR tone mapping post-processing effect.

Compresses linear HDR scene color into linear LDR range. Should be the
last effect in the post-process stack (runs at ``after_post_process``)
so that bloom and other HDR effects are applied first. The output stays
linear; the built-in display-encode pass performs the final linear→sRGB
conversion for the UNORM swapchain.

Supported operators:
    - Reinhard
    - ACES Filmic (default — matches Unity/Unreal look)
"""

from __future__ import annotations

from enum import IntEnum
from typing import List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.components.fields import serialized_field

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class ToneMappingMode(IntEnum):
    """Tone mapping operator."""
    None_    = 0
    Reinhard = 1
    ACES     = 2


class ToneMappingEffect(FullScreenEffect):
    """HDR-to-LDR tone mapping post-processing effect.

    Should be the last effect in the post-process chain so that bloom
    and other HDR effects can operate on the full dynamic range.
    """

    name = "Tone Mapping"
    injection_point = "after_post_process"
    default_order = 900          # high order → runs last within its injection point
    menu_path = "Post-processing/Tone Mapping"

    # ---- Serialized parameters (shown in Inspector) ----
    mode: ToneMappingMode = serialized_field(
        default=ToneMappingMode.ACES,
        tooltip="Tone mapping operator (ACES is recommended for realistic look)",
    )
    exposure: float = serialized_field(
        default=1.0,
        range=(0.01, 10.0),
        drag_speed=0.05,
        slider=False,
        tooltip="Pre-tonemap exposure multiplier",
    )

    @staticmethod
    def _normalize_mode_value(value) -> ToneMappingMode:
        """Ensure the mode value is a valid ToneMappingMode enum member."""
        if isinstance(value, ToneMappingMode):
            return value
        try:
            return ToneMappingMode(int(value))
        except (ValueError, KeyError):
            return ToneMappingMode.ACES

    def set_params_dict(self, params):
        super().set_params_dict(params)
        self.mode = self._normalize_mode_value(self.mode)

    # ------------------------------------------------------------------
    # FullScreenEffect interface
    # ------------------------------------------------------------------

    def get_shader_list(self) -> List[str]:
        return [
            "Fullscreen Triangle",
            "Tonemapping",
        ]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        """Inject the tonemapping pass into the render graph."""
        from Infernux.rendergraph.graph import Format

        mode = self._normalize_mode_value(self.mode)
        self.mode = mode
        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_tonemap_out",
            pass_name="ToneMap_Apply",
            shader_name="Tonemapping",
            format=Format.RGBA16_SFLOAT,
            params={
                "mode": float(int(mode)),
                "exposure": self.exposure,
            },
        )
