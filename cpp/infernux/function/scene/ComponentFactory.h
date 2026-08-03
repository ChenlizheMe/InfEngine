#pragma once

#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace infernux
{
class Component;
struct ComponentTypeConstraints;

class ComponentFactory
{
  public:
    using Creator = std::function<std::unique_ptr<Component>()>;
    using DocumentValidator = std::function<void(const nlohmann::json &)>;

    /// @brief Register a component creator by type name
    /// @return true if registered, false if already exists
    static bool Register(const std::string &typeName, Creator creator, DocumentValidator validator,
                         ComponentTypeConstraints constraints);

    /// @brief Create a component by type name
    /// @return unique_ptr to component or nullptr if not registered
    static std::unique_ptr<Component> Create(const std::string &typeName);

    /// @brief Check if a component type is registered
    static bool IsRegistered(const std::string &typeName);

    static void ValidateDocument(const std::string &typeName, const nlohmann::json &document);

    static const ComponentTypeConstraints &GetTypeConstraints(const std::string &typeName);

    static std::vector<std::string> GetAttachmentBlockers(const std::string &typeName,
                                                          const std::vector<std::string> &attachedTypes);

    /// @brief Get all registered component type names
    static std::vector<std::string> GetRegisteredTypeNames();
};

} // namespace infernux

#define INFERNUX_REGISTER_VALIDATED_COMPONENT(TYPE_STR, CLASS_TYPE)                                                    \
    namespace                                                                                                          \
    {                                                                                                                  \
    const bool s_infernux_component_registered_##CLASS_TYPE = infernux::ComponentFactory::Register(                    \
        TYPE_STR, []() { return std::make_unique<CLASS_TYPE>(); },                                                     \
        [](const nlohmann::json &document) { CLASS_TYPE::ValidateSerializedDocument(document); },                      \
        CLASS_TYPE::GetTypeConstraints());                                                                             \
    }
