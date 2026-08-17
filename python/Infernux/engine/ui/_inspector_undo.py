"""Undo recording helpers for the Inspector component renderers."""

from Infernux.components.component import InxComponent


def _component_service():
    from Infernux.engine.interaction import EditorInteractionCore

    core = EditorInteractionCore.instance()
    if core is None:
        raise RuntimeError("Inspector edit requires EditorInteractionCore")
    return core.components


def _is_python_component_entry(component) -> bool:
    return isinstance(component, InxComponent) or hasattr(component, 'get_py_component')


def _record_property(target, prop_name: str, old_value, new_value,
                     description: str = ""):
    """Submit a legacy call site through SerializedProperty Core."""
    del old_value
    _component_service().set_field(
        target,
        prop_name,
        new_value,
        description=description or f"Set {prop_name}",
    )


def _record_python_component_document_edit(
    component: InxComponent,
    edit,
    description: str,
    *,
    edit_key: str = "",
    validate: bool = False,
):
    """Apply one Python-component edit and record its complete serialized state."""
    def _edit():
        result = edit()
        if validate:
            component._call_on_validate()
        return result

    result = _component_service().edit_document(
        component,
        _edit,
        description=description,
        edit_key=edit_key,
    )
    return result.value


def _record_material_slot(renderer, slot: int, old_guid: str, new_guid: str,
                          description: str = ""):
    """Record a MeshRenderer material-slot change via SetMaterialSlotCommand."""
    _component_service().set_material_slot(
        renderer,
        slot,
        old_guid,
        new_guid,
        description=description or f"Set Material Slot {slot}",
    )


def _record_generic_component(comp, old_document: dict, new_document: dict):
    """Record a generic native-component document edit."""
    changed = _component_service().commit_documents(
        comp,
        old_document,
        new_document,
        description=f"Edit {comp.type_name}",
        restore_before_execute=True,
    )
    if changed:
        from .inspector_snapshot import InspectorSnapshotService, target_for_component

        InspectorSnapshotService.instance().invalidate_value(
            target_for_component(comp),
            component_id=int(getattr(comp, "component_id", 0) or id(comp)),
            domain="document",
        )


def _record_builtin_property(comp, cpp_attr: str, old_value, new_value,
                             description: str):
    """Submit a native setter through the same SerializedProperty Core."""
    del old_value
    _component_service().set_field(
        comp,
        cpp_attr,
        new_value,
        description=description,
    )


def _record_track_volume(comp, track_index: int, old_vol: float, new_vol: float):
    """Record an AudioSource track volume through the component authority."""
    del old_vol
    _component_service().edit_document(
        comp,
        lambda: comp.set_track_volume(track_index, new_vol),
        description=f"Set Track {track_index} Volume",
        edit_key=f"track_volume:{track_index}",
    )
