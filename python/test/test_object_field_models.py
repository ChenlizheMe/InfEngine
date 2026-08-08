import ast
from pathlib import Path

import pytest


def test_object_field_dispatches_locate_open_and_keyboard_open_once():
    from Infernux.engine.interaction.object_fields import (
        ObjectFieldGesture,
        ObjectReferenceFieldModel,
    )

    actions = []
    model = ObjectReferenceFieldModel(
        "material",
        "Smoke",
        "Material",
        on_locate=lambda: actions.append("locate"),
        on_open=lambda: actions.append("open"),
    )

    assert model.dispatch_chrome(int(ObjectFieldGesture.LOCATE)) == ObjectFieldGesture.LOCATE
    model.dispatch_chrome(
        int(ObjectFieldGesture.LOCATE | ObjectFieldGesture.OPEN)
    )
    model.dispatch_chrome(int(ObjectFieldGesture.KEYBOARD_OPEN))

    assert actions == ["locate", "open", "open"]


def test_object_field_requires_identity_and_derives_stable_semantics():
    from Infernux.engine.interaction.object_fields import ObjectReferenceFieldModel

    with pytest.raises(ValueError, match="field id"):
        ObjectReferenceFieldModel("", "None", "Material")

    model = ObjectReferenceFieldModel("material_slot_0", "None", "Material")
    assert model.semantic_id == "object_field.material_slot_0"


def test_object_field_without_opener_locates_for_open_gesture():
    from Infernux.engine.interaction.object_fields import (
        ObjectFieldGesture,
        ObjectReferenceFieldModel,
    )

    actions = []
    model = ObjectReferenceFieldModel(
        "texture",
        "Smoke",
        "Texture",
        on_locate=lambda: actions.append("locate"),
    )

    model.dispatch_chrome(int(ObjectFieldGesture.OPEN))

    assert actions == ["locate"]


def test_asset_field_double_click_always_reveals_in_file_manager():
    from Infernux.engine.interaction.object_fields import (
        AssetReferenceFieldModel,
        ObjectFieldGesture,
    )

    actions = []
    model = AssetReferenceFieldModel(
        "material",
        "Smoke",
        "Material",
        has_value=True,
        on_locate=lambda: actions.append("locate"),
        on_open=lambda: actions.append("open"),
    )

    model.dispatch_chrome(int(ObjectFieldGesture.OPEN))
    model.dispatch_chrome(int(ObjectFieldGesture.KEYBOARD_OPEN))
    model.dispatch_chrome(int(ObjectFieldGesture.LOCATE))

    assert actions == ["locate", "locate"]


def test_object_picker_model_isolates_query_and_focus_per_field():
    from Infernux.engine.interaction.object_fields import ObjectPickerModel

    picker = ObjectPickerModel()
    picker.set_query("mesh", "sphere")
    picker.set_query("material", "smoke")
    picker.request_open("mesh")

    assert picker.query("mesh") == ""
    assert picker.query("material") == "smoke"
    assert picker.open_requested("mesh") is True
    assert picker.consume_focus_request("mesh") is True
    assert picker.consume_focus_request("mesh") is False
    picker.confirm_open("mesh")
    assert picker.open_requested("mesh") is False


def test_object_picker_queries_share_search_revision_and_completion_tokens():
    from Infernux.engine.interaction.object_fields import ObjectPickerModel

    picker = ObjectPickerModel()
    picker.set_query("mesh", "sphere")
    search = picker.query_model("mesh")
    token = search.token(source_generation=4, scope_key="Mesh")

    assert search.matches("UV Sphere")
    assert search.accepts(token, source_generation=4, scope_key="Mesh")

    picker.set_query("mesh", "cube")
    assert not search.accepts(token, source_generation=4, scope_key="Mesh")


def test_object_field_picker_mutations_dispatch_after_intent_resolution():
    from Infernux.engine.interaction.object_fields import ObjectReferenceFieldModel

    actions = []
    model = ObjectReferenceFieldModel(
        "audio",
        "None",
        "AudioClip",
        on_pick=lambda value: actions.append(("pick", value)),
        on_clear=lambda: actions.append(("clear", None)),
    )

    model.dispatch_picker(("pick", "Assets/Audio/click.wav"))
    model.dispatch_picker(("clear", None))
    model.dispatch_picker(None)

    assert actions == [
        ("pick", "Assets/Audio/click.wav"),
        ("clear", None),
    ]


def test_asset_type_registry_is_the_compatibility_authority():
    from Infernux.core.asset_reference_types import asset_type_registry

    texture = asset_type_registry.require("Texture2D")
    mesh = asset_type_registry.require("Model")
    vertex = asset_type_registry.require("Vert")

    assert texture.incompatibility("Assets/Art/smoke.tga") == ""
    assert mesh.incompatibility("Assets/Models/terrain.glb") == ""
    assert vertex.incompatibility("Assets/Shaders/smoke.vert") == ""
    assert "expected one of .vert" in vertex.incompatibility(
        "Assets/Shaders/smoke.frag"
    )


def test_asset_reference_field_rejects_wrong_shader_stage_before_callback():
    from Infernux.engine.interaction.object_fields import AssetReferenceFieldModel

    assigned = []
    rejected = []
    model = AssetReferenceFieldModel(
        "mat_vert",
        "standard",
        "Vert",
        on_assign=assigned.append,
        on_rejected=rejected.append,
    )

    model.dispatch_picker(("pick", "Assets/Shaders/standard.frag"))
    model.dispatch_drop("Assets/Shaders/standard.vert")

    assert assigned == ["Assets/Shaders/standard.vert"]
    assert len(rejected) == 1
    assert "Vertex Shader reference rejects" in rejected[0]


def test_asset_reference_field_rejects_private_catalog_replacement():
    from Infernux.engine.interaction.object_fields import AssetReferenceFieldModel

    with pytest.raises(ValueError, match="shared AssetReferenceCatalog"):
        AssetReferenceFieldModel(
            "private_texture_picker",
            "None",
            "Texture",
            asset_items=lambda _query: (),
        )


def test_asset_reference_field_rejects_split_assignment_callbacks():
    from Infernux.engine.interaction.object_fields import AssetReferenceFieldModel

    with pytest.raises(ValueError, match="one on_assign callback"):
        AssetReferenceFieldModel(
            "legacy_texture",
            "None",
            "Texture",
            on_pick=lambda _value: None,
        )

    with pytest.raises(ValueError, match="one on_assign callback"):
        AssetReferenceFieldModel(
            "legacy_texture",
            "None",
            "Texture",
            on_drop=lambda _value: None,
        )


def test_asset_reference_clipboard_round_trip_uses_assignment_compatibility():
    from Infernux.engine.interaction.object_fields import AssetReferenceFieldModel

    assigned = []
    rejected = []
    source = AssetReferenceFieldModel(
        "source",
        "smoke.png",
        "Texture",
        has_value=True,
        reference_value={
            "guid": "texture-guid",
            "path_hint": "Assets/Art/smoke.png",
        },
    )
    target = AssetReferenceFieldModel(
        "target",
        "None",
        "Texture",
        on_assign=assigned.append,
        on_rejected=rejected.append,
    )
    material = AssetReferenceFieldModel(
        "material",
        "None",
        "Material",
        on_assign=assigned.append,
        on_rejected=rejected.append,
    )

    text = source.copy_reference_text()
    assert text.startswith("infernux.asset_reference ")
    assert target.can_paste_reference(text) is True
    assert target.dispatch_paste_reference(text) is True
    assert assigned == [{
        "asset_type": "Texture",
        "builtin": "",
        "guid": "texture-guid",
        "path_hint": "Assets/Art/smoke.png",
    }]
    assert material.can_paste_reference(text) is False
    assert material.dispatch_paste_reference(text) is False
    assert "rejects asset type 'Texture'" in rejected[-1]


def test_asset_reference_picker_drop_and_paste_ignore_same_value():
    from Infernux.engine.interaction.object_fields import AssetReferenceFieldModel

    assigned = []
    model = AssetReferenceFieldModel(
        "texture",
        "smoke.png",
        "Texture",
        has_value=True,
        reference_value={
            "guid": "texture-guid",
            "path_hint": "Assets/Art/smoke.png",
        },
        on_assign=assigned.append,
    )
    text = model.copy_reference_text()

    model.dispatch_picker(("pick", "Assets\\Art\\smoke.png"))
    model.dispatch_drop({"guid": "TEXTURE-GUID"})
    assert model.dispatch_paste_reference(text) is False
    assert assigned == []


def test_asset_reference_field_accepts_snapshot_transaction_protocol():
    from Infernux.engine.interaction import SnapshotPropertyTransaction
    from Infernux.engine.interaction.object_fields import AssetReferenceFieldModel
    from Infernux.engine.undo import UndoManager

    previous = UndoManager._instance
    manager = UndoManager()
    state = {"value": {"path_hint": "Assets/Art/old.png"}}
    try:
        transaction = SnapshotPropertyTransaction(
            "SpriteRenderer:1:sprite",
            lambda: state["value"],
            lambda value: state.__setitem__("value", value),
            "Set Sprite",
            value_type="Texture",
            normalize=lambda value: (
                value if isinstance(value, dict) else {"path_hint": str(value)}
            ),
            clear_value={},
        )
        model = AssetReferenceFieldModel(
            "sprite",
            "old.png",
            "Texture",
            has_value=True,
            transaction=transaction,
        )

        model.dispatch_picker(("pick", "Assets/Art/new.png"))
        assert state["value"] == {"path_hint": "Assets/Art/new.png"}
        manager.undo()
        assert state["value"] == {"path_hint": "Assets/Art/old.png"}
    finally:
        UndoManager._instance = previous


def test_asset_reference_guid_is_resolved_before_type_validation(monkeypatch):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction.object_fields import AssetReferenceFieldModel

    class Database:
        paths = {
            "fragment-guid": "Assets/Shaders/standard.frag",
            "texture-guid": "Assets/Art/smoke.png",
        }

        def get_path_from_guid(self, guid):
            return self.paths.get(guid, "")

    monkeypatch.setattr(AssetManager, "_asset_database", Database())
    assigned = []
    rejected = []
    vertex = AssetReferenceFieldModel(
        "vertex",
        "None",
        "Shader.Vertex",
        on_assign=assigned.append,
        on_rejected=rejected.append,
    )

    vertex.dispatch_picker(("pick", {"guid": "fragment-guid"}))
    assert assigned == []
    assert "expected one of .vert" in rejected[-1]

    texture = AssetReferenceFieldModel(
        "texture",
        "None",
        "Texture",
        on_assign=assigned.append,
        on_rejected=rejected.append,
    )
    texture.dispatch_picker(("pick", {"guid": "missing-guid"}))
    assert assigned == []
    assert "unknown GUID 'missing-guid'" in rejected[-1]


def test_asset_reference_rejects_scene_objects_without_explicit_alternate_provider():
    from Infernux.engine.interaction.object_fields import AssetReferenceFieldModel

    scene_object = object()
    assigned = []
    rejected = []
    strict = AssetReferenceFieldModel(
        "mesh",
        "None",
        "Mesh",
        on_assign=assigned.append,
        on_rejected=rejected.append,
    )
    strict.dispatch_picker(("pick", scene_object))
    assert assigned == []
    assert "rejects non-asset value" in rejected[-1]

    alternate = AssetReferenceFieldModel(
        "skinned_mesh",
        "None",
        "Mesh",
        on_assign=assigned.append,
        alternate_compatibility=lambda value: "" if value is scene_object else "bad",
    )
    alternate.dispatch_picker(("pick", scene_object))
    assert assigned == [scene_object]


def test_animation_clip3d_accepts_only_its_registered_virtual_take_paths():
    from Infernux.core.asset_reference_types import (
        asset_type_registry,
        resolve_asset_reference_path,
    )

    virtual_take = "model-guid::subanim:3"
    assert (
        asset_type_registry.require("AnimationClip3D").incompatibility(virtual_take)
        == ""
    )
    assert resolve_asset_reference_path("AnimationClip3D", virtual_take) == virtual_take
    assert asset_type_registry.require("AnimationClip").incompatibility(virtual_take)
    assert asset_type_registry.require("AnimationClip3D").incompatibility(
        "Assets/Animations/wrong.animclip2d"
    )


def test_asset_reference_context_menu_defers_copy_until_popup_scope_closes():
    from Infernux.engine.interaction import (
        ClipboardDomain,
        ClipboardService,
        EditorCommandRegistry,
        FocusService,
        SelectionService,
    )
    from Infernux.engine.interaction.object_fields import (
        ASSET_REFERENCE_COPY_COMMAND,
        AssetReferenceFieldModel,
        register_asset_reference_commands,
    )
    from Infernux.engine.i18n import t
    from Infernux.engine.ui.igui import IGUI

    events = []

    class Context:
        clipboard = ""

        def push_id_str(self, value):
            events.append(("push", value))

        def pop_id(self):
            events.append(("pop", None))

        @staticmethod
        def begin_popup(_popup_id):
            return True

        @staticmethod
        def menu_item(label, _shortcut, _selected, _enabled):
            return label == t("asset_reference.copy")

        @staticmethod
        def separator():
            pass

        def close_current_popup(self):
            events.append(("close_popup", None))

        def end_popup(self):
            events.append(("end_popup", None))

        def get_clipboard_text(self):
            return self.clipboard

        def set_clipboard_text(self, value):
            events.append(("set_clipboard", value))
            self.clipboard = value

    ClipboardService()
    registry = EditorCommandRegistry(
        focus=FocusService(),
        selection=SelectionService(),
    )
    register_asset_reference_commands(registry)
    ctx = Context()
    model = AssetReferenceFieldModel(
        "texture",
        "smoke.png",
        "Texture",
        has_value=True,
        reference_value={"path_hint": "Assets/Art/smoke.png"},
    )

    result = IGUI._render_asset_reference_context_menu(ctx, model)
    assert result is not None
    assert result.command.spec.command_id == ASSET_REFERENCE_COPY_COMMAND
    assert result.result.accepted
    assert events[:4] == [
        ("push", "texture"),
        ("close_popup", None),
        ("end_popup", None),
        ("pop", None),
    ]
    assert events[-1][0] == "set_clipboard"
    payload = ClipboardService.instance().peek(ClipboardDomain.ASSET)
    assert payload is not None
    assert payload.items[0].sub_kind == "Texture"


def test_asset_reference_context_menu_contains_no_private_business_handlers():
    source = (
        Path(__file__).parents[1]
        / "Infernux"
        / "engine"
        / "ui"
        / "igui.py"
    ).read_text(encoding="utf-8")

    body = source.split(
        "def _render_asset_reference_context_menu", 1
    )[1].split("def process_object_field_interaction", 1)[0]
    assert "ContextMenuBuilder" in body
    assert "ctx.menu_item(" not in body
    assert "_dispatch_asset_reference_context_intent" not in source


def test_empty_asset_field_single_click_is_inert_but_enter_opens_picker():
    from Infernux.engine.interaction.object_fields import (
        AssetReferenceFieldModel,
        ObjectFieldGesture,
    )

    located = []
    model = AssetReferenceFieldModel(
        "mat_ref",
        "None",
        "Material",
        has_value=False,
        on_locate=lambda: located.append("locate"),
    )

    click = model.dispatch_chrome(int(ObjectFieldGesture.LOCATE))
    enter = model.dispatch_chrome(int(ObjectFieldGesture.KEYBOARD_OPEN))

    assert not click & ObjectFieldGesture.OPEN_PICKER
    assert enter & ObjectFieldGesture.OPEN_PICKER
    assert located == []


def test_asset_field_clear_only_mutates_a_nonempty_reference():
    from Infernux.engine.interaction.object_fields import (
        AssetReferenceFieldModel,
        ObjectFieldGesture,
    )

    cleared = []
    empty = AssetReferenceFieldModel(
        "empty",
        "None",
        "Texture",
        has_value=False,
        on_clear=lambda: cleared.append("empty"),
    )
    assigned = AssetReferenceFieldModel(
        "assigned",
        "smoke.png",
        "Texture",
        has_value=True,
        on_clear=lambda: cleared.append("assigned"),
    )

    empty.dispatch_chrome(int(ObjectFieldGesture.CLEAR))
    assigned.dispatch_chrome(int(ObjectFieldGesture.CLEAR))

    assert cleared == ["assigned"]


def test_asset_reference_catalog_reuses_one_database_generation(monkeypatch):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction.object_fields import AssetReferenceCatalog
    from Infernux.engine import project_context

    monkeypatch.setattr(project_context, "_project_root", "C:/Project")

    class Database:
        query_generation = 7

        def __init__(self):
            self.catalog_queries = 0
            self.paths = {
                "a": "C:/Project/Assets/Art/smoke.png",
                "b": "C:/Project/Assets/Materials/smoke.mat",
                "c": "C:/Engine/Resources/default.png",
            }

        def get_all_asset_paths(self):
            self.catalog_queries += 1
            return list(self.paths.values())

    database = Database()
    monkeypatch.setattr(AssetManager, "_asset_database", database)
    catalog = AssetReferenceCatalog()

    assert catalog.items("Texture", "smoke") == (
        ("smoke.png", "C:/Project/Assets/Art/smoke.png"),
    )
    assert catalog.items("Material", "smoke") == (
        ("smoke.mat", "C:/Project/Assets/Materials/smoke.mat"),
    )
    assert database.catalog_queries == 1

    database.query_generation = 8
    database.paths["d"] = "C:/Project/Assets/Art/cloud.tga"
    assert catalog.items("Texture", "cloud") == (
        ("cloud.tga", "C:/Project/Assets/Art/cloud.tga"),
    )
    assert database.catalog_queries == 2


def test_asset_reference_catalog_uses_assets_except_for_visible_shaders(monkeypatch):
    from Infernux.core.assets import AssetManager
    from Infernux.engine import project_context
    from Infernux.engine.interaction.object_fields import AssetReferenceCatalog
    from Infernux.engine.ui import inspector_shader_utils

    class Database:
        query_generation = 1
        paths = {
            "asset_mat": "C:/Project/Assets/Materials/visible.mat",
            "library_mat": "C:/Project/Library/Generated/internal.mat",
            "asset_shader": "C:/Project/Assets/Shaders/custom.vert",
            "builtin_shader": "C:/Project/Library/Resources/shaders/standard.vert",
            "hidden_shader": "C:/Project/Library/Resources/shaders/internal.vert",
        }

        def get_all_asset_paths(self):
            return tuple(self.paths.values())

    monkeypatch.setattr(project_context, "_project_root", "C:/Project")
    monkeypatch.setattr(AssetManager, "_asset_database", Database())
    hidden_queries = []
    monkeypatch.setattr(
        inspector_shader_utils,
        "is_shader_hidden",
        lambda path: hidden_queries.append(path)
        or path.replace("\\", "/").endswith("/internal.vert"),
    )
    catalog = AssetReferenceCatalog()

    assert catalog.items("Material", "") == (
        ("visible.mat", "C:/Project/Assets/Materials/visible.mat"),
    )
    assert catalog.items("Shader.Vertex", "") == (
        ("custom.vert", "C:/Project/Assets/Shaders/custom.vert"),
        ("standard.vert", "C:/Project/Library/Resources/shaders/standard.vert"),
    )
    assert len(hidden_queries) == 3

    assert catalog.items("Shader.Vertex", "standard") == (
        ("standard.vert", "C:/Project/Library/Resources/shaders/standard.vert"),
    )
    assert len(hidden_queries) == 3


def test_asset_reference_virtual_candidates_extend_the_shared_catalog(monkeypatch):
    from Infernux.engine.interaction.object_fields import (
        AssetReferenceFieldModel,
        asset_reference_catalog,
    )

    monkeypatch.setattr(
        asset_reference_catalog,
        "provider",
        lambda _asset_type: lambda _query: (
            ("Cube", {"builtin": "cube"}),
            ("Terrain.fbx", "Assets/Terrain.fbx"),
        ),
    )
    model = AssetReferenceFieldModel(
        "mesh",
        "None",
        "Mesh",
        additional_asset_items=lambda _query: (
            ("Cube", {"builtin": "cube"}),
            ("Sphere", {"builtin": "sphere"}),
        ),
    )

    assert tuple(model.asset_items("")) == (
        ("Cube", {"builtin": "cube"}),
        ("Sphere", {"builtin": "sphere"}),
        ("Terrain.fbx", "Assets/Terrain.fbx"),
    )


def test_native_object_field_reports_keyboard_clear_gesture():
    source = (
        Path(__file__).parents[2]
        / "cpp"
        / "infernux"
        / "function"
        / "renderer"
        / "gui"
        / "InxGUIContext.cpp"
    ).read_text(encoding="utf-8")

    assert "ImGuiKey_Delete" in source
    assert "ImGuiKey_Backspace" in source
    assert "result |= 16u" in source
    assert "result |= 32u" in source
    assert '"object_field." + fieldId' in source
    assert "resolvedSemanticId + \".picker\"" in source
    assert "clickable || hasPicker" in source


def test_object_picker_popup_is_owned_only_by_igui():
    ui_root = Path(__file__).parents[1] / "Infernux" / "engine" / "ui"
    offenders = []
    for path in ui_root.rglob("*.py"):
        if path.name == "igui.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "_render_object_picker_popup" in text:
            offenders.append(path.relative_to(ui_root).as_posix())
    assert offenders == []


def test_asset_domains_do_not_fall_back_to_generic_object_field_api():
    ui_root = Path(__file__).parents[1] / "Infernux" / "engine" / "ui"
    generic_reference_modules = {
        "_inspector_list_field.py",
        "_inspector_references.py",
        "igui.py",
        "inspector_ui_components.py",
    }
    offenders = []
    for path in ui_root.rglob("*.py"):
        if path.name in generic_reference_modules:
            continue
        text = path.read_text(encoding="utf-8")
        if "render_object_field(" in text or "IGUI.object_field(" in text:
            offenders.append(path.relative_to(ui_root).as_posix())
    assert offenders == []


def test_asset_domains_do_not_define_private_asset_catalogs():
    package_root = Path(__file__).parents[1] / "Infernux"
    roots = (package_root / "components", package_root / "engine" / "ui")
    offenders = []
    forbidden = ("_picker_assets", "_picker_texture_assets", "AssetManager.find_assets(")
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(token in text for token in forbidden):
                offenders.append(path.relative_to(package_root).as_posix())
    assert offenders == []


def test_asset_reference_calls_use_one_assignment_entry_point():
    package_root = Path(__file__).parents[1] / "Infernux"
    asset_calls = {
        "AssetReferenceFieldModel",
        "asset_reference_field",
        "render_asset_reference_field",
    }
    legacy_keywords = {"on_drop", "on_drop_callback", "on_pick"}
    offenders = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            if name not in asset_calls:
                continue
            bad = sorted(
                keyword.arg
                for keyword in node.keywords
                if keyword.arg in legacy_keywords
            )
            if bad:
                offenders.append(
                    (path.relative_to(package_root).as_posix(), node.lineno, bad)
                )
    assert offenders == []
