#pragma once

#include <nlohmann/json.hpp>
#include <string_view>

namespace infernux::material_document_validation
{

void ValidateMaterialDocument(const nlohmann::json &document, std::string_view path = "Material");

} // namespace infernux::material_document_validation
