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

    def __init__(self, raw):
        self.raw = raw

    def get_components(self):
        return [self.raw]


class _Scene:
    structure_version = 1

    def __init__(self, game_object):
        self.game_object = game_object

    def find_by_id(self, object_id):
        return self.game_object if object_id == self.game_object.id else None


class _SceneManagerInstance:
    def __init__(self, scene):
        self.scene = scene

    def get_active_scene(self):
        return self.scene


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
