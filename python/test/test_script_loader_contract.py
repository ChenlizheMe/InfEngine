from __future__ import annotations

import importlib
import py_compile
import sys
import types

import pytest

from Infernux.components.script_loader import (
    load_and_create_component,
    load_all_components_from_file,
    load_component_class_from_file,
)
import Infernux.components.script_loader as script_loader
from Infernux.components import InxComponent
from Infernux.components.component_identity import bind_asset_script_guid
from Infernux.components.registry import get_type, get_type_by_identity
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


def test_script_loader_can_execute_the_captured_source_snapshot(tmp_path):
    script = tmp_path / "captured_component.py"
    script.write_text("def broken(:\n", encoding="utf-8")
    captured = (
        b"from Infernux.components import InxComponent\n"
        b"class CapturedComponent(InxComponent):\n"
        b"    pass\n"
    )

    loaded = load_all_components_from_file(
        str(script),
        register=False,
        source=captured,
    )

    assert [component.__name__ for component in loaded] == ["CapturedComponent"]


def test_script_loader_uses_frontend_code_without_compiling_again(tmp_path, monkeypatch):
    script = tmp_path / "frontend_code_component.py"
    source = (
        b"from Infernux.components import InxComponent\n"
        b"class FrontendCodeComponent(InxComponent):\n"
        b"    pass\n"
    )
    script.write_bytes(source)
    code = compile(source, str(script), "exec", dont_inherit=True)

    def unexpected_compile(*_args, **_kwargs):
        raise AssertionError("provided frontend code must not be compiled again")

    monkeypatch.setattr(script_loader, "compile", unexpected_compile, raising=False)
    loaded = load_all_components_from_file(
        str(script),
        register=False,
        source=source,
        code=code,
    )

    assert [component.__name__ for component in loaded] == ["FrontendCodeComponent"]
    assert isinstance(code, types.CodeType)


def test_script_loader_custom_frontend_without_code_falls_back_to_source_compile(
    tmp_path, monkeypatch
):
    script = tmp_path / "source_fallback_component.py"
    source = (
        b"from Infernux.components import InxComponent\n"
        b"class SourceFallbackComponent(InxComponent):\n"
        b"    pass\n"
    )
    script.write_bytes(source)
    compile_calls = []
    original_compile = compile

    def track_compile(*args, **kwargs):
        compile_calls.append(args[0])
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(script_loader, "compile", track_compile, raising=False)
    loaded = load_all_components_from_file(
        str(script),
        register=False,
        source=source,
        code=None,
    )

    assert [component.__name__ for component in loaded] == ["SourceFallbackComponent"]
    assert compile_calls == [source]


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


def test_component_lookup_prefers_latest_hot_reloaded_class():
    first = type("HotReloadLookupProbe", (InxComponent,), {
        "__module__": "hot_reload_lookup_probe",
    })
    bind_asset_script_guid(first, "hot-reload-script-guid")
    second = type("HotReloadLookupProbe", (InxComponent,), {
        "__module__": "hot_reload_lookup_probe",
    })
    type_guid = bind_asset_script_guid(second, "hot-reload-script-guid")

    assert get_type("HotReloadLookupProbe") is second
    assert get_type_by_identity(
        "HotReloadLookupProbe",
        "hot-reload-script-guid",
        type_guid,
    ) is second


def test_asset_load_binds_every_component_type_in_one_script(tmp_path):
    project = tmp_path / "project"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    script = assets / "SharedComponents.py"
    script.write_text(
        "from Infernux.components import InxComponent\n"
        "class Target(InxComponent):\n"
        "    pass\n"
        "class Caller(InxComponent):\n"
        "    def requested_guid(self):\n"
        "        return Target._get_type_guid()\n",
        encoding="utf-8",
    )
    script_guid = "same-script-asset-guid"
    previous_root = get_project_root()
    set_project_root(str(project))
    try:
        caller = load_and_create_component(
            str(script),
            type_name="Caller",
            script_guid=script_guid,
        )
        target = get_type("Target")

        assert caller is not None
        assert target is not None
        assert caller.requested_guid() == target._get_type_guid()
        assert caller.requested_guid() == get_type_by_identity(
            "Target",
            script_guid,
            target._get_type_guid(),
        )._get_type_guid()
    finally:
        set_project_root(previous_root)


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
