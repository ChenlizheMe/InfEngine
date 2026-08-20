#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <utility>
#include <vector>

namespace infernux
{

void PreflightSceneResourceDependencies(const nlohmann::json &document);
[[nodiscard]] std::vector<std::pair<std::string, std::string>>
CollectSceneResourceDependencies(const nlohmann::json &document);

} // namespace infernux
