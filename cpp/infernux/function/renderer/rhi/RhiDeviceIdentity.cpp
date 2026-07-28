#include "RenderViewContext.h"
#include "RhiHandles.h"

#include <atomic>

namespace infernux::rhi
{

DeviceId AllocateDeviceId() noexcept
{
    static std::atomic<uint32_t> next{1};
    while (true) {
        const auto candidate = static_cast<DeviceId>(next.fetch_add(1, std::memory_order_relaxed));
        if (candidate != InvalidDeviceId)
            return candidate;
    }
}

RenderViewId AllocateRenderViewId() noexcept
{
    static std::atomic<RenderViewId> next{1};
    while (true) {
        const RenderViewId candidate = next.fetch_add(1, std::memory_order_relaxed);
        if (candidate != InvalidRenderViewId)
            return candidate;
    }
}

} // namespace infernux::rhi
