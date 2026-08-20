#include <function/renderer/rhi/RhiDevice.h>
#include <function/renderer/rhi/RhiResourceIndex.h>
#include <function/renderer/rhi/RhiTexture.h>

#include <cassert>
#include <thread>
#include <vector>

using namespace infernux::rhi;

int main()
{
    ResourceIndexAllocator indices(3);
    assert(indices.Fallback().IsFallback());
    assert(indices.ResolveShaderIndex(indices.Fallback()) == 0);

    const ResourceIndex first = indices.Allocate();
    const ResourceIndex second = indices.Allocate();
    assert(first.index == 1 && second.index == 2);
    assert(indices.IsLive(first) && indices.IsLive(second));
    assert(!indices.Allocate().IsValid());

    assert(!indices.RetireAfter(indices.Fallback(), 4));
    assert(!indices.RetireAfter(first, InvalidSubmissionSerial));
    assert(indices.MarkUsed(first, 9));
    assert(!indices.MarkUsed(first, InvalidSubmissionSerial));
    assert(indices.RetireAfter(first, 7));
    assert(!indices.IsLive(first));
    assert(indices.ResolveShaderIndex(first) == ResourceIndex::FallbackIndex);
    assert(!indices.RetireAfter(first, 8));

    // Retirement is not a free-list insertion. The in-flight shader slot must
    // remain unavailable until the exact submission completes.
    assert(!indices.Allocate().IsValid());
    assert(indices.Collect(6) == 0);
    assert(!indices.Allocate().IsValid());
    assert(indices.Collect(7) == 0);
    assert(indices.Collect(8) == 0);
    assert(indices.Collect(9) == 1);

    const ResourceIndex replacement = indices.Allocate();
    assert(replacement.index == first.index);
    assert(replacement.generation != first.generation);
    assert(indices.ResolveShaderIndex(first) == ResourceIndex::FallbackIndex);
    assert(indices.ResolveShaderIndex(replacement) == replacement.index);

    ResourceIndexAllocator unpublished(2);
    const ResourceIndex pending = unpublished.Allocate();
    assert(unpublished.IsLive(pending));
    assert(unpublished.Cancel(pending));
    assert(!unpublished.IsLive(pending));
    const ResourceIndex reused = unpublished.Allocate();
    assert(reused.index == pending.index);
    assert(reused.generation != pending.generation);
    assert(!unpublished.IsLive(pending));
    assert(unpublished.ResolveShaderIndex(pending) == ResourceIndex::FallbackIndex);
    assert(unpublished.ResolveShaderIndex(reused) == reused.index);

    const auto stats = indices.GetStats();
    assert(stats.capacity == 3);
    assert(stats.live == 2);
    assert(stats.pendingRetirement == 0);
    assert(stats.available == 0);
    assert(stats.highWatermark == 2);
    assert(stats.allocations == 3);
    assert(stats.allocationFailures == 3);
    assert(stats.retirements == 1);
    assert(stats.collections == 1);

    // Allocation and validation share one lock so concurrent publication
    // cannot expose duplicate live slots.
    ResourceIndexAllocator concurrent(65);
    std::vector<ResourceIndex> allocated(64);
    std::vector<std::thread> workers;
    workers.reserve(64);
    for (size_t index = 0; index < workers.capacity(); ++index)
        workers.emplace_back([&, index] { allocated[index] = concurrent.Allocate(); });
    for (auto &worker : workers)
        worker.join();

    for (size_t lhs = 0; lhs < allocated.size(); ++lhs) {
        assert(concurrent.IsLive(allocated[lhs]));
        for (size_t rhs = lhs + 1; rhs < allocated.size(); ++rhs)
            assert(allocated[lhs].index != allocated[rhs].index);
    }
    assert(!concurrent.Allocate().IsValid());

    const auto owner = std::make_shared<int>(7);
    TextureViewDesc authoredViewDesc;
    authoredViewDesc.texture = TextureHandle{1, 1};
    authoredViewDesc.format = PixelFormat::BC7Srgb;
    authoredViewDesc.baseMip = 1;
    authoredViewDesc.mipCount = 3;
    const auto view =
        std::make_shared<TextureGpuView>("texture-guid", 4, TextureHandle{1, 1}, TextureViewHandle{2, 1},
                                         SamplerHandle{3, 1}, 128, owner, PixelFormat::BC7Srgb, authoredViewDesc);
    assert(view->IsValid());
    assert(view->GetFormat() == PixelFormat::BC7Srgb);
    assert(view->GetViewDesc().texture == authoredViewDesc.texture);
    assert(view->GetViewDesc().format == PixelFormat::BC7Srgb);
    assert(view->GetViewDesc().baseMip == 1 && view->GetViewDesc().mipCount == 3);
    assert(LinearColorFormat(view->GetFormat()) == PixelFormat::BC7UNorm);
    assert(!view->GetBindlessResourceIndex(1).IsValid());
    const ResourceIndex firstViewSlot{9, 2};
    assert(view->SetBindlessResourceIndex(1, firstViewSlot));
    assert(view->GetBindlessResourceIndex(1) == firstViewSlot);
    // A device-local table may be recreated while the immutable view survives.
    // The new publication must replace the old table-local index.
    const ResourceIndex replacementViewSlot{4, 3};
    assert(view->SetBindlessResourceIndex(2, replacementViewSlot));
    assert(!view->GetBindlessResourceIndex(1).IsValid());
    assert(view->GetBindlessResourceIndex(2) == replacementViewSlot);

    auto publicationOwner = std::make_shared<int>(9);
    auto firstPublication = std::make_shared<const TextureGpuView>(
        "ordered-texture", 3, TextureHandle{4, 1}, TextureViewHandle{5, 1}, SamplerHandle{6, 1}, 64, publicationOwner);
    auto stalePublication = std::make_shared<const TextureGpuView>(
        "ordered-texture", 2, TextureHandle{7, 1}, TextureViewHandle{8, 1}, SamplerHandle{9, 1}, 64, publicationOwner);
    TextureGpuViewSlot orderedSlot("ordered-texture");
    assert(orderedSlot.TryPublish(firstPublication));
    assert(!orderedSlot.TryPublish(stalePublication));
    assert(orderedSlot.Acquire() == firstPublication);

    DeviceCapabilityState boundedState;
    DeviceCapabilityState bindlessState = boundedState;
    bindlessState.bindless.descriptorIndexing.enabled = true;
    const uint64_t boundedContract = ComputeDeviceShaderContractKey(boundedState);
    const uint64_t bindlessContract = ComputeDeviceShaderContractKey(bindlessState);
    assert(boundedContract != bindlessContract);
    return 0;
}
