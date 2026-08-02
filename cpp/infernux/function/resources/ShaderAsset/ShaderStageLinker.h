#pragma once

#include <core/types/ShaderTypes.h>
#include <cstdint>
#include <function/resources/ShaderAsset/ShaderDescriptor.h>
#include <optional>
#include <string>
#include <vector>

namespace infernux
{

enum class ShaderStageVisibility : uint8_t
{
    None = 0,
    Vertex = 1 << 0,
    Fragment = 1 << 1,
};

constexpr ShaderStageVisibility operator|(ShaderStageVisibility lhs, ShaderStageVisibility rhs) noexcept
{
    return static_cast<ShaderStageVisibility>(static_cast<uint8_t>(lhs) | static_cast<uint8_t>(rhs));
}

constexpr ShaderStageVisibility &operator|=(ShaderStageVisibility &lhs, ShaderStageVisibility rhs) noexcept
{
    lhs = lhs | rhs;
    return lhs;
}

constexpr bool HasVisibility(ShaderStageVisibility value, ShaderStageVisibility stage) noexcept
{
    return (static_cast<uint8_t>(value) & static_cast<uint8_t>(stage)) != 0;
}

enum class ShaderLinkDiagnosticSeverity : uint8_t
{
    Warning,
    Error,
};

enum class ShaderLinkDiagnosticCode : uint8_t
{
    InvalidStage,
    MissingEntryPoint,
    DuplicateVarying,
    MissingVertexOutput,
    UnsupportedVaryingType,
    VaryingTypeMismatch,
    InterpolationMismatch,
    SemanticMismatch,
    SpaceMismatch,
    DuplicateSemantic,
    LocationLimitExceeded,
    TextureBindingLimitExceeded,
    PropertyContractMismatch,
};

struct ShaderLinkDiagnostic
{
    ShaderLinkDiagnosticSeverity severity = ShaderLinkDiagnosticSeverity::Error;
    ShaderLinkDiagnosticCode code = ShaderLinkDiagnosticCode::InvalidStage;
    std::string message;
    std::string primaryFile;
    ShaderSourceRange primarySource;
    std::string relatedFile;
    std::optional<ShaderSourceRange> relatedSource;
};

struct ShaderStageReference
{
    std::string shaderId;
    std::string filePath;
};

struct LinkedShaderVarying
{
    std::string name;
    std::string glslType;
    std::string interpolation;
    std::string semantic;
    std::string space;
    uint32_t location = 0;
    uint32_t locationCount = 1;
    ShaderSourceRange vertexSource;
    ShaderSourceRange fragmentSource;
};

struct LinkedShaderProperty
{
    ShaderProperty schema;
    ShaderStageVisibility visibility = ShaderStageVisibility::None;
    std::optional<uint32_t> bufferOffset;
    std::optional<uint32_t> textureSlot;
    uint32_t byteSize = 0;
    uint32_t byteAlignment = 0;
};

struct ShaderProgramInterfaceArtifact
{
    ShaderStageReference vertex;
    ShaderStageReference fragment;
    ShaderProgramDomain domain = ShaderProgramDomain::Mesh;
    std::string shadingModel;
    uint32_t firstUserVaryingLocation = 6;
    uint32_t materialBufferSize = 0;
    std::optional<uint32_t> alphaClipThresholdOffset;
    std::vector<LinkedShaderVarying> varyings;
    std::vector<LinkedShaderProperty> properties;
    uint64_t varyingInterfaceSignature = 0;
    uint64_t materialLayoutSignature = 0;
    uint64_t compatibilitySignature = 0;
    std::vector<ShaderLinkDiagnostic> diagnostics;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct ShaderStageLinkOptions
{
    uint32_t firstUserVaryingLocation = 6;
    // Location 15 is reserved for engine pass data such as the picking ID.
    uint32_t maximumVaryingLocations = 15;
    uint32_t maximumMaterialTextures = 12;
};

class ShaderStageLinker final
{
  public:
    [[nodiscard]] static ShaderProgramInterfaceArtifact
    Link(const ShaderDescriptor &vertex, const ShaderDescriptor &fragment, const ShaderStageLinkOptions &options = {});
};

} // namespace infernux
