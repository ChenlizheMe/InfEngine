import importlib

import pytest

from Infernux.engine.interaction import (
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    GraphActionDiff,
    GraphElementKind,
    GraphElementRef,
    GraphMutation,
    GraphMutationKind,
    GraphSelectionController,
    SelectionDomain,
    SelectionService,
    SelectionTarget,
)
from Infernux.engine.undo import GraphDiffCommand, UndoManager


def test_graph_snapshot_undo_legacy_api_is_removed():
    undo = importlib.import_module("Infernux.engine.undo")

    for name in (
        "NodeGraphSnapshotCommand",
        "record_node_graph_snapshot",
        "AnimFSMSnapshotCommand",
        "record_animfsm_snapshot",
    ):
        assert not hasattr(undo, name)

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("Infernux.engine.undo._animfsm_commands")


class _View:
    def __init__(self):
        self.nodes = ()
        self.link = ""

    def project_selection(self, nodes, link):
        self.nodes = tuple(nodes)
        self.link = link


def test_graph_selection_rejects_views_without_authoritative_projection():
    class _LegacyView:
        selected_nodes = []
        selected_link = ""

    with pytest.raises(TypeError, match="project_selection"):
        GraphSelectionController(
            owner_id="graph-panel",
            document_id=lambda: "graph:one",
            contains=lambda _item: True,
            view=_LegacyView(),
        )


def test_graph_selection_projects_through_global_authority():
    service = SelectionService()
    view = _View()
    live = {
        GraphElementRef(GraphElementKind.NODE, "node-a"),
        GraphElementRef(GraphElementKind.LINK, "link-a"),
    }
    controller = GraphSelectionController(
        owner_id="graph-panel",
        document_id=lambda: "graph:one",
        contains=live.__contains__,
        view=view,
    )
    controller.bind(service)

    controller.select_one(GraphElementKind.NODE, "node-a")

    assert service.snapshot.primary == SelectionTarget.graph_element(
        "graph:one",
        "node-a",
        sub_kind="node",
    )
    assert controller.primary == GraphElementRef(GraphElementKind.NODE, "node-a")
    assert view.nodes == ("node-a",)
    assert view.link == ""

    controller.accept_view_selection((), "link-a", record_history=True)
    assert controller.primary_id(GraphElementKind.LINK) == "link-a"
    assert view.nodes == ()
    assert view.link == "link-a"

    service.select(SelectionTarget.asset("Assets/Test.mat"), owner_id="project")
    assert controller.elements == ()
    assert view.nodes == ()
    assert view.link == ""


def test_graph_selection_rejects_stale_and_mixed_elements():
    service = SelectionService()
    node = GraphElementRef(GraphElementKind.NODE, "node-a")
    parameter = GraphElementRef(GraphElementKind.PARAMETER, "parameter-a")
    controller = GraphSelectionController(
        owner_id="graph-panel",
        document_id=lambda: "graph:one",
        contains=lambda item: item in {node, parameter},
    )
    controller.bind(service)

    with pytest.raises(ValueError, match="multi-select"):
        controller.select((node, parameter))
    with pytest.raises(ValueError, match="outside"):
        controller.select_one(GraphElementKind.NODE, "missing")

    service.select(
        SelectionTarget.graph_element("graph:one", "missing", sub_kind="node"),
        owner_id="graph-panel",
        record_history=False,
    )
    assert service.snapshot.is_empty


def test_graph_selection_maps_document_identity_to_active_canvas_identity():
    service = SelectionService()
    view = _View()
    live = {
        GraphElementRef(GraphElementKind.NODE, "emitter-a/update::node-a"),
        GraphElementRef(GraphElementKind.LINK, "emitter-a/update::link-a"),
    }

    def from_view(kind, view_id):
        return GraphElementRef(kind, f"emitter-a/{view_id}")

    def to_view(element):
        emitter_id, separator, view_id = element.stable_id.partition("/")
        return view_id if separator and emitter_id == "emitter-a" else ""

    projected = []
    controller = GraphSelectionController(
        owner_id="particle-graph",
        document_id=lambda: "particle:one",
        contains=live.__contains__,
        view=view,
        element_from_view=from_view,
        element_to_view=to_view,
        on_changed=lambda elements: projected.append(elements),
    )
    controller.bind(service)

    controller.accept_view_selection(("update::node-a",), "", record_history=True)

    assert controller.primary == GraphElementRef(
        GraphElementKind.NODE,
        "emitter-a/update::node-a",
    )
    assert service.snapshot.primary == SelectionTarget.graph_element(
        "particle:one",
        "emitter-a/update::node-a",
        sub_kind="node",
    )
    assert view.nodes == ("update::node-a",)
    assert projected[-1] == controller.elements


def test_graph_action_diff_is_precise_and_invertible():
    before = {"name": "Old"}
    after = {"name": "New"}
    mutation = GraphMutation(
        GraphMutationKind.UPDATE,
        GraphElementRef(GraphElementKind.PARAMETER, "parameter-a"),
        before=before,
        after=after,
        before_index=2,
        after_index=2,
    )
    diff = GraphActionDiff("graph:one", (mutation,))
    before["name"] = "Mutated outside"

    inverse = diff.inverted()

    assert diff.mutations[0].before == {"name": "Old"}
    assert inverse.mutations[0].before == {"name": "New"}
    assert inverse.mutations[0].after == {"name": "Old"}
    assert inverse.mutations[0].kind is GraphMutationKind.UPDATE
    assert inverse.before_revision == diff.after_revision
    assert inverse.after_revision == diff.before_revision


class _Adapter:
    def __init__(self):
        self.values = {"parameter-a": {"name": "Old"}}
        self.applied = []

    def apply_diff(self, diff):
        self.applied.append(diff)
        for mutation in diff.mutations:
            self.values[mutation.element.stable_id] = mutation.after

    def capture_graph_diff_checkpoint(self):
        return dict(self.values)

    def restore_graph_diff_checkpoint(self, checkpoint):
        self.values = checkpoint


def test_graph_diff_command_replays_through_document_adapter_and_revision():
    registry = DocumentRegistry()
    adapter = _Adapter()
    document, _ = registry.open_or_create(
        DocumentKey.session(DocumentKind.ANIMATION_FSM),
        "FSM",
        revision=1,
        saved_revision=1,
        capabilities=DocumentCapability.SAVE,
        controller=adapter,
    )
    after_revision = registry.reserve_content_revision(document.document_id)
    mutation = GraphMutation(
        GraphMutationKind.UPDATE,
        GraphElementRef(GraphElementKind.PARAMETER, "parameter-a"),
        before={"name": "Old"},
        after={"name": "New"},
    )
    command = GraphDiffCommand(
        "Rename parameter",
        GraphActionDiff(
            document.document_id,
            (mutation,),
            before_revision=1,
            after_revision=after_revision,
        ),
    )

    command.execute()
    assert adapter.values["parameter-a"] == {"name": "New"}
    assert document.revision == after_revision

    command.undo()
    assert adapter.values["parameter-a"] == {"name": "Old"}
    assert document.revision == 1


def test_graph_diff_command_restores_domain_and_revision_after_partial_failure():
    class FailingAdapter(_Adapter):
        def apply_diff(self, diff):
            self.values["parameter-a"] = {"name": "Partially Applied"}
            raise RuntimeError("domain apply failed")

    registry = DocumentRegistry()
    adapter = FailingAdapter()
    document, _ = registry.open_or_create(
        DocumentKey.session(DocumentKind.ANIMATION_FSM),
        "FSM",
        revision=3,
        saved_revision=1,
        controller=adapter,
    )
    mutation = GraphMutation(
        GraphMutationKind.UPDATE,
        GraphElementRef(GraphElementKind.PARAMETER, "parameter-a"),
        before={"name": "Old"},
        after={"name": "New"},
    )
    command = GraphDiffCommand(
        "Rename parameter",
        GraphActionDiff(
            document.document_id,
            (mutation,),
            before_revision=3,
            after_revision=4,
        ),
    )

    with pytest.raises(RuntimeError, match="domain apply failed"):
        command.execute()

    assert adapter.values == {"parameter-a": {"name": "Old"}}
    assert document.revision == 3


def test_graph_diff_command_merges_same_property_without_losing_original_before():
    element = GraphElementRef(GraphElementKind.PARAMETER, "parameter-a")
    first = GraphDiffCommand(
        "Rename parameter",
        GraphActionDiff(
            "graph:one",
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    element,
                    before={"name": "A"},
                    after={"name": "AB"},
                ),
            ),
            before_revision=2,
            after_revision=3,
        ),
        merge_key="parameter-a:name",
    )
    second = GraphDiffCommand(
        "Rename parameter",
        GraphActionDiff(
            "graph:one",
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    element,
                    before={"name": "AB"},
                    after={"name": "ABC"},
                ),
            ),
            before_revision=3,
            after_revision=4,
        ),
        merge_key="parameter-a:name",
    )

    assert first.can_merge(second)
    first.merge(second)

    mutation = first.diff.mutations[0]
    assert mutation.before == {"name": "A"}
    assert mutation.after == {"name": "ABC"}
    assert first.diff.before_revision == 2
    assert first.diff.after_revision == 4
