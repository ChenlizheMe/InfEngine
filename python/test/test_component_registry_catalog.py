import os

from Infernux.components.registry import (
    get_component_constraints,
    get_component_registrations,
    get_python_attachment_blockers,
    get_type_by_identity,
    register_component_script,
    unregister_component_script,
)
from Infernux.engine.bootstrap_inspector._helpers import (
    _get_add_component_entries,
    _get_component_script_error,
    _get_py_components_safe,
)


def test_project_component_source_is_registered_without_execution(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    script = assets / "player.py"
    script.write_text(
        "raise RuntimeError('registry indexing must not execute scripts')\n"
        "class PlayerController(InxComponent):\n"
        "    pass\n",
        encoding="utf-8",
    )
    try:
        assert register_component_script(str(script))
        registrations = get_component_registrations(project_root=str(tmp_path))
        player = next(entry for entry in registrations if entry.type_name == "PlayerController")
        assert player.script_path == os.path.abspath(script)
        assert player.component_type is None
    finally:
        unregister_component_script(str(script))


def test_add_component_menu_reads_registry_without_filesystem_scan(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    assets.mkdir()
    script = assets / "player.py"
    script.write_text("class RegistryOnly(InxComponent):\n    pass\n", encoding="utf-8")
    register_component_script(str(script))
    monkeypatch.setattr(
        "Infernux.engine.project_context.get_project_root",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(os, "walk", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Add Component must not scan the filesystem")
    ))
    try:
        entries = _get_add_component_entries()
        entry = next(item for item in entries if item.display_name == "RegistryOnly")
        assert entry.script_path == os.path.abspath(script)
    finally:
        unregister_component_script(str(script))


def test_deleted_component_script_leaves_registry(tmp_path):
    script = tmp_path / "removed.py"
    script.write_text("class Removed(InxComponent):\n    pass\n", encoding="utf-8")
    assert register_component_script(str(script))
    unregister_component_script(str(script))
    assert all(
        entry.type_name != "Removed"
        for entry in get_component_registrations(project_root=str(tmp_path))
    )


def test_inspector_does_not_resolve_engine_component_identity_as_asset_guid():
    class EngineComponent:
        _script_guid = "intrinsic-guid"

    class Database:
        def get_path_from_guid(self, _guid):
            raise AssertionError("engine component identities are not asset GUIDs")

    assert _get_component_script_error(EngineComponent(), Database()) is None


def test_inspector_reads_python_components_from_selected_object():
    components = [object(), object()]

    class SelectedObject:
        def get_py_components(self):
            return tuple(components)

    assert _get_py_components_safe(SelectedObject()) == components


def test_missing_script_placeholder_does_not_replace_live_registered_type():
    from Infernux.components import InxComponent
    from Infernux.components.missing_script import create_missing_script_component

    class LiveComponent(InxComponent):
        value: int = 1

    script_guid = LiveComponent._get_intrinsic_script_guid()
    type_guid = LiveComponent._get_type_guid()
    placeholder = create_missing_script_component(
        type_name=LiveComponent.__name__,
        script_guid=script_guid,
        type_guid="f" * 32,
        module_name=LiveComponent.__module__,
        qualified_name=LiveComponent.__qualname__,
        fields={"__type_name__": LiveComponent.__name__, "value": 1},
        error="intentional missing script",
    )
    try:
        assert get_type_by_identity(
            LiveComponent.__name__, script_guid, type_guid
        ) is LiveComponent
    finally:
        placeholder._call_on_destroy()


def test_engine_component_catalog_is_explicit_and_complete():
    registrations = get_component_registrations()
    engine_types = {
        entry.type_name for entry in registrations if not entry.project_script
    }
    assert engine_types == {
        "ParticleSystem",
        "LineRenderer",
        "RenderStack",
        "RuntimeAcceptanceRunner",
        "SkeletalAnimator",
        "SpriteRenderer",
        "SpiritAnimator",
        "TimelineAction",
        "UIButton",
        "UICanvas",
        "UIImage",
        "UIText",
    }


def test_component_decorators_refresh_the_authoritative_constraint_record():
    from Infernux.components import InxComponent
    from Infernux.components.decorators import disallow_multiple, require_component

    class RegistryDependency(InxComponent):
        pass

    @disallow_multiple
    @require_component(RegistryDependency)
    class RegistryConstrained(InxComponent):
        pass

    constraints = get_component_constraints(RegistryConstrained)
    assert constraints.allow_multiple is False
    assert constraints.required_types == (RegistryDependency,)


def test_disallow_multiple_uses_stable_type_identity_across_reload():
    from types import SimpleNamespace

    from Infernux.components import InxComponent
    from Infernux.components.decorators import disallow_multiple

    namespace = {"__module__": __name__, "__qualname__": "RegistryReloadedSingle"}
    old_type = disallow_multiple(type("RegistryReloadedSingle", (InxComponent,), dict(namespace)))
    new_type = disallow_multiple(type("RegistryReloadedSingle", (InxComponent,), dict(namespace)))
    assert old_type._get_type_guid() == new_type._get_type_guid()

    owner = SimpleNamespace(get_py_components=lambda: [old_type()])
    assert "only one instance is allowed per GameObject" in get_python_attachment_blockers(
        owner,
        new_type,
    )
