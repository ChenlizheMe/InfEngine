"""Tests for Infernux.engine.undo — UndoCommand subclasses and UndoManager.

Pure-Python tests — no C++ backend needed.
Imports bypass the heavy Infernux.__init__ / native-module chain by loading
the undo package directly via importlib.
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
RemoveComponentsCommand = _undo_mod.RemoveComponentsCommand
GlobalSelectionCommand = _undo_mod.GlobalSelectionCommand
PrefabModeCommand = _undo_mod.PrefabModeCommand
PrefabUnpackCommand = _undo_mod.PrefabUnpackCommand
PrefabRevertCommand = _undo_mod.PrefabRevertCommand
RenderStackFieldCommand = _undo_mod.RenderStackFieldCommand
_snapshot_value = _undo_mod._snapshot_value
SelectionService = sys.modules["Infernux.engine.interaction"].SelectionService
SelectionSnapshot = sys.modules["Infernux.engine.interaction"].SelectionSnapshot
SelectionTarget = sys.modules["Infernux.engine.interaction"].SelectionTarget
EditorContextSnapshot = sys.modules["Infernux.engine.interaction"].EditorContextSnapshot
ContextRestoreStatus = sys.modules["Infernux.engine.interaction"].ContextRestoreStatus
_helpers_mod = sys.modules["Infernux.engine.undo._helpers"]
_property_mod = sys.modules["Infernux.engine.undo._property_commands"]
_structural_mod = sys.modules["Infernux.engine.undo._structural_commands"]
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
    def __init__(self, pipeline=None):
        self.pipeline_class_name = ""
        self.pipeline = pipeline
        self.invalidated = 0
        self.synced = 0

    def set_pipeline_parameter(self, field_name, value, **_kwargs):
        setattr(self.pipeline, field_name, value)
        self.synced += 1
        self.invalidate_graph()

    def invalidate_graph(self):
        self.invalidated += 1


@pytest.fixture(autouse=True)
def _reset_undo_manager():
    """Ensure a fresh UndoManager for every test; tear down afterwards."""
    old = UndoManager._instance
    mgr = UndoManager()
    # Prevent dirty-sync from importing SceneFileManager / PlayModeManager
    yield mgr
    UndoManager._instance = old


def test_unbound_component_identity_probe_falls_back_to_own_id():
    class _UnboundComponent:
        id = 73

        @property
        def game_object(self):
            raise RuntimeError("not bound")

    component = _UnboundComponent()
    assert _helpers_mod._game_object_id_of(component) == 73
    assert _helpers_mod._comp_type_name_of(component) == "GameObject"


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

    def test_explicit_component_actions_do_not_merge(self):
        comp = _FakeComp()
        continuous = GenericComponentCommand(comp, {}, {"a": 1})
        discrete = GenericComponentCommand(
            comp,
            {"a": 1},
            {"a": 2},
            mergeable=False,
        )
        discrete.timestamp = continuous.timestamp + 0.1

        assert not continuous.can_merge(discrete)
        assert not discrete.can_merge(continuous)


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

    def test_only_explicit_selection_context_propagates(self):
        before = SelectionSnapshot.create(
            (SelectionTarget.scene_object(7),),
            owner_id="hierarchy",
        )
        after = SelectionSnapshot.create(
            (SelectionTarget.scene_object(42),),
            owner_id="hierarchy",
        )
        inspector = SetPropertyCommand(_Obj(), "x", 0, 1, "Edit Property")
        create = CreateGameObjectCommand(
            42,
            before_selection=before,
            after_selection=after,
        )

        inspector_only = CompoundCommand([inspector])
        structural = CompoundCommand([inspector, create])

        assert inspector_only.before_selection_snapshot is None
        assert inspector_only.after_selection_snapshot is None
        assert structural.before_selection_snapshot == before
        assert structural.after_selection_snapshot == after

    def test_compound_add_component_undo_reverses_auto_created(self):
        """Simulate adding a component with an automatically-created dependency.

        A CompoundCommand [AddDependency, AddMain] should remove the dependent
        component first and recreate the dependency first.
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

        # AddComponentTransactionCommand stores dependencies first, main last.
        compound = CompoundCommand(
            [FakeAddCmd("Dependency"), FakeAddCmd("Main")],
            "Add Main")

        # Undo should reverse: remove Main, then Dependency
        compound.undo()
        assert log == [("remove", "Main"), ("remove", "Dependency")]

        log.clear()
        # Redo should replay in order: add Dependency, then Main
        compound.redo()
        assert log == [("re-add", "Dependency"), ("re-add", "Main")]

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

    def test_component_batch_removal_owns_selection_transition(self):
        previous = SelectionService._instance
        service = SelectionService()
        before = SelectionSnapshot.create(
            (
                SelectionTarget.component(41, 101),
                SelectionTarget.component(42, 102),
            ),
            owner_id="inspector",
            primary=SelectionTarget.component(42, 102),
        )
        after = SelectionSnapshot.create(
            (
                SelectionTarget.scene_object(41),
                SelectionTarget.scene_object(42),
            ),
            owner_id="inspector",
            primary=SelectionTarget.scene_object(42),
        )
        service.apply_snapshot(before, record_history=False)
        calls = []

        class RemoveStub(UndoCommand):
            def __init__(self, component_id):
                super().__init__(f"Remove {component_id}")
                self.component_id = component_id

            def execute(self):
                calls.append(("remove", self.component_id))

            def undo(self):
                calls.append(("restore", self.component_id))

        command = RemoveComponentsCommand(
            [RemoveStub(101), RemoveStub(102)],
            before,
            after,
        )
        try:
            command.execute()
            assert calls == [("remove", 101), ("remove", 102)]
            assert service.snapshot == after

            command.undo()
            assert calls[-2:] == [("restore", 102), ("restore", 101)]
            assert service.snapshot == before

            command.redo()
            assert calls[-2:] == [("remove", 101), ("remove", 102)]
            assert service.snapshot == after
        finally:
            SelectionService._instance = previous


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
# GlobalSelectionCommand
# ══════════════════════════════════════════════════════════════════════

class TestGlobalSelectionCommand:
    def test_undo_redo_are_inert_because_journal_restores_context(self):
        old = SelectionSnapshot.create(
            (SelectionTarget.scene_object(1), SelectionTarget.scene_object(2)),
            owner_id="hierarchy",
        )
        new = SelectionSnapshot.create(
            (SelectionTarget.asset("Assets/test.mat"),),
            owner_id="project",
        )
        cmd = GlobalSelectionCommand(old, new)
        cmd.undo()
        cmd.redo()
        assert cmd.before_selection_snapshot == old
        assert cmd.after_selection_snapshot == new

    def test_execute_is_noop(self):
        applied = []
        empty = SelectionSnapshot()
        selected = SelectionSnapshot.create(
            (SelectionTarget.scene_object(1),),
            owner_id="hierarchy",
        )
        cmd = GlobalSelectionCommand(empty, selected)
        cmd.execute()
        assert applied == []

    def test_is_non_dirty_and_exposes_explicit_context(self):
        old = SelectionSnapshot()
        new = SelectionSnapshot.create(
            (SelectionTarget.scene_object(1),),
            owner_id="hierarchy",
        )
        cmd = GlobalSelectionCommand(old, new)
        assert cmd.marks_dirty is False
        assert cmd.description == "Change Selection"
        assert cmd.before_selection_snapshot == old
        assert cmd.after_selection_snapshot == new


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

    def test_rejected_transition_raises_and_cannot_be_recorded(self, monkeypatch):
        class _FakeSceneManager:
            @staticmethod
            def open_prefab_mode(_path, preserve_undo_history=False):
                del preserve_undo_history
                return False

        scene_manager_mod = types.ModuleType("Infernux.engine.scene_manager")

        class _SceneFileManager:
            @staticmethod
            def instance():
                return _FakeSceneManager()

        scene_manager_mod.SceneFileManager = _SceneFileManager
        monkeypatch.setitem(sys.modules, "Infernux.engine.scene_manager", scene_manager_mod)

        command = PrefabModeCommand("Assets/test.prefab", enter_mode=True)
        with pytest.raises(RuntimeError, match="Enter Prefab Mode was rejected"):
            command.execute()


# ══════════════════════════════════════════════════════════════════════
# RenderStackFieldCommand
# ══════════════════════════════════════════════════════════════════════

class TestRenderStackFieldCommand:
    def test_execute_undo_redo(self):
        target = _Obj()
        stack = _FakeStack(target)
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
        stack = _FakeStack(target)
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
        stack = _FakeStack(target)
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
        assert len(mgr.action_journal.applied_entries()) == 1
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
        assert len(mgr.action_journal.applied_entries()) == 5

    def test_disabled_rejects_user_edits_but_allows_explicit_system_work(self, _reset_undo_manager):
        from Infernux.engine.interaction import ActionOrigin

        mgr = _reset_undo_manager
        mgr.enabled = False
        obj = _Obj()
        assert not mgr.execute(SetPropertyCommand(obj, "x", 0, 5))
        assert obj.x == 0
        assert mgr.execute(
            SetPropertyCommand(obj, "x", 0, 5),
            origin=ActionOrigin.SYSTEM,
        )
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
# UndoManager — save points belong to DocumentRegistry
# ══════════════════════════════════════════════════════════════════════

class TestUndoManagerSavePoint:
    def test_selection_command_does_not_dirty(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        old = SelectionSnapshot.create(
            (SelectionTarget.scene_object(1),),
            owner_id="hierarchy",
        )
        new = SelectionSnapshot.create(
            (SelectionTarget.scene_object(2),),
            owner_id="hierarchy",
        )
        cmd = GlobalSelectionCommand(old, new)
        mgr.record(cmd)
        # UndoManager exposes no independent save-point API.
        assert not hasattr(mgr, "mark_save_point")
        assert not hasattr(mgr, "is_at_save_point")


# ══════════════════════════════════════════════════════════════════════
# Typed scene-object selection operations
# ══════════════════════════════════════════════════════════════════════

class TestSceneObjectSelectionOperations:
    @pytest.fixture(autouse=True)
    def _fresh_selection(self):
        old = SelectionService._instance
        sel = SelectionService()
        yield sel
        SelectionService._instance = old

    def test_set_ids_replaces_selection(self, _fresh_selection):
        sel = _fresh_selection
        sel.replace_scene_objects(
            [10, 20, 30], owner_id="hierarchy", record_history=False
        )
        assert sel.scene_object_ids() == (10, 20, 30)
        assert sel.primary_scene_object_id() == 30

    def test_set_ids_empty(self, _fresh_selection):
        sel = _fresh_selection
        sel.select_scene_object(1, owner_id="hierarchy", record_history=False)
        sel.replace_scene_objects([], owner_id="hierarchy", record_history=False)
        assert sel.scene_object_ids() == ()
        assert sel.primary_scene_object_id() == 0

    def test_set_ids_fires_callback(self, _fresh_selection):
        sel = _fresh_selection
        called = []
        sel.add_listener(lambda _change: called.append(1))
        sel.select_scene_object(5, owner_id="hierarchy", record_history=False)
        assert len(called) == 1

    def test_set_ids_no_change_no_callback(self, _fresh_selection):
        sel = _fresh_selection
        sel.select_scene_object(5, owner_id="hierarchy", record_history=False)
        called = []
        sel.add_listener(lambda _change: called.append(1))
        sel.select_scene_object(
            5, owner_id="hierarchy", record_history=False
        )  # same -> should not fire
        assert len(called) == 0

    def test_clear(self, _fresh_selection):
        sel = _fresh_selection
        sel.select_scene_object(42, owner_id="hierarchy", record_history=False)
        sel.clear(reason="test_clear", record_history=False)
        assert sel.scene_object_ids() == ()
        assert sel.primary_scene_object_id() == 0

    def test_toggle(self, _fresh_selection):
        sel = _fresh_selection
        sel.select_scene_object(1, owner_id="hierarchy", record_history=False)
        sel.toggle_scene_object(2, owner_id="hierarchy", record_history=False)
        assert sel.scene_object_ids() == (1, 2)
        sel.toggle_scene_object(1, owner_id="hierarchy", record_history=False)
        assert sel.scene_object_ids() == (2,)


# ══════════════════════════════════════════════════════════════════════
# Integration: Selection undo via UndoManager
# ══════════════════════════════════════════════════════════════════════

class TestSelectionUndoIntegration:
    @pytest.fixture(autouse=True)
    def _fresh_selection(self):
        old = SelectionService._instance
        self.sel = SelectionService()
        yield
        SelectionService._instance = old

    @staticmethod
    def _install_recording_listener():
        module = _direct_import(
            "Infernux.engine._bootstrap_selection",
            "engine/_bootstrap_selection.py",
        )
        selection = module.BootstrapSelectionMixin()
        selection.window_manager = None
        selection._present_selection_snapshot = lambda _snapshot: None
        selection._prev_selection_snapshot = SelectionService.instance().snapshot
        SelectionService.instance().add_listener(
            selection._on_global_selection_changed
        )
        manager = UndoManager.instance()
        manager.set_context_hooks(
            lambda: EditorContextSnapshot(
                selection=SelectionService.instance().snapshot,
            ),
            lambda context, _phase: (
                SelectionService.instance().apply_snapshot(
                    context.selection,
                    reason="test_restore",
                    record_history=False,
                ),
                ContextRestoreStatus.READY,
            )[1],
        )
        return selection

    def test_editor_navigation_pushes_non_dirty_undo(self, _reset_undo_manager):
        self._install_recording_listener()
        SelectionService.instance().select(
            SelectionTarget.scene_object(42),
            owner_id="hierarchy",
            record_history=True,
        )

        entries = _reset_undo_manager.action_journal.applied_entries()
        assert len(entries) == 1
        assert isinstance(entries[0].action, GlobalSelectionCommand)
        assert not entries[0].action.marks_dirty

        _reset_undo_manager.undo()
        assert SelectionService.instance().snapshot.is_empty

        _reset_undo_manager.redo()
        assert (
            SelectionService.instance().snapshot.primary
            == SelectionTarget.scene_object(42)
        )

    def test_chained_typed_selection_uses_one_global_cursor(self, _reset_undo_manager):
        mgr = _reset_undo_manager
        self._install_recording_listener()
        service = SelectionService.instance()
        service.select(
            SelectionTarget.scene_object(1),
            owner_id="hierarchy",
            record_history=True,
        )
        service.select(
            SelectionTarget.scene_object(2),
            owner_id="scene_view",
            record_history=True,
        )
        assert len(mgr.action_journal.entries) == 2

        mgr.undo()
        assert service.snapshot.primary == SelectionTarget.scene_object(1)

        mgr.undo()
        assert service.snapshot == SelectionSnapshot()

        mgr.redo()
        assert service.snapshot.primary == SelectionTarget.scene_object(1)

    def test_component_header_selection_undo_restores_object_context(
        self,
        _reset_undo_manager,
    ):
        manager = _reset_undo_manager
        self._install_recording_listener()
        service = SelectionService.instance()
        service.select(
            SelectionTarget.scene_object(42),
            owner_id="hierarchy",
            record_history=False,
        )
        component = SelectionTarget.component(
            42,
            701,
            document_id="scene:active",
            sub_kind="native",
        )

        service.select(
            component,
            owner_id="inspector",
            reason="inspector_component_select",
            record_history=True,
        )

        assert len(manager.action_journal.entries) == 1
        assert service.snapshot.primary == component
        manager.undo()
        assert service.snapshot.primary == SelectionTarget.scene_object(42)
        manager.redo()
        assert service.snapshot.primary == component


# ══════════════════════════════════════════════════════════════════════
# Structural command selection context
# ══════════════════════════════════════════════════════════════════════

# The commands call _get_active_scene() which uses SceneManager (native).
# We patch that helper to return None so __init__ doesn't blow up, then
# poke internal fields to simulate the state we want to test.

_undo_mod_ref = _undo_mod  # keep reference for patching


class TestStructuralCommandSelectionContext:
    """Structural commands use the global action context for selection."""

    @pytest.fixture(autouse=True)
    def _patch_scene(self, monkeypatch):
        _patch_undo_modules(monkeypatch, "_get_active_scene", lambda: None)
        _patch_undo_modules(monkeypatch, "_bump_inspector_structure", lambda: None)
        _patch_undo_modules(monkeypatch, "_notify_gizmos_scene_changed", lambda: None)

    @pytest.fixture(autouse=True)
    def _fresh_sel(self):
        old = SelectionService._instance
        self.sel = SelectionService()
        yield
        SelectionService._instance = old

    def test_delete_is_one_action_and_context_restores_selection(
        self,
        monkeypatch,
        _reset_undo_manager,
    ):
        class _Transform:
            @staticmethod
            def get_sibling_index():
                return 0

        class _Object:
            id = 42
            transform = _Transform()

            @staticmethod
            def get_parent():
                return None

        class _Scene:
            def __init__(self):
                self.object = _Object()

            def find_by_id(self, object_id):
                return self.object if self.object and object_id == 42 else None

        scene = _Scene()
        monkeypatch.setattr(_structural_mod, "_get_active_scene", lambda: scene)
        monkeypatch.setattr(_structural_mod, "_snapshot_object", lambda _obj: {"id": 42})
        monkeypatch.setattr(
            _structural_mod,
            "_destroy_game_object_immediately",
            lambda _scene, _obj: setattr(scene, "object", None),
        )

        service = SelectionService.instance()
        selected = SelectionSnapshot.create(
            (SelectionTarget.scene_object(42),),
            owner_id="hierarchy",
        )
        service.apply_snapshot(selected, record_history=False)
        manager = _reset_undo_manager
        restore_phases = []

        def _restore_context(context, phase):
            if phase == "undo_complete":
                assert scene.object is not None
            restore_phases.append(phase)
            service.apply_snapshot(context.selection, record_history=False)

        manager.set_context_hooks(
            lambda: EditorContextSnapshot(selection=service.snapshot),
            _restore_context,
        )

        assert manager.execute(DeleteGameObjectCommand(42, "Delete"))
        assert service.snapshot == SelectionSnapshot()
        assert len(manager.action_journal.entries) == 1

        def restore_object(*_args, **_kwargs):
            scene.object = _Object()
            return scene.object

        with _override_recreate_game_object(restore_object):
            manager.undo()
        assert service.snapshot == selected
        assert scene.object is not None
        assert len(manager.action_journal.entries) == 1
        assert restore_phases == ["prepare_undo", "undo_complete"]

        manager.redo()
        assert service.snapshot == SelectionSnapshot()
        assert scene.object is None
        assert len(manager.action_journal.entries) == 1

    def test_failed_delete_undo_keeps_cursor_and_selection(
        self,
        monkeypatch,
        _reset_undo_manager,
    ):
        class _Transform:
            @staticmethod
            def get_sibling_index():
                return 0

        class _Object:
            id = 42
            transform = _Transform()

            @staticmethod
            def get_parent():
                return None

        class _Scene:
            def __init__(self):
                self.object = _Object()

            def find_by_id(self, object_id):
                return self.object if self.object and object_id == 42 else None

        scene = _Scene()
        monkeypatch.setattr(_structural_mod, "_get_active_scene", lambda: scene)
        monkeypatch.setattr(_structural_mod, "_snapshot_object", lambda _obj: {"id": 42})
        monkeypatch.setattr(
            _structural_mod,
            "_destroy_game_object_immediately",
            lambda _scene, _obj: setattr(scene, "object", None),
        )
        service = SelectionService.instance()
        selected = SelectionSnapshot.create(
            (SelectionTarget.scene_object(42),), owner_id="hierarchy"
        )
        service.apply_snapshot(selected, record_history=False)
        manager = _reset_undo_manager
        manager.set_context_hooks(
            lambda: EditorContextSnapshot(selection=service.snapshot),
            lambda context, _phase: service.apply_snapshot(
                context.selection, record_history=False
            ),
        )

        assert manager.execute(DeleteGameObjectCommand(42, "Delete"))
        assert service.snapshot.is_empty
        assert manager.can_undo

        with _override_recreate_game_object(lambda *_args, **_kwargs: None):
            manager.undo()

        assert manager.can_undo
        assert not manager.can_redo
        assert service.snapshot.is_empty
        assert scene.object is None

    def test_create_undo_prunes_only_destroyed_selection(self, monkeypatch):
        class _Transform:
            @staticmethod
            def get_sibling_index():
                return 0

        class _Object:
            id = 99
            transform = _Transform()

            @staticmethod
            def get_parent():
                return None

            @staticmethod
            def get_children():
                return []

        class _Scene:
            @staticmethod
            def find_by_id(object_id):
                return _Object() if object_id == 99 else None

        monkeypatch.setattr(_structural_mod, "_get_active_scene", lambda: _Scene())
        monkeypatch.setattr(_structural_mod, "_snapshot_object", lambda _obj: {"id": 99})
        monkeypatch.setattr(
            _structural_mod,
            "_destroy_game_object_immediately",
            lambda _scene, _obj: None,
        )
        self.sel.replace_scene_objects(
            [7, 99], owner_id="hierarchy", record_history=False
        )

        CreateGameObjectCommand(99, "Create").undo()

        assert self.sel.scene_object_ids() == (7,)

    def test_deleting_unselected_object_preserves_selection(self, monkeypatch):
        class _Transform:
            @staticmethod
            def get_sibling_index():
                return 0

        class _Object:
            id = 42
            transform = _Transform()

            @staticmethod
            def get_parent():
                return None

            @staticmethod
            def get_children():
                return []

        class _Scene:
            @staticmethod
            def find_by_id(object_id):
                return _Object() if object_id == 42 else None

        monkeypatch.setattr(_structural_mod, "_get_active_scene", lambda: _Scene())
        monkeypatch.setattr(_structural_mod, "_snapshot_object", lambda _obj: {"id": 42})
        monkeypatch.setattr(
            _structural_mod,
            "_destroy_game_object_immediately",
            lambda _scene, _obj: None,
        )
        self.sel.select_scene_object(
            7, owner_id="hierarchy", record_history=False
        )

        DeleteGameObjectCommand(42, "Delete").execute()

        assert self.sel.scene_object_ids() == (7,)


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
        monkeypatch.setattr(_structural_mod, "_snapshot_object", lambda obj: {"id": obj.id})
        monkeypatch.setattr(
            _structural_mod,
            "_destroy_game_object_immediately",
            lambda _scene, obj: destroyed.append(obj.id),
        )
        monkeypatch.setattr(_structural_mod, "_bump_inspector_structure", lambda: None)
        monkeypatch.setattr(_structural_mod, "_notify_gizmos_scene_changed", lambda: None)

        command = DeleteGameObjectsCommand([20, 21, 10])
        assert [entry["object_id"] for entry in command._entries] == [20, 10]

        command.execute()
        assert destroyed == [20, 10]

        def restore_object(document, parent_id, sibling_index):
            restored.append((document["id"], parent_id, sibling_index))
            return self._Object(document["id"], sibling_index)

        with _override_recreate_game_object(restore_object):
            command.undo()
        assert restored == [(10, None, 0), (20, None, 1)]


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
