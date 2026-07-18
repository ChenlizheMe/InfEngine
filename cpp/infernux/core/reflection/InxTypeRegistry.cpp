#include "InxTypeRegistry.h"

#include <core/log/InxLog.h>
#include <core/types/InxFwdType.h>

#include <any>
#include <functional>
#include <string>
#include <typeindex>

namespace infernux
{
InxTypeRegistry &InxTypeRegistry::GetInstance()
{
    static InxTypeRegistry instance;
    return instance;
}

std::any InxTypeRegistry::Create(const std::string &typeName) const
{
    auto it = m_typeConstructors.find(typeName);
    if (it != m_typeConstructors.end()) {
        return it->second();
    }
    INXLOG_ERROR("Type not registered: ", typeName);
    return std::any{};
}

std::string InxTypeRegistry::GetTypeName(std::type_index typeIndex) const
{
    auto it = m_typeNames.find(typeIndex);
    if (it != m_typeNames.end()) {
        return it->second;
    }
    INXLOG_ERROR("Type not registered: ", typeIndex.name());
    return {};
}

std::string InxTypeRegistry::ToString(const std::string &typeName, const std::any &value) const
{
    auto it = m_toStringFuncs.find(typeName);
    if (it != m_toStringFuncs.end()) {
        return it->second(value);
    }
    INXLOG_ERROR("ToString function not registered for type: ", typeName);
    return {};
}

std::any InxTypeRegistry::FromString(const std::string &typeName, const std::string &str) const
{
    auto it = m_fromStringFuncs.find(typeName);
    if (it != m_fromStringFuncs.end()) {
        return it->second(str);
    }
    INXLOG_ERROR("FromString function not registered for type: ", typeName);
    return {};
}

InxTypeRegistry::InxTypeRegistry()
{
    Build();
}

void InxTypeRegistry::Build()
{
    // Register built-in types and their conversion functions here
    RegisterType<int>(
        "int", [](const std::any &v) { return std::to_string(std::any_cast<int>(v)); },
        [](const std::string &s) { return std::any{std::stoi(s)}; });

    RegisterType<float>(
        "float", [](const std::any &v) { return std::to_string(std::any_cast<float>(v)); },
        [](const std::string &s) { return std::any{std::stof(s)}; });

    RegisterType<double>(
        "double", [](const std::any &v) { return std::to_string(std::any_cast<double>(v)); },
        [](const std::string &s) { return std::any{std::stod(s)}; });

    RegisterType<std::string>(
        "string", [](const std::any &v) { return std::any_cast<std::string>(v); },
        [](const std::string &s) { return std::any{s}; });

    RegisterType<bool>(
        "bool", [](const std::any &v) { return std::any_cast<bool>(v) ? "true" : "false"; },
        [](const std::string &s) { return std::any{s == "true"}; });

    RegisterType<size_t>(
        "size_t", [](const std::any &v) { return std::to_string(std::any_cast<size_t>(v)); },
        [](const std::string &s) { return std::any{static_cast<size_t>(std::stoull(s))}; });

    // Register ResourceType enum with proper type name
    RegisterType<ResourceType>(
        "enum infernux::ResourceType",
        [](const std::any &v) {
            ResourceType rt = std::any_cast<ResourceType>(v);
            switch (rt) {
            case ResourceType::Meta:
                return std::string("Meta");
            case ResourceType::Shader:
                return std::string("Shader");
            case ResourceType::Texture:
                return std::string("Texture");
            case ResourceType::Mesh:
                return std::string("Mesh");
            case ResourceType::Script:
                return std::string("Script");
            case ResourceType::Audio:
                return std::string("Audio");
            case ResourceType::DefaultText:
                return std::string("DefaultText");
            case ResourceType::DefaultBinary:
                return std::string("DefaultBinary");
            case ResourceType::Material:
                return std::string("Material");
            case ResourceType::PhysicMaterial:
                return std::string("PhysicMaterial");
            case ResourceType::RenderEffect:
                return std::string("RenderEffect");
            default:
                return std::string("Unknown");
            }
        },
        [](const std::string &s) -> std::any {
            if (s == "Meta")
                return std::any{ResourceType::Meta};
            if (s == "Shader")
                return std::any{ResourceType::Shader};
            if (s == "Texture")
                return std::any{ResourceType::Texture};
            if (s == "Mesh")
                return std::any{ResourceType::Mesh};
            if (s == "Script")
                return std::any{ResourceType::Script};
            if (s == "Audio")
                return std::any{ResourceType::Audio};
            if (s == "DefaultText")
                return std::any{ResourceType::DefaultText};
            if (s == "DefaultBinary")
                return std::any{ResourceType::DefaultBinary};
            if (s == "Material")
                return std::any{ResourceType::Material};
            if (s == "PhysicMaterial")
                return std::any{ResourceType::PhysicMaterial};
            if (s == "RenderEffect")
                return std::any{ResourceType::RenderEffect};
            if (s == "Unknown")
                return std::any{ResourceType::DefaultText};
            return std::any{ResourceType::DefaultText};
        });
}
} // namespace infernux
