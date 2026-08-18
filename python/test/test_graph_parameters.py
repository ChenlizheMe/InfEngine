from __future__ import annotations

from itertools import permutations

import pytest

from Infernux.graph import (
    GRAPH_PARAMETER_HDR_ATTRIBUTE,
    CoordinateSpace,
    GraphParameterAuthoringPolicy,
    GraphParameterCollection,
    GraphParameterDefinition,
    GraphParameterDiff,
    GraphParameterTransaction,
    TypeRef,
    ValueType,
    graph_parameter_allows_hdr,
    graph_parameter_attributes_with_hdr,
)


def _parameter(stable_id: str, name: str) -> GraphParameterDefinition:
    return GraphParameterDefinition(
        stable_id=stable_id,
        name=name,
        value_type=TypeRef(ValueType.F32),
        default=1.0,
    )


def test_graph_parameter_color_hdr_is_an_attribute_not_a_schema_key():
    color = GraphParameterDefinition(
        stable_id="tint",
        name="Tint",
        value_type=TypeRef(ValueType.COLOR),
        default=[1.0, 1.0, 1.0, 1.0],
    )

    assert "hdr" not in color.to_dict()
    assert color.to_dict()["attributes"] == []
    assert graph_parameter_allows_hdr(color) is False

    enabled = color.with_updates(
        {
            "attributes": list(
                graph_parameter_attributes_with_hdr(color.attributes, hdr=True)
            )
        }
    )
    encoded = enabled.to_dict()
    assert enabled.attributes == (GRAPH_PARAMETER_HDR_ATTRIBUTE,)
    assert graph_parameter_allows_hdr(enabled) is True
    assert "hdr" not in encoded
    assert encoded["attributes"] == ["hdr"]
    assert graph_parameter_attributes_with_hdr(enabled.attributes, hdr=False) == ()
    assert graph_parameter_allows_hdr(_parameter("a", "A")) is False
    assert graph_parameter_allows_hdr(
        _parameter("a", "A").with_updates({"attributes": ["hdr"]})
    ) is False


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


def test_graph_parameter_collection_moves_and_reorders_by_stable_identity():
    original = GraphParameterCollection(
        (_parameter("a", "A"), _parameter("b", "B"), _parameter("c", "C"))
    )

    moved = original.move("c", 0)
    reordered = moved.reorder(("b", "a", "c"))

    assert [value.stable_id for value in original.values] == ["a", "b", "c"]
    assert [value.stable_id for value in moved.values] == ["c", "a", "b"]
    assert [value.stable_id for value in reordered.values] == ["b", "a", "c"]
    assert original.move("a", 0) is original
    assert original.reorder(("a", "b", "c")) is original


def test_graph_parameter_collection_reorder_requires_an_exact_permutation():
    collection = GraphParameterCollection(
        (_parameter("a", "A"), _parameter("b", "B"))
    )

    with pytest.raises(ValueError, match="duplicate stable IDs"):
        collection.reorder(("a", "a"))
    with pytest.raises(ValueError, match="missing=.*b.*unknown=.*c"):
        collection.reorder(("a", "c"))
    with pytest.raises(KeyError, match="not found"):
        collection.move("missing", 0)
    with pytest.raises(IndexError, match="out of range"):
        collection.move("a", -1)
    with pytest.raises(IndexError, match="out of range"):
        collection.move("a", 2)
    with pytest.raises(TypeError, match="must be an integer"):
        collection.move("a", 1.0)


def test_graph_parameter_diff_replays_and_reverts_exact_crud_changes():
    a = _parameter("a", "A")
    renamed = _parameter("a", "Renamed")
    base = GraphParameterCollection((a,))
    created = _parameter("b", "B")
    create = GraphParameterDiff(None, created, None, 1)
    update = GraphParameterDiff(a, renamed, 0, 0)
    delete = GraphParameterDiff(created, None, 1, None)

    with_created = create.apply(base)
    with_updated = update.apply(with_created)
    without_created = delete.apply(with_updated)

    assert with_created.values == (a, created)
    assert with_updated.values == (renamed, created)
    assert without_created.values == (renamed,)
    assert delete.revert(without_created).values == with_updated.values
    assert update.revert(with_updated).values == with_created.values
    assert create.revert(with_created).values == base.values


def test_graph_parameter_diff_rejects_stale_or_conflicting_replay():
    a = _parameter("a", "A")
    base = GraphParameterCollection((a,))

    with pytest.raises(ValueError, match="precondition failed"):
        GraphParameterDiff(a, _parameter("a", "Renamed"), 1, 1).apply(base)
    with pytest.raises(ValueError, match="already exists"):
        GraphParameterDiff(None, a, None, 0).apply(base)
    with pytest.raises(ValueError, match="after_index is out of range"):
        GraphParameterDiff(None, _parameter("b", "B"), None, 2).apply(base)
    with pytest.raises(ValueError, match="stable identity"):
        GraphParameterDiff(a, _parameter("b", "B"), 0, 0)
    with pytest.raises(ValueError, match="no-op"):
        GraphParameterDiff(a, a, 0, 0)


def test_graph_parameter_transaction_is_atomic_composable_and_reversible():
    a = _parameter("a", "A")
    b = _parameter("b", "B")
    base = GraphParameterCollection((a, b))

    initial = GraphParameterTransaction.begin(base)
    transaction = (
        initial.create(_parameter("c", "C"), 1)
        .update(_parameter("a", "Renamed A"))
        .move("b", 0)
        .delete("c")
    )

    assert initial.collection is base
    assert initial.diffs == ()
    assert [value.name for value in transaction.collection.values] == [
        "B",
        "Renamed A",
    ]
    assert transaction.apply().values == transaction.collection.values
    assert transaction.revert().values == base.values
    assert [diff.stable_id for diff in transaction.diffs] == ["c", "a", "b", "c"]

    with pytest.raises(ValueError, match="name already exists"):
        transaction.create(_parameter("d", "B"))
    assert [value.name for value in transaction.collection.values] == [
        "B",
        "Renamed A",
    ]


def test_graph_parameter_transaction_reorder_emits_precise_move_diffs():
    base = GraphParameterCollection(
        (_parameter("a", "A"), _parameter("b", "B"), _parameter("c", "C"))
    )

    transaction = GraphParameterTransaction.begin(base).reorder(("c", "a", "b"))

    assert [value.stable_id for value in transaction.collection.values] == [
        "c",
        "a",
        "b",
    ]
    assert len(transaction.diffs) == 1
    assert transaction.diffs[0].stable_id == "c"
    assert transaction.diffs[0].before_index == 2
    assert transaction.diffs[0].after_index == 0
    assert transaction.revert().values == base.values


def test_graph_parameter_transaction_replays_every_small_permutation():
    base = GraphParameterCollection(
        (_parameter("a", "A"), _parameter("b", "B"), _parameter("c", "C"))
    )

    for order in permutations(("a", "b", "c")):
        transaction = GraphParameterTransaction.begin(base).reorder(order)
        assert tuple(
            parameter.stable_id for parameter in transaction.apply().values
        ) == order
        assert transaction.revert().values == base.values


def test_graph_parameter_transaction_public_constructor_validates_its_result():
    base = GraphParameterCollection((_parameter("a", "A"),))

    with pytest.raises(ValueError, match="does not match its diffs"):
        GraphParameterTransaction(
            base=base,
            collection=GraphParameterCollection((_parameter("b", "B"),)),
        )


def test_graph_parameter_authoring_policy_owns_common_update_semantics():
    policy = GraphParameterAuthoringPolicy(
        GraphParameterDefinition,
        (ValueType.F32, ValueType.VEC3),
        lambda kind: 0.0 if kind is ValueType.F32 else [0.0, 0.0, 0.0],
        writable_types=frozenset({ValueType.F32}),
        allowed_spaces={
            ValueType.VEC3: (CoordinateSpace.NONE, CoordinateSpace.WORLD),
        },
        normalize_name=lambda value: value.replace(" ", "_"),
    )
    original = GraphParameterCollection((_parameter("a", "Speed"),))

    edit = policy.update(
        original,
        "a",
        {
            "name": "World Position",
            "type": TypeRef(ValueType.VEC3, CoordinateSpace.WORLD).to_dict(),
            "writable": True,
        },
    )

    assert edit.before is original.values[0]
    assert edit.after.name == "World_Position"
    assert edit.after.default == [0.0, 0.0, 0.0]
    assert edit.after.writable is False
    assert edit.collection.values == (edit.after,)
    assert original.values[0].name == "Speed"


def test_graph_parameter_authoring_policy_rejects_domain_space_and_duplicates():
    policy = GraphParameterAuthoringPolicy(
        GraphParameterDefinition,
        (ValueType.F32, ValueType.VEC3),
        lambda _kind: 0.0,
    )
    parameters = GraphParameterCollection(
        (_parameter("a", "A"), _parameter("b", "B"))
    )

    with pytest.raises(ValueError, match="coordinate space"):
        policy.update(
            parameters,
            "a",
            {"type": TypeRef(ValueType.VEC3, CoordinateSpace.WORLD)},
        )
    with pytest.raises(ValueError, match="name already exists"):
        policy.update(parameters, "a", {"name": "B"})
