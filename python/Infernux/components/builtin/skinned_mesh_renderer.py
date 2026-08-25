"""
SkinnedMeshRenderer — Python wrapper for the native SkinnedMeshRenderer component.

It reuses MeshRenderer's GUID-backed mesh/material API while exposing animated
model metadata and the active take selection used by SkeletalAnimator.
"""

from __future__ import annotations

from typing import List

from Infernux.components.builtin.mesh_renderer import MeshRenderer
from Infernux.components.builtin_component import CppProperty
from Infernux.components.fields import FieldType


class SkinnedMeshRenderer(MeshRenderer):
    """Python facade for the native animated-model renderer component."""

    _cpp_type_name = "SkinnedMeshRenderer"
    _component_category_ = "Rendering"

    source_model_guid = CppProperty(
        "source_model_guid",
        FieldType.STRING,
        default="",
        readonly=True,
        visible_when=lambda _c: False,
        tooltip="GUID of the source animated model asset",
    )
    active_take_name = CppProperty(
        "active_take_name",
        FieldType.STRING,
        default="",
        tooltip="Currently selected animation take name",
    )
    animation_source_guid = CppProperty(
        "animation_source_guid",
        FieldType.STRING,
        default="",
        readonly=True,
        visible_when=lambda _c: False,
        tooltip="GUID of the model that owns the active animation take",
    )

    def get_animation_take_names(self) -> List[str]:
        cpp = self._cpp_component
        if cpp is None:
            return []
        return list(cpp.get_animation_take_names())

    def set_source_model_guid(self, guid: str) -> None:
        cpp = self._cpp_component
        if cpp is not None and hasattr(cpp, "set_source_model_guid"):
            cpp.set_source_model_guid(guid or "")

    @property
    def animation_take_count(self) -> int:
        return len(self.get_animation_take_names())

    @property
    def has_animation_takes(self) -> bool:
        return self.animation_take_count > 0
