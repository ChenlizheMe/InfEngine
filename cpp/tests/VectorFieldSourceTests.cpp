#include <function/resources/InxTexture/VectorFieldSource.h>

#include <cassert>
#include <cmath>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>

namespace
{
template <typename Callback> void RequireInvalid(Callback callback)
{
    bool rejected = false;
    try {
        callback();
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    assert(rejected);
}

std::string Source(std::string vectors)
{
    return R"({
        "$schema": "infernux.vector_field",
        "$version": 1,
        "dimensions": [2, 1, 1],
        "storage_order": "x_fastest",
        "bake_basis": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "vectors": )" +
           vectors + "}";
}
} // namespace

int main()
{
    const auto decoded = infernux::VectorFieldSource::Decode(Source("[[1, 2, 3], [-4, 5.5, 6]]"));
    assert(decoded.dimension == infernux::TextureDimension::Texture3D);
    assert(decoded.semantic == infernux::TextureSemantic::VectorField);
    assert(decoded.format == infernux::TextureFormat::Rgba32Float);
    assert(decoded.mipLevels.size() == 1);
    assert(decoded.mipLevels[0].width == 2 && decoded.mipLevels[0].height == 1 && decoded.mipLevels[0].depth == 1);
    assert(decoded.mipLevels[0].rowPitch == sizeof(float) * 8U);
    float values[8]{};
    std::memcpy(values, decoded.bytes.data(), sizeof(values));
    assert(values[0] == 1.0f && values[1] == 2.0f && values[2] == 3.0f && values[3] == 0.0f);
    assert(values[4] == -4.0f && std::abs(values[5] - 5.5f) < 1.0e-6f && values[7] == 0.0f);

    RequireInvalid([&] { (void)infernux::VectorFieldSource::Decode(Source("[[1, 2, 3]]")); });
    RequireInvalid([&] { (void)infernux::VectorFieldSource::Decode(Source("[[1, 2], [3, 4, 5]]")); });
    RequireInvalid([&] {
        (void)infernux::VectorFieldSource::Decode(
            R"({"$schema":"infernux.vector_field","$version":2,"dimensions":[1,1,1],"storage_order":"x_fastest","bake_basis":[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],"vectors":[[0,0,0]]})");
    });

    std::cout << "Vector field source tests passed\n";
    return 0;
}
