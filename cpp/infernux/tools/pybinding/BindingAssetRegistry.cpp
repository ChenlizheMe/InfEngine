#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxMesh/InxMesh.h>
#include <function/resources/InxPointCache/InxPointCache.h>
#include <function/resources/InxSkinnedMesh/InxSkinnedMesh.h>
#include <function/resources/InxTexture/InxTexture.h>
#include <function/resources/PhysicMaterial/PhysicMaterial.h>

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

namespace infernux
{
namespace
{
uint32_t PointCacheChannelComponentCount(PointCacheChannelType type)
{
    switch (type) {
    case PointCacheChannelType::Float:
    case PointCacheChannelType::UInt:
        return 1;
    case PointCacheChannelType::Float2:
        return 2;
    case PointCacheChannelType::Float3:
        return 3;
    case PointCacheChannelType::Float4:
        return 4;
    case PointCacheChannelType::Count:
        break;
    }
    throw std::invalid_argument("point cache channel has an invalid type");
}

py::array PointCacheChannelArray(const std::shared_ptr<InxPointCache> &pointCache, const std::string &channelName)
{
    if (!pointCache)
        throw std::invalid_argument("point cache is null");
    const auto cpuData = pointCache->GetCpuData();
    if (!cpuData || !cpuData->IsValid())
        throw std::runtime_error("point cache has no valid CPU data");
    const auto *channel = cpuData->FindChannel(channelName);
    if (!channel)
        throw py::key_error("point cache channel does not exist: " + channelName);

    const uint32_t componentCount = PointCacheChannelComponentCount(channel->type);
    const uint64_t requiredBytes = static_cast<uint64_t>(channel->elementStride) * cpuData->pointCount;
    if (channel->byteOffset > cpuData->bytes.size() || requiredBytes > cpuData->bytes.size() - channel->byteOffset)
        throw std::runtime_error("point cache channel range is outside the immutable CPU payload");

    std::vector<py::ssize_t> shape{static_cast<py::ssize_t>(cpuData->pointCount)};
    std::vector<py::ssize_t> strides{static_cast<py::ssize_t>(channel->elementStride)};
    if (componentCount > 1) {
        shape.push_back(static_cast<py::ssize_t>(componentCount));
        strides.push_back(static_cast<py::ssize_t>(sizeof(float)));
    }

    auto *generation = new std::shared_ptr<const PointCacheCpuData>(cpuData);
    py::capsule owner(generation,
                      [](void *value) { delete static_cast<std::shared_ptr<const PointCacheCpuData> *>(value); });
    const auto dtype =
        channel->type == PointCacheChannelType::UInt ? py::dtype::of<uint32_t>() : py::dtype::of<float>();
    py::array result(dtype, shape, strides, cpuData->bytes.data() + channel->byteOffset, owner);
    result.attr("setflags")(false);
    return result;
}

void PointCacheLookupIndices(const std::shared_ptr<InxPointCache> &pointCache,
                             const py::array_t<uint32_t, py::array::c_style> &stableIds,
                             py::array_t<uint32_t, py::array::c_style> pointIndices)
{
    if (!pointCache)
        throw std::invalid_argument("point cache is null");
    const auto cpuData = pointCache->GetCpuData();
    if (!cpuData || !cpuData->IsValid())
        throw std::runtime_error("point cache has no valid CPU data");
    const auto input = stableIds.request();
    auto output = pointIndices.request();
    if (input.ndim != 1 || output.ndim != 1 || input.shape[0] != output.shape[0])
        throw std::invalid_argument("point cache ID lookup requires matching one-dimensional arrays");
    if (!pointIndices.writeable())
        throw std::invalid_argument("point cache ID lookup output must be writable");
    const auto *source = static_cast<const uint32_t *>(input.ptr);
    auto *destination = static_cast<uint32_t *>(output.ptr);
    const size_t count = static_cast<size_t>(input.shape[0]);
    py::gil_scoped_release release;
    for (size_t index = 0; index < count; ++index)
        destination[index] = cpuData->FindPointIndex(source[index]);
}
} // namespace

void RegisterAssetRegistryBindings(py::module_ &m)
{
    py::class_<AssetLoadTicket, std::shared_ptr<AssetLoadTicket>>(m, "AssetLoadTicket")
        .def_property_readonly("guid", &AssetLoadTicket::GetGuid)
        .def_property_readonly("resource_type", &AssetLoadTicket::GetResourceType)
        .def_property_readonly("complete", &AssetLoadTicket::IsComplete)
        .def_property_readonly("committed", &AssetLoadTicket::IsCommitted)
        .def_property_readonly("produced_on_worker", &AssetLoadTicket::WasProducedOnWorker);

    py::class_<AssetResidencyRecord>(m, "AssetResidencyRecord")
        .def_readonly("guid", &AssetResidencyRecord::guid)
        .def_readonly("resource_type", &AssetResidencyRecord::type)
        .def_readonly("runtime_type_name", &AssetResidencyRecord::runtimeTypeName)
        .def_readonly("runtime_version", &AssetResidencyRecord::runtimeVersion)
        .def_readonly("cpu_bytes", &AssetResidencyRecord::cpuBytes)
        .def_readonly("last_access_serial", &AssetResidencyRecord::lastAccessSerial)
        .def_readonly("explicit_pin_count", &AssetResidencyRecord::explicitPinCount)
        .def_readonly("external_reference_count", &AssetResidencyRecord::externalReferenceCount)
        .def_readonly("evictable", &AssetResidencyRecord::evictable);

    // ── InxMesh — read-only runtime mesh asset ───────────────────────────
    py::class_<InxMesh, std::shared_ptr<InxMesh>>(m, "InxMesh")
        .def_property_readonly("name", &InxMesh::GetName, "Mesh asset name")
        .def_property_readonly("guid", &InxMesh::GetGuid, "Mesh asset GUID")
        .def_property_readonly("file_path", &InxMesh::GetFilePath, "Source file path")
        .def_property_readonly("vertex_count", &InxMesh::GetVertexCount, "Total vertex count")
        .def_property_readonly("index_count", &InxMesh::GetIndexCount, "Total index count")
        .def_property_readonly("submesh_count", &InxMesh::GetSubMeshCount, "Number of submeshes")
        .def_property_readonly("material_slot_count", &InxMesh::GetMaterialSlotCount, "Number of material slots")
        .def_property_readonly("material_slot_names", &InxMesh::GetMaterialSlotNames,
                               "Material slot names from model file")
        .def_property_readonly("has_skinned_data", &InxMesh::HasSkinnedData,
                               "Whether this Mesh carries immutable skin/animation data")
        .def_property_readonly("skinned_bone_count",
                               [](const InxMesh &mesh) {
                                   const auto &skinned = mesh.GetSkinnedData();
                                   return skinned ? skinned->bones.size() : size_t{0};
                               })
        .def_property_readonly("skinned_animation_count",
                               [](const InxMesh &mesh) {
                                   const auto &skinned = mesh.GetSkinnedData();
                                   return skinned ? skinned->animations.size() : size_t{0};
                               })
        .def_property_readonly("skinned_animation_names",
                               [](const InxMesh &mesh) {
                                   std::vector<std::string> names;
                                   const auto &skinned = mesh.GetSkinnedData();
                                   if (!skinned)
                                       return names;
                                   names.reserve(skinned->animations.size());
                                   for (const auto &animation : skinned->animations)
                                       names.push_back(animation.name);
                                   return names;
                               })
        .def(
            "get_material_slot_data",
            [](const InxMesh &self) -> py::list {
                py::list result;
                for (const auto &sd : self.GetMaterialSlotData()) {
                    py::dict d;
                    d["base_color"] = py::make_tuple(sd.baseColor.r, sd.baseColor.g, sd.baseColor.b, sd.baseColor.a);
                    d["emission_color"] =
                        py::make_tuple(sd.emissionColor.r, sd.emissionColor.g, sd.emissionColor.b, sd.emissionColor.a);
                    d["metallic"] = sd.metallic;
                    d["smoothness"] = sd.smoothness;
                    d["opacity"] = sd.opacity;
                    result.append(d);
                }
                return result;
            },
            "Get per-slot material data extracted from model file")
        .def(
            "get_bounds",
            [](const InxMesh &self) -> py::tuple {
                const auto &bmin = self.GetBoundsMin();
                const auto &bmax = self.GetBoundsMax();
                return py::make_tuple(bmin.x, bmin.y, bmin.z, bmax.x, bmax.y, bmax.z);
            },
            "Get AABB as (minX, minY, minZ, maxX, maxY, maxZ)")
        .def(
            "get_submesh_info",
            [](const InxMesh &self, uint32_t index) -> py::dict {
                const auto &sub = self.GetSubMesh(index);
                py::dict d;
                d["name"] = sub.name;
                d["index_start"] = sub.indexStart;
                d["index_count"] = sub.indexCount;
                d["vertex_start"] = sub.vertexStart;
                d["vertex_count"] = sub.vertexCount;
                d["material_slot"] = sub.materialSlot;
                d["bounds_min"] = py::make_tuple(sub.boundsMin.x, sub.boundsMin.y, sub.boundsMin.z);
                d["bounds_max"] = py::make_tuple(sub.boundsMax.x, sub.boundsMax.y, sub.boundsMax.z);
                return d;
            },
            py::arg("index"), "Get submesh info as dict (name, index_start, index_count, ...)")
        .def("__repr__", [](const InxMesh &self) {
            return "<InxMesh '" + self.GetName() + "' " + std::to_string(self.GetVertexCount()) + " verts, " +
                   std::to_string(self.GetSubMeshCount()) + " submesh(es)>";
        });

    py::class_<InxTexture, std::shared_ptr<InxTexture>>(m, "InxTexture")
        .def_property_readonly("name", &InxTexture::GetName)
        .def_property_readonly("guid", &InxTexture::GetGuid)
        .def_property_readonly("file_path", &InxTexture::GetFilePath)
        .def_property_readonly("mip_count",
                               [](const InxTexture &self) {
                                   const auto &cpu = self.GetCpuData();
                                   return cpu ? cpu->mipLevels.size() : size_t{0};
                               })
        .def_property_readonly("pixel_width",
                               [](const InxTexture &self) {
                                   const auto &cpu = self.GetCpuData();
                                   return cpu && !cpu->mipLevels.empty() ? cpu->mipLevels.front().width : 0U;
                               })
        .def_property_readonly("pixel_height",
                               [](const InxTexture &self) {
                                   const auto &cpu = self.GetCpuData();
                                   return cpu && !cpu->mipLevels.empty() ? cpu->mipLevels.front().height : 0U;
                               })
        .def_property_readonly("cpu_byte_size",
                               [](const InxTexture &self) {
                                   const auto &cpu = self.GetCpuData();
                                   return cpu ? cpu->bytes.size() : size_t{0};
                               })
        .def_property_readonly("pixel_storage", [](const InxTexture &self) {
            const auto &cpu = self.GetCpuData();
            if (!cpu)
                return std::string{};
            return std::string(cpu->storage == TexturePixelStorage::Rgba8 ? "rgba8" : "rgba32_float");
        });

    py::class_<InxPointCache, std::shared_ptr<InxPointCache>>(m, "InxPointCache")
        .def_property_readonly("name", &InxPointCache::GetName)
        .def_property_readonly("guid", &InxPointCache::GetGuid)
        .def_property_readonly("file_path", &InxPointCache::GetFilePath)
        .def_property_readonly("generation", &InxPointCache::GetGeneration)
        .def_property_readonly("stable_id",
                               [](const InxPointCache &self) {
                                   const auto &cpu = self.GetCpuData();
                                   return cpu ? cpu->stableId : std::string{};
                               })
        .def_property_readonly("bake_basis",
                               [](const InxPointCache &self) {
                                   const auto &cpu = self.GetCpuData();
                                   return cpu ? cpu->bakeBasis : std::string{};
                               })
        .def_property_readonly("point_count",
                               [](const InxPointCache &self) {
                                   const auto &cpu = self.GetCpuData();
                                   return cpu ? cpu->pointCount : 0U;
                               })
        .def_property_readonly("channel_names",
                               [](const InxPointCache &self) {
                                   std::vector<std::string> names;
                                   const auto &cpu = self.GetCpuData();
                                   if (!cpu)
                                       return names;
                                   names.reserve(cpu->channels.size());
                                   for (const auto &channel : cpu->channels)
                                       names.push_back(channel.name);
                                   return names;
                               })
        .def_property_readonly("cpu_byte_size",
                               [](const InxPointCache &self) {
                                   const auto &cpu = self.GetCpuData();
                                   return cpu ? cpu->bytes.size() : size_t{0};
                               })
        .def(
            "has_channel",
            [](const InxPointCache &self, const std::string &name) {
                const auto &cpu = self.GetCpuData();
                return cpu && cpu->FindChannel(name) != nullptr;
            },
            py::arg("name"))
        .def("channel_array", &PointCacheChannelArray, py::arg("name"),
             "Return a zero-copy, read-only NumPy view of one channel generation")
        .def("lookup_index",
             [](const InxPointCache &self, uint32_t stableId) {
                 const auto &cpu = self.GetCpuData();
                 return cpu ? cpu->FindPointIndex(stableId) : UINT32_MAX;
             },
             py::arg("stable_id"))
        .def("lookup_indices", &PointCacheLookupIndices, py::arg("stable_ids"), py::arg("point_indices"),
             "Map a contiguous uint32 stable-ID array to point indices in one native batch");

    // ── AssetRegistry — unified asset cache (singleton) ─────────────────
    py::class_<AssetRegistry, std::unique_ptr<AssetRegistry, py::nodelete>>(m, "AssetRegistry")
        .def_static("instance", &AssetRegistry::Instance, py::return_value_policy::reference,
                    "Get the AssetRegistry singleton")
        .def("is_initialized", &AssetRegistry::IsInitialized, "Check if the registry is initialized")
        .def("get_asset_database", &AssetRegistry::GetAssetDatabase, py::return_value_policy::reference,
             "Get the owned AssetDatabase (may be None before InitRenderer)")

        // Material convenience wrappers (type-safe, avoids exposing void* to Python)
        .def(
            "load_material",
            [](AssetRegistry &self, const std::string &path) {
                return self.LoadAssetByPath<InxMaterial>(path, ResourceType::Material);
            },
            py::arg("path"), "Load a material by file path (GUID resolved internally)")
        .def(
            "load_material_by_guid",
            [](AssetRegistry &self, const std::string &guid) {
                return self.LoadAsset<InxMaterial>(guid, ResourceType::Material);
            },
            py::arg("guid"), "Load a material by its GUID")
        .def(
            "get_material",
            [](AssetRegistry &self, const std::string &guid) { return self.GetAsset<InxMaterial>(guid); },
            py::arg("guid"), "Get a cached material by GUID (returns None if not loaded)")
        .def("get_builtin_material", &AssetRegistry::GetBuiltinMaterial, py::arg("key"),
             "Get a built-in material by key (e.g. 'DefaultLit', 'ErrorMaterial')")
        .def("load_builtin_material_from_file", &AssetRegistry::LoadBuiltinMaterialFromFile, py::arg("key"),
             py::arg("mat_file_path"), "Load/replace a builtin material from a .mat file (e.g. key='DefaultLit')")

        .def(
            "load_physic_material",
            [](AssetRegistry &self, const std::string &path) {
                return self.LoadAssetByPath<PhysicMaterial>(path, ResourceType::PhysicMaterial);
            },
            py::arg("path"), "Load a PhysicMaterial by file path")
        .def(
            "load_physic_material_by_guid",
            [](AssetRegistry &self, const std::string &guid) {
                return self.LoadAsset<PhysicMaterial>(guid, ResourceType::PhysicMaterial);
            },
            py::arg("guid"), "Load a PhysicMaterial by GUID")
        .def(
            "get_physic_material",
            [](AssetRegistry &self, const std::string &guid) { return self.GetAsset<PhysicMaterial>(guid); },
            py::arg("guid"), "Get a cached PhysicMaterial by GUID")

        // Mesh convenience wrappers
        .def(
            "load_mesh",
            [](AssetRegistry &self, const std::string &path) {
                return self.LoadAssetByPath<InxMesh>(path, ResourceType::Mesh);
            },
            py::arg("path"), "Load a mesh by file path (.fbx, .obj, .gltf, …)")
        .def(
            "load_mesh_by_guid",
            [](AssetRegistry &self, const std::string &guid) {
                return self.LoadAsset<InxMesh>(guid, ResourceType::Mesh);
            },
            py::arg("guid"), "Load a mesh by its GUID")
        .def(
            "get_mesh", [](AssetRegistry &self, const std::string &guid) { return self.GetAsset<InxMesh>(guid); },
            py::arg("guid"), "Get a cached mesh by GUID (returns None if not loaded)")
        .def(
            "begin_load_mesh_by_guid",
            [](AssetRegistry &self, const std::string &guid) { return self.BeginLoadAsset(guid, ResourceType::Mesh); },
            py::arg("guid"), "Schedule Assimp CPU mesh preparation on JobSystem")
        .def(
            "load_texture_by_guid",
            [](AssetRegistry &self, const std::string &guid) {
                return self.LoadAsset<InxTexture>(guid, ResourceType::Texture);
            },
            py::arg("guid"), "Load a decoded texture CPU artifact by GUID")
        .def(
            "get_texture_asset",
            [](AssetRegistry &self, const std::string &guid) { return self.GetAsset<InxTexture>(guid); },
            py::arg("guid"), "Get a cached decoded texture asset by GUID")
        .def(
            "begin_load_texture_by_guid",
            [](AssetRegistry &self, const std::string &guid) {
                return self.BeginLoadAsset(guid, ResourceType::Texture);
            },
            py::arg("guid"), "Schedule texture artifact load/decode on JobSystem")
        .def(
            "load_point_cache",
            [](AssetRegistry &self, const std::string &path) {
                return self.LoadAssetByPath<InxPointCache>(path, ResourceType::PointCache);
            },
            py::arg("path"), "Load an imported Point Cache CPU artifact by path")
        .def(
            "load_point_cache_by_guid",
            [](AssetRegistry &self, const std::string &guid) {
                return self.LoadAsset<InxPointCache>(guid, ResourceType::PointCache);
            },
            py::arg("guid"), "Load an imported Point Cache CPU artifact by GUID")
        .def(
            "get_point_cache",
            [](AssetRegistry &self, const std::string &guid) { return self.GetAsset<InxPointCache>(guid); },
            py::arg("guid"), "Get a cached Point Cache by GUID")
        .def(
            "begin_load_point_cache_by_guid",
            [](AssetRegistry &self, const std::string &guid) {
                return self.BeginLoadAsset(guid, ResourceType::PointCache);
            },
            py::arg("guid"), "Schedule Point Cache artifact loading on JobSystem")
        .def("try_commit_asset_load", &AssetRegistry::TryCommitAssetLoad, py::arg("ticket"),
             "Publish a completed typed CPU payload; false means still pending")

        // Hot-reload / invalidation
        .def("reload_asset", &AssetRegistry::ReloadAsset, py::arg("guid"),
             "Reload an asset in-place from disk (preserves shared_ptr identity)")
        .def("invalidate_asset", &AssetRegistry::InvalidateAsset, py::arg("guid"),
             "Evict an asset from cache so next load re-reads from disk")
        .def("remove_asset", &AssetRegistry::RemoveAsset, py::arg("guid"),
             "Fully remove an asset record (e.g. when file is deleted)")

        .def("update_loaded_asset_path", &AssetRegistry::UpdateLoadedAssetPath, py::arg("old_path"),
             py::arg("new_path"), "Patch path-bearing cached state after a GUID-stable move")

        // Queries
        .def("is_loaded", &AssetRegistry::IsLoaded, py::arg("guid"), "Check if an asset is currently cached")
        .def("get_asset_version", &AssetRegistry::GetAssetVersion, py::arg("guid"),
             "Get the last successfully published runtime generation, or zero before first publication")
        .def("get_asset_runtime_type_name", &AssetRegistry::GetAssetRuntimeTypeName, py::arg("guid"),
             "Get the validated native payload type name, or an empty string")
        .def("get_asset_residency", &AssetRegistry::GetAssetResidency, py::arg("guid"))
        .def("get_all_asset_residency", &AssetRegistry::GetAllAssetResidency)
        .def_property("cpu_budget_bytes", &AssetRegistry::GetCpuBudgetBytes, &AssetRegistry::SetCpuBudgetBytes)
        .def_property_readonly("total_cpu_bytes", &AssetRegistry::GetTotalCpuBytes)
        .def_property_readonly("cpu_eviction_count", &AssetRegistry::GetCpuEvictionCount)
        .def("trim_cpu_budget", &AssetRegistry::TrimCpuBudget)
        .def("pin_asset", &AssetRegistry::PinAsset, py::arg("guid"))
        .def("unpin_asset", &AssetRegistry::UnpinAsset, py::arg("guid"));
}

} // namespace infernux
