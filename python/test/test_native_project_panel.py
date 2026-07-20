"""Tests for native ProjectPanel."""
import os
import json
import tempfile
from pathlib import Path
import pytest
from Infernux.lib import AssetMutationResult, ProjectPanel
from Infernux.engine.ui.project_file_ops import (
    create_material,
    create_physic_material,
    create_prefab_from_gameobject,
    create_particlegraph,
)


class TestProjectPanelCreation:

    def test_creation(self):
        pp = ProjectPanel()
        assert pp is not None

    def test_is_editor_panel(self):
        from Infernux.lib import EditorPanel
        pp = ProjectPanel()
        assert isinstance(pp, EditorPanel)

    def test_window_id(self):
        pp = ProjectPanel()
        assert pp.get_window_id() == "project"

    def test_default_open(self):
        pp = ProjectPanel()
        assert pp.is_open()

    def test_create_physic_material_writes_strict_document_and_imports(self, tmp_path):
        class RecordingAssetDatabase:
            def __init__(self):
                self.paths = []

            def import_asset(self, path):
                self.paths.append(path)
                result = AssetMutationResult()
                result.succeeded = True
                result.database_committed = True
                result.changed = True
                result.guid = "physic-material-guid"
                return result

        database = RecordingAssetDatabase()
        ok, error = create_physic_material(str(tmp_path), "Ice", database)

        assert ok is True, error
        path = tmp_path / "Ice.physicMaterial"
        assert database.paths == [str(path)]
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "friction": 0.4,
            "bounciness": 0.0,
            "friction_combine": 0,
            "bounce_combine": 0,
        }

    def test_create_particlegraph_callback(self):
        pp = ProjectPanel()
        pp.create_particlegraph = lambda cur, name: (True, "")
        opened = []
        pp.open_particle_graph = lambda path: opened.append(path)

        assert pp.create_particlegraph("/path", "Smoke") == (True, "")
        pp.open_particle_graph("/path/Smoke.particlegraph")
        assert opened == ["/path/Smoke.particlegraph"]

    def test_create_material_writes_current_document(self, tmp_path, engine):
        from Infernux.lib import InxMaterial

        class RecordingAssetDatabase:
            def __init__(self):
                self.paths = []

            def import_asset(self, path):
                self.paths.append(path)
                result = AssetMutationResult()
                result.succeeded = True
                result.database_committed = True
                result.changed = True
                result.guid = "material-guid"
                return result

        database = RecordingAssetDatabase()
        ok, error = create_material(str(tmp_path), "NewMaterial", database)

        assert ok is True, error
        path = tmp_path / "NewMaterial.mat"
        assert database.paths == [str(path)]
        document = json.loads(path.read_text(encoding="utf-8"))
        assert "material_version" not in document
        assert document["shaders"]["vertex"]["shader_id"] == "standard"
        assert document["shaders"]["fragment"]["shader_id"] == "unlit"
        assert document["name"] == "NewMaterial"
        assert document["builtin"] is False
        material = InxMaterial()
        assert material.deserialize(json.dumps(document)) is True
        assert material.name == "NewMaterial"

    def test_new_material_import_primes_native_preview(self, tmp_path, monkeypatch):
        from Infernux.core.assets import AssetManager

        path = tmp_path / "Fresh.mat"
        path.write_text('{"name":"Fresh"}', encoding="utf-8")

        class Native:
            def __init__(self):
                self.queries = []
                self.full_speed_requests = 0

            def query_or_schedule_material_preview(self, *args):
                self.queries.append(args)
                return 0

            def request_full_speed_frame(self):
                self.full_speed_requests += 1

        native = Native()
        monkeypatch.setattr(AssetManager, "_native_engine", classmethod(lambda cls: native))

        AssetManager._prime_material_preview(str(path))

        normalized = os.path.normpath(str(path))
        assert native.queries == [(f"mat|{normalized}", normalized, "", path.stat().st_mtime_ns, False)]
        assert native.full_speed_requests == 1

        native.queries.clear()
        AssetManager._prime_material_preview(str(path), '{"name":"Fresh"}')
        assert native.queries == [(
            f"mat|{normalized}", normalized, '{"name":"Fresh"}', 0, False,
        )]

    def test_create_prefab_links_the_saved_source(self, tmp_path, monkeypatch):
        from Infernux.engine import prefab_manager

        source = type("GameObject", (), {"name": "CheckpointGate"})()
        linked = []
        monkeypatch.setattr(prefab_manager, "save_prefab", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            prefab_manager,
            "_link_created_prefab_source",
            lambda game_object, path, database: linked.append((game_object, path, database)) or True,
        )
        database = object()

        ok, path = create_prefab_from_gameobject(source, str(tmp_path), database)

        assert ok is True
        assert path == str(tmp_path / "CheckpointGate.prefab")
        assert linked == [(source, path, database)]


class TestProjectPanelPaths:

    def test_set_root_path(self):
        pp = ProjectPanel()
        with tempfile.TemporaryDirectory() as d:
            pp.set_root_path(d)
            # No crash

    def test_get_set_current_path(self):
        pp = ProjectPanel()
        with tempfile.TemporaryDirectory() as d:
            pp.set_root_path(d)
            pp.set_current_path(d)
            assert pp.get_current_path() == d

    def test_set_current_path_empty(self):
        pp = ProjectPanel()
        pp.set_current_path("")
        assert pp.get_current_path() == ""

    def test_set_icons_directory(self):
        pp = ProjectPanel()
        with tempfile.TemporaryDirectory() as d:
            pp.set_icons_directory(d)
            # No crash


class TestProjectPanelCallbacks:

    def test_translate_callback(self):
        pp = ProjectPanel()
        pp.translate = lambda key: f"[{key}]"
        assert pp.translate("project.create_folder") == "[project.create_folder]"

    def test_on_file_selected_callback(self):
        pp = ProjectPanel()
        received = []
        pp.on_file_selected = lambda path: received.append(path)
        assert pp.on_file_selected is not None

    def test_on_empty_area_clicked_callback(self):
        pp = ProjectPanel()
        called = []
        pp.on_empty_area_clicked = lambda: called.append(True)
        assert pp.on_empty_area_clicked is not None

    def test_on_state_changed_callback(self):
        pp = ProjectPanel()
        called = []
        pp.on_state_changed = lambda: called.append(True)
        assert pp.on_state_changed is not None

    def test_create_folder_callback(self):
        pp = ProjectPanel()
        results = []
        pp.create_folder = lambda cur, name: (
            results.append((cur, name)) or (True, "")
        )
        ok, err = pp.create_folder("/path", "NewFolder")
        assert results == [("/path", "NewFolder")]

    def test_create_script_callback(self):
        pp = ProjectPanel()
        pp.create_script = lambda cur, name: (True, "")
        ok, err = pp.create_script("/path", "MyScript")
        assert ok is True

    def test_create_shader_callback(self):
        pp = ProjectPanel()
        pp.create_shader = lambda cur, name, typ: (True, "")
        ok, err = pp.create_shader("/path", "MyShader", "unlit")
        assert ok is True

    def test_create_material_callback(self):
        pp = ProjectPanel()
        pp.create_material = lambda cur, name: (True, "")
        ok, err = pp.create_material("/path", "MyMat")
        assert ok is True

    def test_create_physic_material_callback(self):
        pp = ProjectPanel()
        pp.create_physic_material = lambda cur, name: (True, "")
        ok, err = pp.create_physic_material("/path", "Ice")
        assert ok is True

    def test_create_scene_callback(self):
        pp = ProjectPanel()
        pp.create_scene = lambda cur, name: (True, "")
        ok, err = pp.create_scene("/path", "Main")
        assert ok is True

    def test_delete_items_callback(self):
        pp = ProjectPanel()
        deleted = []
        pp.delete_items = lambda paths: deleted.extend(paths)
        pp.delete_items(["/a", "/b"])
        assert deleted == ["/a", "/b"]

    def test_do_rename_callback(self):
        pp = ProjectPanel()
        pp.do_rename = lambda old, new_name: f"/dir/{new_name}"
        result = pp.do_rename("/dir/old.txt", "new.txt")
        assert result == "/dir/new.txt"

    def test_project_asset_operations_publish_stable_semantics(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        assert '"project.context.rename"' in source
        assert '"project.context.delete"' in source
        assert '"project.rename.input"' in source
        assert 'itemSemanticId + ".expand"' in source
        assert '"project_model_expand"' in source

    def test_project_asset_selection_waits_for_non_drag_release(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        icon_render = source[source.index("// ── Render icon"):source.index("// ── Drag-drop source")]

        assert icon_render.count("ImGui::IsMouseReleased(ImGuiMouseButton_Left)") == 2
        assert "const bool thumbClicked = ImGui::IsItemClicked(0)" not in icon_render

    def test_empty_prefab_drop_area_is_an_actual_drag_drop_target(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        drop_area = source.index('ctx->InvisibleButton("##drop_prefab_area"')
        drop_target = source.index("ctx->BeginDragDropTarget()", drop_area)
        accept_payload = source.index("ctx->AcceptDragDropPayload(DRAG_TYPE_HIERARCHY_GO", drop_target)
        create_prefab = source.index("createPrefabFromHierarchy(objId, m_currentPath)", accept_payload)
        click_handler = source.index("ctx->IsItemClicked(0)", create_prefab)

        assert drop_area < drop_target < accept_payload < create_prefab < click_handler

    def test_file_grid_background_semantic_survives_a_vertically_filled_grid(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        gutter_capture = source.index("const float gutterWidth = cellW - iconSize")
        bottom_area = source.index('ctx->InvisibleButton("##drop_prefab_area"')
        fallback = source.index("} else if (captureSemantics && semanticBackgroundMax.x", bottom_area)

        assert '"project.file_grid.background"' in source
        assert source.count('"project.file_grid.background"') == 2
        assert gutter_capture < bottom_area < fallback
        assert source.count('ctx->RecordSemanticRect("project_background", "File Grid Background"') == 2

    def test_project_preview_rendering_uses_catalog_stamps_without_ui_thread_polling(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        snapshot_cache = source[source.index("ProjectPanel::DirSnapshot *ProjectPanel::GetDirSnapshot"):
                                source.index("ProjectPanel::DirTreeMeta *ProjectPanel::GetDirTreeMeta")]
        grid_preview = source[source.index("// ── Resolve display texture"):
                              source.index("// ── Render icon")]

        assert "if (catalog || (m_frameTimeNow - it->second.lastValidatedAt) < DIR_CACHE_TTL)" in snapshot_cache
        assert "GetMaterialThumbnail(item.path, item.mtimeNs)" in grid_preview
        assert "GetModelThumbnail(item.path, item.mtimeNs)" in grid_preview
        assert "IsUiPrefabFile(item.path, item.mtimeNs)" in grid_preview
        assert grid_preview.count("IsUiPrefabFile(") == 1

    def test_get_unique_name_callback(self):
        pp = ProjectPanel()
        pp.get_unique_name = lambda cur, base, ext: f"{base}_1{ext}"
        result = pp.get_unique_name("/dir", "File", ".txt")
        assert result == "File_1.txt"

    def test_move_item_to_directory_callback(self):
        pp = ProjectPanel()
        pp.move_item_to_directory = lambda item, dest: f"{dest}/moved"
        result = pp.move_item_to_directory("/a/b.txt", "/c")
        assert result == "/c/moved"

    def test_open_file_callback(self):
        pp = ProjectPanel()
        opened = []
        pp.open_file = lambda path: opened.append(path)
        pp.open_file("/test.py")
        assert opened == ["/test.py"]

    def test_open_scene_callback(self):
        pp = ProjectPanel()
        opened = []
        pp.open_scene = lambda path: opened.append(path)
        pp.open_scene("/test.scene")
        assert opened == ["/test.scene"]

    def test_open_prefab_mode_callback(self):
        pp = ProjectPanel()
        opened = []
        pp.open_prefab_mode = lambda path: opened.append(path)
        pp.open_prefab_mode("/test.prefab")
        assert opened == ["/test.prefab"]

    def test_reveal_in_explorer_callback(self):
        pp = ProjectPanel()
        revealed = []
        pp.reveal_in_explorer = lambda path: revealed.append(path)
        pp.reveal_in_explorer("/dir")
        assert revealed == ["/dir"]

    def test_validate_script_component_callback(self):
        pp = ProjectPanel()
        pp.validate_script_component = lambda path: path.endswith(".py")
        assert pp.validate_script_component("/test.py") is True
        assert pp.validate_script_component("/test.txt") is False

    def test_guid_callbacks(self):
        pp = ProjectPanel()
        pp.get_guid_from_path = lambda path: "guid-123" if path else ""
        pp.get_path_from_guid = lambda guid: "/test.txt" if guid else ""

        assert pp.get_guid_from_path("/test.txt") == "guid-123"
        assert pp.get_path_from_guid("guid-123") == "/test.txt"

    def test_invalidate_asset_inspector_callback(self):
        pp = ProjectPanel()
        invalidated = []
        pp.invalidate_asset_inspector = lambda path: invalidated.append(path)
        pp.invalidate_asset_inspector("/asset.mat")
        assert invalidated == ["/asset.mat"]

    def test_create_prefab_from_hierarchy_callback(self):
        pp = ProjectPanel()
        created = []
        pp.create_prefab_from_hierarchy = lambda oid, path: created.append((oid, path))
        pp.create_prefab_from_hierarchy(42, "/Assets")
        assert created == [(42, "/Assets")]


class TestProjectPanelPublicAPI:

    def test_clear_selection(self):
        pp = ProjectPanel()
        pp.clear_selection()  # No crash

    def test_set_selected_file(self):
        pp = ProjectPanel()
        pp.set_selected_file("/tmp/test.mat")
        # No crash — used by selection undo replay

    def test_invalidate_material_thumbnail(self):
        pp = ProjectPanel()
        pp.invalidate_material_thumbnail("/path/to/mat.mat")
        # No crash — clears internal thumbnail cache entry

    def test_set_open(self):
        pp = ProjectPanel()
        pp.set_open(False)
        assert not pp.is_open()
        pp.set_open(True)
        assert pp.is_open()
