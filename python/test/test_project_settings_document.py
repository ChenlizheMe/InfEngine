import copy
import json

from Infernux.engine.interaction import (
    BUILD_SETTINGS_DEFAULTS,
    DocumentKind,
    DocumentRegistry,
    ProjectSettingsDocumentController,
    ensure_project_settings_document,
)
from Infernux.engine.undo import UndoManager
from Infernux.physics import settings as physics_settings


def _tag_document():
    layers = [""] * 32
    layers[0] = "Default"
    layers[1] = "TransparentFX"
    layers[2] = "IgnoreRaycast"
    layers[4] = "Water"
    layers[5] = "UI"
    return {
        "custom_tags": [],
        "layers": layers,
        "layer_collision_masks": [0xFFFFFFFF] * 32,
    }


class _TagManager:
    def __init__(self):
        self.document = _tag_document()

    def serialize(self):
        return json.dumps(self.document)

    def deserialize(self, payload):
        value = json.loads(payload)
        if set(value) != {"custom_tags", "layers", "layer_collision_masks"}:
            return False
        self.document = copy.deepcopy(value)
        return True


class _PhysicsModule:
    def __init__(self):
        self.applied = []

    @staticmethod
    def normalize(value):
        return physics_settings.normalize(value)

    @staticmethod
    def load(_project_path):
        return copy.deepcopy(physics_settings.DEFAULT_PHYSICS_SETTINGS)

    def apply(self, value):
        self.applied.append(self.normalize(value))


class _WriteTicket:
    def __init__(self):
        self.is_complete = False
        self.status = "pending"

    def complete(self, status="succeeded"):
        self.status = status
        self.is_complete = True


class _Submitter:
    def __init__(self):
        self.calls = []

    def __call__(self, path, content):
        ticket = _WriteTicket()
        self.calls.append((path, json.loads(content), ticket))
        return ticket

    def complete_all(self, status="succeeded"):
        for _, _, ticket in self.calls:
            if not ticket.is_complete:
                ticket.complete(status)


def _controller(tmp_path):
    settings_dir = tmp_path / "ProjectSettings"
    settings_dir.mkdir()
    tags = _TagManager()
    physics = _PhysicsModule()
    submitter = _Submitter()
    controller = ProjectSettingsDocumentController(
        str(tmp_path),
        tag_layer_manager=tags,
        physics_module=physics,
        submitter=submitter,
    )
    return controller, tags, physics, submitter


def test_project_settings_document_is_shared_by_all_settings_views(tmp_path):
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    tags = _TagManager()
    physics = _PhysicsModule()
    try:
        build = ensure_project_settings_document(
            str(tmp_path),
            view_id="build_settings",
            tag_layer_manager=tags,
            physics_module=physics,
        )
        layers = ensure_project_settings_document(
            str(tmp_path),
            view_id="tag_layer_settings",
            tag_layer_manager=tags,
            physics_module=physics,
        )
        matrix = ensure_project_settings_document(
            str(tmp_path),
            view_id="physics_settings",
            tag_layer_manager=tags,
            physics_module=physics,
        )

        assert build is layers is matrix
        document = registry.require(build.document_id)
        assert document.kind is DocumentKind.PROJECT_SETTINGS
        assert document.view_ids == {
            "build_settings",
            "tag_layer_settings",
            "physics_settings",
        }
    finally:
        DocumentRegistry._instance = previous_registry


def test_project_settings_recovers_a_stale_generic_panel_controller(tmp_path):
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    settings_path = tmp_path / "ProjectSettings"
    settings_path.mkdir()
    try:
        stale = registry.create(
            DocumentKind.PROJECT_SETTINGS,
            "Project Settings",
            resource_path=str(settings_path),
            controller=object(),
        )
        controller = ensure_project_settings_document(str(tmp_path))
        assert controller.document_id == stale.document_id
        assert registry.require(stale.document_id).controller is controller
    finally:
        DocumentRegistry._instance = previous_registry


def test_project_settings_edit_undo_and_redo_restore_runtime_sections(tmp_path):
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    controller, tags, physics, submitter = _controller(tmp_path)
    document = registry.create(
        DocumentKind.PROJECT_SETTINGS,
        "Project Settings",
        resource_path=controller.settings_path,
        controller=controller,
    )
    controller.document_id = document.document_id
    try:
        following = controller.capture_document()
        following["build"]["game_name"] = "Interaction Test"
        following["tag_layers"]["custom_tags"] = ["Damageable"]
        following["physics"]["gravity"] = [0.0, -4.0, 0.0]

        assert controller.apply_document(
            following,
            edit_key="project_settings.combined",
            description="Edit Project Settings",
        )
        assert controller.section("build")["game_name"] == "Interaction Test"
        assert tags.document["custom_tags"] == ["Damageable"]
        assert physics.applied[-1]["gravity"] == [0.0, -4.0, 0.0]
        assert document.is_dirty
        assert len(submitter.calls) == 3

        manager.undo()
        assert controller.section("build")["game_name"] == ""
        assert tags.document["custom_tags"] == []
        assert physics.applied[-1]["gravity"] == [0.0, -9.81, 0.0]

        manager.redo()
        assert controller.section("build")["game_name"] == "Interaction Test"
        assert tags.document["custom_tags"] == ["Damageable"]
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_shared_project_settings_revision_tracks_the_authoring_view(tmp_path):
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    controller, _, _, _ = _controller(tmp_path)
    document = registry.create(
        DocumentKind.PROJECT_SETTINGS,
        "Project Settings",
        resource_path=controller.settings_path,
        controller=controller,
    )
    controller.document_id = document.document_id
    for view_id in ("build_settings", "tag_layer_settings", "physics_settings"):
        registry.attach_view(document.document_id, view_id)
    try:
        build = controller.section("build")
        build["game_name"] = "Owned by Build"
        assert controller.apply_section(
            "build",
            build,
            edit_key="project_settings.build.game_name",
            description="Set Game Name",
            view_id="build_settings",
        )
        build_revision = document.revision
        assert document.dirty_owner_view_ids() == frozenset({"build_settings"})

        physics = controller.section("physics")
        physics["gravity"] = [0.0, -4.0, 0.0]
        assert controller.apply_section(
            "physics",
            physics,
            edit_key="project_settings.physics.gravity",
            description="Set Gravity",
            view_id="physics_settings",
        )
        assert document.dirty_owner_view_ids() == frozenset(
            {"build_settings", "physics_settings"}
        )

        manager.undo()
        assert document.revision == build_revision
        assert document.dirty_owner_view_ids() == frozenset({"build_settings"})
        manager.redo()
        assert document.dirty_owner_view_ids() == frozenset(
            {"build_settings", "physics_settings"}
        )
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_build_only_edit_does_not_reapply_unrelated_runtime_settings(tmp_path):
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    registry = DocumentRegistry()
    UndoManager()
    controller, tags, physics, _ = _controller(tmp_path)
    document = registry.create(
        DocumentKind.PROJECT_SETTINGS,
        "Project Settings",
        resource_path=controller.settings_path,
        controller=controller,
    )
    controller.document_id = document.document_id
    registry.attach_view(document.document_id, "build_settings")
    initial_tags = copy.deepcopy(tags.document)
    initial_physics_apply_count = len(physics.applied)
    try:
        build = controller.section("build")
        build["game_name"] = "No Runtime Churn"
        assert controller.apply_section(
            "build",
            build,
            edit_key="project_settings.build.game_name",
            description="Set Game Name",
            view_id="build_settings",
        )
        assert tags.document == initial_tags
        assert len(physics.applied) == initial_physics_apply_count
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_project_settings_async_persistence_owns_saved_revision(tmp_path):
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    registry = DocumentRegistry()
    UndoManager()
    controller, _, _, submitter = _controller(tmp_path)
    document = registry.create(
        DocumentKind.PROJECT_SETTINGS,
        "Project Settings",
        resource_path=controller.settings_path,
        controller=controller,
    )
    controller.document_id = document.document_id
    try:
        following = controller.capture_document()
        following["build"]["window_width"] = 1600
        assert controller.apply_document(
            following,
            edit_key="project_settings.build.window_width",
            description="Set Build Window Width",
        )
        assert document.is_dirty

        submitter.complete_all()
        assert registry.process_pending_saves() == 1
        assert not document.is_dirty
        assert {path.rsplit("\\", 1)[-1] for path, _, _ in submitter.calls} == {
            "BuildSettings.json",
            "TagLayerSettings.json",
            "PhysicsSettings.json",
        }
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_project_settings_derived_update_does_not_publish_a_second_user_action(tmp_path):
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    controller, _, _, submitter = _controller(tmp_path)
    document = registry.create(
        DocumentKind.PROJECT_SETTINGS,
        "Project Settings",
        resource_path=controller.settings_path,
        controller=controller,
    )
    controller.document_id = document.document_id
    try:
        build = controller.section("build")
        build["scenes"] = ["Assets/Renamed.scene"]

        assert controller.apply_derived_section("build", build)
        assert controller.section("build")["scenes"] == ["Assets/Renamed.scene"]
        assert len(manager.action_journal.entries) == 0
        assert document.is_dirty
        assert len(submitter.calls) == 3
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_scene_asset_move_updates_live_build_document_with_portable_path(
    tmp_path,
    monkeypatch,
):
    from Infernux.engine.ui import project_file_ops

    assets = tmp_path / "Assets"
    assets.mkdir()
    old_scene = assets / "Old.scene"
    new_scene = assets / "Renamed.scene"
    old_scene.write_text("{}", encoding="utf-8")

    class Controller:
        def __init__(self):
            self.build = copy.deepcopy(BUILD_SETTINGS_DEFAULTS)
            self.build["scenes"] = ["Assets/Old.scene"]
            self.calls = []

        def section(self, name):
            assert name == "build"
            return copy.deepcopy(self.build)

        def apply_derived_section(self, name, value):
            self.calls.append((name, copy.deepcopy(value)))
            self.build = copy.deepcopy(value)
            return True

    controller = Controller()
    monkeypatch.setattr(
        "Infernux.engine.project_context.get_project_root",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(
        "Infernux.engine.interaction.ensure_project_settings_document",
        lambda _root: controller,
    )

    project_file_ops._update_build_settings_scene_path(
        str(old_scene),
        str(new_scene),
    )

    assert controller.calls == [
        ("build", {**BUILD_SETTINGS_DEFAULTS, "scenes": ["Assets/Renamed.scene"]})
    ]
