"""Shared gradient field editor for Inspector-style surfaces."""

from __future__ import annotations

from Infernux.engine.i18n import t
from Infernux.graph.ramp import GRADIENT_MODES, MAX_RAMP_KEYS, Gradient

from .dpi import editor_dpi_scale


def render_gradient_property(
    ctx,
    widget_id: str,
    value,
    *,
    semantic_prefix: str = "",
    hdr: bool = False,
):
    """Render an authored Gradient document and return its edited document."""
    from Infernux.engine.ui.inspector_utils import render_color_value_bar

    dpi = editor_dpi_scale(ctx)
    gradient = Gradient.from_dict(value)
    keys = [item.to_dict() for item in gradient.keys]
    mode_index = ctx.combo(
        f"{t('particle_graph_editor.gradient_mode')}##{widget_id}_mode",
        GRADIENT_MODES.index(gradient.mode),
        list(GRADIENT_MODES),
        -1,
    )
    remove_index = -1
    for index, item in enumerate(keys):
        ctx.separator()
        ctx.label(f"{t('particle_graph_editor.key')} {index + 1}")
        minimum = keys[index - 1]["time"] + 1.0e-4 if index else 0.0
        maximum = (
            keys[index + 1]["time"] - 1.0e-4
            if index + 1 < len(keys)
            else 1.0
        )
        item["time"] = float(
            ctx.drag_float(
                f"{t('particle_graph_editor.time')}##{widget_id}_{index}_time",
                item["time"],
                0.01,
                minimum,
                maximum,
            )
        )
        prefix = semantic_prefix or f"gradient.{widget_id}"
        ctx.record_semantic_item(
            "drag_float",
            t("particle_graph_editor.time"),
            True,
            f"{prefix}.key.{index}.time",
            numeric_value=item["time"],
        )
        ctx.align_text_to_frame_padding()
        ctx.label(t("particle_graph_editor.color"))
        ctx.same_line(92.0 * dpi)
        ctx.set_next_item_width(-1.0)
        color = render_color_value_bar(
            ctx,
            f"##{widget_id}_{index}_color",
            item["color"],
            allow_hdr=hdr,
            default_hdr_enabled=hdr,
        )
        for channel_index, channel in enumerate(("r", "g", "b", "a")):
            ctx.record_semantic_item(
                "color_channel",
                channel.upper(),
                True,
                f"{prefix}.key.{index}.color.{channel}",
                numeric_value=color[channel_index],
            )
        item["color"] = color
        if len(keys) > 1 and ctx.button(
            f"{t('particle_graph_editor.remove_key')}##{widget_id}_{index}_remove"
        ):
            remove_index = index
    if remove_index >= 0:
        del keys[remove_index]
    if len(keys) < MAX_RAMP_KEYS and ctx.button(
        f"{t('particle_graph_editor.add_key')}##{widget_id}_add"
    ):
        if len(keys) == 1:
            new_time = 1.0 if keys[0]["time"] < 1.0 else 0.0
            keys.append({"time": new_time, "color": list(keys[0]["color"])})
            keys.sort(key=lambda item: item["time"])
        else:
            gap_index = max(
                range(len(keys) - 1),
                key=lambda index: keys[index + 1]["time"] - keys[index]["time"],
            )
            left = keys[gap_index]
            right = keys[gap_index + 1]
            keys.insert(
                gap_index + 1,
                {
                    "time": (left["time"] + right["time"]) * 0.5,
                    "color": [
                        a + (b - a) * 0.5
                        for a, b in zip(left["color"], right["color"])
                    ],
                },
            )
    return Gradient.from_dict(
        {"keys": keys, "mode": GRADIENT_MODES[mode_index]}
    ).to_dict()


__all__ = ["render_gradient_property"]
