from __future__ import annotations

from Infernux.components.component_identity import bind_asset_script_guid, component_type_guid
from Infernux.components.missing_script import create_missing_script_component
from Infernux.components.script_loader import load_component_class_from_file
from Infernux.engine.component_restore import create_component_instance
from Infernux.lib import _Vec3WritebackProxy


def test_asset_script_bind_is_stable_across_module_rename(tmp_path):
    script = tmp_path / "mover.py"
    script.write_text(
        "from Infernux.components import InxComponent\n"
        "class Mover(InxComponent):\n"
        "    speed: float = 1.0\n",
        encoding="utf-8",
    )
    first = load_component_class_from_file(str(script), "Mover")
    assert first is not None
    script_guid = "a" * 32
    first_type_guid = bind_asset_script_guid(first, script_guid)

    renamed = tmp_path / "player_mover.py"
    script.rename(renamed)
    second = load_component_class_from_file(str(renamed), "Mover")
    assert second is not None
    second_type_guid = bind_asset_script_guid(second, script_guid)

    assert first_type_guid == second_type_guid
    assert second_type_guid == component_type_guid(script_guid, "Mover")
    assert first.__module__ != second.__module__


def test_asset_guid_binding_rekeys_numeric_component_data_store(tmp_path):
    script = tmp_path / "numeric_probe.py"
    script.write_text(
        "from Infernux.components import InxComponent\n"
        "class NumericProbe(InxComponent):\n"
        "    speed: float = 1.0\n",
        encoding="utf-8",
    )
    component_type = load_component_class_from_file(str(script), "NumericProbe")
    bind_asset_script_guid(component_type, "9" * 32)

    instance = component_type()
    try:
        assert instance._cds_slot is not None
        assert instance._cds_class_id is not None
        instance.speed = 4.25
        assert instance.speed == 4.25
    finally:
        instance._call_on_destroy()


def test_create_component_instance_accepts_stale_module_type_guid(tmp_path, monkeypatch):
    script = tmp_path / "jump.py"
    script.write_text(
        "from Infernux.components import InxComponent\n"
        "class Jump(InxComponent):\n"
        "    height: float = 2.0\n",
        encoding="utf-8",
    )
    script_guid = "b" * 32
    stale_type_guid = component_type_guid("old.module.name", "Jump")

    class _Db:
        def get_path_from_guid(self, guid):
            assert guid == script_guid
            return str(script)

        def get_guid_from_path(self, path):
            assert path == str(script)
            return script_guid

    instance, path = create_component_instance(
        script_guid,
        stale_type_guid,
        "Jump",
        asset_database=_Db(),
    )
    assert path == str(script)
    assert instance is not None
    assert type(instance).__name__ == "Jump"
    assert instance.__class__._get_type_guid() == component_type_guid(script_guid, "Jump")


def test_create_component_instance_follows_class_rename_in_one_component_script(tmp_path):
    script = tmp_path / "newcomponent2.py"
    script.write_text(
        "from Infernux.components import InxComponent\n"
        "class RenamedComponent(InxComponent):\n"
        "    speed: float = 5.0\n",
        encoding="utf-8",
    )
    script_guid = "e" * 32
    stale_type_guid = component_type_guid(script_guid, "NewComponent2")

    class _Db:
        def get_path_from_guid(self, guid):
            return str(script)

        def get_guid_from_path(self, path):
            return script_guid

    instance, path = create_component_instance(
        script_guid,
        stale_type_guid,
        "NewComponent2",
        asset_database=_Db(),
    )
    assert path == str(script)
    assert instance is not None
    assert type(instance).__name__ == "RenamedComponent"
    assert instance.__class__._get_type_guid() == component_type_guid(script_guid, "RenamedComponent")


def test_deleted_asset_is_not_resurrected_from_loaded_type_registry():
    from Infernux.components import InxComponent

    class DeletedProbe(InxComponent):
        pass

    script_guid = "7" * 32
    type_guid = bind_asset_script_guid(DeletedProbe, script_guid)

    class _DeletedDb:
        @staticmethod
        def get_path_from_guid(_guid):
            return ""

    instance, path = create_component_instance(
        script_guid,
        type_guid,
        "DeletedProbe",
        asset_database=_DeletedDb(),
        prefer_loaded_type=True,
    )

    assert path is None
    assert instance is None


def test_builtin_component_can_use_loaded_type_without_project_asset():
    from Infernux.components import InxComponent

    class BuiltinProbe(InxComponent):
        pass

    script_guid = BuiltinProbe._intrinsic_script_guid_
    type_guid = BuiltinProbe._get_type_guid()

    class _ProjectDb:
        @staticmethod
        def get_path_from_guid(_guid):
            return ""

    instance, path = create_component_instance(
        script_guid,
        type_guid,
        "BuiltinProbe",
        asset_database=_ProjectDb(),
        prefer_loaded_type=True,
    )

    assert path is None
    assert isinstance(instance, BuiltinProbe)


def test_missing_script_placeholder_preserves_identity_and_fields():
    fields = {
        "__type_name__": "Gone",
        "__component_id__": 42,
        "speed": 3.5,
    }
    missing = create_missing_script_component(
        type_name="Gone",
        script_guid="c" * 32,
        type_guid="d" * 32,
        module_name="assets.gone",
        qualified_name="Gone",
        fields=fields,
        error="file missing",
    )
    assert missing._is_broken is True
    assert missing.type_name == "Gone"
    assert missing._script_guid == "c" * 32
    assert missing._get_type_guid() == "d" * 32
    encoded = missing._serialize_fields_document()
    assert encoded["speed"] == 3.5
    assert encoded["__type_name__"] == "Gone"

    from Infernux.mcp.tools.common import serialize_component

    snapshot = serialize_component(missing)
    assert snapshot["broken_script"] is True
    assert snapshot["broken_error"] == "file missing"


def test_vec3_writeback_proxy_is_subscriptable():
    class _Vec:
        def __init__(self, x=0.0, y=0.0, z=0.0):
            self.x = float(x)
            self.y = float(y)
            self.z = float(z)

    class _Owner:
        def __init__(self):
            self.pos = _Vec(1.0, 2.0, 3.0)

    owner = _Owner()
    proxy = _Vec3WritebackProxy(owner, "pos", owner.pos, lambda o, v: setattr(o, "pos", v))
    assert proxy[0] == 1.0
    assert proxy[1] == 2.0
    assert proxy[2] == 3.0
    proxy[1] = 9.0
    assert owner.pos.y == 9.0
