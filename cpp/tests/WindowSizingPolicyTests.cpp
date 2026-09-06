#include <platform/window/WindowSizingPolicy.h>

#include <cassert>
#include <stdexcept>

int main()
{
    using infernux::ResolveEditorInitialWindowSize;

    const auto reference = ResolveEditorInitialWindowSize(1600, 900, 1920, 1080);
    assert(reference.width == 1600);
    assert(reference.height == 900);

    const auto constrained = ResolveEditorInitialWindowSize(1600, 900, 1024, 768);
    assert(constrained.width == 921);
    assert(constrained.height == 691);

    const auto compact = ResolveEditorInitialWindowSize(800, 600, 2560, 1440);
    assert(compact.width == 800);
    assert(compact.height == 600);

    bool rejected = false;
    try {
        (void)ResolveEditorInitialWindowSize(1600, 900, 0, 768);
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    assert(rejected);
    return 0;
}
