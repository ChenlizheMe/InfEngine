#include "MaterialLoader.h"

#include <core/log/InxLog.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <platform/filesystem/InxPath.h>

#include <filesystem>
#include <fstream>
#include <unordered_set>

namespace infernux
{

namespace
{

ShaderAssetReference EnrichShaderReference(ShaderAssetReference reference, const char *stage, AssetDatabase *database)
{
    if (!database)
        return reference;

    std::string resolvedPath;
    if (!reference.guid.empty())
        resolvedPath = database->GetPathFromGuid(reference.guid);
    if (resolvedPath.empty() && !reference.pathHint.empty()) {
        const std::string hintGuid = database->GetGuidFromPath(reference.pathHint);
        std::error_code error;
        const bool hintExists = !hintGuid.empty() || std::filesystem::exists(ToFsPath(reference.pathHint), error);
        if (hintExists && (reference.guid.empty() || hintGuid.empty() || hintGuid == reference.guid))
            resolvedPath = reference.pathHint;
    }
    if (resolvedPath.empty() && !reference.shaderId.empty())
        resolvedPath = database->FindShaderPathById(reference.shaderId, stage);

    if (!resolvedPath.empty()) {
        if (reference.guid.empty())
            reference.guid = database->GetGuidFromPath(resolvedPath);
        reference.pathHint = resolvedPath;
    }
    return reference;
}

void EnrichShaderReferences(InxMaterial &material, AssetDatabase *database)
{
    material.SetVertShaderReference(EnrichShaderReference(material.GetVertShaderReference(), "vertex", database));
    material.SetFragShaderReference(EnrichShaderReference(material.GetFragShaderReference(), "fragment", database));
}

} // namespace

// =============================================================================
// Load — create a brand-new InxMaterial from a .mat file
// =============================================================================

RuntimeAssetPayload MaterialLoader::Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb)
{
    if (filePath.empty() || guid.empty()) {
        INXLOG_WARN("MaterialLoader::Load: empty filePath or guid");
        return nullptr;
    }

    // Read file
    std::ifstream file(ToFsPath(filePath));
    if (!file.is_open()) {
        INXLOG_WARN("MaterialLoader::Load: cannot open '", filePath, "'");
        return nullptr;
    }
    std::string jsonStr((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();

    // Deserialize
    auto material = std::make_shared<InxMaterial>();
    if (!material->Deserialize(jsonStr)) {
        INXLOG_ERROR("MaterialLoader::Load: deserialization failed for '", filePath, "'");
        return nullptr;
    }

    // Identity — authoritative source is .meta / AssetDatabase, NOT JSON
    material->SetFilePath(filePath);
    material->SetName(FromFsPath(ToFsPath(filePath).stem()));
    material->SetGuid(guid);
    EnrichShaderReferences(*material, adb);

    // Dependency graph edges (textures, shaders)
    RegisterDependencies(guid, *material, adb);

    // INXLOG_INFO("MaterialLoader: loaded '", material->GetName(), "' (GUID: ", guid, ")");
    return material;
}

// =============================================================================
// Reload — hot-refresh into an existing instance (pointer identity preserved)
// =============================================================================

bool MaterialLoader::Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                            AssetDatabase *adb)
{
    auto mat = existing.Get<InxMaterial>();
    if (!mat) {
        INXLOG_WARN("MaterialLoader::Reload: null existing instance");
        return false;
    }

    // Read file
    std::ifstream file(ToFsPath(filePath));
    if (!file.is_open()) {
        INXLOG_WARN("MaterialLoader::Reload: cannot open '", filePath, "'");
        return false;
    }
    std::string jsonStr((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();

    // Save authoritative name and GUID (Deserialize may clobber m_name)
    const std::string savedName = mat->GetName();
    const std::string savedGuid = mat->GetGuid();

    // Deserialize *into the same instance* — shared_ptr identity preserved
    if (!mat->Deserialize(jsonStr)) {
        INXLOG_ERROR("MaterialLoader::Reload: deserialization failed for '", filePath, "'");
        return false;
    }

    // Restore authoritative identity
    mat->SetName(savedName);
    mat->SetGuid(savedGuid);
    EnrichShaderReferences(*mat, adb);

    // Re-wire dependency graph (texture/shader deps may have changed)
    RegisterDependencies(savedGuid, *mat, adb);

    // INXLOG_INFO("MaterialLoader: reloaded '", savedName, "' in-place");
    return true;
}

// =============================================================================
// ScanDependencies — enumerate outgoing GUIDs for the dependency graph
// =============================================================================

size_t MaterialLoader::EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const
{
    const auto material = payload.Get<InxMaterial>();
    if (!material)
        throw std::invalid_argument("MaterialLoader cannot estimate an empty runtime payload");
    return material->GetRuntimeMemoryBytes();
}

std::set<std::string> MaterialLoader::ScanDependencies(const std::string &filePath, AssetDatabase *adb)
{
    std::set<std::string> deps;

    std::ifstream file = OpenInputFile(filePath);
    if (!file.is_open())
        return deps;

    std::string jsonStr((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();

    // Temporary material just for parsing — lightweight, no GPU resources
    InxMaterial tmp;
    if (!tmp.Deserialize(jsonStr))
        return deps;

    // Texture GUIDs
    for (const auto &[propName, prop] : tmp.GetAllProperties()) {
        if (prop.type != MaterialPropertyType::Texture2D)
            continue;
        const auto *val = std::get_if<std::string>(&prop.value);
        if (val && !val->empty())
            deps.insert(*val);
    }

    // Shader GUIDs. GUID is authoritative; path hint and shader ID are recovery
    // paths for migrated v3 assets and a freshly rebuilt database.
    if (adb) {
        auto addShaderDep = [&](const ShaderAssetReference &reference, const char *stage) {
            std::string depGuid = reference.guid;
            if (depGuid.empty() && !reference.pathHint.empty())
                depGuid = adb->GetGuidFromPath(reference.pathHint);
            if (depGuid.empty() && !reference.shaderId.empty()) {
                const std::string path = adb->FindShaderPathById(reference.shaderId, stage);
                if (!path.empty())
                    depGuid = adb->GetGuidFromPath(path);
            }
            if (!depGuid.empty())
                deps.insert(depGuid);
        };
        addShaderDep(tmp.GetVertShaderReference(), "vertex");
        addShaderDep(tmp.GetFragShaderReference(), "fragment");
    }

    return deps;
}

// =============================================================================
// RegisterDependencies — wire up AssetDependencyGraph edges
// =============================================================================
// CreateMeta — material-specific .meta creation
// =============================================================================

void MaterialLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                                InxResourceMeta &metaData) const
{
    if (!content) {
        INXLOG_ERROR("Invalid material content for metadata creation");
        return;
    }

    metaData.Init(content, contentSize, filePath, ResourceType::Material);

    // Use filename stem as material name — authoritative name is set by
    // MaterialLoader::Load() at runtime, so parsing JSON here is unnecessary.
    std::filesystem::path path = ToFsPath(filePath);
    metaData.AddMetadata("material_name", FromFsPath(path.stem()));
}

// =============================================================================

void MaterialLoader::RegisterDependencies(const std::string &materialGuid, const InxMaterial &mat, AssetDatabase *adb)
{
    if (materialGuid.empty())
        return;

    std::unordered_set<std::string> dependencies;

    // Texture property GUIDs
    for (const auto &[propName, prop] : mat.GetAllProperties()) {
        if (prop.type != MaterialPropertyType::Texture2D)
            continue;
        const auto *val = std::get_if<std::string>(&prop.value);
        if (val && !val->empty())
            dependencies.insert(*val);
    }

    // Shader GUIDs (shader files have .meta with GUID)
    if (adb) {
        auto addShaderDep = [&](const ShaderAssetReference &reference, const char *stage) {
            std::string depGuid = reference.guid;
            if (depGuid.empty() && !reference.pathHint.empty())
                depGuid = adb->GetGuidFromPath(reference.pathHint);
            if (depGuid.empty() && !reference.shaderId.empty()) {
                const std::string path = adb->FindShaderPathById(reference.shaderId, stage);
                if (!path.empty())
                    depGuid = adb->GetGuidFromPath(path);
            }
            if (!depGuid.empty())
                dependencies.insert(std::move(depGuid));
        };
        addShaderDep(mat.GetVertShaderReference(), "vertex");
        addShaderDep(mat.GetFragShaderReference(), "fragment");
    }
    AssetDependencyGraph::Instance().SetAssetDependencies(materialGuid, dependencies);
}

} // namespace infernux
