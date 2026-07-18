#include "ShaderStageLinker.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <limits>
#include <string_view>
#include <unordered_map>
#include <unordered_set>

namespace infernux
{
namespace
{
constexpr uint64_t FnvOffset = 14695981039346656037ull;
constexpr uint64_t FnvPrime = 1099511628211ull;

uint64_t HashBytes(uint64_t hash, std::string_view value)
{
    for (const unsigned char byte : value) {
        hash ^= byte;
        hash *= FnvPrime;
    }
    hash ^= 0xffu;
    hash *= FnvPrime;
    return hash;
}

uint64_t HashNumber(uint64_t hash, uint64_t value)
{
    for (uint32_t shift = 0; shift < 64; shift += 8) {
        hash ^= static_cast<uint8_t>(value >> shift);
        hash *= FnvPrime;
    }
    return hash;
}

uint64_t HashDouble(uint64_t hash, double value)
{
    static_assert(sizeof(value) == sizeof(uint64_t));
    uint64_t bits = 0;
    std::memcpy(&bits, &value, sizeof(bits));
    return HashNumber(hash, bits);
}

uint32_t AlignUp(uint32_t value, uint32_t alignment)
{
    return (value + alignment - 1u) & ~(alignment - 1u);
}

std::optional<std::string_view> GlslType(std::string_view type)
{
    static constexpr std::array<std::pair<std::string_view, std::string_view>, 7> Types = {
        std::pair{"Float", "float"}, std::pair{"Float2", "vec2"}, std::pair{"Float3", "vec3"},
        std::pair{"Float4", "vec4"}, std::pair{"Color", "vec4"},  std::pair{"Int", "int"},
        std::pair{"Mat4", "mat4"},
    };
    for (const auto &[schemaType, glslType] : Types) {
        if (type == schemaType)
            return glslType;
    }
    return std::nullopt;
}

uint32_t VaryingLocationCount(std::string_view type)
{
    return type == "Mat4" ? 4u : 1u;
}

struct PropertyLayout
{
    uint32_t alignment;
    uint32_t size;
};

std::optional<PropertyLayout> GetPropertyLayout(std::string_view type)
{
    if (type == "Float" || type == "Int")
        return PropertyLayout{4, 4};
    if (type == "Float2")
        return PropertyLayout{8, 8};
    if (type == "Float3" || type == "Float4" || type == "Color")
        return PropertyLayout{16, 16};
    if (type == "Mat4")
        return PropertyLayout{16, 64};
    return std::nullopt;
}

bool SameRange(const std::optional<std::array<double, 2>> &lhs, const std::optional<std::array<double, 2>> &rhs)
{
    if (lhs.has_value() != rhs.has_value())
        return false;
    return !lhs || *lhs == *rhs;
}

bool SamePropertyContract(const ShaderProperty &lhs, const ShaderProperty &rhs)
{
    return lhs.type == rhs.type && lhs.defaultValue == rhs.defaultValue && lhs.isTexture == rhs.isTexture &&
           lhs.textureDefault == rhs.textureDefault && lhs.hdr == rhs.hdr && SameRange(lhs.range, rhs.range);
}

ShaderLinkDiagnostic MakeDiagnostic(ShaderLinkDiagnosticCode code, std::string message, const std::string &primaryFile,
                                    const ShaderSourceRange &primarySource, const std::string &relatedFile = {},
                                    std::optional<ShaderSourceRange> relatedSource = std::nullopt)
{
    return {ShaderLinkDiagnosticSeverity::Error,
            code,
            std::move(message),
            primaryFile,
            primarySource,
            relatedFile,
            relatedSource};
}

void AppendStageProperties(ShaderProgramInterfaceArtifact &artifact, const ShaderDescriptor &stage,
                           ShaderStageVisibility visibility, std::unordered_map<std::string, size_t> &indices)
{
    auto append = [&](const ShaderProperty &property) {
        if (const auto existing = indices.find(property.name); existing != indices.end()) {
            auto &linked = artifact.properties[existing->second];
            if (!SamePropertyContract(linked.schema, property)) {
                artifact.diagnostics.push_back(MakeDiagnostic(
                    ShaderLinkDiagnosticCode::PropertyContractMismatch,
                    "material property '" + property.name + "' has incompatible declarations across shader stages",
                    stage.filePath, property.source,
                    visibility == ShaderStageVisibility::Fragment ? artifact.vertex.filePath
                                                                  : artifact.fragment.filePath,
                    linked.schema.source));
                return;
            }
            linked.visibility |= visibility;
            return;
        }

        LinkedShaderProperty linked;
        linked.schema = property;
        linked.visibility = visibility;
        indices.emplace(property.name, artifact.properties.size());
        artifact.properties.push_back(std::move(linked));
    };

    for (const auto &property : stage.properties)
        append(property);
    for (const auto &property : stage.textureProperties)
        append(property);
}

void AssignPropertyLayout(ShaderProgramInterfaceArtifact &artifact, bool includeAlphaClipThreshold,
                          uint32_t maximumMaterialTextures)
{
    uint32_t bufferCursor = 0;
    uint32_t textureSlot = 0;
    uint64_t signature = FnvOffset;
    for (auto &property : artifact.properties) {
        signature = HashBytes(signature, property.schema.name);
        signature = HashBytes(signature, property.schema.type);
        signature = HashNumber(signature, static_cast<uint8_t>(property.visibility));
        signature = HashNumber(signature, property.schema.hdr ? 1u : 0u);
        signature = HashBytes(signature, property.schema.defaultValue);
        signature = HashNumber(signature, property.schema.range ? 1u : 0u);
        if (property.schema.range) {
            signature = HashDouble(signature, (*property.schema.range)[0]);
            signature = HashDouble(signature, (*property.schema.range)[1]);
        }

        if (property.schema.isTexture) {
            property.textureSlot = textureSlot++;
            if (*property.textureSlot >= maximumMaterialTextures) {
                artifact.diagnostics.push_back(MakeDiagnostic(
                    ShaderLinkDiagnosticCode::TextureBindingLimitExceeded,
                    "material texture '" + property.schema.name + "' exceeds the linked Forward descriptor ABI limit",
                    HasVisibility(property.visibility, ShaderStageVisibility::Vertex) ? artifact.vertex.filePath
                                                                                      : artifact.fragment.filePath,
                    property.schema.source));
            }
            signature = HashNumber(signature, *property.textureSlot);
            continue;
        }

        const auto layout = GetPropertyLayout(property.schema.type);
        if (!layout)
            continue;
        bufferCursor = AlignUp(bufferCursor, layout->alignment);
        property.bufferOffset = bufferCursor;
        property.byteAlignment = layout->alignment;
        property.byteSize = layout->size;
        bufferCursor += layout->size;
        signature = HashNumber(signature, *property.bufferOffset);
        signature = HashNumber(signature, property.byteSize);
    }
    if (includeAlphaClipThreshold) {
        bufferCursor = AlignUp(bufferCursor, 4);
        artifact.alphaClipThresholdOffset = bufferCursor;
        bufferCursor += 4;
        signature = HashBytes(signature, "_AlphaClipThreshold");
        signature = HashNumber(signature, *artifact.alphaClipThresholdOffset);
    }
    artifact.materialBufferSize = AlignUp(bufferCursor, 16);
    artifact.materialLayoutSignature = HashNumber(signature, artifact.materialBufferSize);
}

void ValidateDuplicateVaryings(const ShaderDescriptor &stage, const std::vector<ShaderVarying> &varyings,
                               std::vector<ShaderLinkDiagnostic> &diagnostics)
{
    std::unordered_map<std::string, const ShaderVarying *> names;
    std::unordered_map<std::string, const ShaderVarying *> semantics;
    for (const auto &varying : varyings) {
        if (const auto [iterator, inserted] = names.emplace(varying.name, &varying); !inserted) {
            diagnostics.push_back(MakeDiagnostic(ShaderLinkDiagnosticCode::DuplicateVarying,
                                                 "duplicate varying '" + varying.name + "'", stage.filePath,
                                                 varying.source, stage.filePath, iterator->second->source));
        }
        if (!varying.semantic.empty()) {
            if (const auto [iterator, inserted] = semantics.emplace(varying.semantic, &varying); !inserted) {
                diagnostics.push_back(
                    MakeDiagnostic(ShaderLinkDiagnosticCode::DuplicateSemantic,
                                   "semantic '" + varying.semantic + "' is assigned to multiple varyings",
                                   stage.filePath, varying.source, stage.filePath, iterator->second->source));
            }
        }
    }
}

} // namespace

bool ShaderProgramInterfaceArtifact::IsValid() const noexcept
{
    return std::none_of(diagnostics.begin(), diagnostics.end(), [](const ShaderLinkDiagnostic &diagnostic) {
        return diagnostic.severity == ShaderLinkDiagnosticSeverity::Error;
    });
}

ShaderProgramInterfaceArtifact ShaderStageLinker::Link(const ShaderDescriptor &vertex, const ShaderDescriptor &fragment,
                                                       const ShaderStageLinkOptions &options)
{
    ShaderProgramInterfaceArtifact artifact;
    artifact.vertex = {vertex.shaderId, vertex.filePath, vertex.schemaVersion};
    artifact.fragment = {fragment.shaderId, fragment.filePath, fragment.schemaVersion};
    artifact.shadingModel = fragment.shadingModel;
    artifact.firstUserVaryingLocation = options.firstUserVaryingLocation;

    if (!vertex.isVertexShader) {
        artifact.diagnostics.push_back(MakeDiagnostic(ShaderLinkDiagnosticCode::InvalidStage,
                                                      "stage linker expected a vertex shader", vertex.filePath, {}));
    }
    if (!fragment.isFragmentShader) {
        artifact.diagnostics.push_back(MakeDiagnostic(
            ShaderLinkDiagnosticCode::InvalidStage, "stage linker expected a fragment shader", fragment.filePath, {}));
    }
    if (!vertex.outputs.empty() && !vertex.hasVertexFunc) {
        artifact.diagnostics.push_back(MakeDiagnostic(
            ShaderLinkDiagnosticCode::MissingEntryPoint,
            "a structured vertex stage with Outputs must define the fixed vertex entry point", vertex.filePath, {}));
    }
    if (!fragment.inputs.empty() && !fragment.hasSurfaceFunc) {
        artifact.diagnostics.push_back(
            MakeDiagnostic(ShaderLinkDiagnosticCode::MissingEntryPoint,
                           "a structured fragment stage with Inputs must define the fixed surface entry point",
                           fragment.filePath, {}));
    }

    ValidateDuplicateVaryings(vertex, vertex.outputs, artifact.diagnostics);
    ValidateDuplicateVaryings(fragment, fragment.inputs, artifact.diagnostics);

    std::unordered_map<std::string, const ShaderVarying *> vertexOutputs;
    for (const auto &output : vertex.outputs) {
        if (!GlslType(output.type)) {
            artifact.diagnostics.push_back(
                MakeDiagnostic(ShaderLinkDiagnosticCode::UnsupportedVaryingType,
                               "varying '" + output.name + "' uses a type that cannot cross raster stages",
                               vertex.filePath, output.source));
        }
        vertexOutputs.emplace(output.name, &output);
    }

    struct PendingVarying
    {
        const ShaderVarying *vertex;
        const ShaderVarying *fragment;
        std::string glslType;
        uint32_t locationCount;
    };
    std::vector<PendingVarying> pending;
    pending.reserve(fragment.inputs.size());

    for (const auto &input : fragment.inputs) {
        const auto fragmentGlslType = GlslType(input.type);
        if (!fragmentGlslType) {
            artifact.diagnostics.push_back(
                MakeDiagnostic(ShaderLinkDiagnosticCode::UnsupportedVaryingType,
                               "varying '" + input.name + "' uses a type that cannot cross raster stages",
                               fragment.filePath, input.source));
            continue;
        }
        const auto output = vertexOutputs.find(input.name);
        if (output == vertexOutputs.end()) {
            artifact.diagnostics.push_back(MakeDiagnostic(
                ShaderLinkDiagnosticCode::MissingVertexOutput,
                "fragment input '" + input.name + "' has no matching vertex output", fragment.filePath, input.source));
            continue;
        }

        const ShaderVarying &source = *output->second;
        if (!GlslType(source.type)) {
            artifact.diagnostics.push_back(
                MakeDiagnostic(ShaderLinkDiagnosticCode::UnsupportedVaryingType,
                               "varying '" + input.name + "' uses a type that cannot cross raster stages",
                               fragment.filePath, input.source, vertex.filePath, source.source));
            continue;
        }

        auto mismatch = [&](bool condition, ShaderLinkDiagnosticCode code, std::string_view field,
                            const std::string &lhs, const std::string &rhs) {
            if (!condition)
                return false;
            artifact.diagnostics.push_back(MakeDiagnostic(code,
                                                          "varying '" + input.name + "' has incompatible " +
                                                              std::string(field) + " ('" + rhs + "' -> '" + lhs + "')",
                                                          fragment.filePath, input.source, vertex.filePath,
                                                          source.source));
            return true;
        };

        bool incompatible = false;
        incompatible |= mismatch(input.type != source.type, ShaderLinkDiagnosticCode::VaryingTypeMismatch, "type",
                                 input.type, source.type);
        incompatible |=
            mismatch(input.interpolation != source.interpolation, ShaderLinkDiagnosticCode::InterpolationMismatch,
                     "interpolation", input.interpolation, source.interpolation);
        incompatible |= mismatch(input.semantic != source.semantic, ShaderLinkDiagnosticCode::SemanticMismatch,
                                 "semantic", input.semantic, source.semantic);
        incompatible |= mismatch(input.space != source.space, ShaderLinkDiagnosticCode::SpaceMismatch, "space",
                                 input.space, source.space);
        if (incompatible)
            continue;

        pending.push_back({&source, &input, std::string(*fragmentGlslType), VaryingLocationCount(input.type)});
    }

    std::sort(pending.begin(), pending.end(), [](const PendingVarying &lhs, const PendingVarying &rhs) {
        const std::string &lhsSemantic = lhs.fragment->semantic;
        const std::string &rhsSemantic = rhs.fragment->semantic;
        if (lhsSemantic.empty() != rhsSemantic.empty())
            return !lhsSemantic.empty();
        if (lhsSemantic != rhsSemantic)
            return lhsSemantic < rhsSemantic;
        return lhs.fragment->name < rhs.fragment->name;
    });

    uint32_t location = options.firstUserVaryingLocation;
    uint64_t varyingSignature = FnvOffset;
    for (const auto &entry : pending) {
        if (entry.locationCount > options.maximumVaryingLocations ||
            location > options.maximumVaryingLocations - entry.locationCount) {
            artifact.diagnostics.push_back(
                MakeDiagnostic(ShaderLinkDiagnosticCode::LocationLimitExceeded,
                               "varying '" + entry.fragment->name + "' exceeds the available stage interface locations",
                               fragment.filePath, entry.fragment->source, vertex.filePath, entry.vertex->source));
            continue;
        }
        LinkedShaderVarying linked;
        linked.name = entry.fragment->name;
        linked.glslType = entry.glslType;
        linked.interpolation = entry.fragment->interpolation;
        linked.semantic = entry.fragment->semantic;
        linked.space = entry.fragment->space;
        linked.location = location;
        linked.locationCount = entry.locationCount;
        linked.vertexSource = entry.vertex->source;
        linked.fragmentSource = entry.fragment->source;
        artifact.varyings.push_back(std::move(linked));

        varyingSignature = HashBytes(varyingSignature, entry.fragment->name);
        varyingSignature = HashBytes(varyingSignature, entry.glslType);
        varyingSignature = HashBytes(varyingSignature, entry.fragment->interpolation);
        varyingSignature = HashBytes(varyingSignature, entry.fragment->semantic);
        varyingSignature = HashBytes(varyingSignature, entry.fragment->space);
        varyingSignature = HashNumber(varyingSignature, location);
        varyingSignature = HashNumber(varyingSignature, entry.locationCount);
        location += entry.locationCount;
    }
    artifact.varyingInterfaceSignature = varyingSignature;

    std::unordered_map<std::string, size_t> propertyIndices;
    AppendStageProperties(artifact, vertex, ShaderStageVisibility::Vertex, propertyIndices);
    AppendStageProperties(artifact, fragment, ShaderStageVisibility::Fragment, propertyIndices);
    AssignPropertyLayout(artifact, fragment.hasSurfaceFunc, options.maximumMaterialTextures);

    uint64_t compatibility = FnvOffset;
    compatibility = HashNumber(compatibility, artifact.schemaVersion);
    compatibility = HashNumber(compatibility, artifact.varyingInterfaceSignature);
    compatibility = HashNumber(compatibility, artifact.materialLayoutSignature);
    compatibility = HashBytes(compatibility, artifact.shadingModel);
    artifact.compatibilitySignature = compatibility;
    return artifact;
}

} // namespace infernux
