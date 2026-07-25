#include "InxGUIContext.h"
#include "InxGUISemantics.h"
#include "InxTextLayout.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <cctype>
#include <cfloat>
#include <cmath>
#include <cstring>
#include <function/editor/EditorTheme.h>
#include <function/editor/EditorThemeRegistry.h>
#include <imgui_internal.h>
#include <stdexcept>
#include <type_traits>

namespace infernux
{

float InxGUIContext::s_dpiScale = 1.0f;

float InxGUIContext::GetDpiScale() const
{
    return s_dpiScale;
}

bool InxGUIContext::CanRenderWidgets() const
{
    ImGuiContext *context = ImGui::GetCurrentContext();
    if (context == nullptr || !context->WithinFrameScope || context->CurrentWindowStack.Size <= 0)
        return false;
    ImGuiWindow *stackWindow = context->CurrentWindowStack.back().Window;
    return stackWindow != nullptr && context->CurrentWindow == stackWindow;
}

MaterialTopInteraction
InxGUIContext::RenderMaterialTop(const std::string &shaderSectionLabel, const std::string &vertexLabel,
                                 const std::string &vertexDisplay, const std::string &fragmentLabel,
                                 const std::string &fragmentDisplay, float shaderLabelWidth,
                                 const std::string &surfaceSectionLabel, const PropertyBatchPlan *surfacePlan,
                                 float surfaceLabelWidth, uint64_t pickerTextureId, uint64_t previewTextureId,
                                 const std::string &previewUnavailableLabel, bool defaultOpen, bool readOnly)
{
    MaterialTopInteraction result;
    if (!CanRenderWidgets())
        return result;

    auto themeVec2 = [](const char *name, const ImVec2 &fallback) {
        const auto &values = EditorThemeRegistry::Vec2s();
        const auto it = values.find(name);
        return it != values.end() ? it->second : fallback;
    };
    auto themeColor = [](const char *name, const ImVec4 &fallback) {
        const ImVec4 color = EditorThemeRegistry::Color(name, fallback);
        return std::array<float, 4>{color.x, color.y, color.z, color.w};
    };
    auto renderSectionHeader = [&](const std::string &label) {
        const ImVec2 framePad =
            themeVec2("INSPECTOR_HEADER_SECONDARY_FRAME_PAD", EditorTheme::INSPECTOR_HEADER_SECONDARY_FRAME_PAD);
        const ImVec2 itemSpacing = themeVec2("INSPECTOR_HEADER_ITEM_SPC", EditorTheme::INSPECTOR_HEADER_ITEM_SPC);
        return RenderCompactSectionHeader(
            label, 0, defaultOpen, ImGuiCond_FirstUseEver, false, framePad.x, framePad.y, itemSpacing.x, itemSpacing.y,
            EditorThemeRegistry::Float("INSPECTOR_HEADER_BORDER_SIZE", EditorTheme::INSPECTOR_HEADER_BORDER_SIZE), true,
            EditorThemeRegistry::Float("INSPECTOR_HEADER_SECONDARY_FONT_SCALE",
                                       EditorTheme::INSPECTOR_HEADER_SECONDARY_FONT_SCALE),
            EditorThemeRegistry::Float("INSPECTOR_HEADER_RIGHT_MARGIN", EditorTheme::INSPECTOR_HEADER_RIGHT_MARGIN),
            EditorThemeRegistry::Float("COMPONENT_ICON_SIZE", EditorTheme::COMPONENT_ICON_SIZE),
            themeColor("INSPECTOR_HEADER_SECONDARY", EditorTheme::INSPECTOR_HEADER_SECONDARY),
            themeColor("INSPECTOR_HEADER_SECONDARY_HOVERED", EditorTheme::INSPECTOR_HEADER_SECONDARY_HOVERED),
            themeColor("INSPECTOR_HEADER_SECONDARY_ACTIVE", EditorTheme::INSPECTOR_HEADER_SECONDARY_ACTIVE), false,
            themeColor("TEXT", ImVec4(1, 1, 1, 1)));
    };

    const float totalWidth = (std::max)(1.0f, GetContentRegionAvailWidth());
    const float columnPadding = ImGui::GetStyle().CellPadding.x * 4.0f;
    const float usableWidth = (std::max)(2.0f, totalWidth - columnPadding);
    const float controlsMinWidth = (std::max)(260.0f, (std::max)(shaderLabelWidth, surfaceLabelWidth) + 170.0f);
    const float previewColumnWidth = std::clamp(usableWidth - controlsMinWidth, 10.0f, 200.0f);
    const float controlsColumnWidth = (std::max)(1.0f, usableWidth - previewColumnWidth);
    const int tableFlags = ImGuiTableFlags_SizingFixedFit | ImGuiTableFlags_NoSavedSettings;
    const bool split = BeginTable("##material_top_split", 2, tableFlags, totalWidth);
    if (split) {
        ImGui::TableSetupColumn("##material_controls", ImGuiTableColumnFlags_WidthFixed, controlsColumnWidth);
        ImGui::TableSetupColumn("##material_preview", ImGuiTableColumnFlags_WidthFixed, previewColumnWidth);
        TableNextColumn();
    }
    if (readOnly)
        BeginDisabled();

    auto renderShaderField = [&](const char *fieldId, const std::string &label, const std::string &display,
                                 const char *typeHint, const char *semanticId, uint32_t &flags, bool &popupOpen,
                                 std::string &payload) {
        AlignTextToFramePadding();
        Label(label);
        SameLine(shaderLabelWidth);
        SetNextItemWidth(-1.0f);
        flags = RenderObjectFieldChrome(fieldId, display, typeHint, false, true, true, pickerTextureId, semanticId);
        PushStyleColor(ImGuiCol_DragDropTarget, 0, 0, 0, 0);
        if (BeginDragDropTarget()) {
            AcceptDragDropPayload("SHADER_FILE", &payload);
            EndDragDropTarget();
        }
        PopStyleColor();
        PushID(fieldId);
        popupOpen = ImGui::IsPopupOpen("##obj_picker");
        PopID();
    };

    if (renderSectionHeader(shaderSectionLabel)) {
        renderShaderField("mat_vert", vertexLabel, vertexDisplay, "Vert", "asset.material.shader.vertex",
                          result.vertexFlags, result.vertexPickerOpen, result.vertexPayload);
        renderShaderField("mat_frag", fragmentLabel, fragmentDisplay, "Frag", "asset.material.shader.fragment",
                          result.fragmentFlags, result.fragmentPickerOpen, result.fragmentPayload);
    }

    Separator();
    if (renderSectionHeader(surfaceSectionLabel) && surfacePlan)
        result.surfaceChanges = RenderPropertyBatch(surfacePlan->descriptors, surfaceLabelWidth);

    if (readOnly)
        EndDisabled();

    if (split) {
        TableNextColumn();
        const float availableWidth = (std::max)(1.0f, GetContentRegionAvailWidth());
        const float previewSize = std::clamp(availableWidth, 1.0f, 200.0f);
        if (previewTextureId != 0) {
            const float drawSize = (std::max)(1.0f, (std::min)(availableWidth, previewSize));
            const float offsetY = (std::max)((previewSize - drawSize) * 0.5f, 0.0f);
            if (offsetY > 0.0f)
                Dummy(1.0f, offsetY);
            const float offsetX = (std::max)((availableWidth - drawSize) * 0.5f, 0.0f);
            if (offsetX > 0.0f)
                SetCursorPosX(GetCursorPosX() + offsetX);
            Image(reinterpret_cast<void *>(static_cast<uintptr_t>(previewTextureId)), drawSize, drawSize);
            const float remainingY = (std::max)(previewSize - drawSize - offsetY, 0.0f);
            if (remainingY > 0.0f)
                Dummy(1.0f, remainingY);
        } else {
            const ImVec4 metaText = EditorThemeRegistry::Color("META_TEXT", ImVec4(0.65f, 0.65f, 0.65f, 1.0f));
            PushStyleColor(ImGuiCol_Text, metaText.x, metaText.y, metaText.z, metaText.w);
            Label(previewUnavailableLabel);
            PopStyleColor();
        }
        EndTable();
    }
    Separator();

    result.vertexListPopupOpen = ImGui::IsPopupOpen("mat_vert_popup");
    result.fragmentListPopupOpen = ImGui::IsPopupOpen("mat_frag_popup");
    return result;
}

namespace
{
ImTextureID ToImTextureID(uint64_t textureId)
{
    if constexpr (std::is_pointer_v<ImTextureID>) {
        return (ImTextureID)(static_cast<uintptr_t>(textureId));
    }
    return static_cast<ImTextureID>(textureId);
}

float ResolveFontSize(float fontSize)
{
    return textlayout::ResolveFontSize(fontSize);
}

ImGuiPopupFlags ContextPopupFlagsForMouseButton(int mouseButton)
{
    switch (mouseButton) {
    case 0:
        return ImGuiPopupFlags_MouseButtonLeft;
    case 1:
        return ImGuiPopupFlags_MouseButtonRight;
    case 2:
        return ImGuiPopupFlags_MouseButtonMiddle;
    default:
        return ImGuiPopupFlags_MouseButtonRight;
    }
}

std::string WindowSemanticId(const std::string &name)
{
    const size_t separator = name.rfind("###");
    if (separator == std::string::npos || separator + 3 >= name.size())
        return name;
    return name.substr(separator + 3);
}

void ConstrainNextFloatingWindowToMainViewport(const std::string &name, int flags)
{
    ImGuiViewport *viewport = ImGui::GetMainViewport();
    if (!viewport || (flags & ImGuiWindowFlags_ChildWindow) != 0)
        return;

    ImGuiWindow *window = ImGui::FindWindowByName(name.c_str());
    if (!window)
        return;

#ifdef IMGUI_HAS_DOCK
    if (window->DockNode != nullptr)
        return;
#endif

    const ImVec2 workMin = viewport->WorkPos;
    const ImVec2 workSize = viewport->WorkSize;
    if (workSize.x <= 0.0f || workSize.y <= 0.0f)
        return;

    ImVec2 size = window->SizeFull;
    const ImVec2 constrainedSize(std::min(size.x, workSize.x), std::min(size.y, workSize.y));
    const bool sizeChanged = std::abs(constrainedSize.x - size.x) > 0.5f || std::abs(constrainedSize.y - size.y) > 0.5f;
    if (sizeChanged)
        size = constrainedSize;

    const ImVec2 maxPos(workMin.x + workSize.x - size.x, workMin.y + workSize.y - size.y);
    const ImVec2 constrainedPos(std::clamp(window->Pos.x, workMin.x, maxPos.x),
                                std::clamp(window->Pos.y, workMin.y, maxPos.y));
    const bool positionChanged =
        std::abs(constrainedPos.x - window->Pos.x) > 0.5f || std::abs(constrainedPos.y - window->Pos.y) > 0.5f;
    if (sizeChanged)
        ImGui::SetNextWindowSize(constrainedSize, ImGuiCond_Always);
    if (positionChanged || sizeChanged)
        ImGui::SetNextWindowPos(constrainedPos, ImGuiCond_Always);
}

bool GrabOnlySliderScalar(const char *label, ImGuiDataType dataType, void *data, const void *minimum,
                          const void *maximum, const char *format)
{
    ImGuiWindow *window = ImGui::GetCurrentWindow();
    if (window->SkipItems)
        return false;

    ImGuiContext &g = *GImGui;
    const ImGuiStyle &style = g.Style;
    const ImGuiID id = window->GetID(label);
    const float width = ImGui::CalcItemWidth();
    const ImVec2 labelSize = ImGui::CalcTextSize(label, nullptr, true);
    const ImVec2 cursor = window->DC.CursorPos;
    const ImRect frame(cursor, ImVec2(cursor.x + width, cursor.y + labelSize.y + style.FramePadding.y * 2.0f));
    const ImRect total(
        frame.Min,
        ImVec2(frame.Max.x + (labelSize.x > 0.0f ? style.ItemInnerSpacing.x + labelSize.x : 0.0f), frame.Max.y));

    ImGui::ItemSize(total, style.FramePadding.y);
    if (!ImGui::ItemAdd(total, id, &frame, ImGuiItemFlags_Inputable))
        return false;
    if (!format)
        format = ImGui::DataTypeGetInfo(dataType)->PrintFmt;

    const bool hovered = ImGui::ItemHoverable(frame, id, g.LastItemData.ItemFlags);
    bool tempInputActive = ImGui::TempInputIsActive(id);
    if (!tempInputActive) {
        const bool clicked = hovered && ImGui::IsMouseClicked(0, ImGuiInputFlags_None, id);
        const bool navActivated = g.NavActivateId == id;
        if (clicked)
            ImGui::SetKeyOwner(ImGuiKey_MouseLeft, id);

        bool clickedGrab = false;
        if (clicked) {
            ImRect currentGrab;
            ImGui::SliderBehavior(frame, id, dataType, data, minimum, maximum, format, ImGuiSliderFlags_None,
                                  &currentGrab);
            clickedGrab = currentGrab.Contains(g.IO.MousePos);
        }
        const bool preferInput = navActivated && (g.NavActivateFlags & ImGuiActivateFlags_PreferInput);
        tempInputActive = (clicked && (!clickedGrab || g.IO.KeyCtrl)) || preferInput;

        if (clicked || navActivated)
            std::memcpy(&g.ActiveIdValueOnActivation, data, ImGui::DataTypeGetInfo(dataType)->Size);

        if ((clickedGrab || navActivated) && !tempInputActive) {
            ImGui::SetActiveID(id, window);
            ImGui::SetFocusID(id, window);
            ImGui::FocusWindow(window);
            g.ActiveIdUsingNavDirMask |= (1 << ImGuiDir_Left) | (1 << ImGuiDir_Right);
        }
    }

    if (tempInputActive)
        return ImGui::TempInputScalar(frame, id, label, dataType, data, format, minimum, maximum);

    const ImU32 frameColor = ImGui::GetColorU32(g.ActiveId == id ? ImGuiCol_FrameBgActive
                                                : hovered        ? ImGuiCol_FrameBgHovered
                                                                 : ImGuiCol_FrameBg);
    ImGui::RenderNavCursor(frame, id);
    ImGui::RenderFrame(frame.Min, frame.Max, frameColor, false, style.FrameRounding);
    ImGui::RenderFrameBorder(frame.Min, frame.Max, style.FrameRounding);

    ImRect grab;
    const bool changed =
        ImGui::SliderBehavior(frame, id, dataType, data, minimum, maximum, format, ImGuiSliderFlags_None, &grab);
    if (changed)
        ImGui::MarkItemEdited(id);
    if (grab.Max.x > grab.Min.x)
        window->DrawList->AddRectFilled(
            grab.Min, grab.Max, ImGui::GetColorU32(g.ActiveId == id ? ImGuiCol_SliderGrabActive : ImGuiCol_SliderGrab),
            style.GrabRounding);

    char valueBuffer[64];
    const char *valueEnd =
        valueBuffer + ImGui::DataTypeFormatString(valueBuffer, IM_COUNTOF(valueBuffer), dataType, data, format);
    ImGui::RenderTextClipped(frame.Min, frame.Max, valueBuffer, valueEnd, nullptr, ImVec2(0.5f, 0.5f));
    if (labelSize.x > 0.0f)
        ImGui::RenderText(ImVec2(frame.Max.x + style.ItemInnerSpacing.x, frame.Min.y + style.FramePadding.y), label);
    return changed;
}
} // namespace

/* basic text & labels */
void InxGUIContext::Label(const std::string &text)
{
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(text.c_str());
}

void InxGUIContext::TextWrapped(const std::string &text)
{
    ImGui::TextWrapped("%s", text.c_str());
}

/* buttons / clickables */
bool InxGUIContext::Button(const std::string &label, std::function<void()> onClick, float width, float height)
{
    bool clicked = ImGui::Button(label.c_str(), ImVec2(width, height));
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("button", label);
    if (clicked && onClick)
        onClick();
    return clicked;
}

bool InxGUIContext::RadioButton(const std::string &label, bool active)
{
    const bool clicked = ImGui::RadioButton(label.c_str(), active);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("radio_button", label);
    return clicked;
}

bool InxGUIContext::Selectable(const std::string &label, bool selected, int flags, float width, float height)
{
    const bool clicked = ImGui::Selectable(label.c_str(), selected, flags, ImVec2(width, height));
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("selectable", label);
    return clicked;
}

/* value editors */
void InxGUIContext::Checkbox(const std::string &label, bool *value)
{
    ImGui::Checkbox(label.c_str(), value);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("checkbox", label, true, "", *value);
}

namespace
{

bool DrawInspectorCheckboxSquare(const std::string &label, bool *value, std::string *outVisible)
{
    // Split "Visible##id" so only the square is scaled — label text stays at
    // the ambient inspector font size.
    std::string visible;
    std::string id;
    const size_t hashPos = label.find("##");
    if (hashPos != std::string::npos) {
        visible = label.substr(0, hashPos);
        id = label.substr(hashPos);
    } else {
        visible = label;
        id = "##inx_cb_" + label;
    }
    if (id.empty() || id == "##")
        id = "##inx_cb";

    const float rowY = ImGui::GetCursorPosY();
    const float textH = ImGui::GetTextLineHeight();

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::INSPECTOR_CHECKBOX_FRAME_PAD);
    ImGui::SetWindowFontScale(EditorTheme::INSPECTOR_CHECKBOX_BOX_SCALE);
    const float boxH = ImGui::GetFrameHeight();
    if (boxH < textH)
        ImGui::SetCursorPosY(rowY + (textH - boxH) * 0.5f);

    const bool changed = ImGui::Checkbox(id.c_str(), value);
    ImGui::SetWindowFontScale(1.0f);
    ImGui::PopStyleVar();

    if (!visible.empty()) {
        ImGui::SameLine(0.0f, ImGui::GetStyle().ItemInnerSpacing.x);
        ImGui::SetCursorPosY(rowY);
        ImGui::TextUnformatted(visible.c_str());
    }

    if (outVisible)
        *outVisible = std::move(visible);
    return changed;
}

} // namespace

bool InxGUIContext::CheckboxInspector(const std::string &label, bool *value)
{
    // Semantics are recorded by callers (they own the semantic id).
    return DrawInspectorCheckboxSquare(label, value, nullptr);
}

void InxGUIContext::IntSlider(const std::string &label, int *value, int min, int max)
{
    GrabOnlySliderScalar(label.c_str(), ImGuiDataType_S32, value, &min, &max, "%d");
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("int_slider", label, true, "", std::nullopt, static_cast<double>(*value));
}

void InxGUIContext::FloatSlider(const std::string &label, float *value, float min, float max)
{
    GrabOnlySliderScalar(label.c_str(), ImGuiDataType_Float, value, &min, &max, "%.3f");
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("float_slider", label, true, "", std::nullopt, static_cast<double>(*value));
}

bool InxGUIContext::DragFloat(const std::string &label, float *value, float speed, float min, float max,
                              const char *fmt, float power)
{
    CompensateWarp();
    (void)power;
    const ImGuiSliderFlags flags = min < max ? ImGuiSliderFlags_AlwaysClamp : ImGuiSliderFlags_None;
    bool changed = ImGui::DragFloat(label.c_str(), value, speed, min, max, fmt, flags);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("drag_float", label, true, "", std::nullopt, static_cast<double>(*value));
    HandleDragCapture();
    return changed;
}

bool InxGUIContext::DragInt(const std::string &label, int *value, float speed, int min, int max, const char *fmt)
{
    CompensateWarp();
    const ImGuiSliderFlags flags = min < max ? ImGuiSliderFlags_AlwaysClamp : ImGuiSliderFlags_None;
    bool changed = ImGui::DragInt(label.c_str(), value, speed, min, max, fmt, flags);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("drag_int", label, true, "", std::nullopt, static_cast<double>(*value));
    HandleDragCapture();
    return changed;
}

void InxGUIContext::TextInput(const std::string &label, char *buffer, size_t bufferSize)
{
    ImGui::InputText(label.c_str(), buffer, bufferSize);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("text_input", label, true, "", std::nullopt, std::nullopt, std::string(buffer));
}

void InxGUIContext::TextArea(const std::string &label, char *buffer, size_t bufferSize)
{
    ImGui::InputTextMultiline(label.c_str(), buffer, bufferSize, ImVec2(-FLT_MIN, 100));
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("text_area", label, true, "", std::nullopt, std::nullopt, std::string(buffer));
}

bool InxGUIContext::InputTextWithHint(const std::string &label, const std::string &hint, char *buffer,
                                      size_t bufferSize, int flags)
{
    const bool changed = ImGui::InputTextWithHint(label.c_str(), hint.c_str(), buffer, bufferSize, flags);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("text_input", label.empty() || label.rfind("##", 0) == 0 ? hint : label, true, label,
                           std::nullopt, std::nullopt, std::string(buffer));
    return changed;
}

bool InxGUIContext::InputInt(const std::string &label, int *value, int step, int stepFast, int flags)
{
    const bool changed = ImGui::InputInt(label.c_str(), value, step, stepFast, flags);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("int_input", label, true, "", std::nullopt, static_cast<double>(*value));
    return changed;
}

bool InxGUIContext::InputUInt(const std::string &label, uint32_t *value, uint32_t step, uint32_t stepFast, int flags)
{
    const bool changed = ImGui::InputScalar(label.c_str(), ImGuiDataType_U32, value, &step, &stepFast, "%u", flags);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("uint_input", label, true, "", std::nullopt, static_cast<double>(*value));
    return changed;
}

bool InxGUIContext::InputFloat(const std::string &label, float *value, float step, float stepFast, int flags)
{
    const bool changed =
        ImGui::InputFloat(label.c_str(), value, step, stepFast, "%.3f", static_cast<ImGuiInputTextFlags>(flags));
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("float_input", label, true, "", std::nullopt, static_cast<double>(*value));
    return changed;
}
void InxGUIContext::ColorEdit(const std::string &label, float color[4], bool hdr)
{
    const ImGuiColorEditFlags flags = hdr ? ImGuiColorEditFlags_HDR | ImGuiColorEditFlags_Float |
                                                ImGuiColorEditFlags_DisplayRGB | ImGuiColorEditFlags_InputRGB
                                          : ImGuiColorEditFlags_None;
    ImGui::ColorEdit4(label.c_str(), color, flags);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("color_edit", label);
}

bool InxGUIContext::ColorPicker(const std::string &label, float color[4], int flags)
{
    const bool changed = ImGui::ColorPicker4(label.c_str(), color, static_cast<ImGuiColorEditFlags>(flags));
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("color_picker", label);
    return changed;
}

// Unity-style helper: label on the left, DragFloatN on the right.
static void LabeledDragFloatN(InxGUIContext &ctx, const char *label, float *value, int components, float speed,
                              float labelWidth = 0.0f, const std::string &axisSemanticBase = "")
{
    if (labelWidth <= 0.0f)
        labelWidth = ImGui::CalcTextSize(label).x + 20.0f;
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(label);
    ImGui::SameLine(labelWidth);
    float avail = ImGui::GetContentRegionAvail().x;
    ImGui::SetNextItemWidth(avail);
    std::string hiddenLabel = std::string("##") + label;

    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    if (captureSemantics && components >= 2 && components <= 4 && !axisSemanticBase.empty()) {
        // Match ImGui::DragFloatN's layout so each actual field has a stable,
        // focusable semantic target instead of exposing only the aggregate row.
        static constexpr const char *kAxisNames[] = {"x", "y", "z", "w"};
        static constexpr const char *kAxisLabels[] = {"X", "Y", "Z", "W"};
        ImGui::BeginGroup();
        ImGui::PushID(hiddenLabel.c_str());
        ImGui::PushMultiItemsWidths(components, ImGui::CalcItemWidth());
        for (int axis = 0; axis < components; ++axis) {
            ImGui::PushID(axis);
            if (axis > 0)
                ImGui::SameLine(0.0f, ImGui::GetStyle().ItemInnerSpacing.x);
            ImGui::DragFloat("", &value[axis], speed);
            ctx.RecordSemanticItem("vector_axis", kAxisLabels[axis], true, axisSemanticBase + "." + kAxisNames[axis],
                                   std::nullopt, static_cast<double>(value[axis]));
            ImGui::PopID();
            ImGui::PopItemWidth();
        }
        ImGui::PopID();
        ImGui::EndGroup();
    } else {
        switch (components) {
        case 2:
            ImGui::DragFloat2(hiddenLabel.c_str(), value, speed);
            break;
        case 3:
            ImGui::DragFloat3(hiddenLabel.c_str(), value, speed);
            break;
        case 4:
            ImGui::DragFloat4(hiddenLabel.c_str(), value, speed);
            break;
        default:
            ImGui::DragFloat(hiddenLabel.c_str(), value, speed);
            break;
        }
    }
    if (captureSemantics)
        ctx.RecordSemanticItem("vector", label);
}

void InxGUIContext::Vector2Control(const std::string &label, float value[2], float speed, float labelWidth,
                                   const std::string &axisSemanticBase)
{
    CompensateWarp();
    LabeledDragFloatN(*this, label.c_str(), value, 2, speed, labelWidth, axisSemanticBase);
    HandleDragCapture();
}

void InxGUIContext::Vector3Control(const std::string &label, float value[3], float speed, float labelWidth,
                                   const std::string &axisSemanticBase)
{
    CompensateWarp();
    LabeledDragFloatN(*this, label.c_str(), value, 3, speed, labelWidth, axisSemanticBase);
    HandleDragCapture();
}

void InxGUIContext::Vector4Control(const std::string &label, float value[4], float speed, float labelWidth)
{
    CompensateWarp();
    LabeledDragFloatN(*this, label.c_str(), value, 4, speed, labelWidth);
    HandleDragCapture();
}

/* combo & lists */
bool InxGUIContext::Combo(const std::string &label, int *currentItem, const std::vector<std::string> &items,
                          int popupMaxHeightInItems)
{
    if (currentItem == nullptr)
        return false;

    ImGuiContext &context = *ImGui::GetCurrentContext();
    if (popupMaxHeightInItems >= 0 &&
        (context.NextWindowData.HasFlags & ImGuiNextWindowDataFlags_HasSizeConstraint) == 0) {
        const float popupMaxHeight =
            (context.FontSize + context.Style.ItemSpacing.y) * static_cast<float>(popupMaxHeightInItems) -
            context.Style.ItemSpacing.y + context.Style.WindowPadding.y * 2.0f;
        ImGui::SetNextWindowSizeConstraints(ImVec2(0.0f, 0.0f), ImVec2(FLT_MAX, std::max(0.0f, popupMaxHeight)));
    }

    const bool hasSelection = *currentItem >= 0 && *currentItem < static_cast<int>(items.size());
    const std::string preview = hasSelection ? items[*currentItem] : std::string{};
    const bool open = ImGui::BeginCombo(label.c_str(), preview.c_str());
    const ImGuiLastItemData triggerItem = context.LastItemData;

    const size_t hiddenMarker = label.find("##");
    const std::string semanticBase =
        hiddenMarker != std::string::npos && hiddenMarker + 2 < label.size() ? label.substr(hiddenMarker + 2) : label;
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    if (captureSemantics)
        RecordSemanticItem("combo", preview.empty() ? label : preview, true, semanticBase, std::nullopt, std::nullopt,
                           preview);
    if (!open)
        return false;

    if (captureSemantics)
        RecordSemanticWindow("combo_popup", preview.empty() ? label : preview, semanticBase);
    bool changed = false;
    for (int index = 0; index < static_cast<int>(items.size()); ++index) {
        ImGui::PushID(index);
        const bool selected = index == *currentItem;
        if (ImGui::Selectable(items[index].c_str(), selected)) {
            *currentItem = index;
            changed = true;
        }
        if (captureSemantics)
            RecordSemanticItem("combo_option", items[index], true, semanticBase + ":option:" + std::to_string(index));
        if (selected)
            ImGui::SetItemDefaultFocus();
        ImGui::PopID();
    }
    ImGui::EndCombo();

    // Python panels commonly add a domain-specific semantic alias immediately
    // after combo() returns. Keep that alias attached to the trigger, rather
    // than to the final selectable rendered inside the popup.
    context.LastItemData = triggerItem;
    return changed;
}

bool InxGUIContext::ListBox(const std::string &label, int *currentItem, const std::vector<std::string> &items,
                            int heightInItems)
{
    std::vector<const char *> cstrs;
    cstrs.reserve(items.size());
    for (const auto &s : items)
        cstrs.push_back(s.c_str());
    const bool changed =
        ImGui::ListBox(label.c_str(), currentItem, cstrs.data(), static_cast<int>(cstrs.size()), heightInItems);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("list_box", label);
    return changed;
}

/* progress & indicators */
void InxGUIContext::ProgressBar(float fraction, float width, float height, const std::string &overlay)
{
    ImGui::ProgressBar(fraction, ImVec2(width, height), overlay.c_str());
}

/* layout helpers */
void InxGUIContext::BeginGroup(const std::string &name)
{
    ImGui::BeginGroup();
    if (!name.empty())
        ImGui::TextUnformatted(name.c_str());
}

void InxGUIContext::EndGroup()
{
    ImGui::EndGroup();
}

void InxGUIContext::SameLine(float offsetFromStartX, float spacing)
{
    ImGui::SameLine(offsetFromStartX, spacing);
}

void InxGUIContext::AlignTextToFramePadding()
{
    ImGui::AlignTextToFramePadding();
}

void InxGUIContext::SetScrollHereY(float centerYRatio)
{
    ImGui::SetScrollHereY(centerYRatio);
}

float InxGUIContext::GetScrollY()
{
    return ImGui::GetScrollY();
}

float InxGUIContext::GetScrollMaxY()
{
    return ImGui::GetScrollMaxY();
}

void InxGUIContext::SetScrollX(float scrollX)
{
    ImGui::SetScrollX(scrollX);
}

void InxGUIContext::SetScrollY(float scrollY)
{
    ImGui::SetScrollY(scrollY);
}

void InxGUIContext::CloseCurrentPopup()
{
    ImGui::CloseCurrentPopup();
}

void InxGUIContext::Separator()
{
    ImGui::Separator();
}

void InxGUIContext::Spacing()
{
    ImGui::Spacing();
}

void InxGUIContext::Dummy(float width, float height)
{
    ImGui::Dummy(ImVec2(width, height));
}

void InxGUIContext::NewLine()
{
    ImGui::NewLine();
}

/* tree & collapsing */
bool InxGUIContext::TreeNode(const std::string &label)
{
    const bool open = ImGui::TreeNode(label.c_str());
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("tree_node", label);
    return open;
}

bool InxGUIContext::TreeNodeEx(const std::string &label, int flags)
{
    const bool open = ImGui::TreeNodeEx(label.c_str(), static_cast<ImGuiTreeNodeFlags>(flags));
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("tree_node", label);
    return open;
}

void InxGUIContext::TreePop()
{
    ImGui::TreePop();
}

void InxGUIContext::SetNextItemOpen(bool is_open, int cond)
{
    ImGui::SetNextItemOpen(is_open, cond);
}

void InxGUIContext::SetNextItemAllowOverlap()
{
    ImGui::SetNextItemAllowOverlap();
}

bool InxGUIContext::CollapsingHeader(const std::string &label)
{
    const bool open = ImGui::CollapsingHeader(label.c_str());
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("collapsing_header", label);
    return open;
}

bool InxGUIContext::RenderCompactSectionHeader(
    const std::string &label, uint64_t iconId, bool defaultOpen, int openCondition, bool allowOverlap, float framePadX,
    float framePadY, float itemSpacingX, float itemSpacingY, float borderSize, bool zeroIndent, float fontScale,
    float rightMargin, float iconSize, const std::array<float, 4> &headerColor, const std::array<float, 4> &hoverColor,
    const std::array<float, 4> &activeColor, bool useTextColor, const std::array<float, 4> &textColor)
{
    if (defaultOpen)
        ImGui::SetNextItemOpen(true, openCondition);
    if (allowOverlap)
        ImGui::SetNextItemAllowOverlap();

    auto asColor = [](const std::array<float, 4> &color) { return ImVec4(color[0], color[1], color[2], color[3]); };
    ImGui::PushStyleColor(ImGuiCol_Header, asColor(headerColor));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, asColor(hoverColor));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, asColor(activeColor));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(framePadX, framePadY));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(itemSpacingX, itemSpacingY));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, borderSize);
    if (zeroIndent)
        ImGui::PushStyleVar(ImGuiStyleVar_IndentSpacing, 0.0f);
    ImGui::SetWindowFontScale(fontScale);
    if (useTextColor)
        ImGui::PushStyleColor(ImGuiCol_Text, asColor(textColor));

    if (iconId != 0) {
        ImGui::Image(static_cast<ImTextureID>(iconId), ImVec2(iconSize, iconSize));
        ImGui::SameLine();
    }

    const float maxX =
        ImGui::GetWindowPos().x + ImGui::GetCursorPos().x + ImGui::GetContentRegionAvail().x - rightMargin;
    ImGui::GetWindowDrawList()->PushClipRect(ImVec2(0.0f, 0.0f), ImVec2(maxX, 1e7f), true);
    const bool open = ImGui::CollapsingHeader(label.c_str());
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("collapsing_header", label);
    ImGui::GetWindowDrawList()->PopClipRect();

    if (useTextColor)
        ImGui::PopStyleColor();
    ImGui::SetWindowFontScale(1.0f);
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(zeroIndent ? 4 : 3);
    return open;
}

bool InxGUIContext::IsItemClicked(int mouseButton)
{
    return ImGui::IsItemClicked(static_cast<ImGuiMouseButton>(mouseButton));
}

/* tab bars */
bool InxGUIContext::BeginTabBar(const std::string &id)
{
    return ImGui::BeginTabBar(id.c_str());
}

void InxGUIContext::EndTabBar()
{
    ImGui::EndTabBar();
}

bool InxGUIContext::BeginTabItem(const std::string &label, bool *open)
{
    const bool visible = ImGui::BeginTabItem(label.c_str(), open);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("tab", label, open == nullptr || *open);
    return visible;
}

void InxGUIContext::EndTabItem()
{
    ImGui::EndTabItem();
}

namespace
{
// ─── Unity-style popup / menu chrome ─────────────────────────────────────
// Context menus and utility popups used to inherit the compact global
// spacing, producing cramped menus with a thin hover band. Every popup
// opened through InxGUIContext gets consistent, em-based (DPI-proof)
// padding, taller rows with contiguous hover highlight, and stronger
// hover/active feedback colors. One stack entry per open popup/menu window
// keeps Begin/End pairs balanced across nesting.
struct PopupContentStyle
{
    int styleVars = 0;
    int styleColors = 0;
};
std::vector<PopupContentStyle> s_popupContentStyleStack;

// Pushed *before* Begin so the popup window latches the padding.
// Returns the number of style vars to pop right after Begin.
int PushPopupWindowChrome()
{
    const float em = ImGui::GetFontSize();
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(em * 0.45f, em * 0.5f));
    return 1;
}

// Pushed when the popup is open; popped in the matching End call.
void PushPopupContentStyle()
{
    PopupContentStyle entry;
    const float em = ImGui::GetFontSize();
    // Taller rows: Selectable/MenuItem extend their hover band across the
    // ItemSpacing gap, so a larger vertical spacing yields a contiguous,
    // Unity-like highlight instead of thin strips.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(em * 0.5f, em * 0.5f));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(em * 0.5f, em * 0.3f));
    entry.styleVars = 2;

    // Clearer hover / click feedback, derived from the active theme so any
    // palette keeps its identity.
    auto lift = [](ImVec4 color, float amount) {
        color.x = color.x + (1.0f - color.x) * amount;
        color.y = color.y + (1.0f - color.y) * amount;
        color.z = color.z + (1.0f - color.z) * amount;
        color.w = std::max(color.w, 1.0f);
        return color;
    };
    const ImVec4 hovered = ImGui::GetStyleColorVec4(ImGuiCol_HeaderHovered);
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, lift(hovered, 0.06f));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, lift(hovered, 0.16f));
    entry.styleColors = 2;

    s_popupContentStyleStack.push_back(entry);
}

void PopPopupContentStyle()
{
    if (s_popupContentStyleStack.empty())
        return;
    const PopupContentStyle entry = s_popupContentStyleStack.back();
    s_popupContentStyleStack.pop_back();
    ImGui::PopStyleColor(entry.styleColors);
    ImGui::PopStyleVar(entry.styleVars);
}

// Context menus get a Unity-like minimum width so entries and shortcuts
// are not clipped into a narrow strip.
void ApplyContextMenuMinWidth()
{
    const float em = ImGui::GetFontSize();
    ImGui::SetNextWindowSizeConstraints(ImVec2(em * 14.0f, 0.0f), ImVec2(FLT_MAX, FLT_MAX));
}

} // namespace

/* main menu / menus */
bool InxGUIContext::BeginMainMenuBar()
{
    return ImGui::BeginMainMenuBar();
}

void InxGUIContext::EndMainMenuBar()
{
    ImGui::EndMainMenuBar();
}

bool InxGUIContext::BeginMenu(const std::string &label, bool enabled, const std::string &semanticId)
{
    // Nested BeginMenu windows must NOT inherit the root context-menu
    // WindowPadding push. ImGui places submenus with a 1px overlap against
    // the parent, so an inflated WindowPadding makes 1st/2nd/3rd-level menu
    // edges visually collide. Content style (row spacing / hover colours)
    // is still applied once the submenu is open.
    const bool open = ImGui::BeginMenu(label.c_str(), enabled);
    if (open)
        PushPopupContentStyle();
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("menu", label, enabled, semanticId, open);
    return open;
}

void InxGUIContext::EndMenu()
{
    PopPopupContentStyle();
    ImGui::EndMenu();
}

bool InxGUIContext::MenuItem(const std::string &label, const std::string &shortcut, bool selected, bool enabled)
{
    const bool clicked =
        ImGui::MenuItem(label.c_str(), shortcut.empty() ? nullptr : shortcut.c_str(), selected, enabled);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("menu_item", label, enabled);
    return clicked;
}

/* child & windows */
bool InxGUIContext::BeginChild(const std::string &id, float width, float height, bool border, int flags)
{
    return ImGui::BeginChild(id.c_str(), ImVec2(width, height), border ? ImGuiChildFlags_Borders : 0,
                             static_cast<ImGuiWindowFlags>(flags));
}

void InxGUIContext::EndChild()
{
    ImGui::EndChild();
}

/* popups & tooltips */

void InxGUIContext::OpenPopup(const std::string &id)
{
    ImGui::OpenPopup(id.c_str());
}

bool InxGUIContext::BeginPopup(const std::string &id)
{
    // Panels that build context menus manually (OpenPopup + BeginPopup, e.g.
    // "##HierarchyItemContext") name them accordingly; give those the same
    // minimum width as the BeginPopupContext* variants.
    if (id.find("Context") != std::string::npos || id.find("context") != std::string::npos)
        ApplyContextMenuMinWidth();
    const int chromeVars = PushPopupWindowChrome();
    const bool open = ImGui::BeginPopup(id.c_str());
    ImGui::PopStyleVar(chromeVars);
    if (open) {
        PushPopupContentStyle();
        RecordSemanticWindow("popup", id, id);
    }
    return open;
}

bool InxGUIContext::BeginPopupModal(const std::string &title, int flags)
{
    // Detached editor panels are separate native child windows of the main
    // viewport. A modal merged into the main viewport therefore cannot cover
    // them, regardless of ImGui's internal display order. Give every modal its
    // own short-lived platform viewport parented to the main editor window.
    ImGuiWindowClass modalClass{};
    modalClass.ClassId = ImHashStr("Infernux.GlobalModal");
    modalClass.ParentViewportId = ImGui::GetMainViewport()->ID;
    modalClass.ViewportFlagsOverrideSet = ImGuiViewportFlags_NoAutoMerge | ImGuiViewportFlags_NoTaskBarIcon |
                                          ImGuiViewportFlags_NoDecoration | ImGuiViewportFlags_TopMost;
    ImGui::SetNextWindowClass(&modalClass);

    const bool open = ImGui::BeginPopupModal(title.c_str(), nullptr, static_cast<ImGuiWindowFlags>(flags));
    if (open) {
        // Modals are closed through the shared EndPopup wrapper, which pops
        // one content-style entry — keep the stack balanced.
        PushPopupContentStyle();
        ImGuiWindow *window = ImGui::GetCurrentWindow();
        // Docking preserves the display order of undocked windows even when
        // they are submitted before this popup. A modal must remain above
        // every floating editor window for its entire lifetime.
        ImGui::BringWindowToDisplayFront(window);
        ImGui::BringWindowToFocusFront(window->RootWindow);

        // Keep the modal as the sole ImGui focus owner for its whole lifetime.
        // A detached panel may otherwise regain focus from the title-bar close
        // interaction on the following frame and cover the confirmation.
        ImGui::FocusWindow(window);

        // A new platform viewport is created after the first ImGui frame. The
        // old Appearing-only check therefore ran too early and never raised the
        // native modal. Remember the request until the viewport actually exists.
        static const ImGuiID nativeRaiseState = ImHashStr("Infernux.GlobalModal.NativeRaiseComplete");
        if (window->Appearing)
            window->StateStorage.SetBool(nativeRaiseState, false);
        ImGuiViewport *viewport = window->Viewport;
        if (viewport != nullptr && viewport->PlatformWindowCreated &&
            !window->StateStorage.GetBool(nativeRaiseState, false)) {
            ImGuiPlatformIO &platformIO = ImGui::GetPlatformIO();
            if (platformIO.Platform_SetWindowFocus != nullptr)
                platformIO.Platform_SetWindowFocus(viewport);
            window->StateStorage.SetBool(nativeRaiseState, true);
        }
        RecordSemanticWindow("modal", title, title);
    }
    return open;
}

bool InxGUIContext::BeginPopupContextItem(const std::string &id, int mouseButton)
{
    ApplyContextMenuMinWidth();
    const int chromeVars = PushPopupWindowChrome();
    const bool open =
        ImGui::BeginPopupContextItem(id.empty() ? nullptr : id.c_str(), ContextPopupFlagsForMouseButton(mouseButton));
    ImGui::PopStyleVar(chromeVars);
    if (open) {
        PushPopupContentStyle();
        RecordSemanticWindow("context_menu", id, id);
    }
    return open;
}

bool InxGUIContext::BeginPopupContextWindow(const std::string &id, int mouseButton, bool noOpenOverItems)
{
    ImGuiPopupFlags flags = ContextPopupFlagsForMouseButton(mouseButton);
    if (noOpenOverItems)
        flags |= ImGuiPopupFlags_NoOpenOverItems;
    ApplyContextMenuMinWidth();
    const int chromeVars = PushPopupWindowChrome();
    const bool open = ImGui::BeginPopupContextWindow(id.empty() ? nullptr : id.c_str(), flags);
    ImGui::PopStyleVar(chromeVars);
    if (open) {
        PushPopupContentStyle();
        RecordSemanticWindow("context_menu", id, id);
    }
    return open;
}

void InxGUIContext::EndPopup()
{
    PopPopupContentStyle();
    ImGui::EndPopup();
}

void InxGUIContext::BeginTooltip()
{
    ImGui::BeginTooltip();
}

void InxGUIContext::EndTooltip()
{
    ImGui::EndTooltip();
}

void InxGUIContext::SetTooltip(const std::string &text)
{
    ImGui::SetTooltip("%s", text.c_str());
}

/* images */
void InxGUIContext::Image(void *textureId, float width, float height, float uv0_x, float uv0_y, float uv1_x,
                          float uv1_y)
{
    if (!textureId)
        return;
    ImGui::Image(reinterpret_cast<ImTextureID>(textureId), ImVec2(width, height), ImVec2(uv0_x, uv0_y),
                 ImVec2(uv1_x, uv1_y));
}

bool InxGUIContext::ImageButton(const std::string &id, void *textureId, float width, float height, float uv0_x,
                                float uv0_y, float uv1_x, float uv1_y)
{
    if (!textureId)
        return false;
    const bool clicked = ImGui::ImageButton(id.c_str(), reinterpret_cast<ImTextureID>(textureId), ImVec2(width, height),
                                            ImVec2(uv0_x, uv0_y), ImVec2(uv1_x, uv1_y));
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("image_button", id, true, id);
    return clicked;
}

/* tables */
bool InxGUIContext::BeginTable(const std::string &id, int columns, int flags, float innerWidth)
{
    return ImGui::BeginTable(id.c_str(), columns, flags, ImVec2(innerWidth, 0));
}

void InxGUIContext::EndTable()
{
    ImGui::EndTable();
}

void InxGUIContext::TableSetupColumn(const std::string &label, int flags, float initWidthOrWeight, int userID)
{
    ImGui::TableSetupColumn(label.c_str(), flags, initWidthOrWeight, userID);
}

void InxGUIContext::TableHeadersRow()
{
    ImGui::TableHeadersRow();
}

void InxGUIContext::TableNextRow()
{
    ImGui::TableNextRow();
}

void InxGUIContext::TableSetColumnIndex(int columnIndex)
{
    ImGui::TableSetColumnIndex(columnIndex);
}

bool InxGUIContext::TableNextColumn()
{
    return ImGui::TableNextColumn();
}

/* misc helpers */
bool InxGUIContext::CheckboxFlags(const std::string &label, unsigned int *flags, unsigned int flagValue)
{
    const bool changed = ImGui::CheckboxFlags(label.c_str(), flags, flagValue);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("checkbox", label, true, "", (*flags & flagValue) == flagValue);
    return changed;
}

void InxGUIContext::SetNextItemWidth(float width)
{
    ImGui::SetNextItemWidth(width);
}

void InxGUIContext::SetNextWindowSize(float width, float height, int cond)
{
    ImGui::SetNextWindowSize(ImVec2(width, height), static_cast<ImGuiCond>(cond));
}

void InxGUIContext::SetNextWindowPos(float x, float y, int cond, float pivot_x, float pivot_y)
{
    ImGui::SetNextWindowPos(ImVec2(x, y), static_cast<ImGuiCond>(cond), ImVec2(pivot_x, pivot_y));
}

void InxGUIContext::SetNextWindowFocus()
{
    ImGui::SetNextWindowFocus();
}

void InxGUIContext::SetWindowFocus()
{
    ImGui::SetWindowFocus();
}

bool InxGUIContext::BeginWindow(const std::string &name, bool *open, int flags)
{
    ConstrainNextFloatingWindowToMainViewport(name, flags);
    const bool visible = ImGui::Begin(name.c_str(), open, flags);
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    if (captureSemantics)
        RecordSemanticWindow("window", name, WindowSemanticId(name));
    if (captureSemantics && open != nullptr)
        InxGUISemantics::RecordCurrentWindowCloseButton(WindowSemanticId(name) + ".close");
    return visible;
}

void InxGUIContext::EndWindow()
{
    ImGui::End();
}

/* layout query */
float InxGUIContext::GetContentRegionAvailWidth()
{
    return ImGui::GetContentRegionAvail().x;
}

float InxGUIContext::GetContentRegionAvailHeight()
{
    return ImGui::GetContentRegionAvail().y;
}

float InxGUIContext::GetCursorPosX()
{
    return ImGui::GetCursorPosX();
}

float InxGUIContext::GetCursorPosY()
{
    return ImGui::GetCursorPosY();
}

bool InxGUIContext::IsVirtualizedRegionVisible(float height)
{
    if (height <= 0.0f)
        return true;

    // Visibility must be local to this region. Expanding every virtualized
    // Inspector body while any item is held or any popup is open changes the
    // child window's content height between mouse-down and mouse-up. At the
    // bottom of the Inspector that moves both the scroll position and the
    // item under the cursor. The active body and its popup are already inside
    // the visible clip rect when interaction begins, so they remain rendered
    // without a global interaction bypass.
    const float width = (std::max)(ImGui::GetContentRegionAvail().x, 1.0f);
    return ImGui::IsRectVisible(ImVec2(width, height));
}

void InxGUIContext::SetCursorPosX(float x)
{
    ImGui::SetCursorPosX(x);
}

void InxGUIContext::SetCursorPosY(float y)
{
    ImGui::SetCursorPosY(y);
}

float InxGUIContext::GetWindowPosX()
{
    return ImGui::GetWindowPos().x;
}

float InxGUIContext::GetWindowPosY()
{
    return ImGui::GetWindowPos().y;
}

float InxGUIContext::CalcTextWidth(const std::string &text)
{
    return ImGui::CalcTextSize(text.c_str()).x;
}

float InxGUIContext::GetWindowWidth()
{
    return ImGui::GetWindowWidth();
}

float InxGUIContext::GetItemRectMinX()
{
    return ImGui::GetItemRectMin().x;
}

float InxGUIContext::GetItemRectMinY()
{
    return ImGui::GetItemRectMin().y;
}

float InxGUIContext::GetItemRectMaxX()
{
    return ImGui::GetItemRectMax().x;
}

float InxGUIContext::GetItemRectMaxY()
{
    return ImGui::GetItemRectMax().y;
}

void InxGUIContext::RecordSemanticItem(const std::string &kind, const std::string &label, bool enabled,
                                       const std::string &semanticId, std::optional<bool> boolValue,
                                       std::optional<double> numericValue, std::optional<std::string> stringValue)
{
    InxGUISemantics::RecordLastItem(kind, label, enabled, semanticId, boolValue, numericValue, stringValue);
}

void InxGUIContext::RecordSemanticRect(const std::string &kind, const std::string &label, float x, float y, float width,
                                       float height, bool enabled, const std::string &semanticId)
{
    InxGUISemantics::RecordRect(kind, label, x, y, width, height, enabled, semanticId);
}

void InxGUIContext::RecordSemanticWindow(const std::string &kind, const std::string &label,
                                         const std::string &semanticId)
{
    InxGUISemantics::RecordCurrentWindow(kind, label, semanticId);
}

/* invisible button (for splitter) */
bool InxGUIContext::InvisibleButton(const std::string &id, float width, float height)
{
    const bool clicked = ImGui::InvisibleButton(id.c_str(), ImVec2(width, height));
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("invisible_button", id, true, id);
    return clicked;
}

bool InxGUIContext::IsItemActive()
{
    return ImGui::IsItemActive();
}

bool InxGUIContext::IsAnyItemActive()
{
    return ImGui::IsAnyItemActive();
}

bool InxGUIContext::IsItemHovered()
{
    return ImGui::IsItemHovered();
}

int InxGUIContext::SearchableCombo(const std::string &id, int currentItem, const std::vector<std::string> &items,
                                   float width, int maxVisibleItems, const std::string &searchHint,
                                   const std::string &emptyText)
{
    if (id.empty())
        throw std::invalid_argument("SearchableCombo id cannot be empty");
    if (maxVisibleItems <= 0)
        throw std::invalid_argument("SearchableCombo maxVisibleItems must be positive");

    auto &state = m_searchableComboStates[id];
    const int safeCurrent = currentItem >= 0 && currentItem < static_cast<int>(items.size()) ? currentItem : -1;
    const std::string display = safeCurrent >= 0 ? items[safeCurrent] : std::string{};
    int result = currentItem;

    ImGui::PushID(id.c_str());
    const bool triggerClicked =
        ImGui::Button((display + "##trigger").c_str(), ImVec2(width > 0.0f ? width : 0.0f, 0.0f));
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    if (captureSemantics)
        RecordSemanticItem("searchable_combo", display, true, id);
    if (triggerClicked) {
        state.filter.fill('\0');
        state.highlightedItem = safeCurrent;
        state.needsSearchFocus = true;
        state.scrollToHighlight = true;
        ImGui::OpenPopup("##popup");
    }
    if (state.restoreTriggerFocus) {
        ImGui::SetItemDefaultFocus();
        state.restoreTriggerFocus = false;
    }

    const float popupWidth = std::max(width, 220.0f * s_dpiScale);
    const float rowHeight = ImGui::GetTextLineHeightWithSpacing();
    const float listHeight = rowHeight * static_cast<float>(maxVisibleItems) + ImGui::GetStyle().WindowPadding.y * 2.0f;
    ImGui::SetNextWindowSizeConstraints(ImVec2(popupWidth, 0.0f), ImVec2(popupWidth, FLT_MAX));
    if (ImGui::BeginPopup("##popup")) {
        if (captureSemantics)
            RecordSemanticWindow("combo_popup", id, id);
        state.wasOpen = true;
        if (state.needsSearchFocus) {
            ImGui::SetKeyboardFocusHere();
            state.needsSearchFocus = false;
        }

        ImGui::SetNextItemWidth(-FLT_MIN);
        const std::string previousFilter(state.filter.data());
        const bool submitted =
            ImGui::InputTextWithHint("##search", searchHint.c_str(), state.filter.data(), state.filter.size(),
                                     ImGuiInputTextFlags_AutoSelectAll | ImGuiInputTextFlags_EnterReturnsTrue);
        if (captureSemantics)
            RecordSemanticItem("text_input", searchHint, true, id + ":search");
        const bool searchFocused = ImGui::IsItemFocused();
        const bool filterChanged = previousFilter != state.filter.data();

        std::string filterLower(state.filter.data());
        std::transform(filterLower.begin(), filterLower.end(), filterLower.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        std::vector<int> filtered;
        filtered.reserve(items.size());
        for (int index = 0; index < static_cast<int>(items.size()); ++index) {
            std::string itemLower = items[index];
            std::transform(itemLower.begin(), itemLower.end(), itemLower.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            if (filterLower.empty() || itemLower.find(filterLower) != std::string::npos)
                filtered.push_back(index);
        }

        auto highlighted = std::find(filtered.begin(), filtered.end(), state.highlightedItem);
        if (filterChanged || highlighted == filtered.end()) {
            auto current = std::find(filtered.begin(), filtered.end(), safeCurrent);
            state.highlightedItem = current != filtered.end() ? *current : (filtered.empty() ? -1 : filtered.front());
            state.scrollToHighlight = true;
            highlighted = std::find(filtered.begin(), filtered.end(), state.highlightedItem);
        }

        if (searchFocused && !filtered.empty()) {
            int position = highlighted == filtered.end() ? 0 : static_cast<int>(highlighted - filtered.begin());
            if (ImGui::IsKeyPressed(ImGuiKey_DownArrow)) {
                position = (position + 1) % static_cast<int>(filtered.size());
                state.highlightedItem = filtered[position];
                state.scrollToHighlight = true;
            } else if (ImGui::IsKeyPressed(ImGuiKey_UpArrow)) {
                position = (position + static_cast<int>(filtered.size()) - 1) % static_cast<int>(filtered.size());
                state.highlightedItem = filtered[position];
                state.scrollToHighlight = true;
            }
        }

        bool closePopup = false;
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            closePopup = true;
        } else if (submitted && state.highlightedItem >= 0) {
            result = state.highlightedItem;
            closePopup = true;
        }

        ImGui::Separator();
        if (ImGui::BeginChild("##results", ImVec2(0.0f, listHeight), ImGuiChildFlags_None)) {
            if (filtered.empty()) {
                ImGui::TextDisabled("%s", emptyText.c_str());
            } else {
                for (int index : filtered) {
                    const bool highlightedItem = index == state.highlightedItem;
                    ImGui::PushID(index);
                    const bool selected = ImGui::Selectable((items[index] + "##item").c_str(), highlightedItem);
                    if (captureSemantics)
                        RecordSemanticItem("combo_option", items[index], true, id + ":option:" + std::to_string(index));
                    if (selected) {
                        result = index;
                        closePopup = true;
                    }
                    if (ImGui::IsItemHovered())
                        state.highlightedItem = index;
                    if (highlightedItem && state.scrollToHighlight) {
                        ImGui::SetScrollHereY(0.5f);
                        state.scrollToHighlight = false;
                    }
                    ImGui::PopID();
                }
            }
        }
        ImGui::EndChild();

        if (closePopup) {
            ImGui::CloseCurrentPopup();
            state.wasOpen = false;
            state.restoreTriggerFocus = true;
        }
        ImGui::EndPopup();
    } else if (state.wasOpen) {
        state.wasOpen = false;
        state.restoreTriggerFocus = true;
    }
    ImGui::PopID();
    return result;
}

bool InxGUIContext::IsItemFocused()
{
    return ImGui::IsItemFocused();
}

/* focus & activation */
void InxGUIContext::SetKeyboardFocusHere(int offset)
{
    ImGui::SetKeyboardFocusHere(offset);
}

bool InxGUIContext::IsItemDeactivated()
{
    return ImGui::IsItemDeactivated();
}

bool InxGUIContext::IsItemDeactivatedAfterEdit()
{
    return ImGui::IsItemDeactivatedAfterEdit();
}

float InxGUIContext::GetMouseDragDeltaY(int button)
{
    // Use 0.0f threshold for immediate response (no lock threshold)
    return ImGui::GetMouseDragDelta(button, 0.0f).y;
}

void InxGUIContext::ResetMouseDragDelta(int button)
{
    ImGui::ResetMouseDragDelta(button);
}

/* ID stack */
void InxGUIContext::PushID(int id)
{
    ImGui::PushID(id);
}

void InxGUIContext::PushID(const std::string &id)
{
    ImGui::PushID(id.c_str());
}

void InxGUIContext::PopID()
{
    ImGui::PopID();
}

void InxGUIContext::PushStyleColor(int idx, float r, float g, float b, float a)
{
    ImGui::PushStyleColor(static_cast<ImGuiCol_>(idx), ImVec4(r, g, b, a));
}

void InxGUIContext::PopStyleColor(int count)
{
    ImGui::PopStyleColor(count);
}

void InxGUIContext::PushStyleVarFloat(int idx, float val)
{
    ImGui::PushStyleVar(static_cast<ImGuiStyleVar_>(idx), val);
}

void InxGUIContext::PushStyleVarVec2(int idx, float x, float y)
{
    ImGui::PushStyleVar(static_cast<ImGuiStyleVar_>(idx), ImVec2(x, y));
}

void InxGUIContext::PopStyleVar(int count)
{
    ImGui::PopStyleVar(count);
}

void InxGUIContext::BeginDisabled(bool disabled)
{
    ImGui::BeginDisabled(disabled);
}

void InxGUIContext::EndDisabled()
{
    ImGui::EndDisabled();
}

/* Drag and Drop */
bool InxGUIContext::BeginDragDropSource(int flags)
{
    return ImGui::BeginDragDropSource(static_cast<ImGuiDragDropFlags>(flags));
}

bool InxGUIContext::SetDragDropPayload(const std::string &type, uint64_t data)
{
    return ImGui::SetDragDropPayload(type.c_str(), &data, sizeof(data));
}

bool InxGUIContext::SetDragDropPayload(const std::string &type, const std::string &data)
{
    return ImGui::SetDragDropPayload(type.c_str(), data.c_str(), data.size() + 1);
}

void InxGUIContext::EndDragDropSource()
{
    ImGui::EndDragDropSource();
}

bool InxGUIContext::BeginDragDropTarget()
{
    return ImGui::BeginDragDropTarget();
}

bool InxGUIContext::AcceptDragDropPayload(const std::string &type, uint64_t *outData)
{
    const ImGuiPayload *payload = ImGui::AcceptDragDropPayload(type.c_str());
    if (payload && payload->DataSize == sizeof(uint64_t)) {
        *outData = *static_cast<const uint64_t *>(payload->Data);
        return true;
    }
    return false;
}

bool InxGUIContext::AcceptDragDropPayload(const std::string &type, std::string *outData)
{
    const ImGuiPayload *payload = ImGui::AcceptDragDropPayload(type.c_str());
    if (payload && payload->DataSize > 0) {
        *outData = std::string(static_cast<const char *>(payload->Data), payload->DataSize - 1);
        return true;
    }
    return false;
}

bool InxGUIContext::AcceptAnyDragDropPayload(std::string *outType, uint64_t *outU64, std::string *outStr,
                                             bool *outIsU64)
{
    const ImGuiPayload *preview = ImGui::GetDragDropPayload();
    if (!preview || preview->DataType[0] == '\0')
        return false;
    const ImGuiPayload *acc = ImGui::AcceptDragDropPayload(preview->DataType);
    if (!acc)
        return false;
    outType->assign(preview->DataType);
    if (acc->DataSize == sizeof(uint64_t)) {
        *outIsU64 = true;
        *outU64 = *reinterpret_cast<const uint64_t *>(acc->Data);
        return true;
    }
    *outIsU64 = false;
    if (acc->DataSize > 0) {
        *outStr = std::string(static_cast<const char *>(acc->Data), acc->DataSize - 1);
        return true;
    }
    return false;
}

void InxGUIContext::EndDragDropTarget()
{
    ImGui::EndDragDropTarget();
}

void InxGUIContext::SetMouseCursor(int cursorType)
{
    ImGui::SetMouseCursor(static_cast<ImGuiMouseCursor>(cursorType));
}

// ========================================================================
// Scene View Input API implementation
// ========================================================================

bool InxGUIContext::IsMouseButtonDown(int button)
{
    return ImGui::IsMouseDown(button);
}

bool InxGUIContext::IsMouseButtonClicked(int button)
{
    return ImGui::IsMouseClicked(button);
}

bool InxGUIContext::IsMouseDoubleClicked(int button)
{
    return ImGui::IsMouseDoubleClicked(button);
}

bool InxGUIContext::IsMouseDragging(int button, float lockThreshold)
{
    return ImGui::IsMouseDragging(button, lockThreshold);
}

float InxGUIContext::GetMouseDragDeltaX(int button)
{
    // Use 0.0f threshold for immediate response (no lock threshold)
    return ImGui::GetMouseDragDelta(button, 0.0f).x;
}

float InxGUIContext::GetMousePosX()
{
    return ImGui::GetMousePos().x;
}

float InxGUIContext::GetMousePosY()
{
    return ImGui::GetMousePos().y;
}

float InxGUIContext::GetMouseWheelDelta()
{
    return ImGui::GetIO().MouseWheel;
}

bool InxGUIContext::IsKeyDown(int keyCode)
{
    return ImGui::IsKeyDown(static_cast<ImGuiKey>(keyCode));
}

bool InxGUIContext::IsKeyPressed(int keyCode)
{
    return ImGui::IsKeyPressed(static_cast<ImGuiKey>(keyCode));
}

bool InxGUIContext::IsKeyReleased(int keyCode)
{
    return ImGui::IsKeyReleased(static_cast<ImGuiKey>(keyCode));
}

bool InxGUIContext::IsWindowFocused(int flags)
{
    return ImGui::IsWindowFocused(flags);
}

bool InxGUIContext::IsWindowHovered(int flags)
{
    return ImGui::IsWindowHovered(flags);
}

bool InxGUIContext::WantTextInput()
{
    return ImGui::GetIO().WantTextInput;
}

void InxGUIContext::CaptureMouseFromApp(bool capture)
{
    ImGui::GetIO().WantCaptureMouse = capture;
}

void InxGUIContext::CaptureKeyboardFromApp(bool capture)
{
    ImGui::GetIO().WantCaptureKeyboard = capture;
}

void InxGUIContext::WarpMouseGlobal(float x, float y)
{
    SDL_WarpMouseGlobal(x, y);
}

float InxGUIContext::GetGlobalMousePosX()
{
    float gx = 0, gy = 0;
    SDL_GetGlobalMouseState(&gx, &gy);
    return gx;
}

float InxGUIContext::GetGlobalMousePosY()
{
    float gx = 0, gy = 0;
    SDL_GetGlobalMouseState(&gx, &gy);
    return gy;
}

void InxGUIContext::GetMainViewportBounds(float *x, float *y, float *w, float *h)
{
    ImGuiViewport *vp = ImGui::GetMainViewport();
    *x = vp->Pos.x;
    *y = vp->Pos.y;
    *w = vp->Size.x;
    *h = vp->Size.y;
}

void InxGUIContext::SetClipboardText(const std::string &text)
{
    ImGui::SetClipboardText(text.c_str());
}

std::string InxGUIContext::GetClipboardText()
{
    const char *t = ImGui::GetClipboardText();
    return t ? std::string(t) : std::string();
}

std::string InxGUIContext::InputTextMultiline(const std::string &label, const std::string &text, size_t bufferSize,
                                              float width, float height, int flags)
{
    if (!CanRenderWidgets())
        return text;

    const size_t capacity = std::max<size_t>(bufferSize, 1);
    std::vector<char> buffer(capacity, 0);
    const size_t copyLength = std::min(text.size(), capacity - 1);
    std::copy_n(text.begin(), copyLength, buffer.begin());
    ImGui::InputTextMultiline(label.c_str(), buffer.data(), buffer.size(), ImVec2(width, height), flags);
    if (InxGUISemantics::IsCaptureEnabled())
        RecordSemanticItem("text_area", label, true, label, std::nullopt, std::nullopt, std::string(buffer.data()));
    return std::string(buffer.data());
}

void InxGUIContext::GetDisplayBounds(float *x, float *y, float *w, float *h)
{
    int count = 0;
    SDL_DisplayID *displays = SDL_GetDisplays(&count);
    if (displays && count > 0) {
        SDL_Rect bounds{};
        if (SDL_GetDisplayBounds(displays[0], &bounds)) {
            *x = static_cast<float>(bounds.x);
            *y = static_cast<float>(bounds.y);
            *w = static_cast<float>(bounds.w);
            *h = static_cast<float>(bounds.h);
            SDL_free(displays);
            return;
        }
        SDL_free(displays);
    }
    // Fallback
    *x = 0;
    *y = 0;
    *w = 1920;
    *h = 1080;
}

// ==========================================================================
// Infinite drag — warp cursor to opposite screen edge when it hits a
// boundary, giving a Unity-style infinite-drag feel for DragFloat etc.
// Does NOT use SDL relative mouse mode (that conflicts with ImGui).
// ==========================================================================

void InxGUIContext::HandleDragCapture()
{
    const bool active = ImGui::IsItemActive() && ImGui::IsMouseDragging(ImGuiMouseButton_Left);

    if (active && !m_dragCaptured) {
        // Just started dragging
        m_dragCaptured = true;
    }

    if (active) {
        // Warp cursor to opposite edge when hitting screen boundary
        float mx, my;
        SDL_GetGlobalMouseState(&mx, &my);

        SDL_DisplayID did = SDL_GetPrimaryDisplay();
        SDL_Rect bounds;
        if (SDL_GetDisplayBounds(did, &bounds)) {
            const float margin = 2.0f;
            const float left = static_cast<float>(bounds.x) + margin;
            const float right = static_cast<float>(bounds.x + bounds.w) - margin;
            const float top = static_cast<float>(bounds.y) + margin;
            const float bottom = static_cast<float>(bounds.y + bounds.h) - margin;

            float newMx = mx, newMy = my;
            bool warped = false;
            if (mx <= left) {
                newMx = right - margin;
                warped = true;
            } else if (mx >= right) {
                newMx = left + margin;
                warped = true;
            }
            if (my <= top) {
                newMy = bottom - margin;
                warped = true;
            } else if (my >= bottom) {
                newMy = top + margin;
                warped = true;
            }

            if (warped) {
                SDL_WarpMouseGlobal(newMx, newMy);
                // Ignore two frames of mouse delta after warp to prevent
                // artificial value jumps in DragFloat / DragInt.
                // (Some backends report the warp jump on the next frame,
                // others one frame later.)
                m_ignoreMouseDeltaFrames = 2;
            }
        }
    } else if (m_dragCaptured) {
        // Drag ended — do NOT snap cursor back to start position.
        // Snap-back causes visible stutter and introduces artificial deltas.
        m_dragCaptured = false;
    }
}

void InxGUIContext::CompensateWarp()
{
    ImGuiIO &io = ImGui::GetIO();

    if (m_ignoreMouseDeltaFrames > 0) {
        // Drop delta right after warp (teleport artifact).
        io.MouseDelta = ImVec2(0.0f, 0.0f);
        --m_ignoreMouseDeltaFrames;
        return;
    }

    // Safety net (same idea as Scene panel): ignore implausibly large deltas
    // during drag, which are almost always edge-wrap teleport artifacts.
    constexpr float kWarpJumpThreshold = 400.0f;
    if (ImGui::IsMouseDragging(ImGuiMouseButton_Left) &&
        (std::fabs(io.MouseDelta.x) > kWarpJumpThreshold || std::fabs(io.MouseDelta.y) > kWarpJumpThreshold)) {
        io.MouseDelta = ImVec2(0.0f, 0.0f);
    }
}

void InxGUIContext::SetWindowFontScale(float scale)
{
    ImGui::SetWindowFontScale(scale);
}

void InxGUIContext::DrawRect(float minX, float minY, float maxX, float maxY, float r, float g, float b, float a,
                             float thickness, float rounding)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (drawList) {
        ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
        drawList->AddRect(ImVec2(minX, minY), ImVec2(maxX, maxY), col, rounding, 0, thickness);
    }
}

void InxGUIContext::DrawFilledRect(float minX, float minY, float maxX, float maxY, float r, float g, float b, float a,
                                   float rounding)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (drawList) {
        ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
        drawList->AddRectFilled(ImVec2(minX, minY), ImVec2(maxX, maxY), col, rounding);
    }
}

void InxGUIContext::DrawFilledRectRotated(float minX, float minY, float maxX, float maxY, float r, float g, float b,
                                          float a, float rotation, bool mirrorH, bool mirrorV, float rounding)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (!drawList)
        return;
    ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
    const int vtxStart = drawList->VtxBuffer.Size;
    drawList->AddRectFilled(ImVec2(minX, minY), ImVec2(maxX, maxY), col, rounding);

    rotation = std::fmod(rotation, 360.0f);
    if (rotation < 0.0f)
        rotation += 360.0f;
    if ((std::fabs(rotation) < 0.001f) && !mirrorH && !mirrorV)
        return;

    const float radians = rotation * 3.14159265358979f / 180.0f;
    const float cosA = std::cos(radians);
    const float sinA = std::sin(radians);
    const ImVec2 pivot((minX + maxX) * 0.5f, (minY + maxY) * 0.5f);
    for (int i = vtxStart; i < drawList->VtxBuffer.Size; ++i) {
        ImVec2 local(drawList->VtxBuffer[i].pos.x - pivot.x, drawList->VtxBuffer[i].pos.y - pivot.y);
        if (mirrorH)
            local.x = -local.x;
        if (mirrorV)
            local.y = -local.y;
        const float rx = local.x * cosA - local.y * sinA;
        const float ry = local.x * sinA + local.y * cosA;
        drawList->VtxBuffer[i].pos = ImVec2(pivot.x + rx, pivot.y + ry);
    }
}

void InxGUIContext::DrawLine(float x1, float y1, float x2, float y2, float r, float g, float b, float a,
                             float thickness)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (drawList) {
        ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
        drawList->AddLine(ImVec2(x1, y1), ImVec2(x2, y2), col, thickness);
    }
}

void InxGUIContext::DrawCircle(float centerX, float centerY, float radius, float r, float g, float b, float a,
                               float thickness, int segments)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (drawList) {
        ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
        drawList->AddCircle(ImVec2(centerX, centerY), radius, col, segments, thickness);
    }
}

void InxGUIContext::DrawFilledCircle(float centerX, float centerY, float radius, float r, float g, float b, float a,
                                     int segments)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (drawList) {
        ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
        drawList->AddCircleFilled(ImVec2(centerX, centerY), radius, col, segments);
    }
}

void InxGUIContext::DrawImageRect(uint64_t textureId, float minX, float minY, float maxX, float maxY, float uv0_x,
                                  float uv0_y, float uv1_x, float uv1_y, float tintR, float tintG, float tintB,
                                  float tintA, float rotation, bool mirrorH, bool mirrorV, float rounding)
{
    if (textureId == 0)
        return;
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (!drawList)
        return;
    ImU32 tint = ImGui::ColorConvertFloat4ToU32(ImVec4(tintR, tintG, tintB, tintA));
    const int vtxStart = drawList->VtxBuffer.Size;
    if (rounding > 0.5f)
        drawList->AddImageRounded(ToImTextureID(textureId), ImVec2(minX, minY), ImVec2(maxX, maxY),
                                  ImVec2(uv0_x, uv0_y), ImVec2(uv1_x, uv1_y), tint, rounding);
    else
        drawList->AddImage(ToImTextureID(textureId), ImVec2(minX, minY), ImVec2(maxX, maxY), ImVec2(uv0_x, uv0_y),
                           ImVec2(uv1_x, uv1_y), tint);

    rotation = std::fmod(rotation, 360.0f);
    if (rotation < 0.0f)
        rotation += 360.0f;
    if ((std::fabs(rotation) < 0.001f) && !mirrorH && !mirrorV)
        return;

    const float radians = rotation * 3.14159265358979f / 180.0f;
    const float cosA = std::cos(radians);
    const float sinA = std::sin(radians);
    const ImVec2 pivot((minX + maxX) * 0.5f, (minY + maxY) * 0.5f);
    for (int i = vtxStart; i < drawList->VtxBuffer.Size; ++i) {
        ImVec2 local(drawList->VtxBuffer[i].pos.x - pivot.x, drawList->VtxBuffer[i].pos.y - pivot.y);
        if (mirrorH)
            local.x = -local.x;
        if (mirrorV)
            local.y = -local.y;
        const float rx = local.x * cosA - local.y * sinA;
        const float ry = local.x * sinA + local.y * cosA;
        drawList->VtxBuffer[i].pos = ImVec2(pivot.x + rx, pivot.y + ry);
    }
}

void InxGUIContext::DrawText(float x, float y, const std::string &text, float r, float g, float b, float a,
                             float fontSize)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (!drawList)
        return;
    ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
    const textlayout::TextLayoutResult layout =
        textlayout::LayoutText({text, "", ResolveFontSize(fontSize), 0.0f, 1.0f, 0.0f});
    if (!layout.lines.empty()) {
        drawList->PushTextureID(ImGui::GetIO().Fonts->TexRef);
        textlayout::RenderLine(drawList, layout, layout.lines.front(), x, y, col, 0.0f);
        drawList->PopTextureID();
    }
}

void InxGUIContext::DrawTextAligned(float minX, float minY, float maxX, float maxY, const std::string &text, float r,
                                    float g, float b, float a, float alignX, float alignY, float fontSize, bool clip)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (!drawList)
        return;

    const textlayout::TextLayoutResult layout =
        textlayout::LayoutText({text, "", ResolveFontSize(fontSize), 0.0f, 1.0f, 0.0f});

    float boxW = maxX - minX;
    float boxH = maxY - minY;

    ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));

    if (clip)
        drawList->PushClipRect(ImVec2(minX, minY), ImVec2(maxX, maxY), true);

    drawList->PushTextureID(ImGui::GetIO().Fonts->TexRef);
    textlayout::RenderTextBox(drawList, minX, minY, maxX, maxY, layout, col, alignX, alignY, 0.0f);
    drawList->PopTextureID();

    if (clip)
        drawList->PopClipRect();
}

void InxGUIContext::DrawTextRotated90Aligned(float minX, float minY, float maxX, float maxY, const std::string &text,
                                             float r, float g, float b, float a, float alignX, float alignY,
                                             float fontSize, bool clockwise, bool clip)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (!drawList || text.empty())
        return;

    const textlayout::TextLayoutResult layout =
        textlayout::LayoutText({text, "", ResolveFontSize(fontSize), 0.0f, 1.0f, 0.0f});
    const ImVec2 textSize(layout.totalWidth, layout.totalHeight);

    float rotatedW = textSize.y;
    float rotatedH = textSize.x;
    float targetX = minX + (maxX - minX - rotatedW) * alignX;
    float targetY = minY + (maxY - minY - rotatedH) * alignY;

    const int vtxStart = drawList->VtxBuffer.Size;
    ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));

    if (clip)
        drawList->PushClipRect(ImVec2(minX, minY), ImVec2(maxX, maxY), true);

    drawList->PushTextureID(ImGui::GetIO().Fonts->TexRef);
    textlayout::RenderTextBox(drawList, minX, minY, minX + layout.totalWidth, minY + layout.totalHeight, layout, col,
                              0.0f, 0.0f, 0.0f);
    drawList->PopTextureID();

    if (clip)
        drawList->PopClipRect();

    if (drawList->VtxBuffer.Size <= vtxStart)
        return;

    ImVec2 boundsMin(FLT_MAX, FLT_MAX);
    ImVec2 boundsMax(-FLT_MAX, -FLT_MAX);
    for (int i = vtxStart; i < drawList->VtxBuffer.Size; ++i) {
        const ImVec2 &p = drawList->VtxBuffer[i].pos;
        boundsMin.x = std::min(boundsMin.x, p.x);
        boundsMin.y = std::min(boundsMin.y, p.y);
        boundsMax.x = std::max(boundsMax.x, p.x);
        boundsMax.y = std::max(boundsMax.y, p.y);
    }

    ImVec2 rotatedMin(FLT_MAX, FLT_MAX);
    ImVec2 rotatedMax(-FLT_MAX, -FLT_MAX);
    for (int i = vtxStart; i < drawList->VtxBuffer.Size; ++i) {
        ImVec2 local(drawList->VtxBuffer[i].pos.x - boundsMin.x, drawList->VtxBuffer[i].pos.y - boundsMin.y);
        ImVec2 rotated = clockwise ? ImVec2(local.y, textSize.x - local.x) : ImVec2(textSize.y - local.y, local.x);
        drawList->VtxBuffer[i].pos = rotated;
        rotatedMin.x = std::min(rotatedMin.x, rotated.x);
        rotatedMin.y = std::min(rotatedMin.y, rotated.y);
        rotatedMax.x = std::max(rotatedMax.x, rotated.x);
        rotatedMax.y = std::max(rotatedMax.y, rotated.y);
    }

    ImVec2 delta(targetX - rotatedMin.x, targetY - rotatedMin.y);
    for (int i = vtxStart; i < drawList->VtxBuffer.Size; ++i) {
        drawList->VtxBuffer[i].pos.x += delta.x;
        drawList->VtxBuffer[i].pos.y += delta.y;
    }
}

void InxGUIContext::DrawTextExAligned(float minX, float minY, float maxX, float maxY, const std::string &text, float r,
                                      float g, float b, float a, float alignX, float alignY, float fontSize,
                                      float wrapWidth, float rotation, bool mirrorH, bool mirrorV, bool clip,
                                      const std::string &fontPath, float lineHeight, float letterSpacing)
{
    // Normalise rotation to [0, 360)
    rotation = std::fmod(rotation, 360.0f);
    if (rotation < 0.0f)
        rotation += 360.0f;

    // General path: layout the text inside the element box first, then transform
    // the generated vertices around the box center. This keeps editor and game
    // rendering aligned with the component's bounding box semantics.
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (!drawList || text.empty())
        return;

    const textlayout::TextLayoutResult layout =
        textlayout::LayoutText({text, fontPath, ResolveFontSize(fontSize), wrapWidth, lineHeight, letterSpacing});
    const ImVec2 textSize(layout.totalWidth, layout.totalHeight);

    if (std::fabs(rotation) < 0.001f && !mirrorH && !mirrorV) {
        if (clip)
            drawList->PushClipRect(ImVec2(minX, minY), ImVec2(maxX, maxY), true);
        const ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
        drawList->PushTextureID(ImGui::GetIO().Fonts->TexRef);
        textlayout::RenderTextBox(drawList, minX, minY, maxX, maxY, layout, col, alignX, alignY, letterSpacing);
        drawList->PopTextureID();
        if (clip)
            drawList->PopClipRect();
        return;
    }

    float boxW = maxX - minX;
    float boxH = maxY - minY;
    const ImVec2 pivot((minX + maxX) * 0.5f, (minY + maxY) * 0.5f);

    if (clip)
        drawList->PushClipRect(ImVec2(minX, minY), ImVec2(maxX, maxY), true);

    const int vtxStart = drawList->VtxBuffer.Size;
    ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
    drawList->PushTextureID(ImGui::GetIO().Fonts->TexRef);
    textlayout::RenderTextBox(drawList, minX, minY, maxX, maxY, layout, col, alignX, alignY, letterSpacing);
    drawList->PopTextureID();

    if (clip)
        drawList->PopClipRect();

    if (drawList->VtxBuffer.Size <= vtxStart)
        return;

    // Compute sin/cos for arbitrary angle
    float radians = rotation * 3.14159265358979f / 180.0f;
    float cosA = std::cos(radians);
    float sinA = std::sin(radians);

    for (int i = vtxStart; i < drawList->VtxBuffer.Size; ++i) {
        ImVec2 local(drawList->VtxBuffer[i].pos.x - pivot.x, drawList->VtxBuffer[i].pos.y - pivot.y);
        if (mirrorH)
            local.x = -local.x;
        if (mirrorV)
            local.y = -local.y;
        float rx = local.x * cosA - local.y * sinA;
        float ry = local.x * sinA + local.y * cosA;
        drawList->VtxBuffer[i].pos = ImVec2(pivot.x + rx, pivot.y + ry);
    }
}

std::pair<float, float> InxGUIContext::CalcTextSizeA(const std::string &text, float fontSize,
                                                     const std::string &fontPath, float lineHeight, float letterSpacing)
{
    const textlayout::TextLayoutResult layout =
        textlayout::LayoutText({text, fontPath, ResolveFontSize(fontSize), 0.0f, lineHeight, letterSpacing});
    return {layout.totalWidth, layout.totalHeight};
}

std::pair<float, float> InxGUIContext::CalcTextSizeWrappedA(const std::string &text, float fontSize, float wrapWidth,
                                                            const std::string &fontPath, float lineHeight,
                                                            float letterSpacing)
{
    const textlayout::TextLayoutResult layout =
        textlayout::LayoutText({text, fontPath, ResolveFontSize(fontSize), wrapWidth, lineHeight, letterSpacing});
    return {layout.totalWidth, layout.totalHeight};
}

void InxGUIContext::PushDrawListClipRect(float minX, float minY, float maxX, float maxY, bool intersectWithCurrent)
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (drawList)
        drawList->PushClipRect(ImVec2(minX, minY), ImVec2(maxX, maxY), intersectWithCurrent);
}

void InxGUIContext::PopDrawListClipRect()
{
    ImDrawList *drawList = ImGui::GetWindowDrawList();
    if (drawList)
        drawList->PopClipRect();
}

// ═════════════════════════════════════════════════════════════════════════
//  Batch property renderer
// ═════════════════════════════════════════════════════════════════════════

std::vector<PropertyChange> InxGUIContext::RenderPropertyBatch(const std::vector<PropertyDesc> &descriptors,
                                                               float labelWidth)
{
    std::vector<PropertyChange> changes;

    constexpr float kMinLabelWidth = 156.0f;

    auto doLabel = [&](const std::string &label) {
        if (!label.empty()) {
            float w = labelWidth;
            if (w <= 0.0f)
                w = std::max(ImGui::CalcTextSize(label.c_str()).x + 18.0f, kMinLabelWidth);
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted(label.c_str());
            ImGui::SameLine(w);
            ImGui::SetNextItemWidth(-1);
        } else {
            ImGui::SetNextItemWidth(-1);
        }
    };

    auto drawMixedOverlay = [](bool mixed) {
        if (!mixed || ImGui::IsItemActive())
            return;
        ImDrawList *drawList = ImGui::GetWindowDrawList();
        if (!drawList)
            return;
        const ImVec2 min = ImGui::GetItemRectMin();
        const ImVec2 max = ImGui::GetItemRectMax();
        const ImGuiStyle &style = ImGui::GetStyle();
        const ImVec2 textPos(min.x + style.FramePadding.x, min.y + style.FramePadding.y);
        drawList->AddRectFilled(min, max, ImGui::GetColorU32(ImGuiCol_FrameBg), style.FrameRounding);
        drawList->AddText(textPos, ImGui::GetColorU32(ImGuiCol_Text), "--");
    };

    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    const int count = static_cast<int>(descriptors.size());
    for (int i = 0; i < count; ++i) {
        const auto &d = descriptors[i];
        const std::string &semanticId = d.semanticId.empty() ? d.widgetId : d.semanticId;

        // Layout: header / space
        if (!d.header.empty()) {
            ImGui::Spacing();
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted(d.header.c_str());
        }
        if (d.space > 0)
            ImGui::Dummy(ImVec2(0, d.space));

        switch (d.type) {
        case PropertyDesc::Float: {
            doLabel(d.label);
            float val = d.fVal[0];
            float orig = val;
            CompensateWarp();
            if (d.slider && d.rangeMin > -1e5f)
                GrabOnlySliderScalar(d.widgetId.c_str(), ImGuiDataType_Float, &val, &d.rangeMin, &d.rangeMax, "%.3f");
            else {
                const ImGuiSliderFlags flags =
                    d.rangeMin < d.rangeMax ? ImGuiSliderFlags_AlwaysClamp : ImGuiSliderFlags_None;
                ImGui::DragFloat(d.widgetId.c_str(), &val, d.speed, d.rangeMin, d.rangeMax, "%.3f", flags);
            }
            if (captureSemantics)
                RecordSemanticItem(d.slider ? "float_slider" : "drag_float", d.label, true, semanticId, std::nullopt,
                                   static_cast<double>(val));
            HandleDragCapture();
            drawMixedOverlay(d.mixed);
            if (val != orig) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::Float;
                c.fVal[0] = val;
                changes.push_back(c);
            }
            break;
        }
        case PropertyDesc::Int: {
            doLabel(d.label);
            int val = d.iVal;
            int orig = val;
            CompensateWarp();
            if (d.slider && static_cast<int>(d.rangeMin) > -1000000) {
                const int rangeMin = static_cast<int>(d.rangeMin);
                const int rangeMax = static_cast<int>(d.rangeMax);
                GrabOnlySliderScalar(d.widgetId.c_str(), ImGuiDataType_S32, &val, &rangeMin, &rangeMax, "%d");
            } else {
                const ImGuiSliderFlags flags =
                    d.rangeMin < d.rangeMax ? ImGuiSliderFlags_AlwaysClamp : ImGuiSliderFlags_None;
                ImGui::DragInt(d.widgetId.c_str(), &val, d.speed, static_cast<int>(d.rangeMin),
                               static_cast<int>(d.rangeMax), "%d", flags);
            }
            if (captureSemantics)
                RecordSemanticItem(d.slider ? "int_slider" : "drag_int", d.label, true, semanticId, std::nullopt,
                                   static_cast<double>(val));
            HandleDragCapture();
            drawMixedOverlay(d.mixed);
            if (val != orig) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::Int;
                c.iVal = val;
                changes.push_back(c);
            }
            break;
        }
        case PropertyDesc::Bool: {
            bool val = d.bVal;
            bool orig = val;
            if (d.fieldLabel)
                doLabel(d.label);
            // When the label is drawn separately, only pass an id so the
            // square can scale without shrinking any text.
            std::string cbLabel;
            if (d.fieldLabel) {
                cbLabel = d.widgetId.find("##") != std::string::npos ? d.widgetId : ("##" + d.widgetId);
            } else if (!d.label.empty()) {
                cbLabel = d.label + "##" + d.widgetId;
            } else {
                cbLabel = d.widgetId;
            }
            if (d.mixed)
                ImGui::PushItemFlag(ImGuiItemFlags_MixedValue, true);
            CheckboxInspector(cbLabel, &val);
            if (captureSemantics)
                RecordSemanticItem("checkbox", d.label.empty() ? cbLabel : d.label, true, semanticId, val);
            if (d.mixed)
                ImGui::PopItemFlag();
            if (val != orig) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::Bool;
                c.bVal = val;
                changes.push_back(c);
            }
            break;
        }
        case PropertyDesc::String: {
            doLabel(d.label);
            char buf[4096];
            const std::string shown = d.mixed ? std::string("--") : d.sVal;
            size_t len = std::min(shown.size(), sizeof(buf) - 1);
            std::memcpy(buf, shown.c_str(), len);
            buf[len] = '\0';
            if (d.multiline)
                ImGui::InputTextMultiline(d.widgetId.c_str(), buf, sizeof(buf), ImVec2(-1, 80));
            else
                ImGui::InputText(d.widgetId.c_str(), buf, 256);
            if (captureSemantics)
                RecordSemanticItem(d.multiline ? "text_area" : "text_input", d.label, true, semanticId);
            std::string newStr(buf);
            if ((!d.mixed && newStr != d.sVal) || (d.mixed && newStr != "--")) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::String;
                c.sVal = std::move(newStr);
                changes.push_back(c);
            }
            break;
        }
        case PropertyDesc::Vec2: {
            float v[2] = {d.fVal[0], d.fVal[1]};
            float lw = d.label.empty() ? 1.0f : labelWidth;
            Vector2Control(d.label.empty() ? " " : d.label, v, d.speed, lw);
            if (captureSemantics)
                RecordSemanticItem("vector", d.label, true, semanticId);
            drawMixedOverlay(d.mixed);
            if (v[0] != d.fVal[0] || v[1] != d.fVal[1]) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::Vec2;
                c.fVal[0] = v[0];
                c.fVal[1] = v[1];
                changes.push_back(c);
            }
            break;
        }
        case PropertyDesc::Vec3: {
            float v[3] = {d.fVal[0], d.fVal[1], d.fVal[2]};
            float lw = d.label.empty() ? 1.0f : labelWidth;
            Vector3Control(d.label.empty() ? " " : d.label, v, d.speed, lw);
            if (captureSemantics)
                RecordSemanticItem("vector", d.label, true, semanticId);
            drawMixedOverlay(d.mixed);
            if (v[0] != d.fVal[0] || v[1] != d.fVal[1] || v[2] != d.fVal[2]) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::Vec3;
                c.fVal[0] = v[0];
                c.fVal[1] = v[1];
                c.fVal[2] = v[2];
                changes.push_back(c);
            }
            break;
        }
        case PropertyDesc::Vec4: {
            float v[4] = {d.fVal[0], d.fVal[1], d.fVal[2], d.fVal[3]};
            float lw = d.label.empty() ? 1.0f : labelWidth;
            Vector4Control(d.label.empty() ? " " : d.label, v, d.speed, lw);
            if (captureSemantics)
                RecordSemanticItem("vector", d.label, true, semanticId);
            drawMixedOverlay(d.mixed);
            if (v[0] != d.fVal[0] || v[1] != d.fVal[1] || v[2] != d.fVal[2] || v[3] != d.fVal[3]) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::Vec4;
                c.fVal[0] = v[0];
                c.fVal[1] = v[1];
                c.fVal[2] = v[2];
                c.fVal[3] = v[3];
                changes.push_back(c);
            }
            break;
        }
        case PropertyDesc::Enum: {
            doLabel(d.label);
            int idx = d.iVal;
            int orig = idx;
            Combo(d.widgetId, &idx, d.enumNames);
            if (captureSemantics)
                RecordSemanticItem("combo", d.label, true, semanticId);
            drawMixedOverlay(d.mixed);
            if (idx != orig) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::Enum;
                c.iVal = idx;
                changes.push_back(c);
            }
            break;
        }
        case PropertyDesc::Color: {
            doLabel(d.label);
            float col[4] = {d.fVal[0], d.fVal[1], d.fVal[2], d.fVal[3]};
            if (ImGui::ColorEdit4(d.widgetId.c_str(), col)) {
                PropertyChange c;
                c.index = i;
                c.type = PropertyDesc::Color;
                c.fVal[0] = col[0];
                c.fVal[1] = col[1];
                c.fVal[2] = col[2];
                c.fVal[3] = col[3];
                changes.push_back(c);
            }
            if (captureSemantics)
                RecordSemanticItem("color_edit", d.label, true, semanticId);
            drawMixedOverlay(d.mixed);
            break;
        }
        } // switch

        // Tooltip for the last rendered widget (label hover is not tracked).
        if (!d.tooltip.empty() && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
            ImGui::SetTooltip("%s", d.tooltip.c_str());
    }
    return changes;
}

uint32_t InxGUIContext::RenderObjectFieldChrome(const std::string &fieldId, const std::string &displayText,
                                                const std::string &typeHint, bool selected, bool clickable,
                                                bool hasPicker, uint64_t pickerTextureId, const std::string &semanticId)
{
    constexpr float buttonSide = 20.0f;
    // Match Inspector list-body / header interaction tones so asset slots
    // visibly respond to hover and press (previously all three were identical).
    constexpr ImVec4 bodyColor{0.10f, 0.10f, 0.10f, 0.82f};
    constexpr ImVec4 bodyHover{0.20f, 0.18f, 0.18f, 0.95f};
    constexpr ImVec4 bodyActive{0.28f, 0.22f, 0.22f, 1.0f};
    constexpr ImVec4 borderColor{0.22f, 0.22f, 0.22f, 1.0f};
    constexpr ImVec4 borderHover{0.42f, 0.36f, 0.36f, 1.0f};
    constexpr ImVec4 borderActive{0.55f, 0.38f, 0.38f, 1.0f};
    const ImVec4 buttonIdle = EditorTheme::INSPECTOR_INLINE_BTN_IDLE;
    const ImVec4 buttonHover = EditorTheme::INSPECTOR_INLINE_BTN_HOVER;
    const ImVec4 buttonActive = EditorTheme::INSPECTOR_INLINE_BTN_ACTIVE;

    ImGui::PushID(fieldId.c_str());
    std::string fullText = "   " + displayText + " (" + typeHint + ")";
    if (fullText.size() > 38)
        fullText = fullText.substr(0, 35) + "...";

    const float availableWidth = ImGui::GetContentRegionAvail().x;
    const float buttonWidth = hasPicker ? buttonSide : 0.0f;
    const float fieldWidth = (std::max)(availableWidth - buttonWidth, 10.0f);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(16.0f, 2.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleColor(ImGuiCol_Header, bodyColor);
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, bodyHover);
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, bodyActive);
    ImGui::BeginGroup();

    uint32_t result = 0;
    if (clickable)
        ImGui::SetNextItemAllowOverlap();
    if (ImGui::Selectable(fullText.c_str(), selected, 0, ImVec2(fieldWidth, 0.0f)) && clickable)
        result |= 1u;
    // Bit 4: body double-click — used by Inspector to ping the asset in Project.
    // Detected on the selectable even when clickable is false (MeshRenderer slots).
    if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0))
        result |= 4u;

    if (hasPicker) {
        ImGui::SameLine(0.0f, 0.0f);
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (availableWidth - buttonWidth - fieldWidth));
        ImGui::PushStyleColor(ImGuiCol_Button, buttonIdle);
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, buttonHover);
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, buttonActive);
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4.0f, 2.0f));
        if (ImGui::Button("##picker", ImVec2(buttonSide, 0.0f)))
            result |= 2u;
        if (InxGUISemantics::IsCaptureEnabled())
            RecordSemanticItem("button", "Select " + typeHint, true, fieldId + ".picker");

        const ImVec2 min = ImGui::GetItemRectMin();
        const ImVec2 max = ImGui::GetItemRectMax();
        const float drawSize =
            (std::max)(0.0f, (std::min)(10.0f, (std::min)(max.x - min.x - 6.0f, max.y - min.y - 4.0f)));
        const ImVec2 drawMin{min.x + ((max.x - min.x) - drawSize) * 0.5f, min.y + ((max.y - min.y) - drawSize) * 0.5f};
        if (pickerTextureId != 0 && drawSize > 0.0f) {
            ImGui::GetWindowDrawList()->AddImage(ToImTextureID(pickerTextureId), drawMin,
                                                 ImVec2(drawMin.x + drawSize, drawMin.y + drawSize));
        } else {
            constexpr const char *fallback = "o";
            const ImVec2 textSize = ImGui::CalcTextSize(fallback);
            ImGui::GetWindowDrawList()->AddText(
                ImVec2(min.x + ((max.x - min.x) - textSize.x) * 0.5f, min.y + ((max.y - min.y) - textSize.y) * 0.5f),
                ImGui::GetColorU32(ImGuiCol_Text), fallback);
        }
        ImGui::PopStyleVar();
        ImGui::PopStyleColor(3);
    }

    // Only the picker button opens the object selector. Body single-click is
    // reserved / inert; body double-click (bit 4) is handled by callers as a
    // "ping in Project" action — matching Unity ObjectField behaviour.
    if ((result & 2u) != 0 && hasPicker)
        ImGui::OpenPopup("##obj_picker");

    ImGui::EndGroup();
    RecordSemanticItem("object_field", displayText, clickable, semanticId);
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(2);

    const ImVec2 min = ImGui::GetItemRectMin();
    const ImVec2 max = ImGui::GetItemRectMax();
    const bool groupHovered = ImGui::IsItemHovered();
    const bool groupActive = groupHovered && ImGui::IsMouseDown(ImGuiMouseButton_Left);
    const ImVec4 &drawBorder = groupActive ? borderActive : (groupHovered ? borderHover : borderColor);
    ImGui::GetWindowDrawList()->AddRect(min, max, ImGui::ColorConvertFloat4ToU32(drawBorder), 0.0f, 0, 1.0f);
    ImGui::PopID();
    return result;
}

std::vector<ObjectFieldInteraction> InxGUIContext::RenderMeshRendererInspectorFields(
    const std::string &meshFieldId, const std::string &meshLabel, const std::string &meshDisplay,
    const std::vector<std::string> &slotLabels, const std::vector<std::string> &slotDisplays, uint64_t pickerTextureId,
    float labelWidth)
{
    if (slotLabels.size() != slotDisplays.size())
        throw std::invalid_argument("Material slot label/display counts do not match.");

    constexpr float kMinLabelWidth = 156.0f;
    const auto &colors = EditorThemeRegistry::Colors();
    const auto &floats = EditorThemeRegistry::Floats();
    const auto outlineIt = colors.find("DND_DROP_OUTLINE");
    const ImVec4 dropOutline = outlineIt != colors.end() ? outlineIt->second : ImVec4(1, 1, 1, 0.85f);
    const auto thicknessIt = floats.find("DND_DROP_OUTLINE_THICKNESS");
    const float dropOutlineThickness = thicknessIt != floats.end() ? thicknessIt->second : 1.5f;
    std::vector<ObjectFieldInteraction> interactions;

    auto label = [&](const std::string &text) {
        float width = labelWidth;
        if (width <= 0.0f)
            width = (std::max)(ImGui::CalcTextSize(text.c_str()).x + 18.0f, kMinLabelWidth);
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(text.c_str());
        ImGui::SameLine(width);
        ImGui::SetNextItemWidth(-1.0f);
    };

    auto renderField = [&](int index, const std::string &fieldId, const std::string &fieldLabel,
                           const std::string &display, const std::string &typeHint,
                           const std::vector<std::string> &acceptTypes) {
        label(fieldLabel);
        ObjectFieldInteraction interaction;
        interaction.index = index;
        interaction.flags = RenderObjectFieldChrome(fieldId, display, typeHint, false, false, true, pickerTextureId);

        ImGui::PushStyleColor(ImGuiCol_DragDropTarget, ImVec4(0, 0, 0, 0));
        if (ImGui::BeginDragDropTarget()) {
            const ImVec2 min = ImGui::GetItemRectMin();
            const ImVec2 max = ImGui::GetItemRectMax();
            ImGui::GetWindowDrawList()->AddRect(min, max, ImGui::ColorConvertFloat4ToU32(dropOutline), 0.0f, 0,
                                                dropOutlineThickness);
            for (const auto &acceptType : acceptTypes) {
                if (AcceptDragDropPayload(acceptType, &interaction.payload)) {
                    interaction.payloadType = acceptType;
                    break;
                }
            }
            ImGui::EndDragDropTarget();
        }
        ImGui::PopStyleColor();

        ImGui::PushID(fieldId.c_str());
        interaction.popupOpen = ImGui::IsPopupOpen("##obj_picker");
        ImGui::PopID();
        if (interaction.flags != 0 || interaction.popupOpen || !interaction.payloadType.empty())
            interactions.push_back(std::move(interaction));
    };

    renderField(-1, meshFieldId, meshLabel, meshDisplay, "Mesh", {"MODEL_GUID", "MODEL_FILE"});
    label("Materials");
    ImGui::Text("Size: %d", static_cast<int>(slotLabels.size()));
    for (size_t index = 0; index < slotLabels.size(); ++index) {
        renderField(static_cast<int>(index), "mat_" + std::to_string(index), slotLabels[index], slotDisplays[index],
                    "Material", {"MATERIAL_FILE"});
    }
    return interactions;
}

} // namespace infernux
