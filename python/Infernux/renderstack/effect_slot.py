"""Structured RenderStack stage slots stored by the component serializer."""

from __future__ import annotations

import uuid

from Infernux.components.serializable_object import SerializableObject
from Infernux.components.fields import serialized_field
from Infernux.core.asset_ref import RenderEffectRef


class EffectSlot(SerializableObject):
    """One ordered Effect asset mount in a pipeline-defined stage."""

    slot_id: str = serialized_field(default="", hidden=True)
    stage_id: str = serialized_field(default="", hidden=True)
    effect: RenderEffectRef = serialized_field(default=None, asset_type="RenderEffect")
    enabled: bool = serialized_field(default=True)

    def __init__(
        self,
        *,
        slot_id: str = "",
        stage_id: str = "",
        effect=None,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            slot_id=slot_id or uuid.uuid4().hex,
            stage_id=stage_id,
            effect=effect,
            enabled=enabled,
        )

    @property
    def effect_ref(self) -> RenderEffectRef:
        """Return the raw asset reference without resolving it."""
        from Infernux.components.fields import get_raw_field_value

        return get_raw_field_value(self, "effect")
