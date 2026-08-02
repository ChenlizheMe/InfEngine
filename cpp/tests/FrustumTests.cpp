#include <function/renderer/Frustum.h>

#include <cassert>

int main()
{
    using namespace infernux;

    Frustum perspective;
    perspective.ExtractFromMatrix(glm::perspectiveLH_ZO(glm::radians(90.0f), 1.0f, 1.0f, 10.0f));
    assert(perspective.ContainsPoint({0.0f, 0.0f, 2.0f}));
    assert(!perspective.ContainsPoint({0.0f, 0.0f, 0.5f}));
    assert(!perspective.ContainsPoint({0.0f, 0.0f, 11.0f}));
    assert(!perspective.ContainsPoint({3.0f, 0.0f, 2.0f}));
    assert(perspective.IntersectsSphere({{0.0f, 0.0f, 0.75f}, 0.3f}));
    assert(!perspective.IntersectsSphere({{0.0f, 0.0f, 0.5f}, 0.2f}));

    Frustum translated;
    const glm::mat4 view =
        glm::lookAtLH(glm::vec3(5.0f, 0.0f, 0.0f), glm::vec3(5.0f, 0.0f, 1.0f), glm::vec3(0.0f, 1.0f, 0.0f));
    translated.ExtractFromMatrix(glm::perspectiveLH_ZO(glm::radians(60.0f), 1.0f, 0.1f, 100.0f) * view);
    assert(translated.ContainsPoint({5.0f, 0.0f, 5.0f}));
    assert(!translated.ContainsPoint({0.0f, 0.0f, 5.0f}));

    Frustum orthographic;
    orthographic.ExtractFromMatrix(glm::orthoLH_ZO(-2.0f, 2.0f, -1.0f, 1.0f, 0.5f, 20.0f));
    assert(orthographic.ContainsPoint({1.5f, 0.5f, 5.0f}));
    assert(!orthographic.ContainsPoint({2.5f, 0.0f, 5.0f}));
    assert(!orthographic.ContainsPoint({0.0f, 0.0f, 0.25f}));
    return 0;
}
