#include "ComponentDocumentValidation.h"

#include <cmath>
#include <limits>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <unordered_set>

namespace infernux::component_document_validation
{
namespace
{

std::string FieldPath(std::string_view componentType, std::string_view field)
{
    return std::string(componentType) + "." + std::string(field);
}

const nlohmann::json &RequireField(const nlohmann::json &document, std::string_view field,
                                   std::string_view componentType)
{
    const auto iterator = document.find(std::string(field));
    if (iterator == document.end())
        throw std::invalid_argument(FieldPath(componentType, field) + " is required");
    return *iterator;
}

template <typename RequiredFields, typename OptionalFields>
void ValidateComponentDocumentImpl(const nlohmann::json &document, std::string_view expectedType,
                                   const RequiredFields &requiredFields, const OptionalFields &optionalFields)
{
    if (!document.is_object())
        throw std::invalid_argument(std::string(expectedType) + " document must be an object");

    std::unordered_set<std::string> allowed = {"type", "component_id", "enabled", "execution_order"};
    for (const std::string_view field : requiredFields)
        allowed.emplace(field);
    for (const std::string_view field : optionalFields)
        allowed.emplace(field);
    for (const auto &[field, value] : document.items()) {
        (void)value;
        if (allowed.find(field) == allowed.end())
            throw std::invalid_argument(FieldPath(expectedType, field) + " is not part of the current format");
    }

    const auto &type = RequireField(document, "type", expectedType);
    if (!type.is_string() || type.get_ref<const std::string &>() != expectedType)
        throw std::invalid_argument(FieldPath(expectedType, "type") + " does not match");
    RequireBoolean(document, "enabled", expectedType);
    RequireInteger(document, "execution_order", expectedType);

    if (const auto componentId = document.find("component_id"); componentId != document.end()) {
        if (!componentId->is_number_unsigned() || componentId->get<uint64_t>() == 0)
            throw std::invalid_argument(FieldPath(expectedType, "component_id") +
                                        " must be a non-zero unsigned integer");
    }

    for (const std::string_view field : requiredFields)
        RequireField(document, field, expectedType);
}

} // namespace

void ValidateComponentDocument(const nlohmann::json &document, std::string_view expectedType,
                               std::initializer_list<std::string_view> requiredFields,
                               std::initializer_list<std::string_view> optionalFields)
{
    ValidateComponentDocumentImpl(document, expectedType, requiredFields, optionalFields);
}

void ValidateComponentDocumentFields(const nlohmann::json &document, std::string_view expectedType,
                                     const std::vector<std::string_view> &requiredFields,
                                     const std::vector<std::string_view> &optionalFields)
{
    ValidateComponentDocumentImpl(document, expectedType, requiredFields, optionalFields);
}

float RequireFiniteFloat(const nlohmann::json &document, std::string_view field, std::string_view componentType)
{
    const auto &value = RequireField(document, field, componentType);
    if (!value.is_number())
        throw std::invalid_argument(FieldPath(componentType, field) + " must be a number");
    const double number = value.get<double>();
    if (!std::isfinite(number) || std::abs(number) > std::numeric_limits<float>::max())
        throw std::invalid_argument(FieldPath(componentType, field) + " must be a finite float");
    return static_cast<float>(number);
}

int RequireInteger(const nlohmann::json &document, std::string_view field, std::string_view componentType)
{
    const auto &value = RequireField(document, field, componentType);
    if (!value.is_number_integer())
        throw std::invalid_argument(FieldPath(componentType, field) + " must be an integer");
    try {
        return value.get<int>();
    } catch (const std::exception &) {
        throw std::invalid_argument(FieldPath(componentType, field) + " is outside the supported integer range");
    }
}

uint64_t RequireUnsignedInteger(const nlohmann::json &document, std::string_view field, std::string_view componentType)
{
    const auto &value = RequireField(document, field, componentType);
    if (!value.is_number_unsigned())
        throw std::invalid_argument(FieldPath(componentType, field) + " must be an unsigned integer");
    return value.get<uint64_t>();
}

bool RequireBoolean(const nlohmann::json &document, std::string_view field, std::string_view componentType)
{
    const auto &value = RequireField(document, field, componentType);
    if (!value.is_boolean())
        throw std::invalid_argument(FieldPath(componentType, field) + " must be boolean");
    return value.get<bool>();
}

const std::string &RequireString(const nlohmann::json &document, std::string_view field, std::string_view componentType)
{
    const auto &value = RequireField(document, field, componentType);
    if (!value.is_string())
        throw std::invalid_argument(FieldPath(componentType, field) + " must be a string");
    return value.get_ref<const std::string &>();
}

void RequireFiniteVector(const nlohmann::json &document, std::string_view field, size_t size,
                         std::string_view componentType)
{
    const auto &value = RequireField(document, field, componentType);
    if (!value.is_array() || value.size() != size)
        throw std::invalid_argument(FieldPath(componentType, field) + " must contain exactly " + std::to_string(size) +
                                    " numbers");
    for (size_t index = 0; index < size; ++index) {
        if (!value[index].is_number())
            throw std::invalid_argument(FieldPath(componentType, field) + " must contain only numbers");
        const double number = value[index].get<double>();
        if (!std::isfinite(number) || std::abs(number) > std::numeric_limits<float>::max())
            throw std::invalid_argument(FieldPath(componentType, field) + " must contain finite floats");
    }
}

} // namespace infernux::component_document_validation
