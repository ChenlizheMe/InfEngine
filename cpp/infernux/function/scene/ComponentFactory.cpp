#include "ComponentFactory.h"
#include "Component.h"
#include <algorithm>
#include <stdexcept>
#include <unordered_map>

namespace infernux
{
namespace
{
struct ComponentRegistration
{
    ComponentFactory::Creator creator;
    ComponentFactory::DocumentValidator validator;
    ComponentTypeConstraints constraints;
};

std::unordered_map<std::string, ComponentRegistration> &GetRegistry()
{
    static std::unordered_map<std::string, ComponentRegistration> registry;
    return registry;
}
} // namespace

bool ComponentFactory::Register(const std::string &typeName, Creator creator, DocumentValidator validator,
                                ComponentTypeConstraints constraints)
{
    if (typeName.empty() || !creator || !validator)
        throw std::invalid_argument("component registration requires type name, creator, and document validator");
    auto &registry = GetRegistry();
    const bool inserted =
        registry
            .emplace(typeName, ComponentRegistration{std::move(creator), std::move(validator), std::move(constraints)})
            .second;
    if (!inserted)
        throw std::logic_error("duplicate component registration: " + typeName);
    return true;
}

std::unique_ptr<Component> ComponentFactory::Create(const std::string &typeName)
{
    auto &registry = GetRegistry();
    auto it = registry.find(typeName);
    if (it == registry.end())
        return nullptr;
    return it->second.creator();
}

void ComponentFactory::ValidateDocument(const std::string &typeName, const nlohmann::json &document)
{
    const auto &registry = GetRegistry();
    const auto iterator = registry.find(typeName);
    if (iterator == registry.end())
        throw std::invalid_argument("unregistered component type: " + typeName);
    iterator->second.validator(document);
}

const ComponentTypeConstraints &ComponentFactory::GetTypeConstraints(const std::string &typeName)
{
    const auto &registry = GetRegistry();
    const auto iterator = registry.find(typeName);
    if (iterator == registry.end())
        throw std::invalid_argument("unregistered component type: " + typeName);
    return iterator->second.constraints;
}

std::vector<std::string> ComponentFactory::GetAttachmentBlockers(const std::string &typeName,
                                                                 const std::vector<std::string> &attachedTypes)
{
    std::vector<std::string> blockers;
    if (typeName.empty()) {
        blockers.emplace_back("component type name is empty");
        return blockers;
    }
    if (typeName == "Transform") {
        blockers.emplace_back("Transform is intrinsic and cannot be added");
        return blockers;
    }
    if (!IsRegistered(typeName)) {
        blockers.emplace_back("component type is not registered");
        return blockers;
    }

    const auto &candidateConstraints = GetTypeConstraints(typeName);
    if (!candidateConstraints.allowMultiple &&
        std::find(attachedTypes.begin(), attachedTypes.end(), typeName) != attachedTypes.end()) {
        blockers.emplace_back("only one instance is allowed per GameObject");
    }

    const auto declaresIncompatible = [](const ComponentTypeConstraints &constraints, const std::string &otherType) {
        return std::find(constraints.incompatibleTypes.begin(), constraints.incompatibleTypes.end(), otherType) !=
               constraints.incompatibleTypes.end();
    };
    for (const std::string &existingType : attachedTypes) {
        if (!IsRegistered(existingType))
            continue;
        const auto &existingConstraints = GetTypeConstraints(existingType);
        if (declaresIncompatible(candidateConstraints, existingType) ||
            declaresIncompatible(existingConstraints, typeName)) {
            blockers.push_back("incompatible with existing component '" + existingType + "'");
        }
    }

    std::sort(blockers.begin(), blockers.end());
    blockers.erase(std::unique(blockers.begin(), blockers.end()), blockers.end());
    return blockers;
}

bool ComponentFactory::IsRegistered(const std::string &typeName)
{
    auto &registry = GetRegistry();
    return registry.find(typeName) != registry.end();
}

std::vector<std::string> ComponentFactory::GetRegisteredTypeNames()
{
    auto &registry = GetRegistry();
    std::vector<std::string> names;
    names.reserve(registry.size());
    for (const auto &pair : registry)
        names.push_back(pair.first);
    return names;
}

} // namespace infernux
