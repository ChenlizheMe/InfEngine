from __future__ import annotations

import importlib
import py_compile
import sys

import pytest

from Infernux.components.script_loader import load_component_class_from_file
from Infernux.components import InxComponent
from Infernux.components.registry import get_type_by_identity
from Infernux.engine.component_restore import create_component_instance
from Infernux.engine.project_context import get_project_root, set_project_root


def test_script_loader_falls_back_to_sole_class_after_rename(tmp_path):
    script = tmp_path / "strict_component.py"
    script.write_text(
        "from Infernux.components import InxComponent\n"
        "class CurrentComponent(InxComponent):\n"
        "    speed: float = 1.0\n",
        encoding="utf-8",
    )

    # One-component scripts tolerate an authored class rename.
    remapped = load_component_class_from_file(str(script), "RemovedComponent")
    assert remapped is not None
    assert remapped.__name__ == "CurrentComponent"

    script.write_text(
        "from Infernux.components import InxComponent\n"
        "class CurrentComponent(InxComponent):\n"
        "    speed: float = 1.0\n"
        "class OtherComponent(InxComponent):\n"
        "    value: int = 1\n",
        encoding="utf-8",
    )
    # Multi-component scripts stay strict to avoid picking the wrong class.
    assert load_component_class_from_file(str(script), "RemovedComponent") is None


def test_component_identity_distinguishes_same_named_classes():
    first = type("IdentityTwin", (InxComponent,), {"__module__": "identity_module_a"})
    second = type("IdentityTwin", (InxComponent,), {"__module__": "identity_module_b"})

    assert first._get_intrinsic_script_guid() != second._get_intrinsic_script_guid()
    assert first._get_type_guid() != second._get_type_guid()
    assert len(first._get_intrinsic_script_guid()) == 32
    assert len(first._get_type_guid()) == 32

    assert get_type_by_identity(
        "IdentityTwin",
        first._get_intrinsic_script_guid(),
        first._get_type_guid(),
    ) is first
    assert get_type_by_identity(
        "IdentityTwin",
        first._get_intrinsic_script_guid(),
        second._get_type_guid(),
    ) is None

    instance, path = create_component_instance(
        first._get_intrinsic_script_guid(),
        first._get_type_guid(),
        "IdentityTwin",
    )
    assert type(instance) is first
    assert path is None


def test_script_loader_executes_exact_pyc_with_canonical_project_module(tmp_path, monkeypatch):
    project = tmp_path / "project"
    package = project / "Assets" / "Gameplay"
    package.mkdir(parents=True)
    source = package / "Controller.py"
    bytecode = package / "Controller.pyc"
    source.write_text(
        "from Infernux import InxComponent\n"
        "class Controller(InxComponent):\n"
        "    def update(self, delta_time):\n"
        "        self.last_delta = delta_time\n",
        encoding="utf-8",
    )
    py_compile.compile(str(source), cfile=str(bytecode), doraise=True)
    source.unlink()

    previous_root = get_project_root()
    module_names = ("Gameplay.Controller", "Assets.Gameplay.Controller")
    saved_modules = {name: sys.modules.get(name) for name in module_names}
    for name in module_names:
        sys.modules.pop(name, None)
    set_project_root(str(project))
    original_import_module = importlib.import_module

    def guarded_import_module(name, package=None):
        if name in module_names:
            raise AssertionError("script loader must execute the GUID-resolved artifact directly")
        return original_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", guarded_import_module)
    try:
        loaded = load_component_class_from_file(str(bytecode), "Controller")

        assert loaded is not None
        assert loaded.update is not InxComponent.update
        assert loaded is sys.modules["Gameplay.Controller"].Controller
        assert "Assets.Gameplay.Controller" not in sys.modules
        with pytest.raises(ModuleNotFoundError):
            original_import_module("Assets.Gameplay.Controller")
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("Gameplay.NotPresent")
    finally:
        set_project_root(previous_root)
        for name in module_names:
            sys.modules.pop(name, None)
        for name, module in saved_modules.items():
            if module is not None:
                sys.modules[name] = module


def test_component_restore_keeps_manifest_guid_for_packaged_pyc(tmp_path):
    project = tmp_path / "build" / "Data"
    package = project / "Assets" / "Gameplay"
    package.mkdir(parents=True)
    source = package / "Controller.py"
    bytecode = package / "Controller.pyc"
    source.write_text(
        "from Infernux import InxComponent\n"
        "class Controller(InxComponent):\n"
        "    def update(self, delta_time):\n"
        "        self.last_delta = delta_time\n",
        encoding="utf-8",
    )
    py_compile.compile(str(source), cfile=str(bytecode), doraise=True)
    source.unlink()

    script_guid = "5f5f2228e36a80b47d917d1ab6fac466"

    class PackagedAssetDatabase:
        def get_path_from_guid(self, guid):
            assert guid == script_guid
            return str(bytecode)

        def get_guid_from_path(self, path):
            raise AssertionError("packaged component restore must keep the manifest GUID")

    previous_root = get_project_root()
    set_project_root(str(project))
    try:
        instance, resolved_path = create_component_instance(
            script_guid,
            "f" * 32,
            "Controller",
            PackagedAssetDatabase(),
        )

        assert instance is not None
        assert instance._script_guid == script_guid
        assert type(instance).update is not InxComponent.update
        assert resolved_path == str(bytecode)
    finally:
        set_project_root(previous_root)
