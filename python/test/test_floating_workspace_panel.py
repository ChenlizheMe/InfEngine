from __future__ import annotations

from types import SimpleNamespace

from Infernux.engine.ui.floating_workspace_panel import (
    FloatingOverlayState,
    render_floating_overlay,
)


def test_clipped_floating_overlay_balances_child_and_style_stacks() -> None:
    calls: list[tuple] = []
    ctx = SimpleNamespace(
        set_cursor_pos_x=lambda value: calls.append(("cursor_x", value)),
        set_cursor_pos_y=lambda value: calls.append(("cursor_y", value)),
        push_style_color=lambda *args: calls.append(("push_color", *args)),
        push_style_var_float=lambda *args: calls.append(("push_var", *args)),
        begin_child=lambda *args: calls.append(("begin_child", *args)) or False,
        end_child=lambda: calls.append(("end_child",)),
        pop_style_var=lambda count: calls.append(("pop_var", count)),
        pop_style_color=lambda count: calls.append(("pop_color", count)),
    )

    render_floating_overlay(
        ctx,
        FloatingOverlayState(height=240.0),
        child_id="##test_overlay",
        x=8.0,
        y=12.0,
        width=260.0,
        render_fn=lambda: calls.append(("render",)),
    )

    assert sum(call[0] == "push_color" for call in calls) == 2
    assert ("pop_color", 2) in calls
    assert sum(call[0] == "push_var" for call in calls) == 1
    assert ("pop_var", 1) in calls
    assert sum(call[0] == "begin_child" for call in calls) == 1
    assert sum(call[0] == "end_child" for call in calls) == 1
    assert ("render",) not in calls
