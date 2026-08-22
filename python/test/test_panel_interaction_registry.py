from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from Infernux.engine.interaction import (
    BoundPanelCommand,
    CommandSource,
    EditorInteractionCore,
    KeyChord,
    PanelCommandAdapter,
    PanelCommandSpec,
    PanelInteractionDescriptor,
    PanelInteractionRegistry,
    PanelShortcutSpec,
    SelectionDomain,
    SelectionTarget,
    ShortcutPhase,
    ShortcutScope,
)
from Infernux.engine.ui.panel_registry import PanelRegistry, _PanelRegistration


def _descriptor(calls: list[str]) -> PanelInteractionDescriptor:
    def bind(instance) -> PanelCommandAdapter:
        return PanelCommandAdapter(
            {
                "test.run": BoundPanelCommand(
                    lambda context: calls.append(
                        f"{instance.name}:{context.payload.get('value', '')}"
                    )
                    or True,
                    lambda _context: bool(instance.enabled),
                )
            }
        )

    return PanelInteractionDescriptor(
        commands=(PanelCommandSpec("test.run"),),
        shortcuts=(
            PanelShortcutSpec(
                "test.run",
                KeyChord.parse("Ctrl+R"),
                phase=ShortcutPhase.REPEAT,
                priority=7,
                allow_when_text_input=True,
            ),
        ),
        adapter_factory=bind,
    )


def test_panel_descriptor_rejects_duplicate_and_unknown_contract_entries():
    with pytest.raises(ValueError, match="command ids must be unique"):
        PanelInteractionDescriptor(
            commands=(PanelCommandSpec("test.run"), PanelCommandSpec("test.run")),
            adapter_factory=lambda _panel: PanelCommandAdapter({}),
        )

    with pytest.raises(ValueError, match="unknown commands"):
        PanelInteractionDescriptor(
            shortcuts=(PanelShortcutSpec("test.run", KeyChord.parse("R")),),
        )


def test_panel_binding_rejects_an_incomplete_adapter():
    descriptor = PanelInteractionDescriptor(
        commands=(PanelCommandSpec("test.run"),),
        adapter_factory=lambda _panel: PanelCommandAdapter({}),
    )
    core = EditorInteractionCore()
    core.panels.register_type("test_panel", descriptor)

    with pytest.raises(ValueError, match="missing=.*test.run"):
        core.panels.bind_view("test/1", "test_panel", object())

    core.shutdown()


def test_panel_commands_route_by_active_view_and_unbind_closed_views():
    class Panel:
        def __init__(self, name: str, enabled: bool = True) -> None:
            self.name = name
            self.enabled = enabled

    calls: list[str] = []
    core = EditorInteractionCore()
    core.panels.register_type("test_panel", _descriptor(calls))
    core.panels.bind_view("test/left", "test_panel", Panel("left"))
    core.panels.bind_view("test/right", "test_panel", Panel("right"))

    core.focus.activate_panel("test_panel", view_id="test/right")
    context = core.commands.context(CommandSource.API, payload={"value": "ok"})
    assert core.panels.can_execute_active(context, "test.run")
    assert core.panels.execute_active(context, "test.run")
    assert calls == ["right:ok"]

    assert core.panels.unbind_view("test/right")
    assert not core.panels.can_execute_active(context, "test.run")
    assert calls == ["right:ok"]
    core.shutdown()


def test_cross_panel_command_routes_to_destination_without_changing_focus():
    class Panel:
        def __init__(self, name: str) -> None:
            self.name = name
            self.enabled = True

    calls: list[str] = []
    core = EditorInteractionCore()
    core.panels.register_type("test_panel", _descriptor(calls))
    core.panels.bind_view("test/destination", "test_panel", Panel("destination"))
    core.focus.activate_panel("project", view_id="project")
    context = core.commands.context(CommandSource.DRAG_DROP, {"value": "asset"})

    assert core.panels.owns_view("test/destination", "test.run")
    assert core.panels.can_execute_view("test/destination", context, "test.run")
    assert core.panels.execute_view("test/destination", context, "test.run")
    assert calls == ["destination:asset"]
    assert core.focus.snapshot.active_view_id == "project"
    core.shutdown()


def test_replacing_a_descriptor_rebinds_all_live_views_transactionally():
    class Panel:
        def __init__(self, name: str) -> None:
            self.name = name
            self.enabled = True

    calls: list[str] = []
    core = EditorInteractionCore()
    core.panels.register_type("test_panel", _descriptor(calls))
    core.panels.bind_view("test/left", "test_panel", Panel("left"))
    core.panels.bind_view("test/right", "test_panel", Panel("right"))

    replacement_calls: list[str] = []
    core.panels.register_type(
        "test_panel",
        _descriptor(replacement_calls),
        replace=True,
    )

    core.focus.activate_panel("test_panel", view_id="test/left")
    context = core.commands.context(CommandSource.API, payload={"value": "new"})
    assert core.panels.execute_active(context, "test.run")
    core.focus.activate_panel("test_panel", view_id="test/right")
    context = core.commands.context(CommandSource.API, payload={"value": "new"})
    assert core.panels.execute_active(context, "test.run")
    assert calls == []
    assert replacement_calls == ["left:new", "right:new"]
    core.shutdown()


def test_failed_descriptor_replacement_keeps_the_previous_live_bindings():
    class Panel:
        name = "stable"
        enabled = True

    calls: list[str] = []
    core = EditorInteractionCore()
    core.panels.register_type("test_panel", _descriptor(calls))
    core.panels.bind_view("test/view", "test_panel", Panel())
    broken = PanelInteractionDescriptor(
        commands=(PanelCommandSpec("test.run"),),
        adapter_factory=lambda _panel: PanelCommandAdapter({}),
    )

    with pytest.raises(ValueError, match="missing=.*test.run"):
        core.panels.register_type("test_panel", broken, replace=True)

    core.focus.activate_panel("test_panel", view_id="test/view")
    context = core.commands.context(CommandSource.API, payload={"value": "old"})
    assert core.panels.execute_active(context, "test.run")
    assert calls == ["stable:old"]
    core.shutdown()


def test_panel_shortcut_metadata_projects_without_bootstrap_panel_branches():
    calls: list[str] = []
    core = EditorInteractionCore()
    core.panels.register_type("test_panel", _descriptor(calls))

    (binding,) = tuple(core.panels.iter_shortcut_bindings())
    assert binding.command_id == "test.run"
    assert binding.scope is ShortcutScope.PANEL
    assert binding.owner_id == "test_panel"
    assert binding.phase is ShortcutPhase.REPEAT
    assert binding.priority == 7
    assert binding.allow_when_text_input
    core.shutdown()


def test_panel_registry_rejects_declared_commands_missing_from_global_registry():
    registry = PanelInteractionRegistry()
    registry.register_type("test_panel", _descriptor([]))

    with pytest.raises(RuntimeError, match="test.run"):
        registry.require_registered_commands(())

    registry.require_registered_commands(("test.run",))


def test_panel_shortcut_identity_does_not_depend_on_the_default_chord():
    first = PanelInteractionRegistry()
    second = PanelInteractionRegistry()
    first.register_type(
        "sample",
        PanelInteractionDescriptor(
            commands=(PanelCommandSpec("sample.run"),),
            shortcuts=(
                PanelShortcutSpec("sample.run", KeyChord.parse("Ctrl+R")),
            ),
            adapter_factory=lambda _panel: PanelCommandAdapter(
                {
                    "sample.run": BoundPanelCommand(
                        lambda _context: True,
                        lambda _context: True,
                    )
                }
            ),
        ),
    )
    second.register_type(
        "sample",
        PanelInteractionDescriptor(
            commands=(PanelCommandSpec("sample.run"),),
            shortcuts=(
                PanelShortcutSpec("sample.run", KeyChord.parse("Alt+R")),
            ),
            adapter_factory=lambda _panel: PanelCommandAdapter(
                {
                    "sample.run": BoundPanelCommand(
                        lambda _context: True,
                        lambda _context: True,
                    )
                }
            ),
        ),
    )

    first_binding = next(first.iter_shortcut_bindings())
    second_binding = next(second.iter_shortcut_bindings())
    assert first_binding.binding_id == second_binding.binding_id
    assert first_binding.chord != second_binding.chord


def test_panel_selection_ownership_is_fail_closed_with_explicit_core_authority():
    core = EditorInteractionCore()
    core.panels.register_type(
        "asset_panel",
        PanelInteractionDescriptor(
            owned_selection_domains=frozenset({SelectionDomain.ASSET}),
        ),
    )
    core.panels.bind_view("asset/left", "asset_panel", object())

    assert core.selection.select(
        SelectionTarget.asset("Assets/Allowed.mat"),
        owner_id="asset/left",
    )
    with pytest.raises(ValueError, match="does not declare.*scene_object"):
        core.selection.select(
            SelectionTarget.scene_object(42),
            owner_id="asset/left",
        )

    with pytest.raises(ValueError, match="legacy_service"):
        core.selection.select(
            SelectionTarget.scene_object(42),
            owner_id="legacy_service",
        )

    core.panels.register_selection_authority(
        "test_automation",
        (SelectionDomain.SCENE_OBJECT,),
    )
    assert core.selection.select(
        SelectionTarget.scene_object(42),
        owner_id="test_automation",
    )
    core.shutdown()


def test_panel_focus_history_policy_resolves_by_live_view_or_type():
    core = EditorInteractionCore()
    core.panels.register_type(
        "passive_panel",
        PanelInteractionDescriptor(records_focus_history=False),
    )
    core.panels.bind_view("passive/one", "passive_panel", object())

    assert not core.panels.records_focus_history(view_id="passive/one")
    assert not core.panels.records_focus_history(type_id="passive_panel")
    assert not core.panels.records_focus_history(type_id="unregistered_panel")
    core.shutdown()


def test_panel_view_binding_rejects_unregistered_panel_types():
    core = EditorInteractionCore()
    try:
        with pytest.raises(KeyError, match="not registered: missing_panel"):
            core.panels.bind_view("missing/one", "missing_panel", object())
    finally:
        core.shutdown()


def test_panel_registry_can_require_a_complete_surface_manifest():
    core = EditorInteractionCore()
    try:
        core.panels.register_type("scene_view", PanelInteractionDescriptor())
        core.panels.register_type("toolbar", PanelInteractionDescriptor())

        core.panels.require_types(("scene_view", "toolbar"))
        with pytest.raises(RuntimeError, match="missing_surface"):
            core.panels.require_types(("scene_view", "missing_surface"))
    finally:
        core.shutdown()


def test_bootstrap_surface_manifest_covers_all_permanent_editor_chrome():
    from Infernux.engine._bootstrap_panels import (
        BUILTIN_EDITOR_WINDOW_TYPE_IDS,
        NATIVE_BUILTIN_WINDOW_TYPES,
        PERMANENT_EDITOR_WINDOW_TYPE_IDS,
        PERMANENT_EDITOR_SURFACE_TYPE_IDS,
    )

    assert BUILTIN_EDITOR_WINDOW_TYPE_IDS == {
        "toolbar",
        "hierarchy",
        "inspector",
        "project",
        "console",
        "scene_view",
        "game_view",
        "ui_editor",
    }
    native = {spec.type_id: spec for spec in NATIVE_BUILTIN_WINDOW_TYPES}
    assert set(native) == {"toolbar", "hierarchy", "inspector", "project", "console"}
    assert native["hierarchy"].menu_path == "Window"
    assert native["hierarchy"].user_closable
    assert native["toolbar"].menu_path == ""
    assert not native["toolbar"].user_closable
    assert PERMANENT_EDITOR_WINDOW_TYPE_IDS == {"toolbar"}
    assert PERMANENT_EDITOR_SURFACE_TYPE_IDS == {"menu_bar", "status_bar"}


def test_native_builtin_manifest_registers_hierarchy_and_toolbar_as_window_types(
    monkeypatch,
):
    import Infernux.engine._bootstrap_panels as bootstrap_panels

    registrations = []
    manager = type(
        "Manager",
        (),
        {"register_window_type": lambda _self, **kwargs: registrations.append(kwargs)},
    )()
    bootstrap = type(
        "Bootstrap",
        (),
        {
            spec.factory_name: (lambda _self, _type_id=spec.type_id: _type_id)
            for spec in bootstrap_panels.NATIVE_BUILTIN_WINDOW_TYPES
        },
    )()
    monkeypatch.setattr(
        bootstrap_panels,
        "_native_builtin_type",
        lambda spec: type(f"{spec.display_name}Panel", (), {}),
    )

    bootstrap_panels.register_native_builtin_window_types(bootstrap, manager)

    by_type = {item["type_id"]: item for item in registrations}
    assert set(by_type) == {
        "toolbar",
        "hierarchy",
        "inspector",
        "project",
        "console",
    }
    assert by_type["hierarchy"]["factory"]() == "hierarchy"
    assert by_type["toolbar"]["factory"]() == "toolbar"
    assert by_type["toolbar"]["menu_path"] == ""


def test_builtin_windows_cannot_bypass_window_manager_registration():
    from Infernux.engine._bootstrap_panels import (
        BUILTIN_EDITOR_WINDOW_TYPE_IDS,
        BootstrapPanelsMixin,
    )

    tree = ast.parse(textwrap.dedent(inspect.getsource(BootstrapPanelsMixin._create_panels)))
    managed: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        name = node.args[0].value
        if not isinstance(name, str):
            continue
        if node.func.attr == "register_existing_window":
            managed.add(name)
        elif node.func.attr == "register_gui":
            direct.add(name)

    assert BUILTIN_EDITOR_WINDOW_TYPE_IDS <= managed
    assert BUILTIN_EDITOR_WINDOW_TYPE_IDS.isdisjoint(direct)


def test_toolbar_manifest_rejects_native_title_bar_close_intent():
    from Infernux.engine._bootstrap_panels import (
        enforce_native_builtin_window_policy,
    )

    panel = type("Panel", (), {})()
    panel.on_request_close = lambda: True

    enforce_native_builtin_window_policy("toolbar", panel)

    assert panel.on_request_close() is False


def test_toolbar_descriptor_routes_view_commands_to_scene_view():
    from Infernux.engine.ui.core_panel_interactions import (
        toolbar_panel_interaction,
    )

    descriptor = toolbar_panel_interaction()

    assert not descriptor.records_focus_history
    assert descriptor.view_command_target_id == "scene_view"


def test_all_builtin_decorated_panels_are_in_the_interaction_matrix():
    # Import every built-in panel explicitly. Infernux.engine.ui exposes these
    # lazily, so importing only PanelRegistry would produce an empty inventory.
    from Infernux.engine.ui import (  # noqa: F401
        AnimClip2DEditorPanel,
        AnimFSMEditorPanel,
        AnimTimelineEditorPanel,
        BuildSettingsPanel,
        EnvironmentSettingsPanel,
        GameViewPanel,
        HistoryPanel,
        ParticleGraphEditorPanel,
        PhysicsLayerMatrixPanel,
        PreferencesPanel,
        SceneViewPanel,
        TagLayerSettingsPanel,
        UIEditorPanel,
    )
    from Infernux.engine._bootstrap_panels import BUILTIN_EDITOR_WINDOW_TYPE_IDS

    registrations = PanelRegistry.get_registrations()
    by_type = {registration.type_id: registration for registration in registrations}
    assert len(by_type) == len(registrations)
    assert set(by_type) == {
        "animclip2d_editor",
        "animfsm_editor",
        "animtimeline_editor",
        "build_settings",
        "environment_settings",
        "game_view",
        "history",
        "particle_graph_editor",
        "physics_settings",
        "preferences",
        "scene_view",
        "tag_layer_settings",
        "ui_editor",
    }

    pre_registered = {"scene_view", "ui_editor"}
    assert pre_registered <= BUILTIN_EDITOR_WINDOW_TYPE_IDS
    assert {
        type_id
        for type_id, registration in by_type.items()
        if registration.interaction is None
    } == pre_registered
    assert {
        type_id
        for type_id, registration in by_type.items()
        if registration.interaction is not None
        and registration.interaction.document_backed
    } == {
        "animclip2d_editor",
        "animfsm_editor",
        "animtimeline_editor",
        "build_settings",
        "particle_graph_editor",
        "physics_settings",
        "tag_layer_settings",
    }


def test_document_backed_policy_resolves_by_live_view_or_type():
    core = EditorInteractionCore()
    core.panels.register_type(
        "document_panel",
        PanelInteractionDescriptor(document_backed=True),
    )
    core.panels.bind_view("document/one", "document_panel", object())

    assert core.panels.is_document_backed(view_id="document/one")
    assert core.panels.is_document_backed(type_id="document_panel")
    assert not core.panels.is_document_backed(type_id="utility_panel")
    core.shutdown()


def test_panel_registry_rejects_missing_interaction_contract_before_registration():
    class WindowManagerStub:
        def __init__(self) -> None:
            self.registry = None
            self.registered: list[str] = []

        def set_panel_interaction_registry(self, registry) -> None:
            self.registry = registry

        def register_window_type(self, **kwargs) -> None:
            self.registered.append(kwargs["type_id"])

    original = PanelRegistry.get_registrations()
    PanelRegistry.clear()
    core = EditorInteractionCore()
    try:
        PanelRegistry._register(
            _PanelRegistration(
                object,
                "missing_contract",
                "Missing Contract",
                "",
                None,
                True,
            )
        )
        manager = WindowManagerStub()

        with pytest.raises(RuntimeError, match="missing_contract"):
            PanelRegistry.apply_all(manager, core.panels)

        assert manager.registered == []
    finally:
        core.shutdown()
        PanelRegistry.clear()
        for registration in original:
            PanelRegistry._register(registration)


def test_panel_registry_accepts_a_pre_registered_core_panel_contract():
    class WindowManagerStub:
        def set_panel_interaction_registry(self, registry) -> None:
            self.registry = registry

        def register_window_type(self, **kwargs) -> None:
            self.registered = kwargs["type_id"]

    original = PanelRegistry.get_registrations()
    PanelRegistry.clear()
    core = EditorInteractionCore()
    try:
        PanelRegistry._register(
            _PanelRegistration(
                object,
                "core_panel",
                "Core Panel",
                "",
                None,
                True,
            )
        )
        core.panels.register_type("core_panel", PanelInteractionDescriptor())
        manager = WindowManagerStub()

        assert PanelRegistry.apply_all(manager, core.panels) == 1
        assert manager.registered == "core_panel"
    finally:
        core.shutdown()
        PanelRegistry.clear()
        for registration in original:
            PanelRegistry._register(registration)


def test_panel_registry_installs_all_descriptors_before_binding_live_views():
    class WindowManagerStub:
        def __init__(self) -> None:
            self.bound_after_contract = False

        def set_panel_interaction_registry(self, registry) -> None:
            self.bound_after_contract = registry.descriptor("ordered") is not None

        def register_window_type(self, **_kwargs) -> None:
            assert self.bound_after_contract

    original = PanelRegistry.get_registrations()
    PanelRegistry.clear()
    core = EditorInteractionCore()
    try:
        PanelRegistry._register(
            _PanelRegistration(
                object,
                "ordered",
                "Ordered",
                "",
                None,
                True,
                interaction=PanelInteractionDescriptor(),
            )
        )
        manager = WindowManagerStub()

        assert PanelRegistry.apply_all(manager, core.panels) == 1
        assert manager.bound_after_contract
    finally:
        core.shutdown()
        PanelRegistry.clear()
        for registration in original:
            PanelRegistry._register(registration)


def test_panel_registry_rejects_duplicate_type_ids_before_mutation():
    class WindowManagerStub:
        def set_panel_interaction_registry(self, _registry) -> None:
            raise AssertionError("duplicate manifest must fail before binding")

        def register_window_type(self, **_kwargs) -> None:
            raise AssertionError("duplicate manifest must fail before registration")

    original = PanelRegistry.get_registrations()
    PanelRegistry.clear()
    core = EditorInteractionCore()
    try:
        for display_name in ("First", "Second"):
            PanelRegistry._register(
                _PanelRegistration(
                    object,
                    "duplicate",
                    display_name,
                    "",
                    None,
                    True,
                    interaction=PanelInteractionDescriptor(),
                )
            )

        with pytest.raises(RuntimeError, match="duplicate"):
            PanelRegistry.apply_all(WindowManagerStub(), core.panels)
    finally:
        core.shutdown()
        PanelRegistry.clear()
        for registration in original:
            PanelRegistry._register(registration)


def test_legacy_editor_window_decorator_uses_strict_panel_manifest():
    from Infernux.engine.ui.editor_window import EditorWindow, editor_window

    original = PanelRegistry.get_registrations()
    PanelRegistry.clear()
    try:
        descriptor = PanelInteractionDescriptor()

        @editor_window(
            "Strict Tool",
            type_id="strict_tool",
            interaction=descriptor,
        )
        class StrictTool(EditorWindow):
            pass

        registrations = PanelRegistry.get_registrations()
        assert len(registrations) == 1
        assert registrations[0].panel_class is StrictTool
        assert registrations[0].type_id == "strict_tool"
        assert registrations[0].interaction is descriptor
        assert StrictTool.PANEL_INTERACTION is descriptor
    finally:
        PanelRegistry.clear()
        for registration in original:
            PanelRegistry._register(registration)
