from __future__ import annotations

import json

import pytest


def test_runtime_type_registry_binds_declared_phase_contract(tmp_path):
    from Infernux.components.component import InxComponent
    from Infernux.engine.runtime_type_registry import (
        bind_runtime_lifecycle_contract,
        clear_runtime_type_registry,
        install_runtime_type_registry,
        validate_runtime_component_identity,
    )

    class PlayerMover(InxComponent):
        def update(self, delta_time):
            del delta_time

    type_guid = PlayerMover._get_type_guid()
    path = tmp_path / "RuntimeTypeRegistry.json"
    path.write_text(
        json.dumps(
            {
                "$schema": "infernux.runtime_type_registry",
                "registry_version": 1,
                "types": [
                    {
                        "script_guid": "script-guid",
                        "type_guid": type_guid,
                        "module": PlayerMover.__module__,
                        "qualname": PlayerMover.__qualname__,
                        "runtime_path": "Assets/Scripts/player.pyc",
                        "lifecycle": ["update"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        assert install_runtime_type_registry(str(path)) == 1
        record = validate_runtime_component_identity(
            script_guid="script-guid",
            type_guid=type_guid,
            module_name=PlayerMover.__module__,
            qualified_name=PlayerMover.__qualname__,
        )
        bind_runtime_lifecycle_contract(PlayerMover, record)
        assert PlayerMover._runtime_declared_phases_ == frozenset({"update"})
    finally:
        clear_runtime_type_registry()


def test_runtime_type_registry_rejects_unlisted_component(tmp_path):
    from Infernux.engine.runtime_type_registry import (
        clear_runtime_type_registry,
        install_runtime_type_registry,
        validate_runtime_component_identity,
    )

    path = tmp_path / "RuntimeTypeRegistry.json"
    path.write_text(
        json.dumps(
            {
                "$schema": "infernux.runtime_type_registry",
                "registry_version": 1,
                "types": [],
            }
        ),
        encoding="utf-8",
    )
    try:
        install_runtime_type_registry(str(path))
        # An empty registry is valid, but no user component may appear later.
        with pytest.raises(RuntimeError, match="absent"):
            validate_runtime_component_identity(
                script_guid="script",
                type_guid="missing",
                module_name="Scripts.missing",
                qualified_name="Missing",
            )
    finally:
        clear_runtime_type_registry()


def test_runtime_type_registry_rejects_identity_drift(tmp_path):
    from Infernux.engine.runtime_type_registry import (
        clear_runtime_type_registry,
        install_runtime_type_registry,
        validate_runtime_component_identity,
    )

    path = tmp_path / "RuntimeTypeRegistry.json"
    path.write_text(
        json.dumps(
            {
                "$schema": "infernux.runtime_type_registry",
                "registry_version": 1,
                "types": [
                    {
                        "script_guid": "script",
                        "type_guid": "type",
                        "module": "Scripts.player",
                        "qualname": "Player",
                        "runtime_path": "Assets/Scripts/player.pyc",
                        "lifecycle": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    try:
        install_runtime_type_registry(str(path))
        with pytest.raises(RuntimeError, match="disagrees"):
            validate_runtime_component_identity(
                script_guid="script",
                type_guid="type",
                module_name="Scripts.renamed",
                qualified_name="Player",
            )
    finally:
        clear_runtime_type_registry()
