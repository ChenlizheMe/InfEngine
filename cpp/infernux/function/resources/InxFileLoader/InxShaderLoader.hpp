#pragma once

#include <SPIRV/GlslangToSpv.h>
#include <array>
#include <core/types/ShaderTypes.h>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/resources/ShaderAsset/ShaderInfoSchema.h>
#include <functional>
#include <glslang/Public/ShaderLang.h>
#include <optional>
#include <set>
#include <unordered_map>
#include <vector>

namespace infernux
{

// ============================================================================
// ShaderDescriptor IR — structured intermediate representation for parsed shaders
// ============================================================================

/// A single material property declared by the imported shader schema.
struct ShaderProperty
{
    std::string name;
    std::string type;     // "Float", "Float2", "Float3", "Float4", "Color", "Int", "Mat4", "Texture2D"
    std::string glslType; // "float", "vec2", "vec3", "vec4", "int", "mat4", "" (Texture2D)
    std::string defaultValue;
    bool isTexture = false;
    std::string textureDefault; // "white", "black", "normal" (Texture2D only)
    bool hdr = false;
    std::optional<std::array<double, 2>> range;
    ShaderSourceRange source;
};

struct ShaderVarying
{
    std::string interpolation;
    std::string type;
    std::string name;
    std::string semantic;
    std::string space;
    ShaderSourceRange source;
};

/// Surface rendering options declared via annotations.
struct SurfaceOptions
{
    std::string surfaceType = "opaque"; // "opaque" | "transparent"
    std::string alphaClip = "off";      // "off" | threshold string e.g. "0.5"
    std::string cullMode = "back";      // "back" | "front" | "none"
    std::string blendMode = "off";      // "off" | "alpha" | "additive" | "premultiply"
    bool receiveShadows = true;
    bool castShadows = true;
};

/// Complete structured representation of a parsed shader source file.
struct ShaderDescriptor
{
    uint32_t schemaVersion = 0;
    bool usesStructuredInfo = false;

    // Identity
    std::string shaderId;
    std::string filePath;
    std::string fileExtension; // ".vert", ".frag", ".glsl", ".shadingmodel"

    // Shader stage
    bool isVertexShader = false;
    bool isFragmentShader = false;
    bool isLibrary = false;      // .glsl
    bool isShadingModel = false; // .shadingmodel

    // Shading model (from @shading_model)
    std::string shadingModel;     // "pbr", "unlit", "custom", etc.
    bool hasExplicitType = false; // true when @shading_model is present
    bool hasSurfaceFunc = false;  // source contains void surface(
    bool hasMainFunc = false;     // source contains void main(
    bool hasVertexFunc = false;   // source contains void vertex(

    // Surface Options
    SurfaceOptions surfaceOptions;

    // Render state (from annotations)
    int renderQueue = -1; // -1 = auto (opaque=2000, transparent=3000)
    std::string passTag;
    std::string depthWrite;
    std::string depthTest;
    std::string stencil;
    bool hidden = false;

    // Properties
    std::vector<ShaderProperty> properties;
    std::vector<ShaderProperty> textureProperties;
    std::vector<ShaderVarying> inputs;
    std::vector<ShaderVarying> outputs;
    std::vector<std::string> capabilities;
    std::vector<ShaderInfoEntry> entries;

    // @import list
    std::vector<std::string> imports;

    // #version directive (empty if not present, defaults to "#version 450")
    std::string versionDirective;

    // .shadingmodel target blocks (only populated for .shadingmodel files)
    struct TargetBlock
    {
        std::string name; // "forward", "gbuffer", "shadow"
        std::string code; // GLSL code for this target
    };
    std::vector<TargetBlock> targets;

    /// Helper: find a target block by name, or nullptr.
    const TargetBlock *FindTarget(const std::string &name) const
    {
        for (const auto &t : targets)
            if (t.name == name)
                return &t;
        return nullptr;
    }

    /// Helper: does this shading model need LightingUBO?
    bool NeedsLightingUBO() const
    {
        for (const auto &imp : imports)
            if (imp == "lighting")
                return true;
        return false;
    }

    /// Helper: does this shading model need GBuffer outputs?
    bool HasGBufferTarget() const
    {
        return FindTarget("gbuffer") != nullptr;
    }

    // Errors and warnings
    std::vector<std::string> errors;
    std::vector<std::string> warnings;
};

// ============================================================================
// InxShaderLoader
// ============================================================================

/// @brief Shader compiler and meta creator utility.
///
/// Runtime asset loading is handled by ShaderLoader (IAssetLoader).
/// This class provides the GLSL compilation engine and meta creation
/// logic that ShaderLoader delegates to.
class InxShaderLoader
{
  public:
    InxShaderLoader(bool generateDebugInfo, bool stripDebugInfo, bool disableOptimizer, bool optimizeSize,
                    bool disassemble, bool validate, bool emitNonSemanticShaderDebugInfo,
                    bool emitNonSemanticShaderDebugSource, bool compileOnly, bool optimizerAllowExpandedIDBound);
    void SetShaderCompilerOptions(const std::string &prop, bool value);

    /// Register an additional directory to scan for @import resolution.
    static void AddShaderSearchPath(const std::string &dir);

    /// Invalidate cached shader-id maps and shading-model descriptors for a
    /// directory so the next compile rescans the filesystem.
    /// Pass an empty string to clear ALL cached directories.
    static void InvalidateDirectoryCache(const std::string &dir = "");

    /// Invalidate cached shader templates so edits under _templates/ are
    /// picked up on the next compile / reload.
    static void InvalidateTemplateCache();

    /// Get the currently registered shader search paths.
    static const std::vector<std::string> &GetShaderSearchPaths()
    {
        return s_additionalSearchPaths;
    }

    void CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                    InxResourceMeta &metaData) const;

    /// Compile shader source to SPIR-V and populate variant caches.
    /// Returns compiled data as shared_ptr<vector<char>> (forward SPIR-V), or nullptr on failure.
    std::shared_ptr<std::vector<char>> Compile(const char *content, size_t contentSize, InxResourceMeta &metaData);

    /// Parse a single "@key: value" or "// @key: value" annotation line.
    /// Returns {key, value} or nullopt if the line is not an annotation.
    static std::optional<std::pair<std::string, std::string>> ParseAnnotation(const std::string &line);

    /// Parse shader source into a structured ShaderDescriptor (single pass, no code generation).
    ShaderDescriptor ParseShaderSource(const std::string &source, const std::string &filePath) const;

    /// Last shader compile error message (empty on success).
    /// Set by Load() when glslang parse/link fails; read by Infernux::ReloadShaderRuntime.
    static std::string s_lastCompileError;

    /// Shadow fragment variant SPIR-V cache.
    /// Populated by Load() when a surface .frag is compiled; keyed by file path.
    /// Consumed by Infernux::ReloadShaderRuntime to register the shadow variant.
    static std::unordered_map<std::string, std::vector<char>> s_shadowVariantCache;

    /// Shadow vertex variant SPIR-V cache.
    /// Populated by Load() when a surface .vert is compiled; keyed by file path.
    static std::unordered_map<std::string, std::vector<char>> s_shadowVertexVariantCache;

    /// GBuffer variant SPIR-V cache.
    /// Populated by Compile() when a surface .frag with a gbuffer-capable shading model is compiled.
    /// Consumed by ShaderLoader to populate ShaderAsset::spirvGBuffer.
    static std::unordered_map<std::string, std::vector<char>> s_gbufferVariantCache;

  private:
    glslang::SpvOptions m_options;
    TBuiltInResource m_builtInResources;

    void InitGLSLBuiltResources();
    EShLanguage GetShaderType(const std::string &typeStr);

    /// Trim trailing content after last '}' and trailing whitespace.
    static std::string TrimShaderSource(const std::string &source);

    /// Compile GLSL source to SPIR-V. Returns false on failure (sets s_lastCompileError).
    bool CompileGLSL(const std::string &glslSource, EShLanguage shaderType, const std::string &filePath,
                     std::vector<char> &outSpirv);

    /// Preprocess and compile a shader variant (shadow/gbuffer), storing the result in cache.
    void CompileVariant(const char *content, const std::string &filePath, ShaderCompileTarget target,
                        const std::string &variantName, std::unordered_map<std::string, std::vector<char>> &cache,
                        EShLanguage shaderType = EShLangFragment);

    /// Full preprocessing pipeline: parse → resolve imports → generate GLSL.
    std::string PreprocessShaderSource(const std::string &source, const std::string &filePath = "",
                                       ShaderCompileTarget target = ShaderCompileTarget::Forward);

    /// Generate final GLSL text from a descriptor, import-resolved source, and optional shading model.
    std::string GenerateGLSL(const ShaderDescriptor &desc, const std::string &resolvedSource,
                             const ShaderDescriptor *shadingModel = nullptr,
                             ShaderCompileTarget target = ShaderCompileTarget::Forward) const;

    /// Build a mapping of shader_id → file_path by recursively scanning shader directories.
    std::unordered_map<std::string, std::string> BuildShaderIdMap(const std::string &dir);

    /// Resolve @import directives by inlining referenced shader files.
    std::string ResolveImports(const std::string &source,
                               const std::unordered_map<std::string, std::string> &shaderIdMap,
                               std::set<std::string> &includeStack, int depth = 0);

    /// Load and parse a .shadingmodel file by its shader_id.
    ShaderDescriptor LoadShadingModel(const std::string &modelName,
                                      const std::unordered_map<std::string, std::string> &shaderIdMap) const;

    /// Load a template file from _templates/ directory (with caching).
    static std::string LoadTemplate(const std::string &templateName);

    /// Replace all occurrences of a placeholder in a string.
    static void ReplacePlaceholder(std::string &str, const std::string &placeholder, const std::string &replacement);

    /// Additional directories registered via AddShaderSearchPath()
    static std::vector<std::string> s_additionalSearchPaths;

    /// Template file cache (static — shared across all loader instances)
    static std::unordered_map<std::string, std::string> s_templateCache;

    /// Shading model cache (static — avoids re-parsing .shadingmodel files)
    static std::unordered_map<std::string, ShaderDescriptor> s_shadingModelCache;

    /// BuildShaderIdMap cache (static — avoids rescanning shader directories on every compile)
    static std::unordered_map<std::string, std::unordered_map<std::string, std::string>> s_shaderIdMapCache;
};
} // namespace infernux
