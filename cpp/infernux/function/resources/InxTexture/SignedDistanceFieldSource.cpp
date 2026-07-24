#include "SignedDistanceFieldSource.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <unordered_set>

namespace infernux
{
namespace
{
constexpr uint64_t MaximumPayloadBytes = 1ULL << 30;
constexpr uint32_t MaximumDimension = 65'536;

void RequireExactKeys(const nlohmann::json &value, std::initializer_list<const char *> expected,
                      std::string_view location)
{
    if (!value.is_object())
        throw std::invalid_argument(std::string(location) + " must be an object");
    std::unordered_set<std::string> keys;
    for (const char *key : expected)
        keys.emplace(key);
    if (value.size() != keys.size())
        throw std::invalid_argument(std::string(location) + " contains missing or unknown fields");
    for (const auto &[key, ignored] : value.items()) {
        (void)ignored;
        if (keys.find(key) == keys.end())
            throw std::invalid_argument(std::string(location) + " contains unknown field '" + key + "'");
    }
}

float ReadFiniteFloat(const nlohmann::json &value, std::string_view location)
{
    if (!value.is_number())
        throw std::invalid_argument(std::string(location) + " must be numeric");
    const double decoded = value.get<double>();
    if (!std::isfinite(decoded) || std::abs(decoded) > std::numeric_limits<float>::max())
        throw std::invalid_argument(std::string(location) + " must be a finite 32-bit float");
    return static_cast<float>(decoded);
}

uint32_t ReadDimension(const nlohmann::json &value, size_t index)
{
    if (!value.is_number_integer())
        throw std::invalid_argument("signed distance field dimensions[" + std::to_string(index) +
                                    "] must be an integer");
    const bool valid = value.is_number_unsigned()
                           ? value.get<uint64_t>() > 0 && value.get<uint64_t>() <= MaximumDimension
                           : value.get<int64_t>() > 0 && value.get<int64_t>() <= MaximumDimension;
    if (!valid)
        throw std::invalid_argument("signed distance field dimensions[" + std::to_string(index) +
                                    "] is outside the supported range");
    return value.get<uint32_t>();
}
} // namespace

TextureCpuData SignedDistanceFieldSource::Decode(std::string_view document)
{
    nlohmann::json root;
    try {
        root = nlohmann::json::parse(document);
    } catch (const nlohmann::json::exception &exception) {
        throw std::invalid_argument(std::string("signed distance field JSON is invalid: ") + exception.what());
    }

    RequireExactKeys(root, {"$schema", "dimensions", "storage_order", "distance_unit", "bake_basis", "distances"},
                     "signed distance field");
    if (!root["$schema"].is_string() || root["$schema"].get<std::string>() != "infernux.sdf")
        throw std::invalid_argument("signed distance field $schema must be 'infernux.sdf'");
    if (!root["storage_order"].is_string() || root["storage_order"].get<std::string>() != "x_fastest")
        throw std::invalid_argument("signed distance field storage_order must be 'x_fastest'");
    if (!root["distance_unit"].is_string() || root["distance_unit"].get<std::string>() != "field")
        throw std::invalid_argument("signed distance field distance_unit must be 'field'");

    const auto &dimensions = root["dimensions"];
    if (!dimensions.is_array() || dimensions.size() != 3)
        throw std::invalid_argument("signed distance field dimensions must contain [width, height, depth]");
    const uint32_t width = ReadDimension(dimensions[0], 0);
    const uint32_t height = ReadDimension(dimensions[1], 1);
    const uint32_t depth = ReadDimension(dimensions[2], 2);
    const uint64_t plane = static_cast<uint64_t>(width) * height;
    if (plane > MaximumPayloadBytes / depth / (sizeof(float) * 4U))
        throw std::invalid_argument("signed distance field dimensions exceed the one-gibibyte decoded payload limit");
    const uint64_t texelCount = plane * depth;

    const auto &basis = root["bake_basis"];
    if (!basis.is_array() || basis.size() != 16)
        throw std::invalid_argument("signed distance field bake_basis must contain 16 row-major matrix values");
    const auto &distances = root["distances"];
    if (!distances.is_array() || distances.size() != texelCount)
        throw std::invalid_argument("signed distance field distances length must equal width * height * depth");

    TextureCpuData texture;
    texture.dimension = TextureDimension::Texture3D;
    texture.semantic = TextureSemantic::SignedDistanceField;
    texture.format = TextureFormat::Rgba32Float;
    for (size_t index = 0; index < texture.bakeBasis.size(); ++index)
        texture.bakeBasis[index] =
            ReadFiniteFloat(basis[index], "signed distance field bake_basis[" + std::to_string(index) + "]");

    const uint64_t byteSize = texelCount * sizeof(float) * 4U;
    texture.bytes.resize(static_cast<size_t>(byteSize));
    float minimum = std::numeric_limits<float>::max();
    float maximum = std::numeric_limits<float>::lowest();
    for (uint64_t texel = 0; texel < texelCount; ++texel) {
        const float distance = ReadFiniteFloat(distances[static_cast<size_t>(texel)], "signed distance field distance");
        const float decoded[4] = {distance, 0.0f, 0.0f, 0.0f};
        std::memcpy(texture.bytes.data() + texel * sizeof(decoded), decoded, sizeof(decoded));
        minimum = (std::min)(minimum, distance);
        maximum = (std::max)(maximum, distance);
    }
    texture.valueMin = {minimum, 0.0f, 0.0f, 0.0f};
    texture.valueMax = {maximum, 0.0f, 0.0f, 0.0f};
    texture.mipLevels.push_back({width, height, depth, 0, byteSize, static_cast<uint64_t>(width) * sizeof(float) * 4U,
                                 plane * sizeof(float) * 4U});
    return texture;
}

} // namespace infernux
