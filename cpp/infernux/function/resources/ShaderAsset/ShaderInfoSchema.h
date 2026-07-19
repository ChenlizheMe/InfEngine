#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace infernux
{

enum class ShaderInfoKind : uint8_t
{
    Shader,
    ShadingModel,
};

enum class ShaderInfoDiagnosticSeverity : uint8_t
{
    Warning,
    Error,
};

struct ShaderSourceLocation
{
    size_t offset = 0;
    uint32_t line = 1;
    uint32_t column = 1;
};

struct ShaderSourceRange
{
    ShaderSourceLocation begin;
    ShaderSourceLocation end;
};

struct ShaderInfoDiagnostic
{
    ShaderInfoDiagnosticSeverity severity = ShaderInfoDiagnosticSeverity::Error;
    ShaderSourceLocation location;
    std::string message;
};

struct ShaderInfoRangeAttribute
{
    double minimum = 0.0;
    double maximum = 0.0;
};

struct ShaderInfoProperty
{
    std::string name;
    std::string type;
    std::string defaultValue;
    bool hdr = false;
    std::optional<ShaderInfoRangeAttribute> range;
    ShaderSourceRange source;
};

struct ShaderInfoVarying
{
    std::string interpolation = "Smooth";
    std::string type;
    std::string name;
    std::string semantic;
    std::string space;
    ShaderSourceRange source;
};

struct ShaderInfoEntry
{
    std::string role;
    std::string function;
    ShaderSourceRange source;
};

struct ShaderInfoDocument
{
    bool foundDeclaration = false;
    ShaderInfoKind kind = ShaderInfoKind::Shader;
    std::string name;
    std::string shadingModel;
    std::string surfaceType;
    std::optional<int> renderQueue;
    std::string cullMode;
    std::string depthWrite;
    std::string depthTest;
    std::string blendMode;
    std::string passTag;
    std::string stencil;
    std::string alphaClip;
    std::optional<bool> castShadows;
    std::optional<bool> receiveShadows;
    std::optional<bool> hidden;
    std::vector<ShaderInfoProperty> properties;
    std::vector<ShaderInfoVarying> inputs;
    std::vector<ShaderInfoVarying> outputs;
    std::vector<std::string> imports;
    std::vector<std::string> capabilities;
    std::vector<ShaderInfoEntry> entries;
    ShaderSourceRange declaration;
    std::vector<ShaderInfoDiagnostic> diagnostics;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct ShaderEntryPointSet
{
    bool main = false;
    bool surface = false;
    bool vertex = false;
};

/// Parse the first uncommented ShaderInfo or ShadingModelInfo declaration.
[[nodiscard]] ShaderInfoDocument ParseShaderInfo(std::string_view source);

/// Replace a parsed declaration with whitespace while preserving line numbers.
[[nodiscard]] std::string StripShaderInfoDeclaration(std::string_view source, const ShaderInfoDocument &document);

/// Detect GLSL entry declarations without matching comments or string literals.
[[nodiscard]] ShaderEntryPointSet DetectShaderEntryPoints(std::string_view source);

/// Rename the first matching GLSL function declaration while preserving all other source text.
[[nodiscard]] std::string RewriteShaderEntryPoint(std::string_view source, std::string_view returnType,
                                                  std::string_view entryPoint, std::string_view replacement);

/// Find a user-authored layout(...) qualifier without matching comments or strings.
[[nodiscard]] std::optional<ShaderSourceLocation> FindShaderLayoutDeclaration(std::string_view source);

} // namespace infernux
