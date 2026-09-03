from __future__ import annotations

import threading
from pathlib import Path

import pytest

from Infernux.engine.script_dependency_graph import (
    DependencyGraphRollbackError,
    DependencyKind,
    ModuleIdentityError,
    ScriptDependencyGraphError,
    ScriptDependencyGraph,
    StaleDependencyGraphTransaction,
)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "Project"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    return project, assets


def _write(assets: Path, relative: str, source: str) -> Path:
    path = assets / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_assets_paths_map_to_modules_and_packages(tmp_path):
    project, assets = _project(tmp_path)
    root = _write(assets, "main.py", "import pkg.worker\n")
    init = _write(assets, "pkg/__init__.py", "from .worker import Worker\n")
    worker = _write(assets, "pkg/worker.py", "class Worker: pass\n")

    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    assert graph.module_for_path(root).id.module_name == "main"
    assert graph.module_for_path(init).is_package is True
    assert graph.module_for_path(init).id.module_name == "pkg"
    assert graph.module_for_path(worker).id.module_name == "pkg.worker"


def test_package_runtime_is_namespaced_and_editor_is_excluded(tmp_path):
    project, _assets = _project(tmp_path)
    package = project / "Packages" / "studio" / "vfx-kit"
    runtime = package / "Runtime"
    editor = package / "Editor"
    runtime.mkdir(parents=True)
    editor.mkdir()
    (package / "InxPackage.json").write_text("{}", encoding="utf-8")
    helper = _write(runtime, "helpers/value.py", "VALUE = 1\n")
    component = _write(
        runtime,
        "helpers/component.py",
        "from .value import VALUE\n",
    )
    panel = _write(editor, "panel.py", "VALUE = 2\n")

    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    assert graph.module_for_path(helper).id.module_name == (
        "_infernux_packages.studio.vfx_2dkit.runtime.helpers.value"
    )
    assert {
        edge.target.module_name
        for edge in graph.dependencies_of(component)
        if edge.target is not None
    } == {
        "_infernux_packages.studio.vfx_2dkit.runtime.helpers.value"
    }
    assert graph.module_for_path(panel) is None


def test_index_ignores_pyc_when_source_is_present(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "worker.py", "value = 1\n")
    bytecode = assets / "worker.pyc"
    bytecode.write_bytes(b"not executable bytecode")

    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    assert [module.source_path for module in graph.snapshot().modules] == [str(source.resolve())]
    assert graph.module_for_name("worker").source_path == str(source.resolve())
    assert graph.module_for_path(bytecode) is None


def test_incremental_graph_operations_reject_pyc(tmp_path):
    project, assets = _project(tmp_path)
    graph = ScriptDependencyGraph(project)
    bytecode = assets / "worker.pyc"
    bytecode.write_bytes(b"not executable bytecode")

    with pytest.raises(ModuleIdentityError):
        graph.upsert(bytecode)


def test_absolute_and_relative_imports_resolve_project_edges(tmp_path):
    project, assets = _project(tmp_path)
    main = _write(assets, "main.py", "import pkg.worker\nfrom helper import value\n")
    _write(assets, "pkg/__init__.py", "from .worker import Worker\n")
    worker = _write(assets, "pkg/worker.py", "from .common import value\n")
    common = _write(assets, "pkg/common.py", "value = 1\n")
    helper = _write(assets, "helper.py", "value = 2\n")

    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    main_edges = graph.dependencies_of(main)
    worker_edges = graph.dependencies_of(worker)
    assert {edge.target.module_name for edge in main_edges if edge.target} == {"pkg", "pkg.worker", "helper"}
    assert {edge.target.module_name for edge in worker_edges if edge.target} == {"pkg", "pkg.common"}
    assert all(edge.kind is DependencyKind.PROJECT for edge in main_edges + worker_edges)
    assert graph.resolve_module(
        "common",
        from_module=graph.module_for_path(worker).id,
        level=1,
    ) is not None
    assert graph.resolve_module("pkg.common") is not None


def test_from_package_import_records_package_and_submodule(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "consumer.py", "from pkg import helper\n")
    _write(assets, "pkg/__init__.py", "value = 1\n")
    helper = _write(assets, "pkg/helper.py", "value = 2\n")

    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    edges = graph.dependencies_of(source)
    assert {edge.target.module_name for edge in edges if edge.target} == {"pkg", "pkg.helper"}
    assert graph.module_for_path(helper).id.module_name == "pkg.helper"


def test_external_dependencies_are_recorded_but_not_indexed(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(
        assets,
        "main.py",
        "import json\nimport Infernux\nimport numpy\nfrom .missing import value\n",
    )
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    edges = graph.external_dependencies(source)
    by_name = {edge.external_name: edge for edge in edges}
    assert by_name["json"].external_origin == "stdlib"
    assert by_name["Infernux"].external_origin == "engine"
    assert by_name["numpy"].external_origin == "third_party"
    unresolved = [edge for edge in edges if edge.kind is DependencyKind.UNRESOLVED]
    assert unresolved and unresolved[0].external_name == ""
    assert graph.module_for_name("numpy") is None


def test_dynamic_imports_are_explicit_and_can_target_project_modules(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "main.py", "import importlib\nimportlib.import_module('helper')\n__import__('third_party')\n")
    helper = _write(assets, "helper.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    dynamic = [edge for edge in graph.dependencies_of(source) if edge.dynamic]
    assert any(edge.target and edge.target.module_name == "helper" for edge in dynamic)
    assert any(edge.external_name == "third_party" and edge.kind is DependencyKind.DYNAMIC for edge in dynamic)
    assert graph.module_for_path(helper) is not None


def test_reverse_affected_closure_includes_transitive_dependents(tmp_path):
    project, assets = _project(tmp_path)
    leaf = _write(assets, "leaf.py", "value = 1\n")
    middle = _write(assets, "middle.py", "from leaf import value\n")
    root = _write(assets, "root.py", "from middle import value\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    names = {module.module_name for module in graph.affected_closure(leaf)}
    assert names == {"leaf", "middle", "root"}
    assert {module.module_name for module in graph.dependents_of(leaf)} == {"middle"}


def test_tarjan_groups_cycles_and_keeps_acyclic_nodes_separate(tmp_path):
    project, assets = _project(tmp_path)
    _write(assets, "a.py", "import b\n")
    _write(assets, "b.py", "from c import value\n")
    _write(assets, "c.py", "import a\nvalue = 1\n")
    _write(assets, "leaf.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    components = [{module.module_name for module in component} for component in graph.strongly_connected_components()]
    assert {"a", "b", "c"} in components
    assert {"leaf"} in components


def test_upsert_refreshes_an_importer_when_a_missing_module_appears(tmp_path):
    project, assets = _project(tmp_path)
    consumer = _write(assets, "consumer.py", "import later\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    assert any(edge.kind is DependencyKind.EXTERNAL for edge in graph.dependencies_of(consumer))

    later = _write(assets, "later.py", "value = 3\n")
    mutation = graph.upsert(later)
    assert {item.module_name for item in mutation.affected} == {"consumer", "later"}
    assert graph.dependencies_of(consumer)[0].kind is DependencyKind.PROJECT


def test_remove_returns_old_identity_and_affected_dependents(tmp_path):
    project, assets = _project(tmp_path)
    leaf = _write(assets, "leaf.py", "value = 1\n")
    consumer = _write(assets, "consumer.py", "import leaf\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    mutation = graph.remove(leaf)
    assert mutation.removed[0].module_name == "leaf"
    assert {item.module_name for item in mutation.affected} == {"leaf", "consumer"}
    assert graph.module_for_name("leaf") is None
    assert graph.dependencies_of(consumer)[0].kind is DependencyKind.EXTERNAL


def test_rename_is_remove_then_upsert_with_stable_new_identity(tmp_path):
    project, assets = _project(tmp_path)
    old = _write(assets, "old_name.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    old_id = graph.module_for_path(old).id
    graph.remove(old)
    new = assets / "new_name.py"
    new.write_text("value = 1\n", encoding="utf-8")
    graph.upsert(new)
    assert graph.module_for_path(new).id.module_name == "new_name"
    assert graph.module_for_path(new).id != old_id


def test_package_module_collision_is_rejected_without_partial_mutation(tmp_path):
    project, assets = _project(tmp_path)
    module = _write(assets, "pkg.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    package = _write(assets, "pkg/__init__.py", "value = 2\n")

    with pytest.raises(ModuleIdentityError):
        graph.upsert(package)
    assert graph.module_for_path(module) is not None
    assert graph.module_for_path(package) is None


def test_public_snapshots_are_immutable_tuples_and_frozen_records(tmp_path):
    project, assets = _project(tmp_path)
    _write(assets, "main.py", "import helper\n")
    _write(assets, "helper.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    snapshot = graph.snapshot()

    assert isinstance(snapshot.modules, tuple)
    assert isinstance(snapshot.edges, tuple)
    with pytest.raises((AttributeError, TypeError)):
        snapshot.revision = 99


def test_index_and_reads_are_safe_under_concurrent_access(tmp_path):
    project, assets = _project(tmp_path)
    for index in range(12):
        _write(assets, f"module_{index}.py", f"value = {index}\n")
    graph = ScriptDependencyGraph(project)
    failures: list[BaseException] = []

    def reader() -> None:
        try:
            for _ in range(80):
                graph.snapshot()
                graph.strongly_connected_components()
                graph.external_dependencies()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    graph.index_assets()
    for thread in threads:
        thread.join()
    assert failures == []
    assert len(graph.snapshot().modules) == 12


def test_paths_outside_assets_are_rejected(tmp_path):
    project, assets = _project(tmp_path)
    graph = ScriptDependencyGraph(project)
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(ModuleIdentityError):
        graph.upsert(outside)


def test_path_aliases_use_the_same_module_identity(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "nested/main.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    alias = assets / "nested" / ".." / "nested" / "main.py"
    assert graph.module_for_path(alias).id == graph.module_for_path(source).id


def test_staged_batch_is_side_effect_free_until_atomic_commit(tmp_path):
    project, assets = _project(tmp_path)
    first = _write(assets, "first.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    before = graph.snapshot()
    before_revision = graph.revision
    second = assets / "second.py"

    stage = graph.stage_transaction(
        [(first, "value = 2\n"), (second, "from first import value\n")]
    )

    assert graph.revision == before_revision
    assert graph.snapshot() == before
    assert stage.base_revision == before_revision
    assert stage.revision == before_revision + 1
    assert {item.module_name for item in stage.upserts} == {"first", "second"}
    with pytest.raises(AttributeError):
        stage.base_revision = 99

    mutation = graph.commit_transaction(stage)
    assert mutation == stage.mutation
    assert graph.revision == before_revision + 1
    assert graph.module_for_path(second).source_hash != ""
    assert graph.dependencies_of(second)[0].kind is DependencyKind.PROJECT


def test_staged_module_collision_does_not_change_live_graph(tmp_path):
    project, assets = _project(tmp_path)
    module = _write(assets, "pkg.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    before = graph.snapshot()
    package = _write(assets, "pkg/__init__.py", "value = 2\n")

    with pytest.raises(ModuleIdentityError):
        graph.stage_transaction([package])

    assert graph.snapshot() == before
    assert graph.module_for_path(module) is not None
    assert graph.module_for_path(package) is None


def test_stale_staged_transaction_cannot_partially_publish(tmp_path):
    project, assets = _project(tmp_path)
    first = _write(assets, "first.py", "value = 1\n")
    second = _write(assets, "second.py", "value = 2\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    stage = graph.stage_transaction([(first, "value = 3\n")])
    graph.upsert(second, "value = 4\n")
    before = graph.snapshot()

    with pytest.raises(StaleDependencyGraphTransaction):
        graph.commit_transaction(stage)

    assert graph.snapshot() == before
    assert graph.module_for_path(first).source_hash == before.modules[0].source_hash
    assert graph.module_for_path(second).source_hash != ""


def test_staged_remove_then_upsert_supports_one_transaction_move(tmp_path):
    project, assets = _project(tmp_path)
    old = _write(assets, "old_name.py", "value = 1\n")
    consumer = _write(assets, "consumer.py", "import old_name\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    old_id = graph.module_for_path(old).id
    new = assets / "new_name.py"

    stage = graph.stage_transaction(
        [(new, "value = 1\n")],
        removals=[old],
    )
    assert {item.module_name for item in stage.removals} == {"old_name"}
    assert {item.module_name for item in stage.upserts} == {"new_name"}
    graph.commit_transaction(stage)

    assert graph.module_for_path(old) is None
    assert graph.module_for_path(new).id.module_name == "new_name"
    assert graph.module_for_path(new).id != old_id
    assert graph.dependencies_of(consumer)[0].kind is DependencyKind.EXTERNAL
    assert {item.module_name for item in stage.affected} == {
        "old_name",
        "new_name",
        "consumer",
    }


def test_staged_multiple_upserts_rebuild_edges_and_affected_closure(tmp_path):
    project, assets = _project(tmp_path)
    root = _write(assets, "root.py", "import middle\n")
    middle = _write(assets, "middle.py", "import leaf\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    leaf = assets / "leaf.py"
    extra = assets / "extra.py"

    stage = graph.stage_transaction(
        {
            leaf: "value = 1\n",
            extra: "import leaf\n",
        }
    )
    assert graph.module_for_path(leaf) is None
    graph.commit_transaction(stage)

    assert graph.dependencies_of(middle)[0].target.module_name == "leaf"
    assert graph.dependencies_of(extra)[0].target.module_name == "leaf"
    assert {item.module_name for item in stage.affected} == {
        "leaf",
        "middle",
        "root",
        "extra",
    }


def test_staged_dependency_order_places_dependencies_before_dependents(tmp_path):
    project, assets = _project(tmp_path)
    a = _write(assets, "a.py", "value = 1\n")
    _write(assets, "b.py", "import a\n")
    _write(assets, "c.py", "import b\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    stage = graph.stage_transaction([(a, "value = 2\n")])

    assert [
        tuple(module.module_name for module in batch)
        for batch in stage.dependency_batches
    ] == [("a",), ("b",), ("c",)]
    assert tuple(module.module_name for module in stage.ordered_modules) == (
        "a",
        "b",
        "c",
    )


def test_staged_cycle_is_one_stable_dependency_batch(tmp_path):
    project, assets = _project(tmp_path)
    a = _write(assets, "a.py", "import b\n")
    _write(assets, "b.py", "import a\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    stage = graph.stage_transaction([(a, "import b\nvalue = 2\n")])

    assert [
        tuple(module.module_name for module in batch)
        for batch in stage.strongly_connected_components
    ] == [("a", "b")]
    assert stage.strongly_connected_components == stage.dependency_batches


def test_staged_order_uses_candidate_edges_for_addition_and_removal(tmp_path):
    project, assets = _project(tmp_path)
    user = _write(assets, "a_user.py", "value = 1\n")
    dependency = _write(assets, "z_dependency.py", "value = 2\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()

    add_edge = graph.stage_transaction(
        [
            (user, "import z_dependency\n"),
            (dependency, "value = 3\n"),
        ]
    )
    assert tuple(module.module_name for module in add_edge.ordered_modules) == (
        "z_dependency",
        "a_user",
    )
    graph.commit_transaction(add_edge)

    remove_edge = graph.stage_transaction(
        [
            (user, "value = 4\n"),
            (dependency, "value = 5\n"),
        ]
    )
    assert tuple(module.module_name for module in remove_edge.ordered_modules) == (
        "a_user",
        "z_dependency",
    )


def test_staged_dependency_results_are_immutable_and_do_not_expose_maps(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "source.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    stage = graph.stage_transaction([(source, "value = 2\n")])

    assert isinstance(stage.dependency_batches, tuple)
    assert isinstance(stage.dependency_batches[0], tuple)
    assert isinstance(stage.ordered_modules, tuple)
    assert not hasattr(stage, "modules")
    assert not hasattr(stage, "module_by_name")
    with pytest.raises(AttributeError):
        stage.dependency_batches = ()
    with pytest.raises(TypeError):
        stage.dependency_batches[0][0] = stage.ordered_modules[0]


def test_committed_transaction_can_atomically_rollback_to_base_snapshot(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "source.py", "value = 1\n")
    added = assets / "added.py"
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    before = graph.snapshot()

    stage = graph.stage_transaction(
        [(source, "value = 2\n"), (added, "import source\n")]
    )
    graph.commit_transaction(stage)
    assert graph.snapshot() != before

    mutation = graph.rollback_transaction(stage)

    assert mutation.operation == "rollback"
    assert mutation.revision == stage.base_revision
    assert graph.snapshot() == before
    assert graph.module_for_path(added) is None


def test_rollback_reports_restored_deletion_as_changed_and_candidate_only_as_removed(tmp_path):
    project, assets = _project(tmp_path)
    kept = _write(assets, "kept.py", "value = 1\n")
    deleted = _write(assets, "deleted.py", "value = 2\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    before = graph.snapshot()

    stage = graph.stage_transaction(
        [(kept, "value = 3\n"), (assets / "added.py", "value = 4\n")],
        removals=[deleted],
    )
    graph.commit_transaction(stage)

    mutation = graph.rollback_transaction(stage)

    assert {item.module_name for item in mutation.changed} == {"kept", "deleted"}
    assert {item.module_name for item in mutation.removed} == {"added"}
    assert graph.snapshot() == before


def test_stage_and_reads_are_cross_thread_but_live_mutation_is_owner_thread_only(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "source.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.bind_owner_thread()
    before = graph.snapshot()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            staged = graph.stage_transaction([(source, "value = 2\n")])
            assert graph.snapshot() == before
            graph.commit_transaction(staged)
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert len(errors) == 1
    assert isinstance(errors[0], ScriptDependencyGraphError)
    assert graph.snapshot() == before


def test_every_live_mutation_entrypoint_rejects_non_owner_without_changing_graph(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "source.py", "value = 1\n")
    extra = _write(assets, "extra.py", "value = 2\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    staged = graph.stage_transaction([(source, "value = 3\n")])
    graph.commit_transaction(staged)
    rollback_candidate = graph.stage_transaction([(source, "value = 4\n")])
    before = graph.snapshot()
    errors: list[BaseException] = []

    def worker() -> None:
        operations = (
            graph.index_assets,
            graph.rebuild,
            lambda: graph.upsert(source, "value = 5\n"),
            lambda: graph.remove(extra),
            lambda: graph.commit_transaction(rollback_candidate),
            lambda: graph.rollback_transaction(staged),
        )
        for operation in operations:
            try:
                operation()
            except BaseException as exc:  # pragma: no cover - assertion below
                errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert len(errors) == 6
    assert all(isinstance(error, ScriptDependencyGraphError) for error in errors)
    assert graph.snapshot() == before
    graph.rollback_transaction(staged)


def test_first_live_mutation_binds_owner_and_explicit_bind_rejects_other_thread(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "source.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    stage = graph.stage_transaction([(source, "value = 2\n")])
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            graph.bind_owner_thread()
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert len(errors) == 1
    assert isinstance(errors[0], ScriptDependencyGraphError)
    graph.commit_transaction(stage)


def test_uncommitted_transaction_cannot_rollback_live_graph(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "source.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    before = graph.snapshot()
    stage = graph.stage_transaction([(source, "value = 2\n")])

    with pytest.raises(DependencyGraphRollbackError):
        graph.rollback_transaction(stage)

    assert graph.snapshot() == before


def test_rollback_rejects_transaction_after_a_newer_graph_mutation(tmp_path):
    project, assets = _project(tmp_path)
    source = _write(assets, "source.py", "value = 1\n")
    later = _write(assets, "later.py", "value = 1\n")
    graph = ScriptDependencyGraph(project)
    graph.index_assets()
    stage = graph.stage_transaction([(source, "value = 2\n")])
    graph.commit_transaction(stage)
    graph.upsert(later, "value = 3\n")
    after_later_mutation = graph.snapshot()

    with pytest.raises(DependencyGraphRollbackError):
        graph.rollback_transaction(stage)

    assert graph.snapshot() == after_later_mutation
    assert graph.module_for_path(later).source_hash != ""


def test_rollback_rejects_transaction_owned_by_another_graph(tmp_path):
    first_project = tmp_path / "FirstProject"
    first_assets = first_project / "Assets"
    first_assets.mkdir(parents=True)
    first_source = _write(first_assets, "source.py", "value = 1\n")
    first_graph = ScriptDependencyGraph(first_project)
    first_graph.index_assets()
    stage = first_graph.stage_transaction([(first_source, "value = 2\n")])
    first_graph.commit_transaction(stage)

    second_project = tmp_path / "SecondProject"
    second_assets = second_project / "Assets"
    second_assets.mkdir(parents=True)
    _write(second_assets, "source.py", "value = 7\n")
    second_graph = ScriptDependencyGraph(second_project)
    second_graph.index_assets()
    before = second_graph.snapshot()

    with pytest.raises(DependencyGraphRollbackError):
        second_graph.rollback_transaction(stage)

    assert second_graph.snapshot() == before
