from __future__ import annotations

import dataclasses
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from Infernux.engine.candidate_import import (
    CandidateImportError,
    CandidateImportTransaction,
)
from Infernux.engine.project_context import (
    get_project_root,
    get_script_import_paths,
    get_script_module_name,
    set_project_root,
)


@pytest.fixture
def candidate_project(tmp_path):
    previous = get_project_root()
    project = tmp_path / "CandidateImportProject"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    set_project_root(str(project))
    try:
        yield assets
    finally:
        set_project_root(previous)


def _broker(assets: Path, name: str, source: str) -> CandidateImportTransaction:
    path = assets / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    broker = CandidateImportTransaction()
    broker.register(name, str(path), source=source)
    return broker


def test_os_is_a_trusted_stdlib_import(candidate_project):
    broker = _broker(
        candidate_project,
        "os_candidate",
        "import os\nVALUE = os.path.join('a', 'b')\n",
    )

    module = broker.load("os_candidate")

    assert module.VALUE == os.path.join("a", "b")
    assert "os_candidate" not in sys.modules
    broker.rollback()


def test_trusted_import_bypasses_project_descendant_scan(candidate_project, monkeypatch):
    broker = _broker(
        candidate_project,
        "trusted_import_root",
        "import Infernux\nVALUE = Infernux.__name__\n",
    )

    def reject_scan(_name):
        raise AssertionError("trusted imports must not scan project LKG descendants")

    monkeypatch.setattr(broker, "_has_lkg_descendant", reject_scan)

    module = broker.load("trusted_import_root")

    assert module.VALUE == "Infernux"
    broker.rollback()


@pytest.mark.parametrize("runtime_library", [
    ".runtime/python313/Lib",
    ".runtime/python313/lib/python3.13",
    ".venv/lib/python3.13",
])
def test_package_candidate_does_not_own_project_private_stdlib(
    candidate_project, monkeypatch, runtime_library,
):
    import pathlib

    project = candidate_project.parent
    installed_stdlib = project / runtime_library / "pathlib" / "__init__.py"
    installed_stdlib.parent.mkdir(parents=True)
    installed_stdlib.touch()
    monkeypatch.setattr(pathlib, "__file__", str(installed_stdlib))
    package = project / "Packages" / "probe"
    runtime = package / "runtime"
    runtime.mkdir(parents=True)
    (package / "inx_package.json").write_text("{}", encoding="utf-8")
    component = runtime / "component.py"
    component.write_text("from pathlib import Path\nVALUE = Path\n", encoding="utf-8")
    name = get_script_module_name(str(component))
    broker = CandidateImportTransaction()
    broker.register(name, str(component))
    try:
        assert broker.load(name).VALUE is pathlib.Path
        assert "pathlib" not in broker.loaded_module_names
        assert "pathlib" not in broker._reused_lkg
    finally:
        broker.rollback()


def test_lowercase_public_engine_namespace_is_lazily_admitted(candidate_project):
    previous = sys.modules.pop("infernux", None)
    try:
        broker = _broker(
            candidate_project,
            "lowercase_engine_candidate",
            "import infernux as inx\nBASE = inx.InxComponent\n",
        )

        module = broker.load("lowercase_engine_candidate")

        from Infernux import InxComponent

        assert module.BASE is InxComponent
        assert sys.modules["infernux"].InxComponent is InxComponent
        assert "lowercase_engine_candidate" not in sys.modules
        broker.rollback()
    finally:
        if previous is None:
            sys.modules.pop("infernux", None)
        else:
            sys.modules["infernux"] = previous


def test_candidate_scc_uses_private_table_until_commit(candidate_project):
    assets = candidate_project
    (assets / "scc_a.py").write_text(
        "VALUE_A = 'a'\n"
        "import scc_b\n"
        "VALUE_FROM_B = scc_b.VALUE_B\n",
        encoding="utf-8",
    )
    (assets / "scc_b.py").write_text(
        "VALUE_B = 'b'\n"
        "import scc_a\n"
        "VALUE_FROM_A = scc_a.VALUE_A\n",
        encoding="utf-8",
    )
    broker = CandidateImportTransaction()
    broker.register("scc_a", str(assets / "scc_a.py"))
    broker.register("scc_b", str(assets / "scc_b.py"))

    module = broker.load("scc_a")

    assert module.VALUE_FROM_B == "b"
    assert broker.module_for("scc_b").VALUE_FROM_A == "a"
    assert "scc_a" not in sys.modules
    assert "scc_b" not in sys.modules

    broker.commit()
    assert sys.modules["scc_a"] is module
    assert sys.modules["scc_b"] is broker.module_for("scc_b")
    broker.rollback()
    assert "scc_a" not in sys.modules
    assert "scc_b" not in sys.modules


def test_helper_import_and_dataclass_string_annotation_are_supported(candidate_project):
    assets = candidate_project
    (assets / "candidate_helper.py").write_text(
        "HELPER_VALUE = 7\n",
        encoding="utf-8",
    )
    source = (
        "from dataclasses import dataclass\n"
        "from candidate_helper import HELPER_VALUE\n"
        "@dataclass\n"
        "class CandidateData:\n"
        "    value: 'int' = HELPER_VALUE\n"
    )
    broker = _broker(assets, "candidate_root", source)
    broker.register("candidate_helper", str(assets / "candidate_helper.py"))
    module = broker.load("candidate_root")

    assert dataclasses.is_dataclass(module.CandidateData)
    assert module.CandidateData.__annotations__ == {"value": "int"}
    assert module.CandidateData().value == 7
    assert "candidate_root" not in sys.modules
    assert "candidate_helper" not in sys.modules
    broker.rollback()


def test_future_annotations_are_supported_without_project_dependency(candidate_project):
    source = (
        "from __future__ import annotations\n"
        "class FutureAnnotated:\n"
        "    value: MissingUntilRuntime\n"
    )
    broker = _broker(candidate_project, "future_annotations_candidate", source)

    module = broker.load("future_annotations_candidate")

    assert module.FutureAnnotated.__annotations__ == {"value": "MissingUntilRuntime"}
    assert "__future__" not in broker.loaded_module_names
    broker.rollback()


def test_package_fromlist_and_relative_import_attach_private_submodule(candidate_project):
    package = candidate_project / "package_helper"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from .helper import PACKAGE_VALUE\n",
        encoding="utf-8",
    )
    (package / "helper.py").write_text(
        "PACKAGE_VALUE = 'package-ok'\n",
        encoding="utf-8",
    )
    root = candidate_project / "package_root.py"
    root.write_text(
        "from package_helper import helper\n"
        "from package_helper import PACKAGE_VALUE\n",
        encoding="utf-8",
    )
    broker = CandidateImportTransaction()
    broker.register("package_root", str(root))
    broker.register("package_helper", str(package / "__init__.py"))
    broker.register("package_helper.helper", str(package / "helper.py"))

    module = broker.load("package_root")

    package_module = broker.module_for("package_helper")
    helper_module = broker.module_for("package_helper.helper")
    assert module.helper is helper_module
    assert module.PACKAGE_VALUE == "package-ok"
    assert package_module.helper is helper_module
    assert "package_helper" not in sys.modules
    assert "package_helper.helper" not in sys.modules
    broker.rollback()


def test_installed_package_runtime_relative_import_uses_isolated_namespace(
    candidate_project,
):
    project = candidate_project.parent
    runtime = project / "Packages" / "studio" / "ai-tools" / "runtime"
    runtime.mkdir(parents=True)
    (runtime.parent / "inx_package.json").write_text("{}", encoding="utf-8")
    helper = runtime / "helper.py"
    component = runtime / "component.py"
    helper.write_text("VALUE = 'package-runtime'\n", encoding="utf-8")
    component.write_text("from .helper import VALUE\n", encoding="utf-8")
    helper_name = get_script_module_name(str(helper))
    component_name = get_script_module_name(str(component))
    broker = CandidateImportTransaction()
    broker.register(helper_name, str(helper))
    broker.register(component_name, str(component))

    module = broker.load(component_name)

    assert module.VALUE == "package-runtime"
    assert component_name == (
        "_infernux_packages.studio.ai_2dtools.runtime.component"
    )
    assert get_script_import_paths(str(component))[:2] == [
        str(runtime.resolve()),
        str(runtime.parent.resolve()),
    ]
    assert helper_name not in sys.modules
    broker.rollback()


def test_legacy_package_editor_relative_import_keeps_canonical_identity(
    candidate_project,
):
    project = candidate_project.parent
    package = project / "Packages" / "infernux" / "platform-web"
    editor = package / "Editor" / "infernux_web"
    editor.mkdir(parents=True)
    (package / "InxPackage.json").write_text("{}", encoding="utf-8")
    helper = editor / "exporter.py"
    lifecycle = editor / "lifecycle.py"
    helper.write_text("VALUE = 'legacy-editor-ok'\n", encoding="utf-8")
    lifecycle.write_text("from .exporter import VALUE\n", encoding="utf-8")

    helper_name = get_script_module_name(str(helper))
    lifecycle_name = get_script_module_name(str(lifecycle))
    assert helper_name == "_infernux_packages.infernux.platform_2dweb.editor.infernux_web.exporter"
    assert lifecycle_name == "_infernux_packages.infernux.platform_2dweb.editor.infernux_web.lifecycle"
    assert get_script_import_paths(str(lifecycle))[:2] == [
        str((package / "Editor").resolve()),
        str(package.resolve()),
    ]

    broker = CandidateImportTransaction()
    broker.register(helper_name, str(helper))
    broker.register(lifecycle_name, str(lifecycle))
    module = broker.load(lifecycle_name)

    assert module.VALUE == "legacy-editor-ok"
    broker.rollback()


def test_preloaded_package_namespace_with_encoded_underscore_is_reused(
    candidate_project,
):
    project = candidate_project.parent
    package = project / "Packages" / "infernux" / "multiplatform_probe"
    runtime = package / "runtime"
    runtime.mkdir(parents=True)
    (package / "inx_package.json").write_text("{}", encoding="utf-8")
    lifecycle = runtime / "lifecycle.py"
    component = runtime / "component.py"
    lifecycle.write_text("VALUE = 'preloaded'\n", encoding="utf-8")
    component.write_text("VALUE = 'candidate'\n", encoding="utf-8")
    lifecycle_name = get_script_module_name(str(lifecycle))
    component_name = get_script_module_name(str(component))
    assert lifecycle_name is not None and component_name is not None

    created = []
    directory = lifecycle.parent
    parent_names = [
        ".".join(lifecycle_name.split(".")[:index])
        for index in range(1, len(lifecycle_name.split(".")))
    ]
    directories = {}
    for parent_name in reversed(parent_names):
        directories[parent_name] = directory
        directory = directory.parent
    previous = {name: sys.modules.get(name) for name in parent_names}
    try:
        for parent_name in parent_names:
            namespace = type(sys)(parent_name)
            namespace.__path__ = [str(directories[parent_name])]
            namespace.__file__ = str(directories[parent_name] / "__init__.py")
            namespace.__spec__ = importlib.util.spec_from_loader(
                parent_name, loader=None, is_package=True
            )
            sys.modules[parent_name] = namespace
            created.append(parent_name)

        broker = CandidateImportTransaction()
        broker.register(component_name, str(component))
        loaded = broker.load(component_name)

        assert loaded.VALUE == "candidate"
        assert broker.module_for(component_name) is loaded
        broker.rollback()
    finally:
        for parent_name in reversed(created):
            if previous[parent_name] is None:
                sys.modules.pop(parent_name, None)
            else:
                sys.modules[parent_name] = previous[parent_name]


def test_namespace_package_can_load_a_private_child(candidate_project):
    namespace = candidate_project / "namespace_pkg"
    namespace.mkdir()
    (namespace / "helper.py").write_text(
        "VALUE = 'namespace-ok'\n",
        encoding="utf-8",
    )
    root = candidate_project / "namespace_root.py"
    root.write_text(
        "from namespace_pkg import helper\n"
        "VALUE = helper.VALUE\n",
        encoding="utf-8",
    )
    broker = CandidateImportTransaction()
    broker.register("namespace_root", str(root))
    broker.register("namespace_pkg.helper", str(namespace / "helper.py"))

    module = broker.load("namespace_root")

    assert module.VALUE == "namespace-ok"
    assert broker.module_for("namespace_pkg").helper is broker.module_for(
        "namespace_pkg.helper"
    )
    assert "namespace_pkg" not in sys.modules
    broker.rollback()


def test_unregistered_preloaded_project_helper_is_reused_without_republication(candidate_project):
    helper_path = candidate_project / "preloaded_helper.py"
    helper_path.write_text("VALUE = 'lkg'\n", encoding="utf-8")
    helper = type(sys)("preloaded_helper")
    helper.__file__ = str(helper_path)
    helper.VALUE = "lkg"
    previous = sys.modules.get("preloaded_helper")
    sys.modules["preloaded_helper"] = helper
    root = candidate_project / "lkg_root.py"
    root.write_text(
        "import preloaded_helper\n"
        "VALUE = preloaded_helper.VALUE\n",
        encoding="utf-8",
    )
    broker = _broker(candidate_project, "lkg_root", root.read_text(encoding="utf-8"))

    module = broker.load("lkg_root")

    assert module.VALUE == "lkg"
    assert broker.module_for("preloaded_helper") is None
    broker.commit()
    assert sys.modules["preloaded_helper"] is helper
    broker.rollback()
    if previous is None:
        sys.modules.pop("preloaded_helper", None)
    else:
        sys.modules["preloaded_helper"] = previous


def test_nested_package_lkg_accepts_any_valid_project_import_root(candidate_project):
    package = candidate_project / "nested_pkg"
    package.mkdir()
    helper_path = package / "helper.py"
    helper_path.write_text("VALUE = 'nested-lkg'\n", encoding="utf-8")
    helper = type(sys)("nested_pkg.helper")
    helper.__file__ = str(helper_path)
    helper.VALUE = "nested-lkg"
    previous = sys.modules.get("nested_pkg.helper")
    sys.modules["nested_pkg.helper"] = helper
    root = candidate_project / "nested_root.py"
    root.write_text(
        "from nested_pkg import helper\n"
        "VALUE = helper.VALUE\n",
        encoding="utf-8",
    )
    broker = _broker(candidate_project, "nested_root", root.read_text(encoding="utf-8"))

    module = broker.load("nested_root")

    assert module.VALUE == "nested-lkg"
    assert broker.module_for("nested_pkg.helper") is None
    broker.rollback()
    if previous is None:
        sys.modules.pop("nested_pkg.helper", None)
    else:
        sys.modules["nested_pkg.helper"] = previous


def test_candidate_child_does_not_mutate_live_parent_package_before_commit(
    candidate_project,
):
    package = candidate_project / "live_parent_pkg"
    package.mkdir()
    package_init = package / "__init__.py"
    child_path = package / "child.py"
    package_init.write_text("MARKER = 'lkg'\n", encoding="utf-8")
    child_path.write_text("VALUE = 'candidate'\n", encoding="utf-8")

    live_parent = type(sys)("live_parent_pkg")
    live_parent.__file__ = str(package_init)
    live_parent.__path__ = [str(package)]
    sentinel = object()
    live_parent.child = sentinel
    previous_parent = sys.modules.get("live_parent_pkg")
    previous_child = sys.modules.get("live_parent_pkg.child")
    sys.modules["live_parent_pkg"] = live_parent
    sys.modules.pop("live_parent_pkg.child", None)

    root_path = candidate_project / "live_parent_root.py"
    root_path.write_text(
        "from live_parent_pkg import child\nVALUE = child.VALUE\n",
        encoding="utf-8",
    )
    broker = CandidateImportTransaction()
    broker.register("live_parent_root", str(root_path))
    broker.register("live_parent_pkg.child", str(child_path))

    try:
        root = broker.load("live_parent_root")
        candidate_child = broker.module_for("live_parent_pkg.child")
        assert root.VALUE == "candidate"
        assert live_parent.child is sentinel
        assert "live_parent_pkg.child" not in sys.modules

        broker.commit()
        assert sys.modules["live_parent_pkg"] is live_parent
        assert sys.modules["live_parent_pkg.child"] is candidate_child
        assert live_parent.child is candidate_child

        broker.rollback()
        assert live_parent.child is sentinel
        assert "live_parent_pkg.child" not in sys.modules
    finally:
        if previous_parent is None:
            sys.modules.pop("live_parent_pkg", None)
        else:
            sys.modules["live_parent_pkg"] = previous_parent
        if previous_child is None:
            sys.modules.pop("live_parent_pkg.child", None)
        else:
            sys.modules["live_parent_pkg.child"] = previous_child


def test_unregistered_project_helper_without_lkg_is_rejected(candidate_project):
    root = candidate_project / "missing_helper_root.py"
    root.write_text(
        "import missing_project_helper\n",
        encoding="utf-8",
    )
    broker = _broker(
        candidate_project,
        "missing_helper_root",
        root.read_text(encoding="utf-8"),
    )

    with pytest.raises(CandidateImportError, match="not registered"):
        broker.load("missing_helper_root")
    assert "missing_helper_root" not in sys.modules
    broker.rollback()


def test_numpy_is_admitted_for_value_declarations_when_available(candidate_project):
    numpy = pytest.importorskip("numpy")
    source = (
        "import numpy as np\n"
        "DEFAULT = np.zeros(3, dtype=np.float32)\n"
    )
    broker = _broker(candidate_project, "numpy_candidate", source)
    module = broker.load("numpy_candidate")

    assert module.DEFAULT.shape == (3,)
    assert module.DEFAULT.dtype == numpy.float32
    assert "numpy_candidate" not in sys.modules


def test_public_jit_module_is_lazily_admitted_for_declaration_decorators(
    candidate_project,
):
    source = (
        "from Infernux.jit import njit\n"
        "@njit(auto_parallel=True)\n"
        "def scale(values):\n"
        "    for index in range(len(values)):\n"
        "        values[index] *= 2\n"
    )
    broker = _broker(candidate_project, "jit_candidate", source)

    module = broker.load("jit_candidate")

    assert module.scale.auto_parallel is True
    assert module.scale.py is not None
    assert "jit_candidate" not in sys.modules


def test_missing_general_engine_submodule_is_not_lazily_imported(candidate_project):
    source = "import Infernux.not_a_public_candidate_capability\n"
    broker = _broker(candidate_project, "unknown_engine_candidate", source)

    with pytest.raises(
        CandidateImportError,
        match="trusted candidate import is not preloaded",
    ):
        broker.load("unknown_engine_candidate")
    broker.rollback()


def test_unknown_import_is_fail_closed(candidate_project):
    broker = _broker(
        candidate_project,
        "unknown_import_candidate",
        "import definitely_not_a_project_dependency\n",
    )

    with pytest.raises(CandidateImportError, match="candidate import rejected"):
        broker.load("unknown_import_candidate")
    assert "unknown_import_candidate" not in sys.modules
    broker.rollback()


def test_execution_failure_restores_existing_module_entry(candidate_project):
    sentinel = object()
    previous = sys.modules.get("failure_candidate", sentinel)
    sys.modules["failure_candidate"] = sentinel
    broker = _broker(
        candidate_project,
        "failure_candidate",
        "raise RuntimeError('candidate execution failed')\n",
    )

    with pytest.raises(RuntimeError, match="candidate execution failed"):
        broker.load("failure_candidate")
    broker.rollback()

    if previous is sentinel:
        sys.modules.pop("failure_candidate", None)
    else:
        sys.modules["failure_candidate"] = previous
