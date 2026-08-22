from __future__ import annotations

import pytest

from Infernux.engine.interaction import (
    CollectionInteractionModel,
    TreeProjectionModel,
)


def test_collection_rejects_missing_and_duplicate_stable_ids():
    with pytest.raises(ValueError, match="non-empty"):
        CollectionInteractionModel(("a", ""))
    with pytest.raises(ValueError, match="duplicate"):
        CollectionInteractionModel(("a", "a"))


def test_collection_selection_projection_is_ordered_and_not_a_second_authority():
    model = CollectionInteractionModel(("a", "b", "c"))

    assert model.project_selection(("c", "a"), primary_id="c")
    assert model.selection.selected_ids == ("a", "c")
    assert model.selection.primary_id == "c"

    with pytest.raises(KeyError, match="unknown"):
        model.project_selection(("missing",))
    with pytest.raises(ValueError, match="primary"):
        model.project_selection(("a",), primary_id="b")


def test_collection_range_toggle_and_keyboard_navigation_share_one_anchor():
    model = CollectionInteractionModel(("a", "b", "c", "d"))

    model.activate("b")
    assert model.activate("d", extend=True).selected_ids == ("b", "c", "d")
    assert model.selection.anchor_id == "b"
    assert model.activate("c", toggle=True).selected_ids == ("b", "d")
    assert model.move_cursor(-1).primary_id == "b"
    assert model.move_to_edge(last=True, extend=True).selected_ids == ("b", "c", "d")


def test_collection_reconcile_preserves_valid_state_and_retires_removed_transients():
    model = CollectionInteractionModel(("a", "b", "c"))
    model.activate("b")
    model.begin_rename("b", "Before")
    model.set_insertion(3, ("b",))

    assert model.set_items(("a", "c"))
    assert model.selection.selected_ids == ()
    assert model.rename_session is None
    assert model.insertion is not None
    assert model.insertion.index == 2
    assert model.insertion.dragged_ids == ()


def test_collection_rename_focus_candidate_and_virtualization_are_deterministic():
    model = CollectionInteractionModel(("a", "b", "c", "d", "e"))
    model.begin_rename("c", "Old")

    assert model.consume_rename_focus() is True
    assert model.consume_rename_focus() is False
    assert model.update_rename("  New  ")
    assert model.rename_candidate() == ("c", "New")
    assert model.viewport(2, 1, overscan=1).item_ids == ("b", "c", "d")
    assert model.finish_rename()


def test_tree_projection_preserves_foldout_across_reorder_and_reparent():
    tree = TreeProjectionModel(("parent", "child"))
    rows = tree.project(
        ("parent", "other"),
        {"parent": ("child",), "child": ("leaf",)},
    )
    assert [(row.item_id, row.depth) for row in rows] == [
        ("parent", 0),
        ("child", 1),
        ("leaf", 2),
        ("other", 0),
    ]

    rows = tree.project(
        ("other", "parent"),
        {"other": ("child",), "child": ("leaf",)},
    )
    assert [(row.item_id, row.depth) for row in rows] == [
        ("other", 0),
        ("parent", 0),
    ]
    assert tree.is_expanded("parent")
    assert tree.is_expanded("child")


def test_tree_reveal_reconcile_and_validation_fail_closed():
    tree = TreeProjectionModel()
    assert tree.reveal_ancestors(
        "leaf",
        {"leaf": "child", "child": "root", "root": ""},
    )
    assert tree.expanded_ids == frozenset({"root", "child"})
    assert tree.reconcile(("root", "leaf"))
    assert tree.expanded_ids == frozenset({"root"})

    tree.set_expanded("a", True)
    tree.set_expanded("b", True)
    with pytest.raises(ValueError, match="cycle"):
        tree.project(("a",), {"a": ("b",), "b": ("a",)})
    with pytest.raises(ValueError, match="multiple parents"):
        tree.project(("a", "b"), {"a": ("shared",), "b": ("shared",)})


def test_tree_projection_can_replace_an_undo_snapshot_atomically():
    tree = TreeProjectionModel(("old", "keep"))

    assert tree.replace_expanded(("keep", "new"))
    assert tree.expanded_ids == frozenset({"keep", "new"})
    assert not tree.replace_expanded(("new", "keep"))
