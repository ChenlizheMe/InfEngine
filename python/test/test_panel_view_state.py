from __future__ import annotations

from pathlib import Path

import pytest

from Infernux.engine.interaction import PanelViewStateField, PanelViewStateSchema
from Infernux.engine.ui.editor_panel import EditorPanel


class _UnpersistedPanel(EditorPanel):
    def __init__(self) -> None:
        super().__init__("Unpersisted", window_id="unpersisted")
        self._selected_item = "selection-is-global"
        self._dirty = True
        self._runtime_cache = {"expensive": object()}


class _DeclaredPanel(EditorPanel):
    VIEW_STATE_SCHEMA = PanelViewStateSchema(
        "test.declared",
        (
            PanelViewStateField("zoom", "zoom", float),
            PanelViewStateField("show_grid", "show_grid", bool),
            PanelViewStateField("pan_x", "view.pan_x", float),
        ),
    )

    def __init__(self) -> None:
        super().__init__("Declared", window_id="declared")
        self.zoom = 1.0
        self.show_grid = True
        self.view = type("View", (), {"pan_x": 0.0})()
        self._selected_item = "selection-is-global"
        self._runtime_cache = object()


def test_panel_without_schema_does_not_persist_arbitrary_instance_fields() -> None:
    panel = _UnpersistedPanel()

    assert panel.save_state() == {}
    with pytest.raises(ValueError, match="does not declare persisted view state"):
        panel.load_state({"selected_item": "must-not-restore"})


def test_declared_panel_view_state_round_trips_only_allow_listed_fields() -> None:
    panel = _DeclaredPanel()
    panel.zoom = 2.5
    panel.show_grid = False

    state = panel.save_state()

    assert state == {
        "schema": "test.declared",
        "values": {"zoom": 2.5, "show_grid": False, "pan_x": 0.0},
    }
    panel.zoom = 9.0
    panel.show_grid = True
    panel.view.pan_x = 4.0
    panel._selected_item = "new-selection"
    panel.load_state(state)
    assert panel.zoom == 2.5
    assert panel.show_grid is False
    assert panel.view.pan_x == 0.0
    assert panel._selected_item == "new-selection"


@pytest.mark.parametrize(
    "state",
    [
        {"schema": "test.declared", "version": 1, "values": {"zoom": 1.0}},
        {
            "schema": "test.declared",
            "values": {"zoom": 1.0, "show_grid": True, "dirty": True},
        },
    ],
)
def test_declared_panel_view_state_rejects_stale_or_unknown_shape(state: dict) -> None:
    with pytest.raises(ValueError):
        _DeclaredPanel().load_state(state)


def test_declared_panel_view_state_rejects_nonfinite_and_runtime_values() -> None:
    panel = _DeclaredPanel()
    panel.zoom = float("nan")
    with pytest.raises(ValueError, match="finite"):
        panel.save_state()

    runtime_schema = PanelViewStateSchema(
        "test.runtime",
        (PanelViewStateField("runtime", "runtime", object),),
    )
    panel.runtime = object()
    with pytest.raises(TypeError, match="cannot store runtime value"):
        runtime_schema.capture(panel)


def test_editor_panel_has_no_heuristic_instance_state_persistence() -> None:
    source = Path(__file__).parents[1] / "Infernux" / "engine" / "ui" / "editor_panel.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in ("__auto_state__", "_collect_auto_state", "_AUTO_STATE_SKIP"):
        assert forbidden not in text


def test_authoring_panels_never_persist_document_payload_in_panel_state() -> None:
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.animclip2d_editor_panel import AnimClip2DEditorPanel
    from Infernux.engine.ui.animfsm_editor_panel import AnimFSMEditorPanel
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    DocumentRegistry()
    panels = (
        ParticleGraphEditorPanel(),
        AnimFSMEditorPanel(),
        AnimTimelineEditorPanel(),
        AnimClip2DEditorPanel(),
    )
    forbidden = {
        "draft",
        "dirty",
        "file_path",
        "resource_path",
        "timeline",
        "fsm",
        "asset",
        "clips",
        "texture_path",
    }

    def keys(value):
        if isinstance(value, dict):
            yield from value.keys()
            for item in value.values():
                yield from keys(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from keys(item)

    for panel in panels:
        state = panel.save_state()
        assert forbidden.isdisjoint(set(keys(state))), type(panel).__name__

    session = DocumentRegistry.instance().capture_session_state()
    assert len(session["documents"]) == 4
    assert all(record["restore_state"] is not None for record in session["documents"])


def test_panel_persistence_only_publishes_its_view_state(monkeypatch, tmp_path) -> None:
    from Infernux.engine.ui import panel_state

    panel_state.init(str(tmp_path / "layout"))
    sentinel_session = {"documents": [{"document_id": "owned-by-bootstrap"}]}
    panel_state.put("document_session", sentinel_session)

    save_calls = []
    monkeypatch.setattr(panel_state, "save", lambda: save_calls.append(True))

    panel = _DeclaredPanel()
    panel.zoom = 3.0
    panel.show_grid = False
    panel._persist_panel_state()

    assert panel_state.get("panel:declared") == panel.save_state()
    assert panel_state.get("document_session") == sentinel_session
    assert save_calls == []


def test_editor_bootstrap_owns_document_session_and_disk_flush(monkeypatch, tmp_path) -> None:
    from Infernux.engine.bootstrap import EditorBootstrap
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui import panel_state

    panel_state.init(str(tmp_path / "layout"))
    panel = _DeclaredPanel()
    panel.zoom = 2.0

    class Console:
        show_info = True
        show_warnings = True
        show_errors = True
        collapse = False
        clear_on_play = True
        error_pause = False
        auto_scroll = True

    class ProjectPanel:
        @staticmethod
        def get_current_path():
            return "Assets"

    class WindowManager:
        _default_instances = {"declared": panel}
        _window_instances = {}

        @staticmethod
        def save_state():
            return {"windows": []}

        @staticmethod
        def is_document_backed_view(_view_id, _type_id=""):
            return False

        @staticmethod
        def window_type_id(view_id):
            return view_id

    session = {"documents": [{"document_id": "authoring-document"}]}
    registry = DocumentRegistry()
    capture_calls = []
    monkeypatch.setattr(
        registry,
        "capture_session_state",
        lambda: capture_calls.append(True) or session,
    )
    save_calls = []
    monkeypatch.setattr(panel_state, "save", lambda: save_calls.append(True))

    bootstrap = EditorBootstrap.__new__(EditorBootstrap)
    bootstrap._suspend_persist_state = False
    bootstrap.console = Console()
    bootstrap.project_panel = ProjectPanel()
    bootstrap.window_manager = WindowManager()
    bootstrap.toolbar = None
    bootstrap.scene_file_manager = None

    bootstrap._persist_editor_state(include_scene_draft=True)

    assert capture_calls == [True]
    assert panel_state.get("document_session") == session
    assert panel_state.get("panel:declared") == panel.save_state()
    assert save_calls == [True]


def test_panel_state_rejects_invalid_persisted_document(tmp_path):
    from Infernux.engine.ui import panel_state

    layout = tmp_path / "layout"
    layout.mkdir()
    (layout / "panel_state.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must contain a JSON object"):
        panel_state.init(str(layout))


def test_panel_state_save_exposes_document_store_failure(tmp_path, monkeypatch):
    from Infernux.core import document_store
    from Infernux.engine.ui import panel_state

    panel_state.init(str(tmp_path / "layout"))
    panel_state.put("panel:console", {"filter": "error"})

    def reject_write(*_args, **_kwargs):
        raise RuntimeError("document store unavailable")

    monkeypatch.setattr(document_store, "write_document_text", reject_write)

    with pytest.raises(RuntimeError, match="document store unavailable"):
        panel_state.save()
