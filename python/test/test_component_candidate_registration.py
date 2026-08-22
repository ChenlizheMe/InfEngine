from __future__ import annotations

import sys
from pathlib import Path

import pytest

from Infernux.components import _cds_bridge
from Infernux.components._component_registration import (
    candidate_component_registration_scope,
    is_component_registration_pending,
)
from Infernux.components.component import InxComponent
from Infernux.components.component_identity import bind_asset_script_guid
from Infernux.components.registry import (
    restore_component_registry_state,
    snapshot_component_registry_state,
)
from Infernux.components.script_loader import (
    ComponentBodyReloadRequest,
    ScriptLoadError,
    ScriptReloadRejected,
    create_component_instance,
    load_all_components_from_file,
    stage_component_body_reload_batch,
)
from Infernux.engine.project_context import get_project_root, set_project_root
import Infernux.engine.runtime_dispatch as runtime_dispatch


class _CDSProbe:
    def __init__(self) -> None:
        self.class_keys: list[str] = []
        self.allocations: list[int] = []
        self.frees: list[tuple[int, tuple[int, int]]] = []
        self._class_ids: dict[str, int] = {}
        self._published: dict[int, dict[str, object]] = {}
        self._transactions: dict[int, dict[str, object]] = {}
        self._next_transaction = 1

    def _cds_register_class(self, key: str) -> int:
        self.class_keys.append(key)
        class_id = self._class_ids.setdefault(key, len(self._class_ids) + 100)
        self._published.setdefault(
            class_id,
            {"fields": {}, "slots": {}, "next_slot": 0},
        )
        return class_id

    def _cds_register_field(self, class_id: int, name: str, type_code: int) -> int:
        fields = self._published[class_id]["fields"]
        field_id = len(fields)
        fields[name] = (field_id, type_code)
        return field_id

    def _cds_alloc(self, class_id: int) -> tuple[int, int]:
        self.allocations.append(class_id)
        data = self._published[class_id]
        slot = (data["next_slot"], 1)
        data["next_slot"] += 1
        data["slots"][slot] = {}
        return slot

    def _cds_is_alive(self, class_id: int, slot: tuple[int, int]) -> bool:
        return slot in self._published.get(class_id, {}).get("slots", {})

    def _cds_free(self, class_id: int, slot: tuple[int, int]) -> None:
        self.frees.append((class_id, slot))
        self._published[class_id]["slots"].pop(tuple(slot), None)

    def _cds_set(
        self,
        class_id: int,
        field_id: int,
        slot,
        _type_code: int,
        value,
    ) -> None:
        self._published[class_id]["slots"][tuple(slot)][field_id] = value

    def _cds_get(self, class_id: int, field_id: int, slot, _type_code: int):
        return self._published[class_id]["slots"][tuple(slot)].get(field_id, 0)

    def _cds_schema_begin(self) -> int:
        transaction_id = self._next_transaction
        self._next_transaction += 1
        self._transactions[transaction_id] = {
            "classes": {},
            "active": True,
            "sealed": {},
            "finalized": False,
        }
        return transaction_id

    def _cds_schema_prepare_class(self, transaction_id: int, key: str) -> int:
        classes = self._transactions[transaction_id]["classes"]
        candidate_id = len(classes) + 1
        classes[candidate_id] = {
            "key": key,
            "fields": {},
            "slots": {},
            "next_slot": 0,
        }
        return candidate_id

    def _cds_schema_prepare_field(
        self,
        transaction_id: int,
        candidate_id: int,
        name: str,
        type_code: int,
    ) -> int:
        fields = self._transactions[transaction_id]["classes"][candidate_id]["fields"]
        field_id = len(fields)
        fields[name] = (field_id, type_code)
        return field_id

    def _cds_schema_reserve(self, *_args) -> None:
        return None

    def _cds_schema_alloc(self, transaction_id: int, candidate_id: int):
        data = self._transactions[transaction_id]["classes"][candidate_id]
        slot = (data["next_slot"], 1)
        data["next_slot"] += 1
        data["slots"][slot] = {}
        return slot

    def _cds_schema_migrate_slot(
        self,
        transaction_id: int,
        candidate_id: int,
        _source_class_id: int,
        _source_slot,
        field_map,
    ):
        slot = self._cds_schema_alloc(transaction_id, candidate_id)
        destination = self._transactions[transaction_id]["classes"][candidate_id]
        source = self._published[_source_class_id]["slots"][tuple(_source_slot)]
        destination_types = {
            field_id: type_code
            for field_id, type_code in destination["fields"].values()
        }
        for source_field, destination_field in field_map:
            value = source[source_field]
            if destination_types[destination_field] == 0:
                value = float(value)
            destination["slots"][slot][destination_field] = value
        return slot

    def _cds_schema_set(
        self,
        transaction_id: int,
        candidate_id: int,
        field_id: int,
        slot,
        _type_code: int,
        value,
    ) -> None:
        data = self._transactions[transaction_id]["classes"][candidate_id]
        data["slots"][tuple(slot)][field_id] = value

    def _cds_schema_seal(self, transaction_id: int):
        transaction = self._transactions[transaction_id]
        sealed = {}
        for candidate_id, data in transaction["classes"].items():
            key = data["key"]
            sealed[candidate_id] = self._class_ids.setdefault(
                key,
                len(self._class_ids) + 100,
            )
        transaction["sealed"] = sealed
        return dict(sealed)

    def _cds_schema_final_class_id(self, transaction_id: int, candidate_id: int):
        return self._transactions[transaction_id]["sealed"].get(candidate_id)

    def _cds_schema_commit(self, transaction_id: int):
        transaction = self._transactions[transaction_id]
        transaction["committed"] = []
        for candidate_id, data in transaction["classes"].items():
            self.class_keys.append(data["key"])
            class_id = transaction["sealed"][candidate_id]
            self._published[class_id] = {
                "fields": dict(data["fields"]),
                "slots": dict(data["slots"]),
                "next_slot": data["next_slot"],
            }
            transaction["committed"].append((data["key"], class_id))
        return dict(transaction["sealed"])

    def _cds_schema_finalize(self, transaction_id: int) -> bool:
        transaction = self._transactions[transaction_id]
        transaction["active"] = False
        transaction["finalized"] = True
        return True

    def _cds_schema_rollback(self, transaction_id: int) -> bool:
        transaction = self._transactions[transaction_id]
        if transaction.get("finalized", False):
            return False
        for key, class_id in reversed(transaction.get("committed", ())):
            self._published.pop(class_id, None)
            if key in self.class_keys:
                self.class_keys.remove(key)
        transaction["active"] = False
        return True

    def _cds_schema_active(self, transaction_id: int) -> bool:
        return bool(self._transactions[transaction_id]["active"])


class _DispatchPublication:
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


@pytest.fixture
def candidate_environment(tmp_path, monkeypatch):
    previous_root = get_project_root()
    registry_snapshot = snapshot_component_registry_state()
    project = tmp_path / "CandidateProject"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    set_project_root(str(project))

    probe = _CDSProbe()
    monkeypatch.setattr(_cds_bridge, "_class_registry", {})
    monkeypatch.setattr(_cds_bridge, "_get_lib", lambda: probe)
    monkeypatch.setattr(
        runtime_dispatch,
        "assert_runtime_dispatch_safe_point",
        lambda: None,
    )
    monkeypatch.setattr(
        runtime_dispatch,
        "publish_runtime_dispatch_epoch",
        lambda *_args, **_kwargs: _DispatchPublication(),
    )

    yield assets, probe

    restore_component_registry_state(registry_snapshot)
    root_key = str(project.resolve()).casefold()
    for name, module in tuple(sys.modules.items()):
        module_path = str(getattr(module, "__file__", "") or "")
        if module_path and str(Path(module_path).resolve()).casefold().startswith(root_key):
            sys.modules.pop(name, None)
    set_project_root(previous_root)


def _write_candidate(assets: Path, name: str, source: str) -> Path:
    path = assets / name
    path.write_text(source, encoding="utf-8")
    return path


def _stage(path: Path, source: str, guid: str = "candidate-guid"):
    return stage_component_body_reload_batch((
        ComponentBodyReloadRequest(
            str(path),
            script_guid=guid,
            source=source.encode("utf-8"),
        ),
    ))


def test_ordinary_component_definition_still_registers_immediately(
    candidate_environment,
):
    _assets, probe = candidate_environment
    before = snapshot_component_registry_state()

    component_type = type(
        "ImmediateRegistrationProbe",
        (InxComponent,),
        {"__module__": "ordinary_component_registration", "amount": 1.0},
    )

    assert not is_component_registration_pending(component_type)
    assert snapshot_component_registry_state() != before
    assert len(probe.class_keys) == 1


def test_ordinary_loader_execution_still_registers_immediately(
    candidate_environment,
):
    assets, probe = candidate_environment
    source = (
        "from Infernux.components import InxComponent\n"
        "class OrdinaryLoaderProbe(InxComponent):\n"
        "    amount: float = 1.0\n"
    )
    path = _write_candidate(assets, "ordinary_loader_probe.py", source)

    components = load_all_components_from_file(
        str(path),
        register=False,
        source_only=True,
        source=source.encode("utf-8"),
    )

    assert len(components) == 1
    assert not is_component_registration_pending(components[0])
    assert len(probe.class_keys) == 1


def test_candidate_schema_is_published_only_by_successful_owner_commit(
    candidate_environment,
):
    assets, probe = candidate_environment
    source = (
        "from Infernux.components import InxComponent\n"
        "class CandidateCommitProbe(InxComponent):\n"
        "    amount: float = 1.0\n"
    )
    path = _write_candidate(assets, "CandidateCommitProbe.py", source)
    before = snapshot_component_registry_state()

    transaction = _stage(path, source)
    candidate = transaction.registry_entries[0][1][0]

    assert is_component_registration_pending(candidate)
    assert snapshot_component_registry_state() == before
    assert probe.class_keys == []

    transaction.commit()

    assert not is_component_registration_pending(candidate)
    assert snapshot_component_registry_state() != before
    assert len(probe.class_keys) == 1


def test_candidate_import_failure_never_registers_python_or_cds_state(
    candidate_environment,
):
    assets, probe = candidate_environment
    source = (
        "from Infernux.components import InxComponent\n"
        "class CandidateImportFailure(InxComponent):\n"
        "    amount: float = 1.0\n"
        "raise RuntimeError('candidate import failed')\n"
    )
    path = _write_candidate(assets, "CandidateImportFailure.py", source)
    before = snapshot_component_registry_state()

    with pytest.raises(ScriptLoadError, match="candidate import failed"):
        _stage(path, source)

    assert snapshot_component_registry_state() == before
    assert probe.class_keys == []
    assert probe.allocations == []


def test_recursive_candidate_imports_share_the_registration_scope(
    candidate_environment,
):
    assets, probe = candidate_environment
    helper_source = (
        "from Infernux.components import InxComponent\n"
        "class CandidateDependency(InxComponent):\n"
        "    amount: float = 2.0\n"
    )
    root_source = (
        "from Infernux.components import InxComponent\n"
        "from candidate_dependency import CandidateDependency\n"
        "class CandidateRoot(InxComponent):\n"
        "    amount: float = CandidateDependency().amount\n"
    )
    _write_candidate(assets, "candidate_dependency.py", helper_source)
    root_path = _write_candidate(assets, "candidate_root.py", root_source)

    transaction = stage_component_body_reload_batch((
        ComponentBodyReloadRequest(
            str(root_path),
            script_guid="candidate-root-guid",
            source=root_source.encode("utf-8"),
        ),
        ComponentBodyReloadRequest(
            str(assets / "candidate_dependency.py"),
            source=helper_source.encode("utf-8"),
        ),
    ))

    assert probe.class_keys == []
    assert all(
        is_component_registration_pending(component_type)
        for _path, component_types in transaction.registry_entries
        for component_type in component_types
    )
    transaction.rollback()
    assert probe.class_keys == []


def test_live_publication_failure_precedes_registry_and_schema_publication(
    candidate_environment,
    monkeypatch,
):
    assets, probe = candidate_environment
    source = (
        "from Infernux.components import InxComponent\n"
        "class CandidateLiveFailure(InxComponent):\n"
        "    amount: float = 1.0\n"
    )
    path = _write_candidate(assets, "CandidateLiveFailure.py", source)
    target_type = type(
        "CandidateLiveFailure",
        (InxComponent,),
        {
            "__module__": "CandidateLiveFailure",
            "amount": 1.0,
        },
    )
    bind_asset_script_guid(target_type, "candidate-guid")
    stable_class_keys = tuple(probe.class_keys)
    before = snapshot_component_registry_state()
    transaction = stage_component_body_reload_batch((
        ComponentBodyReloadRequest(
            str(path),
            target_types=(target_type,),
            script_guid="candidate-guid",
            source=source.encode("utf-8"),
        ),
    ))
    published_type = transaction.registry_entries[0][1][0]
    assert published_type is target_type
    monkeypatch.setattr(
        runtime_dispatch,
        "publish_runtime_dispatch_epoch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated live publication failure")
        ),
    )

    with pytest.raises(RuntimeError, match="simulated live publication failure"):
        transaction.commit()

    assert transaction.rolled_back
    assert snapshot_component_registry_state() == before
    assert tuple(probe.class_keys) == stable_class_keys
    assert probe.allocations == []


def test_owner_safe_point_rejection_precedes_every_live_and_schema_mutation(
    candidate_environment,
    monkeypatch,
):
    assets, probe = candidate_environment
    name = "OwnerSafePointFailureProbe"
    guid = "owner-safe-point-failure-guid"
    target = _schema_target(name, guid)
    instance = target()
    instance.count = 64
    old_class_id = instance._cds_class_id
    old_slot = instance._cds_slot
    before_registry = snapshot_component_registry_state()
    before_class_keys = tuple(probe.class_keys)
    source = _schema_candidate_source(name)
    path = _write_candidate(assets, f"{name}.py", source)
    transaction = _stage_schema_reload(path, target, (instance,), source, guid)

    monkeypatch.setattr(
        runtime_dispatch,
        "assert_runtime_dispatch_safe_point",
        lambda: (_ for _ in ()).throw(
            RuntimeError("runtime dispatch publication requires an owner safe point; active frame")
        ),
    )

    with pytest.raises(RuntimeError, match="owner safe point"):
        transaction.commit()

    assert transaction.rolled_back
    assert snapshot_component_registry_state() == before_registry
    assert tuple(probe.class_keys) == before_class_keys
    assert instance._cds_class_id == old_class_id
    assert instance._cds_slot == old_slot
    assert instance.count == 64
    assert instance.update(0.0) == "old"
    assert "velocity" not in target.__dict__


def test_native_schema_commit_then_python_descriptor_failure_rolls_back_exactly(
    candidate_environment,
    monkeypatch,
):
    assets, probe = candidate_environment
    name = "SchemaDescriptorPublishFailure"
    guid = "schema-descriptor-publish-failure-guid"
    target = _schema_target(name, guid)
    instance = target()
    instance.count = 73
    old_class_id = instance._cds_class_id
    old_slot = instance._cds_slot
    before_registry = snapshot_component_registry_state()
    source = _schema_candidate_source(name)
    path = _write_candidate(assets, f"{name}.py", source)
    transaction = _stage_schema_reload(path, target, (instance,), source, guid)
    candidate = transaction.cds_publish_types[0]
    module_name = transaction._candidate_import.publishable_modules[0].__name__
    previous_module = sys.modules.get(module_name)
    real_descriptor = _cds_bridge._descriptor

    def fail_candidate_descriptor(owner, field_name):
        if owner is candidate:
            return None
        return real_descriptor(owner, field_name)

    monkeypatch.setattr(_cds_bridge, "_descriptor", fail_candidate_descriptor)

    with pytest.raises(RuntimeError, match="serialized descriptor"):
        transaction.commit()

    assert transaction.rolled_back
    assert snapshot_component_registry_state() == before_registry
    assert type(instance) is target
    assert instance._cds_class_id == old_class_id
    assert instance._cds_slot == old_slot
    assert instance.count == 73
    assert instance.update(0.0) == "old"
    assert "velocity" not in target.__dict__
    assert probe._cds_is_alive(old_class_id, old_slot)
    assert is_component_registration_pending(candidate)
    assert sys.modules.get(module_name) is previous_module
    assert transaction._cds_publication is not None
    assert transaction._cds_publication.rolled_back
    assert not any(
        transaction_state["active"]
        for transaction_state in probe._transactions.values()
    )


def test_registry_failure_after_live_mutation_restores_body_schema_and_registry(
    candidate_environment,
    monkeypatch,
):
    import Infernux.components.registry as registry

    assets, probe = candidate_environment
    name = "SchemaRegistryPublishFailure"
    guid = "schema-registry-publish-failure-guid"
    target = _schema_target(name, guid)
    instance = target()
    instance.count = 81
    old_class_id = instance._cds_class_id
    old_slot = instance._cds_slot
    before_registry = snapshot_component_registry_state()
    source = _schema_candidate_source(name)
    path = _write_candidate(assets, f"{name}.py", source)
    transaction = _stage_schema_reload(path, target, (instance,), source, guid)
    candidate = transaction.cds_publish_types[0]
    real_publish = registry.publish_component_script_types_batch

    def publish_then_fail(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise RuntimeError("simulated registry publication failure")

    monkeypatch.setattr(
        registry,
        "publish_component_script_types_batch",
        publish_then_fail,
    )

    with pytest.raises(RuntimeError, match="registry publication failure"):
        transaction.commit()

    assert transaction.rolled_back
    assert snapshot_component_registry_state() == before_registry
    assert instance._cds_class_id == old_class_id
    assert instance._cds_slot == old_slot
    assert instance.count == 81
    assert instance.update(0.0) == "old"
    assert "velocity" not in target.__dict__
    assert probe._cds_is_alive(old_class_id, old_slot)
    assert is_component_registration_pending(candidate)


def test_dispatch_failure_after_epoch_commit_rolls_back_owner_transaction(
    candidate_environment,
    monkeypatch,
):
    assets, _probe = candidate_environment
    name = "DispatchCommitFailureProbe"
    guid = "dispatch-commit-failure-guid"
    target = type(
        name,
        (InxComponent,),
        {
            "__module__": name,
            "_uses_component_data_store": False,
            "update": lambda self, _dt: "old",
        },
    )
    bind_asset_script_guid(target, guid)
    instance = target()
    source = (
        "from Infernux.components import InxComponent\n"
        f"class {name}(InxComponent):\n"
        "    _uses_component_data_store = False\n"
        "    def update(self, _dt): return 'new'\n"
    )
    path = _write_candidate(assets, f"{name}.py", source)
    transaction = _stage_schema_reload(path, target, (instance,), source, guid)
    state = {"committed": False, "rolled_back": False}

    class _FailingDispatchPublication:
        def commit(self):
            state["committed"] = True
            raise RuntimeError("simulated dispatch commit failure")

        def rollback(self):
            state["rolled_back"] = True

    monkeypatch.setattr(
        runtime_dispatch,
        "publish_runtime_dispatch_epoch",
        lambda *_args, **_kwargs: _FailingDispatchPublication(),
    )

    with pytest.raises(RuntimeError, match="dispatch commit failure"):
        transaction.commit()

    assert state == {"committed": True, "rolled_back": True}
    assert transaction.rolled_back
    assert instance.update(0.0) == "old"
    assert type(instance) is target


def test_candidate_module_failure_after_sys_modules_write_restores_every_entry(
    candidate_environment,
    monkeypatch,
):
    assets, probe = candidate_environment
    source = (
        "from Infernux.components import InxComponent\n"
        "class CandidateModuleCommitFailure(InxComponent):\n"
        "    amount: float = 1.0\n"
    )
    path = _write_candidate(assets, "CandidateModuleCommitFailure.py", source)
    before_registry = snapshot_component_registry_state()
    transaction = _stage(path, source, "candidate-module-commit-failure-guid")
    candidate = transaction.cds_publish_types[0]
    module_name = transaction._candidate_import.publishable_modules[0].__name__
    previous_module = sys.modules.get(module_name)
    real_commit = transaction._candidate_import.commit

    def commit_then_fail():
        real_commit()
        assert sys.modules.get(module_name) is not previous_module
        raise RuntimeError("simulated candidate module publication failure")

    monkeypatch.setattr(transaction._candidate_import, "commit", commit_then_fail)

    with pytest.raises(RuntimeError, match="candidate module publication failure"):
        transaction.commit()

    assert transaction.rolled_back
    assert snapshot_component_registry_state() == before_registry
    assert sys.modules.get(module_name) is previous_module
    assert is_component_registration_pending(candidate)
    assert probe.class_keys == []


def test_partial_pending_marker_publication_is_restored_on_failure(
    candidate_environment,
    monkeypatch,
):
    import Infernux.components._component_registration as registration

    assets, probe = candidate_environment
    source = (
        "from Infernux.components import InxComponent\n"
        "class PendingMarkerFirst(InxComponent):\n"
        "    amount: float = 1.0\n"
        "class PendingMarkerSecond(InxComponent):\n"
        "    amount: float = 2.0\n"
    )
    path = _write_candidate(assets, "pending_marker_pair.py", source)
    transaction = _stage(path, source, "pending-marker-guid")
    candidates = transaction.cds_publish_types
    assert len(candidates) == 2
    real_mark = registration.mark_component_registration_published
    calls = 0

    def mark_then_fail(component_type):
        nonlocal calls
        calls += 1
        real_mark(component_type)
        if calls == 2:
            raise RuntimeError("simulated pending marker publication failure")

    monkeypatch.setattr(
        registration,
        "mark_component_registration_published",
        mark_then_fail,
    )

    with pytest.raises(RuntimeError, match="pending marker publication failure"):
        transaction.commit()

    assert transaction.rolled_back
    assert all(is_component_registration_pending(candidate) for candidate in candidates)
    assert probe.class_keys == []


@pytest.mark.parametrize("discard_reason", ("stale", "superseded"))
def test_discarded_candidate_has_no_cds_state(
    candidate_environment,
    discard_reason,
):
    assets, probe = candidate_environment
    source = (
        "from Infernux.components import InxComponent\n"
        f"class Candidate{discard_reason.title()}(InxComponent):\n"
        "    amount: float = 1.0\n"
    )
    path = _write_candidate(assets, f"Candidate{discard_reason.title()}.py", source)
    before = snapshot_component_registry_state()
    transaction = _stage(path, source, f"{discard_reason}-guid")
    candidate = transaction.registry_entries[0][1][0]

    transaction.rollback()

    assert transaction.rolled_back
    assert is_component_registration_pending(candidate)
    assert snapshot_component_registry_state() == before
    assert probe.class_keys == []
    assert probe.allocations == []


def test_component_free_helper_commit_does_not_create_cds_state(
    candidate_environment,
):
    assets, probe = candidate_environment
    source = "HELPER_VALUE = 42\n"
    path = _write_candidate(assets, "candidate_helper.py", source)
    before = snapshot_component_registry_state()
    transaction = _stage(path, source, "")

    transaction.commit()

    assert transaction.registry_entries == ()
    assert snapshot_component_registry_state() == before
    assert probe.class_keys == []
    assert probe.allocations == []


def test_pending_candidate_cannot_reuse_live_schema_or_allocate_slot(
    candidate_environment,
):
    _assets, probe = candidate_environment
    live_type = type(
        "SharedLayoutProbe",
        (InxComponent,),
        {"__module__": "candidate_shared_layout", "amount": 1.0},
    )
    bind_asset_script_guid(live_type, "shared-layout-guid")
    allocation_count = len(probe.allocations)

    with candidate_component_registration_scope():
        candidate_type = type(
            "SharedLayoutProbe",
            (InxComponent,),
            {"__module__": "candidate_shared_layout", "amount": 1.0},
        )
    bind_asset_script_guid(candidate_type, "shared-layout-guid")
    candidate = create_component_instance(candidate_type)

    assert is_component_registration_pending(candidate_type)
    assert candidate._cds_slot is None
    assert candidate._cds_class_id is None
    assert len(probe.allocations) == allocation_count


def _schema_target(name: str, guid: str) -> type:
    component_type = type(
        name,
        (InxComponent,),
        {
            "__module__": name,
            "__annotations__": {
                "count": int,
                "obsolete": float,
                "label": str,
            },
            "count": 1,
            "obsolete": 2.0,
            "label": "old",
            "update": lambda self, _dt: "old",
        },
    )
    bind_asset_script_guid(component_type, guid)
    return component_type


def _schema_candidate_source(name: str) -> str:
    return (
        "from typing import Annotated\n"
        "from Infernux.components import InxComponent, FormerlySerializedAs\n"
        f"class {name}(InxComponent):\n"
        "    velocity: Annotated[float, FormerlySerializedAs('count')] = 0.0\n"
        "    label: str = 'new'\n"
        "    tags: list[str] = ['fresh']\n"
        "    def awake(self):\n"
        "        raise AssertionError('schema migration must not invoke awake')\n"
        "    def start(self):\n"
        "        raise AssertionError('schema migration must not invoke start')\n"
        "    def on_validate(self):\n"
        "        raise AssertionError('schema migration must not invoke on_validate')\n"
        "    def update(self, _dt):\n"
        "        return 'new'\n"
    )


def _stage_schema_reload(path: Path, target: type, instances: tuple[object, ...], source: str, guid: str):
    return stage_component_body_reload_batch((
        ComponentBodyReloadRequest(
            str(path),
            target_types=(target,),
            instances_by_type={target: instances},
            script_guid=guid,
            source=source.encode("utf-8"),
        ),
    ))


def test_schema_transaction_migrates_one_thousand_live_instances_in_place(
    candidate_environment,
):
    assets, probe = candidate_environment
    name = "SchemaThousandProbe"
    guid = "schema-thousand-guid"
    target = _schema_target(name, guid)
    instances = tuple(target() for _ in range(1000))
    old_class_id = instances[0]._cds_class_id
    old_slots = tuple(instance._cds_slot for instance in instances)
    for index, instance in enumerate(instances):
        instance.count = index
        instance.label = f"label-{index}"
    source = _schema_candidate_source(name)
    path = _write_candidate(assets, f"{name}.py", source)

    transaction = _stage_schema_reload(path, target, instances, source, guid)
    transaction.commit()

    assert all(type(instance) is target for instance in instances)
    assert all(instance._cds_class_id != old_class_id for instance in instances)
    assert [instances[index].velocity for index in (0, 1, 127, 999)] == [
        0.0,
        1.0,
        127.0,
        999.0,
    ]
    assert instances[127].label == "label-127"
    assert instances[0].tags == ["fresh"]
    assert instances[0].tags is not instances[1].tags
    assert instances[0].update(0.0) == "new"
    assert "count" not in target.__dict__
    assert "obsolete" not in target.__dict__
    assert "velocity" in target.__dict__
    assert all("count" not in instance.__dict__ for instance in instances)
    assert all(probe._cds_is_alive(old_class_id, slot) for slot in old_slots)

    transaction.finalize()

    assert all(not probe._cds_is_alive(old_class_id, slot) for slot in old_slots)


def test_multi_class_schema_prepare_failure_rolls_back_every_candidate(
    candidate_environment,
    monkeypatch,
):
    _assets, probe = candidate_environment
    with candidate_component_registration_scope():
        first = type(
            "SchemaBatchFirst",
            (InxComponent,),
            {"__module__": "schema_batch", "amount": 1.0},
        )
        second = type(
            "SchemaBatchSecond",
            (InxComponent,),
            {"__module__": "schema_batch", "amount": 2.0},
        )
    real_prepare = probe._cds_schema_prepare_field
    prepare_count = 0

    def fail_second_class(transaction_id, candidate_id, name, type_code):
        nonlocal prepare_count
        prepare_count += 1
        if prepare_count == 2:
            raise RuntimeError("simulated second class prepare failure")
        return real_prepare(transaction_id, candidate_id, name, type_code)

    monkeypatch.setattr(probe, "_cds_schema_prepare_field", fail_second_class)

    with pytest.raises(RuntimeError, match="second class prepare failure"):
        _cds_bridge.prepare_schema_publication((first, second))

    assert probe.class_keys == []
    assert all(not transaction["active"] for transaction in probe._transactions.values())
    assert _cds_bridge.get_class_info(first) is None
    assert _cds_bridge.get_class_info(second) is None


def test_schema_transaction_nth_instance_failure_restores_every_live_surface(
    candidate_environment,
    monkeypatch,
):
    import Infernux.components.script_loader as script_loader

    assets, probe = candidate_environment
    name = "SchemaNthFailureProbe"
    guid = "schema-nth-failure-guid"
    target = _schema_target(name, guid)
    instances = tuple(target() for _ in range(32))
    for index, instance in enumerate(instances):
        instance.count = index + 10
        instance.label = f"before-{index}"
    old_slots = tuple(instance._cds_slot for instance in instances)
    old_class_id = instances[0]._cds_class_id
    before_registry = snapshot_component_registry_state()
    before_class_keys = tuple(probe.class_keys)
    source = _schema_candidate_source(name)
    path = _write_candidate(assets, f"{name}.py", source)
    transaction = _stage_schema_reload(path, target, instances, source, guid)

    real_write = script_loader._write_private_descriptor_value
    write_count = 0

    def fail_on_seventeenth(descriptor, instance, value):
        nonlocal write_count
        write_count += 1
        if write_count == 17:
            raise RuntimeError("simulated instance migration failure")
        real_write(descriptor, instance, value)

    monkeypatch.setattr(
        script_loader,
        "_write_private_descriptor_value",
        fail_on_seventeenth,
    )

    with pytest.raises(RuntimeError, match="simulated instance migration failure"):
        transaction.commit()

    assert transaction.rolled_back
    assert snapshot_component_registry_state() == before_registry
    assert tuple(probe.class_keys) == before_class_keys
    assert "velocity" not in target.__dict__
    assert instances[5].count == 15
    assert instances[5].label == "before-5"
    assert instances[5].update(0.0) == "old"
    assert all(instance._cds_class_id == old_class_id for instance in instances)
    assert all(instance._cds_slot == slot for instance, slot in zip(instances, old_slots))
    assert all(probe._cds_is_alive(old_class_id, slot) for slot in old_slots)


def test_committed_schema_transaction_restores_exact_old_slots_before_finalize(
    candidate_environment,
):
    assets, probe = candidate_environment
    name = "SchemaCommittedRollbackProbe"
    guid = "schema-committed-rollback-guid"
    target = _schema_target(name, guid)
    instances = tuple(target() for _ in range(3))
    for index, instance in enumerate(instances):
        instance.count = index + 40
    old_class_id = instances[0]._cds_class_id
    old_slots = tuple(instance._cds_slot for instance in instances)
    source = _schema_candidate_source(name)
    path = _write_candidate(assets, f"{name}.py", source)
    transaction = _stage_schema_reload(path, target, instances, source, guid)
    published_before = tuple(probe.class_keys)

    transaction.commit()
    assert tuple(probe.class_keys) != published_before
    assert all(probe._cds_is_alive(old_class_id, slot) for slot in old_slots)

    transaction.rollback()

    assert transaction.rolled_back
    assert "count" in target.__dict__
    assert "velocity" not in target.__dict__
    assert [instance.count for instance in instances] == [40, 41, 42]
    assert all(instance._cds_class_id == old_class_id for instance in instances)
    assert tuple(instance._cds_slot for instance in instances) == old_slots
    assert all(
        probe._cds_is_alive(old_class_id, instance._cds_slot)
        for instance in instances
    )
    assert instances[0].update(0.0) == "old"
    assert tuple(probe.class_keys) == published_before


def test_committed_schema_rollback_does_not_depend_on_per_slot_free(
    candidate_environment,
    monkeypatch,
):
    assets, probe = candidate_environment
    name = "SchemaWholeStorageRollbackProbe"
    guid = "schema-whole-storage-rollback-guid"
    target = _schema_target(name, guid)
    instances = tuple(target() for _ in range(4))
    old_class_id = instances[0]._cds_class_id
    old_slots = tuple(instance._cds_slot for instance in instances)
    source = _schema_candidate_source(name)
    path = _write_candidate(assets, f"{name}.py", source)
    transaction = _stage_schema_reload(path, target, instances, source, guid)

    transaction.commit()

    def reject_redundant_slot_free(*_args):
        raise AssertionError("committed schema rollback must destroy candidate storage atomically")

    monkeypatch.setattr(probe, "_cds_free", reject_redundant_slot_free)
    transaction.rollback()

    assert transaction.rolled_back
    assert all(instance._cds_class_id == old_class_id for instance in instances)
    assert tuple(instance._cds_slot for instance in instances) == old_slots
    assert all(probe._cds_is_alive(old_class_id, slot) for slot in old_slots)


def test_finalized_schema_transaction_rejects_rollback_without_mutation(
    candidate_environment,
):
    assets, probe = candidate_environment
    name = "SchemaFinalizedRollbackProbe"
    guid = "schema-finalized-rollback-guid"
    target = _schema_target(name, guid)
    instance = target()
    instance.count = 91
    old_class_id = instance._cds_class_id
    old_slot = instance._cds_slot
    source = _schema_candidate_source(name)
    path = _write_candidate(assets, f"{name}.py", source)
    transaction = _stage_schema_reload(path, target, (instance,), source, guid)

    transaction.commit()
    new_class_id = instance._cds_class_id
    new_slot = instance._cds_slot
    transaction.finalize()

    with pytest.raises(ScriptReloadRejected, match="can no longer be rolled back"):
        transaction.rollback()

    assert transaction.committed
    assert instance._cds_class_id == new_class_id
    assert instance._cds_slot == new_slot
    assert instance.velocity == 91.0
    assert instance.update(0.0) == "new"
    assert not probe._cds_is_alive(old_class_id, old_slot)
