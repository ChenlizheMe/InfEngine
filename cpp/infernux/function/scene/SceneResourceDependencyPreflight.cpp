#include "SceneResourceDependencyPreflight.h"

#include <algorithm>
#include <core/types/InxFwdType.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxMaterial/MaterialDocumentValidation.h>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/scene/ComponentRecord.h>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{
namespace
{

const char *ResourceTypeName(ResourceType type)
{
    switch (type) {
    case ResourceType::Meta:
        return "Meta";
    case ResourceType::Shader:
        return "Shader";
    case ResourceType::Texture:
        return "Texture";
    case ResourceType::Mesh:
        return "Mesh";
    case ResourceType::Material:
        return "Material";
    case ResourceType::Script:
        return "Script";
    case ResourceType::Audio:
        return "Audio";
    case ResourceType::DefaultText:
        return "DefaultText";
    case ResourceType::DefaultBinary:
        return "DefaultBinary";
    case ResourceType::PhysicMaterial:
        return "PhysicMaterial";
    case ResourceType::RenderEffect:
        return "RenderEffect";
    case ResourceType::ParticleGraph:
        return "ParticleGraph";
    }
    return "Unknown";
}

bool IsBuiltinTexture(const std::string &guid)
{
    return guid == "white" || guid == "black" || guid == "normal";
}

std::optional<ResourceType> ResourceTypeFromName(const std::string &name)
{
    static const std::unordered_map<std::string, ResourceType> types = {
        {"Shader", ResourceType::Shader},
        {"Texture", ResourceType::Texture},
        {"Mesh", ResourceType::Mesh},
        {"Material", ResourceType::Material},
        {"Script", ResourceType::Script},
        {"Audio", ResourceType::Audio},
        {"PhysicMaterial", ResourceType::PhysicMaterial},
        {"RenderEffect", ResourceType::RenderEffect},
        {"ParticleGraph", ResourceType::ParticleGraph},
    };
    const auto found = types.find(name);
    return found == types.end() ? std::nullopt : std::optional<ResourceType>(found->second);
}

class ResourcePreflight
{
  public:
    void Validate(const nlohmann::json &document)
    {
        const auto &objects = document.at("objects");
        for (size_t index = 0; index < objects.size(); ++index)
            ValidateObject(objects[index], "Scene.objects[" + std::to_string(index) + "]");

        CollectSerializedAssetRefs(document, "Scene");
        if (const auto environment = document.find("environment");
            environment != document.end() && environment->is_object()) {
            const auto skybox = environment->find("skybox_material_guid");
            if (skybox != environment->end() && skybox->is_string() && !skybox->get_ref<const std::string &>().empty())
                RequireAsset(skybox->get<std::string>(), ResourceType::Material,
                             "Scene.environment.skybox_material_guid");
        }
        ExpandAssetDependencies();
    }

    [[nodiscard]] std::vector<std::pair<std::string, std::string>> Dependencies() const
    {
        std::vector<std::pair<std::string, std::string>> result;
        result.reserve(m_assetTypes.size());
        for (const auto &[guid, type] : m_assetTypes)
            result.emplace_back(guid, ResourceTypeName(type));
        std::sort(result.begin(), result.end());
        return result;
    }

  private:
    void CollectSerializedAssetRefs(const nlohmann::json &value, const std::string &path)
    {
        if (value.is_array()) {
            for (size_t index = 0; index < value.size(); ++index)
                CollectSerializedAssetRefs(value[index], path + "[" + std::to_string(index) + "]");
            return;
        }
        if (!value.is_object())
            return;

        const auto marker = value.find("$type");
        if (marker != value.end() && marker->is_string() && marker->get_ref<const std::string &>() == "asset_ref") {
            const auto typeName = value.find("asset_type");
            const auto guid = value.find("guid");
            if (typeName != value.end() && typeName->is_string() && guid != value.end() && guid->is_string() &&
                !guid->get_ref<const std::string &>().empty()) {
                if (const auto type = ResourceTypeFromName(typeName->get<std::string>()); type.has_value())
                    RequireAsset(guid->get<std::string>(), *type, path + ".guid");
            }
        }
        for (const auto &[name, child] : value.items())
            CollectSerializedAssetRefs(child, path + "." + name);
    }

    void ExpandAssetDependencies()
    {
        AssetDatabase &database = GetDatabase("Scene dependencies");
        std::vector<std::string> pending;
        pending.reserve(m_assetTypes.size());
        for (const auto &[guid, _] : m_assetTypes)
            pending.push_back(guid);

        for (size_t index = 0; index < pending.size(); ++index) {
            for (const auto &dependencyGuid : AssetDependencyGraph::Instance().GetDependencies(pending[index])) {
                if (dependencyGuid.empty() || m_assetTypes.find(dependencyGuid) != m_assetTypes.end())
                    continue;
                const auto metadata = database.GetMetaByGuid(dependencyGuid);
                if (!metadata)
                    continue;
                m_assetTypes.emplace(dependencyGuid, metadata->GetResourceType());
                pending.push_back(dependencyGuid);
            }
        }
    }

    void RequireAsset(const std::string &guid, ResourceType expectedType, const std::string &path)
    {
        if (guid.empty())
            throw std::invalid_argument(path + " must not be empty");
        if (const auto cached = m_assetTypes.find(guid); cached != m_assetTypes.end()) {
            if (cached->second != expectedType)
                throw std::invalid_argument(path + " expects " + ResourceTypeName(expectedType) + " but GUID '" + guid +
                                            "' was already validated as " + ResourceTypeName(cached->second));
            return;
        }

        AssetDatabase &database = GetDatabase(path);

        const auto metadata = database.GetMetaByGuid(guid);
        if (!metadata) {
            // Missing references are valid authoring state.  The field itself
            // provides the expected type and the stable GUID allows Undo or a
            // later import to reconnect without rewriting the scene.
            m_assetTypes.emplace(guid, expectedType);
            return;
        }
        const ResourceType actualType = metadata->GetResourceType();
        if (actualType != expectedType) {
            throw std::invalid_argument(path + " expects " + ResourceTypeName(expectedType) + " but GUID '" + guid +
                                        "' is " + ResourceTypeName(actualType));
        }
        m_assetTypes.emplace(guid, actualType);
    }

    AssetDatabase &GetDatabase(const std::string &path)
    {
        if (m_database != nullptr)
            return *m_database;
        auto &registry = AssetRegistry::Instance();
        m_database = registry.GetAssetDatabase();
        if (!registry.IsInitialized() || m_database == nullptr)
            throw std::logic_error(path + " requires an initialized AssetDatabase");
        if (!m_database->IsOwnerThread())
            throw std::logic_error("scene resource dependency preflight must run on the AssetDatabase owner thread");
        return *m_database;
    }

    void ValidateEmbeddedMaterial(const nlohmann::json &document, const std::string &path)
    {
        material_document_validation::ValidateMaterialDocument(document, path);
        if (document.contains("properties") && document["properties"].is_object()) {
            for (const auto &[name, property] : document["properties"].items()) {
                if (!property.is_object() || !property.contains("type") || !property["type"].is_number_integer())
                    continue;
                if (property["type"].get<int>() != static_cast<int>(MaterialPropertyType::Texture2D))
                    continue;
                if (!property.contains("guid") || !property["guid"].is_string())
                    throw std::invalid_argument(path + ".properties." + name + ".guid must be a string");
                const std::string guid = property["guid"].get<std::string>();
                if (!guid.empty() && !IsBuiltinTexture(guid))
                    RequireAsset(guid, ResourceType::Texture, path + ".properties." + name + ".guid");
            }
        }
    }

    void ValidateComponent(const nlohmann::json &component, const std::string &path)
    {
        const DecodedComponentRecord record = DecodeComponentRecord(component);
        if (record.kind == ComponentRecordKind::Python)
            return;
        const std::string &type = record.nativeTypeName;
        const nlohmann::json data = BuildNativeComponentDocument(record);
        if (type == "BoxCollider" || type == "SphereCollider" || type == "CapsuleCollider" ||
            type == "CylinderCollider" || type == "MeshCollider") {
            const std::string guid = data.at("physic_material_guid").get<std::string>();
            if (!guid.empty())
                RequireAsset(guid, ResourceType::PhysicMaterial, path + ".physic_material_guid");
            return;
        }

        if (type == "AudioSource") {
            const auto &tracks = data.at("tracks");
            for (size_t index = 0; index < tracks.size(); ++index) {
                if (!tracks[index].contains("clip_guid"))
                    continue;
                RequireAsset(tracks[index]["clip_guid"].get<std::string>(), ResourceType::Audio,
                             path + ".tracks[" + std::to_string(index) + "].clip_guid");
            }
            return;
        }

        if (type != "MeshRenderer" && type != "SkinnedMeshRenderer" && type != "SpriteRenderer" &&
            type != "LineRenderer")
            return;

        if (data.contains("meshAssetGuid"))
            RequireAsset(data["meshAssetGuid"].get<std::string>(), ResourceType::Mesh, path + ".meshAssetGuid");
        const auto &materials = data.at("materials");
        for (size_t index = 0; index < materials.size(); ++index) {
            const auto &slot = materials[index];
            const std::string slotPath = path + ".materials[" + std::to_string(index) + "]";
            if (slot.is_string()) {
                RequireAsset(slot.get<std::string>(), ResourceType::Material, slotPath);
            } else if (slot.is_object()) {
                ValidateEmbeddedMaterial(slot.at("material"), slotPath + ".material");
            }
        }
        if (type == "SpriteRenderer" && data.contains("spriteGuid"))
            RequireAsset(data["spriteGuid"].get<std::string>(), ResourceType::Texture, path + ".spriteGuid");
    }

    void ValidateObject(const nlohmann::json &object, const std::string &path)
    {
        const auto &components = object.at("components");
        for (size_t index = 0; index < components.size(); ++index)
            ValidateComponent(components[index], path + ".components[" + std::to_string(index) + "]");
        const auto &children = object.at("children");
        for (size_t index = 0; index < children.size(); ++index)
            ValidateObject(children[index], path + ".children[" + std::to_string(index) + "]");
    }

    AssetDatabase *m_database = nullptr;
    std::unordered_map<std::string, ResourceType> m_assetTypes;
};

} // namespace

void PreflightSceneResourceDependencies(const nlohmann::json &document)
{
    ResourcePreflight{}.Validate(document);
}

std::vector<std::pair<std::string, std::string>> CollectSceneResourceDependencies(const nlohmann::json &document)
{
    ResourcePreflight preflight;
    preflight.Validate(document);
    return preflight.Dependencies();
}

} // namespace infernux
