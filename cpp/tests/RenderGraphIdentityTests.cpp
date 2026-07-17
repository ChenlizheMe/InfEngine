#include <function/renderer/RenderGraphIdentity.h>

#include <cassert>
#include <type_traits>
#include <utility>

using infernux::GraphPassHandle;
using infernux::GraphPassHandleHash;
using infernux::GraphResourceHandle;
using infernux::GraphResourceHandleHash;
using infernux::RenderGraphIdentitySource;

int main()
{
    static_assert(!std::is_same_v<GraphResourceHandle, GraphPassHandle>);

    RenderGraphIdentitySource first;
    RenderGraphIdentitySource second;
    assert(first.Current().IsValid());
    assert(first.Current() != second.Current());

    GraphResourceHandle resource{first.Current(), 4, 2};
    GraphResourceHandle nextVersion{first.Current(), 4, 3};
    GraphPassHandle pass{first.Current(), 4};
    assert(resource.IsValid());
    assert(pass.IsValid());
    assert(resource != nextVersion);
    assert(GraphResourceHandleHash{}(resource) != GraphResourceHandleHash{}(nextVersion));
    assert(GraphPassHandleHash{}(pass) == GraphPassHandleHash{}(pass));

    const auto firstEpoch = first.Current();
    first.AdvanceEpoch();
    assert(first.Current() != firstEpoch);
    assert(resource.scope != first.Current());

    const auto transferableScope = second.Current();
    RenderGraphIdentitySource moved(std::move(second));
    assert(moved.Current() == transferableScope);
    assert(second.Current() != transferableScope);

    RenderGraphIdentitySource assigned;
    assigned = std::move(moved);
    assert(assigned.Current() == transferableScope);
    assert(moved.Current() != transferableScope);

    return 0;
}
