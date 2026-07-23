"""Tests for Infernux.engine.undo — UndoCommand subclasses and UndoManager.

Pure-Python tests — no C++ backend needed.
Imports bypass the heavy Infernux.__init__ / native-module chain by loading
undo.py and selection_manager.py directly via importlib.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import os
import sys
import time
import types

import pytest

# ── Direct-load helper ───────────────────────────────────────────────
# Load a module or package file directly, avoiding __init__.py chains that
# pull in the C++ native backend.

_PROJECT_PY = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "Infernux"))


def _direct_import(module_name: str, rel_path: str):
    """Import *rel_path* (relative to python/Infernux/) as *module_name*."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    filepath = os.path.join(_PROJECT_PY, *rel_path.split("/"))

    package_dir = None
    if os.path.isdir(filepath):
        package_dir = filepath
        filepath = os.path.join(filepath, "__init__.py")
    elif not os.path.exists(filepath) and filepath.endswith(".py"):
        candidate_package_dir = filepath[:-3]
        candidate_init = os.path.join(candidate_package_dir, "__init__.py")
        if os.path.exists(candidate_init):
            package_dir = candidate_package_dir
            filepath = candidate_init

    if package_dir is not None:
        spec = importlib.util.spec_from_file_location(
            module_name,
            filepath,
            submodule_search_locations=[package_dir],
        )
    else:
        spec = importlib.util.spec_from_file_location(module_name, filepath)

    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import {module_name!r} from {filepath!r}")

    mod = importlib.util.module_from_spec(spec)
    if package_dir is not None:
        mod.__path__ = [package_dir]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Pre-register lightweight stub packages so sub-module imports resolve.
for _pkg in [
    "Infernux",
    "Infernux.engine",
    "Infernux.engine.ui",
]:
    if _pkg not in sys.modules:
        _p = types.ModuleType(_pkg)
        _p.__path__ = [os.path.join(_PROJECT_PY,
                                     *_pkg.split(".")[1:])] if _pkg != "Infernux" \
                       else [_PROJECT_PY]
        _p.__package__ = _pkg
        sys.modules[_pkg] = _p

# Now load the actual modules we need — this executes their code but
# won't trigger Infernux/__init__.py's heavy imports.
_undo_mod = _direct_import("Infernux.engine.undo", "engine/undo.py")
_sel_mod = _direct_import(
    "Infernux.engine.ui.selection_manager", "engine/ui/selection_manager.py")

# Pull symbols into module scope for convenience.
UndoCommand = _undo_mod.UndoCommand
UndoManager = _undo_mod.UndoManager
SetPropertyCommand = _undo_mod.SetPropertyCommand
GenericComponentCommand = _undo_mod.GenericComponentCommand
BuiltinPropertyCommand = _undo_mod.BuiltinPropertyCommand
CreateGameObjectCommand = _undo_mod.CreateGameObjectCommand
DeleteGameObjectCommand = _undo_mod.DeleteGameObjectCommand
DeleteGameObjectsCommand = _undo_mod.DeleteGameObjectsCommand
ReparentCommand = _undo_mod.ReparentCommand
MoveGameObjectCommand = _undo_mod.MoveGameObjectCommand
MaterialDocumentCommand = _undo_mod.MaterialDocumentCommand
CompoundCommand = _undo_mod.CompoundCommand
SelectionCommand = _undo_mod.SelectionCommand
EditorSelectionCommand = _undo_mod.EditorSelectionCommand
PrefabModeCommand = _undo_mod.PrefabModeCommand
PrefabUnpackCommand = _undo_mod.PrefabUnpackCommand
PrefabRevertCommand = _undo_mod.PrefabRevertCommand
InspectorSnapshotCommand = _undo_mod.InspectorSnapshotCommand
InspectorUndoTracker = _undo_mod.InspectorUndoTracker
HierarchyUndoTracker = _undo_mod.HierarchyUndoTracker
RenderStackFieldCommand = _undo_mod.RenderStackFieldCommand
_snapshot_value = _undo_mod._snapshot_value
SelectionManager = _sel_mod.SelectionManager
_helpers_mod = sys.modules["Infernux.engine.undo._helpers"]
_property_mod = sys.modules["Infernux.engine.undo._property_commands"]
_structural_mod = sys.modules["Infernux.engine.undo._structural_commands"]
_trackers_mod = sys.modules["Infernux.engine.undo._trackers"]
_recreate_mod = sys.modules["Infernux.engine.undo._recreate"]


def _patch_undo_modules(monkeypatch, attr: str, value):
    for mod in (_undo_mod, _helpers_mod, _property_mod, _structural_mod):
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value)


@contextmanager
def _override_recreate_game_object(fn):
    orig_root = _undo_mod._recreate_game_object_from_document
    orig_sub = _recreate_mod._recreate_game_object_from_document
    _undo_mod._recreate_game_object_from_document = fn
    _recreate_mod._recreate_game_object_from_document = fn
    try:
        yield
    finally:
        _undo_mod._recreate_game_object_from_document = orig_root
        _recreate_mod._recreate_game_object_from_document = orig_sub


# ── Helpers ──

class _Obj:
    """Simple mutable target for SetPropertyCommand."""
    def __init__(self):
        self.x = 0
        self.y = 0


class _FakeComp:
    """Fake native component with typed document serialization."""
    def __init__(self):
        self.component_id = 42
        self.type_name = "FakeComp"
        self._document = {}

    def serialize_document(self):
        return dict(self._document)

    def deserialize_document(self, document):
        self._document = dict(document)
        return True


class _FakeStack:
    """Fake RenderStack with invalidate_graph() for RenderStackFieldCommand."""
    def __init__(self):
        self.invalidated = 0

    def invalidate_graph(self):
        self.invalidated += 1


@pytest.fixture(autouse=True)
def _reset_undo_manager():
    """Ensure a fresh UndoManager for every test; tear down afterwards."""
    old = UndoManager._instance
    mgr = UndoManager()
    # Prevent dirty-sync from importing SceneFileManager / PlayModeManager
    mgr._sync_dirty = lambda: None
    yield mgr
    UndoManager._instance = old


# ══════════════════════════════════════════════════════════════════════
# _snapshot_value
# ══════════════════════════════════════════════════════════════════════

class TestSnapshotValue:
    def test_immutable_pass_through(self):
        assert _snapshot_value(42) == 42
        assert _snapshot_value("hello") == "hello"
        assert _snapshot_value(None) is None

    def test_list_deepcopy(self):
        original = [[1, 2], [3, 4]]
        snapped = _snapshot_value(original)
        snapped[0][0] = 999
        assert original[0][0] == 1

    def test_dict_deepcopy(self):
        original = {"a": [1, 2]}
        snapped = _snapshot_value(original)
        snapped["a"].append(3)
        assert len(original["a"]) == 2


# ══════════════════════════════════════════════════════════════════════
# SetPropertyCommand
# ══════════════════════════════════════════════════════════════════════

class TestSetPropertyCommand:
    def test_execute_sets_value(self):
        obj = _Obj()
        cmd = SetPropertyCommand(obj, "x", 0, 42)
        cmd.execute()
        assert obj.x == 42

    def test_undo_restores_old(self):
        obj = _Obj()
        cmd = SetPropertyCommand(obj, "x", 0, 42)
        cmd.execute()
        cmd.undo()
        assert obj.x == 0

    def test_redo_reapplies(self):
        obj = _Obj()
        cmd = SetPropertyCommand(obj, "x", 0, 42)
        cmd.execute()
        cmd.undo()
        cmd.redo()
        assert obj.x == 42

    def test_merge_within_window(self):
        obj = _Obj()
        cmd1 = SetPropertyCommand(obj, "x", 0, 1)
        cmd2 = SetPropertyCommand(obj, "x", 1, 2)
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert cmd1.can_merge(cmd2)
        cmd1.merge(cmd2)
        # After merge, new value is from cmd2
        cmd1.execute()
        assert obj.x == 2
        # Undo goes to original old value
        cmd1.undo()
        assert obj.x == 0

    def test_merge_rejected_outside_window(self):
        obj = _Obj()
        cmd1 = SetPropertyCommand(obj, "x", 0, 1)
        cmd2 = SetPropertyCommand(obj, "x", 1, 2)
        cmd2.timestamp = cmd1.timestamp + 10.0
        assert not cmd1.can_merge(cmd2)

    def test_merge_rejected_different_property(self):
        obj = _Obj()
        cmd1 = SetPropertyCommand(obj, "x", 0, 1)
        cmd2 = SetPropertyCommand(obj, "y", 0, 2)
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert not cmd1.can_merge(cmd2)

    def test_is_property_edit_flag(self):
        obj = _Obj()
        cmd = SetPropertyCommand(obj, "x", 0, 1)
        assert cmd.marks_dirty is True

    def test_default_description(self):
        obj = _Obj()
        cmd = SetPropertyCommand(obj, "x", 0, 1)
        assert "x" in cmd.description

    def test_native_backed_undo_fails_closed_when_target_missing(self, monkeypatch):
        class _NativeTarget:
            def __init__(self):
                self.x = 99
                self.game_object_id = 42
                self.type_name = "Rigidbody"

        target = _NativeTarget()
        cmd = SetPropertyCommand(target, "x", 1, 2)
        _patch_undo_modules(monkeypatch, "_get_active_scene", lambda: None)
        cmd.undo()
        assert target.x == 99

    def test_gameobject_target_resolves_live_object(self, monkeypatch):
        class _GameObject:
            def __init__(self, object_id, active=True):
                self.id = object_id
                self.active = active

        live = _GameObject(7, active=True)
        stale = _GameObject(7, active=True)

        class _Scene:
            def find_by_id(self, object_id):
                return live if object_id == 7 else None

        cmd = SetPropertyCommand(stale, "active", True, False)
        _patch_undo_modules(monkeypatch, "_get_active_scene", lambda: _Scene())

        cmd.execute()
        assert live.active is False
        assert stale.active is True

        live.active = False
        cmd.undo()
        assert live.active is True

    def test_resolve_live_ref_finds_python_component(self, monkeypatch):
        """_resolve_live_ref falls back to get_py_components() for Python
        components (RenderStack etc.) that get_component() cannot find."""

        class _PyComp:
            """Mimics a Python InxComponent attached via PyComponentProxy."""
            pass

        py_comp = _PyComp()

        class _FakeObj:
            def get_component(self, name):
                return None  # C++ lookup always misses Python components

            def get_py_components(self):
                return [py_comp]

            @property
            def transform(self):
                return None

        fake_obj = _FakeObj()

        class _FakeScene:
            def find_by_id(self, oid):
                return fake_obj if oid == 7 else None

        _patch_undo_modules(monkeypatch, "_get_active_scene", lambda: _FakeScene())

        resolve = getattr(_undo_mod_ref, "_resolve_live_ref")
        result = resolve("stale_ref", 7, "_PyComp")
        assert result is py_comp


# ══════════════════════════════════════════════════════════════════════
# GenericComponentCommand
# ══════════════════════════════════════════════════════════════════════

class TestGenericComponentCommand:
    def test_execute_and_undo(self):
        comp = _FakeComp()
        cmd = GenericComponentCommand(comp, {"old": 1}, {"new": 2})
        cmd.execute()
        assert comp._document == {"new": 2}
        cmd.undo()
        assert comp._document == {"old": 1}

    def test_merge(self):
        comp = _FakeComp()
        cmd1 = GenericComponentCommand(comp, {}, {"a": 1})
        cmd2 = GenericComponentCommand(comp, {"a": 1}, {"a": 2})
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert cmd1.can_merge(cmd2)
        cmd1.merge(cmd2)
        cmd1.execute()
        assert comp._document == {"a": 2}

    def test_documents_are_owned_by_command(self):
        comp = _FakeComp()
        old_document = {"value": [1]}
        new_document = {"value": [2]}
        cmd = GenericComponentCommand(comp, old_document, new_document)
        old_document["value"].append(99)
        new_document["value"].append(99)
        cmd.execute()
        assert comp._document == {"value": [2]}
        cmd.undo()
        assert comp._document == {"value": [1]}


# ══════════════════════════════════════════════════════════════════════
# BuiltinPropertyCommand
# ══════════════════════════════════════════════════════════════════════

class TestBuiltinPropertyCommand:
    def test_execute_undo_redo(self):
        obj = _Obj()
        obj.component_id = 1
        cmd = BuiltinPropertyCommand(obj, "x", 0, 5)
        cmd.execute()
        assert obj.x == 5
        cmd.undo()
        assert obj.x == 0
        cmd.redo()
        assert obj.x == 5


# ══════════════════════════════════════════════════════════════════════
# MaterialDocumentCommand
# ══════════════════════════════════════════════════════════════════════

class TestMaterialDocumentCommand:
    def test_execute_undo_redo(self):
        mat = _FakeComp()
        mat.guid = "test-guid"
        cmd = MaterialDocumentCommand(mat, {"old": 1}, {"new": 2})
        cmd.execute()
        assert mat._document == {"new": 2}
        cmd.undo()
        assert mat._document == {"old": 1}
        cmd.redo()
        assert mat._document == {"new": 2}

    def test_marks_dirty_false(self):
        mat = _FakeComp()
        cmd = MaterialDocumentCommand(mat, {}, {})
        assert cmd.marks_dirty is False

    def test_merge(self):
        mat = _FakeComp()
        mat.guid = "g"
        cmd1 = MaterialDocumentCommand(mat, {}, {"a": 1})
        cmd2 = MaterialDocumentCommand(mat, {"a": 1}, {"a": 2})
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert cmd1.can_merge(cmd2)

    def test_merge_rejected_different_edit_key(self):
        mat = _FakeComp()
        mat.guid = "g"
        cmd1 = MaterialDocumentCommand(mat, {}, {"a": 1}, edit_key="property.a")
        cmd2 = MaterialDocumentCommand(mat, {"a": 1}, {"b": 2}, edit_key="property.b")
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert not cmd1.can_merge(cmd2)

    def test_documents_are_owned_by_command(self):
        mat = _FakeComp()
        old_document = {"properties": {"x": [1]}}
        new_document = {"properties": {"x": [2]}}
        cmd = MaterialDocumentCommand(mat, old_document, new_document)
        old_document["properties"]["x"].append(99)
        new_document["properties"]["x"].append(99)
        cmd.execute()
        assert mat._document == {"properties": {"x": [2]}}
        cmd.undo()
        assert mat._document == {"properties": {"x": [1]}}


# ══════════════════════════════════════════════════════════════════════
# CompoundCommand
# ══════════════════════════════════════════════════════════════════════

class TestCompoundCommand:
    def test_execute_all(self):
        a = _Obj()
        b = _Obj()
        cmds = [
            SetPropertyCommand(a, "x", 0, 10),
            SetPropertyCommand(b, "x", 0, 20),
        ]
        compound = CompoundCommand(cmds, "Compound Edit")
        compound.execute()
        assert a.x == 10
        assert b.x == 20

    def test_undo_in_reverse(self):
        log = []

        class LogCmd(UndoCommand):
            def __init__(self, tag):
                super().__init__(tag)
                self.tag = tag
            def execute(self):
                log.append(("exec", self.tag))
            def undo(self):
                log.append(("undo", self.tag))

        cmds = [LogCmd("A"), LogCmd("B")]
        compound = CompoundCommand(cmds)
        compound.execute()
        log.clear()
        compound.undo()
        assert log == [("undo", "B"), ("undo", "A")]

    def test_supports_redo_all_true(self):
        obj = _Obj()
        cmds = [SetPropertyCommand(obj, "x", 0, 1)]
        compound = CompoundCommand(cmds)
        assert compound.supports_redo is True

    def test_compound_add_component_undo_reverses_auto_created(self):
        """Simulate adding Rigidbody (which auto-creates BoxCollider).

        A CompoundCommand [AddBoxCollider, AddRigidbody] should:
        - undo in reverse: remove Rigidbody first, then BoxCollider
        - redo in order:  add BoxCollider first, then Rigidbody
        """
        log: list = []

        class FakeAddCmd(UndoCommand):
            def __init__(self, tag):
                super().__init__(f"Add {tag}")
                self.tag = tag
            def execute(self):
                log.append(("add", self.tag))
            def undo(self):
                log.append(("remove", self.tag))
            def redo(self):
                log.append(("re-add", self.tag))

        # Mirror the compound construction from _record_add_component_compound:
        # auto-created first, main last.
        compound = CompoundCommand(
            [FakeAddCmd("BoxCollider"), FakeAddCmd("Rigidbody")],
            "Add Rigidbody")

        # Undo should reverse: remove Rigidbody, then BoxCollider
        compound.undo()
        assert log == [("remove", "Rigidbody"), ("remove", "BoxCollider")]

        log.clear()
        # Redo should replay in order: add BoxCollider, then Rigidbody
        compound.redo()
        assert log == [("re-add", "BoxCollider"), ("re-add", "Rigidbody")]

    def test_compound_add_single_undo_through_manager(self, _reset_undo_manager):
        """A CompoundCommand recorded via mgr.record() is a single undo step."""
        mgr = _reset_undo_manager
        state = {"a": False, "b": False}

        class FlagCmd(UndoCommand):
            def __init__(self, key):
                super().__init__(f"Set {key}")
                self.key = key
            def execute(self):
                state[self.key] = True
            def undo(self):
                state[self.key] = False
            def redo(self):
                state[self.key] = True

        # Simulate both already executed before recording
        state["a"] = True
        state["b"] = True
        compound = CompoundCommand([FlagCmd("a"), FlagCmd("b")], "Compound")
        mgr.record(compound)

        assert mgr.can_undo
        # Single undo should revert BOTH flags
        mgr.undo()
        assert state["a"] is False
        assert state["b"] is False
        assert not mgr.can_undo  # only one entry was on the stack


# ══════════════════════════════════════════════════════════════════════
# UndoCommand base
# ══════════════════════════════════════════════════════════════════════

class TestUndoCommandBase:
    def test_default_flags(self):
        class Concrete(UndoCommand):
            def execute(self): pass
            def undo(self): pass
        cmd = Concrete("test")
        assert cmd.supports_redo is True
        assert cmd.marks_dirty is True
        assert isinstance(cmd.timestamp, float)

    def test_default_redo_calls_execute(self):
        calls = []
        class Concrete(UndoCommand):
            def execute(self):
                calls.append("exec")
            def undo(self):
                calls.append("undo")
        cmd = Concrete()
        cmd.redo()
        assert calls == ["exec"]

    def test_default_merge_returns_false(self):
        class Concrete(UndoCommand):
            def execute(self): pass
            def undo(self): pass
        cmd = Concrete()
        assert not cmd.can_merge(cmd)


# ══════════════════════════════════════════════════════════════════════
# SelectionCommand
# ══════════════════════════════════════════════════════════════════════

class TestSelectionCommand:
    def test_undo_calls_apply_with_old_ids(self):
        applied = []
        cmd = SelectionCommand([1, 2], [3], apply_fn=lambda ids: applied.append(list(ids)))
        cmd.undo()
        assert applied == [[1, 2]]

    def test_redo_calls_apply_with_new_ids(self):
        applied = []
        cmd = SelectionCommand([1], [2, 3], apply_fn=lambda ids: applied.append(list(ids)))
        cmd.redo()
        assert applied == [[2, 3]]

    def test_execute_is_noop(self):
        applied = []
        cmd = SelectionCommand([], [1], apply_fn=lambda ids: applied.append(ids))
        cmd.execute()
        assert applied == []

    def test_marks_dirty_false(self):
        cmd = SelectionCommand([], [], apply_fn=lambda ids: None)
        assert cmd.marks_dirty is False

    def test_default_description(self):
        cmd = SelectionCommand([], [], apply_fn=lambda ids: None)
        assert cmd.description == "Change Selection"

    def test_custom_description(self):
        cmd = SelectionCommand([], [], apply_fn=lambda ids: None, description="Pick Object")
        assert cmd.description == "Pick Object"

    def test_supports_redo(self):
        cmd = SelectionCommand([], [1], apply_fn=lambda ids: None)
        assert cmd.supports_redo is True


class TestEditorSelectionCommand:
    def test_undo_redo_restore_object_and_file_state(self):
        applied = []
        cmd = EditorSelectionCommand(
            [1], "",
            [], "Assets/test.mat",
            apply_fn=lambda ids, path: applied.append((list(ids), path)),
        )
        cmd.undo()
        cmd.redo()
        assert applied == [([1], ""), ([], "Assets/test.mat")]


class TestPrefabUnpackCommand:
    def test_execute_undo_redo_restore_complete_linkage(self, monkeypatch):
        class _GameObject:
            def __init__(self, object_id, guid, is_root=False, children=None):
                self.id = object_id
                self.prefab_guid = guid
                self.prefab_root = is_root
                self._children = list(children or [])

            def get_children(self):
                return list(self._children)

        left = _GameObject(2, "prefab-guid")
        right = _GameObject(3, "prefab-guid")
        root = _GameObject(1, "prefab-guid", True, [left, right])
        objects = {obj.id: obj for obj in (root, left, right)}

        class _Scene:
            @staticmethod
            def find_by_id(object_id):
                return objects.get(object_id)

        monkeypatch.setattr(_structural_mod, "_get_active_scene", lambda: _Scene())

        cmd = PrefabUnpackCommand(root.id)
        cmd.execute()
        assert [(obj.prefab_guid, obj.prefab_root) for obj in (root, left, right)] == [
            ("", False), ("", False), ("", False),
        ]

        cmd.undo()
        assert [(obj.prefab_guid, obj.prefab_root) for obj in (root, left, right)] == [
            ("prefab-guid", True),
            ("prefab-guid", False),
            ("prefab-guid", False),
        ]

        cmd.redo()
        assert [(obj.prefab_guid, obj.prefab_root) for obj in (root, left, right)] == [
            ("", False), ("", False), ("", False),
        ]


class TestPrefabModeCommand:
    def test_enter_undo_redo_call_scene_manager(self, monkeypatch):
        calls = []

        class _FakeSceneManager:
            def open_prefab_mode(self, path, preserve_undo_history=False):
                calls.append(("open", path, preserve_undo_history))
                return True

            def _do_exit_prefab_mode(self, preserve_undo_history=False):
                calls.append(("exit", "", preserve_undo_history))
                return True

        fake_sfm = _FakeSceneManager()
        scene_manager_mod = types.ModuleType("Infernux.engine.scene_manager")

        class _SceneFileManager:
            @staticmethod
            def instance():
                return fake_sfm

        scene_manager_mod.SceneFileManager = _SceneFileManager
        monkeypatch.setitem(sys.modules, "Infernux.engine.scene_manager", scene_manager_mod)

        cmd = PrefabModeCommand("Assets/test.prefab", enter_mode=True)
        cmd.execute()
        cmd.undo()
        cmd.redo()

        assert calls == [
            ("open", "Assets/test.prefab", True),
            ("exit", "", True),
            ("open", "Assets/test.prefab", True),
        ]


# ══════════════════════════════════════════════════════════════════════
# InspectorSnapshotCommand
# ══════════════════════════════════════════════════════════════════════

class TestInspectorSnapshotCommand:
    def test_execute_restores_new(self):
        restored = []
        cmd = InspectorSnapshotCommand("k", "old_snap", "new_snap",
                                       restore_fn=lambda s: restored.append(s))
        cmd.execute()
        assert restored == ["new_snap"]

    def test_undo_restores_old(self):
        restored = []
        cmd = InspectorSnapshotCommand("k", "old_snap", "new_snap",
                                       restore_fn=lambda s: restored.append(s))
        cmd.undo()
        assert restored == ["old_snap"]

    def test_redo_restores_new(self):
        restored = []
        cmd = InspectorSnapshotCommand("k", "old_snap", "new_snap",
                                       restore_fn=lambda s: restored.append(s))
        cmd.redo()
        assert restored == ["new_snap"]

    def test_merge_within_window(self):
        cmd1 = InspectorSnapshotCommand("go:1", "a", "b", restore_fn=lambda s: None)
        cmd2 = InspectorSnapshotCommand("go:1", "b", "c", restore_fn=lambda s: None)
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert cmd1.can_merge(cmd2)
        cmd1.merge(cmd2)
        # After merge, old stays "a", new becomes "c"
        restored = []
        cmd1.undo()
        assert restored == [] or True  # undo restores old
        # Verify via execute
        results = []
        cmd1._restore_fn = lambda s: results.append(s)
        cmd1.execute()
        assert results == ["c"]
        cmd1.undo()
        assert results == ["c", "a"]

    def test_merge_rejected_different_key(self):
        cmd1 = InspectorSnapshotCommand("go:1", "a", "b", restore_fn=lambda s: None)
        cmd2 = InspectorSnapshotCommand("go:2", "a", "b", restore_fn=lambda s: None)
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert not cmd1.can_merge(cmd2)

    def test_merge_rejected_outside_window(self):
        cmd1 = InspectorSnapshotCommand("go:1", "a", "b", restore_fn=lambda s: None)
        cmd2 = InspectorSnapshotCommand("go:1", "b", "c", restore_fn=lambda s: None)
        cmd2.timestamp = cmd1.timestamp + 10.0
        assert not cmd1.can_merge(cmd2)


# ══════════════════════════════════════════════════════════════════════
# RenderStackFieldCommand
# ══════════════════════════════════════════════════════════════════════

class TestRenderStackFieldCommand:
    def test_execute_undo_redo(self):
        target = _Obj()
        stack = _FakeStack()
        cmd = RenderStackFieldCommand(stack, target, "x", 0, 42)
        cmd.execute()
        assert target.x == 42
        assert stack.invalidated == 1
        cmd.undo()
        assert target.x == 0
        assert stack.invalidated == 2
        cmd.redo()
        assert target.x == 42
        assert stack.invalidated == 3

    def test_merge_same_target_field(self):
        target = _Obj()
        stack = _FakeStack()
        cmd1 = RenderStackFieldCommand(stack, target, "x", 0, 1)
        cmd2 = RenderStackFieldCommand(stack, target, "x", 1, 5)
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert cmd1.can_merge(cmd2)
        cmd1.merge(cmd2)
        cmd1.execute()
        assert target.x == 5
        cmd1.undo()
        assert target.x == 0

    def test_merge_rejected_different_field(self):
        target = _Obj()
        stack = _FakeStack()
        cmd1 = RenderStackFieldCommand(stack, target, "x", 0, 1)
        cmd2 = RenderStackFieldCommand(stack, target, "y", 0, 2)
        cmd2.timestamp = cmd1.timestamp + 0.1
        assert not cmd1.can_merge(cmd2)


# ══════════════════════════════════════════════════════════════════════
# UndoManager — core operations
# ══════════════════════════════════════════════════════════════════════

class TestUndoManager:
    def test_execute_then_undo(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 10))
        assert obj.x == 10
        mgr.undo()
        assert obj.x == 0

    def test_redo_after_undo(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 10))
        mgr.undo()
        mgr.redo()
        assert obj.x == 10

    def test_execute_clears_redo(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 1))
        mgr.undo()
        assert mgr.can_redo
        mgr.execute(SetPropertyCommand(obj, "y", 0, 2))
        assert not mgr.can_redo

    def test_can_undo_redo_properties(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        assert not mgr.can_undo
        assert not mgr.can_redo
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 1))
        assert mgr.can_undo
        assert not mgr.can_redo

    def test_undo_description(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 1, "Move X"))
        assert mgr.undo_description == "Move X"

    def test_record_pushes_without_executing(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        executed = []

        class TrackCmd(UndoCommand):
            def execute(self):
                executed.append(True)
            def undo(self):
                pass

        cmd = TrackCmd("test")
        mgr.record(cmd)
        assert len(executed) == 0
        assert mgr.can_undo

    def test_merge_auto(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        cmd1 = SetPropertyCommand(obj, "x", 0, 1)
        mgr.execute(cmd1)
        cmd2 = SetPropertyCommand(obj, "x", 1, 5)
        cmd2.timestamp = cmd1.timestamp + 0.1
        mgr.execute(cmd2)
        # Should have merged → only 1 entry on the stack
        assert len(mgr._undo_stack) == 1
        mgr.undo()
        assert obj.x == 0

    def test_clear(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 1))
        mgr.clear()
        assert not mgr.can_undo
        assert not mgr.can_redo

    def test_is_executing_during_undo_redo(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        seen_exec = []

        class SpyCmd(UndoCommand):
            def execute(self):
                seen_exec.append(mgr.is_executing)
            def undo(self):
                seen_exec.append(mgr.is_executing)
            def redo(self):
                seen_exec.append(mgr.is_executing)

        mgr.execute(SpyCmd("test"))
        assert seen_exec == [True]
        mgr.undo()
        assert seen_exec == [True, True]
        mgr.redo()
        assert seen_exec == [True, True, True]

    def test_suppress_context_manager(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        assert not mgr.is_executing

    def test_execute_reports_failure_without_recording(self, _reset_undo_manager):
        mgr = _reset_undo_manager

        class FailingCommand(UndoCommand):
            def execute(self):
                raise RuntimeError("expected test failure")

            def undo(self):
                pass

        assert mgr.execute(FailingCommand("fail")) is False
        assert not mgr.can_undo
        with mgr.suppress():
            assert mgr.is_executing
        assert not mgr.is_executing

    def test_suppress_property_recording(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        with mgr.suppress_property_recording():
            mgr.execute(SetPropertyCommand(obj, "x", 0, 99))
        # Value was set but command not recorded
        assert obj.x == 99
        assert not mgr.can_undo

    def test_suppress_property_recording_allows_structural(self, _reset_undo_manager):
        mgr = _reset_undo_manager

        class StructuralCmd(UndoCommand):
            _is_property_edit = False
            def execute(self): pass
            def undo(self): pass

        with mgr.suppress_property_recording():
            mgr.execute(StructuralCmd("create object"))
        # Structural commands are recorded even under suppression
        assert mgr.can_undo

    def test_depth_limit(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        mgr.MAX_STACK_DEPTH = 5
        obj = _Obj()
        for i in range(10):
            # Use large timestamp gap to prevent merging
            cmd = SetPropertyCommand(obj, "x", i, i + 1)
            cmd.timestamp = i * 10.0
            mgr.execute(cmd)
        assert len(mgr._undo_stack) == 5

    def test_disabled_still_executes(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        mgr.enabled = False
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 5))
        assert obj.x == 5
        assert not mgr.can_undo

    def test_state_changed_callback(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        calls = []
        mgr.set_on_state_changed(lambda: calls.append(1))
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 1))
        assert len(calls) == 1
        mgr.undo()
        assert len(calls) == 2

    def test_multiple_undo_redo(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        for i in range(3):
            cmd = SetPropertyCommand(obj, "x", i, i + 1)
            cmd.timestamp = i * 10.0
            mgr.execute(cmd)
        assert obj.x == 3
        mgr.undo()
        assert obj.x == 2
        mgr.undo()
        assert obj.x == 1
        mgr.redo()
        assert obj.x == 2


# ══════════════════════════════════════════════════════════════════════
# UndoManager — save point / dirty tracking
# ══════════════════════════════════════════════════════════════════════

class TestUndoManagerSavePoint:
    def test_starts_at_save_point(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        assert mgr.is_at_save_point

    def test_dirty_after_execute(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 1))
        assert not mgr.is_at_save_point

    def test_clean_after_undo(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 1))
        mgr.undo()
        assert mgr.is_at_save_point

    def test_mark_save_point(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        mgr.execute(SetPropertyCommand(obj, "x", 0, 1))
        mgr.mark_save_point()
        assert mgr.is_at_save_point

    def test_dirty_after_undo_past_save(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        obj = _Obj()
        cmd1 = SetPropertyCommand(obj, "x", 0, 1)
        cmd1.timestamp = 0.0
        mgr.execute(cmd1)
        mgr.mark_save_point()
        mgr.undo()
        assert not mgr.is_at_save_point

    def test_selection_command_does_not_dirty(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        cmd = SelectionCommand([1], [2], apply_fn=lambda ids: None)
        mgr.record(cmd)
        # SelectionCommand has marks_dirty=False, so save point unchanged
        assert mgr.is_at_save_point


# ══════════════════════════════════════════════════════════════════════
# InspectorUndoTracker
# ══════════════════════════════════════════════════════════════════════

class TestInspectorUndoTracker:
    def test_detects_change(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        tracker = InspectorUndoTracker()
        state = {"val": "A"}
        restored = []

        tracker.begin_frame()
        tracker.track("k1",
                       snapshot_fn=lambda: state["val"],
                       restore_fn=lambda s: restored.append(s),
                       description="Edit")
        # Simulate edit
        state["val"] = "B"
        tracker.end_frame()

        assert mgr.can_undo
        mgr.undo()
        assert restored == ["A"]

    def test_no_change_no_record(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        tracker = InspectorUndoTracker()
        state = {"val": "A"}

        tracker.begin_frame()
        tracker.track("k1",
                       snapshot_fn=lambda: state["val"],
                       restore_fn=lambda s: None,
                       description="Edit")
        # No edit → no command recorded
        tracker.end_frame()
        assert not mgr.can_undo

    def test_multiple_targets(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        tracker = InspectorUndoTracker()
        s1 = {"v": "1"}
        s2 = {"v": "X"}

        tracker.begin_frame()
        tracker.track("a", lambda: s1["v"], lambda s: None, "edit a")
        tracker.track("b", lambda: s2["v"], lambda s: None, "edit b")
        s1["v"] = "2"  # only s1 changed
        tracker.end_frame()

        assert len(mgr._undo_stack) == 1  # only 1 command recorded

    def test_begin_frame_clears_previous(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        tracker = InspectorUndoTracker()

        tracker.begin_frame()
        tracker.track("k1", lambda: "a", lambda s: None, "edit")
        # Begin new frame without calling end_frame
        tracker.begin_frame()
        tracker.end_frame()
        assert not mgr.can_undo

    def test_snapshot_exception_skipped(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        tracker = InspectorUndoTracker()

        def bad_snapshot():
            raise RuntimeError("broken")

        tracker.begin_frame()
        tracker.track("k1", bad_snapshot, lambda s: None, "edit")
        tracker.end_frame()
        assert not mgr.can_undo

    def test_duplicate_key_ignored(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        tracker = InspectorUndoTracker()
        calls = [0]

        def snap():
            calls[0] += 1
            return "v"

        tracker.begin_frame()
        tracker.track("k", snap, lambda s: None, "edit")
        tracker.track("k", snap, lambda s: None, "edit")  # duplicate
        # Only 1 snapshot call (the first)
        assert calls[0] == 1

    def test_invalidate_all_drops_tracked_entries(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        tracker = InspectorUndoTracker()
        state = {"val": "A"}

        tracker.begin_frame()
        tracker.track("k1",
                      snapshot_fn=lambda: state["val"],
                      restore_fn=lambda s: None,
                      description="Edit")
        state["val"] = "B"
        tracker.invalidate_all()
        tracker.end_frame()

        assert not mgr.can_undo

    def test_invalidate_all_resets_activity_flags(self, _reset_undo_manager):
        tracker = InspectorUndoTracker()

        tracker.begin_frame()
        tracker.track("k1", lambda: "A", lambda s: None, "Edit")
        tracker.end_frame(any_item_active=True)

        tracker.invalidate_all()

        assert tracker._entries == {}
        assert tracker._was_active is False
        assert tracker._is_active is False


# ══════════════════════════════════════════════════════════════════════
# HierarchyUndoTracker
# ══════════════════════════════════════════════════════════════════════

class TestHierarchyUndoTracker:
    def test_rename_records_undo_and_redo(self, monkeypatch, _reset_undo_manager):
        class _GameObject:
            def __init__(self):
                self.id = 7
                self.name = "New Name"

        class _Scene:
            def __init__(self, obj):
                self.obj = obj

            def find_by_id(self, object_id):
                return self.obj if object_id == self.obj.id else None

        obj = _GameObject()
        scene = _Scene(obj)
        monkeypatch.setattr(_trackers_mod, "_get_active_scene", lambda: scene)
        monkeypatch.setattr(_helpers_mod, "_get_active_scene", lambda: scene)

        tracker = HierarchyUndoTracker()
        tracker.record_rename(obj.id, "Old Name", "New Name")

        mgr = _reset_undo_manager
        assert mgr.undo_description == "Rename GameObject"
        mgr.undo()
        assert obj.name == "Old Name"
        mgr.redo()
        assert obj.name == "New Name"


# ══════════════════════════════════════════════════════════════════════
# SelectionManager.set_ids
# ══════════════════════════════════════════════════════════════════════

class TestSelectionManagerSetIds:
    @pytest.fixture(autouse=True)
    def _fresh_selection(self):
        old = SelectionManager._instance
        sel = SelectionManager()
        yield sel
        SelectionManager._instance = old

    def test_set_ids_replaces_selection(self, _fresh_selection):
        sel = _fresh_selection
        sel.set_ids([10, 20, 30])
        assert sel.get_ids() == [10, 20, 30]
        assert sel.get_primary() == 30

    def test_set_ids_empty(self, _fresh_selection):
        sel = _fresh_selection
        sel.set_ids([1])
        sel.set_ids([])
        assert sel.get_ids() == []
        assert sel.get_primary() == 0

    def test_set_ids_fires_callback(self, _fresh_selection):
        sel = _fresh_selection
        called = []
        sel.add_listener(lambda: called.append(1))
        sel.set_ids([5])
        assert len(called) == 1

    def test_set_ids_no_change_no_callback(self, _fresh_selection):
        sel = _fresh_selection
        sel.set_ids([5])
        called = []
        sel.add_listener(lambda: called.append(1))
        sel.set_ids([5])  # same → should not fire
        assert len(called) == 0

    def test_clear(self, _fresh_selection):
        sel = _fresh_selection
        sel.select(42)
        sel.clear()
        assert sel.get_ids() == []
        assert sel.get_primary() == 0

    def test_toggle(self, _fresh_selection):
        sel = _fresh_selection
        sel.select(1)
        sel.toggle(2)
        assert sel.get_ids() == [1, 2]
        sel.toggle(1)
        assert sel.get_ids() == [2]


# ══════════════════════════════════════════════════════════════════════
# Integration: Selection undo via UndoManager
# ══════════════════════════════════════════════════════════════════════

class TestSelectionUndoIntegration:
    @pytest.fixture(autouse=True)
    def _fresh_selection(self):
        old = SelectionManager._instance
        self.sel = SelectionManager()
        yield
        SelectionManager._instance = old

    def _apply_fn(self, ids):
        self.sel.set_ids(ids)

    def test_editor_navigation_pushes_non_dirty_undo(self, _reset_undo_manager):
        module = _direct_import(
            "Infernux.engine._bootstrap_selection",
            "engine/_bootstrap_selection.py",
        )

        selection = module.BootstrapSelectionMixin()
        selection._prev_selection_ids = []
        selection._prev_selected_file = ""
        selection._apply_editor_selection_undo = lambda ids, path: (
            setattr(selection, "_prev_selection_ids", list(ids)),
            setattr(selection, "_prev_selected_file", path or ""),
        )
        selection._record_editor_selection_change([42], "")

        assert len(_reset_undo_manager._undo_stack) == 1
        assert isinstance(_reset_undo_manager._undo_stack[0], EditorSelectionCommand)
        assert not _reset_undo_manager._undo_stack[0].marks_dirty
        assert selection._prev_selection_ids == [42]

        _reset_undo_manager.undo()
        assert selection._prev_selection_ids == []

        _reset_undo_manager.redo()
        assert selection._prev_selection_ids == [42]

    def test_select_undo_redo_cycle(self, _reset_undo_manager, _fresh_selection):
        mgr = _reset_undo_manager
        sel = self.sel
        # Simulate: nothing selected → select [1]
        mgr.record(SelectionCommand([], [1], self._apply_fn))
        sel.set_ids([1])
        assert sel.get_primary() == 1

        mgr.undo()
        assert sel.get_ids() == []

        mgr.redo()
        assert sel.get_ids() == [1]

    def test_multi_selection_undo(self, _reset_undo_manager, _fresh_selection):
        mgr = _reset_undo_manager
        sel = self.sel

        mgr.record(SelectionCommand([], [1, 2, 3], self._apply_fn))
        sel.set_ids([1, 2, 3])

        mgr.undo()
        assert sel.get_ids() == []

    def test_chained_selection_undo(self, _reset_undo_manager, _fresh_selection):
        mgr = _reset_undo_manager
        sel = self.sel

        # Step 1: select [1]
        c1 = SelectionCommand([], [1], self._apply_fn)
        c1.timestamp = 0.0
        mgr.record(c1)
        sel.set_ids([1])

        # Step 2: select [2]
        c2 = SelectionCommand([1], [2], self._apply_fn)
        c2.timestamp = 10.0
        mgr.record(c2)
        sel.set_ids([2])

        # Undo step 2 → back to [1]
        mgr.undo()
        assert sel.get_ids() == [1]

        # Undo step 1 → back to []
        mgr.undo()
        assert sel.get_ids() == []


# ══════════════════════════════════════════════════════════════════════
# Delete/Create selection restore callback
# ══════════════════════════════════════════════════════════════════════

# The commands call _get_active_scene() which uses SceneManager (native).
# We patch that helper to return None so __init__ doesn't blow up, then
# poke internal fields to simulate the state we want to test.

_undo_mod_ref = _undo_mod  # keep reference for patching


class TestDeleteCommandSelectionRestore:
    """DeleteGameObjectCommand should capture pre-delete selection and restore
    it on undo via _selection_restore_fn."""

    @pytest.fixture(autouse=True)
    def _patch_scene(self, monkeypatch):
        _patch_undo_modules(monkeypatch, "_get_active_scene", lambda: None)
        _patch_undo_modules(monkeypatch, "_bump_inspector_structure", lambda: None)
        _patch_undo_modules(monkeypatch, "_notify_gizmos_scene_changed", lambda: None)

    @pytest.fixture(autouse=True)
    def _fresh_sel(self):
        old = SelectionManager._instance
        self.sel = SelectionManager()
        SelectionManager._instance = self.sel
        yield
        SelectionManager._instance = old

    def test_captures_pre_delete_selection(self):
        self.sel.set_ids([42])
        cmd = DeleteGameObjectCommand(42, "Delete")
        assert cmd._pre_delete_selection_ids == [42]

    def test_undo_calls_selection_restore(self, _reset_undo_manager):
        restored = []
        old_fn = DeleteGameObjectCommand._selection_restore_fn
        DeleteGameObjectCommand._selection_restore_fn = lambda ids: restored.append(list(ids))
        try:
            self.sel.set_ids([42])
            cmd = DeleteGameObjectCommand(42, "Delete")
            cmd._document = {"id": 42}
            with _override_recreate_game_object(lambda *a, **k: None):
                cmd.undo()
            assert restored == [[42]]
        finally:
            DeleteGameObjectCommand._selection_restore_fn = old_fn

    def test_redo_clears_selection(self, _reset_undo_manager):
        restored = []
        old_fn = DeleteGameObjectCommand._selection_restore_fn
        DeleteGameObjectCommand._selection_restore_fn = lambda ids: restored.append(list(ids))
        try:
            cmd = DeleteGameObjectCommand(42, "Delete")
            cmd.redo()  # no scene → no-op destruction, but selection should clear
            assert restored == [[]]
        finally:
            DeleteGameObjectCommand._selection_restore_fn = old_fn

    def test_no_restore_when_callback_not_set(self, _reset_undo_manager):
        old_fn = DeleteGameObjectCommand._selection_restore_fn
        DeleteGameObjectCommand._selection_restore_fn = None
        try:
            cmd = DeleteGameObjectCommand(42, "Delete")
            cmd._pre_delete_selection_ids = [42]
            cmd._document = {"id": 42}
            with _override_recreate_game_object(lambda *a, **k: None):
                cmd.undo()  # should not raise
        finally:
            DeleteGameObjectCommand._selection_restore_fn = old_fn

    def test_empty_selection_not_restored(self, _reset_undo_manager):
        """If nothing was selected before delete, undo doesn't call restore."""
        restored = []
        old_fn = DeleteGameObjectCommand._selection_restore_fn
        DeleteGameObjectCommand._selection_restore_fn = lambda ids: restored.append(list(ids))
        try:
            # Don't select anything → pre_delete_selection_ids is []
            cmd = DeleteGameObjectCommand(42, "Delete")
            cmd._document = {"id": 42}
            with _override_recreate_game_object(lambda *a, **k: None):
                cmd.undo()
            # Empty list → fn not called (guard: `if fn and ids`)
            assert restored == []
        finally:
            DeleteGameObjectCommand._selection_restore_fn = old_fn


class TestDeleteGameObjectsCommand:
    class _Transform:
        def __init__(self, sibling_index):
            self._sibling_index = sibling_index

        def get_sibling_index(self):
            return self._sibling_index

    class _Object:
        def __init__(self, object_id, sibling_index, parent=None):
            self.id = object_id
            self.transform = TestDeleteGameObjectsCommand._Transform(sibling_index)
            self._parent = parent

        def get_parent(self):
            return self._parent

    class _Scene:
        def __init__(self, objects):
            self.objects = {obj.id: obj for obj in objects}

        def find_by_id(self, object_id):
            return self.objects.get(object_id)

    def test_filters_selected_descendants_and_restores_sibling_order(self, monkeypatch):
        first = self._Object(10, 0)
        parent = self._Object(20, 1)
        child = self._Object(21, 0, parent)
        scene = self._Scene([first, parent, child])
        destroyed = []
        restored = []

        monkeypatch.setattr(_structural_mod, "_get_active_scene", lambda: scene)
        monkeypatch.setattr(_structural_mod, "_get_current_selection_ids", lambda: [20, 21, 10])
        monkeypatch.setattr(_structural_mod, "_snapshot_object", lambda obj: {"id": obj.id})
        monkeypatch.setattr(
            _structural_mod,
            "_destroy_game_object_immediately",
            lambda _scene, obj: destroyed.append(obj.id),
        )
        monkeypatch.setattr(_structural_mod, "_bump_inspector_structure", lambda: None)
        monkeypatch.setattr(_structural_mod, "_notify_gizmos_scene_changed", lambda: None)

        old_fn = DeleteGameObjectsCommand._selection_restore_fn
        DeleteGameObjectsCommand._selection_restore_fn = lambda ids: restored.append(list(ids))
        try:
            command = DeleteGameObjectsCommand([20, 21, 10])
            assert [entry["object_id"] for entry in command._entries] == [20, 10]

            command.execute()
            assert destroyed == [20, 10]

            with _override_recreate_game_object(
                lambda document, parent_id, sibling_index: restored.append(
                    (document["id"], parent_id, sibling_index)
                )
            ):
                command.undo()
            assert restored == [(10, None, 0), (20, None, 1), [20, 21, 10]]
        finally:
            DeleteGameObjectsCommand._selection_restore_fn = old_fn


class TestCreateCommandSelectionRestore:
    """CreateGameObjectCommand should clear selection on undo and re-select
    on redo via _selection_restore_fn."""

    @pytest.fixture(autouse=True)
    def _patch_scene(self, monkeypatch):
        _patch_undo_modules(monkeypatch, "_get_active_scene", lambda: None)
        _patch_undo_modules(monkeypatch, "_bump_inspector_structure", lambda: None)
        _patch_undo_modules(monkeypatch, "_notify_gizmos_scene_changed", lambda: None)

    def test_undo_clears_selection(self, _reset_undo_manager):
        restored = []
        old_fn = CreateGameObjectCommand._selection_restore_fn
        CreateGameObjectCommand._selection_restore_fn = lambda ids: restored.append(list(ids))
        try:
            cmd = CreateGameObjectCommand(99, "Create")
            cmd.undo()  # no scene → no-op destruction, but selection should clear
            assert restored == [[]]
        finally:
            CreateGameObjectCommand._selection_restore_fn = old_fn


class TestImmediateDestroyHelpers:
    @pytest.fixture(autouse=True)
    def _patch_editor_side_effects(self, monkeypatch):
        _patch_undo_modules(monkeypatch, "_bump_inspector_structure", lambda: None)
        _patch_undo_modules(monkeypatch, "_notify_gizmos_scene_changed", lambda: None)

    def test_destroy_game_object_immediately_invalidates_tree_and_flushes(self, monkeypatch):
        class _FakeWrapper:
            def __init__(self):
                self.invalidated = 0

            def _invalidate_native_binding(self):
                self.invalidated += 1

        class _FakeComp:
            def __init__(self, comp_id):
                self.component_id = comp_id

        class _FakeObject:
            def __init__(self, object_id, name, components=None, children=None):
                self.id = object_id
                self.name = name
                self._components = list(components or [])
                self._children = list(children or [])

            def get_components(self):
                return list(self._components)

            def get_children(self):
                return list(self._children)

        class _FakeScene:
            def __init__(self):
                self.calls = []

            def destroy_game_object(self, obj):
                self.calls.append(("destroy", obj.id))

            def process_pending_destroys(self):
                self.calls.append(("flush", None))

        w1 = _FakeWrapper()
        w2 = _FakeWrapper()
        fake_builtin_mod = types.ModuleType("Infernux.components.builtin_component")
        fake_builtin_mod.BuiltinComponent = types.SimpleNamespace(
            _wrapper_cache={10: w1, 20: w2}
        )
        monkeypatch.setitem(sys.modules, "Infernux.components.builtin_component", fake_builtin_mod)

        child = _FakeObject(2, "Child", [_FakeComp(20)])
        root = _FakeObject(1, "Root", [_FakeComp(10)], [child])
        scene = _FakeScene()
        side_fx = []
        _patch_undo_modules(monkeypatch, "_bump_inspector_structure", lambda: side_fx.append("bump"))
        _patch_undo_modules(monkeypatch, "_notify_gizmos_scene_changed", lambda: side_fx.append("gizmo"))

        _undo_mod_ref._destroy_game_object_immediately(scene, root)

        assert scene.calls == [("destroy", 1), ("flush", None)]
        assert w1.invalidated == 1
        assert w2.invalidated == 1
        assert side_fx == ["bump", "gizmo"]

    def test_create_delete_commands_use_immediate_destroy_helper(self, monkeypatch):
        class _FakeTransform:
            def get_sibling_index(self):
                return 0

        class _FakeObject:
            def __init__(self, object_id):
                self.id = object_id
                self.name = f"Obj{object_id}"
                self.transform = _FakeTransform()

            def serialize_document(self):
                return {"id": self.id, "components": [], "children": []}

            def get_components(self):
                return []

            def get_children(self):
                return []

            def get_parent(self):
                return None

        class _FakeScene:
            def __init__(self, obj):
                self._obj = obj

            def find_by_id(self, object_id):
                if self._obj.id == object_id:
                    return self._obj
                return None

        fake_obj = _FakeObject(42)
        fake_scene = _FakeScene(fake_obj)
        calls = []
        _patch_undo_modules(monkeypatch, "_get_active_scene", lambda: fake_scene)
        _patch_undo_modules(
            monkeypatch,
            "_destroy_game_object_immediately",
            lambda scene, obj: calls.append((scene is fake_scene, obj.id)),
        )

        create_cmd = CreateGameObjectCommand(42, "Create")
        create_cmd.undo()

        delete_cmd = DeleteGameObjectCommand(42, "Delete")
        delete_cmd.execute()
        delete_cmd.redo()

        assert calls == [(True, 42), (True, 42), (True, 42)]

    def test_redo_restores_selection(self, _reset_undo_manager):
        restored = []
        old_fn = CreateGameObjectCommand._selection_restore_fn
        CreateGameObjectCommand._selection_restore_fn = lambda ids: restored.append(list(ids))
        try:
            cmd = CreateGameObjectCommand(99, "Create")
            cmd._post_create_ids = [99]
            cmd._document = {"id": 99}
            with _override_recreate_game_object(lambda *a, **k: None):
                cmd.redo()
            assert restored == [[99]]
        finally:
            CreateGameObjectCommand._selection_restore_fn = old_fn

    def test_redo_no_restore_when_no_post_ids(self, _reset_undo_manager):
        """If post_create_ids is empty, redo doesn't call restore."""
        restored = []
        old_fn = CreateGameObjectCommand._selection_restore_fn
        CreateGameObjectCommand._selection_restore_fn = lambda ids: restored.append(list(ids))
        try:
            cmd = CreateGameObjectCommand(99, "Create")
            cmd._post_create_ids = []
            cmd._document = {"id": 99}
            with _override_recreate_game_object(lambda *a, **k: None):
                cmd.redo()
            assert restored == []
        finally:
            CreateGameObjectCommand._selection_restore_fn = old_fn
