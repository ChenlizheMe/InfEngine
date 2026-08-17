from __future__ import annotations

import json
from pathlib import Path

import pytest

from Infernux.core.asset_ref import ParticleGraphRef
from Infernux.engine.interaction import (
    ActionOrigin,
    AssetReferenceRelocationPlanner,
    AssetContentChange,
    AssetMutationKind,
    AssetRenameContentRegistry,
    AssetMutationService,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    DocumentState,
    SelectionService,
    SelectionTarget,
)
from Infernux.engine.path_utils import path_key
from Infernux.engine.project_context import get_project_root, set_project_root
from Infernux.engine.ui import project_file_ops
from Infernux.engine.undo import LambdaCommand, UndoManager
from Infernux.particle.artifact import (
    PARTICLE_RUNTIME_INDEX_FILENAME,
    PARTICLE_RUNTIME_INDEX_SCHEMA,
    ParticleArtifactRegistry,
)


@pytest.fixture(autouse=True)
def _isolate_asset_mutation_service():
    yield
    service = AssetMutationService.instance()
    if service is not None:
        service.shutdown()
    AssetRenameContentRegistry.instance().shutdown()


class _Controller:
    def __init__(self) -> None:
        self.moves = []
        self.reloaded = False

    def resource_moved(self, **payload) -> None:
        self.moves.append(payload)

    def reload_from_resource(self, **_payload) -> bool:
        self.reloaded = True
        return True


def test_content_mutations_are_typed_ordered_and_never_create_undo(tmp_path):
    documents = DocumentRegistry()
    selection = SelectionService()
    service = AssetMutationService(documents, selection)
    published = []
    service.add_listener(published.append)
    path = tmp_path / "Smoke.png"

    created = service.publish_content_change(
        str(path),
        AssetMutationKind.CREATED,
        guid="texture-guid",
        origin=ActionOrigin.USER,
    )
    modified = service.publish_content_change(
        str(path),
        AssetMutationKind.MODIFIED,
        guid="texture-guid",
        origin=ActionOrigin.EXTERNAL,
    )
    deleted = service.publish_content_change(
        str(path),
        AssetMutationKind.DELETED,
        guid="texture-guid",
        origin=ActionOrigin.USER,
    )

    assert published == [created, modified, deleted]
    assert all(isinstance(item, AssetContentChange) for item in published)
    assert [item.mutation.kind for item in published] == [
        AssetMutationKind.CREATED,
        AssetMutationKind.MODIFIED,
        AssetMutationKind.DELETED,
    ]
    assert [item.revision for item in published] == [1, 2, 3]
    assert service.resolve_path_hint("texture-guid") == ""


def test_only_external_content_mutation_advances_document_external_revision(tmp_path):
    documents = DocumentRegistry()
    service = AssetMutationService(documents, SelectionService())
    path = tmp_path / "Smoke.particlegraph"
    path.write_text("baseline", encoding="utf-8")
    controller = _Controller()
    document = documents.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        key=DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(path)),
        resource_path=str(path),
        revision=1,
        saved_revision=0,
        controller=controller,
    )

    service.publish_content_change(
        str(path),
        AssetMutationKind.MODIFIED,
        origin=ActionOrigin.USER,
    )
    assert document.external_revision == 0
    assert document.state is DocumentState.READY

    path.write_text("external-change", encoding="utf-8")
    service.publish_content_change(
        str(path),
        AssetMutationKind.MODIFIED,
        origin=ActionOrigin.EXTERNAL,
    )
    assert document.external_revision == 1
    assert controller.reloaded
    assert document.state is DocumentState.READY
    assert not document.is_dirty


def test_project_directory_listener_flattens_typed_asset_notifications():
    source = Path("python/Infernux/engine/bootstrap_project.py").read_text(
        encoding="utf-8"
    )

    assert "for mutation in iter_asset_mutations(change):" in source
    assert "change.plan.mutations" not in source


def test_asset_mutation_core_has_no_ui_event_bus_fallback():
    assets_source = Path("python/Infernux/core/assets.py").read_text(encoding="utf-8")
    event_bus_path = Path("python/Infernux/engine/ui/event_bus.py")
    production_root = Path("python/Infernux")

    assert "engine.ui.event_bus" not in assets_source
    assert not event_bus_path.exists()
    assert all(
        "ASSET_CHANGED" not in path.read_text(encoding="utf-8")
        and "EditorEventBus" not in path.read_text(encoding="utf-8")
        for path in production_root.rglob("*.py")
    )


def test_particle_graph_internal_name_uses_shared_rename_content_adapter(tmp_path):
    source = tmp_path / "NewParticleGraph.particlegraph"
    destination = tmp_path / "Smoke Trail.particlegraph"
    source.write_text(
        json.dumps(
            {
                "$schema": "infernux.particle_graph",
                "stable_id": "graph-id",
                "name": "NewParticleGraph",
                "emitters": [],
                "parameters": [],
                "event_types": [],
            }
        ),
        encoding="utf-8",
    )

    patch = AssetRenameContentRegistry.instance().build_patch(
        str(source), str(destination)
    )

    assert patch is not None
    original, updated = patch
    assert json.loads(original)["name"] == "NewParticleGraph"
    assert json.loads(updated)["name"] == "Smoke Trail"


def test_asset_rename_content_adapters_are_extensible(tmp_path):
    source = tmp_path / "Old.customasset"
    destination = tmp_path / "New.customasset"
    source.write_text("Old", encoding="utf-8")
    registry = AssetRenameContentRegistry.instance()
    registry.register(
        (".customasset",),
        lambda content, _source, target: content.replace(
            "Old", Path(target).stem
        ),
    )

    assert registry.build_patch(str(source), str(destination)) == ("Old", "New")


def test_asset_relocation_upgrades_path_only_json_references_to_guid(tmp_path):
    project = tmp_path / "Project"
    shader = project / "Assets" / "Shaders" / "Old.frag"
    effect = project / "Assets" / "Effects" / "Outline.effect"
    shader.parent.mkdir(parents=True)
    effect.parent.mkdir(parents=True)
    shader.write_text("#version 450\n", encoding="utf-8")
    effect.write_text(
        json.dumps(
            {
                "$schema": "infernux.render_effect",
                "dependencies": [
                    {"guid": "", "path_hint": "Assets/Shaders/Old.frag"}
                ],
            }
        ),
        encoding="utf-8",
    )
    destination = project / "Assets" / "Rendering" / "Outline.frag"

    class _Database:
        @staticmethod
        def get_all_asset_paths():
            return [str(shader), str(effect)]

    patches = AssetReferenceRelocationPlanner.build_patches(
        ((str(shader), str(destination), "shader-guid"),),
        database=_Database(),
        project_root=str(project),
    )

    assert len(patches) == 1
    assert patches[0].source_path == str(effect)
    reference = json.loads(patches[0].updated)["dependencies"][0]
    assert reference == {
        "guid": "shader-guid",
        "path_hint": "Assets/Rendering/Outline.frag",
    }


def test_asset_relocation_does_not_rewrite_guid_authoritative_reference(tmp_path):
    project = tmp_path / "Project"
    material = project / "Assets" / "Materials" / "Surface.mat"
    texture = project / "Assets" / "Textures" / "Old.png"
    material.parent.mkdir(parents=True)
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"texture")
    material.write_text(
        json.dumps(
            {
                "texture": {
                    "guid": "texture-guid",
                    "path_hint": "Assets/Already/Stale.png",
                }
            }
        ),
        encoding="utf-8",
    )
    destination = project / "Assets" / "Art" / "New.png"

    class _Database:
        @staticmethod
        def get_all_asset_paths():
            return [str(texture), str(material)]

    patches = AssetReferenceRelocationPlanner.build_patches(
        ((str(texture), str(destination), "texture-guid"),),
        database=_Database(),
        project_root=str(project),
    )

    assert patches == ()


def test_asset_move_remaps_documents_selection_and_reference_display(tmp_path):
    old_path = tmp_path / "Old.particlegraph"
    new_path = tmp_path / "New.particlegraph"
    controller = _Controller()
    documents = DocumentRegistry()
    selection = SelectionService()
    service = AssetMutationService(documents, selection)
    document = documents.create(
        DocumentKind.PARTICLE_GRAPH,
        "Old",
        key=DocumentKey.asset(DocumentKind.PARTICLE_GRAPH, "graph-guid"),
        resource_path=str(old_path),
        controller=controller,
    )
    selection.select(
        SelectionTarget.asset(str(old_path)),
        owner_id="project",
        record_history=False,
    )
    reference = ParticleGraphRef(guid="graph-guid", path_hint="Assets/Old.particlegraph")

    change = service.publish_move(
        str(old_path),
        str(new_path),
        guid="graph-guid",
        origin=ActionOrigin.USER,
        operation_id="rename-operation",
    )

    assert change.remapped_document_ids == (document.document_id,)
    assert change.selection_changed is True
    assert document.resource_path == str(new_path)
    assert document.title == "New"
    assert document.key == DocumentKey.asset(DocumentKind.PARTICLE_GRAPH, "graph-guid")
    assert selection.snapshot.primary == SelectionTarget.asset(str(new_path))
    assert reference.display_name == "New.particlegraph"
    assert reference.to_dict()["path_hint"] == "Assets/Old.particlegraph"
    assert controller.moves == [
        {
            "document_id": document.document_id,
            "source_path": str(old_path),
            "destination_path": str(new_path),
            "guid": "graph-guid",
        }
    ]


def test_asset_move_rekeys_path_documents_and_subresource_selection(tmp_path):
    old_path = tmp_path / "Old.fbx"
    new_path = tmp_path / "New.fbx"
    documents = DocumentRegistry()
    selection = SelectionService()
    service = AssetMutationService(documents, selection)
    document = documents.create(
        DocumentKind.GENERIC,
        "Old",
        key=DocumentKey.resource(DocumentKind.GENERIC, str(old_path)),
        resource_path=str(old_path),
    )
    target = SelectionTarget.asset_subresource(
        str(old_path),
        "mesh-0",
        sub_kind="mesh",
    )
    selection.select(target, owner_id="project", record_history=False)

    service.publish_move(str(old_path), str(new_path))

    assert document.key == DocumentKey.resource(DocumentKind.GENERIC, str(new_path))
    assert selection.snapshot.primary == SelectionTarget.asset_subresource(
        str(new_path),
        "mesh-0",
        sub_kind="mesh",
    )


def test_particle_artifact_index_follows_global_asset_move(tmp_path):
    previous_root = get_project_root()
    set_project_root(str(tmp_path))
    try:
        artifact_root = tmp_path / "Library" / "Artifacts" / "Particle"
        artifact_root.mkdir(parents=True)
        index_path = artifact_root / PARTICLE_RUNTIME_INDEX_FILENAME
        index_path.write_text(
            json.dumps(
                {
                    "$schema": PARTICLE_RUNTIME_INDEX_SCHEMA,
                    "entries": [
                        {
                            "guid": "graph-guid",
                            "path_hint": "Assets/Old.particlegraph",
                            "stable_id": "stable-graph",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        old_path = tmp_path / "Assets" / "Old.particlegraph"
        new_path = tmp_path / "Assets" / "New.particlegraph"

        ParticleArtifactRegistry.remap_source(
            str(old_path),
            str(new_path),
            guid="graph-guid",
        )

        payload = json.loads(index_path.read_text(encoding="utf-8"))
        assert payload["entries"] == [
            {
                "guid": "graph-guid",
                "path_hint": "Assets/New.particlegraph",
                "stable_id": "stable-graph",
            }
        ]
    finally:
        ParticleArtifactRegistry.clear()
        set_project_root(previous_root)


def test_document_move_is_rejected_while_save_is_pending(tmp_path):
    old_path = tmp_path / "Old.scene"
    new_path = tmp_path / "New.scene"
    documents = DocumentRegistry()
    selection = SelectionService()
    service = AssetMutationService(documents, selection)
    document = documents.create(
        DocumentKind.SCENE,
        "Old",
        key=DocumentKey.resource(DocumentKind.SCENE, str(old_path)),
        resource_path=str(old_path),
    )
    documents.begin_save(document.document_id)

    with pytest.raises(RuntimeError, match="while document is saving"):
        service.publish_move(str(old_path), str(new_path))

    assert path_key(document.resource_path) == path_key(old_path)


def test_project_move_preflights_before_touching_the_workspace(tmp_path):
    class _Database:
        @staticmethod
        def get_guid_from_path(_path):
            return "scene-guid"

    old_path = tmp_path / "Old.scene"
    new_path = tmp_path / "New.scene"
    old_path.write_text("{}", encoding="utf-8")
    documents = DocumentRegistry()
    service = AssetMutationService(documents, SelectionService())
    document = documents.create(
        DocumentKind.SCENE,
        "Old",
        key=DocumentKey.asset(DocumentKind.SCENE, "scene-guid"),
        resource_path=str(old_path),
    )
    documents.begin_save(document.document_id)

    with pytest.raises(RuntimeError, match="while document is saving"):
        project_file_ops.move_path(str(old_path), str(new_path), _Database())

    assert old_path.exists()
    assert not new_path.exists()


def test_relocation_batch_publishes_once_and_keeps_one_operation_id(tmp_path):
    documents = DocumentRegistry()
    selection = SelectionService()
    service = AssetMutationService(documents, selection)
    published = []
    service.add_listener(published.append)
    operation_id = "directory-move"
    plan = service.prepare_relocation(
        (
            (str(tmp_path / "A.mat"), str(tmp_path / "Moved" / "A.mat"), "a"),
            (str(tmp_path / "B.png"), str(tmp_path / "Moved" / "B.png"), "b"),
        ),
        origin=ActionOrigin.USER,
        operation_id=operation_id,
    )

    change = service.commit_relocation(plan)

    assert len(published) == 1
    assert published[0] is change
    assert change.operation_id == operation_id
    assert {item.mutation.operation_id for item in change.changes} == {operation_id}


def test_undo_command_and_journal_share_operation_identity():
    manager = UndoManager()
    command = LambdaCommand("Identity", lambda: None, lambda: None)

    assert manager.execute(command)

    assert manager.action_journal.entries[0].operation_id == command.operation_id
