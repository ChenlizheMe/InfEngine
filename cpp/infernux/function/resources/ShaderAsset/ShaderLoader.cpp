#include "ShaderLoader.h"

#include <core/log/InxLog.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/resources/ShaderAsset/ShaderAsset.h>

#include <platform/filesystem/InxPath.h>

#include <filesystem>

namespace infernux
{

// =============================================================================
// Internal helper — compile a shader file into a ShaderAsset
// =============================================================================

static std::shared_ptr<ShaderAsset> CompileShaderAsset(const std::string &filePath, const std::string &guid,
                                                       AssetDatabase *adb)
{
    if (filePath.empty() || guid.empty()) {
        INXLOG_WARN("ShaderLoader: empty filePath or guid");
        return nullptr;
    }

    if (!adb) {
        INXLOG_ERROR("ShaderLoader: no AssetDatabase");
        return nullptr;
    }

    // Read shader source
    std::vector<char> content;
    if (!adb->ReadFile(filePath, content)) {
        INXLOG_ERROR("ShaderLoader: failed to read '", filePath, "'");
        return nullptr;
    }
    if (content.empty()) {
        INXLOG_ERROR("ShaderLoader: empty file '", filePath, "'");
        return nullptr;
    }

    // Runtime shader metadata remains authoritative; the source extension is
    // a fallback for loose Editor files and packed Player GLSL alike.
    std::filesystem::path fsPath = ToFsPath(filePath);
    std::string ext = FromFsPath(fsPath.extension());

    // Read metadata for shader_id
    const auto meta = adb->GetMetaByGuid(guid);
    std::string shaderId;
    if (meta && meta->HasKey("shader_id")) {
        shaderId = meta->GetDataAs<std::string>("shader_id");
    }
    if (shaderId.empty()) {
        shaderId = FromFsPath(fsPath.stem());
    }
    std::string shaderType;
    if (meta && meta->HasKey("type"))
        shaderType = meta->GetDataAs<std::string>("type");
    if (shaderType != "vertex" && shaderType != "fragment") {
        if (ext == ".vert")
            shaderType = "vertex";
        else if (ext == ".frag")
            shaderType = "fragment";
    }

    if (shaderType != "vertex" && shaderType != "fragment") {
        INXLOG_ERROR("ShaderLoader: shader stage metadata is invalid for '", filePath, "'");
        return nullptr;
    }

    // Ensure null-terminated for the GLSL compiler.
    if (content.back() != '\0')
        content.push_back('\0');

    const std::string compilePath = InxShaderLoader::StageQualifiedVirtualPath(filePath, shaderType);

    // Use InxShaderLoader to compile (it manages glslang, preprocessing, etc.)
    InxShaderLoader compiler(true, false, false, false, false, false, false, false, false, false);

    // Asset import already created the .meta; use it for Load().
    InxResourceMeta loadMeta;
    if (meta) {
        loadMeta = *meta;
        loadMeta.UpdateFilePath(compilePath);
    } else {
        // Build minimal meta for compilation
        loadMeta.AddMetadata("file_path", InxResourceMeta::NormalizeFilePath(compilePath));
        loadMeta.AddMetadata("type", shaderType);
        loadMeta.AddMetadata("shader_id", shaderId);
    }

    InxShaderLoader::s_lastCompileError.clear();

    auto compiledPtr = compiler.Compile(content.data(), content.size(), loadMeta);
    if (!compiledPtr || compiledPtr->empty()) {
        INXLOG_ERROR("ShaderLoader: compilation failed for '", filePath, "'");
        return nullptr;
    }

    // Build ShaderAsset
    auto asset = std::make_shared<ShaderAsset>();
    asset->shaderId = shaderId;
    asset->shaderType = shaderType;
    asset->filePath = filePath;
    asset->descriptor = compiler.ParseShaderSource(std::string(content.data(), content.size() - 1), compilePath);
    if (!asset->SetVariant(ShaderCompileTarget::Forward, std::move(*compiledPtr))) {
        INXLOG_ERROR("ShaderLoader: compiler returned invalid Forward SPIR-V for '", filePath, "'");
        return nullptr;
    }

    // Extract variant SPIR-V from InxShaderLoader's static caches
    // Use the meta's file_path as cache key (matches InxShaderLoader::CompileVariant)
    const std::string cacheKey = compilePath;

    auto variants = InxShaderLoader::TakeCompiledVariants(cacheKey);
    for (auto &[target, spirv] : variants)
        asset->SetVariant(target, std::move(spirv));

    if (shaderType == "fragment") {
        // Extract render-state annotations from meta
        if (meta) {
            if (meta->HasKey("shader_cull_mode"))
                asset->renderMeta.cullMode = meta->GetDataAs<std::string>("shader_cull_mode");
            if (meta->HasKey("shader_depth_write"))
                asset->renderMeta.depthWrite = meta->GetDataAs<std::string>("shader_depth_write");
            if (meta->HasKey("shader_depth_test"))
                asset->renderMeta.depthTest = meta->GetDataAs<std::string>("shader_depth_test");
            if (meta->HasKey("shader_blend"))
                asset->renderMeta.blend = meta->GetDataAs<std::string>("shader_blend");
            if (meta->HasKey("shader_queue"))
                asset->renderMeta.queue = meta->GetDataAs<int>("shader_queue");
            if (meta->HasKey("shader_pass_tag"))
                asset->renderMeta.passTag = meta->GetDataAs<std::string>("shader_pass_tag");
            if (meta->HasKey("shader_stencil"))
                asset->renderMeta.stencil = meta->GetDataAs<std::string>("shader_stencil");
            if (meta->HasKey("shader_alpha_test"))
                asset->renderMeta.alphaClip = meta->GetDataAs<std::string>("shader_alpha_test");
        }
    }

    INXLOG_INFO("ShaderLoader: compiled '", shaderId, "' (", asset->shaderType, ") from '", filePath, "'");
    return asset;
}

// =============================================================================
// Load
// =============================================================================

RuntimeAssetPayload ShaderLoader::Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb)
{
    return CompileShaderAsset(filePath, guid, adb);
}

// =============================================================================
// Reload — recompile and replace in-place
// =============================================================================

bool ShaderLoader::Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                          AssetDatabase *adb)
{
    auto oldAsset = existing.Get<ShaderAsset>();
    if (!oldAsset) {
        INXLOG_WARN("ShaderLoader::Reload: null existing instance");
        return false;
    }

    auto newAsset = CompileShaderAsset(filePath, guid, adb);
    if (!newAsset) {
        return false;
    }

    // Replace data in-place (preserving shared_ptr identity)
    *oldAsset = std::move(*newAsset);
    return true;
}

// =============================================================================
// ScanDependencies — shaders have no outgoing asset dependencies
// =============================================================================

size_t ShaderLoader::EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const
{
    const auto shader = payload.Get<ShaderAsset>();
    if (!shader)
        throw std::invalid_argument("ShaderLoader cannot estimate an empty runtime payload");
    return shader->GetRuntimeMemoryBytes();
}

std::set<std::string> ShaderLoader::ScanDependencies(const std::string & /*filePath*/, AssetDatabase * /*adb*/)
{
    return {};
}

// =============================================================================
// CreateMeta — delegate to InxShaderLoader (the shader compiler)
// =============================================================================

void ShaderLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                              InxResourceMeta &metaData) const
{
    InxShaderLoader compiler(true, false, false, false, false, false, false, false, false, false);
    compiler.CreateMeta(content, contentSize, filePath, metaData);
}

} // namespace infernux
