#include "MaterialDocumentValidation.h"

#include "InxMaterial.h"

#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>
#include <unordered_set>

namespace infernux::material_document_validation
{
namespace
{

using json = nlohmann::json;

[[noreturn]] void Fail(std::string_view path, const std::string &message)
{
    throw std::invalid_argument(std::string(path) + ": " + message);
}

void RequireExactFields(const json &document, const std::unordered_set<std::string> &required,
                        const std::unordered_set<std::string> &optional, std::string_view path)
{
    if (!document.is_object())
        Fail(path, "must be an object");
    for (const auto &field : required) {
        if (!document.contains(field))
            Fail(path, "missing required field '" + field + "'");
    }
    for (const auto &[field, value] : document.items()) {
        (void)value;
        if (required.find(field) == required.end() && optional.find(field) == optional.end())
            Fail(path, "contains unknown field '" + field + "'");
    }
}

int RequireInteger(const json &document, const char *field, std::string_view path)
{
    if (!document[field].is_number_integer())
        Fail(path, std::string(field) + " must be an integer");
    try {
        return document[field].get<int>();
    } catch (const std::exception &) {
        Fail(path, std::string(field) + " is outside the supported integer range");
    }
}

uint32_t RequireUnsigned(const json &document, const char *field, std::string_view path)
{
    if (!document[field].is_number_unsigned())
        Fail(path, std::string(field) + " must be an unsigned integer");
    const uint64_t value = document[field].get<uint64_t>();
    if (value > std::numeric_limits<uint32_t>::max())
        Fail(path, std::string(field) + " exceeds uint32 range");
    return static_cast<uint32_t>(value);
}

double RequireFiniteNumber(const json &document, const char *field, std::string_view path)
{
    if (!document[field].is_number())
        Fail(path, std::string(field) + " must be a number");
    const double value = document[field].get<double>();
    if (!std::isfinite(value) || std::abs(value) > std::numeric_limits<float>::max())
        Fail(path, std::string(field) + " must be a finite float");
    return value;
}

void RequireIntegerRange(const json &document, const char *field, int minimum, int maximum, std::string_view path)
{
    const int value = RequireInteger(document, field, path);
    if (value < minimum || value > maximum)
        Fail(path, std::string(field) + " is out of range");
}

void ValidateStencil(const json &document, std::string_view path)
{
    static const std::unordered_set<std::string> required = {
        "failOp", "passOp", "depthFailOp", "compareOp", "compareMask", "writeMask", "reference",
    };
    RequireExactFields(document, required, {}, path);
    RequireIntegerRange(document, "failOp", VK_STENCIL_OP_KEEP, VK_STENCIL_OP_DECREMENT_AND_WRAP, path);
    RequireIntegerRange(document, "passOp", VK_STENCIL_OP_KEEP, VK_STENCIL_OP_DECREMENT_AND_WRAP, path);
    RequireIntegerRange(document, "depthFailOp", VK_STENCIL_OP_KEEP, VK_STENCIL_OP_DECREMENT_AND_WRAP, path);
    RequireIntegerRange(document, "compareOp", VK_COMPARE_OP_NEVER, VK_COMPARE_OP_ALWAYS, path);
    RequireUnsigned(document, "compareMask", path);
    RequireUnsigned(document, "writeMask", path);
    RequireUnsigned(document, "reference", path);
}

void ValidateRenderState(const json &document, std::string_view path)
{
    static const std::unordered_set<std::string> required = {
        "cullMode",
        "frontFace",
        "polygonMode",
        "lineWidth",
        "depthBiasEnable",
        "depthBiasConstantFactor",
        "depthBiasSlopeFactor",
        "depthBiasClamp",
        "topology",
        "depthTestEnable",
        "depthWriteEnable",
        "depthCompareOp",
        "blendEnable",
        "srcColorBlendFactor",
        "dstColorBlendFactor",
        "colorBlendOp",
        "srcAlphaBlendFactor",
        "dstAlphaBlendFactor",
        "alphaBlendOp",
        "alphaClipEnabled",
        "alphaClipThreshold",
        "renderQueue",
        "stencilTestEnable",
    };
    static const std::unordered_set<std::string> optional = {"stencilFront", "stencilBack"};
    RequireExactFields(document, required, optional, path);

    for (const char *field : {"depthBiasEnable", "depthTestEnable", "depthWriteEnable", "blendEnable",
                              "alphaClipEnabled", "stencilTestEnable"}) {
        if (!document[field].is_boolean())
            Fail(path, std::string(field) + " must be a boolean");
    }

    RequireIntegerRange(document, "cullMode", VK_CULL_MODE_NONE, VK_CULL_MODE_FRONT_AND_BACK, path);
    RequireIntegerRange(document, "frontFace", VK_FRONT_FACE_COUNTER_CLOCKWISE, VK_FRONT_FACE_CLOCKWISE, path);
    RequireIntegerRange(document, "polygonMode", VK_POLYGON_MODE_FILL, VK_POLYGON_MODE_POINT, path);
    RequireIntegerRange(document, "topology", VK_PRIMITIVE_TOPOLOGY_POINT_LIST, VK_PRIMITIVE_TOPOLOGY_PATCH_LIST, path);
    RequireIntegerRange(document, "depthCompareOp", VK_COMPARE_OP_NEVER, VK_COMPARE_OP_ALWAYS, path);
    for (const char *field :
         {"srcColorBlendFactor", "dstColorBlendFactor", "srcAlphaBlendFactor", "dstAlphaBlendFactor"}) {
        RequireIntegerRange(document, field, VK_BLEND_FACTOR_ZERO, VK_BLEND_FACTOR_ONE_MINUS_SRC1_ALPHA, path);
    }
    RequireIntegerRange(document, "colorBlendOp", VK_BLEND_OP_ADD, VK_BLEND_OP_MAX, path);
    RequireIntegerRange(document, "alphaBlendOp", VK_BLEND_OP_ADD, VK_BLEND_OP_MAX, path);

    const double lineWidth = RequireFiniteNumber(document, "lineWidth", path);
    RequireFiniteNumber(document, "depthBiasConstantFactor", path);
    RequireFiniteNumber(document, "depthBiasSlopeFactor", path);
    RequireFiniteNumber(document, "depthBiasClamp", path);
    const double alphaClipThreshold = RequireFiniteNumber(document, "alphaClipThreshold", path);
    if (lineWidth <= 0.0)
        Fail(path, "lineWidth must be positive");
    if (alphaClipThreshold < 0.0 || alphaClipThreshold > 1.0)
        Fail(path, "alphaClipThreshold must be in [0, 1]");
    if (RequireInteger(document, "renderQueue", path) < 0)
        Fail(path, "renderQueue must be non-negative");

    const bool stencilEnabled = document["stencilTestEnable"].get<bool>();
    if (stencilEnabled) {
        if (!document.contains("stencilFront") || !document.contains("stencilBack"))
            Fail(path, "enabled stencil state requires stencilFront and stencilBack");
        ValidateStencil(document["stencilFront"], std::string(path) + ".stencilFront");
        ValidateStencil(document["stencilBack"], std::string(path) + ".stencilBack");
    } else if (document.contains("stencilFront") || document.contains("stencilBack")) {
        Fail(path, "disabled stencil state must not contain stencil documents");
    }
}

void ValidateProperty(const std::string &name, const json &document, std::string_view path)
{
    static const std::unordered_set<std::string> textureFields = {"type", "guid"};
    static const std::unordered_set<std::string> valueFields = {"type", "value"};
    static const std::unordered_set<std::string> metadataFields = {"hdr", "range"};
    if (name.empty())
        Fail(path, "property name must not be empty");
    if (!document.is_object() || !document.contains("type") || !document["type"].is_number_integer())
        Fail(path, "property must contain an integer type");
    const int type = RequireInteger(document, "type", path);
    if (type < static_cast<int>(MaterialPropertyType::Float) || type > static_cast<int>(MaterialPropertyType::Color)) {
        Fail(path, "property type is out of range");
    }

    const auto propertyType = static_cast<MaterialPropertyType>(type);
    if (document.contains("hdr") && !document["hdr"].is_boolean())
        Fail(path, "hdr must be a boolean");
    if (document.contains("range")) {
        const auto &range = document["range"];
        if (propertyType != MaterialPropertyType::Float && propertyType != MaterialPropertyType::Int)
            Fail(path, "range is only valid for Float and Int properties");
        if (!range.is_array() || range.size() != 2 || !range[0].is_number() || !range[1].is_number())
            Fail(path, "range must contain exactly two numbers");
        const double minimum = range[0].get<double>();
        const double maximum = range[1].get<double>();
        if (!std::isfinite(minimum) || !std::isfinite(maximum) || minimum >= maximum)
            Fail(path, "range bounds must be finite and strictly increasing");
        if (propertyType == MaterialPropertyType::Int &&
            (!range[0].is_number_integer() || !range[1].is_number_integer()))
            Fail(path, "Int property range bounds must be integers");
    }
    if (propertyType == MaterialPropertyType::Texture2D) {
        RequireExactFields(document, textureFields, metadataFields, path);
        if (!document["guid"].is_string())
            Fail(path, "guid must be a string");
        return;
    }

    RequireExactFields(document, valueFields, metadataFields, path);
    if (propertyType == MaterialPropertyType::Int) {
        RequireInteger(document, "value", path);
        return;
    }
    if (propertyType == MaterialPropertyType::Float) {
        RequireFiniteNumber(document, "value", path);
        return;
    }

    size_t expectedSize = 0;
    switch (propertyType) {
    case MaterialPropertyType::Float2:
        expectedSize = 2;
        break;
    case MaterialPropertyType::Float3:
        expectedSize = 3;
        break;
    case MaterialPropertyType::Float4:
    case MaterialPropertyType::Color:
        expectedSize = 4;
        break;
    case MaterialPropertyType::Mat4:
        expectedSize = 16;
        break;
    default:
        Fail(path, "unsupported property type");
    }
    if (!document["value"].is_array() || document["value"].size() != expectedSize)
        Fail(path, "value has the wrong vector or matrix length");
    for (size_t index = 0; index < expectedSize; ++index) {
        if (!document["value"][index].is_number())
            Fail(std::string(path) + ".value[" + std::to_string(index) + "]", "must be a number");
        const double value = document["value"][index].get<double>();
        if (!std::isfinite(value) || std::abs(value) > std::numeric_limits<float>::max())
            Fail(std::string(path) + ".value[" + std::to_string(index) + "]", "must be a finite float");
    }
}

void ValidateShaderReference(const json &document, std::string_view path)
{
    static const std::unordered_set<std::string> fields = {"guid", "shader_id", "path_hint"};
    RequireExactFields(document, fields, {}, path);
    for (const char *field : {"guid", "shader_id", "path_hint"}) {
        if (!document[field].is_string())
            Fail(path, std::string(field) + " must be a string");
    }
    if (document["guid"].get_ref<const std::string &>().empty() &&
        document["shader_id"].get_ref<const std::string &>().empty()) {
        Fail(path, "requires guid or shader_id");
    }
}

} // namespace

void ValidateMaterialDocument(const nlohmann::json &document, std::string_view path)
{
    static const std::unordered_set<std::string> required = {
        "material_version", "name", "builtin", "shaders", "renderState", "properties",
    };
    static const std::unordered_set<std::string> optional = {
        "passTag",
        "renderStateOverrides",
        "_shader_property_order",
    };
    static const std::unordered_set<std::string> shaderFields = {"vertex", "fragment"};
    RequireExactFields(document, required, optional, path);
    if (!document["material_version"].is_number_integer())
        Fail(path, "material_version must be an integer");
    if (document["material_version"].get<int>() != 4)
        Fail(path, "material_version must be the current canonical value 4");
    if (!document["name"].is_string())
        Fail(path, "name must be a string");
    if (!document["builtin"].is_boolean())
        Fail(path, "builtin must be a boolean");

    const std::string shadersPath = std::string(path) + ".shaders";
    RequireExactFields(document["shaders"], shaderFields, {}, shadersPath);
    ValidateShaderReference(document["shaders"]["vertex"], shadersPath + ".vertex");
    ValidateShaderReference(document["shaders"]["fragment"], shadersPath + ".fragment");

    ValidateRenderState(document["renderState"], std::string(path) + ".renderState");
    if (document.contains("passTag") && !document["passTag"].is_string())
        Fail(path, "passTag must be a string");
    if (document.contains("renderStateOverrides")) {
        constexpr uint32_t allOverrides = (1u << 9u) - 1u;
        if ((RequireUnsigned(document, "renderStateOverrides", path) & ~allOverrides) != 0)
            Fail(path, "renderStateOverrides contains unknown bits");
    }

    if (!document["properties"].is_object())
        Fail(path, "properties must be an object");
    for (const auto &[name, property] : document["properties"].items())
        ValidateProperty(name, property, std::string(path) + ".properties." + name);

    if (document.contains("_shader_property_order")) {
        const auto &order = document["_shader_property_order"];
        if (!order.is_array())
            Fail(path, "_shader_property_order must be an array");
        std::unordered_set<std::string> orderedNames;
        orderedNames.reserve(order.size());
        for (size_t index = 0; index < order.size(); ++index) {
            if (!order[index].is_string())
                Fail(std::string(path) + "._shader_property_order[" + std::to_string(index) + "]", "must be a string");
            const std::string &name = order[index].get_ref<const std::string &>();
            if (name.empty())
                Fail(path, "_shader_property_order must not contain empty names");
            if (!document["properties"].contains(name))
                Fail(path, "_shader_property_order references missing property '" + name + "'");
            if (!orderedNames.insert(name).second)
                Fail(path, "_shader_property_order contains duplicate property '" + name + "'");
        }
    }
}

} // namespace infernux::material_document_validation
