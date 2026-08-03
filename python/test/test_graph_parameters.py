from __future__ import annotations

import pytest

from Infernux.graph import (
    GraphParameterCollection,
    GraphParameterDefinition,
    TypeRef,
    ValueType,
)


def _parameter(stable_id: str, name: str) -> GraphParameterDefinition:
    return GraphParameterDefinition(
        stable_id=stable_id,
        name=name,
        value_type=TypeRef(ValueType.F32),
        default=1.0,
    )


def test_graph_parameter_round_trip_uses_the_complete_current_schema():
    parameter = GraphParameterDefinition(
        stable_id="speed",
        name="Speed",
        value_type=TypeRef(ValueType.F32),
        default=2.0,
        category="Motion",
        tooltip="Movement speed",
        attributes=("runtime",),
    )

    assert GraphParameterDefinition.from_dict(parameter.to_dict()) == parameter


def test_graph_parameter_rejects_legacy_or_partial_documents():
    with pytest.raises(ValueError, match="fields mismatch"):
        GraphParameterDefinition.from_dict(
            {
                "stable_id": "speed",
                "name": "Speed",
                "kind": "float",
                "default_float": 1.0,
            }
        )


def test_graph_parameter_collection_enforces_stable_identity_and_names():
    collection = GraphParameterCollection(
        (_parameter("a", "A"), _parameter("b", "B"))
    )

    with pytest.raises(ValueError, match="stable ID already exists"):
        collection.insert(_parameter("a", "B"))
    with pytest.raises(ValueError, match="name already exists"):
        collection.insert(_parameter("c", "A"))
    with pytest.raises(ValueError, match="name already exists"):
        collection.replace(_parameter("a", "B"))


def test_graph_parameter_collection_operations_are_immutable():
    original = GraphParameterCollection((_parameter("a", "A"),))
    inserted = original.insert(_parameter("b", "B"))
    replaced = inserted.replace(_parameter("b", "Renamed"))
    removed = replaced.remove("a")

    assert [value.name for value in original.values] == ["A"]
    assert [value.name for value in inserted.values] == ["A", "B"]
    assert [value.name for value in replaced.values] == ["A", "Renamed"]
    assert [value.name for value in removed.values] == ["Renamed"]
