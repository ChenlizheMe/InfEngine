#include <function/scene/LightingData.h>

#include <cassert>
#include <cstdint>

using infernux::CanonicalLightAffectsParticles;
using infernux::CanonicalLightAffectsScene;
using infernux::CanonicalLightData;
using infernux::CanonicalLightSnapshot;
using infernux::CanonicalLightType;

int main()
{
    static_assert(sizeof(CanonicalLightData) == 80);
    static_assert(alignof(CanonicalLightData) == 16);

    CanonicalLightSnapshot snapshot;
    snapshot.Clear(41);

    CanonicalLightData directional{};
    directional.metadata = glm::uvec4(static_cast<uint32_t>(CanonicalLightType::Directional), 0xFFFFFFFFu, 2u,
                                      CanonicalLightAffectsScene | CanonicalLightAffectsParticles);
    snapshot.Add(directional);

    for (uint32_t index = 0; index < 1000; ++index) {
        CanonicalLightData local{};
        local.positionRange = glm::vec4(static_cast<float>(index), 0.0f, 0.0f, 12.0f);
        local.metadata = glm::uvec4(index % 2u == 0u ? static_cast<uint32_t>(CanonicalLightType::Point)
                                                     : static_cast<uint32_t>(CanonicalLightType::Spot),
                                    1u << (index % 31u), 0u, CanonicalLightAffectsScene);
        snapshot.Add(local);
    }

    assert(snapshot.generation == 41);
    assert(snapshot.directionalLights.size() == 1);
    assert(snapshot.localLights.size() == 1000);
    assert(snapshot.Size() == 1001);
    assert(snapshot.localLights[999].positionRange.x == 999.0f);

    snapshot.Clear(42);
    assert(snapshot.generation == 42);
    assert(snapshot.Size() == 0);
    assert(snapshot.directionalLights.capacity() >= 1);
    assert(snapshot.localLights.capacity() >= 1000);
    return 0;
}
