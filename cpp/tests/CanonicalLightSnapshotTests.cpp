#include <function/scene/LightingData.h>
#include <function/renderer/lighting/CanonicalLightGpuBuffer.h>

#include <cassert>
#include <cstdint>

using infernux::CanonicalLightAffectsParticles;
using infernux::CanonicalLightAffectsGeometry;
using infernux::CanonicalLightData;
using infernux::CanonicalLightSnapshot;
using infernux::CanonicalLightType;
using infernux::lighting::BuildCanonicalLightUpload;
using infernux::lighting::CanonicalLightGpuHeader;

int main()
{
    static_assert(sizeof(CanonicalLightData) == 80);
    static_assert(alignof(CanonicalLightData) == 16);

    CanonicalLightSnapshot snapshot;
    snapshot.Clear(41);

    CanonicalLightData directional{};
    directional.metadata = glm::uvec4(static_cast<uint32_t>(CanonicalLightType::Directional), 0xFFFFFFFFu, 2u,
                                      CanonicalLightAffectsGeometry | CanonicalLightAffectsParticles);
    snapshot.Add(directional);

    for (uint32_t index = 0; index < 1000; ++index) {
        CanonicalLightData local{};
        local.positionRange = glm::vec4(static_cast<float>(index), 0.0f, 0.0f, 12.0f);
        local.metadata = glm::uvec4(index % 2u == 0u ? static_cast<uint32_t>(CanonicalLightType::Point)
                                                     : static_cast<uint32_t>(CanonicalLightType::Spot),
                                    1u << (index % 31u), 0u, CanonicalLightAffectsGeometry);
        snapshot.Add(local);
    }

    assert(snapshot.generation == 41);
    assert(snapshot.directionalLights.size() == 1);
    assert(snapshot.localLights.size() == 1000);
    assert(snapshot.Size() == 1001);
    assert(snapshot.localLights[999].positionRange.x == 999.0f);

    const auto upload = BuildCanonicalLightUpload(snapshot);
    assert(upload.header.countsAndGeneration.x == 1);
    assert(upload.header.countsAndGeneration.y == 1000);
    assert(upload.header.countsAndGeneration.z == 41);
    assert(upload.bytes.size() == sizeof(CanonicalLightGpuHeader) + 1001 * sizeof(CanonicalLightData));
    CanonicalLightData packedDirectional{};
    std::memcpy(&packedDirectional, upload.bytes.data() + sizeof(CanonicalLightGpuHeader),
                sizeof(CanonicalLightData));
    assert(packedDirectional.metadata.w == (CanonicalLightAffectsGeometry | CanonicalLightAffectsParticles));

    snapshot.Clear(42);
    assert(snapshot.generation == 42);
    assert(snapshot.Size() == 0);
    assert(snapshot.directionalLights.capacity() >= 1);
    assert(snapshot.localLights.capacity() >= 1000);
    return 0;
}
