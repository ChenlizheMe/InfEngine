#pragma once

#include <cstdint>
#include <function/renderer/rhi/RenderViewContext.h>
#include <memory>
#include <string>

namespace infernux
{
namespace vk
{
class ImageReadbackTicket;
}

enum class CaptureSource
{
    Scene,
    Game,
};

enum class CaptureStatus
{
    PendingGpu,
    PendingEncode,
    Completed,
    Failed,
    Cancelled,
    SourceExpired,
};

struct CaptureSnapshot
{
    uint64_t id = 0;
    CaptureSource source = CaptureSource::Game;
    CaptureStatus status = CaptureStatus::Failed;
    uint64_t sourceGeneration = 0;
    rhi::RenderViewContext view;
    uint64_t engineFrame = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    std::string outputPath;
    std::string error;
};

[[nodiscard]] const char *CaptureSourceName(CaptureSource source) noexcept;
[[nodiscard]] const char *CaptureStatusName(CaptureStatus status) noexcept;

/// Owns asynchronous render-target readbacks and image encoding. All public
/// methods are called on the renderer thread; encoding runs off-thread.
class CaptureService
{
  public:
    CaptureService();
    ~CaptureService();

    CaptureService(const CaptureService &) = delete;
    CaptureService &operator=(const CaptureService &) = delete;

    /// Register a capture before the renderer produces the source frame.
    /// The renderer attaches the readback ticket after that frame is submitted.
    [[nodiscard]] uint64_t Request(CaptureSource source, const rhi::RenderViewContext &view, uint64_t sourceGeneration,
                                   uint64_t engineFrame, std::string outputPath);
    /// Attach a readback produced by the requested render frame. The captured
    /// frame replaces the request-time frame in the returned metadata.
    [[nodiscard]] bool AttachReadback(uint64_t captureId, std::shared_ptr<vk::ImageReadbackTicket> ticket,
                                      uint64_t engineFrame);
    void Fail(uint64_t captureId, std::string error);
    [[nodiscard]] CaptureSnapshot Query(uint64_t captureId) const;
    [[nodiscard]] bool Cancel(uint64_t captureId);
    void InvalidateSource(CaptureSource source, uint64_t sourceGeneration);
    void Poll();

  private:
    struct Impl;
    std::unique_ptr<Impl> m_impl;
};

} // namespace infernux
