"""Tests for native ProjectPanel."""
import os
import json
import tempfile
from pathlib import Path
import pytest
from Infernux.lib import AssetMutationResult, ProjectPanel
from Infernux.engine.ui.project_file_ops import (
    SCRIPT_TEMPLATE,
    create_material,
    create_physic_material,
    create_prefab_from_gameobject,
    create_particlegraph,
    create_render_effect,
    create_render_effect_group,
)


class TestProjectPanelCreation:

    def test_script_template_imports_component_surface(self):
        assert "from Infernux import *" in SCRIPT_TEMPLATE
        assert "from Infernux.components import *" in SCRIPT_TEMPLATE

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

    def test_explicit_global_rename_target_bridges_empty_native_selection(self):
        pp = ProjectPanel()
        target = "C:/Project/Assets/Smoke.mat"

        assert pp.can_rename_selected_asset(target)
        assert pp.begin_rename_selected_asset(target)

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

    def test_create_render_effect_assets_write_current_documents(self, tmp_path):
        from Infernux.renderstack.render_effect_asset import (
            RenderEffectAsset,
            RenderEffectGroupAsset,
            parse_render_effect_document,
        )

        ok, error = create_render_effect(
            str(tmp_path), "Pixels", "infernux.route.pixelation"
        )
        assert ok is True, error
        effect = parse_render_effect_document(
            (tmp_path / "Pixels.effect").read_text(encoding="utf-8")
        )
        assert isinstance(effect, RenderEffectAsset)
        assert effect.feature_type == "infernux.route.pixelation"
        assert effect.parameters == {}

        ok, error = create_render_effect_group(str(tmp_path), "Post")
        assert ok is True, error
        group = parse_render_effect_document(
            (tmp_path / "Post.effectgroup").read_text(encoding="utf-8")
        )
        assert isinstance(group, RenderEffectGroupAsset)
        assert group.entries == ()

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
        assert document["shaders"]["vertex"]["shader_id"] == "Standard"
        assert document["shaders"]["fragment"]["shader_id"] == "Unlit"
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
            assert pp.can_navigate_to_path(d)
            assert pp.set_current_path(d)
            assert pp.get_current_path() == d

    def test_set_current_path_empty(self):
        pp = ProjectPanel()
        assert pp.set_current_path("") is False
        assert pp.get_current_path() == ""

    def test_selection_projection_does_not_navigate_to_asset_parent(self, tmp_path):
        assets = tmp_path / "Assets"
        first = assets / "First"
        second = assets / "Second"
        first.mkdir(parents=True)
        second.mkdir()
        asset = second / "Selected.mat"
        asset.write_text("{}", encoding="utf-8")
        pp = ProjectPanel()
        pp.set_root_path(str(tmp_path))
        assert pp.set_current_path(str(first))

        pp.set_selected_file(str(asset), False)

        assert pp.get_current_path() == str(first)

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

    def test_external_drop_is_forwarded_as_one_global_command(self, tmp_path):
        assets = tmp_path / "Assets"
        assets.mkdir()
        pp = ProjectPanel()
        pp.set_root_path(str(tmp_path))
        pp.set_current_path(str(assets))
        calls = []
        pp.execute_command = (
            lambda command_id, source, argument: calls.append(
                (command_id, source, json.loads(argument))
            )
            or True
        )

        pp.receive_dropped_files(["C:/External/One.png", "C:/External/Two.png"])

        assert calls == [
            (
                "asset.import_external",
                "drag_drop",
                {
                    "paths": ["C:/External/One.png", "C:/External/Two.png"],
                    "destination": str(assets),
                },
            )
        ]

    def test_selection_snapshot_reports_all_paths_and_supports_silent_sync(self, tmp_path):
        first = tmp_path / "First.mat"
        second = tmp_path / "Second.mat"
        first.write_text("{}", encoding="utf-8")
        second.write_text("{}", encoding="utf-8")
        pp = ProjectPanel()
        pp.set_root_path(str(tmp_path))
        received = []
        pp.on_selection_changed = (
            lambda paths, primary: received.append((list(paths), primary))
        )

        pp.set_selected_file(str(first))
        pp.clear_selection(False)
        pp.set_selected_file(str(second), False)

        assert received == [([str(first)], str(first))]

    def test_set_selected_files_publishes_one_multi_selection_snapshot(self, tmp_path):
        first = tmp_path / "First.mat"
        second = tmp_path / "Second.mat"
        first.write_text("{}", encoding="utf-8")
        second.write_text("{}", encoding="utf-8")
        pp = ProjectPanel()
        pp.set_root_path(str(tmp_path))
        received = []
        pp.on_selection_changed = (
            lambda paths, primary: received.append((list(paths), primary))
        )

        pp.set_selected_files([str(first), str(second)], str(first))

        assert received == [([str(first), str(second)], str(first))]

    def test_on_state_changed_callback(self):
        pp = ProjectPanel()
        called = []
        pp.on_state_changed = lambda: called.append(True)
        assert pp.on_state_changed is not None

    def test_project_command_adapters_share_one_selection_contract(self, tmp_path):
        asset = tmp_path / "Selected.mat"
        asset.write_text("{}", encoding="utf-8")
        pp = ProjectPanel()
        pp.set_root_path(str(tmp_path))
        pp.set_selected_file(str(asset))
        assert pp.has_selected_assets() is True
        assert pp.can_rename_selected_asset() is True
        assert pp.can_rename_selected_asset(str(asset)) is True

    def test_project_drag_move_emits_one_global_transfer_command(self):

        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(
            encoding="utf-8"
        )
        move_body = source[
            source.index("void ProjectPanel::MoveProjectItemsToFolder") : source.index(
                "void ProjectPanel::PreRender"
            )
        ]
        assert 'ExecuteEditorCommand("asset.transfer"' in move_body
        assert "MakeAssetTransferCommandArgument(sources, targetDir)" in move_body
        assert "moveItemToDirectory" not in move_body

    def test_project_panel_exposes_no_asset_business_callback_bridge(self):
        header = Path("cpp/infernux/function/editor/ProjectPanel.h").read_text(
            encoding="utf-8"
        )
        binding = Path("cpp/infernux/tools/pybinding/BindingGUI.cpp").read_text(
            encoding="utf-8"
        )
        bootstrap = Path("python/Infernux/engine/bootstrap_project.py").read_text(
            encoding="utf-8"
        )
        forbidden = (
            "writeAssetClipboard",
            "readAssetClipboard",
            "consumeAssetClipboard",
            "pasteAssetClipboard",
            "moveAssetPaths",
            "createAsset",
            "deleteItems",
            "getUniqueName",
            "openAsset",
            "revealInExplorer",
            "write_asset_clipboard",
            "read_asset_clipboard",
            "consume_asset_clipboard",
            "paste_asset_clipboard",
            "move_asset_paths",
            "pp.create_asset",
            "pp.delete_items",
            "pp.open_asset",
            "pp.reveal_in_explorer",
        )
        for token in forbidden:
            assert token not in header
            assert token not in binding
            assert token not in bootstrap

    def test_project_shortcuts_are_not_polled_inside_the_panel(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(
            encoding="utf-8"
        )
        menu_source = Path(
            "python/Infernux/engine/ui/core_context_menus.py"
        ).read_text(encoding="utf-8")

        assert "HandleKeyboardShortcuts" not in source
        assert 'ExecuteEditorCommand("edit.copy")' not in source
        assert 'ExecuteEditorCommand("edit.delete")' not in source
        assert 'ExecuteEditorCommand("project.create_folder")' not in source
        assert '"edit.copy"' in menu_source
        assert '"project.create_folder"' in menu_source

    def test_inline_rename_uses_global_command_without_native_callback(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(
            encoding="utf-8"
        )
        header = Path("cpp/infernux/function/editor/ProjectPanel.h").read_text(
            encoding="utf-8"
        )
        binding = Path("cpp/infernux/tools/pybinding/BindingGUI.cpp").read_text(
            encoding="utf-8"
        )

        assert 'ExecuteEditorCommand("asset.rename"' in source
        assert "doRename" not in header
        assert 'def_readwrite("do_rename"' not in binding

    def test_folder_rename_maps_nested_asset_paths(self, tmp_path, monkeypatch):
        from Infernux.engine.ui import project_file_ops

        source = tmp_path / "OldFolder"
        nested = source / "Nested"
        nested.mkdir(parents=True)
        (source / "root.mat").write_text("{}", encoding="utf-8")
        (nested / "child.png").write_bytes(b"png")

        moved = []
        monkeypatch.setattr(
            project_file_ops,
            "_notify_asset_moved",
            lambda old, new, _database=None: moved.append((Path(old), Path(new))),
        )

        destination = project_file_ops.do_rename(str(source), "RenamedFolder")

        expected = tmp_path / "RenamedFolder"
        assert Path(destination) == expected.resolve()
        assert not source.exists()
        assert (expected / "root.mat").is_file()
        assert (expected / "Nested" / "child.png").is_file()
        assert set(moved) == {
            (source.resolve() / "root.mat", expected.resolve() / "root.mat"),
            (
                source.resolve() / "Nested" / "child.png",
                expected.resolve() / "Nested" / "child.png",
            ),
        }

    def test_project_asset_operations_publish_stable_semantics(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        menu_source = Path(
            "python/Infernux/engine/ui/core_context_menus.py"
        ).read_text(encoding="utf-8")
        assert '"project.context.rename"' in menu_source
        assert '"project.context.delete"' in menu_source
        assert '"project.rename.input"' in source
        assert 'itemSemanticId + ".expand"' in source
        assert '"project_model_expand"' in source

    def test_particle_runtime_artifacts_are_hidden_from_the_project_panel(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        hidden_extensions = source[
            source.index("sHiddenExtensions") : source.index("sHiddenFiles")
        ]

        assert '".inxparticle"' in hidden_extensions
        assert '".inxtex"' in hidden_extensions
        assert '".inxvfield"' in hidden_extensions
        assert '".inxsdf"' in hidden_extensions

    def test_project_asset_selection_waits_for_non_drag_release(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        icon_render = source[source.index("// ── Render icon"):source.index("// ── Drag-drop source")]

        assert icon_render.count("ImGui::IsMouseReleased(ImGuiMouseButton_Left)") == 2
        assert "const bool thumbClicked = ImGui::IsItemClicked(0)" not in icon_render

    def test_project_selection_feedback_uses_normalized_path_keys(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        selection = source[source.index("void ProjectPanel::SetSelectedFile"):
                           source.index("void ProjectPanel::InvalidateMaterialThumbnail")]
        grid = source[source.index("void ProjectPanel::RenderFileGrid"):
                      source.index("void ProjectPanel::RenderContextMenu")]

        assert "m_selectedSet.insert(AssetSelectionPathKey(path))" in selection
        assert "m_selectedSet = std::move(keys)" in selection
        assert "m_selectedSet.count(AssetSelectionPathKey(item.path))" in grid
        assert "m_selectedSet.count(item.path)" not in grid
        assert 'itemSemanticId, isSelected' in grid

    def test_project_multi_selection_uses_normalized_path_identity(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        click = source[source.index("void ProjectPanel::HandleItemClick"):
                       source.index("bool ProjectPanel::HasSelectedAssets")]

        assert "AssetSelectionPathKey(path) == itemKey" in click
        assert "visibleKey == anchorKey" in click
        assert "visibleKey == targetKey" in click
        assert "vi.path == m_selectedFile" not in click

    def test_project_user_selection_is_intent_only_until_global_projection(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        click = source[
            source.index("void ProjectPanel::HandleItemClick") : source.index("bool ProjectPanel::HasSelectedAssets")
        ]
        grid = source[
            source.index("void ProjectPanel::RenderFileGrid") : source.index("void ProjectPanel::RenderContextMenu")
        ]

        assert "m_selectedFile = item.path" not in click
        assert "m_selectedFiles = {item.path}" not in click
        assert "PublishSelectionIntent({item.path}, item.path)" in click
        assert "m_selectedFile = item.path" not in grid
        assert "PublishSelectionIntent(alreadySelected" in grid

    def test_project_context_menu_lifecycle_is_independent_of_directory_data(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(
            encoding="utf-8"
        )
        grid = source[
            source.index("void ProjectPanel::RenderFileGrid") : source.index(
                "void ProjectPanel::RenderContextMenu"
            )
        ]

        popup_guard = grid.index('ImGui::IsPopupOpen("ProjectContextMenu")')
        snapshot_lookup = grid.index("GetDirSnapshot(m_currentPath)")
        assert popup_guard < snapshot_lookup
        assert "bool contextMenuRendered = false;" in grid
        assert "if (!contextMenuRendered)" in grid

    def test_project_callbacks_never_forge_a_missing_command_context(self):
        bootstrap = Path("python/Infernux/engine/bootstrap_project.py").read_text(
            encoding="utf-8"
        )

        assert "_activate_project_command_context" not in bootstrap

    def test_project_subresources_use_typed_identity_and_real_backing_paths(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        path_header = Path("cpp/infernux/platform/filesystem/InxPath.h").read_text(encoding="utf-8")

        assert "static std::string AssetSelectionPathKey" in source
        assert "return infernux::AssetPathKey(path);" in source
        assert "inline std::string AssetPathKey" in path_header
        assert "const std::string backingPath = ResolveRealAssetPath(path)" in source
        assert "const std::string &animVirtualBase = modelPath" in source
        assert "animVirtualBase = std::move(g)" not in source

    def test_project_folder_semantics_publish_the_current_selection(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        tree = source[source.index("void ProjectPanel::RenderFolderTree"):
                      source.index("void ProjectPanel::RenderFileGrid")]

        assert 'row.isRoot ? "project.folder.root" : MakeProjectFolderSemanticId(row.path)' in tree
        assert "ctx->RecordSemanticItem(\"project_folder\", row.name, true, semanticId, selected)" in tree

    def test_project_folder_tree_uses_revision_cache_and_clipper(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        header = Path("cpp/infernux/function/editor/ProjectPanel.h").read_text(encoding="utf-8")
        tree = source[source.index("void ProjectPanel::RebuildFolderTreeRows"):
                      source.index("// File grid")]

        assert "m_directoryRevision" in header
        assert "m_folderTreeRowsProjectionRevision" in header
        assert "m_folderTreeRowsDirectoryRevision != m_directoryRevision" in tree
        assert "m_folderTreeProjection.Revision()" in tree
        assert "ImGuiListClipper" in tree
        assert "++m_directoryRevision" in source

    def test_empty_prefab_drop_area_is_an_actual_drag_drop_target(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        drop_area = source.index('ctx->InvisibleButton("##drop_prefab_area"')
        drop_target = source.index("ctx->BeginDragDropTarget()", drop_area)
        accept_payload = source.index("ctx->AcceptDragDropPayload(DRAG_TYPE_HIERARCHY_GO", drop_target)
        create_prefab = source.index('ExecuteEditorCommand("prefab.save_as"', accept_payload)
        click_handler = source.index("ctx->IsItemClicked(0)", create_prefab)

        assert drop_area < drop_target < accept_payload < create_prefab < click_handler

    def test_prefab_drop_uses_global_command_without_a_native_callback_bridge(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        header = Path("cpp/infernux/function/editor/ProjectPanel.h").read_text(encoding="utf-8")
        binding = Path("cpp/infernux/tools/pybinding/BindingGUI.cpp").read_text(encoding="utf-8")
        bootstrap = Path("python/Infernux/engine/bootstrap_project.py").read_text(encoding="utf-8")

        assert source.count('ExecuteEditorCommand("prefab.save_as"') == 3
        assert "MakePrefabSaveAsCommandArgument" in source
        assert 'if command_id == "prefab.save_as":' in bootstrap
        assert '"object_id": resolved_object_id' in bootstrap
        assert '"current_path": current_path' in bootstrap
        for text in (header, binding, bootstrap):
            assert "createPrefabFromHierarchy" not in text
            assert "create_prefab_from_hierarchy" not in text

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

    def test_project_search_filters_a_generation_cached_memory_index(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        search = source[source.index("void ProjectPanel::RebuildSearchIndex"):
                        source.index("void ProjectPanel::RenderSearchResults")]

        assert "m_searchIndexGeneration" in search
        assert "m_searchIndex.push_back" in search
        assert "m_search.MatchesNormalized(indexed.searchKey)" in search
        assert "m_search.MakeToken(generation, folderRoot)" in search
        assert "CollectMatchingFolders" not in source
        assert "directory_iterator" not in search

    def test_project_search_exposes_the_shared_semantic_target(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")

        assert '"project.search"' in source
        assert 'RecordSemanticItem("text_input", Tr("project.search_hint")' in source

    def test_search_activation_does_not_clear_the_container_being_iterated(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        search = source[source.index("void ProjectPanel::RenderSearchResults"):
                        source.index("// Folder tree")]
        iteration = search[search.index("for (const auto &item : m_searchResults)"):
                           search.index("if (!hasActivatedItem)")]

        assert "activatedItem = item" in iteration
        assert "m_searchResults.clear()" not in iteration
        assert search.index("RequestDirectoryNavigation(activatedItem.path)") < search.index(
            "m_searchResults.clear()"
        )
        assert search.index("RequestAssetLocation(activatedItem.path)") < search.index(
            "m_searchResults.clear()"
        )

    def test_parent_navigation_stops_using_the_previous_grid_snapshot(self):
        source = Path("cpp/infernux/function/editor/ProjectPanel.cpp").read_text(encoding="utf-8")
        grid = source[source.index("void ProjectPanel::RenderFileGrid"):
                      source.index("// Context menu")]
        parent_navigation = grid[grid.index('ctx->Selectable("[..]", false)'):
                                 grid.index("// Grid config")]

        assert "RequestDirectoryNavigation(parent)" in parent_navigation
        assert "return;" in parent_navigation

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
