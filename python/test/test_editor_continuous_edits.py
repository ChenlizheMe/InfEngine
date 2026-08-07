from types import SimpleNamespace

from Infernux.engine.interaction import (
    ContinuousEditService,
    DocumentKind,
    DocumentRegistry,
    EditorInteractionCore,
    SelectionDomain,
    SelectionTarget,
    ensure_editable_resource_document,
)


def test_continuous_edit_commits_or_rolls_back_exactly_once():
    service = ContinuousEditService()
    committed = []
    cancelled = []

    service.begin(
        "inspector:value",
        owner_id="inspector",
        description="Edit Value",
        initial_value=1.0,
        on_commit=lambda session: committed.append(
            (session.initial_value, session.current_value)
        ),
        on_cancel=lambda session: cancelled.append(session.initial_value),
    )
    assert service.update("inspector:value", 3.0)
    assert service.commit("inspector:value") is True
    assert service.commit("inspector:value") is False
    assert committed == [(1.0, 3.0)]

    service.begin(
        "inspector:cancelled",
        owner_id="inspector",
        description="Edit Value",
        initial_value=4.0,
        on_cancel=lambda session: cancelled.append(session.initial_value),
    )
    assert service.update("inspector:cancelled", 8.0)
    assert service.cancel_owner("inspector") == 1
    assert cancelled == [4.0]
    assert service.active_count == 0


def test_continuous_edit_rolls_back_when_commit_is_rejected_or_raises():
    service = ContinuousEditService()
    cancelled = []

    for key, callback in (
        ("rejected", lambda _session: False),
        ("failed", lambda _session: (_ for _ in ()).throw(RuntimeError("boom"))),
    ):
        service.begin(
            key,
            owner_id="inspector",
            description="Edit Value",
            initial_value=1.0,
            on_commit=callback,
            on_cancel=lambda session: cancelled.append(
                (session.key, session.initial_value)
            ),
        )
        assert service.update(key, 2.0)
        assert service.commit(key) is False

    assert cancelled == [("rejected", 1.0), ("failed", 1.0)]


def test_continuous_edit_idle_commit_ignores_render_frames_between_inputs():
    service = ContinuousEditService()
    committed = []
    session = service.begin(
        "inspector:number",
        owner_id="inspector",
        description="Edit Number",
        initial_value=2100,
        on_commit=lambda item: committed.append(
            (item.initial_value, item.current_value)
        ),
    )
    service.update(session.key, 220)
    assert service.commit_if_idle(session.key, idle_seconds=0.75) is False
    service.update(session.key, 2200)
    session.last_update_at -= 1.0
    assert service.commit_if_idle(session.key, idle_seconds=0.75) is True
    assert committed == [(2100, 2200)]


def test_interaction_core_commits_edits_on_selection_and_panel_transitions():
    core = EditorInteractionCore()
    commits = []
    try:
        core.panels.register_selection_authority(
            "project",
            (SelectionDomain.ASSET,),
        )
        core.continuous_edits.begin(
            "material:roughness",
            owner_id="inspector",
            description="Edit Material",
            initial_value=0.2,
            on_commit=lambda session: commits.append(session.key),
        )
        core.continuous_edits.update("material:roughness", 0.8)
        core.selection.select(
            SelectionTarget.asset("Assets/Other.mat"),
            owner_id="project",
            record_history=False,
        )
        assert commits == ["material:roughness"]

        core.focus.activate_panel("animtimeline_editor", record_history=False)
        core.continuous_edits.begin(
            "timeline:key.time",
            owner_id="animtimeline_editor",
            description="Move Key",
            initial_value=1.0,
            on_commit=lambda session: commits.append(session.key),
        )
        core.continuous_edits.update("timeline:key.time", 2.0)
        core.focus.activate_panel("project", record_history=False)
        assert commits == ["material:roughness", "timeline:key.time"]
    finally:
        core.shutdown()


def test_material_continuous_edit_publishes_one_document_command():
    from Infernux.engine.ui.inspector_material import _update_material_edit_session
    from Infernux.engine.undo import UndoManager

    class _Material:
        guid = "material-guid"
        file_path = "Assets/Test.mat"

        def __init__(self):
            self.document = {"properties": {"roughness": {"value": 0.8}}}

        def deserialize_document(self, document):
            self.document = document
            return True

        def serialize_document(self):
            return self.document

        def save(self):
            return True

    previous_manager = UndoManager.instance()
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    edits = ContinuousEditService()
    material = _Material()
    old_document = {"properties": {"roughness": {"value": 0.2}}}
    new_document = {"properties": {"roughness": {"value": 0.8}}}
    state = SimpleNamespace(
        file_path=material.file_path,
        extra={"cached_data": new_document, "cached_json": ""},
        resource_controller=None,
        document_id="",
    )
    controller = ensure_editable_resource_document(
        category="material",
        document_kind=DocumentKind.MATERIAL,
        file_path=material.file_path,
        resource=material,
        guid=material.guid,
    )
    state.resource_controller = controller
    state.document_id = controller.document_id

    try:
        _update_material_edit_session(
            None,
            state,
            material,
            old_document,
            new_document,
            "property.roughness",
        )
        assert manager.action_journal.entries == ()
        assert edits.commit_owner("inspector") == 1
        assert len(manager.action_journal.entries) == 1
        assert registry.require(controller.document_id).revision == 1

        manager.undo()
        assert material.document == old_document
        manager.redo()
        assert material.document == new_document
    finally:
        manager.clear()
        edits.clear(commit=False)
        UndoManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry


def test_material_structural_text_edit_does_not_record_intermediate_values():
    from Infernux.engine.ui.inspector_material import (
        _apply_material_changes,
        _flush_deferred_undo,
        _material_edit_session_key,
    )
    from Infernux.engine.undo import UndoManager

    class _Material:
        guid = "material-guid"
        file_path = "Assets/Test.mat"

        def __init__(self):
            self.document = {"renderState": {"renderQueue": 2100}}

        def deserialize_document(self, document):
            self.document = document
            return True

        def serialize_document(self):
            return self.document

        def save(self):
            return True

    previous_manager = UndoManager.instance()
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    edits = ContinuousEditService()
    material = _Material()
    old_document = {"renderState": {"renderQueue": 2100}}
    live_document = {"renderState": {"renderQueue": 220}}
    state = SimpleNamespace(
        file_path=material.file_path,
        extra={
            "cached_data": live_document,
            "cached_json": '{"renderState":{"renderQueue":2100}}',
        },
        resource_controller=None,
        document_id="",
    )
    controller = ensure_editable_resource_document(
        category="material",
        document_kind=DocumentKind.MATERIAL,
        file_path=material.file_path,
        resource=material,
        guid=material.guid,
    )
    state.resource_controller = controller
    state.document_id = controller.document_id

    try:
        _apply_material_changes(
            None,
            state,
            live_document,
            material,
            True,
            True,
            old_document,
            "render_state.render_queue",
            None,
        )
        live_document["renderState"]["renderQueue"] = 2200
        _apply_material_changes(
            None,
            state,
            live_document,
            material,
            True,
            True,
            old_document,
            "render_state.render_queue",
            None,
        )

        assert material.document["renderState"]["renderQueue"] == 2200
        assert manager.action_journal.entries == ()
        session = edits.get(_material_edit_session_key(state, material))
        assert session is not None
        session.last_update_at -= 1.0
        _flush_deferred_undo(
            None,
            state,
            live_document,
            material,
            input_active=True,
        )
        assert manager.action_journal.entries == ()
        _flush_deferred_undo(None, state, live_document, material)
        assert len(manager.action_journal.entries) == 1

        manager.undo()
        assert material.document["renderState"]["renderQueue"] == 2100
        manager.redo()
        assert material.document["renderState"]["renderQueue"] == 2200
    finally:
        manager.clear()
        edits.clear(commit=False)
        UndoManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry
