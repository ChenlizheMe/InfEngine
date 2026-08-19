from types import SimpleNamespace

from Infernux.components.builtin_component import BuiltinComponent
from Infernux.engine.bootstrap_inspector._wire import (
    _wire_cache_init,
    _wire_component_list,
)


class _CacheProbeBuiltin(BuiltinComponent):
    _cpp_type_name = "CacheProbeBuiltin"


class _RawComponent:
    type_name = "CacheProbeBuiltin"
    component_id = 41
    enabled = True
    execution_order = 0


class _GameObject:
    id = 7
    handle = None

    def __init__(self, raw, scene=None):
        self.raw = raw
        self.scene = scene

    def get_components(self):
        return [self.raw]


class _Scene:
    structure_version = 1

    def __init__(self, game_object):
        self.game_object = game_object

    def find_by_id(self, object_id):
        return (
            self.game_object
            if self.game_object is not None and object_id == self.game_object.id
            else None
        )


class _SceneManagerInstance:
    def __init__(self, scene, persistent_scene=None):
        self.scene = scene
        self.persistent_scene = persistent_scene

    def get_active_scene(self):
        return self.scene

    def find_runtime_object_by_id(self, object_id):
        found = self.scene.find_by_id(object_id) if self.scene is not None else None
        if found is not None:
            return found
        if self.persistent_scene is not None:
            return self.persistent_scene.find_by_id(object_id)
        return None


class _SceneManager:
    current = None

    @classmethod
    def instance(cls):
        return cls.current


class _InspectorComponentInfo:
    pass


def test_invalidated_builtin_wrapper_is_rebound_from_live_object(monkeypatch):
    raw = _RawComponent()
    game_object = _GameObject(raw)
    scene = _Scene(game_object)
    _SceneManager.current = _SceneManagerInstance(scene)
    monkeypatch.setitem(
        BuiltinComponent._builtin_registry,
        _RawComponent.type_name,
        _CacheProbeBuiltin,
    )
    BuiltinComponent._wrapper_cache.clear()

    ctx = SimpleNamespace(
        SceneManager=_SceneManager,
        InspectorComponentInfo=_InspectorComponentInfo,
        InxComponent=__import__(
            "Infernux.components.component", fromlist=["InxComponent"]
        ).InxComponent,
        _inspector_support=SimpleNamespace(
            get_component_structure_version=lambda: 1,
        ),
        get_component_icon_id=lambda *_args: 0,
        ip=SimpleNamespace(),
    )
    _wire_cache_init(ctx)
    _wire_component_list(ctx)

    first = ctx.resolve_component(game_object.id, raw.component_id, True)
    assert isinstance(first, _CacheProbeBuiltin)
    first._invalidate_native_binding()

    rebound = ctx.resolve_component(game_object.id, raw.component_id, True)
    assert isinstance(rebound, _CacheProbeBuiltin)
    assert rebound is not first
    assert rebound._get_bound_native_component() is raw


def test_scene_structure_bump_rebinds_live_looking_builtin_wrapper(monkeypatch):
    raw = _RawComponent()
    scene = _Scene(None)
    game_object = _GameObject(raw, scene)
    scene.game_object = game_object
    _SceneManager.current = _SceneManagerInstance(scene)
    monkeypatch.setitem(
        BuiltinComponent._builtin_registry,
        _RawComponent.type_name,
        _CacheProbeBuiltin,
    )
    BuiltinComponent._wrapper_cache.clear()

    ctx = SimpleNamespace(
        SceneManager=_SceneManager,
        InspectorComponentInfo=_InspectorComponentInfo,
        InxComponent=__import__(
            "Infernux.components.component", fromlist=["InxComponent"]
        ).InxComponent,
        _inspector_support=SimpleNamespace(
            get_component_structure_version=lambda: 1,
        ),
        get_component_icon_id=lambda *_args: 0,
        ip=SimpleNamespace(),
    )
    _wire_cache_init(ctx)
    _wire_component_list(ctx)

    first = ctx.resolve_component(game_object.id, raw.component_id, True)
    assert isinstance(first, _CacheProbeBuiltin)
    assert first._get_bound_native_component() is raw
    assert not first._is_native_binding_stale()

    replacement = _RawComponent()
    replacement.component_id = raw.component_id
    game_object.raw = replacement
    scene.structure_version += 1

    rebound = ctx.resolve_component(game_object.id, raw.component_id, True)
    assert isinstance(rebound, _CacheProbeBuiltin)
    assert rebound is first
    assert rebound._get_bound_native_component() is replacement


def test_python_script_error_lookup_runs_only_when_diagnostics_change(monkeypatch, tmp_path):
    class _RawPythonComponent:
        type_name = "CacheProbeScript"
        component_id = 73
        enabled = True

        def get_py_component(self):
            return self

    component = _RawPythonComponent()
    game_object = _GameObject(component)
    scene = _Scene(game_object)
    _SceneManager.current = _SceneManagerInstance(scene)

    lookups = []
    monkeypatch.setattr(
        "Infernux.engine.bootstrap_inspector._helpers._get_component_script_error",
        lambda comp, _database: lookups.append(comp) or None,
    )

    ctx = SimpleNamespace(
        SceneManager=_SceneManager,
        InspectorComponentInfo=_InspectorComponentInfo,
        InxComponent=__import__(
            "Infernux.components.component", fromlist=["InxComponent"]
        ).InxComponent,
        _inspector_support=SimpleNamespace(
            get_component_structure_version=lambda: 1,
        ),
        get_component_icon_id=lambda *_args: 0,
        engine=SimpleNamespace(get_asset_database=lambda: None),
        ip=SimpleNamespace(),
    )
    _wire_cache_init(ctx)
    _wire_component_list(ctx)

    ctx.ip.get_component_list(game_object.id)
    ctx.ip.get_component_list(game_object.id)
    assert lookups == [component]

    from Infernux.components.script_loader import _clear_script_error, set_script_error

    changed_script = tmp_path / "changed.py"
    set_script_error(str(changed_script), "syntax error")
    try:
        ctx.ip.get_component_list(game_object.id)
        assert lookups == [component, component]
    finally:
        _clear_script_error(str(changed_script))


def test_component_cache_resolves_dont_destroy_on_load_object_from_persistent_scene(
    monkeypatch,
):
    raw = _RawComponent()
    game_object = _GameObject(raw)
    active_scene = _Scene(None)
    persistent_scene = _Scene(game_object)
    game_object.scene = persistent_scene
    _SceneManager.current = _SceneManagerInstance(active_scene, persistent_scene)
    monkeypatch.setitem(
        BuiltinComponent._builtin_registry,
        _RawComponent.type_name,
        _CacheProbeBuiltin,
    )
    BuiltinComponent._wrapper_cache.clear()

    ctx = SimpleNamespace(
        SceneManager=_SceneManager,
        InspectorComponentInfo=_InspectorComponentInfo,
        InxComponent=__import__(
            "Infernux.components.component", fromlist=["InxComponent"]
        ).InxComponent,
        _inspector_support=SimpleNamespace(
            get_component_structure_version=lambda: 1,
        ),
        get_component_icon_id=lambda *_args: 0,
        engine=SimpleNamespace(get_asset_database=lambda: None),
        ip=SimpleNamespace(),
    )
    _wire_cache_init(ctx)
    _wire_component_list(ctx)

    items = ctx.ip.get_component_list(game_object.id)
    resolved = ctx.resolve_component(game_object.id, raw.component_id, True)

    assert [item.type_name for item in items] == ["CacheProbeBuiltin"]
    assert isinstance(resolved, _CacheProbeBuiltin)
    assert ctx.current_scene_and_versions(game_object.id)[0] is persistent_scene
