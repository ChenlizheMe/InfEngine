#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace infernux
{

// Read-only semantic data for tooling that must operate the editor through
// the same pointer and keyboard path as a human user.
struct InxGUISemanticTarget
{
    std::string id;
    std::string semanticId;
    std::string label;
    std::string kind;
    std::string window;
    std::string windowId;
    std::string occludedByWindow;
    std::string occludedByWindowId;
    uint32_t itemId = 0;
    float x = 0.0f;
    float y = 0.0f;
    float width = 0.0f;
    float height = 0.0f;
    float clickX = 0.0f;
    float clickY = 0.0f;
    bool enabled = true;
    bool visible = true;
    bool hasClickPoint = false;
    bool active = false;
    bool focused = false;
    bool hasBoolValue = false;
    bool boolValue = false;
    bool hasNumericValue = false;
    double numericValue = 0.0;
    bool hasStringValue = false;
    std::string stringValue;
};

struct InxGUISemanticSnapshot
{
    bool captureEnabled = false;
    uint64_t frame = 0;
    uint64_t requestSequence = 0;
    uint64_t inputSequence = 0;
    float mouseX = 0.0f;
    float mouseY = 0.0f;
    bool wantsTextInput = false;
    bool dragDropActive = false;
    bool dragDropPreview = false;
    bool dragDropDelivery = false;
    std::string dragDropPayloadType;
    uint32_t dragDropSourceId = 0;
    uint32_t dragDropAcceptId = 0;
    std::string focusedWindow;
    std::string focusedWindowId;
    std::vector<InxGUISemanticTarget> targets;
};

struct InxGUISemanticCaptureState
{
    bool continuous = false;
    bool active = false;
    uint64_t requestedSequence = 0;
    uint64_t completedSequence = 0;
    uint64_t pendingInputSequence = 0;
    uint64_t workingRequestSequence = 0;
    uint64_t beginFrameCount = 0;
    uint64_t endFrameCount = 0;
    uint64_t abortFrameCount = 0;
    uint64_t publishCount = 0;
};

class InxGUISemantics
{
  public:
    static void SetCaptureEnabled(bool enabled);
    [[nodiscard]] static bool IsCaptureEnabled();
    [[nodiscard]] static bool HasPendingCaptureRequest();
    static uint64_t RequestSnapshot(uint64_t inputSequence = 0);

    static void BeginFrame(uint64_t frame);
    static void EndFrame();
    static void AbortFrame() noexcept;

    /// Return the focused ImGui window identity tracked every GUI frame.
    /// This remains available when full semantic capture is disabled.
    [[nodiscard]] static std::string GetFocusedWindowId();

    /// Visibility of a root editor window in the last fully completed ImGui
    /// frame. nullopt means no frame snapshot has been published yet.
    [[nodiscard]] static std::optional<bool> WasWindowContentPresented(const std::string &windowId);

    /// Selected window in the same dock node during the last completed frame.
    /// Floating windows and unknown windows return nullopt.
    [[nodiscard]] static std::optional<std::string> PresentedDockPeerForWindow(const std::string &windowId);

    static void RecordLastItem(const std::string &kind, const std::string &label, bool enabled = true,
                               const std::string &semanticId = "", std::optional<bool> boolValue = std::nullopt,
                               std::optional<double> numericValue = std::nullopt,
                               std::optional<std::string> stringValue = std::nullopt);
    static void RecordRect(const std::string &kind, const std::string &label, float x, float y, float width,
                           float height, bool enabled = true, const std::string &semanticId = "");
    static void RecordCurrentWindow(const std::string &kind, const std::string &label,
                                    const std::string &semanticId = "");
    static void RecordCurrentWindowCloseButton(const std::string &semanticId);

    [[nodiscard]] static InxGUISemanticSnapshot GetSnapshot();
    [[nodiscard]] static InxGUISemanticCaptureState GetCaptureState() noexcept;
};

} // namespace infernux
