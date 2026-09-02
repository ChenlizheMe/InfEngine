#include "function/resources/AssetDatabase/AssetDatabase.h"
#include "function/resources/AssetDependencyGraph.h"
#include "function/resources/InxResource/InxResourceMeta.h"

#include <pybind11/functional.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

namespace infernux
{

void RegisterAssetDatabaseBindings(py::module_ &m)
{
    // AssetEvent enum
    py::enum_<AssetEvent>(m, "AssetEvent")
        .value("Deleted", AssetEvent::Deleted)
        .value("Modified", AssetEvent::Modified)
        .value("Moved", AssetEvent::Moved);

    py::enum_<AssetMutationErrorCode>(m, "AssetMutationErrorCode")
        .value("NONE", AssetMutationErrorCode::None)
        .value("INVALID_PATH", AssetMutationErrorCode::InvalidPath)
        .value("NOT_FOUND", AssetMutationErrorCode::NotFound)
        .value("UNSUPPORTED_TYPE", AssetMutationErrorCode::UnsupportedType)
        .value("IMPORT_FAILED", AssetMutationErrorCode::ImportFailed)
        .value("RUNTIME_APPLY_FAILED", AssetMutationErrorCode::RuntimeApplyFailed);

    py::class_<AssetMutationResult>(m, "AssetMutationResult")
        .def(py::init<>())
        .def_readwrite("succeeded", &AssetMutationResult::succeeded)
        .def_readwrite("database_committed", &AssetMutationResult::databaseCommitted)
        .def_readwrite("changed", &AssetMutationResult::changed)
        .def_readwrite("operation", &AssetMutationResult::operation)
        .def_readwrite("guid", &AssetMutationResult::guid)
        .def_readwrite("path", &AssetMutationResult::path)
        .def_readwrite("previous_path", &AssetMutationResult::previousPath)
        .def_readwrite("resource_type", &AssetMutationResult::resourceType)
        .def_readwrite("error_code", &AssetMutationResult::errorCode)
        .def_readwrite("error", &AssetMutationResult::error)
        .def_readwrite("query_generation", &AssetMutationResult::queryGeneration)
        .def("__bool__", [](const AssetMutationResult &result) { return result.succeeded; });

    // AssetDependencyGraph singleton
    py::class_<AssetDependencyGraph, std::unique_ptr<AssetDependencyGraph, py::nodelete>>(m, "AssetDependencyGraph")
        .def_static("instance", &AssetDependencyGraph::Instance, py::return_value_policy::reference,
                    "Get the singleton instance")
        .def("add_asset_dependency", &AssetDependencyGraph::AddAssetDependency, py::arg("asset_guid"),
             py::arg("dependency_guid"), "Register an asset-to-asset dependency")
        .def("remove_asset_dependency", &AssetDependencyGraph::RemoveAssetDependency, py::arg("asset_guid"),
             py::arg("dependency_guid"), "Remove an asset-to-asset dependency")
        .def("clear_asset_dependencies_of", &AssetDependencyGraph::ClearAssetDependenciesOf, py::arg("asset_guid"),
             "Clear dependencies declared by an asset")
        .def("add_runtime_dependency", &AssetDependencyGraph::AddRuntimeDependency, py::arg("object_guid"),
             py::arg("asset_guid"), "Register runtime object usage of an asset")
        .def("remove_runtime_dependency", &AssetDependencyGraph::RemoveRuntimeDependency, py::arg("object_guid"),
             py::arg("asset_guid"), "Remove runtime object usage of an asset")
        .def("clear_runtime_dependencies_of", &AssetDependencyGraph::ClearRuntimeDependenciesOf, py::arg("object_guid"),
             "Clear asset usage declared by a runtime object")
        .def("remove_asset", &AssetDependencyGraph::RemoveAsset, py::arg("guid"), "Remove all records for an asset")
        .def("set_asset_dependencies", &AssetDependencyGraph::SetAssetDependencies, py::arg("asset_guid"),
             py::arg("dependency_guids"), "Replace all dependencies for an asset")
        .def("get_dependencies", &AssetDependencyGraph::GetDependencies, py::arg("guid"),
             "Get all GUIDs that this asset depends on")
        .def("get_dependents", &AssetDependencyGraph::GetDependents, py::arg("guid"),
             "Get all GUIDs that depend on this asset")
        .def("has_dependency", &AssetDependencyGraph::HasDependency, py::arg("user_guid"), py::arg("dependency_guid"),
             "Check if user depends on dependency")
        .def("get_edge_count", &AssetDependencyGraph::GetEdgeCount, "Total dependency edges")
        .def("get_node_count", &AssetDependencyGraph::GetNodeCount, "Total tracked assets")
        .def_property_readonly("asset_generation", &AssetDependencyGraph::GetAssetGeneration)
        .def("clear", &AssetDependencyGraph::Clear, "Clear the entire graph");

    // AssetDatabase
    py::class_<AssetDatabase>(m, "AssetDatabase")
        .def(py::init<>())
        .def("initialize", &AssetDatabase::Initialize, py::arg("project_root"),
             "Initialize asset database with project root")
        .def("refresh", &AssetDatabase::Refresh, "Refresh assets by scanning Assets folder")
        .def("begin_refresh", &AssetDatabase::BeginRefresh,
             "Schedule filesystem scan and fingerprint collection on the engine JobSystem")
        .def("try_commit_refresh", &AssetDatabase::TryCommitRefresh,
             "Advance asynchronous scan/import and finalize a completed artifact on the owner thread")
        .def("complete_pending_refresh", &AssetDatabase::CompletePendingRefresh,
             "Wait for and commit the currently pending asynchronous refresh")
        .def_property_readonly("refresh_pending", &AssetDatabase::IsRefreshPending)
        .def("flush_derived_index", &AssetDatabase::FlushDerivedIndex, py::arg("wait_for_pending_scan") = true,
             "Persist a dirty derived AssetIndex")
        .def("add_scan_root", &AssetDatabase::AddScanRoot, py::arg("path"),
             "Add an extra directory to scan during Refresh (e.g. Library/Resources)")
        .def("import_asset", &AssetDatabase::ImportAsset, py::arg("path"), "Import a single asset")
        .def("reimport_asset", &AssetDatabase::ReimportAsset, py::arg("path"), py::call_guard<py::gil_scoped_release>(),
             "Reimport an existing asset while preserving its GUID")
        .def("delete_asset", &AssetDatabase::DeleteAsset, py::arg("path"), "Delete asset and its meta")
        .def("move_asset", &AssetDatabase::MoveAsset, py::arg("old_path"), py::arg("new_path"),
             "Move/rename asset preserving GUID")
        .def("move_assets_batch", &AssetDatabase::MoveAssetsBatch, py::arg("moves"),
             "Commit a preflighted asset relocation batch with one catalog publication")
        .def("contains_guid", &AssetDatabase::ContainsGuid, py::arg("guid"), py::call_guard<py::gil_scoped_release>(),
             "Check if GUID exists")
        .def("contains_path", &AssetDatabase::ContainsPath, py::arg("path"), py::call_guard<py::gil_scoped_release>(),
             "Check if path exists")
        .def("get_guid_from_path", &AssetDatabase::GetGuidFromPath, py::arg("path"),
             py::call_guard<py::gil_scoped_release>(), "Get GUID from asset path")
        .def("get_path_from_guid", &AssetDatabase::GetPathFromGuid, py::arg("guid"),
             py::call_guard<py::gil_scoped_release>(), "Get asset path from GUID")
        .def(
            "get_meta_by_guid",
            [](const AssetDatabase &database, const std::string &guid) {
                return std::const_pointer_cast<InxResourceMeta>(database.GetMetaByGuid(guid));
            },
            py::arg("guid"), py::call_guard<py::gil_scoped_release>(), "Get immutable meta by GUID")
        .def(
            "get_meta_by_path",
            [](const AssetDatabase &database, const std::string &path) {
                return std::const_pointer_cast<InxResourceMeta>(database.GetMetaByPath(path));
            },
            py::arg("path"), py::call_guard<py::gil_scoped_release>(), "Get immutable meta by path")
        .def("get_all_guids", &AssetDatabase::GetAllGuids, py::call_guard<py::gil_scoped_release>(),
             "Get all GUIDs in one published generation")
        .def("get_all_asset_paths", &AssetDatabase::GetAllAssetPaths, py::call_guard<py::gil_scoped_release>(),
             "Get all asset paths in one published generation")
        .def(
            "get_directory_catalog",
            [](const AssetDatabase &database, const std::string &directory) {
                py::list result;
                for (const auto &entry : database.GetDirectoryCatalog(directory)) {
                    py::dict item;
                    item["guid"] = entry.guid;
                    item["path"] = entry.path;
                    item["name"] = entry.name;
                    item["resource_type"] = entry.resourceType;
                    item["size"] = entry.source.size;
                    item["modified_ns"] = entry.source.modifiedNs;
                    result.append(std::move(item));
                }
                return result;
            },
            py::arg("directory"), "Get one immutable-generation asset listing for a directory")
        .def_property_readonly("catalog_generation",
                               [](const AssetDatabase &database) {
                                   const auto catalog = database.GetCatalogSnapshot();
                                   return catalog ? catalog->GetGeneration() : uint64_t{0};
                               })
        .def_property_readonly("query_generation", &AssetDatabase::GetQueryGeneration)
        .def_property_readonly("asset_count", &AssetDatabase::GetAssetCount)
        .def_property_readonly("last_refresh_reused_count", &AssetDatabase::GetLastRefreshReusedCount)
        .def_property_readonly("last_refresh_imported_count", &AssetDatabase::GetLastRefreshImportedCount)
        .def_property_readonly("last_refresh_imported_paths", &AssetDatabase::GetLastRefreshImportedPaths)
        .def_property_readonly("last_refresh_importer_task_count", &AssetDatabase::GetLastRefreshImporterTaskCount)
        .def_property_readonly("last_refresh_metadata_task_count", &AssetDatabase::GetLastRefreshMetadataTaskCount)
        .def_property_readonly("last_refresh_worker_metadata_count", &AssetDatabase::GetLastRefreshWorkerMetadataCount)
        .def_property_readonly("last_refresh_worker_importer_count", &AssetDatabase::GetLastRefreshWorkerImporterCount)
        .def_property_readonly("last_refresh_scanned_count", &AssetDatabase::GetLastRefreshScannedCount)
        .def_property_readonly("last_refresh_scan_ms", &AssetDatabase::GetLastRefreshScanMilliseconds)
        .def_property_readonly("last_refresh_commit_ms", &AssetDatabase::GetLastRefreshCommitMilliseconds)
        .def_property_readonly("last_refresh_prepare_ms", &AssetDatabase::GetLastRefreshPrepareMilliseconds)
        .def_property_readonly("last_refresh_finalize_ms", &AssetDatabase::GetLastRefreshFinalizeMilliseconds)
        .def_property_readonly("last_refresh_owner_merge_max_slice_ms",
                               &AssetDatabase::GetLastRefreshOwnerMergeMaxSliceMilliseconds)
        .def_property_readonly("last_refresh_owner_merge_slice_count",
                               &AssetDatabase::GetLastRefreshOwnerMergeSliceCount)
        .def_property_readonly("last_refresh_metadata_write_ms",
                               &AssetDatabase::GetLastRefreshMetadataWriteMilliseconds)
        .def_property_readonly("last_refresh_journal_uncompressed_bytes",
                               &AssetDatabase::GetLastRefreshJournalUncompressedBytes)
        .def_property_readonly("last_refresh_journal_bytes", &AssetDatabase::GetLastRefreshJournalBytes)
        .def_property_readonly("last_refresh_journal_serialize_ms",
                               &AssetDatabase::GetLastRefreshJournalSerializeMilliseconds)
        .def_property_readonly("last_refresh_journal_write_ms", &AssetDatabase::GetLastRefreshJournalWriteMilliseconds)
        .def_property_readonly("last_refresh_journal_apply_ms", &AssetDatabase::GetLastRefreshJournalApplyMilliseconds)
        .def_property_readonly("last_refresh_restore_ms", &AssetDatabase::GetLastRefreshRestoreMilliseconds)
        .def_property_readonly("last_refresh_import_ms", &AssetDatabase::GetLastRefreshImportMilliseconds)
        .def_property_readonly("last_refresh_index_build_ms", &AssetDatabase::GetLastRefreshIndexBuildMilliseconds)
        .def_property_readonly("last_refresh_index_save_ms", &AssetDatabase::GetLastRefreshIndexSaveMilliseconds)
        .def_property_readonly("last_refresh_index_build_on_worker", &AssetDatabase::WasLastRefreshIndexBuildOnWorker)
        .def_property_readonly("last_refresh_query_build_ms", &AssetDatabase::GetLastRefreshQueryBuildMilliseconds)
        .def_property_readonly("last_refresh_query_build_on_worker", &AssetDatabase::WasLastRefreshQueryBuildOnWorker)
        .def_property_readonly("last_refresh_dependency_build_ms",
                               &AssetDatabase::GetLastRefreshDependencyBuildMilliseconds)
        .def_property_readonly("last_refresh_dependency_build_on_worker",
                               &AssetDatabase::WasLastRefreshDependencyBuildOnWorker)
        .def_property_readonly("last_refresh_publish_ms", &AssetDatabase::GetLastRefreshPublishMilliseconds)
        .def_property_readonly("last_refresh_scan_on_worker", &AssetDatabase::WasLastRefreshScanOnWorker)
        .def_property_readonly("asset_index_path", &AssetDatabase::GetAssetIndexPath)
        .def("is_asset_path", &AssetDatabase::IsAssetPath, py::arg("path"), "Check if path is in Assets folder")
        .def_property_readonly("project_root", &AssetDatabase::GetProjectRoot, "Project root path")
        .def_property_readonly("assets_root", &AssetDatabase::GetAssetsRoot, "Assets root path")
        .def("get_runtime_artifact_path", &AssetDatabase::GetRuntimeArtifactPath, py::arg("guid"), py::arg("type"),
             "Resolve the project-local runtime CPU artifact path")
        .def("get_resource_type", &AssetDatabase::GetResourceTypeForPath, py::arg("file_path"),
             "Get the ResourceType for a file based on its path");
}

} // namespace infernux
