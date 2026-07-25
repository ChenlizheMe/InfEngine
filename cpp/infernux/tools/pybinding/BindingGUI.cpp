#include "Infernux.h"

#ifdef DrawText
#undef DrawText
#endif

#include "gui/InxGUIContext.h"
#include "gui/InxGUIRenderable.h"
#include "gui/InxGUISemantics.h"
#include "gui/InxResourcePreviewer.h"
#include <function/editor/ConsolePanel.h>
#include <function/editor/EditorPanel.h>
#include <function/editor/EditorTheme.h>
#include <function/editor/EditorThemeRegistry.h>
#include <function/editor/HierarchyPanel.h>
#include <function/editor/InspectorPanel.h>
#include <function/editor/MenuBarPanel.h>
#include <function/editor/ProjectPanel.h>
#include <function/editor/StatusBarPanel.h>
#include <function/editor/ToolbarPanel.h>
#include <function/renderer/ProfileConfig.h>
#include <memory>
#include <pybind11/chrono.h>
#include <pybind11/complex.h>
#include <pybind11/functional.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

namespace infernux
{
class PyGUIRenderable : public InxGUIRenderable
{
  public:
    using InxGUIRenderable::InxGUIRenderable;

    void OnRender(InxGUIContext *ctx) override
    {
        PYBIND11_OVERRIDE_NAME(void, InxGUIRenderable, "on_render", OnRender, ctx);
    }
};

// ═══════════════════════════════════════════════════════════════════════════
//  Dedicated UI helper — render Position/Rotation/Scale in one bridge call
// ═══════════════════════════════════════════════════════════════════════════

namespace
{

// Render 3 Vector3 controls (Position, Rotation, Scale) and return all 9
// floats in a single flat tuple.  This avoids the per-call pybind11
// dispatch overhead that a generic batch would incur.
py::tuple RenderTransformFields(InxGUIContext &ctx, float px, float py, float pz, float rx, float ry, float rz,
                                float sx, float sy, float sz, float speedPos, float speedRot, float speedScl,
                                float labelWidth)
{
    float pos[3] = {px, py, pz};
    float rot[3] = {rx, ry, rz};
    float scl[3] = {sx, sy, sz};
    ctx.Vector3Control("Position", pos, speedPos, labelWidth);
    ctx.Vector3Control("Rotation", rot, speedRot, labelWidth);
    ctx.Vector3Control("Scale", scl, speedScl, labelWidth);
    return py::make_tuple(pos[0], pos[1], pos[2], rot[0], rot[1], rot[2], scl[0], scl[1], scl[2]);
}

PropertyDesc DecodePropertyDesc(const py::dict &d)
{
    PropertyDesc p;
    p.type = static_cast<PropertyDesc::Type>(d["t"].cast<int>());
    p.widgetId = d["w"].cast<std::string>();
    p.label = d["n"].cast<std::string>();
    if (d.contains("sid"))
        p.semanticId = d["sid"].cast<std::string>();
    switch (p.type) {
    case PropertyDesc::Float:
        p.fVal[0] = d["f"].cast<float>();
        break;
    case PropertyDesc::Int:
        p.iVal = d["i"].cast<int>();
        break;
    case PropertyDesc::Bool:
        p.bVal = d["b"].cast<bool>();
        break;
    case PropertyDesc::String:
        p.sVal = d["s"].cast<std::string>();
        break;
    case PropertyDesc::Vec2:
        p.fVal[0] = d["f"].cast<float>();
        p.fVal[1] = d["f2"].cast<float>();
        break;
    case PropertyDesc::Vec3:
        p.fVal[0] = d["f"].cast<float>();
        p.fVal[1] = d["f2"].cast<float>();
        p.fVal[2] = d["f3"].cast<float>();
        break;
    case PropertyDesc::Vec4:
        p.fVal[0] = d["f"].cast<float>();
        p.fVal[1] = d["f2"].cast<float>();
        p.fVal[2] = d["f3"].cast<float>();
        p.fVal[3] = d["f4"].cast<float>();
        break;
    case PropertyDesc::Enum:
        p.iVal = d["ei"].cast<int>();
        p.enumNames = d["en"].cast<std::vector<std::string>>();
        break;
    case PropertyDesc::Color:
        p.fVal[0] = d["f"].cast<float>();
        p.fVal[1] = d["f2"].cast<float>();
        p.fVal[2] = d["f3"].cast<float>();
        p.fVal[3] = d["f4"].cast<float>();
        break;
    }
    if (d.contains("mn"))
        p.rangeMin = d["mn"].cast<float>();
    if (d.contains("mx"))
        p.rangeMax = d["mx"].cast<float>();
    if (d.contains("sp"))
        p.speed = d["sp"].cast<float>();
    if (d.contains("sl"))
        p.slider = d["sl"].cast<bool>();
    if (d.contains("ml"))
        p.multiline = d["ml"].cast<bool>();
    if (d.contains("mix"))
        p.mixed = d["mix"].cast<bool>();
    if (d.contains("fl"))
        p.fieldLabel = d["fl"].cast<bool>();
    if (d.contains("hdr"))
        p.header = d["hdr"].cast<std::string>();
    if (d.contains("spc"))
        p.space = d["spc"].cast<float>();
    if (d.contains("tt"))
        p.tooltip = d["tt"].cast<std::string>();
    return p;
}

std::vector<PropertyDesc> DecodePropertyBatch(py::list descriptors)
{
    std::vector<PropertyDesc> props;
    const int n = static_cast<int>(py::len(descriptors));
    props.reserve(n);
    for (int i = 0; i < n; ++i) {
        props.push_back(DecodePropertyDesc(descriptors[i].cast<py::dict>()));
    }
    return props;
}

void UpdatePropertyBatchValues(PropertyBatchPlan &plan, const py::sequence &values)
{
    if (py::len(values) != static_cast<py::ssize_t>(plan.descriptors.size()))
        throw py::value_error("Property batch value count does not match the compiled plan.");

    for (size_t i = 0; i < plan.descriptors.size(); ++i) {
        auto &desc = plan.descriptors[i];
        const py::handle value = values[static_cast<py::ssize_t>(i)];
        switch (desc.type) {
        case PropertyDesc::Float:
            desc.fVal[0] = py::cast<float>(value);
            break;
        case PropertyDesc::Int:
        case PropertyDesc::Enum:
            desc.iVal = py::cast<int>(value);
            break;
        case PropertyDesc::Bool:
            desc.bVal = py::cast<bool>(value);
            break;
        case PropertyDesc::String:
            desc.sVal = py::cast<std::string>(value);
            break;
        case PropertyDesc::Vec2:
        case PropertyDesc::Vec3:
        case PropertyDesc::Vec4:
        case PropertyDesc::Color: {
            const auto components = py::reinterpret_borrow<py::sequence>(value);
            const int count = desc.type == PropertyDesc::Vec2 ? 2 : desc.type == PropertyDesc::Vec3 ? 3 : 4;
            if (py::len(components) != count)
                throw py::value_error("Property batch vector value has the wrong component count.");
            for (int component = 0; component < count; ++component)
                desc.fVal[component] = py::cast<float>(components[component]);
            break;
        }
        }
    }
}

py::dict EncodePropertyChanges(const std::vector<PropertyChange> &changes)
{
    py::dict result;
    for (const auto &c : changes) {
        py::int_ key(c.index);
        switch (c.type) {
        case PropertyDesc::Float:
            result[key] = py::float_(c.fVal[0]);
            break;
        case PropertyDesc::Int:
            result[key] = py::int_(c.iVal);
            break;
        case PropertyDesc::Bool:
            result[key] = py::bool_(c.bVal);
            break;
        case PropertyDesc::String:
            result[key] = py::str(c.sVal);
            break;
        case PropertyDesc::Vec2:
            result[key] = py::make_tuple(c.fVal[0], c.fVal[1]);
            break;
        case PropertyDesc::Vec3:
            result[key] = py::make_tuple(c.fVal[0], c.fVal[1], c.fVal[2]);
            break;
        case PropertyDesc::Vec4:
            result[key] = py::make_tuple(c.fVal[0], c.fVal[1], c.fVal[2], c.fVal[3]);
            break;
        case PropertyDesc::Enum:
            result[key] = py::int_(c.iVal);
            break;
        case PropertyDesc::Color:
            result[key] = py::make_tuple(c.fVal[0], c.fVal[1], c.fVal[2], c.fVal[3]);
            break;
        }
    }
    return result;
}

} // anonymous namespace

void RegisterGUIBindings(py::module_ &m)
{
    m.def(
        "is_frame_profile_enabled", []() { return INFERNUX_FRAME_PROFILE != 0; },
        "Return whether this native module was built with detailed frame profiling enabled.");
    m.def("set_gui_semantic_capture_enabled", &InxGUISemantics::SetCaptureEnabled, py::arg("enabled"),
          "Enable or disable the read-only editor UI semantic capture registry.");
    m.def(
        "request_gui_semantic_snapshot", []() { return InxGUISemantics::RequestSnapshot(); },
        "Request one semantic UI snapshot from the next rendered Editor frame.");
    m.def(
        "get_gui_semantic_snapshot",
        []() {
            const InxGUISemanticSnapshot snapshot = InxGUISemantics::GetSnapshot();
            py::dict result;
            result["capture_enabled"] = snapshot.captureEnabled;
            result["frame"] = snapshot.frame;
            result["snapshot_id"] = std::to_string(snapshot.frame);
            result["request_sequence"] = snapshot.requestSequence;
            result["input_sequence"] = snapshot.inputSequence;
            result["mouse"] = py::make_tuple(snapshot.mouseX, snapshot.mouseY);
            result["wants_text_input"] = snapshot.wantsTextInput;
            result["focused_window"] = snapshot.focusedWindow;
            result["focused_window_id"] = snapshot.focusedWindowId;
            py::list targets;
            for (const auto &target : snapshot.targets) {
                py::dict item;
                item["id"] = target.id;
                item["semantic_id"] = target.semanticId;
                item["label"] = target.label;
                item["kind"] = target.kind;
                item["window"] = target.window;
                item["window_id"] = target.windowId;
                if (!target.occludedByWindow.empty()) {
                    item["occluded_by_window"] = target.occludedByWindow;
                    item["occluded_by_window_id"] = target.occludedByWindowId;
                }
                item["item_id"] = target.itemId;
                item["rect"] = py::make_tuple(target.x, target.y, target.width, target.height);
                item["has_click_point"] = target.hasClickPoint;
                if (target.hasClickPoint)
                    item["click_point"] = py::make_tuple(target.clickX, target.clickY);
                item["enabled"] = target.enabled;
                item["visible"] = target.visible;
                item["active"] = target.active;
                item["focused"] = target.focused;
                if (target.hasBoolValue) {
                    item["value"] = target.boolValue;
                    item["checked"] = target.boolValue;
                } else if (target.hasNumericValue) {
                    item["value"] = target.numericValue;
                } else if (target.hasStringValue) {
                    item["value"] = target.stringValue;
                }
                targets.append(std::move(item));
            }
            result["targets"] = std::move(targets);
            return result;
        },
        "Return targets actually rendered by the latest captured editor UI frame.");

    // ── Editor theme single source of truth ─────────────────────────────
    // Python's Theme class calls these once at import and overrides its
    // class attributes by name, so styling is defined in C++
    // (EditorThemeTable.inl) and can't be drifted from Python.
    m.def(
        "get_editor_theme_colors",
        []() {
            py::dict out;
            for (const auto &[name, c] : EditorThemeRegistry::Colors())
                out[py::str(name)] = py::make_tuple(c.x, c.y, c.z, c.w);
            return out;
        },
        "Editor theme RGBA colors keyed by constant name (single source of truth)");
    m.def(
        "get_editor_theme_vec2s",
        []() {
            py::dict out;
            for (const auto &[name, v] : EditorThemeRegistry::Vec2s())
                out[py::str(name)] = py::make_tuple(v.x, v.y);
            return out;
        },
        "Editor theme 2D size/padding constants keyed by name");
    m.def(
        "get_editor_theme_floats",
        []() {
            py::dict out;
            for (const auto &[name, f] : EditorThemeRegistry::Floats())
                out[py::str(name)] = f;
            return out;
        },
        "Editor theme scalar constants keyed by name");
    m.def(
        "list_editor_themes",
        []() {
            py::list out;
            for (const auto &name : EditorThemeRegistry::ThemeNames())
                out.append(py::str(name));
            return out;
        },
        "Names of all registered editor themes");
    m.def("get_editor_theme", []() { return py::str(EditorThemeRegistry::ActiveTheme()); }, "Active editor theme name");
    m.def(
        "editor_theme_generation", []() { return static_cast<unsigned long long>(EditorThemeRegistry::Generation()); },
        "Monotonic counter bumped on every theme switch (cheap change detection)");
    m.def(
        "set_editor_theme",
        [](const std::string &name) {
            bool ok = EditorThemeRegistry::SetActiveTheme(name);
            if (ok)
                EditorThemeRegistry::ApplyImGuiColors(); // re-skin all built-in widgets at once
            return ok;
        },
        py::arg("name"), "Switch the active editor theme and re-apply the ImGui palette; returns False if unknown");

    py::class_<PropertyBatchPlan, std::shared_ptr<PropertyBatchPlan>>(m, "PropertyBatchPlan")
        .def_property_readonly("size",
                               [](const PropertyBatchPlan &plan) { return static_cast<int>(plan.descriptors.size()); });

    py::class_<InxGUIContext>(m, "InxGUIContext")
        .def("label", &InxGUIContext::Label)
        .def("text_wrapped", &InxGUIContext::TextWrapped)
        .def("button", &InxGUIContext::Button, py::arg("label"), py::arg("on_click") = py::none(),
             py::arg("width") = 0.0f, py::arg("height") = 0.0f,
             "Button widget. width=-1 fills available width, height=0 uses default.")
        .def("radio_button", &InxGUIContext::RadioButton)
        .def("selectable", &InxGUIContext::Selectable, py::arg("label"), py::arg("selected") = false,
             py::arg("flags") = 0, py::arg("width") = 0.0f, py::arg("height") = 0.0f)
        .def("checkbox",
             [](InxGUIContext &ctx, const std::string &label, bool value) {
                 ctx.Checkbox(label, &value);
                 return value;
             })
        .def(
            "checkbox_inspector",
            [](InxGUIContext &ctx, const std::string &label, bool value) {
                ctx.CheckboxInspector(label, &value);
                return value;
            },
            py::arg("label"), py::arg("value"),
            "Inspector checkbox: square scaled to 75%, label at ambient font size")
        .def("int_slider",
             [](InxGUIContext &ctx, const std::string &label, int value, int min, int max) {
                 ctx.IntSlider(label, &value, min, max);
                 return value;
             })
        .def("float_slider",
             [](InxGUIContext &ctx, const std::string &label, float value, float min, float max) {
                 ctx.FloatSlider(label, &value, min, max);
                 return value;
             })
        .def("drag_int",
             [](InxGUIContext &ctx, const std::string &label, int value, float speed, int min, int max) {
                 ctx.DragInt(label, &value, speed, min, max);
                 return value;
             })
        .def("drag_float",
             [](InxGUIContext &ctx, const std::string &label, float value, float speed, float min, float max) {
                 ctx.DragFloat(label, &value, speed, min, max);
                 return value;
             })
        .def("text_input",
             [](InxGUIContext &ctx, const std::string &label, const std::string &value, size_t buffer_size) {
                 std::vector<char> buffer(buffer_size, 0);
                 if (value.size() < buffer_size) {
                     std::copy(value.begin(), value.end(), buffer.begin());
                 } else {
                     std::copy(value.begin(), value.begin() + buffer_size - 1, buffer.begin());
                 }
                 ctx.TextInput(label, buffer.data(), buffer_size);
                 return std::string(buffer.data());
             })
        .def("text_area", &InxGUIContext::TextArea)
        .def(
            "input_text_with_hint",
            [](InxGUIContext &ctx, const std::string &label, const std::string &hint, const std::string &value,
               size_t buffer_size, int flags) {
                std::vector<char> buffer(buffer_size, 0);
                if (value.size() < buffer_size) {
                    std::copy(value.begin(), value.end(), buffer.begin());
                } else {
                    std::copy(value.begin(), value.begin() + buffer_size - 1, buffer.begin());
                }
                ctx.InputTextWithHint(label, hint, buffer.data(), buffer_size, flags);
                return std::string(buffer.data());
            },
            py::arg("label"), py::arg("hint"), py::arg("value"), py::arg("buffer_size") = 256, py::arg("flags") = 0)
        .def(
            "input_int",
            [](InxGUIContext &ctx, const std::string &label, int value, int step, int step_fast, int flags) {
                ctx.InputInt(label, &value, step, step_fast, flags);
                return value;
            },
            py::arg("label"), py::arg("value"), py::arg("step") = 1, py::arg("step_fast") = 100, py::arg("flags") = 0)
        .def(
            "input_uint",
            [](InxGUIContext &ctx, const std::string &label, uint32_t value, uint32_t step, uint32_t step_fast,
               int flags) {
                ctx.InputUInt(label, &value, step, step_fast, flags);
                return value;
            },
            py::arg("label"), py::arg("value"), py::arg("step") = 1u, py::arg("step_fast") = 100u,
            py::arg("flags") = 0)
        .def(
            "input_float",
            [](InxGUIContext &ctx, const std::string &label, float value, float step, float step_fast, int flags) {
                ctx.InputFloat(label, &value, step, step_fast, flags);
                return value;
            },
            py::arg("label"), py::arg("value"), py::arg("step") = 0.0f, py::arg("step_fast") = 0.0f,
            py::arg("flags") = 0)
        .def(
            "color_edit",
            [](InxGUIContext &ctx, const std::string &label, float r, float g, float b, float a,
               bool hdr) -> py::tuple {
                float color[4] = {r, g, b, a};
                ctx.ColorEdit(label, color, hdr);
                return py::make_tuple(py::float_(color[0]), py::float_(color[1]), py::float_(color[2]),
                                      py::float_(color[3]));
            },
            py::arg("label"), py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a") = 1.0f,
            py::arg("hdr") = false)
        .def(
            "color_picker",
            [](InxGUIContext &ctx, const std::string &label, float r, float g, float b, float a,
               int flags) -> py::tuple {
                float color[4] = {r, g, b, a};
                bool changed = ctx.ColorPicker(label, color, flags);
                return py::make_tuple(py::bool_(changed), py::float_(color[0]), py::float_(color[1]),
                                      py::float_(color[2]), py::float_(color[3]));
            },
            py::arg("label"), py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a") = 1.0f, py::arg("flags") = 0)
        .def(
            "vector2",
            [](InxGUIContext &ctx, const std::string &label, float x, float y, float speed,
               float labelWidth, const std::string &semanticId) -> py::tuple {
                float value[2] = {x, y};
                ctx.Vector2Control(label, value, speed, labelWidth, semanticId);
                return py::make_tuple(py::float_(value[0]), py::float_(value[1]));
            },
            py::arg("label"), py::arg("x"), py::arg("y"), py::arg("speed") = 0.1f,
            py::arg("label_width") = 0.0f, py::arg("semantic_id") = "")
        .def(
            "vector3",
            [](InxGUIContext &ctx, const std::string &label, float x, float y, float z, float speed,
               float labelWidth, const std::string &semanticId) -> py::tuple {
                float value[3] = {x, y, z};
                ctx.Vector3Control(label, value, speed, labelWidth, semanticId);
                return py::make_tuple(py::float_(value[0]), py::float_(value[1]), py::float_(value[2]));
            },
            py::arg("label"), py::arg("x"), py::arg("y"), py::arg("z"), py::arg("speed") = 0.1f,
            py::arg("label_width") = 0.0f, py::arg("semantic_id") = "")
        .def(
            "vector4",
            [](InxGUIContext &ctx, const std::string &label, float x, float y, float z, float w, float speed,
               float labelWidth) -> py::tuple {
                float value[4] = {x, y, z, w};
                ctx.Vector4Control(label, value, speed, labelWidth);
                return py::make_tuple(py::float_(value[0]), py::float_(value[1]), py::float_(value[2]),
                                      py::float_(value[3]));
            },
            py::arg("label"), py::arg("x"), py::arg("y"), py::arg("z"), py::arg("w"), py::arg("speed") = 0.1f,
            py::arg("label_width") = 0.0f)
        .def(
            "combo",
            [](InxGUIContext &ctx, const std::string &label, int currentItem, const std::vector<std::string> &items,
               int popupMaxHeightInItems) {
                ctx.Combo(label, &currentItem, items, popupMaxHeightInItems);
                return currentItem;
            },
            py::arg("label"), py::arg("current_item"), py::arg("items"), py::arg("popup_max_height_in_items") = -1)
        .def("searchable_combo", &InxGUIContext::SearchableCombo, py::arg("id"), py::arg("current_item"),
             py::arg("items"), py::arg("width") = 0.0f, py::arg("max_visible_items") = 8,
             py::arg("search_hint") = "Filter...", py::arg("empty_text") = "No results",
             "Render the shared keyboard-navigable searchable dropdown")
        .def(
            "list_box",
            [](InxGUIContext &ctx, const std::string &label, int currentItem, const std::vector<std::string> &items,
               int heightInItems) {
                ctx.ListBox(label, &currentItem, items, heightInItems);
                return currentItem;
            },
            py::arg("label"), py::arg("current_item"), py::arg("items"), py::arg("height_in_items") = -1)
        .def("progress_bar", &InxGUIContext::ProgressBar)
        .def("begin_group", &InxGUIContext::BeginGroup, py::arg("name") = "")
        .def("end_group", &InxGUIContext::EndGroup)
        .def("same_line", &InxGUIContext::SameLine, py::arg("offset_from_start_x") = 0.0f, py::arg("spacing") = -1.0f)
        .def("align_text_to_frame_padding", &InxGUIContext::AlignTextToFramePadding,
             "Vertically align upcoming text baseline to FramePadding.y so it aligns with framed widgets")
        .def("set_scroll_here_y", &InxGUIContext::SetScrollHereY, py::arg("center_y_ratio") = 0.5f,
             "Adjust scrolling amount to make current cursor position visible. 0.0=top, 0.5=center, 1.0=bottom")
        .def("get_scroll_y", &InxGUIContext::GetScrollY, "Get current scroll Y position")
        .def("get_scroll_max_y", &InxGUIContext::GetScrollMaxY, "Get maximum scroll Y value")
        .def("set_scroll_x", &InxGUIContext::SetScrollX, py::arg("scroll_x"), "Set horizontal scroll position")
        .def("set_scroll_y", &InxGUIContext::SetScrollY, py::arg("scroll_y"), "Set vertical scroll position")
        .def("separator", &InxGUIContext::Separator)
        .def("spacing", &InxGUIContext::Spacing)
        .def("dummy", &InxGUIContext::Dummy)
        .def("new_line", &InxGUIContext::NewLine)
        .def("tree_node", &InxGUIContext::TreeNode)
        .def("tree_node_ex", &InxGUIContext::TreeNodeEx, py::arg("label"), py::arg("flags"),
             "Create tree node with flags (ImGuiTreeNodeFlags)")
        .def("tree_pop", &InxGUIContext::TreePop)
        .def("set_next_item_open", &InxGUIContext::SetNextItemOpen, py::arg("is_open"), py::arg("cond") = 0)
        .def("set_next_item_allow_overlap", &InxGUIContext::SetNextItemAllowOverlap,
             "Allow the next item to be overlapped by a subsequent item (e.g. checkbox over CollapsingHeader).")
        .def("collapsing_header", &InxGUIContext::CollapsingHeader)
        .def("render_compact_section_header", &InxGUIContext::RenderCompactSectionHeader,
             py::arg("label"), py::arg("icon_id"), py::arg("default_open"), py::arg("open_condition"),
             py::arg("allow_overlap"), py::arg("frame_pad_x"), py::arg("frame_pad_y"),
             py::arg("item_spacing_x"), py::arg("item_spacing_y"), py::arg("border_size"),
             py::arg("zero_indent"), py::arg("font_scale"), py::arg("right_margin"), py::arg("icon_size"),
             py::arg("header_color"), py::arg("hover_color"), py::arg("active_color"),
             py::arg("use_text_color"), py::arg("text_color"),
             "Render shared compact Inspector header styling in one native call.")
        .def("is_item_clicked", &InxGUIContext::IsItemClicked, py::arg("mouse_button") = 0)
        .def("begin_tab_bar", &InxGUIContext::BeginTabBar)
        .def("end_tab_bar", &InxGUIContext::EndTabBar)
        .def(
            "begin_tab_item",
            [](InxGUIContext &context, const std::string &label) { return context.BeginTabItem(label, nullptr); },
            py::arg("label"))
        .def("end_tab_item", &InxGUIContext::EndTabItem)
        .def("begin_main_menu_bar", &InxGUIContext::BeginMainMenuBar)
        .def("end_main_menu_bar", &InxGUIContext::EndMainMenuBar)
        .def("begin_menu", &InxGUIContext::BeginMenu, py::arg("label"), py::arg("enabled") = true,
             py::arg("semantic_id") = "")
        .def("end_menu", &InxGUIContext::EndMenu)
        .def("menu_item", &InxGUIContext::MenuItem,
             py::arg("label"), py::arg("shortcut") = "", py::arg("selected") = false, py::arg("enabled") = true)
        .def("begin_child", &InxGUIContext::BeginChild, py::arg("id"), py::arg("width") = 0.0f,
             py::arg("height") = 0.0f, py::arg("border") = false, py::arg("flags") = 0,
             "Begin a child region. flags takes ImGuiWindowFlags (e.g. NoScrollWithMouse).")
        .def("end_child", &InxGUIContext::EndChild)
        .def("open_popup", &InxGUIContext::OpenPopup)
        .def("begin_popup", &InxGUIContext::BeginPopup)
        .def("begin_popup_modal", &InxGUIContext::BeginPopupModal, py::arg("title"), py::arg("flags") = 0,
             "Open a modal popup. Returns true while open. Must call end_popup() when true.")
        .def("begin_popup_context_item", &InxGUIContext::BeginPopupContextItem, py::arg("id") = "",
             py::arg("mouse_button") = 1, "Open context popup on right-click of last item")
        .def("begin_popup_context_window", &InxGUIContext::BeginPopupContextWindow, py::arg("id") = "",
             py::arg("mouse_button") = 1, py::arg("no_open_over_items") = false,
             "Open context popup on right-click in a window, optionally excluding rendered items")
        .def("end_popup", &InxGUIContext::EndPopup)
        .def("close_current_popup", &InxGUIContext::CloseCurrentPopup, "Close current popup")
        .def("record_semantic_item", &InxGUIContext::RecordSemanticItem, py::arg("kind"), py::arg("label"),
             py::arg("enabled") = true, py::arg("semantic_id") = "",
             py::arg("bool_value") = std::nullopt,
             py::arg("numeric_value") = std::nullopt,
             py::arg("string_value") = std::nullopt,
             "Record a stable semantic identity for the previously rendered widget.")
        .def("record_semantic_rect", &InxGUIContext::RecordSemanticRect, py::arg("kind"), py::arg("label"),
             py::arg("x"), py::arg("y"), py::arg("width"), py::arg("height"), py::arg("enabled") = true,
             py::arg("semantic_id") = "", "Record a semantic target for engine UI drawn without an ImGui item.")
        .def("record_semantic_window", &InxGUIContext::RecordSemanticWindow, py::arg("kind"), py::arg("label"),
             py::arg("semantic_id") = "", "Record a stable semantic identity for the current window.")
        .def_property_readonly(
            "semantic_capture_enabled", [](const InxGUIContext &) { return InxGUISemantics::IsCaptureEnabled(); },
            "Whether this GUI frame is collecting a requested semantic snapshot.")
        .def("begin_tooltip", &InxGUIContext::BeginTooltip)
        .def("end_tooltip", &InxGUIContext::EndTooltip)
        .def("set_tooltip", &InxGUIContext::SetTooltip)
        .def(
            "image",
            [](InxGUIContext &ctx, uint64_t textureId, float width, float height, float uv0_x, float uv0_y, float uv1_x,
               float uv1_y) {
                ctx.Image(reinterpret_cast<void *>(textureId), width, height, uv0_x, uv0_y, uv1_x, uv1_y);
            },
            py::arg("texture_id"), py::arg("width"), py::arg("height"), py::arg("uv0_x") = 0.0f,
            py::arg("uv0_y") = 0.0f, py::arg("uv1_x") = 1.0f, py::arg("uv1_y") = 1.0f)
        .def(
            "image_button",
            [](InxGUIContext &ctx, const std::string &id, uint64_t textureId, float width, float height, float uv0_x,
               float uv0_y, float uv1_x, float uv1_y) {
                return ctx.ImageButton(id, reinterpret_cast<void *>(textureId), width, height, uv0_x, uv0_y, uv1_x,
                                       uv1_y);
            },
            py::arg("id"), py::arg("texture_id"), py::arg("width"), py::arg("height"), py::arg("uv0_x") = 0.0f,
            py::arg("uv0_y") = 0.0f, py::arg("uv1_x") = 1.0f, py::arg("uv1_y") = 1.0f)
        .def("begin_table", &InxGUIContext::BeginTable)
        .def("end_table", &InxGUIContext::EndTable)
        .def("table_setup_column", &InxGUIContext::TableSetupColumn)
        .def("table_headers_row", &InxGUIContext::TableHeadersRow)
        .def("table_next_row", &InxGUIContext::TableNextRow)
        .def("table_set_column_index", &InxGUIContext::TableSetColumnIndex)
        .def("table_next_column", &InxGUIContext::TableNextColumn)
        .def("checkbox_flags", &InxGUIContext::CheckboxFlags)
        .def("set_next_item_width", &InxGUIContext::SetNextItemWidth)
        .def("set_next_window_size", &InxGUIContext::SetNextWindowSize)
        .def("set_next_window_pos", &InxGUIContext::SetNextWindowPos)
        .def("set_next_window_focus", &InxGUIContext::SetNextWindowFocus)
        .def("set_window_focus", &InxGUIContext::SetWindowFocus, "Focus the current window immediately")
        .def("begin_window", &InxGUIContext::BeginWindow)
        // begin_window_closable returns tuple (is_visible, is_open) for closable windows
        .def(
            "begin_window_closable",
            [](InxGUIContext &ctx, const std::string &name, bool is_open, int flags) -> std::tuple<bool, bool> {
                bool open = is_open;
                bool visible = ctx.BeginWindow(name, &open, flags);
                return std::make_tuple(visible, open);
            },
            py::arg("name"), py::arg("is_open") = true, py::arg("flags") = 0,
            "Begin a closable window. Returns (is_visible, is_open). "
            "When user clicks close button, is_open becomes False.")
        .def("end_window", &InxGUIContext::EndWindow)
        // Layout query methods
        .def("calc_text_width", &InxGUIContext::CalcTextWidth, py::arg("text"),
             "Calculate the pixel width of the given text string")
        .def("get_content_region_avail_width", &InxGUIContext::GetContentRegionAvailWidth)
        .def("get_content_region_avail_height", &InxGUIContext::GetContentRegionAvailHeight)
        .def("get_cursor_pos_x", &InxGUIContext::GetCursorPosX)
        .def("get_cursor_pos_y", &InxGUIContext::GetCursorPosY)
        .def("is_virtualized_region_visible", &InxGUIContext::IsVirtualizedRegionVisible, py::arg("height"),
             "Return whether a measured vertical region intersects the current window clip rectangle.")
        .def("set_cursor_pos_x", &InxGUIContext::SetCursorPosX)
        .def("set_cursor_pos_y", &InxGUIContext::SetCursorPosY)
        .def("get_window_pos_x", &InxGUIContext::GetWindowPosX)
        .def("get_window_pos_y", &InxGUIContext::GetWindowPosY)
        .def("get_window_width", &InxGUIContext::GetWindowWidth)
        .def("get_item_rect_min_x", &InxGUIContext::GetItemRectMinX)
        .def("get_item_rect_min_y", &InxGUIContext::GetItemRectMinY)
        .def("get_item_rect_max_x", &InxGUIContext::GetItemRectMaxX)
        .def("get_item_rect_max_y", &InxGUIContext::GetItemRectMaxY)
        // Splitter helper methods
        .def("invisible_button", &InxGUIContext::InvisibleButton)
        .def("is_item_active", &InxGUIContext::IsItemActive)
        .def("is_any_item_active", &InxGUIContext::IsAnyItemActive)
        .def("is_item_hovered", &InxGUIContext::IsItemHovered)
        .def("is_item_focused", &InxGUIContext::IsItemFocused)
        .def("set_keyboard_focus_here", &InxGUIContext::SetKeyboardFocusHere, py::arg("offset") = 0,
             "Set keyboard focus to the next item (or previous with negative offset)")
        .def("is_item_deactivated", &InxGUIContext::IsItemDeactivated,
             "Check if the last item was deactivated (focused -> unfocused)")
        .def("is_item_deactivated_after_edit", &InxGUIContext::IsItemDeactivatedAfterEdit,
             "Check if the last item was deactivated and value was modified")
        .def("get_mouse_drag_delta_y", &InxGUIContext::GetMouseDragDeltaY, py::arg("button") = 0)
        .def("reset_mouse_drag_delta", &InxGUIContext::ResetMouseDragDelta, py::arg("button") = 0)
        // ID stack for unique widget IDs
        .def("push_id", py::overload_cast<int>(&InxGUIContext::PushID), py::arg("id"),
             "Push integer ID onto the ID stack")
        .def("push_id_str", py::overload_cast<const std::string &>(&InxGUIContext::PushID), py::arg("id"),
             "Push string ID onto the ID stack")
        .def("pop_id", &InxGUIContext::PopID, "Pop from the ID stack") // Style
        .def("push_style_color", &InxGUIContext::PushStyleColor, py::arg("idx"), py::arg("r"), py::arg("g"),
             py::arg("b"), py::arg("a"), "Push style color (ImGuiCol enum value)")
        .def("pop_style_color", &InxGUIContext::PopStyleColor, py::arg("count") = 1, "Pop style color")
        .def("push_style_var_float", &InxGUIContext::PushStyleVarFloat, py::arg("idx"), py::arg("val"),
             "Push style var (float) by ImGuiStyleVar enum value")
        .def("push_style_var_vec2", &InxGUIContext::PushStyleVarVec2, py::arg("idx"), py::arg("x"), py::arg("y"),
             "Push style var (ImVec2) by ImGuiStyleVar enum value")
        .def("pop_style_var", &InxGUIContext::PopStyleVar, py::arg("count") = 1, "Pop style var")
        .def("begin_disabled", &InxGUIContext::BeginDisabled, py::arg("disabled") = true,
             "Begin a disabled section (grayed out, no interaction)")
        .def("end_disabled", &InxGUIContext::EndDisabled, "End disabled section") // Drag and Drop
        .def("begin_drag_drop_source", &InxGUIContext::BeginDragDropSource, py::arg("flags") = 0,
             "Begin a drag source on the last item")
        .def("set_drag_drop_payload",
             py::overload_cast<const std::string &, uint64_t>(&InxGUIContext::SetDragDropPayload), py::arg("type"),
             py::arg("data"), "Set drag-drop payload (uint64 data)")
        .def("set_drag_drop_payload_str",
             py::overload_cast<const std::string &, const std::string &>(&InxGUIContext::SetDragDropPayload),
             py::arg("type"), py::arg("data"), "Set drag-drop payload (string data)")
        .def("end_drag_drop_source", &InxGUIContext::EndDragDropSource, "End drag source")
        .def("begin_drag_drop_target", &InxGUIContext::BeginDragDropTarget, "Begin a drag-drop target on last item")
        .def(
            "accept_drag_drop_payload",
            [](InxGUIContext &ctx, const std::string &type) -> py::
                                                                object {
                                                                    // Try uint64_t first
                                                                    uint64_t data_int = 0;
                                                                    if (ctx.AcceptDragDropPayload(type, &data_int)) {
                                                                        return py::cast(data_int);
                                                                    }
                                                                    // Try string
                                                                    std::string data_str;
                                                                    if (ctx.AcceptDragDropPayload(type, &data_str)) {
                                                                        return py::cast(data_str);
                                                                    }
                                                                    return py::none();
                                                                },
            py::arg("type"), "Accept drag-drop payload, returns data (int or str) or None")
        .def(
            "accept_any_drag_drop_payload",
            [](InxGUIContext &ctx) -> py::object {
                std::string ty;
                uint64_t u64 = 0;
                std::string s;
                bool isU64 = false;
                if (!ctx.AcceptAnyDragDropPayload(&ty, &u64, &s, &isU64))
                    return py::none();
                if (isU64)
                    return py::make_tuple(ty, py::cast(u64));
                return py::make_tuple(ty, py::cast(s));
            },
            "Accept whatever drag payload is offered (type string + str or uint64). None if none.")
        .def("end_drag_drop_target", &InxGUIContext::EndDragDropTarget, "End drag-drop target")
        // Mouse cursor
        .def("set_mouse_cursor", &InxGUIContext::SetMouseCursor, py::arg("cursor_type"),
             "Set mouse cursor: 0=Arrow, 1=TextInput, 2=ResizeAll, 3=ResizeNS, 4=ResizeEW, 5=ResizeNESW, 6=ResizeNWSE, "
             "7=Hand, 8=NotAllowed")
        // ========================================================================
        // Scene View Input API - Unity-style camera controls
        // ========================================================================
        // Mouse state
        .def("is_mouse_button_down", &InxGUIContext::IsMouseButtonDown, py::arg("button"),
             "Check if mouse button is held down (0=left, 1=right, 2=middle)")
        .def("is_mouse_button_clicked", &InxGUIContext::IsMouseButtonClicked, py::arg("button"),
             "Check if mouse button was clicked this frame")
        .def("is_mouse_double_clicked", &InxGUIContext::IsMouseDoubleClicked, py::arg("button") = 0,
             "Check if mouse button was double-clicked this frame")
        .def("is_mouse_dragging", &InxGUIContext::IsMouseDragging, py::arg("button"), py::arg("lock_threshold") = -1.0f,
             "Check if mouse is being dragged")
        .def("get_mouse_drag_delta_x", &InxGUIContext::GetMouseDragDeltaX, py::arg("button") = 0,
             "Get horizontal mouse drag delta")
        .def("get_mouse_pos_x", &InxGUIContext::GetMousePosX, "Get current mouse X position")
        .def("get_mouse_pos_y", &InxGUIContext::GetMousePosY, "Get current mouse Y position")
        .def("get_mouse_wheel_delta", &InxGUIContext::GetMouseWheelDelta, "Get mouse wheel scroll delta")
        // Keyboard state
        .def("is_key_down", &InxGUIContext::IsKeyDown, py::arg("key_code"),
             "Check if key is held down (ImGuiKey enum values)")
        .def("is_key_pressed", &InxGUIContext::IsKeyPressed, py::arg("key_code"), "Check if key was pressed this frame")
        .def("is_key_released", &InxGUIContext::IsKeyReleased, py::arg("key_code"),
             "Check if key was released this frame")
        // Window focus state
        .def("is_window_focused", &InxGUIContext::IsWindowFocused, py::arg("flags") = 0,
             "Check if current window is focused")
        .def("is_window_hovered", &InxGUIContext::IsWindowHovered, py::arg("flags") = 0,
             "Check if mouse is over current window")
        .def("want_text_input", &InxGUIContext::WantTextInput,
             "Returns true when ImGui wants keyboard input (e.g. text field is active)")
        // Input capture
        .def("capture_mouse_from_app", &InxGUIContext::CaptureMouseFromApp, py::arg("capture"),
             "Capture mouse input from application")
        .def("capture_keyboard_from_app", &InxGUIContext::CaptureKeyboardFromApp, py::arg("capture"),
             "Capture keyboard input from application")
        // Mouse warp / global pos for Unity-style screen-edge wrapping
        .def("warp_mouse_global", &InxGUIContext::WarpMouseGlobal, py::arg("x"), py::arg("y"),
             "Warp mouse cursor to global screen coordinates")
        .def("get_global_mouse_pos_x", &InxGUIContext::GetGlobalMousePosX, "Get global (screen) mouse X coordinate")
        .def("get_global_mouse_pos_y", &InxGUIContext::GetGlobalMousePosY, "Get global (screen) mouse Y coordinate")
        .def(
            "get_main_viewport_bounds",
            [](InxGUIContext &ctx) -> py::
                                       tuple {
                                           float x = 0, y = 0, w = 0, h = 0;
                                           ctx.GetMainViewportBounds(&x, &y, &w, &h);
                                           return py::make_tuple(py::float_(x), py::float_(y), py::float_(w),
                                                                 py::float_(h));
                                       },
            "Returns (x, y, width, height) of the main ImGui viewport (app window client area)")
        .def("set_clipboard_text", &InxGUIContext::SetClipboardText, py::arg("text"), "Set the system clipboard text")
        .def("get_clipboard_text", &InxGUIContext::GetClipboardText, "Get the system clipboard text")
        .def("input_text_multiline", &InxGUIContext::InputTextMultiline, py::arg("label"), py::arg("text"),
             py::arg("buffer_size") = 4096, py::arg("width") = -1.0f, py::arg("height") = -1.0f,
             py::arg("flags") = 0, "Editable multiline text input. Returns the (possibly modified) text.")
        .def("draw_rect", &InxGUIContext::DrawRect, py::arg("min_x"), py::arg("min_y"), py::arg("max_x"),
             py::arg("max_y"), py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a"), py::arg("thickness") = 1.0f,
             py::arg("rounding") = 0.0f, "Draw a rectangle outline on the current window's draw list (screen coords)")
        .def("draw_filled_rect", &InxGUIContext::DrawFilledRect, py::arg("min_x"), py::arg("min_y"), py::arg("max_x"),
             py::arg("max_y"), py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a"), py::arg("rounding") = 0.0f,
             "Draw a filled rectangle on the current window's draw list (screen coords)")
        .def("draw_filled_rect_rotated", &InxGUIContext::DrawFilledRectRotated, py::arg("min_x"), py::arg("min_y"),
             py::arg("max_x"), py::arg("max_y"), py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a"),
             py::arg("rotation") = 0.0f, py::arg("mirror_h") = false, py::arg("mirror_v") = false,
             py::arg("rounding") = 0.0f,
             "Draw a filled rectangle with rotation/mirror on the current window's draw list")
        .def("draw_line", &InxGUIContext::DrawLine, py::arg("x1"), py::arg("y1"), py::arg("x2"), py::arg("y2"),
             py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a"), py::arg("thickness") = 1.0f,
             "Draw a line on the current window's draw list (screen coords)")
        .def("draw_circle", &InxGUIContext::DrawCircle, py::arg("center_x"), py::arg("center_y"), py::arg("radius"),
             py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a"), py::arg("thickness") = 1.0f,
             py::arg("segments") = 0, "Draw a circle outline on the current window's draw list (screen coords)")
        .def("draw_filled_circle", &InxGUIContext::DrawFilledCircle, py::arg("center_x"), py::arg("center_y"),
             py::arg("radius"), py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a"), py::arg("segments") = 0,
             "Draw a filled circle on the current window's draw list (screen coords)")
        .def("draw_image_rect", &InxGUIContext::DrawImageRect, py::arg("texture_id"), py::arg("min_x"),
             py::arg("min_y"), py::arg("max_x"), py::arg("max_y"), py::arg("uv0_x") = 0.0f, py::arg("uv0_y") = 0.0f,
             py::arg("uv1_x") = 1.0f, py::arg("uv1_y") = 1.0f, py::arg("tint_r") = 1.0f, py::arg("tint_g") = 1.0f,
             py::arg("tint_b") = 1.0f, py::arg("tint_a") = 1.0f, py::arg("rotation") = 0.0f,
             py::arg("mirror_h") = false, py::arg("mirror_v") = false, py::arg("rounding") = 0.0f,
             "Draw an image quad in absolute screen coordinates with optional rotation, mirroring and rounding")
        .def("set_window_font_scale", &InxGUIContext::SetWindowFontScale, py::arg("scale"),
             "Set font scale for the current window (1.0 = default)")
        .def("get_dpi_scale", &InxGUIContext::GetDpiScale,
             "Get the OS display scale factor (e.g. 2.0 for 200% scaling)")
        .def("draw_text", &InxGUIContext::DrawText, py::arg("x"), py::arg("y"), py::arg("text"), py::arg("r"),
             py::arg("g"), py::arg("b"), py::arg("a"), py::arg("font_size") = 0.0f,
             "Draw text at absolute screen coordinates with colour and optional font size")
        .def("draw_text_aligned", &InxGUIContext::DrawTextAligned, py::arg("min_x"), py::arg("min_y"), py::arg("max_x"),
             py::arg("max_y"), py::arg("text"), py::arg("r"), py::arg("g"), py::arg("b"), py::arg("a"),
             py::arg("align_x") = 0.0f, py::arg("align_y") = 0.0f, py::arg("font_size") = 0.0f, py::arg("clip") = false,
             "Draw aligned text within a bounding box (align 0=left/top, 0.5=center, 1=right/bottom)")
        .def("draw_text_rotated_90_aligned", &InxGUIContext::DrawTextRotated90Aligned, py::arg("min_x"),
             py::arg("min_y"), py::arg("max_x"), py::arg("max_y"), py::arg("text"), py::arg("r"), py::arg("g"),
             py::arg("b"), py::arg("a"), py::arg("align_x") = 0.0f, py::arg("align_y") = 0.0f,
             py::arg("font_size") = 0.0f, py::arg("clockwise") = false, py::arg("clip") = false,
             "Draw text rotated by 90 degrees inside a bounding box")
        .def("draw_text_ex_aligned", &InxGUIContext::DrawTextExAligned, py::arg("min_x"), py::arg("min_y"),
             py::arg("max_x"), py::arg("max_y"), py::arg("text"), py::arg("r"), py::arg("g"), py::arg("b"),
             py::arg("a"), py::arg("align_x") = 0.0f, py::arg("align_y") = 0.0f, py::arg("font_size") = 0.0f,
             py::arg("wrap_width") = 0.0f, py::arg("rotation") = 0.0f, py::arg("mirror_h") = false,
             py::arg("mirror_v") = false, py::arg("clip") = false, py::arg("font_path") = std::string(),
             py::arg("line_height") = 1.0f, py::arg("letter_spacing") = 0.0f,
             "Draw aligned text with arbitrary rotation (degrees) and optional horizontal/vertical mirror")
        .def(
            "calc_text_size",
            [](InxGUIContext &ctx, const std::string &text, float fontSize, const std::string &fontPath,
               float lineHeight, float letterSpacing) -> py::tuple {
                auto [w, h] = ctx.CalcTextSizeA(text, fontSize, fontPath, lineHeight, letterSpacing);
                return py::make_tuple(py::float_(w), py::float_(h));
            },
            py::arg("text"), py::arg("font_size") = 0.0f, py::arg("font_path") = std::string(),
            py::arg("line_height") = 1.0f, py::arg("letter_spacing") = 0.0f,
            "Calculate pixel size of text at given font size. Returns (width, height).")
        .def(
            "calc_text_size_wrapped",
            [](InxGUIContext &ctx, const std::string &text, float fontSize, float wrapWidth,
               const std::string &fontPath, float lineHeight, float letterSpacing) -> py::tuple {
                auto [w, h] = ctx.CalcTextSizeWrappedA(text, fontSize, wrapWidth, fontPath, lineHeight, letterSpacing);
                return py::make_tuple(py::float_(w), py::float_(h));
            },
            py::arg("text"), py::arg("font_size") = 0.0f, py::arg("wrap_width") = 0.0f,
            py::arg("font_path") = std::string(), py::arg("line_height") = 1.0f, py::arg("letter_spacing") = 0.0f,
            "Calculate wrapped pixel size of text at given font size. Returns (width, height).")
        .def("push_draw_list_clip_rect", &InxGUIContext::PushDrawListClipRect, py::arg("min_x"), py::arg("min_y"),
             py::arg("max_x"), py::arg("max_y"), py::arg("intersect_with_current") = true,
             "Push a clip rect onto the draw list for subsequent draw calls")
        .def("pop_draw_list_clip_rect", &InxGUIContext::PopDrawListClipRect,
             "Pop the last clip rect from the draw list")
        .def(
            "get_display_bounds",
            [](InxGUIContext &ctx) -> py::
                                       tuple {
                                           float x, y, w, h;
                                           ctx.GetDisplayBounds(&x, &y, &w, &h);
                                           return py::make_tuple(py::float_(x), py::float_(y), py::float_(w),
                                                                 py::float_(h));
                                       },
            "Get primary display bounds as (x, y, width, height)")
        // ── Dedicated Transform renderer (1 call for 3 vector3) ────
        .def(
            "render_transform_fields",
            [](InxGUIContext &ctx, float px, float py, float pz, float rx, float ry, float rz, float sx, float sy,
               float sz, float speedPos, float speedRot, float speedScl, float labelWidth) {
                return RenderTransformFields(ctx, px, py, pz, rx, ry, rz, sx, sy, sz, speedPos, speedRot, speedScl,
                                             labelWidth);
            },
            py::arg("px"), py::arg("py"), py::arg("pz"), py::arg("rx"), py::arg("ry"), py::arg("rz"), py::arg("sx"),
            py::arg("sy"), py::arg("sz"), py::arg("speed_pos"), py::arg("speed_rot"), py::arg("speed_scl"),
            py::arg("label_width"), "Render Position/Rotation/Scale vector3 controls in one call.")
        .def(
            "create_property_batch_plan",
            [](InxGUIContext &, py::list descriptors) {
                auto plan = std::make_shared<PropertyBatchPlan>();
                plan->descriptors = DecodePropertyBatch(descriptors);
                return plan;
            },
            py::arg("descriptors"),
            "Compile a property descriptor list into a reusable native batch plan.")
        // ── Batch property renderer (N fields in 1 call) ───────────
        .def(
            "render_property_batch",
            [](InxGUIContext &ctx, py::list descriptors, float labelWidth) -> py::dict {
                return EncodePropertyChanges(ctx.RenderPropertyBatch(DecodePropertyBatch(descriptors), labelWidth));
            },
            py::arg("descriptors"), py::arg("label_width"),
            "Render a batch of property fields in one call. Returns {index: new_value} for changed fields.")
        .def(
            "render_property_batch_plan",
            [](InxGUIContext &ctx, const std::shared_ptr<PropertyBatchPlan> &plan, float labelWidth) -> py::dict {
                if (!plan)
                    return py::dict();
                return EncodePropertyChanges(ctx.RenderPropertyBatch(plan->descriptors, labelWidth));
            },
            py::arg("plan"), py::arg("label_width"),
            "Render a reusable native property batch plan. Returns {index: new_value} for changed fields.")
        .def(
            "render_property_batch_plan_values",
            [](InxGUIContext &ctx, const std::shared_ptr<PropertyBatchPlan> &plan, py::sequence values,
               float labelWidth) -> py::dict {
                if (!plan)
                    return py::dict();
                UpdatePropertyBatchValues(*plan, values);
                return EncodePropertyChanges(ctx.RenderPropertyBatch(plan->descriptors, labelWidth));
            },
            py::arg("plan"), py::arg("values"), py::arg("label_width"),
            "Update only the values of a compiled property plan, render it, and return changed fields.")
        .def(
            "render_material_top",
            [](InxGUIContext &ctx, const std::string &shaderSectionLabel, const std::string &vertexLabel,
               const std::string &vertexDisplay, const std::string &fragmentLabel,
               const std::string &fragmentDisplay, float shaderLabelWidth, const std::string &surfaceSectionLabel,
               const std::shared_ptr<PropertyBatchPlan> &surfacePlan, py::sequence surfaceValues,
               float surfaceLabelWidth, uint64_t pickerTextureId, uint64_t previewTextureId,
               const std::string &previewUnavailableLabel, bool defaultOpen, bool readOnly) -> py::tuple {
                if (surfacePlan)
                    UpdatePropertyBatchValues(*surfacePlan, surfaceValues);
                const auto interaction = ctx.RenderMaterialTop(
                    shaderSectionLabel, vertexLabel, vertexDisplay, fragmentLabel, fragmentDisplay,
                    shaderLabelWidth, surfaceSectionLabel, surfacePlan.get(), surfaceLabelWidth,
                    pickerTextureId, previewTextureId, previewUnavailableLabel, defaultOpen, readOnly);
                return py::make_tuple(
                    interaction.vertexFlags, interaction.vertexPickerOpen, interaction.vertexPayload,
                    interaction.vertexListPopupOpen, interaction.fragmentFlags, interaction.fragmentPickerOpen,
                    interaction.fragmentPayload, interaction.fragmentListPopupOpen,
                    EncodePropertyChanges(interaction.surfaceChanges));
            },
            py::arg("shader_section_label"), py::arg("vertex_label"), py::arg("vertex_display"),
            py::arg("fragment_label"), py::arg("fragment_display"), py::arg("shader_label_width"),
            py::arg("surface_section_label"), py::arg("surface_plan"), py::arg("surface_values"),
            py::arg("surface_label_width"), py::arg("picker_texture_id"), py::arg("preview_texture_id"),
            py::arg("preview_unavailable_label"), py::arg("default_open"), py::arg("read_only"),
            "Render the stable material shader/surface/preview header in one native call.")
        .def("render_object_field_chrome", &InxGUIContext::RenderObjectFieldChrome, py::arg("field_id"),
             py::arg("display_text"), py::arg("type_hint"), py::arg("selected") = false,
             py::arg("clickable") = true, py::arg("has_picker") = false, py::arg("picker_texture_id") = 0,
             py::arg("semantic_id") = "",
             "Render steady-state object-reference chrome in one native call; returns interaction flags.")
        .def(
            "render_mesh_renderer_inspector_fields",
            [](InxGUIContext &ctx, const std::string &meshFieldId, const std::string &meshLabel,
               const std::string &meshDisplay, const std::vector<std::string> &slotLabels,
               const std::vector<std::string> &slotDisplays, uint64_t pickerTextureId, float labelWidth) {
                py::list result;
                for (const auto &interaction : ctx.RenderMeshRendererInspectorFields(
                         meshFieldId, meshLabel, meshDisplay, slotLabels, slotDisplays, pickerTextureId, labelWidth)) {
                    py::dict item;
                    item["index"] = interaction.index;
                    item["flags"] = interaction.flags;
                    item["popup_open"] = interaction.popupOpen;
                    item["payload_type"] = interaction.payloadType;
                    item["payload"] = interaction.payload;
                    result.append(std::move(item));
                }
                return result;
            },
            py::arg("mesh_field_id"), py::arg("mesh_label"), py::arg("mesh_display"), py::arg("slot_labels"),
            py::arg("slot_displays"), py::arg("picker_texture_id"), py::arg("label_width"),
            "Render MeshRenderer mesh and material reference fields in one native call.");

    py::class_<InxGUIRenderable, PyGUIRenderable, std::shared_ptr<InxGUIRenderable>>(m, "InxGUIRenderable",
                                                                                     py::dynamic_attr())
        .def(py::init<>());

    // ResourcePreviewManager - manages resource previewers for Inspector
    py::class_<ResourcePreviewManager>(m, "ResourcePreviewManager")
        .def("has_previewer", &ResourcePreviewManager::HasPreviewer, py::arg("extension"),
             "Check if there's a previewer for the given file extension")
        .def("get_previewer_type_name", &ResourcePreviewManager::GetPreviewerTypeName, py::arg("extension"),
             "Get the previewer type name for a file extension")
        .def("get_all_supported_extensions", &ResourcePreviewManager::GetAllSupportedExtensions,
             "Get all supported extensions")
        .def("load_preview", &ResourcePreviewManager::LoadPreview, py::arg("file_path"), "Load a file for preview")
        .def("render_preview", &ResourcePreviewManager::RenderPreview, py::arg("ctx"), py::arg("avail_width"),
             py::arg("avail_height"), "Render the current preview")
        .def("render_metadata", &ResourcePreviewManager::RenderMetadata, py::arg("ctx"),
             "Render metadata for the current preview")
        .def("unload_preview", &ResourcePreviewManager::UnloadPreview, "Unload the current preview")
        .def("is_preview_loaded", &ResourcePreviewManager::IsPreviewLoaded, "Check if a preview is currently loaded")
        .def("get_loaded_path", &ResourcePreviewManager::GetLoadedPath, "Get the currently loaded file path")
        .def("get_current_type_name", &ResourcePreviewManager::GetCurrentTypeName,
             "Get the current previewer type name")
        .def("set_preview_settings", &ResourcePreviewManager::SetPreviewSettings, py::arg("display_mode"),
             py::arg("max_size"), py::arg("srgb"), "Set preview settings (display mode, max size, sRGB)");

    // EditorPanel — C++ base class for native panels
    py::class_<EditorPanel, InxGUIRenderable, std::shared_ptr<EditorPanel>>(m, "EditorPanel", py::dynamic_attr())
        .def("is_open", &EditorPanel::IsOpen, "Check if the panel is open")
        .def("set_open", &EditorPanel::SetOpen, py::arg("open"), "Set whether the panel is open")
        .def("get_window_id", &EditorPanel::GetWindowId, "Get the stable window ID");

    // ConsolePanel — C++ native console that replaces the Python ConsolePanel
    py::class_<ConsolePanel, EditorPanel, std::shared_ptr<ConsolePanel>>(m, "ConsolePanel")
        .def(py::init<>())
        .def("log_from_python", &ConsolePanel::LogFromPython, py::arg("level"), py::arg("message"),
             py::arg("stack_trace") = "", py::arg("source_file") = "", py::arg("source_line") = 0,
             "Log a message originating from Python Debug.log()")
        .def("clear", &ConsolePanel::Clear, "Clear all log entries")
        .def("get_info_count", &ConsolePanel::GetInfoCount, "Get count of info messages")
        .def("get_warning_count", &ConsolePanel::GetWarningCount, "Get count of warning messages")
        .def("get_error_count", &ConsolePanel::GetErrorCount, "Get count of error messages")
        .def("select_latest_entry", &ConsolePanel::SelectLatestEntry, "Select last visible entry and focus window")
        .def("_select_entry", &ConsolePanel::SelectEntry, py::arg("uid"))
        .def_property_readonly("_selected_uid", &ConsolePanel::GetSelectedUid)
        .def_property_readonly("_revision", &ConsolePanel::GetRevision)
        .def("_get_status_snapshot",
             [](ConsolePanel &self) {
                 std::string message;
                 std::string level;
                 int infoCount = 0;
                 int warningCount = 0;
                 int errorCount = 0;
                 uint64_t uid = 0;
                 self.GetStatusBarSnapshot(message, level, infoCount, warningCount, errorCount, uid);
                 return py::make_tuple(message, level, infoCount, warningCount, errorCount, uid);
             })
        .def_readwrite("show_info", &ConsolePanel::showInfo)
        .def_readwrite("show_warnings", &ConsolePanel::showWarnings)
        .def_readwrite("show_errors", &ConsolePanel::showErrors)
        .def_readwrite("collapse", &ConsolePanel::collapse)
        .def_readwrite("clear_on_play", &ConsolePanel::clearOnPlay)
        .def_readwrite("error_pause", &ConsolePanel::errorPause)
        .def_readwrite("auto_scroll", &ConsolePanel::autoScroll)
        .def_readwrite("on_double_click_entry", &ConsolePanel::onDoubleClickEntry)
        .def_readwrite("on_error_pause", &ConsolePanel::onErrorPause)
        .def_readwrite("on_request_focus", &ConsolePanel::onRequestFocus);

    // ── PlayState enum ─────────────────────────────────────────────────
    py::enum_<PlayState>(m, "PlayState")
        .value("Edit", PlayState::Edit)
        .value("Playing", PlayState::Playing)
        .value("Paused", PlayState::Paused);

    // ── WindowTypeInfo ─────────────────────────────────────────────────
    py::class_<WindowTypeInfo>(m, "WindowTypeInfo")
        .def(py::init<>())
        .def_readwrite("type_id", &WindowTypeInfo::typeId)
        .def_readwrite("display_name", &WindowTypeInfo::displayName)
        .def_readwrite("menu_path", &WindowTypeInfo::menuPath)
        .def_readwrite("singleton", &WindowTypeInfo::singleton);

    // ── StatusBarPanel ─────────────────────────────────────────────────
    py::class_<StatusBarPanel, InxGUIRenderable, std::shared_ptr<StatusBarPanel>>(m, "StatusBarPanel")
        .def(py::init<>())
        .def(
            "set_console_panel",
            [](StatusBarPanel &self, std::shared_ptr<ConsolePanel> panel) { self.SetConsolePanel(panel.get()); },
            py::arg("console"), py::keep_alive<1, 2>(), "Wire to ConsolePanel for click-to-select-latest")
        .def("set_engine_status", &StatusBarPanel::SetEngineStatus, py::arg("text"), py::arg("progress"),
             py::arg("kind") = "activity", "Update engine-status indicator");

    // ── ToolbarPanel ───────────────────────────────────────────────────
    py::class_<ToolbarPanel, EditorPanel, std::shared_ptr<ToolbarPanel>>(m, "ToolbarPanel")
        .def(py::init<>())
        .def_readwrite("on_play", &ToolbarPanel::onPlay)
        .def_readwrite("on_pause", &ToolbarPanel::onPause)
        .def_readwrite("on_step", &ToolbarPanel::onStep)
        .def_readwrite("get_play_state", &ToolbarPanel::getPlayState)
        .def_readwrite("get_play_time_str", &ToolbarPanel::getPlayTimeStr)
        .def_readwrite("is_show_grid", &ToolbarPanel::isShowGrid)
        .def_readwrite("set_show_grid", &ToolbarPanel::setShowGrid)
        .def_readwrite("translate", &ToolbarPanel::translate)
        .def(
            "get_camera_settings",
            [](const ToolbarPanel &self) -> py::dict {
                auto s = self.GetCameraSettings();
                py::dict d;
                d["orthographic"] = s.orthographic;
                d["fov"] = s.fov;
                d["orthographic_size"] = s.orthographicSize;
                d["rotation_speed"] = s.rotationSpeed;
                d["pan_speed"] = s.panSpeed;
                d["zoom_speed"] = s.zoomSpeed;
                d["move_speed"] = s.moveSpeed;
                d["move_speed_boost"] = s.moveSpeedBoost;
                return d;
            },
            "Get camera settings as dict")
        .def(
            "set_camera_settings",
            [](ToolbarPanel &self, py::dict d) {
                ToolbarPanel::CameraSettings s;
                s.orthographic = d.contains("orthographic") ? d["orthographic"].cast<bool>() : false;
                s.fov = d.contains("fov") ? d["fov"].cast<float>() : 60.0f;
                s.orthographicSize = d.contains("orthographic_size") ? d["orthographic_size"].cast<float>() : 5.0f;
                s.rotationSpeed = d.contains("rotation_speed") ? d["rotation_speed"].cast<float>() : 0.05f;
                s.panSpeed = d.contains("pan_speed") ? d["pan_speed"].cast<float>() : 1.0f;
                s.zoomSpeed = d.contains("zoom_speed") ? d["zoom_speed"].cast<float>() : 1.0f;
                s.moveSpeed = d.contains("move_speed") ? d["move_speed"].cast<float>() : 5.0f;
                s.moveSpeedBoost = d.contains("move_speed_boost") ? d["move_speed_boost"].cast<float>() : 3.0f;
                self.SetCameraSettings(s);
            },
            py::arg("settings"), "Set camera settings from dict")
        .def_property(
            "sync_camera_from_engine", [](const ToolbarPanel &self) -> py::object { return py::none(); },
            [](ToolbarPanel &self, py::function fn) {
                self.syncCameraFromEngine = [fn]() -> ToolbarPanel::CameraSettings {
                    py::gil_scoped_acquire acquire;
                    py::dict d = fn();
                    ToolbarPanel::CameraSettings s;
                    s.orthographic = d.contains("orthographic") ? d["orthographic"].cast<bool>() : false;
                    s.fov = d.contains("fov") ? d["fov"].cast<float>() : 60.0f;
                    s.orthographicSize = d.contains("orthographic_size") ? d["orthographic_size"].cast<float>() : 5.0f;
                    s.rotationSpeed = d.contains("rotation_speed") ? d["rotation_speed"].cast<float>() : 0.05f;
                    s.panSpeed = d.contains("pan_speed") ? d["pan_speed"].cast<float>() : 1.0f;
                    s.zoomSpeed = d.contains("zoom_speed") ? d["zoom_speed"].cast<float>() : 1.0f;
                    s.moveSpeed = d.contains("move_speed") ? d["move_speed"].cast<float>() : 5.0f;
                    s.moveSpeedBoost = d.contains("move_speed_boost") ? d["move_speed_boost"].cast<float>() : 3.0f;
                    return s;
                };
            },
            "Set a Python callback that returns camera settings dict")
        .def_property(
            "apply_camera_to_engine", [](const ToolbarPanel &self) -> py::object { return py::none(); },
            [](ToolbarPanel &self, py::function fn) {
                self.applyCameraToEngine = [fn](const ToolbarPanel::CameraSettings &s) {
                    py::gil_scoped_acquire acquire;
                    py::dict d;
                    d["orthographic"] = s.orthographic;
                    d["fov"] = s.fov;
                    d["orthographic_size"] = s.orthographicSize;
                    d["rotation_speed"] = s.rotationSpeed;
                    d["pan_speed"] = s.panSpeed;
                    d["zoom_speed"] = s.zoomSpeed;
                    d["move_speed"] = s.moveSpeed;
                    d["move_speed_boost"] = s.moveSpeedBoost;
                    fn(d);
                };
            },
            "Set a Python callback that receives camera settings dict");

    // ── MenuBarPanel ───────────────────────────────────────────────────
    py::class_<MenuBarPanel, InxGUIRenderable, std::shared_ptr<MenuBarPanel>>(m, "MenuBarPanel")
        .def(py::init<>())
        .def("invalidate_window_type_cache", &MenuBarPanel::InvalidateWindowTypeCache)
        .def_readwrite("on_save", &MenuBarPanel::onSave)
        .def_readwrite("on_save_as", &MenuBarPanel::onSaveAs)
        .def_readwrite("on_save_focused", &MenuBarPanel::onSaveFocused)
        .def_readwrite("on_save_focused_as", &MenuBarPanel::onSaveFocusedAs)
        .def_readwrite("on_new_scene", &MenuBarPanel::onNewScene)
        .def_readwrite("on_request_close", &MenuBarPanel::onRequestClose)
        .def_readwrite("on_undo", &MenuBarPanel::onUndo)
        .def_readwrite("on_redo", &MenuBarPanel::onRedo)
        .def_readwrite("can_undo", &MenuBarPanel::canUndo)
        .def_readwrite("can_redo", &MenuBarPanel::canRedo)
        .def_readwrite("get_registered_types", &MenuBarPanel::getRegisteredTypes)
        .def_readwrite("get_open_windows", &MenuBarPanel::getOpenWindows)
        .def_readwrite("open_window", &MenuBarPanel::openWindow)
        .def_readwrite("close_window", &MenuBarPanel::closeWindow)
        .def_readwrite("reset_layout", &MenuBarPanel::resetLayout)
        .def_readwrite("is_close_requested", &MenuBarPanel::isCloseRequested)
        .def_readwrite("toggle_build_settings", &MenuBarPanel::toggleBuildSettings)
        .def_readwrite("toggle_preferences", &MenuBarPanel::togglePreferences)
        .def_readwrite("toggle_physics_layer_matrix", &MenuBarPanel::togglePhysicsLayerMatrix)
        .def_readwrite("toggle_environment_settings", &MenuBarPanel::toggleEnvironmentSettings)
        .def_readwrite("is_build_settings_open", &MenuBarPanel::isBuildSettingsOpen)
        .def_readwrite("is_preferences_open", &MenuBarPanel::isPreferencesOpen)
        .def_readwrite("is_physics_layer_matrix_open", &MenuBarPanel::isPhysicsLayerMatrixOpen)
        .def_readwrite("is_environment_settings_open", &MenuBarPanel::isEnvironmentSettingsOpen)
        .def_readwrite("translate", &MenuBarPanel::translate);

    // ── HierarchyPanel ─────────────────────────────────────────────────
    py::class_<HierarchyPanel, EditorPanel, std::shared_ptr<HierarchyPanel>>(m, "HierarchyPanel")
        .def(py::init<>())
        // Public API
        .def("set_ui_mode", &HierarchyPanel::SetUiMode, py::arg("enabled"))
        .def("get_ui_mode", &HierarchyPanel::GetUiMode)
        .def_property("ui_mode", &HierarchyPanel::GetUiMode, &HierarchyPanel::SetUiMode)
        .def("clear_search", &HierarchyPanel::ClearSearch)
        .def("clear_selection_and_notify", &HierarchyPanel::ClearSelectionAndNotify)
        .def("set_selected_object_by_id", &HierarchyPanel::SetSelectedObjectById, py::arg("id"),
             py::arg("clear_search") = false)
        .def("set_selection_snapshot", &HierarchyPanel::SetSelectionSnapshot, py::arg("ids"), py::arg("primary"))
        .def("set_runtime_hidden_ids", &HierarchyPanel::SetRuntimeHiddenIds, py::arg("ids"))
        .def("set_scene_header_snapshot", &HierarchyPanel::SetSceneHeaderSnapshot, py::arg("scene_display_name"),
             py::arg("prefab_mode"), py::arg("prefab_display_name"))
        .def("expand_to_object", &HierarchyPanel::ExpandToObject, py::arg("obj_id"))
        .def("set_pending_expand_id", &HierarchyPanel::SetPendingExpandId, py::arg("obj_id"))
        .def("invalidate_scene_structure_cache", &HierarchyPanel::InvalidateSceneStructureCache)
        // Selection callbacks
        .def_readwrite("is_selected", &HierarchyPanel::isSelected)
        .def_readwrite("select_id", &HierarchyPanel::selectId)
        .def_readwrite("toggle_id", &HierarchyPanel::toggleId)
        .def_readwrite("range_select_id", &HierarchyPanel::rangeSelectId)
        .def_readwrite("clear_selection", &HierarchyPanel::clearSelection)
        .def_readwrite("get_primary", &HierarchyPanel::getPrimary)
        .def_readwrite("get_selected_ids", &HierarchyPanel::getSelectedIds)
        .def_readwrite("selection_count", &HierarchyPanel::selectionCount)
        .def_readwrite("is_selection_empty", &HierarchyPanel::isSelectionEmpty)
        .def_readwrite("set_ordered_ids", &HierarchyPanel::setOrderedIds)
        // Notification callbacks
        .def_readwrite("on_selection_changed", &HierarchyPanel::onSelectionChanged)
        .def_readwrite("on_double_click_focus", &HierarchyPanel::onDoubleClickFocus)
        .def_readwrite("on_selection_changed_ui_editor", &HierarchyPanel::onSelectionChangedUiEditor)
        // Undo callbacks
        .def_readwrite("undo_record_create", &HierarchyPanel::undoRecordCreate)
        .def_readwrite("undo_record_delete", &HierarchyPanel::undoRecordDelete)
        .def_readwrite("undo_record_rename", &HierarchyPanel::undoRecordRename)
        .def_readwrite("undo_record_move", &HierarchyPanel::undoRecordMove)
        // Scene info callbacks
        .def_readwrite("get_scene_display_name", &HierarchyPanel::getSceneDisplayName)
        .def_readwrite("is_prefab_mode", &HierarchyPanel::isPrefabMode)
        .def_readwrite("get_prefab_display_name", &HierarchyPanel::getPrefabDisplayName)
        // Runtime hidden
        .def_readwrite("get_runtime_hidden_ids", &HierarchyPanel::getRuntimeHiddenIds)
        // Canvas / UI-mode queries
        .def_readwrite("go_has_canvas", &HierarchyPanel::goHasCanvas)
        .def_readwrite("go_has_ui_screen_component", &HierarchyPanel::goHasUiScreenComponent)
        .def_readwrite("parent_has_canvas_ancestor", &HierarchyPanel::parentHasCanvasAncestor)
        .def_readwrite("has_canvas_descendant", &HierarchyPanel::hasCanvasDescendant)
        .def_readwrite("get_canvas_root_ids", &HierarchyPanel::getCanvasRootIds)
        // Context-menu action callbacks
        .def_readwrite("create_primitive", &HierarchyPanel::createPrimitive)
        .def_readwrite("create_light", &HierarchyPanel::createLight)
        .def_readwrite("create_empty", &HierarchyPanel::createEmpty)
        .def_readwrite("create_empty_parent", &HierarchyPanel::createEmptyParent)
        .def("add_create_entry", &HierarchyPanel::AddCreateEntry, py::arg("category"), py::arg("locale_key"),
             py::arg("callback"), "Register a data-driven entry for the Hierarchy context menu")
        .def("clear_create_entries", &HierarchyPanel::ClearCreateEntries, "Remove all data-driven create-menu entries")
        .def_readwrite("save_as_prefab", &HierarchyPanel::saveAsPrefab)
        .def_readwrite("prefab_select_asset", &HierarchyPanel::prefabSelectAsset)
        .def_readwrite("prefab_open_asset", &HierarchyPanel::prefabOpenAsset)
        .def_readwrite("prefab_apply_overrides", &HierarchyPanel::prefabApplyOverrides)
        .def_readwrite("prefab_revert_overrides", &HierarchyPanel::prefabRevertOverrides)
        .def_readwrite("prefab_unpack", &HierarchyPanel::prefabUnpack)
        // Hierarchy panel focus callback
        .def_readwrite("on_hierarchy_panel_focused", &HierarchyPanel::onHierarchyPanelFocused)
        // Clipboard callbacks
        .def_readwrite("copy_selected", &HierarchyPanel::copySelected)
        .def_readwrite("paste_clipboard", &HierarchyPanel::pasteClipboard)
        .def_readwrite("has_clipboard_data", &HierarchyPanel::hasClipboardData)
        // External drop callbacks
        .def_readwrite("instantiate_prefab", &HierarchyPanel::instantiatePrefab)
        .def_readwrite("create_model_object", &HierarchyPanel::createModelObject)
        // Delete
        .def_readwrite("delete_selected_objects", &HierarchyPanel::deleteSelectedObjects)
        // Translation
        .def_readwrite("translate", &HierarchyPanel::translate)
        // Warning
        .def_readwrite("show_warning", &HierarchyPanel::showWarning);

    // ── ProjectPanel ───────────────────────────────────────────────────
    py::class_<ProjectPanel, EditorPanel, std::shared_ptr<ProjectPanel>>(m, "ProjectPanel")
        .def(py::init<>())
        // Public API
        .def("set_root_path", &ProjectPanel::SetRootPath, py::arg("path"))
        .def(
            "setup_from_engine",
            [](ProjectPanel &self, Infernux &engine) {
                self.SetEngine(&engine);
                self.SetRenderer(engine.GetRenderer());
                self.SetAssetDatabase(engine.GetAssetDatabase());
            },
            py::arg("engine"))
        .def("set_icons_directory", &ProjectPanel::SetIconsDirectory, py::arg("dir"))
        .def("clear_selection", &ProjectPanel::ClearSelection)
        .def("set_selected_file", &ProjectPanel::SetSelectedFile, py::arg("path"))
        .def("invalidate_material_thumbnail", &ProjectPanel::InvalidateMaterialThumbnail, py::arg("file_path"))
        .def("invalidate_texture_thumbnail", &ProjectPanel::InvalidateTextureThumbnail, py::arg("file_path"))
        .def("invalidate_dir_cache", &ProjectPanel::InvalidateDirCache)
        .def("receive_dropped_files", &ProjectPanel::ReceiveDroppedFiles, py::arg("paths"))
        .def("get_current_path", &ProjectPanel::GetCurrentPath)
        .def("set_current_path", &ProjectPanel::SetCurrentPath, py::arg("path"))
        // Project panel focus callback
        .def_readwrite("on_project_panel_focused", &ProjectPanel::onProjectPanelFocused)
        // Notification callbacks
        .def_readwrite("on_file_selected", &ProjectPanel::onFileSelected)
        .def_readwrite("on_empty_area_clicked", &ProjectPanel::onEmptyAreaClicked)
        .def_readwrite("on_state_changed", &ProjectPanel::onStateChanged)
        // File operation callbacks
        .def_readwrite("create_folder", &ProjectPanel::createFolder)
        .def_readwrite("create_script", &ProjectPanel::createScript)
        .def_readwrite("create_shader", &ProjectPanel::createShader)
        .def_readwrite("create_material", &ProjectPanel::createMaterial)
        .def_readwrite("create_physic_material", &ProjectPanel::createPhysicMaterial)
        .def_readwrite("create_scene", &ProjectPanel::createScene)
        .def_readwrite("create_animclip", &ProjectPanel::createAnimClip)
        .def_readwrite("create_animclip3d", &ProjectPanel::createAnimClip3D)
        .def_readwrite("create_animfsm", &ProjectPanel::createAnimFsm)
        .def_readwrite("create_particlegraph", &ProjectPanel::createParticleGraph)
        .def_readwrite("create_render_effect", &ProjectPanel::createRenderEffect)
        .def_readwrite("create_render_effect_group", &ProjectPanel::createRenderEffectGroup)
        .def_readwrite("create_animtimeline", &ProjectPanel::createAnimTimeline)
        .def_readwrite("create_timelinefsm", &ProjectPanel::createTimelineFsm)
        .def_readwrite("create_prefab_from_hierarchy", &ProjectPanel::createPrefabFromHierarchy)
        .def_readwrite("delete_items", &ProjectPanel::deleteItems)
        .def_readwrite("do_rename", &ProjectPanel::doRename)
        .def_readwrite("get_unique_name", &ProjectPanel::getUniqueName)
        .def_readwrite("move_item_to_directory", &ProjectPanel::moveItemToDirectory)
        // Open/Reveal callbacks
        .def_readwrite("open_file", &ProjectPanel::openFile)
        .def_readwrite("open_scene", &ProjectPanel::openScene)
        .def_readwrite("open_prefab_mode", &ProjectPanel::openPrefabMode)
        .def_readwrite("open_anim_clip", &ProjectPanel::openAnimClip)
        .def_readwrite("open_anim_fsm", &ProjectPanel::openAnimFsm)
        .def_readwrite("open_particle_graph", &ProjectPanel::openParticleGraph)
        .def_readwrite("open_anim_timeline", &ProjectPanel::openAnimTimeline)
        .def_readwrite("open_timeline_fsm", &ProjectPanel::openTimelineFsm)
        .def_readwrite("reveal_in_explorer", &ProjectPanel::revealInExplorer)
        // Validation / GUID callbacks
        .def_readwrite("validate_script_component", &ProjectPanel::validateScriptComponent)
        .def_readwrite("get_guid_from_path", &ProjectPanel::getGuidFromPath)
        .def_readwrite("get_path_from_guid", &ProjectPanel::getPathFromGuid)
        .def_readwrite("invalidate_asset_inspector", &ProjectPanel::invalidateAssetInspector)
        .def_readwrite("is_hierarchy_selection_empty", &ProjectPanel::isHierarchySelectionEmpty)
        // Translation
        .def_readwrite("translate", &ProjectPanel::translate);

    // ── InspectorPanel ─────────────────────────────────────────────────
    py::class_<ComponentInfo>(m, "InspectorComponentInfo")
        .def(py::init<>())
        .def_readwrite("type_name", &ComponentInfo::typeName)
        .def_readwrite("display_name", &ComponentInfo::displayName)
        .def_readwrite("component_id", &ComponentInfo::componentId)
        .def_readwrite("enabled", &ComponentInfo::enabled)
        .def_readwrite("is_native", &ComponentInfo::isNative)
        .def_readwrite("is_script", &ComponentInfo::isScript)
        .def_readwrite("is_broken", &ComponentInfo::isBroken)
        .def_readwrite("broken_error", &ComponentInfo::brokenError)
        .def_readwrite("icon_id", &ComponentInfo::iconId);

    py::class_<InspectorPanel::ObjectInfo>(m, "InspectorObjectInfo")
        .def(py::init<>())
        .def_readwrite("name", &InspectorPanel::ObjectInfo::name)
        .def_readwrite("active", &InspectorPanel::ObjectInfo::active)
        .def_readwrite("tag", &InspectorPanel::ObjectInfo::tag)
        .def_readwrite("layer", &InspectorPanel::ObjectInfo::layer)
        .def_readwrite("prefab_guid", &InspectorPanel::ObjectInfo::prefabGuid)
        .def_readwrite("hide_transform", &InspectorPanel::ObjectInfo::hideTransform);

    py::class_<InspectorPanel::TransformData>(m, "InspectorTransformData")
        .def(py::init<>())
        .def_readwrite("px", &InspectorPanel::TransformData::px)
        .def_readwrite("py_", &InspectorPanel::TransformData::py) // avoid shadow of pybind11 py
        .def_readwrite("pz", &InspectorPanel::TransformData::pz)
        .def_readwrite("rx", &InspectorPanel::TransformData::rx)
        .def_readwrite("ry", &InspectorPanel::TransformData::ry)
        .def_readwrite("rz", &InspectorPanel::TransformData::rz)
        .def_readwrite("sx", &InspectorPanel::TransformData::sx)
        .def_readwrite("sy", &InspectorPanel::TransformData::sy)
        .def_readwrite("sz", &InspectorPanel::TransformData::sz);

    py::class_<InspectorPanel::AddComponentEntry>(m, "InspectorAddComponentEntry")
        .def(py::init<>())
        .def_readwrite("display_name", &InspectorPanel::AddComponentEntry::displayName)
        .def_readwrite("category", &InspectorPanel::AddComponentEntry::category)
        .def_readwrite("is_native", &InspectorPanel::AddComponentEntry::isNative)
        .def_readwrite("script_path", &InspectorPanel::AddComponentEntry::scriptPath);

    py::class_<InspectorPanel::PrefabInfo>(m, "InspectorPrefabInfo")
        .def(py::init<>())
        .def_readwrite("override_count", &InspectorPanel::PrefabInfo::overrideCount)
        .def_readwrite("is_readonly", &InspectorPanel::PrefabInfo::isReadonly)
        .def_readwrite("is_transform_readonly", &InspectorPanel::PrefabInfo::isTransformReadonly);

    py::class_<InspectorPanel, EditorPanel, std::shared_ptr<InspectorPanel>>(m, "InspectorPanel")
        .def(py::init<>())
        // Public API
        .def("set_selected_object_id", &InspectorPanel::SetSelectedObjectId, py::arg("id"))
        .def("clear_selected_object", &InspectorPanel::ClearSelectedObject)
        .def("get_selected_object_id", &InspectorPanel::GetSelectedObjectId)
        .def("set_selected_file", &InspectorPanel::SetSelectedFile, py::arg("file_path"), py::arg("category"))
        .def("clear_selected_file", &InspectorPanel::ClearSelectedFile)
        .def("get_selected_file", &InspectorPanel::GetSelectedFile)
        .def("set_detail_file", &InspectorPanel::SetDetailFile, py::arg("file_path"), py::arg("category"))
        // Selection callbacks
        .def_readwrite("is_multi_selection", &InspectorPanel::isMultiSelection)
        .def_readwrite("get_selected_ids", &InspectorPanel::getSelectedIds)
        .def_readwrite("get_value_generation", &InspectorPanel::getValueGeneration)
        // Object info callbacks
        .def_readwrite("get_object_info", &InspectorPanel::getObjectInfo)
        .def_readwrite("set_object_property", &InspectorPanel::setObjectProperty)
        // Transform callbacks
        .def_readwrite("get_transform_data", &InspectorPanel::getTransformData)
        .def_readwrite("set_transform_data", &InspectorPanel::setTransformData)
        // Component enumeration
        .def_readwrite("get_component_list", &InspectorPanel::getComponentList)
        .def_readwrite("get_component_icon_id", &InspectorPanel::getComponentIconId)
        // Component body rendering
        .def_readwrite("render_component_body", &InspectorPanel::renderComponentBody)
        .def_readwrite("render_multi_component_body", &InspectorPanel::renderMultiComponentBody)
        .def_readwrite("consume_component_body_profile", &InspectorPanel::consumeComponentBodyProfile)
        .def_readwrite("render_component_context_menu", &InspectorPanel::renderComponentContextMenu)
        .def_readwrite("set_component_enabled", &InspectorPanel::setComponentEnabled)
        // Add Component
        .def_readwrite("get_add_component_entries", &InspectorPanel::getAddComponentEntries)
        .def_readwrite("add_component", &InspectorPanel::addComponent)
        // Remove Component
        .def_readwrite("remove_component", &InspectorPanel::removeComponent)
        // Asset / File preview
        .def_readwrite("render_asset_inspector", &InspectorPanel::renderAssetInspector)
        .def_readwrite("render_file_preview", &InspectorPanel::renderFilePreview)
        // Material sections
        .def_readwrite("render_material_sections", &InspectorPanel::renderMaterialSections)
        // Prefab
        .def_readwrite("get_prefab_info", &InspectorPanel::getPrefabInfo)
        .def_readwrite("prefab_action", &InspectorPanel::prefabAction)
        // Undo
        .def_readwrite("undo_begin_frame", &InspectorPanel::undoBeginFrame)
        .def_readwrite("undo_end_frame", &InspectorPanel::undoEndFrame)
        .def_readwrite("undo_invalidate_all", &InspectorPanel::undoInvalidateAll)
        // Tag & Layer
        .def_readwrite("get_all_tags", &InspectorPanel::getAllTags)
        .def_readwrite("get_all_layers", &InspectorPanel::getAllLayers)
        // Translation
        .def_readwrite("translate", &InspectorPanel::translate)
        // Script drop
        .def_readwrite("handle_script_drop", &InspectorPanel::handleScriptDrop)
        // Window manager
        .def_readwrite("open_window", &InspectorPanel::openWindow);
}

} // namespace infernux
