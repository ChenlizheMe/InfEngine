#include "SceneResourceDependencyPreflight.h"

#include <core/types/InxFwdType.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxMaterial/MaterialDocumentValidation.h>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/scene/ComponentRecord.h>
#include <stdexcept>
#include <string>
#include <unordered_map>

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
    case ResourceType::PointCache:
        return "PointCache";
    }
    return "Unknown";
}

bool IsBuiltinTexture(const std::string &guid)
{
    return guid == "white" || guid == "black" || guid == "normal";
}

class ResourcePreflight
{
  public:
    void Validate(const nlohmann::json &document)
    {
        const auto &objects = document.at("objects");
        for (size_t index = 0; index < objects.size(); ++index)
            ValidateObject(objects[index], "Scene.objects[" + std::to_string(index) + "]");
    }

  private:
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
        if (!metadata)
            throw std::invalid_argument(path + " references missing asset GUID '" + guid + "'");
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
        if (type == "BoxCollider" || type == "SphereCollider" || type == "CapsuleCollider" || type == "MeshCollider") {
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

        if (type != "MeshRenderer" && type != "SkinnedMeshRenderer" && type != "SpriteRenderer")
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

} // namespace infernux
