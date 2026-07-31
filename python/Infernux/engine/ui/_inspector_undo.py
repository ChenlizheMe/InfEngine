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


def _record_add_component(obj, type_name: str, comp_ref,
                          is_py: bool = False):
    """Record the addition of a component through the undo system."""
    from Infernux.engine.undo import (
        UndoManager, AddNativeComponentCommand, AddPyComponentCommand)
    mgr = UndoManager.instance()
    if mgr:
        if is_py:
            mgr.record(AddPyComponentCommand(
                obj.id, comp_ref,
                f"Add {getattr(comp_ref, 'type_name', type_name)}"))
        else:
            mgr.record(AddNativeComponentCommand(
                obj.id, type_name, comp_ref, f"Add {type_name}"))
        return
    _notify_scene_modified()


def _get_component_ids(obj) -> set:
    """Snapshot all component IDs on a GameObject before an add operation."""
    from Infernux.debug import Debug
    ids: set = set()
    if hasattr(obj, 'get_components'):
        for c in obj.get_components():
            try:
                cid = c.component_id
                if cid:
                    ids.add(cid)
            except Exception as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass
    return ids


def _get_native_component_documents(obj) -> dict:
    """Snapshot existing native components before an add operation."""
    documents = {}
    if not hasattr(obj, 'get_components'):
        return documents
    for component in obj.get_components():
        if _is_python_component_entry(component):
            continue
        component_id = getattr(component, 'component_id', 0)
        serializer = getattr(component, 'serialize_document', None)
        if component_id and callable(serializer):
            documents[component_id] = (component, serializer())
    return documents


def _record_add_component_compound(obj, type_name: str, comp_ref,
                                   before_ids: set,
                                   is_py: bool = False,
                                   before_documents: dict | None = None):
    """Record add-component with auto-dependency detection.

    Compares current component IDs against *before_ids* to find
    auto-created components (e.g. BoxCollider when adding Rigidbody).
    Groups all additions into a single :class:`CompoundCommand` so that
    undo/redo operates atomically on the whole group.
    """
    from Infernux.debug import Debug
    from Infernux.engine.undo import (
        UndoManager, AddNativeComponentCommand, AddPyComponentCommand,
        CompoundCommand, GenericComponentCommand)
    mgr = UndoManager.instance()
    if not mgr:
        _notify_scene_modified()
        return

    # Detect native auto-created components
    auto_created: list = []
    changed_existing: list = []
    main_id = getattr(comp_ref, 'component_id', None) or id(comp_ref)
    if hasattr(obj, 'get_components'):
        for c in obj.get_components():
            try:
                cid = c.component_id
                tn = c.type_name
                if (cid and cid not in before_ids
                        and cid != main_id
                        and tn != "Transform"
                        and not _is_python_component_entry(c)):
                    auto_created.append((tn, c))
            except Exception as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass

    for component_id, (component, old_document) in (before_documents or {}).items():
        if component_id not in before_ids:
            raise RuntimeError("component document snapshot contains an unknown component ID")
        serializer = getattr(component, 'serialize_document', None)
        if not callable(serializer):
            raise RuntimeError("snapshotted native component lost serialize_document")
        new_document = serializer()
        if new_document != old_document:
            changed_existing.append((component, old_document, new_document))

    if not auto_created and not changed_existing:
        # No auto-creation — record a single command
        _record_add_component(obj, type_name, comp_ref, is_py=is_py)
        return

    # Build compound: auto-created first, main last.
    # Undo reverses order (removes main → then auto-created).
    # Redo replays order (adds auto-created → then main, PostAddComponent
    # sees dependencies already present and skips auto-creation).
    cmds: list = []
    for changed_ref, old_document, new_document in changed_existing:
        cmds.append(GenericComponentCommand(
            changed_ref, old_document, new_document,
            f"Update {getattr(changed_ref, 'type_name', 'component')} for {type_name}"))
    for auto_tn, auto_ref in auto_created:
        cmds.append(AddNativeComponentCommand(
            obj.id, auto_tn, auto_ref, f"Auto-add {auto_tn}"))
    if is_py:
        cmds.append(AddPyComponentCommand(
            obj.id, comp_ref,
            f"Add {getattr(comp_ref, 'type_name', type_name)}"))
    else:
        cmds.append(AddNativeComponentCommand(
            obj.id, type_name, comp_ref, f"Add {type_name}"))
    mgr.record(CompoundCommand(cmds, f"Add {type_name}"))


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
