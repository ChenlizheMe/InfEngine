#pragma once

#include <initializer_list>
#include <nlohmann/json.hpp>
#include <string>
#include <string_view>
#include <vector>

namespace infernux::component_document_validation
{

void ValidateComponentDocument(const nlohmann::json &document, std::string_view expectedType,
                               std::initializer_list<std::string_view> requiredFields,
                               std::initializer_list<std::string_view> optionalFields = {});
void ValidateComponentDocumentFields(const nlohmann::json &document, std::string_view expectedType,
                                     const std::vector<std::string_view> &requiredFields,
                                     const std::vector<std::string_view> &optionalFields);
float RequireFiniteFloat(const nlohmann::json &document, std::string_view field, std::string_view componentType);
int RequireInteger(const nlohmann::json &document, std::string_view field, std::string_view componentType);
uint64_t RequireUnsignedInteger(const nlohmann::json &document, std::string_view field, std::string_view componentType);
bool RequireBoolean(const nlohmann::json &document, std::string_view field, std::string_view componentType);
const std::string &RequireString(const nlohmann::json &document, std::string_view field,
                                 std::string_view componentType);
void RequireFiniteVector(const nlohmann::json &document, std::string_view field, size_t size,
                         std::string_view componentType);

} // namespace infernux::component_document_validation
