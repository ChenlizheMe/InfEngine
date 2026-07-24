#include <function/resources/InxTexture/SignedDistanceFieldSource.h>

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

std::string Source(std::string distances)
{
    return R"({
        "$schema": "infernux.sdf",
        "dimensions": [2, 1, 1],
        "storage_order": "x_fastest",
        "distance_unit": "field",
        "bake_basis": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "distances": )" +
           distances + "}";
}
} // namespace

int main()
{
    const auto decoded = infernux::SignedDistanceFieldSource::Decode(Source("[-0.25, 0.75]"));
    assert(decoded.dimension == infernux::TextureDimension::Texture3D);
    assert(decoded.semantic == infernux::TextureSemantic::SignedDistanceField);
    assert(decoded.format == infernux::TextureFormat::Rgba32Float);
    assert(decoded.mipLevels.size() == 1);
    assert(decoded.mipLevels[0].width == 2 && decoded.mipLevels[0].height == 1 && decoded.mipLevels[0].depth == 1);
    assert(decoded.mipLevels[0].rowPitch == sizeof(float) * 8U);
    assert(decoded.valueMin[0] == -0.25f && decoded.valueMax[0] == 0.75f);
    float values[8]{};
    std::memcpy(values, decoded.bytes.data(), sizeof(values));
    assert(values[0] == -0.25f && values[1] == 0.0f && values[3] == 0.0f);
    assert(values[4] == 0.75f && values[5] == 0.0f && values[7] == 0.0f);

    RequireInvalid([&] { (void)infernux::SignedDistanceFieldSource::Decode(Source("[-0.25]")); });
    RequireInvalid([&] {
        (void)infernux::SignedDistanceFieldSource::Decode(
            R"({"$schema":"infernux.sdf","unknown":1,"dimensions":[1,1,1],"storage_order":"x_fastest","distance_unit":"field","bake_basis":[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],"distances":[0]})");
    });
    RequireInvalid([&] {
        std::string wrongUnit = Source("[-0.25, 0.75]");
        const auto position = wrongUnit.find("\"field\"");
        wrongUnit.replace(position, 7, "\"world\"");
        (void)infernux::SignedDistanceFieldSource::Decode(wrongUnit);
    });

    std::cout << "Signed distance field source tests passed\n";
    return 0;
}
