#pragma once

#include "RhiHandles.h"
#include "RhiTypes.h"

#include <cstdint>

namespace infernux::rhi
{

using RenderViewId = uint64_t;
inline constexpr RenderViewId InvalidRenderViewId = 0;

/// Allocate a process-unique render-view identity shared by Scene, Game,
/// Preview, Capture, and future presentation surfaces.
[[nodiscard]] RenderViewId AllocateRenderViewId() noexcept;

enum class RenderViewKind : uint8_t
{
    Scene,
    Game,
    Preview,
    Capture,
    Presentation,
};

enum class RenderOutputKind : uint8_t
{
    OffscreenTexture,
    PresentationImage,
    Readback,
    CrossDeviceTransfer,
};

/// Backend-neutral view ownership. A view is not a swapchain: Scene, Game,
/// material preview and capture all carry the same target contract.
struct RenderViewContext
{
    RenderViewId id = InvalidRenderViewId;
    /// Source view for derived outputs such as Capture; invalid for roots.
    RenderViewId source = InvalidRenderViewId;
    DeviceId device = InvalidDeviceId;
    RenderViewKind kind = RenderViewKind::Game;
    RenderOutputKind output = RenderOutputKind::OffscreenTexture;
    uint32_t width = 0;
    uint32_t height = 0;
    PixelFormat colorFormat = PixelFormat::Undefined;
    PixelFormat depthFormat = PixelFormat::Undefined;
    SampleCount samples = SampleCount::One;
    TextureViewHandle color;
    TextureViewHandle depth;
    TextureViewHandle motion;
    TextureViewHandle history;
    uint64_t revision = 0;

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return id != InvalidRenderViewId && device != InvalidDeviceId && width > 0 && height > 0;
    }
};

} // namespace infernux::rhi
