#include "ComponentRecord.h"
#include "Component.h"
#include "PyComponentProxy.h"
#include <stdexcept>
#include <unordered_set>
#include <vector>

namespace infernux
{

namespace
{
using json = nlohmann::json;

constexpr const char *NATIVE_TYPE_PREFIX = "native:infernux.";
constexpr const char *PYTHON_TYPE_PREFIX = "python:";

const std::unordered_set<std::string> RECORD_FIELDS = {
    "component_id", "type_id", "enabled", "execution_order", "data",
};

const std::unordered_set<std::string> COMPONENT_BASE_FIELDS = {
    "type",
    "component_id",
    "enabled",
    "execution_order",
};

void RequireExactFields(const json &document, const std::unordered_set<std::string> &allowed, const char *label)
{
    if (!document.is_object())
        throw std::invalid_argument(std::string(label) + " must be an object");
    for (const auto &[key, value] : document.items()) {
        (void)value;
        if (allowed.find(key) == allowed.end())
            throw std::invalid_argument(std::string(label) + " contains unknown field: " + key);
    }
    for (const auto &field : allowed) {
        if (!document.contains(field))
            throw std::invalid_argument(std::string(label) + " is missing field: " + field);
    }
}

void RejectReservedDataFields(const json &data, const std::unordered_set<std::string> &reserved, const char *label)
{
    if (!data.is_object())
        throw std::invalid_argument(std::string(label) + ".data must be an object");
    for (const auto &field : reserved) {
        if (data.contains(field))
            throw std::invalid_argument(std::string(label) + ".data contains reserved field: " + field);
    }
}

std::vector<std::string> SplitPythonTypeId(const std::string &typeId)
{
    std::vector<std::string> parts;
    size_t begin = std::char_traits<char>::length(PYTHON_TYPE_PREFIX);
    while (begin <= typeId.size()) {
        const size_t separator = typeId.find(':', begin);
        parts.push_back(typeId.substr(begin, separator == std::string::npos ? std::string::npos : separator - begin));
        if (separator == std::string::npos)
            break;
        begin = separator + 1;
    }
    return parts;
}

std::string PythonTypeNameFromQualname(const std::string &qualifiedName)
{
    const size_t separator = qualifiedName.rfind('.');
    return separator == std::string::npos ? qualifiedName : qualifiedName.substr(separator + 1);
}

} // namespace

nlohmann::json SerializeComponentRecord(const Component &component)
{
    const auto *pythonProxy = dynamic_cast<const PyComponentProxy *>(&component);
    json record = {
        {"component_id", component.GetComponentID()},
        {"enabled", component.IsEnabled()},
        {"execution_order", component.GetExecutionOrder()},
    };

    if (pythonProxy) {
        if (pythonProxy->GetScriptGuid().empty() || pythonProxy->GetTypeGuid().empty() ||
            pythonProxy->GetModuleName().empty() || pythonProxy->GetQualifiedName().empty()) {
            throw std::logic_error("Python component '" + pythonProxy->GetPyTypeName() +
                                   "' has incomplete stable type identity");
        }

        const json proxyDocument = pythonProxy->SerializeDocument();
        record["enabled"] = proxyDocument.at("enabled");
        record["execution_order"] = proxyDocument.at("execution_order");
        json fields = proxyDocument.at("py_fields");
        if (!fields.is_object() || !fields.contains("__type_name__") || !fields["__type_name__"].is_string() ||
            fields["__type_name__"].get<std::string>() != pythonProxy->GetPyTypeName() ||
            !fields.contains("__component_id__") || !fields["__component_id__"].is_number_unsigned() ||
            fields["__component_id__"].get<uint64_t>() != component.GetComponentID()) {
            throw std::logic_error("Python component fields contain invalid identity metadata");
        }

        record["type_id"] = std::string(PYTHON_TYPE_PREFIX) + pythonProxy->GetScriptGuid() + ":" +
                            pythonProxy->GetTypeGuid() + ":" + pythonProxy->GetModuleName() + ":" +
                            pythonProxy->GetQualifiedName();
        fields.erase("__type_name__");
        fields.erase("__component_id__");
        record["data"] = std::move(fields);
        return record;
    }

    json componentDocument = component.SerializeDocument();
    if (!componentDocument.is_object() || !componentDocument.contains("type") ||
        !componentDocument["type"].is_string() ||
        componentDocument["type"].get<std::string>() != component.GetTypeName()) {
        throw std::logic_error("native component returned an invalid typed document");
    }

    record["type_id"] = std::string(NATIVE_TYPE_PREFIX) + component.GetTypeName();
    for (const auto &field : COMPONENT_BASE_FIELDS)
        componentDocument.erase(field);
    record["data"] = std::move(componentDocument);
    return record;
}

DecodedComponentRecord DecodeComponentRecord(const nlohmann::json &document)
{
    RequireExactFields(document, RECORD_FIELDS, "ComponentRecord");
    if (!document["component_id"].is_number_unsigned() || document["component_id"].get<uint64_t>() == 0)
        throw std::invalid_argument("ComponentRecord.component_id must be a non-zero unsigned integer");
    if (!document["type_id"].is_string() || document["type_id"].get_ref<const std::string &>().empty())
        throw std::invalid_argument("ComponentRecord.type_id must be a non-empty string");
    if (!document["enabled"].is_boolean())
        throw std::invalid_argument("ComponentRecord.enabled must be boolean");
    if (!document["execution_order"].is_number_integer())
        throw std::invalid_argument("ComponentRecord.execution_order must be an integer");
    if (!document["data"].is_object())
        throw std::invalid_argument("ComponentRecord.data must be an object");

    DecodedComponentRecord record;
    record.componentId = document["component_id"].get<uint64_t>();
    record.typeId = document["type_id"].get<std::string>();
    record.enabled = document["enabled"].get<bool>();
    record.executionOrder = document["execution_order"].get<int>();
    record.data = document["data"];

    if (record.typeId.rfind(NATIVE_TYPE_PREFIX, 0) == 0) {
        record.kind = ComponentRecordKind::Native;
        record.nativeTypeName = record.typeId.substr(std::char_traits<char>::length(NATIVE_TYPE_PREFIX));
        if (record.nativeTypeName.empty())
            throw std::invalid_argument("native ComponentRecord.type_id has no type name");
        RejectReservedDataFields(record.data, COMPONENT_BASE_FIELDS, "native ComponentRecord");
        return record;
    }

    if (record.typeId.rfind(PYTHON_TYPE_PREFIX, 0) != 0)
        throw std::invalid_argument("ComponentRecord.type_id has an unknown namespace");
    const std::vector<std::string> parts = SplitPythonTypeId(record.typeId);
    if (parts.size() != 4 || parts[0].empty() || parts[1].empty() || parts[2].empty() || parts[3].empty())
        throw std::invalid_argument(
            "Python ComponentRecord.type_id must encode script GUID, type GUID, module, and qualname");
    static const std::unordered_set<std::string> pythonMetadata = {
        "__type_name__",
        "__component_id__",
    };
    RejectReservedDataFields(record.data, pythonMetadata, "Python ComponentRecord");
    record.kind = ComponentRecordKind::Python;
    record.scriptGuid = parts[0];
    record.typeGuid = parts[1];
    record.moduleName = parts[2];
    record.qualifiedName = parts[3];
    record.pythonTypeName = PythonTypeNameFromQualname(record.qualifiedName);
    return record;
}

nlohmann::json BuildNativeComponentDocument(const DecodedComponentRecord &record)
{
    if (record.kind != ComponentRecordKind::Native)
        throw std::invalid_argument("cannot build a native document from a Python ComponentRecord");
    json document = record.data;
    document["type"] = record.nativeTypeName;
    document["component_id"] = record.componentId;
    document["enabled"] = record.enabled;
    document["execution_order"] = record.executionOrder;
    return document;
}

nlohmann::json BuildPythonFieldsDocument(const DecodedComponentRecord &record)
{
    if (record.kind != ComponentRecordKind::Python)
        throw std::invalid_argument("cannot build Python fields from a native ComponentRecord");
    json document = record.data;
    document["__type_name__"] = record.pythonTypeName;
    document["__component_id__"] = record.componentId;
    return document;
}

} // namespace infernux
