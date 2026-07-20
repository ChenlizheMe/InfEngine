from __future__ import annotations

from types import SimpleNamespace

from Infernux.core.asset_ref import RenderEffectRef
from Infernux.mcp.tools import renderstack as module
from Infernux.renderstack.effect_slot import EffectSlot


class _Stack:
    def __init__(self):
        self.effect_stages = (
            SimpleNamespace(stable_id="final", display_name="Final", scope="composite"),
        )
        self.effect_slots = []

    def get_effect_stage_slots(self, stage_id):
        assert stage_id == "final"
        return tuple(slot for slot in self.effect_slots if slot.stage_id == stage_id)


def test_effect_stage_snapshot_preserves_order_and_asset_identity():
    stack = _Stack()
    stack.effect_slots = [
        EffectSlot(
            slot_id="slot-a",
            stage_id="final",
            effect=RenderEffectRef(guid="effect-guid", path_hint="Assets/Post.effect"),
            enabled=False,
        )
    ]

    stages = module._effect_stages(stack)

    assert stages == [
        {
            "stable_id": "final",
            "display_name": "Final",
            "scope": "composite",
            "slots": [
                {
                    "index": 0,
                    "slot_id": "slot-a",
                    "enabled": False,
                    "asset": {
                        "guid": "effect-guid",
                        "path_hint": "Assets/Post.effect",
                    },
                }
            ],
        }
    ]
