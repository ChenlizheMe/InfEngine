from __future__ import annotations

from types import SimpleNamespace

from Infernux.core.asset_ref import RenderEffectRef
from Infernux.mcp.tools import renderstack as module
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.default_forward_pipeline import MSAASamples
from Infernux.renderstack.render_stack import RenderStack


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


def test_pipeline_snapshot_reports_live_values_separately_from_defaults():
    stack = RenderStack()
    stack.pipeline.msaa_samples = MSAASamples.X2

    parameters = {
        item["name"]: item for item in module._pipeline_parameters(stack)
    }

    assert parameters["msaa_samples"]["value"] == {"name": "X2", "value": 2}
    assert parameters["msaa_samples"]["default"] == {"name": "X4", "value": 4}


def test_pipeline_parameter_coercion_accepts_enum_name_for_agent_editing():
    from Infernux.components.serialized_field import get_serialized_fields
    from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline

    metadata = get_serialized_fields(DefaultForwardPipeline)["msaa_samples"]

    assert module._coerce_pipeline_parameter("X8", metadata, "msaa") is MSAASamples.X8
