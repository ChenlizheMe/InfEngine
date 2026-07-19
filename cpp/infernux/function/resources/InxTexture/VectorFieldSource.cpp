#include "VectorFieldSource.h"

#include <nlohmann/json.hpp>

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
        throw std::invalid_argument("vector field dimensions[" + std::to_string(index) + "] must be an integer");
    const bool valid = value.is_number_unsigned()
                           ? value.get<uint64_t>() > 0 && value.get<uint64_t>() <= MaximumDimension
                           : value.get<int64_t>() > 0 && value.get<int64_t>() <= MaximumDimension;
    if (!valid)
        throw std::invalid_argument("vector field dimensions[" + std::to_string(index) +
                                    "] is outside the supported range");
    return value.get<uint32_t>();
}
} // namespace

TextureCpuData VectorFieldSource::Decode(std::string_view document)
{
    nlohmann::json root;
    try {
        root = nlohmann::json::parse(document);
    } catch (const nlohmann::json::exception &exception) {
        throw std::invalid_argument(std::string("vector field JSON is invalid: ") + exception.what());
    }

    RequireExactKeys(root, {"$schema", "$version", "dimensions", "storage_order", "bake_basis", "vectors"},
                     "vector field");
    if (!root["$schema"].is_string() || root["$schema"].get<std::string>() != "infernux.vector_field")
        throw std::invalid_argument("vector field $schema must be 'infernux.vector_field'");
    if (!root["$version"].is_number_integer() || root["$version"].get<int64_t>() != FormatVersion)
        throw std::invalid_argument("vector field $version is unsupported");
    if (!root["storage_order"].is_string() || root["storage_order"].get<std::string>() != "x_fastest")
        throw std::invalid_argument("vector field storage_order must be 'x_fastest'");

    const auto &dimensions = root["dimensions"];
    if (!dimensions.is_array() || dimensions.size() != 3)
        throw std::invalid_argument("vector field dimensions must contain [width, height, depth]");
    const uint32_t width = ReadDimension(dimensions[0], 0);
    const uint32_t height = ReadDimension(dimensions[1], 1);
    const uint32_t depth = ReadDimension(dimensions[2], 2);
    const uint64_t plane = static_cast<uint64_t>(width) * height;
    if (plane > MaximumPayloadBytes / depth / (sizeof(float) * 4U))
        throw std::invalid_argument("vector field dimensions exceed the one-gibibyte decoded payload limit");
    const uint64_t texelCount = plane * depth;

    const auto &basis = root["bake_basis"];
    if (!basis.is_array() || basis.size() != 16)
        throw std::invalid_argument("vector field bake_basis must contain 16 row-major matrix values");

    const auto &vectors = root["vectors"];
    if (!vectors.is_array() || vectors.size() != texelCount)
        throw std::invalid_argument("vector field vectors length must equal width * height * depth");

    TextureCpuData texture;
    texture.dimension = TextureDimension::Texture3D;
    texture.semantic = TextureSemantic::VectorField;
    texture.format = TextureFormat::Rgba32Float;
    for (size_t index = 0; index < texture.bakeBasis.size(); ++index)
        texture.bakeBasis[index] =
            ReadFiniteFloat(basis[index], "vector field bake_basis[" + std::to_string(index) + "]");

    const uint64_t byteSize = texelCount * sizeof(float) * 4U;
    texture.bytes.resize(static_cast<size_t>(byteSize));
    for (uint64_t texel = 0; texel < texelCount; ++texel) {
        const auto &encoded = vectors[static_cast<size_t>(texel)];
        if (!encoded.is_array() || encoded.size() != 3)
            throw std::invalid_argument("vector field vectors[" + std::to_string(texel) +
                                        "] must contain three components");
        float decoded[4] = {
            ReadFiniteFloat(encoded[0], "vector field vector component"),
            ReadFiniteFloat(encoded[1], "vector field vector component"),
            ReadFiniteFloat(encoded[2], "vector field vector component"),
            0.0f,
        };
        std::memcpy(texture.bytes.data() + texel * sizeof(decoded), decoded, sizeof(decoded));
    }
    texture.mipLevels.push_back({width, height, depth, 0, byteSize, static_cast<uint64_t>(width) * sizeof(float) * 4U,
                                 plane * sizeof(float) * 4U});
    return texture;
}

} // namespace infernux
