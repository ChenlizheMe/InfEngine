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
    std::string focusedWindow;
    std::string focusedWindowId;
    std::vector<InxGUISemanticTarget> targets;
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
};

} // namespace infernux
