#pragma once

#include <SPIRV/GlslangToSpv.h>
#include <atomic>
#include <core/types/ShaderProgramArtifact.h>
#include <core/types/ShaderTypes.h>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/resources/ShaderAsset/ShaderDescriptor.h>
#include <function/resources/ShaderAsset/ShaderPassVariantPlanner.h>
#include <function/resources/ShaderAsset/ShaderStageLinker.h>
#include <functional>
#include <glslang/Public/ShaderLang.h>
#include <set>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace infernux
{

struct LinkedShaderProgramCompilation
{
    ShaderCompileTarget target = ShaderCompileTarget::Forward;
    ShaderProgramInterfaceArtifact interfaceArtifact;
    std::string generatedVertexSource;
    std::string generatedFragmentSource;
    std::vector<char> vertexSpirv;
    std::vector<char> fragmentSpirv;
    std::vector<std::string> errors;
    bool usesBindlessTextureABI = false;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return interfaceArtifact.IsValid() && errors.empty() && !vertexSpirv.empty() && !fragmentSpirv.empty();
    }

    [[nodiscard]] ShaderProgramArtifact CreateRuntimeArtifact() const;
};

struct LinkedShaderProgramArtifactCompilation
{
    ShaderProgramInterfaceArtifact interfaceArtifact;
    ShaderPassVariantPlan passPlan;
    std::vector<LinkedShaderProgramCompilation> compiledVariants;
    std::vector<ShaderCompileTarget> pendingTargets;
    std::vector<std::string> errors;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] ShaderProgramArtifact CreateRuntimeArtifact() const;
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

    /// Register an additional directory to scan for ShaderInfo import resolution.
    static void AddShaderSearchPath(const std::string &dir);

    /// Invalidate cached shader-id maps and shading-model descriptors for a
    /// directory so the next compile rescans the filesystem.
    /// Pass an empty string to clear ALL cached directories.
    static void InvalidateDirectoryCache(const std::string &dir = "");

    /// Invalidate cached shader templates so edits under _templates/ are
    /// picked up on the next compile / reload.
    static void InvalidateTemplateCache();

    /// Select the device-supported material texture ABI used by generated
    /// shader source. When disabled, shaders that declare BindlessTextures
    /// are deliberately generated with the bounded sampler ABI instead.
    /// This is configured once after the Vulkan device capability probe and
    /// remains device-global for the lifetime of the active renderer.
    static void SetBindlessTextureABIEnabled(bool enabled) noexcept;
    [[nodiscard]] static bool IsBindlessTextureABIEnabled() noexcept;

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

    /// Link and compile a structured vertex/fragment pair as one Forward program.
    [[nodiscard]] LinkedShaderProgramCompilation CompileLinkedForward(const std::string &vertexSource,
                                                                      const std::string &vertexPath,
                                                                      const std::string &fragmentSource,
                                                                      const std::string &fragmentPath);

    [[nodiscard]] LinkedShaderProgramCompilation CompileLinkedProgram(const std::string &vertexSource,
                                                                      const std::string &vertexPath,
                                                                      const std::string &fragmentSource,
                                                                      const std::string &fragmentPath,
                                                                      ShaderCompileTarget target);

    /// Link once, plan every semantic pass, and AOT compile the complete set
    /// currently supported by the runtime. Unsupported planned targets remain
    /// explicit in pendingTargets and are never substituted with Forward code.
    [[nodiscard]] LinkedShaderProgramArtifactCompilation CompileLinkedProgramArtifact(const std::string &vertexSource,
                                                                                      const std::string &vertexPath,
                                                                                      const std::string &fragmentSource,
                                                                                      const std::string &fragmentPath);

    /// Compile generated compute GLSL directly to SPIR-V. This bypasses the
    /// authored material/shading-model preprocessor and is intended for AOT
    /// backends such as Particle Kernel IR.
    [[nodiscard]] std::vector<char> CompileComputeGlsl(const std::string &source,
                                                       const std::string &virtualPath = "<generated-compute>");

    /// Compile generated graphics GLSL directly to SPIR-V without invoking
    /// the authored material/shading-model preprocessor.
    [[nodiscard]] std::vector<char> CompileVertexGlsl(const std::string &source,
                                                      const std::string &virtualPath = "<generated-vertex>");
    [[nodiscard]] std::vector<char> CompileFragmentGlsl(const std::string &source,
                                                        const std::string &virtualPath = "<generated-fragment>");

    /// Parse shader source into a structured ShaderDescriptor (single pass, no code generation).
    ShaderDescriptor ParseShaderSource(const std::string &source, const std::string &filePath) const;

    /// Return a virtual source path whose extension carries the explicit shader
    /// stage while preserving the real file's parent directory for imports.
    /// This is also useful for virtual sources while preserving their parent
    /// directory for relative imports.
    [[nodiscard]] static std::string StageQualifiedVirtualPath(const std::string &filePath,
                                                               const std::string &shaderType);

    /// Last shader compile error message (empty on success).
    /// Set by Load() when glslang parse/link fails; read by Infernux::ReloadShaderRuntime.
    static thread_local std::string s_lastCompileError;
    [[nodiscard]] static const std::string &GetLastCompileError() noexcept;

    using CompiledVariantSet = std::unordered_map<ShaderCompileTarget, std::vector<char>>;

    /// Consume optional pass variants produced alongside the Forward return
    /// value. The returned entry is removed from the calling thread's transient
    /// compiler cache, so parallel imports cannot exchange variant payloads.
    [[nodiscard]] static CompiledVariantSet TakeCompiledVariants(const std::string &filePath);

  private:
    static thread_local std::unordered_map<std::string, CompiledVariantSet> s_compiledVariantCache;
    static std::atomic_bool s_bindlessTextureABIEnabled;

    glslang::SpvOptions m_options{};
    TBuiltInResource m_builtInResources{};
    void InitGLSLBuiltResources();
    EShLanguage GetShaderType(const std::string &typeStr);

    /// Trim trailing content after last '}' and trailing whitespace.
    static std::string TrimShaderSource(const std::string &source);

    /// Compile GLSL source to SPIR-V. Returns false on failure (sets s_lastCompileError).
    bool CompileGLSL(const std::string &glslSource, EShLanguage shaderType, const std::string &filePath,
                     std::vector<char> &outSpirv);

    [[nodiscard]] LinkedShaderProgramCompilation
    CompileLinkedProgramVariant(const std::string &vertexSource, const std::string &vertexPath,
                                const std::string &fragmentSource, const std::string &fragmentPath,
                                ShaderCompileTarget target, const ShaderProgramInterfaceArtifact &interfaceArtifact);

    /// Preprocess and compile a shader variant, storing it in the transient target cache.
    void CompileVariant(const char *content, const std::string &filePath, ShaderCompileTarget target,
                        const std::string &variantName, EShLanguage shaderType = EShLangFragment);

    /// Full preprocessing pipeline: parse → resolve imports → generate GLSL.
    std::string PreprocessShaderSource(const std::string &source, const std::string &filePath = "",
                                       ShaderCompileTarget target = ShaderCompileTarget::Forward,
                                       const ShaderProgramInterfaceArtifact *linkedInterface = nullptr);

    /// Generate final GLSL text from a descriptor, import-resolved source, and optional shading model.
    std::string GenerateGLSL(const ShaderDescriptor &desc, const std::string &resolvedSource,
                             const ShaderDescriptor *shadingModel = nullptr,
                             ShaderCompileTarget target = ShaderCompileTarget::Forward,
                             const ShaderProgramInterfaceArtifact *linkedInterface = nullptr,
                             const std::string &deferredShadingRegistry = {}) const;

    /// Build the generated Deferred dispatcher from every shading model that
    /// does not explicitly declare Unsupported [Deferred].
    std::string BuildDeferredShadingRegistry(const std::unordered_map<std::string, std::string> &shaderIdMap,
                                             std::vector<ShaderDescriptor> &models) const;

    /// Stable model identifier stored in the GBuffer object metadata target.
    static uint32_t ShadingModelId(std::string_view name) noexcept;

    /// Build a mapping of shader_id → file_path by recursively scanning shader directories.
    std::unordered_map<std::string, std::string> BuildShaderIdMap(const std::string &dir);

    /// Resolve structured Imports by inlining referenced shader libraries.
    std::string ResolveImports(const std::string &source, const std::vector<std::string> &imports,
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
