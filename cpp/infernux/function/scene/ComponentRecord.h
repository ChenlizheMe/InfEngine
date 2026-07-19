#pragma once

#include <cstdint>
#include <nlohmann/json.hpp>
#include <string>

namespace infernux
{

class Component;

enum class ComponentRecordKind
{
    Native,
    Python,
};

struct DecodedComponentRecord
{
    ComponentRecordKind kind = ComponentRecordKind::Native;
    uint64_t componentId = 0;
    std::string typeId;
    bool enabled = true;
    int executionOrder = 0;
    nlohmann::json data = nlohmann::json::object();

    std::string nativeTypeName;
    std::string pythonTypeName;
    std::string scriptGuid;
    std::string typeGuid;
    std::string moduleName;
    std::string qualifiedName;
};

[[nodiscard]] nlohmann::json SerializeComponentRecord(const Component &component);
[[nodiscard]] DecodedComponentRecord DecodeComponentRecord(const nlohmann::json &document);
[[nodiscard]] nlohmann::json BuildNativeComponentDocument(const DecodedComponentRecord &record);
[[nodiscard]] nlohmann::json BuildPythonFieldsDocument(const DecodedComponentRecord &record);

} // namespace infernux
