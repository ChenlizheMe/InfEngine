from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path
import re
import subprocess
import sys

import Infernux
import infernux as inx


_FORBIDDEN_PUBLIC_IMPORTS = (
    "from Infernux import ",
    "from Infernux.",
    "import Infernux",
    "from infernux import *",
)


def _owned_public_documents(repository: Path) -> list[Path]:
    paths = {
        repository / name
        for name in (
            "README.md",
            "README-zh.md",
            "UpdateLog.md",
            "UpdateLog-zh.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "SUPPORT.md",
            "scripts/README.md",
            "docs/tools/README.md",
        )
    }
    paths.update((repository / "docs" / "learn").glob("*.md"))
    paths.update((repository / "docs" / "learn").glob("*.html"))
    paths.update((repository / "docs" / "wiki" / "docs").rglob("*.md"))
    paths.add(repository / "docs" / "tools" / "apply-api-curation.mjs")
    paths.update((repository / "external" / "plugins").glob("*/README*.md"))
    paths.update((repository / "external" / "plugins").glob("*/package/plugin_pages/*.md"))
    paths.update((repository / "tests" / "fixtures").glob("**/README*.md"))
    return sorted(path for path in paths if path.is_file())


def _owned_public_fixture_scripts(repository: Path) -> list[Path]:
    return sorted(
        (repository / "tests" / "fixtures").glob("**/Assets/Scripts/*.py")
    )


def _forbidden_import_violations(
    repository: Path, paths: list[Path]
) -> list[str]:
    violations: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for marker in _FORBIDDEN_PUBLIC_IMPORTS:
            if marker in text:
                violations.append(f"{path.relative_to(repository)}: {marker}")
    return violations


def test_lowercase_namespace_exposes_gameplay_api() -> None:
    assert inx.__version__
    assert inx.InxComponent.__module__.startswith("Infernux.")
    assert inx.GameObject.__module__.startswith("Infernux.")
    assert inx.Vector3.__module__.startswith("Infernux.")
    assert inx.InxComponent is Infernux.InxComponent
    assert inx.GameObject is Infernux.GameObject


def test_lowercase_namespace_lazily_forwards_subsystems() -> None:
    for name in (
        "components",
        "core",
        "input",
        "lifecycle",
        "physics",
        "rendergraph",
        "renderstack",
        "resources",
        "scene",
        "ui",
    ):
        assert name in inx.__all__
    assert inx.input.__name__ == "Infernux.input"
    assert inx.lifecycle.__name__ == "Infernux.lifecycle"
    assert inx.physics.__name__ == "Infernux.physics"
    assert inx.renderstack.__name__ == "Infernux.renderstack"
    assert inx.resources.__name__ == "Infernux.resources"
    assert callable(inx.renderstack.discovery_import_failures)


def test_runtime_ui_public_import_does_not_load_editor_theme() -> None:
    repository = Path(__file__).parents[2]
    python_root = repository / "python"
    code = r'''
import builtins
import sys

original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "Infernux.engine.ui.theme":
        raise AssertionError("runtime UI imported editor theme")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import infernux as inx

assert inx.ui.UIText().font_size == 18.0
assert inx.ui.UIButton().background_color == [0.922, 0.341, 0.341, 1.0]
assert "Infernux.engine.ui.theme" not in sys.modules
'''
    env = os.environ.copy()
    previous_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(python_root), previous_pythonpath) if value
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_lowercase_namespace_exports_lifecycle_with_shared_identity() -> None:
    assert inx.InxPreload is Infernux.InxPreload
    assert inx.PreloadContext is Infernux.PreloadContext


def test_lowercase_namespace_export_record_is_unique_and_complete() -> None:
    assert len(inx.__all__) == len(set(inx.__all__))
    assert set(Infernux.__all__).issubset(inx.__all__)
    for name in inx.__all__:
        assert hasattr(inx, name), name


def test_lowercase_type_stub_explicitly_covers_runtime_exports() -> None:
    stub = Path(__file__).parents[1] / "infernux.pyi"
    tree = ast.parse(stub.read_text(encoding="utf-8"), filename=str(stub))
    explicit: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        assert all(alias.name != "*" for alias in node.names)
        if node.module == "Infernux":
            explicit.update(alias.asname or alias.name for alias in node.names)
    assert set(inx.__all__) <= explicit


def test_lowercase_namespace_reload_preserves_runtime_type_identity() -> None:
    component_type = inx.InxComponent
    reloaded = importlib.reload(inx)
    assert reloaded.InxComponent is component_type
    assert reloaded.InxComponent is Infernux.InxComponent


def test_public_gameplay_docs_keep_the_lowercase_namespace_contract() -> None:
    repository = Path(__file__).parents[2]
    gameplay_docs = sorted((repository / "docs" / "learn").glob("gameplay-*.md"))
    assert gameplay_docs

    violations = _forbidden_import_violations(repository, gameplay_docs)
    assert not violations, "\n".join(violations)


def test_public_docs_never_import_the_internal_package() -> None:
    repository = Path(__file__).parents[2]
    public_docs = _owned_public_documents(repository)
    assert len(public_docs) >= 50
    violations = _forbidden_import_violations(repository, public_docs)
    assert not violations, "\n".join(violations)


def test_curated_wiki_examples_reference_existing_public_api() -> None:
    repository = Path(__file__).parents[2]
    checked = 0
    for path in (repository / "docs/wiki/docs").glob("*/api/*.md"):
        text = path.read_text(encoding="utf-8")
        for source in re.findall(r"```python\s*\n(.*?)```", text, flags=re.DOTALL):
            if "import infernux as inx" not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            compile(tree, str(path), "exec")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                attributes = []
                root = node
                while isinstance(root, ast.Attribute):
                    attributes.append(root.attr)
                    root = root.value
                if not isinstance(root, ast.Name) or root.id != "inx":
                    continue
                value = inx
                for attribute in reversed(attributes):
                    assert hasattr(value, attribute), (path, ast.unparse(node))
                    value = getattr(value, attribute)
            checked += 1
    assert checked >= 28


def test_public_fixture_scripts_use_the_lowercase_namespace() -> None:
    repository = Path(__file__).parents[2]
    scripts = _owned_public_fixture_scripts(repository)
    assert scripts
    violations = _forbidden_import_violations(repository, scripts)
    assert not violations, "\n".join(violations)
    for path in scripts:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
        assert "import infernux as inx" in source, path.relative_to(repository)
