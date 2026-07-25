#pragma once

#include <imgui.h>

#include <array>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

struct SDL_Window;

namespace infernux
{

// ── Property batch rendering ────────────────────────────────────────────
// Python builds a list of PropertyDesc, C++ renders all widgets in one call.

struct PropertyDesc
{
    enum Type : uint8_t
    {
        Float = 0,
        Int = 1,
        Bool = 2,
        String = 3,
        Vec2 = 4,
        Vec3 = 5,
        Vec4 = 6,
        Enum = 7,
        Color = 8
    };
    Type type = Float;
    std::string widgetId;
    std::string label;
    // Stable automation identity. Kept separate from the invisible ImGui
    // widget ID so Inspector renderers can change layout without changing
    // the public semantic contract.
    std::string semanticId;
    float fVal[4] = {0, 0, 0, 0}; // Float or vector x/y/z/w
    int iVal = 0;                 // Int or enum index
    bool bVal = false;            // Bool
    std::string sVal;             // String value
    float rangeMin = -1e6f;
    float rangeMax = 1e6f;
    float speed = 0.1f;
    bool slider = false;
    bool multiline = false;
    bool mixed = false;
    bool fieldLabel = false; // Render Bool as a label + checkbox field.
    std::vector<std::string> enumNames;
    std::string header;  // Section header text above this field (empty = none)
    float space = 0;     // Vertical padding before this field
    std::string tooltip; // Hover tooltip for the field label (empty = none)
};

struct PropertyChange
{
    int index;
    PropertyDesc::Type type;
    float fVal[4] = {0, 0, 0, 0};
    int iVal = 0;
    bool bVal = false;
    std::string sVal;
};

struct ObjectFieldInteraction
{
    int index = -1;
    uint32_t flags = 0;
    bool popupOpen = false;
    std::string payloadType;
    std::string payload;
};

struct PropertyBatchPlan
{
    std::vector<PropertyDesc> descriptors;
};

struct MaterialTopInteraction
{
    uint32_t vertexFlags = 0;
    bool vertexPickerOpen = false;
    std::string vertexPayload;
    bool vertexListPopupOpen = false;
    uint32_t fragmentFlags = 0;
    bool fragmentPickerOpen = false;
    std::string fragmentPayload;
    bool fragmentListPopupOpen = false;
    std::vector<PropertyChange> surfaceChanges;
};

class InxGUIContext
{
  public:
    /* DPI scale — set by InxGUI::Init, read by Python/UI code */
    static float s_dpiScale;
    float GetDpiScale() const;

    /// True only while ImGui is inside a frame with an active window.
    /// Native batch renderers use this to avoid entering widgets from
    /// selection/change callbacks that can run between panel renders.
    [[nodiscard]] bool CanRenderWidgets() const;
    /* basic text & labels */
    void Label(const std::string &text);
    void TextWrapped(const std::string &text);

    /* buttons / clickables */
    bool Button(const std::string &label, std::function<void()> onClick, float width = 0.0f, float height = 0.0f);
    bool RadioButton(const std::string &label, bool active);
    bool Selectable(const std::string &label, bool selected = false, int flags = 0, float width = 0.0f,
                    float height = 0.0f);

    /* value editors */
    void Checkbox(const std::string &label, bool *value);
    /// Inspector checkbox: square at ``INSPECTOR_CHECKBOX_BOX_SCALE``, label at normal font size.
    bool CheckboxInspector(const std::string &label, bool *value);
    void IntSlider(const std::string &label, int *value, int min, int max);
    void FloatSlider(const std::string &label, float *value, float min, float max);
    bool DragFloat(const std::string &label, float *value, float speed = 1.0f, float min = 0.0f, float max = 0.0f,
                   const char *fmt = "%.3f", float power = 1.0f);
    bool DragInt(const std::string &label, int *value, float speed = 1.0f, int min = 0, int max = 0,
                 const char *fmt = "%d");

    void TextInput(const std::string &label, char *buffer, size_t bufferSize);
    void TextArea(const std::string &label, char *buffer, size_t bufferSize);
    bool InputTextWithHint(const std::string &label, const std::string &hint, char *buffer, size_t bufferSize,
                           int flags = 0);
    bool InputInt(const std::string &label, int *value, int step = 1, int stepFast = 100, int flags = 0);
    bool InputUInt(const std::string &label, uint32_t *value, uint32_t step = 1, uint32_t stepFast = 100,
                   int flags = 0);
    bool InputFloat(const std::string &label, float *value, float step = 0.0f, float stepFast = 0.0f, int flags = 0);

    void ColorEdit(const std::string &label, float color[4], bool hdr = false);
    bool ColorPicker(const std::string &label, float color[4], int flags = 0);
    void Vector2Control(const std::string &label, float value[2], float speed = 0.1f, float labelWidth = 0.0f,
                        const std::string &axisSemanticBase = "");
    void Vector3Control(const std::string &label, float value[3], float speed = 0.1f, float labelWidth = 0.0f,
                        const std::string &axisSemanticBase = "");
    void Vector4Control(const std::string &label, float value[4], float speed = 0.1f, float labelWidth = 0.0f);

    /* combo & lists */
    bool Combo(const std::string &label, int *currentItem, const std::vector<std::string> &items,
               int popupMaxHeightInItems = -1);
    int SearchableCombo(const std::string &id, int currentItem, const std::vector<std::string> &items,
                        float width = 0.0f, int maxVisibleItems = 8, const std::string &searchHint = "Filter...",
                        const std::string &emptyText = "No results");
    bool ListBox(const std::string &label, int *currentItem, const std::vector<std::string> &items,
                 int heightInItems = -1);

    /* progress & indicators */
    void ProgressBar(float fraction, float width = 0.0f, float height = 0.0f, const std::string &overlay = "");

    /* layout helpers */
    void BeginGroup(const std::string &name = "");
    void EndGroup();

    void SameLine(float offsetFromStartX = 0.0f, float spacing = -1.0f);
    void AlignTextToFramePadding();
    void SetScrollHereY(float centerYRatio = 0.5f);
    float GetScrollY();
    float GetScrollMaxY();
    void SetScrollX(float scrollX);
    void SetScrollY(float scrollY);
    void Separator();
    void Spacing();
    void Dummy(float width, float height);
    void NewLine();

    /* tree & collapsing sections */
    bool TreeNode(const std::string &label);
    bool TreeNodeEx(const std::string &label, int flags);
    void TreePop();
    void SetNextItemOpen(bool is_open, int cond = 0);
    void SetNextItemAllowOverlap();
    bool CollapsingHeader(const std::string &label);
    bool RenderCompactSectionHeader(const std::string &label, uint64_t iconId, bool defaultOpen, int openCondition,
                                    bool allowOverlap, float framePadX, float framePadY, float itemSpacingX,
                                    float itemSpacingY, float borderSize, bool zeroIndent, float fontScale,
                                    float rightMargin, float iconSize, const std::array<float, 4> &headerColor,
                                    const std::array<float, 4> &hoverColor, const std::array<float, 4> &activeColor,
                                    bool useTextColor, const std::array<float, 4> &textColor);
    bool IsItemClicked(int mouseButton = 0);

    /* tab bars */
    bool BeginTabBar(const std::string &id);
    void EndTabBar();
    bool BeginTabItem(const std::string &label, bool *open = nullptr);
    void EndTabItem();

    /* main-menu / menus */
    bool BeginMainMenuBar();
    void EndMainMenuBar();
    bool BeginMenu(const std::string &label, bool enabled = true, const std::string &semanticId = "");
    void EndMenu();
    bool MenuItem(const std::string &label, const std::string &shortcut = "", bool selected = false,
                  bool enabled = true);

    /* child regions & windows */
    bool BeginChild(const std::string &id, float width = 0.0f, float height = 0.0f, bool border = false, int flags = 0);
    void EndChild();

    /* pop-ups & tooltips */
    void OpenPopup(const std::string &id);
    bool BeginPopup(const std::string &id);
    bool BeginPopupModal(const std::string &title, int flags = 0);
    bool BeginPopupContextItem(const std::string &id = "", int mouseButton = 1);
    bool BeginPopupContextWindow(const std::string &id = "", int mouseButton = 1, bool noOpenOverItems = false);
    void EndPopup();
    void CloseCurrentPopup();
    void BeginTooltip();
    void EndTooltip();
    void SetTooltip(const std::string &text);

    /* images */
    void Image(void *textureId, float width, float height, float uv0_x = 0.0f, float uv0_y = 0.0f, float uv1_x = 1.0f,
               float uv1_y = 1.0f);
    bool ImageButton(const std::string &id, void *textureId, float width, float height, float uv0_x = 0.0f,
                     float uv0_y = 0.0f, float uv1_x = 1.0f, float uv1_y = 1.0f);

    /* tables */
    bool BeginTable(const std::string &id, int columns, int flags = 0, float innerWidth = 0.0f);
    void EndTable();
    void TableSetupColumn(const std::string &label, int flags = 0, float initWidthOrWeight = 0.0f, int userID = 0);
    void TableHeadersRow();
    void TableNextRow();
    void TableSetColumnIndex(int columnIndex);
    bool TableNextColumn();

    /* misc helpers */
    bool CheckboxFlags(const std::string &label, unsigned int *flags, unsigned int flagValue);

    void SetNextItemWidth(float width);
    void SetNextWindowSize(float width, float height, int cond = 0);
    void SetNextWindowPos(float x, float y, int cond = 0, float pivot_x = 0.0f, float pivot_y = 0.0f);
    void SetNextWindowFocus();
    void SetWindowFocus();
    void GetMainViewportBounds(float *x, float *y, float *w, float *h);
    bool BeginWindow(const std::string &name, bool *open = nullptr, int flags = 0);
    void EndWindow();

    /* layout query */
    float CalcTextWidth(const std::string &text);
    float GetContentRegionAvailWidth();
    float GetContentRegionAvailHeight();
    float GetCursorPosX();
    float GetCursorPosY();
    /// Return whether the vertical region at the current cursor intersects
    /// the current window clip rectangle.
    bool IsVirtualizedRegionVisible(float height);
    void SetCursorPosX(float x);
    void SetCursorPosY(float y);
    float GetWindowPosX();
    float GetWindowPosY();
    float GetWindowWidth();

    // Item rectangle (last item)
    float GetItemRectMinX();
    float GetItemRectMinY();
    float GetItemRectMaxX();
    float GetItemRectMaxY();

    // Explicit semantic hooks for raw ImGui widgets that bypass this wrapper.
    // They are inert unless the remote validation capture is enabled.
    void RecordSemanticItem(const std::string &kind, const std::string &label, bool enabled = true,
                            const std::string &semanticId = "", std::optional<bool> boolValue = std::nullopt,
                            std::optional<double> numericValue = std::nullopt,
                            std::optional<std::string> stringValue = std::nullopt);
    void RecordSemanticRect(const std::string &kind, const std::string &label, float x, float y, float width,
                            float height, bool enabled = true, const std::string &semanticId = "");
    void RecordSemanticWindow(const std::string &kind, const std::string &label, const std::string &semanticId = "");

    /* invisible button (for splitter) */
    bool InvisibleButton(const std::string &id, float width, float height);
    bool IsItemActive();
    bool IsAnyItemActive();
    bool IsItemHovered();
    bool IsItemFocused();

    /* focus & activation */
    void SetKeyboardFocusHere(int offset = 0);
    bool IsItemDeactivated();
    bool IsItemDeactivatedAfterEdit();

    float GetMouseDragDeltaY(int button = 0);
    void ResetMouseDragDelta(int button = 0);

    /* ID stack - for unique widget IDs */
    void PushID(int id);
    void PushID(const std::string &id);
    void PopID();

    /* Style API */
    void PushStyleColor(int idx, float r, float g, float b, float a);
    void PopStyleColor(int count = 1);
    void PushStyleVarFloat(int idx, float val);
    void PushStyleVarVec2(int idx, float x, float y);
    void PopStyleVar(int count = 1);
    void BeginDisabled(bool disabled = true);
    void EndDisabled();

    /* Drag and Drop */
    bool BeginDragDropSource(int flags = 0);
    bool SetDragDropPayload(const std::string &type, uint64_t data);
    bool SetDragDropPayload(const std::string &type, const std::string &data);
    void EndDragDropSource();
    bool BeginDragDropTarget();
    bool AcceptDragDropPayload(const std::string &type, uint64_t *outData);
    bool AcceptDragDropPayload(const std::string &type, std::string *outData);
    /// Accept whichever payload is being dragged (uses current ImGui payload ``DataType``).
    bool AcceptAnyDragDropPayload(std::string *outType, uint64_t *outU64, std::string *outStr, bool *outIsU64);
    void EndDragDropTarget();

    /* mouse cursor */
    void SetMouseCursor(int cursorType); // 0=Arrow, 1=TextInput, 2=ResizeAll, 3=ResizeNS, 4=ResizeEW, 5=ResizeNESW,
                                         // 6=ResizeNWSE, 7=Hand, 8=NotAllowed

    // ========================================================================
    // Scene View Input API - for Unity-style editor camera controls
    // ========================================================================

    /* mouse state */
    bool IsMouseButtonDown(int button); // 0=left, 1=right, 2=middle
    bool IsMouseButtonClicked(int button);
    bool IsMouseDoubleClicked(int button = 0);
    bool IsMouseDragging(int button, float lockThreshold = -1.0f);
    float GetMouseDragDeltaX(int button = 0);
    float GetMousePosX();
    float GetMousePosY();
    float GetMouseWheelDelta(); // scroll delta

    /* keyboard state */
    bool IsKeyDown(int keyCode);     // ImGuiKey enum values
    bool IsKeyPressed(int keyCode);  // Just pressed this frame
    bool IsKeyReleased(int keyCode); // Just released this frame

    /* window focus */
    bool IsWindowFocused(int flags = 0); // Is current window focused
    bool IsWindowHovered(int flags = 0); // Is mouse over window
    bool WantTextInput();                // True when a text field is active (shortcuts should be suppressed)

    /* helper: capture mouse input in scene view */
    void CaptureMouseFromApp(bool capture);    // Prevent app from receiving mouse
    void CaptureKeyboardFromApp(bool capture); // Prevent app from receiving keyboard

    /* mouse warp for Unity-style screen-edge wrapping */
    void WarpMouseGlobal(float x, float y);                        // Warp cursor to global screen coords
    float GetGlobalMousePosX();                                    // Global (screen) mouse X
    float GetGlobalMousePosY();                                    // Global (screen) mouse Y
    void GetDisplayBounds(float *x, float *y, float *w, float *h); // Primary display rect

    /* clipboard */
    void SetClipboardText(const std::string &text);
    std::string GetClipboardText();

    /* editable multiline text input */
    std::string InputTextMultiline(const std::string &label, const std::string &text, size_t bufferSize, float width,
                                   float height, int flags);

    /* font scale (affects all subsequent ImGui text in the current window) */
    void SetWindowFontScale(float scale);

    /* draw list primitives (screen-space) */
    void DrawRect(float minX, float minY, float maxX, float maxY, float r, float g, float b, float a,
                  float thickness = 1.0f, float rounding = 0.0f);
    void DrawFilledRect(float minX, float minY, float maxX, float maxY, float r, float g, float b, float a,
                        float rounding = 0.0f);
    void DrawFilledRectRotated(float minX, float minY, float maxX, float maxY, float r, float g, float b, float a,
                               float rotation = 0.0f, bool mirrorH = false, bool mirrorV = false,
                               float rounding = 0.0f);
    void DrawLine(float x1, float y1, float x2, float y2, float r, float g, float b, float a, float thickness = 1.0f);
    void DrawCircle(float centerX, float centerY, float radius, float r, float g, float b, float a,
                    float thickness = 1.0f, int segments = 0);
    void DrawFilledCircle(float centerX, float centerY, float radius, float r, float g, float b, float a,
                          int segments = 0);
    void DrawImageRect(uint64_t textureId, float minX, float minY, float maxX, float maxY, float uv0_x = 0.0f,
                       float uv0_y = 0.0f, float uv1_x = 1.0f, float uv1_y = 1.0f, float tintR = 1.0f,
                       float tintG = 1.0f, float tintB = 1.0f, float tintA = 1.0f, float rotation = 0.0f,
                       bool mirrorH = false, bool mirrorV = false, float rounding = 0.0f);
    void DrawText(float x, float y, const std::string &text, float r, float g, float b, float a, float fontSize = 0.0f);
    void DrawTextAligned(float minX, float minY, float maxX, float maxY, const std::string &text, float r, float g,
                         float b, float a, float alignX, float alignY, float fontSize = 0.0f, bool clip = false);
    void DrawTextRotated90Aligned(float minX, float minY, float maxX, float maxY, const std::string &text, float r,
                                  float g, float b, float a, float alignX, float alignY, float fontSize = 0.0f,
                                  bool clockwise = false, bool clip = false);
    void DrawTextExAligned(float minX, float minY, float maxX, float maxY, const std::string &text, float r, float g,
                           float b, float a, float alignX, float alignY, float fontSize = 0.0f, float wrapWidth = 0.0f,
                           float rotation = 0.0f, bool mirrorH = false, bool mirrorV = false, bool clip = false,
                           const std::string &fontPath = "", float lineHeight = 1.0f, float letterSpacing = 0.0f);
    std::pair<float, float> CalcTextSizeA(const std::string &text, float fontSize = 0.0f,
                                          const std::string &fontPath = "", float lineHeight = 1.0f,
                                          float letterSpacing = 0.0f);
    std::pair<float, float> CalcTextSizeWrappedA(const std::string &text, float fontSize = 0.0f, float wrapWidth = 0.0f,
                                                 const std::string &fontPath = "", float lineHeight = 1.0f,
                                                 float letterSpacing = 0.0f);

    /* draw list clip rect (for custom clipping of draw primitives) */
    void PushDrawListClipRect(float minX, float minY, float maxX, float maxY, bool intersectWithCurrent = true);
    void PopDrawListClipRect();

    /* batch property rendering — renders all scalar fields in one call */
    std::vector<PropertyChange> RenderPropertyBatch(const std::vector<PropertyDesc> &descriptors, float labelWidth);
    uint32_t RenderObjectFieldChrome(const std::string &fieldId, const std::string &displayText,
                                     const std::string &typeHint, bool selected, bool clickable, bool hasPicker,
                                     uint64_t pickerTextureId, const std::string &semanticId = "");
    std::vector<ObjectFieldInteraction> RenderMeshRendererInspectorFields(const std::string &meshFieldId,
                                                                          const std::string &meshLabel,
                                                                          const std::string &meshDisplay,
                                                                          const std::vector<std::string> &slotLabels,
                                                                          const std::vector<std::string> &slotDisplays,
                                                                          uint64_t pickerTextureId, float labelWidth);
    MaterialTopInteraction RenderMaterialTop(const std::string &shaderSectionLabel, const std::string &vertexLabel,
                                             const std::string &vertexDisplay, const std::string &fragmentLabel,
                                             const std::string &fragmentDisplay, float shaderLabelWidth,
                                             const std::string &surfaceSectionLabel,
                                             const PropertyBatchPlan *surfacePlan, float surfaceLabelWidth,
                                             uint64_t pickerTextureId, uint64_t previewTextureId,
                                             const std::string &previewUnavailableLabel, bool defaultOpen,
                                             bool readOnly);

  private:
    struct SearchableComboState
    {
        std::array<char, 256> filter{};
        int highlightedItem = -1;
        bool needsSearchFocus = false;
        bool scrollToHighlight = false;
        bool wasOpen = false;
        bool restoreTriggerFocus = false;
    };

    // Infinite-drag helper: warps cursor to opposite screen edge when it
    // reaches the boundary, giving a Unity-style infinite-drag feel.
    void HandleDragCapture(); // call after every ImGui::DragXXX
    void CompensateWarp();    // call before every ImGui::DragXXX to fix delta

    bool m_dragCaptured = false;
    int m_ignoreMouseDeltaFrames = 0; // suppress N frames after SDL warp
    std::unordered_map<std::string, SearchableComboState> m_searchableComboStates;
};

} // namespace infernux
