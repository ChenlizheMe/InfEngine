from types import SimpleNamespace
import json

from Infernux.engine.interaction import (
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    EditableResourceDocumentController,
    ensure_editable_resource_document,
)
from Infernux.engine.undo import EditableDocumentDraftCommand, UndoManager


class _Resource:
    def __init__(self, value=1.0):
        self.value = float(value)

    def serialize_document(self):
        return {"value": self.value}

    def deserialize_document(self, document):
        self.value = float(document["value"])
        return True


class _ExecutionLayer:
    def __init__(self):
        self.pending = False
        self.saved = []
        self.binding = None

    def refresh_binding(self, category, file_path):
        self.binding = (category, file_path)

    def schedule_rw_save(self, resource):
        self.pending = True
        self._resource = resource

    def flush_rw_autosave(self, *, force=False):
        del force
        if not self.pending:
            return False
        self.pending = False
        self.saved.append(self._resource.serialize_document())
        return True


class _AsyncWriteTicket:
    def __init__(self):
        self.is_complete = False
        self.status = "pending"

    def complete(self, status="succeeded"):
        self.status = str(status)
        self.is_complete = True


class _AsyncExecutionLayer(_ExecutionLayer):
    def __init__(self):
        super().__init__()
        self.tickets = []

    def flush_rw_autosave(self, *, force=False):
        del force
        if not self.pending:
            return False
        self.pending = False
        self.saved.append(self._resource.serialize_document())
        ticket = _AsyncWriteTicket()
        self.tickets.append(ticket)
        return ticket


class _ViewScopedExecutionLayer(_ExecutionLayer):
    view_scoped_persistence = True

    def schedule_rw_save(self, resource):
        raise AssertionError("view-scoped persistence must not own the write")

    def flush_rw_autosave(self, *, force=False):
        raise AssertionError("view-scoped persistence must not own the flush")


def test_editable_resource_document_is_shared_without_an_inspector_view():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    resource = _Resource()
    execution = _ExecutionLayer()
    state = SimpleNamespace(resource_controller=None, settings=resource)
    try:
        inspector_controller = ensure_editable_resource_document(
            category="render_effect",
            document_kind=DocumentKind.RENDER_EFFECT,
            file_path="Assets/Bloom.effect",
            resource=resource,
            guid="effect-guid",
            view_id="inspector",
            state=state,
            exec_layer=execution,
            autosave_debounce_sec=0.5,
        )
        state.resource_controller = inspector_controller
        stack_controller = ensure_editable_resource_document(
            category="render_effect",
            document_kind=DocumentKind.RENDER_EFFECT,
            file_path="Assets/Bloom.effect",
            resource=resource,
            guid="effect-guid",
            autosave_debounce_sec=0.5,
        )

        assert stack_controller is inspector_controller
        document = registry.require(stack_controller.document_id)
        assert document.view_ids == {"inspector"}
        assert document.key == DocumentKey.asset(
            DocumentKind.RENDER_EFFECT, "effect-guid"
        )
        assert stack_controller.exec_layer is execution
        assert stack_controller.autosave_debounce_sec == 0.5
    finally:
        DocumentRegistry._instance = previous_registry


def test_dirty_editable_resource_keeps_authoritative_instance_across_views():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    original = _Resource(3.0)
    replacement = _Resource(1.0)
    first_state = SimpleNamespace(resource_controller=None, settings=original)
    second_state = SimpleNamespace(resource_controller=None, settings=replacement)
    execution = _ExecutionLayer()
    try:
        controller = ensure_editable_resource_document(
            category="material",
            document_kind=DocumentKind.MATERIAL,
            file_path="Assets/Ocean.mat",
            resource=original,
            guid="ocean-guid",
            state=first_state,
            exec_layer=execution,
        )
        registry.mark_changed(controller.document_id)

        rebound = ensure_editable_resource_document(
            category="material",
            document_kind=DocumentKind.MATERIAL,
            file_path="Assets/Ocean.mat",
            resource=replacement,
            guid="ocean-guid",
            state=second_state,
            exec_layer=execution,
        )

        assert rebound is controller
        assert controller.resource is original
        assert second_state.resource_controller is controller
        assert second_state.settings is original
    finally:
        DocumentRegistry._instance = previous_registry


def test_view_scoped_execution_uses_controller_owned_asset_path(monkeypatch):
    from Infernux.core.assets import AssetManager

    resource = _Resource(6.0)
    controller = EditableResourceDocumentController(
        "material", "Assets/Ocean.mat", resource
    )
    controller.exec_layer = _ViewScopedExecutionLayer()
    scheduled = []
    flushed = []
    monkeypatch.setattr(
        AssetManager,
        "schedule_asset_save",
        classmethod(
            lambda _cls, category, path, value, debounce_sec=0.0: scheduled.append(
                (category, path, value, debounce_sec)
            )
        ),
    )
    monkeypatch.setattr(
        AssetManager,
        "flush_scheduled_saves",
        classmethod(
            lambda _cls, path=None, force=False: flushed.append((path, force)) or True
        ),
    )

    controller.schedule_autosave()
    assert controller._flush_submission(force=True) is True

    assert scheduled == [("material", "Assets/Ocean.mat", resource, 0.35)]
    assert flushed == [("Assets/Ocean.mat", True)]


def test_editable_resource_document_tracks_autosave_and_undo_revisions():
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    resource = _Resource()
    execution = _ExecutionLayer()
    state = SimpleNamespace(resource_controller=None, settings=resource)
    controller = EditableResourceDocumentController(
        "physic_material", "Assets/Ice.physmat", resource
    )
    document = registry.create(
        DocumentKind.PHYSIC_MATERIAL,
        "Ice.physmat",
        key=DocumentKey.resource(
            DocumentKind.PHYSIC_MATERIAL, "Assets/Ice.physmat"
        ),
        resource_path="Assets/Ice.physmat",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    state.resource_controller = controller
    controller.bind(
        file_path="Assets/Ice.physmat",
        resource=resource,
        exec_layer=execution,
        state=state,
    )
    try:
        next_revision = registry.reserve_content_revision(document.document_id)
        assert manager.execute(
            EditableDocumentDraftCommand(
                controller,
                {"value": 1.0},
                {"value": 2.0},
                document.revision,
                next_revision,
                edit_key="value",
                description="Set value",
            )
        )
        assert resource.value == 2.0
        assert document.is_dirty
        assert execution.pending

        assert controller.flush_autosave()
        assert not document.is_dirty
        assert execution.saved == [{"value": 2.0}]

        manager.undo()
        assert resource.value == 1.0
        assert document.is_dirty
        assert controller.flush_autosave()
        assert not document.is_dirty
        assert execution.saved[-1] == {"value": 1.0}
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_explicit_resource_save_commits_only_its_captured_revision():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    resource = _Resource(4.0)
    execution = _ExecutionLayer()
    controller = EditableResourceDocumentController(
        "render_effect", "Assets/Bloom.effect", resource
    )
    document = registry.create(
        DocumentKind.RENDER_EFFECT,
        "Bloom.effect",
        resource_path="Assets/Bloom.effect",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path="Assets/Bloom.effect",
        resource=resource,
        exec_layer=execution,
        state=None,
    )
    registry.mark_changed(document.document_id)
    original_flush = execution.flush_rw_autosave

    def _flush_with_publication(*, force=False):
        flushed = original_flush(force=force)
        if flushed:
            registry.mark_changed(document.document_id)
        return flushed

    execution.flush_rw_autosave = _flush_with_publication
    try:
        result = registry.request_save(document.document_id)

        assert result.accepted
        assert execution.saved == [{"value": 4.0}]
        assert document.saved_revision == 1
        assert document.revision == 2
        assert document.is_dirty is True
    finally:
        DocumentRegistry._instance = previous_registry


def test_async_resource_save_stays_pending_until_io_succeeds():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    resource = _Resource(4.0)
    execution = _AsyncExecutionLayer()
    controller = EditableResourceDocumentController(
        "render_effect", "Assets/Bloom.effect", resource
    )
    document = registry.create(
        DocumentKind.RENDER_EFFECT,
        "Bloom.effect",
        resource_path="Assets/Bloom.effect",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path="Assets/Bloom.effect",
        resource=resource,
        exec_layer=execution,
        state=None,
    )
    registry.mark_changed(document.document_id)
    try:
        result = registry.request_save(document.document_id)

        assert result.status.value == "pending"
        assert document.is_dirty
        assert registry.active_save_ticket(document.document_id) is not None

        execution.tickets[-1].complete()
        assert registry.process_pending_saves() == 1
        assert registry.active_save_ticket(document.document_id) is None
        assert document.is_dirty is False
    finally:
        DocumentRegistry._instance = previous_registry


def test_async_resource_save_preserves_edits_made_after_submission():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    resource = _Resource(4.0)
    execution = _AsyncExecutionLayer()
    controller = EditableResourceDocumentController(
        "render_effect", "Assets/Bloom.effect", resource
    )
    document = registry.create(
        DocumentKind.RENDER_EFFECT,
        "Bloom.effect",
        resource_path="Assets/Bloom.effect",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path="Assets/Bloom.effect",
        resource=resource,
        exec_layer=execution,
        state=None,
    )
    saved_revision = registry.mark_changed(document.document_id)
    try:
        assert registry.request_save(document.document_id).status.value == "pending"
        resource.value = 8.0
        registry.mark_changed(document.document_id)

        execution.tickets[-1].complete()
        registry.process_pending_saves()

        assert document.saved_revision == saved_revision
        assert document.is_dirty
        assert controller._saved_document == {"value": 4.0}
    finally:
        DocumentRegistry._instance = previous_registry


def test_async_resource_save_failure_never_moves_save_point():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    resource = _Resource(4.0)
    execution = _AsyncExecutionLayer()
    controller = EditableResourceDocumentController(
        "render_effect", "Assets/Bloom.effect", resource
    )
    document = registry.create(
        DocumentKind.RENDER_EFFECT,
        "Bloom.effect",
        resource_path="Assets/Bloom.effect",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path="Assets/Bloom.effect",
        resource=resource,
        exec_layer=execution,
        state=None,
    )
    registry.mark_changed(document.document_id)
    try:
        assert registry.request_save(document.document_id).status.value == "pending"
        execution.tickets[-1].complete("failed")

        registry.process_pending_saves()

        assert registry.active_save_ticket(document.document_id) is None
        assert document.saved_revision == 0
        assert document.is_dirty
    finally:
        DocumentRegistry._instance = previous_registry


def test_background_async_autosave_marks_saved_only_after_io_completion():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    resource = _Resource(4.0)
    execution = _AsyncExecutionLayer()
    controller = EditableResourceDocumentController(
        "render_effect", "Assets/Bloom.effect", resource
    )
    document = registry.create(
        DocumentKind.RENDER_EFFECT,
        "Bloom.effect",
        resource_path="Assets/Bloom.effect",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path="Assets/Bloom.effect",
        resource=resource,
        exec_layer=execution,
        state=None,
    )
    registry.mark_changed(document.document_id)
    controller.schedule_autosave()
    try:
        assert controller.flush_autosave(force=True)
        assert document.is_dirty

        execution.tickets[-1].complete()
        registry.process_pending_saves()

        assert document.is_dirty is False
        assert controller._saved_document == {"value": 4.0}
    finally:
        DocumentRegistry._instance = previous_registry


def test_discard_supersedes_an_already_submitted_background_write():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    resource = _Resource(1.0)
    execution = _AsyncExecutionLayer()
    controller = EditableResourceDocumentController(
        "render_effect", "Assets/Bloom.effect", resource
    )
    document = registry.create(
        DocumentKind.RENDER_EFFECT,
        "Bloom.effect",
        resource_path="Assets/Bloom.effect",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path="Assets/Bloom.effect",
        resource=resource,
        exec_layer=execution,
        state=None,
    )
    resource.value = 9.0
    registry.mark_changed(document.document_id)
    controller.schedule_autosave()
    assert controller.flush_autosave(force=True)
    stale_ticket = execution.tickets[-1]
    try:
        result = registry.request_discard(document.document_id)

        assert result.accepted
        assert resource.value == 1.0
        assert document.is_dirty is False
        assert len(execution.tickets) == 2
        baseline_ticket = execution.tickets[-1]

        stale_ticket.complete("superseded")
        baseline_ticket.complete("succeeded")
        assert registry.process_pending_saves() == 2
        assert controller._saved_document == {"value": 1.0}
        assert document.revision == 0
        assert document.saved_revision == 0
    finally:
        DocumentRegistry._instance = previous_registry


def test_material_document_save_completes_from_native_document_store(tmp_path):
    from Infernux.core.assets import AssetManager
    from Infernux.core.document_store import DocumentStore
    from Infernux.engine.path_utils import path_key
    from Infernux.engine.ui.asset_execution_layer import (
        AssetAccessMode,
        AssetExecutionLayer,
    )

    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    path = tmp_path / "Surface.mat"

    class _MaterialResource(_Resource):
        file_path = str(path)

        def serialize(self):
            return json.dumps(self.serialize_document())

    resource = _MaterialResource(0.4)
    execution = AssetExecutionLayer(
        "material",
        str(path),
        AssetAccessMode.READ_WRITE_RESOURCE,
        autosave_debounce_sec=0.0,
    )
    controller = EditableResourceDocumentController("material", str(path), resource)
    document = registry.create(
        DocumentKind.MATERIAL,
        "Surface.mat",
        resource_path=str(path),
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path=str(path),
        resource=resource,
        exec_layer=execution,
        state=None,
    )
    registry.mark_changed(document.document_id)
    normalized = path_key(str(path))
    try:
        result = registry.request_save(document.document_id)

        assert result.status.value in {"pending", "applied"}
        if result.status.value == "pending":
            assert document.is_dirty
        else:
            assert path.exists()
            assert document.is_dirty is False

        DocumentStore.flush(str(path))
        AssetManager.poll_pending_asset_writes()
        registry.process_pending_saves()

        assert json.loads(path.read_text(encoding="utf-8")) == {"value": 0.4}
        assert document.is_dirty is False
        assert registry.active_save_ticket(document.document_id) is None
    finally:
        AssetManager._scheduled_saves.pop(str(path), None)
        AssetManager._material_save_snapshots.pop(normalized, None)
        AssetManager._pending_document_write_records.pop(normalized, None)
        DocumentRegistry._instance = previous_registry


def test_document_controller_claims_write_submitted_by_runtime_owner(tmp_path):
    from Infernux.core.assets import AssetManager
    from Infernux.core.document_store import DocumentStore
    from Infernux.engine.path_utils import path_key
    from Infernux.engine.ui.asset_execution_layer import (
        AssetAccessMode,
        AssetExecutionLayer,
    )

    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    path = tmp_path / "Shared.mat"

    class _MaterialResource(_Resource):
        file_path = str(path)

        def serialize(self):
            return json.dumps(self.serialize_document())

    resource = _MaterialResource(0.8)
    execution = AssetExecutionLayer(
        "material",
        str(path),
        AssetAccessMode.READ_WRITE_RESOURCE,
        autosave_debounce_sec=0.0,
    )
    controller = EditableResourceDocumentController("material", str(path), resource)
    document = registry.create(
        DocumentKind.MATERIAL,
        "Shared.mat",
        resource_path=str(path),
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path=str(path),
        resource=resource,
        exec_layer=execution,
        state=None,
    )
    registry.mark_changed(document.document_id)
    controller.schedule_autosave()
    normalized = path_key(str(path))
    try:
        runtime_ticket = AssetManager.flush_scheduled_saves(str(path), force=True)
        assert runtime_ticket is not False

        assert controller.flush_autosave(force=True)
        pending = controller._pending_writes.get(id(runtime_ticket))
        if pending is not None:
            assert pending.ticket is runtime_ticket
            assert document.is_dirty
        else:
            assert runtime_ticket.status == "succeeded"
            assert document.is_dirty is False

        DocumentStore.flush(str(path))
        AssetManager.poll_pending_asset_writes()
        registry.process_pending_saves()

        assert document.is_dirty is False
        assert json.loads(path.read_text(encoding="utf-8")) == {"value": 0.8}
    finally:
        AssetManager._scheduled_saves.pop(str(path), None)
        AssetManager._material_save_snapshots.pop(normalized, None)
        AssetManager._pending_document_write_records.pop(normalized, None)
        DocumentRegistry._instance = previous_registry


def test_editable_resource_controller_applies_user_document_transaction():
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    resource = _Resource()
    execution = _ExecutionLayer()
    state = SimpleNamespace(resource_controller=None, settings=resource)
    controller = EditableResourceDocumentController(
        "render_effect", "Assets/Bloom.effect", resource
    )
    document = registry.create(
        DocumentKind.RENDER_EFFECT,
        "Bloom.effect",
        resource_path="Assets/Bloom.effect",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    state.resource_controller = controller
    controller.bind(
        file_path="Assets/Bloom.effect",
        resource=resource,
        exec_layer=execution,
        state=state,
    )
    try:
        assert controller.apply_document(
            {"value": 3.0},
            view_id="inspector",
            edit_key="value",
            description="Set value",
        )
        assert resource.value == 3.0
        assert document.is_dirty
        assert execution.pending
        assert manager.action_journal.applied_entries()[-1].origin.value == "user"

        manager.undo()
        assert resource.value == 1.0
        assert document.is_dirty is False
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_editable_resource_controller_preserves_automation_origin():
    from Infernux.engine.interaction import ActionOrigin

    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    resource = _Resource()
    controller = EditableResourceDocumentController(
        "material", "Assets/Automation.mat", resource
    )
    document = registry.create(
        DocumentKind.MATERIAL,
        "Automation.mat",
        resource_path="Assets/Automation.mat",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    controller.bind(
        file_path=document.resource_path,
        resource=resource,
        exec_layer=_ExecutionLayer(),
        state=None,
    )
    try:
        assert controller.apply_document(
            {"value": 7.0},
            view_id="automation",
            edit_key="value",
            description="Set Material Value",
            origin=ActionOrigin.AUTOMATION,
        )
        entry = manager.action_journal.applied_entries()[-1]
        assert entry.origin is ActionOrigin.AUTOMATION
        assert resource.value == 7.0
        manager.undo()
        assert resource.value == 1.0
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_editable_resource_discard_restores_last_durable_document():
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    resource = _Resource()
    execution = _ExecutionLayer()
    state = SimpleNamespace(resource_controller=None, settings=resource)
    controller = EditableResourceDocumentController(
        "physic_material", "Assets/Ice.physmat", resource
    )
    document = registry.create(
        DocumentKind.PHYSIC_MATERIAL,
        "Ice.physmat",
        resource_path="Assets/Ice.physmat",
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )
    controller.document_id = document.document_id
    state.resource_controller = controller
    controller.bind(
        file_path="Assets/Ice.physmat",
        resource=resource,
        exec_layer=execution,
        state=state,
    )
    try:
        revision = registry.reserve_content_revision(document.document_id)
        controller.restore_document({"value": 9.0}, revision, persist=False)
        assert document.is_dirty

        result = registry.request_discard(document.document_id)
        assert result.accepted
        assert resource.value == 1.0
        assert not document.is_dirty
    finally:
        DocumentRegistry._instance = previous_registry


def test_animation_clip_documents_replace_state_without_losing_asset_identity():
    from Infernux.core.animation_clip import AnimationClip, AnimationFrame
    from Infernux.core.animation_clip3d import AnimationClip3D

    clip_2d = AnimationClip(
        name="Run",
        authoring_texture_guid="texture-guid",
        frames=[
            AnimationFrame(sprite_frame_id=f"{index + 1:032x}")
            for index in (0, 1)
        ],
        fps=12.0,
    )
    clip_2d.file_path = "Assets/Run.animclip2d"
    document_2d = clip_2d.serialize_document()
    document_2d["fps"] = 24.0
    assert clip_2d.deserialize_document(document_2d)
    assert clip_2d.fps == 24.0
    assert clip_2d.file_path == "Assets/Run.animclip2d"
    assert not clip_2d.deserialize_document({"fps": 60.0})

    clip_3d = AnimationClip3D(
        name="Walk",
        source_model_guid="model-guid",
        take_name="Walk",
    )
    clip_3d.file_path = "Assets/Walk.animclip3d"
    document_3d = clip_3d.serialize_document()
    document_3d["take_name"] = "Run"
    assert clip_3d.deserialize_document(document_3d)
    assert clip_3d.take_name == "Run"
    assert clip_3d.file_path == "Assets/Walk.animclip3d"
    assert not clip_3d.deserialize_document({"take_name": "Idle"})


def test_animation_clip_asset_inspector_binds_the_shared_document_controller():
    from Infernux.core.animation_clip import AnimationClip, AnimationFrame
    from Infernux.engine.ui.asset_details_renderer import (
        AssetCategoryDef,
        _State,
        _bind_editable_resource_document,
    )
    from Infernux.engine.ui.asset_execution_layer import AssetAccessMode

    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    clip = AnimationClip(
        name="Run",
        frames=[
            AnimationFrame(sprite_frame_id=f"{index + 1:032x}")
            for index in (0, 1)
        ],
        fps=12.0,
    )
    clip.file_path = "Assets/Run.animclip2d"
    state = _State()
    state.category = "animclip"
    state.file_path = clip.file_path
    state.settings = clip
    state.meta = {"guid": "clip-guid"}
    state.exec_layer = _ExecutionLayer()
    category = AssetCategoryDef(
        display_name="Animation Clip",
        access_mode=AssetAccessMode.READ_WRITE_RESOURCE,
        load_fn=lambda _path: None,
        autosave_debounce=0.5,
    )
    try:
        _bind_editable_resource_document(state, category)

        document = registry.require(state.document_id)
        assert document.kind is DocumentKind.ANIMATION_CLIP
        assert document.key == DocumentKey.asset(
            DocumentKind.ANIMATION_CLIP, "clip-guid"
        )
        assert state.resource_controller.resource is clip
        assert document.view_ids == {"inspector"}
    finally:
        DocumentRegistry._instance = previous_registry


def test_material_asset_and_inline_inspectors_share_one_native_document():
    from Infernux.engine.ui.asset_details_renderer import (
        AssetCategoryDef,
        _State,
        _bind_editable_resource_document,
    )
    from Infernux.engine.ui.asset_execution_layer import AssetAccessMode
    from Infernux.engine.ui.inspector_material import _build_inline_state

    class _NativeMaterial:
        guid = "material-guid"
        file_path = "Assets/Test.mat"
        is_builtin = False

        def __init__(self):
            self.document = {"properties": {"roughness": {"value": 0.5}}}

        def get_version(self):
            return 1

        def serialize_document(self):
            return self.document

        def deserialize_document(self, document):
            self.document = document
            return True

    class _MaterialWrapper:
        def __init__(self, native):
            self.native = native

        def serialize_document(self):
            return self.native.serialize_document()

        def deserialize_document(self, document):
            return self.native.deserialize_document(document)

    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    native = _NativeMaterial()
    wrapper = _MaterialWrapper(native)
    state = _State()
    state.category = "material"
    state.file_path = native.file_path
    state.settings = wrapper
    state.meta = {"guid": native.guid}
    state.extra = {"native_mat": native}
    state.exec_layer = _ExecutionLayer()
    category = AssetCategoryDef(
        display_name="Material",
        access_mode=AssetAccessMode.READ_WRITE_RESOURCE,
        load_fn=lambda _path: None,
        autosave_debounce=0.35,
    )
    try:
        _bind_editable_resource_document(state, category)
        asset_controller = state.resource_controller
        inline_state = _build_inline_state(SimpleNamespace(), native)

        document = registry.require(state.document_id)
        assert document.kind is DocumentKind.MATERIAL
        assert document.key == DocumentKey.asset(DocumentKind.MATERIAL, native.guid)
        assert asset_controller.resource is native
        assert state.settings is wrapper
        assert inline_state.resource_controller is asset_controller
        assert inline_state.document_id == state.document_id
        assert document.view_ids == {"inspector"}
    finally:
        DocumentRegistry._instance = previous_registry
