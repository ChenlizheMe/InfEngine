"""Undo recording helpers for the Inspector component renderers."""

from Infernux.components.component import InxComponent


def _notify_scene_modified():
    """Mark the active scene as dirty (unsaved) in SceneFileManager."""
    from Infernux.engine.scene_manager import SceneFileManager
    sfm = SceneFileManager.instance()
    if sfm:
        sfm.mark_dirty()


def _is_python_component_entry(component) -> bool:
    return isinstance(component, InxComponent) or hasattr(component, 'get_py_component')


def _record_property(target, prop_name: str, old_value, new_value,
                     description: str = ""):
    """Record a property change through the undo system.

    Falls back to direct ``setattr`` + dirty-mark if UndoManager is
    unavailable.
    """
    from Infernux.engine.undo import UndoManager, SetPropertyCommand
    mgr = UndoManager.instance()
    if mgr:
        mgr.execute(SetPropertyCommand(
            target, prop_name, old_value, new_value,
            description or f"Set {prop_name}"))
        return
    # Fallback
    setattr(target, prop_name, new_value)
    _notify_scene_modified()


def _record_python_component_document_edit(
    component: InxComponent,
    edit,
    description: str,
    *,
    edit_key: str = "",
    validate: bool = False,
):
    """Apply one Python-component edit and record its complete serialized state."""
    from Infernux.engine.undo import UndoManager, PythonComponentDocumentCommand

    serializer = getattr(component, "_serialize_fields_document", None)
    if not callable(serializer):
        result = edit()
        _notify_scene_modified()
        return result

    manager = UndoManager.instance()
    if manager is not None and manager.enabled:
        with manager.suppress():
            old_document = serializer()
            result = edit()
            if validate:
                component._call_on_validate()
            new_document = serializer()
        if new_document != old_document:
            manager.record(PythonComponentDocumentCommand(
                component,
                old_document,
                new_document,
                description,
                edit_key=edit_key,
            ))
        return result

    old_document = serializer()
    result = edit()
    if validate:
        component._call_on_validate()
    if serializer() != old_document:
        _notify_scene_modified()
    return result


def _record_material_slot(renderer, slot: int, old_guid: str, new_guid: str,
                          description: str = ""):
    """Record a MeshRenderer material-slot change via SetMaterialSlotCommand."""
    from Infernux.engine.undo import UndoManager, SetMaterialSlotCommand
    mgr = UndoManager.instance()
    if mgr:
        mgr.execute(SetMaterialSlotCommand(
            renderer, slot, old_guid, new_guid,
            description or f"Set Material Slot {slot}"))
        return
    # Fallback — the slot was already set by the caller
    _notify_scene_modified()


def _record_generic_component(comp, old_document: dict, new_document: dict):
    """Record a generic native-component document edit."""
    from Infernux.engine.undo import UndoManager, GenericComponentCommand
    mgr = UndoManager.instance()
    if mgr:
        mgr.execute(GenericComponentCommand(
            comp, old_document, new_document, f"Edit {comp.type_name}"))
        return
    # Fallback
    if not comp.deserialize_document(new_document):
        raise RuntimeError(f"Failed to restore {comp.type_name} document")
    _notify_scene_modified()


def _record_builtin_property(comp, cpp_attr: str, old_value, new_value,
                             description: str):
    """Apply a property change to a C++ component via direct setter, with undo.

    The setter path (e.g. ``comp.size = …``) goes through the pybind11
    property → C++ ``SetSize()`` → ``RebuildShape()`` → physics sync,
    which is exactly what we need for runtime changes.
    """
    from Infernux.engine.undo import UndoManager, BuiltinPropertyCommand
    mgr = UndoManager.instance()
    if mgr:
        cmd = BuiltinPropertyCommand(comp, cpp_attr, old_value, new_value,
                                     description)
        mgr.execute(cmd)
        return
    # Fallback — just set the property directly
    setattr(comp, cpp_attr, new_value)
    _notify_scene_modified()


class _TrackVolumeCommand:
    """Lightweight undo command for AudioSource track volume.

    Implements the same interface that UndoManager expects from
    ``UndoCommand`` without pulling in a heavy ABC import.
    """
    supports_redo = True
    marks_dirty = True
    MERGE_WINDOW = 0.3

    def __init__(self, comp, track_index: int, old_vol: float, new_vol: float):
        import time as _time
        self.description = f"Set Track {track_index} Volume"
        self.timestamp = _time.time()
        self._comp = comp
        self._track = track_index
        self._old = old_vol
        self._new = new_vol
        self._comp_id = getattr(comp, "component_id", id(comp))

    def execute(self):
        self._comp.set_track_volume(self._track, self._new)

    def undo(self):
        self._comp.set_track_volume(self._track, self._old)

    def redo(self):
        self.execute()

    def can_merge(self, other):
        return (isinstance(other, _TrackVolumeCommand)
                and self._comp_id == other._comp_id
                and self._track == other._track
                and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW)

    def merge(self, other):
        self._new = other._new
        self.timestamp = other.timestamp


def _record_track_volume(comp, track_index: int, old_vol: float, new_vol: float):
    """Record an AudioSource track volume change through undo."""
    from Infernux.engine.undo import UndoManager
    mgr = UndoManager.instance()
    if mgr:
        mgr.record(_TrackVolumeCommand(comp, track_index, old_vol, new_vol))
        return
    _notify_scene_modified()
