import pytest

from Infernux.core.assets import AssetManager
from Infernux.ui import ui_texture_cache as texture_cache_module


class _NativeTexturePreview:
    def __init__(self):
        self.live_texture_id = 0
        self.queries = []

    def get_texture_preview_texture_id(self, resource_key):
        self.queries.append(resource_key)
        return self.live_texture_id


class _Engine:
    def __init__(self, native):
        self._native = native

    def get_native_engine(self):
        return self._native


def test_cached_ui_texture_refreshes_replaced_native_descriptor(monkeypatch, tmp_path):
    from Infernux.engine import project_context

    texture = tmp_path / "Assets" / "UI" / "button.png"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"png")
    monkeypatch.setattr(project_context, "_project_root", str(tmp_path))
    monkeypatch.setattr(texture_cache_module, "texture_stamp", lambda *_args: 17)
    monkeypatch.setattr(
        AssetManager,
        "_asset_database",
        type(
            "_AssetDatabase",
            (),
            {"get_guid_from_path": lambda _self, _path: "button-texture-guid"},
        )(),
    )

    scheduled = iter(((101, 4, 4), (303, 4, 4)))
    monkeypatch.setattr(
        texture_cache_module,
        "query_or_schedule_texture",
        lambda *_args, **_kwargs: next(scheduled),
    )

    native = _NativeTexturePreview()
    cache = texture_cache_module.UITextureCache()
    engine = _Engine(native)

    assert cache.get(engine, "Assets/UI/button.png") == 101
    first_generation = cache.generation

    native.live_texture_id = 202
    assert cache.get(engine, "Assets/UI/button.png") == 202
    assert cache.generation == first_generation + 1

    native.live_texture_id = 0
    assert cache.get(engine, "Assets/UI/button.png") == 303
    assert cache.generation == first_generation + 2


def test_ui_texture_requires_registered_asset_guid(monkeypatch):
    monkeypatch.setattr(
        AssetManager,
        "_asset_database",
        type(
            "_AssetDatabase",
            (),
            {"get_guid_from_path": lambda _self, _path: ""},
        )(),
    )

    cache = texture_cache_module.UITextureCache()
    with pytest.raises(KeyError, match="not registered in AssetDatabase"):
        cache.get(_Engine(_NativeTexturePreview()), "Assets/UI/missing.png")
