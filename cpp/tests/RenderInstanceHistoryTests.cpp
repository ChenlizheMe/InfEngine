#include <function/renderer/RenderInstanceHistory.h>

#include <cassert>
#include <cstdint>

using infernux::kGPUInstanceAuxFlagValidHistory;
using infernux::PackGPUObjectId;
using infernux::RenderDomain;
using infernux::RenderDrawIdentity;
using infernux::RenderInstanceHistory;
using infernux::UnpackGPUObjectId;

namespace
{
bool MatrixEquals(const glm::mat4 &lhs, const glm::mat4 &rhs)
{
    for (glm::length_t column = 0; column < 4; ++column) {
        if (lhs[column] != rhs[column])
            return false;
    }
    return true;
}
} // namespace

int main()
{
    constexpr uint64_t objectId = 0xfedcba9876543210ull;
    assert(UnpackGPUObjectId(PackGPUObjectId(objectId)) == objectId);

    const RenderDrawIdentity first{101, 0, RenderDomain::SceneGeometry};
    const RenderDrawIdentity second{202, 0, RenderDomain::SceneGeometry};
    const glm::mat4 modelA(1.0f);
    glm::mat4 modelB(1.0f);
    modelB[3].x = 4.0f;
    glm::mat4 modelC(1.0f);
    modelC[3].x = 9.0f;

    RenderInstanceHistory history;
    history.BeginFrame(1);
    const auto firstAppearance = history.Resolve(first, modelA, objectId);
    assert(MatrixEquals(firstAppearance.previousModel, modelA));
    assert(firstAppearance.flags == 0);

    history.BeginFrame(2);
    const auto continuous = history.Resolve(first, modelB, objectId);
    assert(MatrixEquals(continuous.previousModel, modelA));
    assert(continuous.flags == kGPUInstanceAuxFlagValidHistory);

    // A second camera/pass in the same logical frame sees the same history.
    const auto repeated = history.Resolve(first, modelC, objectId);
    assert(MatrixEquals(repeated.previousModel, modelA));
    assert(repeated.flags == kGPUInstanceAuxFlagValidHistory);

    // Independent identities never share transform history.
    const auto independent = history.Resolve(second, modelC, 7);
    assert(MatrixEquals(independent.previousModel, modelC));
    assert(independent.flags == 0);
    assert(history.Size() == 2);

    // Missing a frame suppresses stale motion when the primitive reappears.
    history.BeginFrame(3);
    const auto secondFrame = history.Resolve(second, modelC, 7);
    assert(secondFrame.flags == kGPUInstanceAuxFlagValidHistory);
    history.BeginFrame(4);
    const auto reappeared = history.Resolve(first, modelC, objectId);
    assert(MatrixEquals(reappeared.previousModel, modelC));
    assert(reappeared.flags == 0);

    // Invalid identities remain usable for picking but are never retained.
    const size_t retained = history.Size();
    const auto invalid = history.Resolve({}, modelB, objectId);
    assert(MatrixEquals(invalid.previousModel, modelB));
    assert(UnpackGPUObjectId(invalid.objectId) == objectId);
    assert(history.Size() == retained);

    history.Clear();
    assert(history.Size() == 0);

    return 0;
}
