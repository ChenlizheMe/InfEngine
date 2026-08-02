from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.mcp.tools import material as material_tools


def _builtin_material():
    return SimpleNamespace(
        name="ParticleSixWaySmokeMaterial",
        is_builtin=True,
        shader_name="",
        vert_shader_name="Particle Sprite",
        frag_shader_name="Particle Six-Way Smoke",
        render_queue=3000,
        surface_type="transparent",
        blend_enable=True,
        depth_write_enable=False,
        get_all_properties=lambda: {
            "positiveAxesMap": "white",
            "negativeAxesMap": "black",
        },
    )


def test_load_builtin_material_by_explicit_uri(monkeypatch):
    from Infernux.core.material import Material

    expected = _builtin_material()
    monkeypatch.setattr(
        Material,
        "get",
        staticmethod(
            lambda key: expected if key == "ParticleSixWaySmokeMaterial" else None
        ),
    )

    actual = material_tools._load_material(
        "C:/Project",
        "builtin://ParticleSixWaySmokeMaterial",
        allow_builtin=True,
    )

    assert actual is expected
    info = material_tools._material_info(actual)
    assert info["is_builtin"] is True
    assert info["shader"]["vertex"] == "Particle Sprite"
    assert info["shader"]["fragment"] == "Particle Six-Way Smoke"
    assert info["properties"]["positiveAxesMap"] == "white"
    assert info["properties"]["negativeAxesMap"] == "black"


def test_builtin_material_is_read_only_by_default(monkeypatch):
    from Infernux.core.material import Material

    monkeypatch.setattr(Material, "get", staticmethod(lambda _key: _builtin_material()))

    with pytest.raises(PermissionError, match="Built-in materials are read-only"):
        material_tools._load_material(
            "C:/Project", "builtin://ParticleSixWaySmokeMaterial"
        )


@pytest.mark.parametrize(
    "path",
    ["builtin://", "builtin://folder/material", "builtin://folder\\material"],
)
def test_builtin_material_uri_rejects_missing_or_nested_keys(path):
    with pytest.raises(ValueError, match="builtin://<material-key>"):
        material_tools._builtin_material_key(path)


def test_missing_builtin_material_reports_key(monkeypatch):
    from Infernux.core.material import Material

    monkeypatch.setattr(Material, "get", staticmethod(lambda _key: None))

    with pytest.raises(FileNotFoundError, match="MissingMaterial"):
        material_tools._load_material(
            "C:/Project", "builtin://MissingMaterial", allow_builtin=True
        )
