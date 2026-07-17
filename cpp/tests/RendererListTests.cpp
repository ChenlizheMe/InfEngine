#include <function/renderer/RendererList.h>

#include <cassert>
#include <vector>

using infernux::DrawCall;
using infernux::RenderDomain;
using infernux::RenderDomainBit;
using infernux::RendererList;
using infernux::RendererListPurpose;

int main()
{
    std::vector<DrawCall> source(2);
    source[0].objectId = 10;
    source[1].objectId = 20;

    RendererList borrowed =
        RendererList::Borrow(source, RendererListPurpose::CameraVisible, RenderDomainBit(RenderDomain::SceneGeometry));
    assert(borrowed.IsBorrowed());
    assert(borrowed.Size() == 2);
    assert(borrowed.Purpose() == RendererListPurpose::CameraVisible);
    assert(borrowed.ContainsDomain(RenderDomain::SceneGeometry));
    assert(!borrowed.ContainsDomain(RenderDomain::Particle));

    source[0].objectId = 11;
    assert(borrowed.DrawCalls()[0].objectId == 11);

    RendererList borrowedCopy = borrowed;
    assert(borrowedCopy.IsBorrowed());
    assert(&borrowedCopy.DrawCalls() == &source);

    std::vector<DrawCall> materialized = borrowedCopy.Consume();
    assert(materialized.size() == 2);
    assert(materialized[0].objectId == 11);
    assert(borrowedCopy.Empty());
    assert(!borrowedCopy.IsBorrowed());

    std::vector<DrawCall> ownedSource(1);
    ownedSource[0].objectId = 42;
    RendererList owned = RendererList::Own(std::move(ownedSource), RendererListPurpose::ShadowCasters,
                                           RenderDomainBit(RenderDomain::SceneGeometry));
    assert(!owned.IsBorrowed());
    assert(owned.Size() == 1);
    assert(owned.Purpose() == RendererListPurpose::ShadowCasters);

    RendererList moved = std::move(owned);
    std::vector<DrawCall> consumed = moved.Consume();
    assert(consumed.size() == 1);
    assert(consumed[0].objectId == 42);
    assert(moved.Empty());

    return 0;
}
