#include "InxShaderLoader.hpp"

#include <SPIRV/GlslangToSpv.h>
#include <glslang/Public/ShaderLang.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <core/log/InxLog.h>
#include <filesystem>
#include <fstream>
#include <function/renderer/shader/ShaderReflection.h>
#include <function/resources/ShaderAsset/GlslStageInterfaceEmitter.h>
#include <nlohmann/json.hpp>
#include <platform/filesystem/InxPath.h>
#include <set>
#include <sstream>

namespace infernux
{
namespace
{
ShaderProgramStageMask ToRuntimeStageMask(ShaderStageVisibility visibility) noexcept
{
    ShaderProgramStageMask result = ShaderProgramStageMask::None;
    if (HasVisibility(visibility, ShaderStageVisibility::Vertex))
        result = result | ShaderProgramStageMask::Vertex;
    if (HasVisibility(visibility, ShaderStageVisibility::Fragment))
        result = result | ShaderProgramStageMask::Fragment;
    return result;
}

bool EqualsInsensitive(std::string_view lhs, std::string_view rhs)
{
    return lhs.size() == rhs.size() && std::equal(lhs.begin(), lhs.end(), rhs.begin(), [](char left, char right) {
               return std::tolower(static_cast<unsigned char>(left)) == std::tolower(static_cast<unsigned char>(right));
           });
}

bool DescriptorHasCapability(const ShaderDescriptor &descriptor, std::string_view capability)
{
    return std::find(descriptor.capabilities.begin(), descriptor.capabilities.end(), capability) !=
           descriptor.capabilities.end();
}

bool DescriptorMarksUnsupported(const ShaderDescriptor &descriptor, std::string_view feature)
{
    return std::any_of(descriptor.unsupported.begin(), descriptor.unsupported.end(),
                       [&](const std::string &value) { return EqualsInsensitive(value, feature); });
}

void CopyRuntimeInterface(const ShaderProgramInterfaceArtifact &source, ShaderProgramArtifact &target)
{
    target.domain = source.domain;
    target.usesParticleSceneDepthBinding = source.domain == ShaderProgramDomain::ParticleSprite;
    target.shadingModel = source.shadingModel;
    target.materialBufferSize = source.materialBufferSize;
    target.alphaClipThresholdOffset = source.alphaClipThresholdOffset;
    target.properties.reserve(source.properties.size());
    for (const auto &property : source.properties) {
        target.properties.push_back({property.schema.name, property.schema.type, property.schema.defaultValue,
                                     property.schema.textureDefault, ToRuntimeStageMask(property.visibility),
                                     property.schema.hdr, property.schema.range, property.bufferOffset,
                                     property.textureSlot, property.byteSize, property.byteAlignment});
    }
}
} // namespace

ShaderProgramArtifact LinkedShaderProgramCompilation::CreateRuntimeArtifact() const
{
    ShaderProgramArtifact artifact;
    artifact.key.stages.vertexShaderId = interfaceArtifact.vertex.shaderId;
    artifact.key.stages.fragmentShaderId = interfaceArtifact.fragment.shaderId;
    artifact.varyingInterfaceSignature = interfaceArtifact.varyingInterfaceSignature;
    artifact.materialLayoutSignature = interfaceArtifact.materialLayoutSignature;
    artifact.compatibilitySignature = interfaceArtifact.compatibilitySignature;
    CopyRuntimeInterface(interfaceArtifact, artifact);
    artifact.usesBindlessTextureABI = usesBindlessTextureABI;
    artifact.variants.push_back({target, interfaceArtifact.compatibilitySignature, vertexSpirv, fragmentSpirv});
    artifact.key.revision = ComputeShaderProgramArtifactRevision(artifact);
    return artifact;
}

bool LinkedShaderProgramArtifactCompilation::IsValid() const noexcept
{
    if (!interfaceArtifact.IsValid() || !passPlan.IsValid() || !errors.empty() || compiledVariants.empty())
        return false;
    if (passPlan.stages.vertexShaderId != interfaceArtifact.vertex.shaderId ||
        passPlan.stages.fragmentShaderId != interfaceArtifact.fragment.shaderId) {
        return false;
    }

    bool hasForward = false;
    uint64_t compiledTargets = 0;
    for (const auto &variant : compiledVariants) {
        if (!variant.IsValid() ||
            variant.interfaceArtifact.compatibilitySignature != interfaceArtifact.compatibilitySignature) {
            return false;
        }
        const int targetIndex = static_cast<int>(variant.target);
        if (targetIndex < 0 || targetIndex >= static_cast<int>(ShaderCompileTarget::Count))
            return false;
        const uint64_t targetBit = 1ull << static_cast<uint32_t>(targetIndex);
        if ((compiledTargets & targetBit) != 0)
            return false;
        compiledTargets |= targetBit;
        hasForward |= variant.target == ShaderCompileTarget::Forward;
    }
    if (!hasForward)
        return false;

    for (const auto &requirement : passPlan.requirements) {
        if (!requirement.enabled)
            continue;
        const bool supported =
            requirement.target == ShaderCompileTarget::Forward ||
            requirement.target == ShaderCompileTarget::ForwardPlus ||
            requirement.target == ShaderCompileTarget::GBuffer || requirement.target == ShaderCompileTarget::Shadow ||
            requirement.target == ShaderCompileTarget::Depth || requirement.target == ShaderCompileTarget::Picking ||
            requirement.target == ShaderCompileTarget::Motion || requirement.target == ShaderCompileTarget::Normal ||
            requirement.target == ShaderCompileTarget::BaseColor;
        const uint64_t targetBit = 1ull << static_cast<uint32_t>(requirement.target);
        if (supported && (compiledTargets & targetBit) == 0)
            return false;
    }
    return true;
}

ShaderProgramArtifact LinkedShaderProgramArtifactCompilation::CreateRuntimeArtifact() const
{
    if (!IsValid())
        return {};

    ShaderProgramArtifact artifact;
    artifact.key.stages = passPlan.stages;
    artifact.varyingInterfaceSignature = interfaceArtifact.varyingInterfaceSignature;
    artifact.materialLayoutSignature = interfaceArtifact.materialLayoutSignature;
    artifact.compatibilitySignature = interfaceArtifact.compatibilitySignature;
    CopyRuntimeInterface(interfaceArtifact, artifact);
    artifact.usesBindlessTextureABI = std::any_of(compiledVariants.begin(), compiledVariants.end(),
                                                  [](const auto &variant) { return variant.usesBindlessTextureABI; });
    artifact.variants.reserve(compiledVariants.size());
    for (const auto &variant : compiledVariants) {
        artifact.variants.push_back(
            {variant.target, interfaceArtifact.compatibilitySignature, variant.vertexSpirv, variant.fragmentSpirv});
    }
    std::sort(artifact.variants.begin(), artifact.variants.end(), [](const auto &lhs, const auto &rhs) {
        return static_cast<int>(lhs.target) < static_cast<int>(rhs.target);
    });
    artifact.key.revision = ComputeShaderProgramArtifactRevision(artifact);
    return artifact;
}

// Static members
std::vector<std::string> InxShaderLoader::s_additionalSearchPaths;
std::recursive_mutex InxShaderLoader::s_compilationMutex;
std::unordered_map<std::string, std::string> InxShaderLoader::s_templateCache;
std::unordered_map<std::string, ShaderDescriptor> InxShaderLoader::s_shadingModelCache;
thread_local std::unordered_map<std::string, InxShaderLoader::CompiledVariantSet>
    InxShaderLoader::s_compiledVariantCache;
thread_local std::string InxShaderLoader::s_lastCompileError;
std::atomic_bool InxShaderLoader::s_bindlessTextureABIEnabled{false};

InxShaderLoader::CompilationGuard::CompilationGuard() : m_lock(InxShaderLoader::s_compilationMutex)
{
}

const std::string &InxShaderLoader::GetLastCompileError() noexcept
{
    return s_lastCompileError;
}

void InxShaderLoader::SetBindlessTextureABIEnabled(bool enabled) noexcept
{
    s_bindlessTextureABIEnabled.store(enabled, std::memory_order_release);
}

bool InxShaderLoader::IsBindlessTextureABIEnabled() noexcept
{
    return s_bindlessTextureABIEnabled.load(std::memory_order_acquire);
}
std::unordered_map<std::string, std::unordered_map<std::string, std::string>> InxShaderLoader::s_shaderIdMapCache;

InxShaderLoader::CompiledVariantSet InxShaderLoader::TakeCompiledVariants(const std::string &filePath)
{
    const auto found = s_compiledVariantCache.find(filePath);
    if (found == s_compiledVariantCache.end())
        return {};
    CompiledVariantSet variants = std::move(found->second);
    s_compiledVariantCache.erase(found);
    return variants;
}

namespace
{
VkFormat ExpectedVaryingFormat(const std::string &glslType)
{
    if (glslType == "float")
        return VK_FORMAT_R32_SFLOAT;
    if (glslType == "vec2")
        return VK_FORMAT_R32G32_SFLOAT;
    if (glslType == "vec3")
        return VK_FORMAT_R32G32B32_SFLOAT;
    if (glslType == "vec4" || glslType == "mat4")
        return VK_FORMAT_R32G32B32A32_SFLOAT;
    if (glslType == "int")
        return VK_FORMAT_R32_SINT;
    return VK_FORMAT_UNDEFINED;
}

void ValidateReflectedVaryings(const std::vector<ShaderIOVariable> &reflected,
                               const ShaderProgramInterfaceArtifact &artifact, const std::string &stage,
                               std::vector<std::string> &errors)
{
    for (const auto &expected : artifact.varyings) {
        const auto actual = std::find_if(reflected.begin(), reflected.end(), [&](const ShaderIOVariable &candidate) {
            return candidate.location == expected.location;
        });
        if (actual == reflected.end()) {
            errors.push_back(stage + " SPIR-V reflection is missing linked varying '" + expected.name +
                             "' at location " + std::to_string(expected.location));
            continue;
        }
        const std::string expectedName = "_inx_v_" + expected.name;
        if (actual->name != expectedName) {
            errors.push_back(stage + " SPIR-V varying at location " + std::to_string(expected.location) +
                             " has name '" + actual->name + "', expected '" + expectedName + "'");
        }
        const VkFormat expectedFormat = ExpectedVaryingFormat(expected.glslType);
        if (actual->format != expectedFormat) {
            errors.push_back(stage + " SPIR-V varying '" + expected.name +
                             "' has a reflected format that does not "
                             "match the linked interface");
        }
    }
}

void ValidateReflectedMaterial(const ShaderReflection &reflection, const ShaderProgramInterfaceArtifact &artifact,
                               ShaderStageVisibility stage, ShaderCompileTarget target, const std::string &stageName,
                               bool bindlessTextureABI, std::vector<std::string> &errors)
{
    // The depth-only Shadow fragment intentionally strips all surface
    // resources unless its specialized alpha path needs them. The generated
    // program and Vulkan reflection still validate that specialized layout;
    // the linked full-material contract applies to Forward/GBuffer and to the
    // deforming Shadow vertex stage.
    if (target == ShaderCompileTarget::Shadow && stage == ShaderStageVisibility::Fragment && !bindlessTextureABI)
        return;

    const uint32_t expectedSet = target == ShaderCompileTarget::Shadow
                                     ? 2u
                                     : (artifact.domain == ShaderProgramDomain::ParticleSprite
                                            ? GlslStageInterfaceEmitter::ParticleSurfaceDescriptorSet
                                            : GlslStageInterfaceEmitter::MaterialDescriptorSet);
    const uint32_t expectedTextureBinding =
        target == ShaderCompileTarget::Shadow ? 0u : GlslStageInterfaceEmitter::FirstTextureBinding;
    const bool stageUsesMaterialBuffer =
        std::any_of(artifact.properties.begin(), artifact.properties.end(),
                    [&](const LinkedShaderProperty &property) {
                        return property.bufferOffset && HasVisibility(property.visibility, stage);
                    }) ||
        (stage == ShaderStageVisibility::Fragment && artifact.alphaClipThresholdOffset.has_value());
    if (stageUsesMaterialBuffer) {
        const auto uniformBuffer =
            std::find_if(reflection.GetUniformBuffers().begin(), reflection.GetUniformBuffers().end(),
                         [](const UniformBufferInfo &buffer) { return buffer.name == "MaterialProperties"; });
        if (uniformBuffer == reflection.GetUniformBuffers().end()) {
            errors.push_back(stageName + " SPIR-V reflection is missing the linked MaterialProperties block");
        } else {
            const uint32_t expectedMaterialBinding =
                target == ShaderCompileTarget::Shadow && stage == ShaderStageVisibility::Fragment
                    ? 8u
                    : GlslStageInterfaceEmitter::MaterialBufferBinding;
            if (uniformBuffer->set != expectedSet || uniformBuffer->binding != expectedMaterialBinding) {
                errors.push_back(stageName + " MaterialProperties reflected at an unexpected descriptor binding");
            }
            uint32_t requiredStageExtent = 0;
            for (const auto &property : artifact.properties) {
                if (property.bufferOffset && HasVisibility(property.visibility, stage)) {
                    requiredStageExtent = std::max(requiredStageExtent, *property.bufferOffset + property.byteSize);
                }
            }
            if (stage == ShaderStageVisibility::Fragment && artifact.alphaClipThresholdOffset)
                requiredStageExtent = std::max(requiredStageExtent, *artifact.alphaClipThresholdOffset + 4u);
            if (uniformBuffer->size < requiredStageExtent || uniformBuffer->size > artifact.materialBufferSize) {
                errors.push_back(stageName + " MaterialProperties reflected size " +
                                 std::to_string(uniformBuffer->size) + " is outside the linked stage range [" +
                                 std::to_string(requiredStageExtent) + ", " +
                                 std::to_string(artifact.materialBufferSize) + "]");
            }
            auto validateMember = [&](const std::string &name, uint32_t expectedOffset) {
                const auto member =
                    std::find_if(uniformBuffer->members.begin(), uniformBuffer->members.end(),
                                 [&](const UniformMember &candidate) { return candidate.name == name; });
                if (member == uniformBuffer->members.end()) {
                    errors.push_back(stageName + " MaterialProperties is missing member '" + name + "'");
                } else if (member->offset != expectedOffset) {
                    errors.push_back(stageName + " MaterialProperties member '" + name +
                                     "' has an offset that differs from the linked artifact");
                }
            };
            for (const auto &property : artifact.properties) {
                if (property.bufferOffset && HasVisibility(property.visibility, stage))
                    validateMember(property.schema.name, *property.bufferOffset);
            }
            if (stage == ShaderStageVisibility::Fragment && artifact.alphaClipThresholdOffset)
                validateMember("_AlphaClipThreshold", *artifact.alphaClipThresholdOffset);
        }
    }

    if (bindlessTextureABI && stage == ShaderStageVisibility::Fragment) {
        constexpr uint32_t bindlessTextureSet = 3;
        constexpr uint32_t bindlessTextureBinding = 0;
        const uint32_t textureIndexSet = target == ShaderCompileTarget::Shadow
                                             ? 2u
                                             : (artifact.domain == ShaderProgramDomain::ParticleSprite
                                                    ? GlslStageInterfaceEmitter::ParticleSurfaceDescriptorSet
                                                    : GlslStageInterfaceEmitter::MaterialDescriptorSet);
        const uint32_t textureIndexBinding = target == ShaderCompileTarget::Shadow
                                                 ? 15u
                                                 : (artifact.domain == ShaderProgramDomain::ParticleSprite
                                                        ? GlslStageInterfaceEmitter::FirstTextureBinding
                                                        : 15u);

        const auto textureTable =
            std::find_if(reflection.GetSampledImages().begin(), reflection.GetSampledImages().end(),
                         [=](const SampledImageInfo &candidate) {
                             return candidate.set == bindlessTextureSet && candidate.binding == bindlessTextureBinding;
                         });
        if (textureTable == reflection.GetSampledImages().end())
            errors.push_back(stageName + " SPIR-V reflection is missing the bindless material texture table");

        const auto indexBuffer =
            std::find_if(reflection.GetUniformBuffers().begin(), reflection.GetUniformBuffers().end(),
                         [=](const UniformBufferInfo &buffer) {
                             return buffer.set == textureIndexSet && buffer.binding == textureIndexBinding;
                         });
        if (indexBuffer == reflection.GetUniformBuffers().end()) {
            errors.push_back(stageName + " SPIR-V reflection is missing the bindless material texture-index block");
            return;
        }
        for (const auto &property : artifact.properties) {
            if (!property.textureSlot || !HasVisibility(property.visibility, stage))
                continue;
            const auto member =
                std::find_if(indexBuffer->members.begin(), indexBuffer->members.end(),
                             [&](const UniformMember &candidate) { return candidate.name == property.schema.name; });
            if (member == indexBuffer->members.end()) {
                errors.push_back(stageName + " bindless texture-index block is missing member '" +
                                 property.schema.name + "'");
            }
        }
        return;
    }

    for (const auto &property : artifact.properties) {
        if (!property.textureSlot || !HasVisibility(property.visibility, stage))
            continue;
        const auto image =
            std::find_if(reflection.GetSampledImages().begin(), reflection.GetSampledImages().end(),
                         [&](const SampledImageInfo &candidate) { return candidate.name == property.schema.name; });
        if (image == reflection.GetSampledImages().end()) {
            errors.push_back(stageName + " SPIR-V reflection is missing texture '" + property.schema.name + "'");
            continue;
        }
        const uint32_t expectedBinding = expectedTextureBinding + *property.textureSlot;
        if (image->set != expectedSet || image->binding != expectedBinding) {
            errors.push_back(stageName + " texture '" + property.schema.name +
                             "' reflected at an unexpected descriptor binding");
        }
    }
}
} // namespace

void InxShaderLoader::InvalidateDirectoryCache(const std::string &dir)
{
    const CompilationGuard guard;
    if (dir.empty()) {
        s_shaderIdMapCache.clear();
        s_shadingModelCache.clear();
    } else {
        const std::string normalized = FromFsPath(ToFsPath(dir));
        s_shaderIdMapCache.erase(normalized);
        // Shading models may have been loaded from this directory — clear all
        // since we cannot cheaply map model-name → source-dir.
        s_shadingModelCache.clear();
    }
}

void InxShaderLoader::InvalidateTemplateCache()
{
    s_templateCache.clear();
}

void InxShaderLoader::AddShaderSearchPath(const std::string &dir)
{
    const CompilationGuard guard;
    const std::string normalizedDir = FromFsPath(ToFsPath(dir));

    // Avoid duplicates
    for (const auto &existing : s_additionalSearchPaths) {
        if (existing == normalizedDir)
            return;
    }
    s_additionalSearchPaths.push_back(normalizedDir);
    INXLOG_INFO("Shader search path added: ", normalizedDir);
}

InxShaderLoader::InxShaderLoader(bool generateDebugInfo, bool stripDebugInfo, bool disableOptimizer, bool optimizeSize,
                                 bool disassemble, bool validate, bool emitNonSemanticShaderDebugInfo,
                                 bool emitNonSemanticShaderDebugSource, bool compileOnly,
                                 bool optimizerAllowExpandedIDBound)
{
    // Initialize the glslang library and set options
    glslang::InitializeProcess();
    m_options.generateDebugInfo = generateDebugInfo;
    m_options.stripDebugInfo = stripDebugInfo;
    m_options.disableOptimizer = disableOptimizer;
    m_options.optimizeSize = optimizeSize;
    m_options.disassemble = disassemble;
    m_options.validate = validate;
    m_options.emitNonSemanticShaderDebugInfo = emitNonSemanticShaderDebugInfo;
    m_options.emitNonSemanticShaderDebugSource = emitNonSemanticShaderDebugSource;
    m_options.compileOnly = compileOnly;
    m_options.optimizerAllowExpandedIDBound = optimizerAllowExpandedIDBound;

    // Initialize built-in resources
    InitGLSLBuiltResources();
}

void InxShaderLoader::SetShaderCompilerOptions(const std::string &prop, bool value)
{
    if (prop == "generateDebugInfo") {
        m_options.generateDebugInfo = value;
    } else if (prop == "stripDebugInfo") {
        m_options.stripDebugInfo = value;
    } else if (prop == "disableOptimizer") {
        m_options.disableOptimizer = value;
    } else if (prop == "optimizeSize") {
        m_options.optimizeSize = value;
    } else if (prop == "disassemble") {
        m_options.disassemble = value;
    } else if (prop == "validate") {
        m_options.validate = value;
    } else if (prop == "emitNonSemanticShaderDebugInfo") {
        m_options.emitNonSemanticShaderDebugInfo = value;
    } else if (prop == "emitNonSemanticShaderDebugSource") {
        m_options.emitNonSemanticShaderDebugSource = value;
    } else if (prop == "compileOnly") {
        m_options.compileOnly = value;
    } else if (prop == "optimizerAllowExpandedIDBound") {
        m_options.optimizerAllowExpandedIDBound = value;
    }
}

void InxShaderLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                                 InxResourceMeta &metaData) const
{
    const CompilationGuard guard;
    if (!content) {
        INXLOG_ERROR("Invalid shader content for metadata creation");
        return;
    }
    metaData.Init(content, contentSize, filePath, ResourceType::Shader);

    // Parse shader into structured descriptor (single pass)
    auto desc = ParseShaderSource(std::string(content, contentSize), filePath);

    // ----------------------------------------------------------------
    // Apply surface defaults when the structured declaration does not
    // explicitly override an individual render state.
    // ----------------------------------------------------------------
    if (EqualsInsensitive(desc.surfaceOptions.surfaceType, "transparent")) {
        if (desc.renderQueue < 0)
            desc.renderQueue = 3000;
        if (desc.surfaceOptions.blendMode == "off")
            desc.surfaceOptions.blendMode = "alpha";
        if (desc.depthWrite.empty())
            desc.depthWrite = "off";
        if (desc.passTag.empty())
            desc.passTag = "transparent";
    } else {
        // opaque defaults
        if (desc.renderQueue < 0)
            desc.renderQueue = 2000;
        if (desc.passTag.empty())
            desc.passTag = "opaque";
    }

    // Determine shader type from file extension
    std::string type = "vertex";
    if (desc.fileExtension == ".frag")
        type = "fragment";
    metaData.AddMetadata("type", type);

    // Serialize one canonical property schema for runtime import and editor UI.
    nlohmann::json properties = nlohmann::json::array();
    auto emitProperty = [&](const ShaderProperty &prop) {
        nlohmann::json defaultValue =
            prop.isTexture ? nlohmann::json(prop.textureDefault) : nlohmann::json(prop.defaultValue);
        if (!prop.isTexture) {
            try {
                defaultValue = nlohmann::json::parse(prop.defaultValue);
            } catch (const nlohmann::json::exception &) {
                // Preserve the authored token for future enum/symbolic defaults.
            }
        }
        nlohmann::json item = {
            {"name", prop.name},
            {"type", prop.type},
            {"default", std::move(defaultValue)},
            {"hdr", prop.hdr},
            {"internal", prop.internal},
            {"line", prop.source.begin.line},
            {"column", prop.source.begin.column},
        };
        if (prop.range)
            item["range"] = {(*prop.range)[0], (*prop.range)[1]};
        properties.push_back(std::move(item));
    };
    for (const auto &property : desc.properties)
        emitProperty(property);
    for (const auto &property : desc.textureProperties)
        emitProperty(property);

    auto serializeVaryings = [](const std::vector<ShaderVarying> &varyings) {
        nlohmann::json result = nlohmann::json::array();
        for (const auto &varying : varyings) {
            result.push_back({
                {"name", varying.name},
                {"type", varying.type},
                {"interpolation", varying.interpolation},
                {"semantic", varying.semantic},
                {"space", varying.space},
                {"line", varying.source.begin.line},
                {"column", varying.source.begin.column},
            });
        }
        return result.dump();
    };

    const std::string propertiesJson = properties.dump();

    metaData.AddMetadata("shader_id", desc.shaderId);
    metaData.AddMetadata("properties", propertiesJson);
    metaData.AddMetadata("shader_schema_format", std::string("ShaderInfo"));
    metaData.AddMetadata("shader_inputs", serializeVaryings(desc.inputs));
    metaData.AddMetadata("shader_outputs", serializeVaryings(desc.outputs));
    metaData.AddMetadata("shader_capabilities", nlohmann::json(desc.capabilities).dump());
    metaData.AddMetadata("shader_unsupported", nlohmann::json(desc.unsupported).dump());
    metaData.AddMetadata("shader_imports", nlohmann::json(desc.imports).dump());
    metaData.AddMetadata("shader_requirements", nlohmann::json(desc.requirements).dump());
    nlohmann::json entries = nlohmann::json::object();
    for (const auto &entry : desc.entries)
        entries[entry.role] = entry.function;
    metaData.AddMetadata("shader_entries", entries.dump());
    metaData.AddMetadata("shader_lighting_type", desc.shadingModel.empty() ? "Unlit" : desc.shadingModel);
    metaData.AddMetadata("shader_cull_mode", desc.surfaceOptions.cullMode);
    metaData.AddMetadata("shader_depth_write", desc.depthWrite);
    metaData.AddMetadata("shader_depth_test", desc.depthTest);
    metaData.AddMetadata("shader_blend", desc.surfaceOptions.blendMode == "off" ? "" : desc.surfaceOptions.blendMode);
    metaData.AddMetadata("shader_queue", desc.renderQueue);
    metaData.AddMetadata("shader_pass_tag", desc.passTag);
    metaData.AddMetadata("shader_stencil", desc.stencil);
    metaData.AddMetadata("shader_hidden", desc.hidden);
    metaData.AddMetadata("shader_alpha_test",
                         desc.surfaceOptions.alphaClip == "off" ? "" : desc.surfaceOptions.alphaClip);
    metaData.AddMetadata("shader_surface_type", desc.surfaceOptions.surfaceType);
    metaData.AddMetadata("shader_receive_shadows", desc.surfaceOptions.receiveShadows);
    metaData.AddMetadata("shader_cast_shadows", desc.surfaceOptions.castShadows);

    INXLOG_DEBUG("Shader metadata created - type: ", type, ", shader_id: ", desc.shaderId,
                 ", lighting_type: ", desc.shadingModel, ", properties: ", propertiesJson, " for file: ", filePath);
}

// ============================================================================
// ParseShaderSource — build ShaderDescriptor from raw source text
// ============================================================================

ShaderDescriptor InxShaderLoader::ParseShaderSource(const std::string &source, const std::string &filePath) const
{
    const CompilationGuard guard;
    ShaderDescriptor desc;
    desc.filePath = filePath;

    // Infer stage from file extension
    if (!filePath.empty()) {
        const std::filesystem::path fsPath = ToFsPath(filePath);
        desc.fileExtension = FromFsPath(fsPath.extension());
        desc.isVertexShader = (desc.fileExtension == ".vert");
        desc.isFragmentShader = (desc.fileExtension == ".frag");
        desc.isLibrary = (desc.fileExtension == ".glsl");
        desc.isShadingModel = (desc.fileExtension == ".shadingmodel");

        // Default shaderId from filename (without extension)
        std::string filename = FromFsPath(fsPath.filename());
        size_t dotPos = filename.find_last_of('.');
        if (dotPos != std::string::npos)
            desc.shaderId = filename.substr(0, dotPos);
        else
            desc.shaderId = filename;
    }

    // Trim + toLower helper
    auto toLower = [](std::string s) {
        std::transform(s.begin(), s.end(), s.begin(),
                       [](unsigned char character) { return static_cast<char>(std::tolower(character)); });
        return s;
    };

    static const std::unordered_map<std::string, std::string> typeMap = {
        {"Float4", "vec4"}, {"Color", "vec4"}, {"Float3", "vec3"}, {"Float2", "vec2"},
        {"Float", "float"}, {"Int", "int"},    {"Mat4", "mat4"},
    };

    const ShaderInfoDocument shaderInfo = ParseShaderInfo(source);
    std::string shaderCode = source;
    if (!shaderInfo.foundDeclaration) {
        desc.errors.push_back(filePath +
                              ":1:1: every authored shader requires a ShaderInfo or ShadingModelInfo declaration");
    } else {
        shaderCode = StripShaderInfoDeclaration(source, shaderInfo);
        if (!shaderInfo.name.empty())
            desc.shaderId = shaderInfo.name;
        if (!shaderInfo.shadingModel.empty()) {
            // Preserve authored casing — shader_ids are Title Case with spaces
            // (e.g. "PBR", "Unlit") and LoadShadingModel looks them up exactly.
            desc.shadingModel = shaderInfo.shadingModel;
            desc.hasExplicitType = true;
        }
        if (!shaderInfo.surfaceType.empty())
            desc.surfaceOptions.surfaceType = shaderInfo.surfaceType;
        if (shaderInfo.renderQueue)
            desc.renderQueue = *shaderInfo.renderQueue;
        if (!shaderInfo.cullMode.empty())
            desc.surfaceOptions.cullMode = shaderInfo.cullMode;
        if (!shaderInfo.depthWrite.empty())
            desc.depthWrite = shaderInfo.depthWrite;
        if (!shaderInfo.depthTest.empty())
            desc.depthTest = shaderInfo.depthTest;
        if (!shaderInfo.blendMode.empty())
            desc.surfaceOptions.blendMode = shaderInfo.blendMode;
        if (!shaderInfo.passTag.empty())
            desc.passTag = shaderInfo.passTag;
        if (!shaderInfo.stencil.empty())
            desc.stencil = shaderInfo.stencil;
        if (!shaderInfo.alphaClip.empty())
            desc.surfaceOptions.alphaClip = shaderInfo.alphaClip;
        if (shaderInfo.castShadows)
            desc.surfaceOptions.castShadows = *shaderInfo.castShadows;
        if (shaderInfo.receiveShadows)
            desc.surfaceOptions.receiveShadows = *shaderInfo.receiveShadows;
        if (shaderInfo.hidden)
            desc.hidden = *shaderInfo.hidden;

        for (const auto &infoProperty : shaderInfo.properties) {
            ShaderProperty property;
            property.name = infoProperty.name;
            property.type = infoProperty.type;
            property.defaultValue = infoProperty.defaultValue;
            property.hdr = infoProperty.hdr;
            property.internal = infoProperty.internal;
            property.source = infoProperty.source;
            if (infoProperty.range)
                property.range = std::array<double, 2>{infoProperty.range->minimum, infoProperty.range->maximum};
            if (property.type == "Texture2D") {
                property.isTexture = true;
                property.textureDefault = property.defaultValue;
                desc.textureProperties.push_back(std::move(property));
            } else if (const auto type = typeMap.find(property.type); type != typeMap.end()) {
                property.glslType = type->second;
                desc.properties.push_back(std::move(property));
            }
        }
        auto copyVaryings = [](const std::vector<ShaderInfoVarying> &sourceVaryings,
                               std::vector<ShaderVarying> &targetVaryings) {
            targetVaryings.reserve(sourceVaryings.size());
            for (const auto &varying : sourceVaryings) {
                targetVaryings.push_back({varying.interpolation, varying.type, varying.name, varying.semantic,
                                          varying.space, varying.source});
            }
        };
        copyVaryings(shaderInfo.inputs, desc.inputs);
        copyVaryings(shaderInfo.outputs, desc.outputs);
        desc.imports = shaderInfo.imports;
        desc.requirements = shaderInfo.requirements;
        desc.capabilities = shaderInfo.capabilities;
        desc.unsupported = shaderInfo.unsupported;
        desc.entries = shaderInfo.entries;
        desc.resources = shaderInfo.resources;
        desc.pushConstants = shaderInfo.pushConstants;
        for (const auto &diagnostic : shaderInfo.diagnostics) {
            const std::string message = filePath + ":" + std::to_string(diagnostic.location.line) + ":" +
                                        std::to_string(diagnostic.location.column) + ": " + diagnostic.message;
            if (diagnostic.severity == ShaderInfoDiagnosticSeverity::Error)
                desc.errors.push_back(message);
            else
                desc.warnings.push_back(message);
        }
        if (const auto layout = FindShaderLayoutDeclaration(source)) {
            desc.errors.push_back(filePath + ":" + std::to_string(layout->line) + ":" + std::to_string(layout->column) +
                                  ": ShaderInfo source must not declare layout(...); the stage linker owns Vulkan ABI");
        }
    }

    const ShaderEntryPointSet entryPoints = DetectShaderEntryPoints(shaderCode);
    desc.hasSurfaceFunc = entryPoints.surface;
    desc.hasMainFunc = entryPoints.main;
    desc.hasVertexFunc = entryPoints.vertex;
    desc.hasShadingFunc = entryPoints.shading;

    // Record the GLSL version and reject old annotation statements without
    // interpreting them. Comments containing an at-sign remain ordinary GLSL.
    std::istringstream stream(shaderCode);
    std::string line;
    uint32_t lineNumber = 0;
    while (std::getline(stream, line)) {
        ++lineNumber;
        // Check #version
        size_t start = line.find_first_not_of(" \t");
        std::string trimmedLine = (start != std::string::npos) ? line.substr(start) : "";
        if (trimmedLine.rfind("#version", 0) == 0) {
            desc.versionDirective = line;
            continue;
        }

        if (!trimmedLine.empty() && trimmedLine.front() == '@')
            desc.errors.push_back(filePath + ":" + std::to_string(lineNumber) +
                                  ":1: legacy @ shader syntax was removed; use ShaderInfo fields");
    }

    // A shading model is deliberately pipeline-agnostic. It exposes one fixed
    // shading() function; Forward, Forward+, Deferred, and custom pipelines
    // receive generated adapters around that same source.
    if (desc.isShadingModel) {
        if (!desc.entries.empty())
            desc.errors.push_back(filePath +
                                  ": ShadingModelInfo Entry declarations were removed; define the fixed shading() "
                                  "function once");
        if (!desc.hasShadingFunc)
            desc.errors.push_back(filePath + ": a shading model must define void shading(...)");
        if (desc.hasShadingFunc)
            desc.targets.push_back({"shading", shaderCode});
    }

    return desc;
}

std::string InxShaderLoader::StageQualifiedVirtualPath(const std::string &filePath, const std::string &shaderType)
{
    const std::string expectedExtension = shaderType == "vertex"     ? ".vert"
                                          : shaderType == "fragment" ? ".frag"
                                                                     : std::string{};
    if (expectedExtension.empty() || filePath.empty())
        return filePath;

    const std::filesystem::path path = ToFsPath(filePath);
    if (FromFsPath(path.extension()) == expectedExtension)
        return filePath;

    // Append instead of replacing so diagnostics retain the virtual source
    // identity and relative imports keep the same parent directory.
    return filePath + expectedExtension;
}

// ============================================================================
// Template loading and placeholder replacement
// ============================================================================

std::string InxShaderLoader::LoadTemplate(const std::string &templateName)
{
    auto it = s_templateCache.find(templateName);
    if (it != s_templateCache.end())
        return it->second;

    for (const auto &searchPath : s_additionalSearchPaths) {
        std::filesystem::path templatePath = ToFsPath(searchPath) / "_templates" / templateName;
        std::error_code ec;
        if (std::filesystem::exists(templatePath, ec)) {
            std::ifstream file(templatePath);
            if (file.is_open()) {
                std::ostringstream content;
                content << file.rdbuf();
                s_templateCache[templateName] = content.str();
                return s_templateCache[templateName];
            }
        }
    }
    INXLOG_ERROR("Shader template not found: ", templateName);
    return "";
}

void InxShaderLoader::ReplacePlaceholder(std::string &str, const std::string &placeholder,
                                         const std::string &replacement)
{
    size_t pos = 0;
    while ((pos = str.find(placeholder, pos)) != std::string::npos) {
        str.replace(pos, placeholder.length(), replacement);
        pos += replacement.length();
    }
}

// ============================================================================
// LoadShadingModel — find, read, cache, and parse a .shadingmodel file
// ============================================================================

ShaderDescriptor
InxShaderLoader::LoadShadingModel(const std::string &modelName,
                                  const std::unordered_map<std::string, std::string> &shaderIdMap) const
{
    // Check cache first
    auto cacheIt = s_shadingModelCache.find(modelName);
    if (cacheIt != s_shadingModelCache.end())
        return cacheIt->second;

    // Use a namespaced key so shading models cannot collide with regular imports.
    auto mapIt = shaderIdMap.find("shadingmodel/" + modelName);
    if (mapIt == shaderIdMap.end()) {
        INXLOG_ERROR("Shading model '", modelName, "' not found in shader search paths");
        ShaderDescriptor empty;
        empty.errors.push_back("Shading model not found: " + modelName);
        return empty;
    }

    const std::string &filePath = mapIt->second;
    std::ifstream file = OpenInputFile(filePath);
    if (!file.is_open()) {
        INXLOG_ERROR("Failed to open shading model file: ", filePath);
        ShaderDescriptor empty;
        empty.errors.push_back("Failed to open: " + filePath);
        return empty;
    }

    std::ostringstream content;
    content << file.rdbuf();
    file.close();

    // Parse through the same structured source path used by regular shaders.
    ShaderDescriptor desc = ParseShaderSource(content.str(), filePath);

    // Cache the result
    s_shadingModelCache[modelName] = desc;

    INXLOG_DEBUG("Loaded shading model '", modelName, "' from ", filePath, " with ", desc.targets.size(), " targets");

    return desc;
}

uint32_t InxShaderLoader::ShadingModelId(std::string_view name) noexcept
{
    // FNV-1a keeps authored model IDs stable across processes, platforms, and
    // asset import order. Zero is reserved for an invalid GBuffer record.
    uint32_t hash = 2166136261u;
    for (const unsigned char character : name) {
        hash ^= character;
        hash *= 16777619u;
    }
    return hash == 0u ? 1u : hash;
}

std::string
InxShaderLoader::BuildDeferredShadingRegistry(const std::unordered_map<std::string, std::string> &shaderIdMap,
                                              std::vector<ShaderDescriptor> &models) const
{
    std::vector<std::pair<std::string, std::string>> authoredModels;
    for (const auto &[key, path] : shaderIdMap) {
        constexpr std::string_view prefix = "shadingmodel/";
        if (key.size() >= prefix.size() && key.compare(0, prefix.size(), prefix) == 0)
            authoredModels.emplace_back(key.substr(prefix.size()), path);
    }
    std::sort(authoredModels.begin(), authoredModels.end());

    std::ostringstream functions;
    std::ostringstream dispatch;
    std::unordered_map<uint32_t, std::string> assignedIds;
    dispatch << "\nvoid inxDispatchShading(uint modelId, out vec4 color) {\n"
                "    switch (modelId) {\n";

    for (const auto &[name, path] : authoredModels) {
        (void)name;
        std::ifstream file = OpenInputFile(path);
        if (!file.is_open())
            continue;
        std::ostringstream stream;
        stream << file.rdbuf();
        ShaderDescriptor model = ParseShaderSource(stream.str(), path);
        if (!model.errors.empty() || !model.hasShadingFunc || DescriptorMarksUnsupported(model, "Deferred"))
            continue;

        const uint32_t modelId = ShadingModelId(model.shaderId);
        if (const auto existing = assignedIds.find(modelId); existing != assignedIds.end()) {
            INXLOG_ERROR("Deferred shading-model ID collision between '", existing->second, "' and '", model.shaderId,
                         "'");
            continue;
        }
        assignedIds.emplace(modelId, model.shaderId);
        models.push_back(model);

        const auto *sourceBlock = model.FindTarget("shading");
        if (!sourceBlock)
            continue;
        const std::string functionName = "_inx_shading_" + std::to_string(modelId);
        std::string modelSource = RewriteShaderEntryPoint(sourceBlock->code, "void", "shading", functionName);
        std::istringstream lines(modelSource);
        std::string line;
        functions << "\n// Deferred shading model: " << model.shaderId << "\n";
        while (std::getline(lines, line)) {
            const size_t first = line.find_first_not_of(" \t");
            if (first != std::string::npos && line.compare(first, 8, "#version") == 0)
                continue;
            functions << line << '\n';
        }
        dispatch << "    case " << modelId << "u: " << functionName << "(_inx_DeferredSurfaceData, color); return;\n";
    }

    dispatch << "    default: color = vec4(1.0, 0.0, 1.0, _inx_DeferredSurfaceData.alpha); return;\n"
                "    }\n"
                "}\n";
    return functions.str() + dispatch.str();
}

// ============================================================================
// GenerateGLSL — produce compilable GLSL from descriptor + resolved source
// ============================================================================

std::string InxShaderLoader::GenerateGLSL(const ShaderDescriptor &desc, const std::string &resolvedSource,
                                          const ShaderDescriptor *shadingModel, ShaderCompileTarget target,
                                          const ShaderProgramInterfaceArtifact *linkedInterface,
                                          const std::string &deferredShadingRegistry) const
{
    const auto hasCapability = [&](std::string_view capability) { return DescriptorHasCapability(desc, capability); };
    const bool particleSpriteDomain =
        DescriptorHasCapability(desc, "ParticleSprite") ||
        (linkedInterface && linkedInterface->domain == ShaderProgramDomain::ParticleSprite);
    const bool fullscreenDomain = hasCapability("Fullscreen");
    const bool deferredLightingDomain = hasCapability("DeferredLighting");
    const bool passBuffersDomain = hasCapability("PassBuffers");
    const bool requestsEngineGlobals = hasCapability("EngineGlobals");
    const bool particleBindlessTarget =
        !particleSpriteDomain || target == ShaderCompileTarget::Forward || target == ShaderCompileTarget::ForwardPlus;
    const bool shadowAlphaClipTarget = target == ShaderCompileTarget::Shadow && desc.isFragmentShader &&
                                       desc.surfaceOptions.alphaClip != "off" && !desc.surfaceOptions.alphaClip.empty();
    const bool bindlessTextureABI =
        hasCapability("BindlessTextures") && IsBindlessTextureABIEnabled() && desc.isFragmentShader &&
        (target != ShaderCompileTarget::Shadow || shadowAlphaClipTarget) && particleBindlessTarget;
    std::string bindlessTextureAliases;
    std::string bindlessTextureAliasCleanup;
    // Separate the version directive from the generated shader body.
    std::istringstream stream(resolvedSource);
    std::string line;
    std::string versionLine;
    std::vector<std::string> codeLines;

    while (std::getline(stream, line)) {
        size_t start = line.find_first_not_of(" \t");
        std::string trimmedLine = (start != std::string::npos) ? line.substr(start) : "";

        if (trimmedLine.rfind("#version", 0) == 0) {
            versionLine = line;
        } else {
            codeLines.push_back(line);
        }
    }

    std::ostringstream codeSourceStream;
    for (const auto &codeLine : codeLines)
        codeSourceStream << codeLine << '\n';
    std::string codeSource = codeSourceStream.str();
    const bool userHasLayoutDecls = FindShaderLayoutDeclaration(codeSource).has_value();
    const ShaderEntryPointSet generatedEntries = DetectShaderEntryPoints(codeSource);
    const bool hasSurfaceFunc = generatedEntries.surface;
    const bool hasMainFunc = generatedEntries.main;
    if (desc.isVertexShader && desc.hasVertexFunc) {
        codeSource = RewriteShaderEntryPoint(codeSource, "void", "vertex", "inxVertexEntry");
        codeSource = RewriteShaderEntryPoint(codeSource, "VertexOutput", "vertex", "inxVertexEntry");
        codeLines.clear();
        std::istringstream rewrittenCode(codeSource);
        while (std::getline(rewrittenCode, line))
            codeLines.push_back(line);
    }

    std::ostringstream result;

    // #version must be first line
    result << (versionLine.empty() ? "#version 450" : versionLine) << "\n";

    // ================================================================
    // Determine shading model capabilities
    // ================================================================
    bool needsLightingUBO = deferredLightingDomain;
    bool hasGBufferTarget = false;
    // Shadow alpha-clip needs
    // texture samplers, MaterialProperties UBO, and user surface() code
    // so it can sample alpha and discard transparent fragments.
    bool shadowNeedsAlphaClip = false;
    if (target != ShaderCompileTarget::Shadow && target != ShaderCompileTarget::GBuffer &&
        target != ShaderCompileTarget::BaseColor) {
        needsLightingUBO = needsLightingUBO || desc.NeedsLightingUBO();
        if (shadingModel)
            needsLightingUBO = needsLightingUBO || shadingModel->NeedsLightingUBO();
    }
    if (shadingModel) {
        // GBuffer target always uses the engine-owned canonical packing.
        if (target == ShaderCompileTarget::GBuffer) {
            hasGBufferTarget = true;
        }
    }
    if (target == ShaderCompileTarget::Shadow && desc.surfaceOptions.alphaClip != "off" &&
        !desc.surfaceOptions.alphaClip.empty()) {
        shadowNeedsAlphaClip = true;
    }

    // ================================================================
    // Compile-time target defines
    // ================================================================
    if (target == ShaderCompileTarget::Forward)
        result << "#define INX_FORWARD_PASS 1\n";
    else if (target == ShaderCompileTarget::ForwardPlus)
        result << "#define INX_FORWARD_PLUS_PASS 1\n";
    else if (target == ShaderCompileTarget::GBuffer)
        result << "#define INX_GBUFFER_PASS 1\n";
    else if (target == ShaderCompileTarget::Shadow)
        result << "#define INX_SHADOW_PASS 1\n";
    else if (target == ShaderCompileTarget::Depth)
        result << "#define INX_DEPTH_PASS 1\n";
    else if (target == ShaderCompileTarget::Picking)
        result << "#define INX_PICKING_PASS 1\n";
    else if (target == ShaderCompileTarget::Motion)
        result << "#define INX_MOTION_PASS 1\n";
    else if (target == ShaderCompileTarget::Normal)
        result << "#define INX_NORMAL_PASS 1\n";
    else if (target == ShaderCompileTarget::BaseColor)
        result << "#define INX_BASE_COLOR_PASS 1\n";
    if (deferredLightingDomain) {
        result << "#define INX_DEFERRED_LIGHTING_PASS 1\n";
        result << "#define INX_FORWARD_PLUS_PASS 1\n";
    }
    // Every geometry surface may use camera helpers, including an Unlit
    // surface that calls getViewDir() directly. The engine globals set is
    // shared by Scene and Game graphs and is staged from the editor camera,
    // so it is never a valid per-view camera source. Geometry fragments use
    // the camera-local set-1 UBO regardless of their lighting model.
    const bool needsCameraLocalView = !fullscreenDomain && !particleSpriteDomain && desc.isFragmentShader &&
                                      (desc.hasExplicitType || hasSurfaceFunc) &&
                                      target != ShaderCompileTarget::Shadow;
    if (needsLightingUBO || needsCameraLocalView) {
        result << "#define INX_SHADING_CAMERA_POSITION lighting.cameraPos.xyz\n";
    } else if (particleSpriteDomain) {
        result << "#define INX_SHADING_CAMERA_POSITION vec3(0.0)\n";
    } else {
        result << "#define INX_SHADING_CAMERA_POSITION _Globals._WorldSpaceCameraPos.xyz\n";
    }

    // ================================================================
    // Inject engine globals UBO — always available except shadow
    // For shadow vertex with vertex(), inject at set 1 (shadow globals set)
    // For shadow fragment with alpha clip, inject at set 1 (libraries need _Globals)
    // ================================================================
    bool shadowVertexNeedsGlobals =
        (target == ShaderCompileTarget::Shadow && desc.isVertexShader && desc.hasVertexFunc);
    bool shadowFragmentNeedsGlobals =
        (target == ShaderCompileTarget::Shadow && desc.isFragmentShader && shadowNeedsAlphaClip);
    if (target != ShaderCompileTarget::Shadow && !particleSpriteDomain &&
        (!fullscreenDomain || requestsEngineGlobals)) {
        result << "\n// Auto-generated engine globals UBO (set 2)\n";
        result << LoadTemplate("globals_ubo.glsl") << "\n";
    } else if (shadowVertexNeedsGlobals || shadowFragmentNeedsGlobals) {
        // Shadow pipeline has globals descriptor set at set 1 (not set 2)
        result << "\n// Auto-generated engine globals UBO (set 1 — shadow pipeline)\n";
        result << LoadTemplate("shadow_globals_ubo.glsl") << "\n";
    }

    // ================================================================
    // Inject remaining builtins (only when user has no layout declarations)
    // ================================================================
    if (!userHasLayoutDecls && deferredLightingDomain && desc.isFragmentShader) {
        result << "\n// Canonical per-view lighting resources for deferred evaluation\n";
        result << LoadTemplate("lighting_ubo.glsl") << "\n";
        result << LoadTemplate("forward_plus_lighting.glsl") << "\n";
        result << "uint _inx_ObjectLayerMask = 0xffffffffu;\n";
    }
    if (!userHasLayoutDecls && !fullscreenDomain) {
        if (desc.isVertexShader && target == ShaderCompileTarget::Shadow) {
            // Shadow vertex variant: use shadow-specific builtins (shadow UBO at set 0)
            result << "\n// Auto-generated shadow vertex builtins\n";
            result << LoadTemplate("shadow_vertex_builtins.glsl") << "\n";
            if (linkedInterface && !desc.outputs.empty())
                result << GlslStageInterfaceEmitter::EmitVertexDeclarations(*linkedInterface, desc);
        } else if (desc.isFragmentShader && target == ShaderCompileTarget::Shadow) {
            // Shadow fragment variant: only fragment varyings for interface matching
            // No InxGlobals (set 2) — shadow pipeline layout only provides set 0
            result << "\n// Auto-generated fragment varyings (shadow — interface match)\n";
            result << LoadTemplate("fragment_varyings.glsl") << "\n";
            if (linkedInterface && !desc.inputs.empty())
                result << GlslStageInterfaceEmitter::EmitFragmentDeclarations(*linkedInterface);
        } else {
            if (desc.isVertexShader) {
                const bool explicitVertexInterface =
                    linkedInterface == nullptr && hasMainFunc && (!desc.inputs.empty() || !desc.outputs.empty());
                // Explicit main() stages receive the engine geometry inputs but
                // declare their own stage outputs through ShaderInfo.
                result << "\n// Auto-generated vertex builtins (unified)\n";
                std::string vertexBuiltins = LoadTemplate(
                    particleSpriteDomain
                        ? "particle_sprite_vertex_builtins.glsl"
                        : (explicitVertexInterface ? "custom_geometry_vertex_builtins.glsl" : "vertex_builtins.glsl"));
                if (particleSpriteDomain && target == ShaderCompileTarget::Motion) {
                    ReplacePlaceholder(vertexBuiltins, "layout(location = 15) flat out uint _inx_ObjectLayerMask;",
                                       "uint _inx_ObjectLayerMask;");
                }
                result << vertexBuiltins << "\n";
                if (linkedInterface && !desc.outputs.empty())
                    result << GlslStageInterfaceEmitter::EmitVertexDeclarations(*linkedInterface, desc);
                if (target == ShaderCompileTarget::Picking)
                    result << "\n" << LoadTemplate("picking_vertex_interface.glsl") << "\n";
                else if (target == ShaderCompileTarget::Motion)
                    result << "\n" << LoadTemplate("motion_vertex_interface.glsl") << "\n";
                else if ((target == ShaderCompileTarget::Forward || target == ShaderCompileTarget::ForwardPlus ||
                          target == ShaderCompileTarget::GBuffer) &&
                         !particleSpriteDomain && !explicitVertexInterface)
                    result << "\n" << LoadTemplate("forward_plus_vertex_interface.glsl") << "\n";
            } else if (desc.isFragmentShader && (desc.hasExplicitType || hasSurfaceFunc)) {
                // Forward / GBuffer: full varying + output injection
                // The canonical per-view UBO is also the camera source for
                // Unlit surface shaders. Keeping camera data here avoids the
                // shared EngineGlobals camera leaking between render views.
                if (needsLightingUBO || needsCameraLocalView) {
                    result << "\n// Auto-generated per-view LightingUBO\n";
                    result << LoadTemplate(particleSpriteDomain ? "particle_lighting_ubo.glsl" : "lighting_ubo.glsl")
                           << "\n";
                    if (target == ShaderCompileTarget::ForwardPlus) {
                        result << "\n// Canonical tiled Forward+ light resources\n";
                        result << LoadTemplate(particleSpriteDomain ? "particle_forward_plus_lighting.glsl"
                                                                    : "forward_plus_lighting.glsl")
                               << "\n";
                    }
                }

                // Unified fragment varying inputs
                result << "\n// Auto-generated fragment varyings (unified)\n";
                std::string fragmentVaryings = LoadTemplate(
                    particleSpriteDomain ? "particle_sprite_fragment_varyings.glsl" : "fragment_varyings.glsl");
                if (particleSpriteDomain && target == ShaderCompileTarget::Motion) {
                    ReplacePlaceholder(fragmentVaryings, "layout(location = 15) flat in uint _inx_ObjectLayerMask;",
                                       "const uint _inx_ObjectLayerMask = 0xffffffffu;");
                }
                result << fragmentVaryings << "\n";
                if (particleSpriteDomain)
                    result << "\n// Canonical particle output controls\n"
                           << LoadTemplate("particle_sprite_fragment_controls.glsl") << "\n";
                if (!particleSpriteDomain &&
                    (target == ShaderCompileTarget::Forward || target == ShaderCompileTarget::ForwardPlus ||
                     target == ShaderCompileTarget::GBuffer)) {
                    // The matching geometry vertex stage always exports the
                    // object layer mask for these passes. Declare the input
                    // for unlit fragments too so the SPIR-V stage interface
                    // remains complete even when lighting does not read it.
                    result << "\n" << LoadTemplate("object_layer_fragment_interface.glsl") << "\n";
                } else if (needsLightingUBO && !particleSpriteDomain) {
                    result << "\nconst uint _inx_ObjectLayerMask = 0xffffffffu;\n";
                }
                if (linkedInterface && !desc.inputs.empty())
                    result << GlslStageInterfaceEmitter::EmitFragmentDeclarations(*linkedInterface);

                // Fragment output declarations
                if (target == ShaderCompileTarget::Picking) {
                    result << "\n" << LoadTemplate("picking_fragment_interface.glsl") << "\n";
                } else if (target == ShaderCompileTarget::Motion) {
                    result << "\n" << LoadTemplate("motion_fragment_interface.glsl") << "\n";
                } else if (target == ShaderCompileTarget::Normal) {
                    result << "\nlayout(location = 0) out vec4 outNormal;\n";
                } else if (target == ShaderCompileTarget::BaseColor) {
                    result << "\nlayout(location = 0) out vec4 outBaseColor;\n";
                } else if (target == ShaderCompileTarget::Depth) {
                    // Depth-only variants intentionally declare no color output.
                } else if (target == ShaderCompileTarget::GBuffer && hasGBufferTarget) {
                    result << "\n// GBuffer outputs (deferred rendering)\n";
                    result << LoadTemplate("gbuffer_outputs.glsl") << "\n";
                } else if (needsLightingUBO) {
                    result << "\n" << LoadTemplate("fragment_outputs_lit.glsl") << "\n";
                } else {
                    result << "\n" << LoadTemplate("fragment_outputs_unlit.glsl") << "\n";
                }
            }
        }
    }

    // Standalone passes declare their stage interfaces in ShaderInfo. Linked
    // material programs use the stage linker path above instead.
    // Surface/vertex() stages already occupy locations 0-5 with engine
    // varyings. Runtime import validates those files before pair linking, so
    // ShaderInfo Inputs/Outputs must not reuse those locations.
    const bool explicitVertexInterface =
        linkedInterface == nullptr && hasMainFunc && (!desc.inputs.empty() || !desc.outputs.empty());
    const bool injectedUnifiedFragmentVaryings =
        !fullscreenDomain && desc.isFragmentShader && (desc.hasExplicitType || hasSurfaceFunc);
    const bool injectedUnifiedVertexBuiltins =
        !fullscreenDomain && desc.isVertexShader && !explicitVertexInterface;
    if (!userHasLayoutDecls && linkedInterface == nullptr && (!desc.inputs.empty() || !desc.outputs.empty())) {
        if (injectedUnifiedFragmentVaryings) {
            if (!desc.inputs.empty())
                result << GlslStageInterfaceEmitter::EmitStandaloneFragmentInputPreview(desc);
        } else if (injectedUnifiedVertexBuiltins) {
            if (!desc.outputs.empty())
                result << GlslStageInterfaceEmitter::EmitStandaloneVertexOutputPreview(desc);
        } else {
            const auto glslType = [](std::string_view type) -> std::string_view {
                if (type == "Float")
                    return "float";
                if (type == "Float2")
                    return "vec2";
                if (type == "Float3")
                    return "vec3";
                if (type == "Float4" || type == "Color")
                    return "vec4";
                if (type == "Int")
                    return "int";
                if (type == "Mat4")
                    return "mat4";
                return {};
            };
            const auto qualifier = [](std::string_view interpolation) -> std::string_view {
                if (interpolation == "Flat")
                    return "flat ";
                if (interpolation == "NoPerspective")
                    return "noperspective ";
                if (interpolation == "Centroid")
                    return "centroid ";
                return {};
            };
            result << "\n// Auto-generated standalone stage interface\n";
            for (size_t index = 0; index < desc.inputs.size(); ++index) {
                const auto &input = desc.inputs[index];
                result << "layout(location = " << index << ") " << qualifier(input.interpolation) << "in "
                       << glslType(input.type) << " " << input.name << ";\n";
            }
            for (size_t index = 0; index < desc.outputs.size(); ++index) {
                const auto &output = desc.outputs[index];
                result << "layout(location = " << index << ") " << qualifier(output.interpolation) << "out "
                       << glslType(output.type) << " " << output.name << ";\n";
            }
        }
    }

    if (!userHasLayoutDecls && (hasCapability("CameraMatrices") || passBuffersDomain)) {
        result << "\n// Auto-generated camera matrices interface\n";
        result << LoadTemplate("camera_matrices_ubo.glsl") << "\n";
    }

    if (!userHasLayoutDecls && passBuffersDomain) {
        result << "\n// Canonical pass-buffer interface\n";
        result << "layout(set = 0, binding = 0) uniform sampler2D _InxPassColor;\n";
        result << "layout(set = 0, binding = 1) uniform sampler2D _InxPassDepth;\n";
        result << "layout(set = 0, binding = 2) uniform sampler2D _InxPassNormal;\n";
        result << "layout(set = 0, binding = 3) uniform sampler2D _InxPassMotion;\n";
    }

    if (!userHasLayoutDecls && !desc.resources.empty()) {
        result << "\n// Auto-generated pass resources\n";
        for (size_t index = 0; index < desc.resources.size(); ++index) {
            const auto &resource = desc.resources[index];
            const size_t binding = index + (passBuffersDomain ? 4u : 0u);
            if (resource.type == "Texture2D")
                result << "layout(set = 0, binding = " << binding << ") uniform sampler2D " << resource.name << ";\n";
            else if (resource.type == "Texture2DUInt")
                result << "layout(set = 0, binding = " << binding << ") uniform usampler2D " << resource.name << ";\n";
        }
    }

    if (!userHasLayoutDecls && desc.pushConstants) {
        const auto glslType = [](std::string_view type) -> std::string_view {
            if (type == "Float")
                return "float";
            if (type == "Float2")
                return "vec2";
            if (type == "Float3")
                return "vec3";
            if (type == "Float4" || type == "Color")
                return "vec4";
            if (type == "Int")
                return "int";
            if (type == "Mat4")
                return "mat4";
            return {};
        };
        result << "\n// Auto-generated push constants\nlayout(push_constant) uniform InxPushConstants {\n";
        for (const auto &field : desc.pushConstants->fields)
            result << "    " << glslType(field.type) << " " << field.name << ";\n";
        result << "} " << desc.pushConstants->instanceName << ";\n";
    }

    // ================================================================
    // Texture sampler declarations  (skip for shadow unless alpha clip)
    // ================================================================
    int texBaseBinding = needsLightingUBO ? 2 : 1;
    // Shadow alpha-clip fragment: pack textures + material UBO into set 2
    // (the per-material shadow descriptor set), starting from binding 0.
    const bool shadowAlphaFragment =
        (target == ShaderCompileTarget::Shadow && shadowNeedsAlphaClip && desc.isFragmentShader);
    int shadowTexBaseBinding = 0; // textures start at binding 0 in set 2
    if (target != ShaderCompileTarget::Shadow || shadowNeedsAlphaClip || shadowVertexNeedsGlobals) {
        if (bindlessTextureABI) {
            std::string bindlessTemplate = LoadTemplate("bindless_material_textures.glsl");
            std::string members;
            for (const auto &property : desc.textureProperties) {
                members += "    uint " + property.name + ";\n";
                bindlessTextureAliases +=
                    "#define " + property.name + " _InxMaterialTextureIndices." + property.name + "\n";
                bindlessTextureAliasCleanup += "#undef " + property.name + "\n";
            }
            if (members.empty())
                members = "    uint _Reserved;\n";
            ReplacePlaceholder(bindlessTemplate, "${TEXTURE_MEMBERS}", members);
            ReplacePlaceholder(bindlessTemplate, "${TEXTURE_TABLE_SET}", "3");
            ReplacePlaceholder(bindlessTemplate, "${TEXTURE_TABLE_BINDING}", "0");
            ReplacePlaceholder(bindlessTemplate, "${TEXTURE_INDEX_SET}",
                               target == ShaderCompileTarget::Shadow ? "2" : (particleSpriteDomain ? "2" : "0"));
            ReplacePlaceholder(bindlessTemplate, "${TEXTURE_INDEX_BINDING}",
                               target == ShaderCompileTarget::Shadow ? "15" : (particleSpriteDomain ? "2" : "15"));
            result << "\n// Canonical bindless material texture ABI\n" << bindlessTemplate << "\n";
        } else if (linkedInterface) {
            const ShaderStageVisibility stage =
                desc.isVertexShader ? ShaderStageVisibility::Vertex : ShaderStageVisibility::Fragment;
            const uint32_t descriptorSet =
                target == ShaderCompileTarget::Shadow
                    ? 2u
                    : (particleSpriteDomain ? GlslStageInterfaceEmitter::ParticleSurfaceDescriptorSet
                                            : GlslStageInterfaceEmitter::MaterialDescriptorSet);
            const uint32_t firstBinding =
                target == ShaderCompileTarget::Shadow ? 0u : GlslStageInterfaceEmitter::FirstTextureBinding;
            const std::string declarations = GlslStageInterfaceEmitter::EmitTextureDeclarations(
                *linkedInterface, stage, descriptorSet, firstBinding);
            if (!declarations.empty())
                result << "\n// Linked material texture bindings\n" << declarations << "\n";
        } else if (!desc.textureProperties.empty() && desc.isFragmentShader) {
            result << "\n// Auto-generated material texture samplers\n";
            for (size_t i = 0; i < desc.textureProperties.size(); ++i) {
                int binding = shadowAlphaFragment ? (shadowTexBaseBinding + static_cast<int>(i))
                                                  : (texBaseBinding + static_cast<int>(i));
                if (shadowAlphaFragment) {
                    result << "layout(set = 2, binding = " << binding << ") uniform sampler2D "
                           << desc.textureProperties[i].name << ";\n";
                } else {
                    result << "layout(binding = " << binding << ") uniform sampler2D " << desc.textureProperties[i].name
                           << ";\n";
                }
            }
            result << "\n";
        }
    }

    // ================================================================
    // MaterialProperties UBO  (skip for shadow unless alpha clip)
    // ================================================================
    // Surface fragment shaders always get _AlphaClipThreshold injected
    // so that alpha clip can be toggled at runtime via material properties.
    bool isSurfaceFragment = desc.isFragmentShader && hasSurfaceFunc;
    const ShaderStageVisibility linkedStage =
        desc.isVertexShader ? ShaderStageVisibility::Vertex : ShaderStageVisibility::Fragment;
    const bool linkedStageUsesMaterialBuffer =
        linkedInterface && (std::any_of(linkedInterface->properties.begin(), linkedInterface->properties.end(),
                                        [&](const LinkedShaderProperty &property) {
                                            return property.bufferOffset &&
                                                   HasVisibility(property.visibility, linkedStage);
                                        }) ||
                            (desc.isFragmentShader && linkedInterface->alphaClipThresholdOffset.has_value()));
    bool needsMaterialUBO =
        linkedInterface ? linkedStageUsesMaterialBuffer : (!desc.properties.empty() || isSurfaceFragment);
    // Vertex shaders with vertex() may need material properties in shadow (as constants)
    bool shadowVertexNeedsMaterial = (target == ShaderCompileTarget::Shadow && desc.isVertexShader &&
                                      desc.hasVertexFunc && !desc.properties.empty());
    if (target != ShaderCompileTarget::Shadow || shadowNeedsAlphaClip || shadowVertexNeedsMaterial) {
        if (needsMaterialUBO) {
            // Vertex shader MaterialProperties gets a dedicated high binding (14) to
            // avoid collision with fragment-side bindings (lighting UBO, textures, etc.)
            int materialBinding;
            if (linkedInterface && target != ShaderCompileTarget::Shadow) {
                materialBinding = GlslStageInterfaceEmitter::MaterialBufferBinding;
            } else if (desc.isVertexShader) {
                materialBinding = 14; // Reserved for vertex-stage material properties
            } else if (shadowAlphaFragment) {
                // Shadow alpha-clip fragment: place MaterialProperties at a fixed binding
                // that matches the descriptor set layout (kMaxShadowTextures = 8).
                // Bindings 0..7 are reserved for COMBINED_IMAGE_SAMPLER in the layout,
                // so the fragment UBO MUST go at binding 8 to avoid a type mismatch.
                materialBinding = 8;
            } else {
                materialBinding = bindlessTextureABI ? texBaseBinding
                                                     : texBaseBinding + static_cast<int>(desc.textureProperties.size());
            }
            result << "\n// Auto-generated MaterialProperties UBO\n";
            if (linkedInterface && target != ShaderCompileTarget::Shadow) {
                const uint32_t materialDescriptorSet = particleSpriteDomain
                                                           ? GlslStageInterfaceEmitter::ParticleSurfaceDescriptorSet
                                                           : GlslStageInterfaceEmitter::MaterialDescriptorSet;
                result << "layout(std140, set = " << materialDescriptorSet << ", binding = " << materialBinding
                       << ") uniform MaterialProperties {\n";
            } else if (target == ShaderCompileTarget::Shadow && (desc.isVertexShader || shadowAlphaFragment)) {
                result << "layout(std140, set = 2, binding = " << materialBinding << ") uniform MaterialProperties {\n";
            } else {
                result << "layout(std140, binding = " << materialBinding << ") uniform MaterialProperties {\n";
            }

            if (linkedInterface) {
                result << GlslStageInterfaceEmitter::EmitMaterialBlockMembers(*linkedInterface);
            } else {
                auto writeByType = [&](const std::string &glslType) {
                    for (const auto &prop : desc.properties) {
                        if (prop.glslType == glslType) {
                            result << "    " << prop.glslType << " " << prop.name << ";\n";
                        }
                    }
                };
                writeByType("vec4");
                writeByType("vec3");
                writeByType("vec2");
                // Inject _AlphaClipThreshold for surface fragment shaders (before user floats)
                if (isSurfaceFragment) {
                    result << "    float _AlphaClipThreshold;\n";
                }
                writeByType("float");
                writeByType("int");
                writeByType("mat4");
            }

            result << "} material;\n\n";
        }
    }

    // ================================================================
    // User code (with annotation lines stripped)
    // Skip for shadow target unless alpha clip is needed
    // OR vertex shader with vertex() function (shadow deformation)
    // ================================================================
    if (target != ShaderCompileTarget::Shadow || shadowNeedsAlphaClip || shadowVertexNeedsMaterial) {
        size_t lastImportEnd = std::string::npos;
        for (size_t index = 0; index < codeLines.size(); ++index) {
            if (codeLines[index].find("// --- end import:") != std::string::npos)
                lastImportEnd = index;
        }
        if (bindlessTextureABI && lastImportEnd == std::string::npos)
            result << bindlessTextureAliases;

        // For shadow alpha-clip fragments, skip import blocks that reference
        // UBOs not available in the shadow pipeline (LightingUBO, shadowMap).
        // The lighting.glsl import depends on the LightingUBO declaration
        // which is (correctly) not injected for shadow — strip its block.
        bool skipBlock = false;
        for (size_t index = 0; index < codeLines.size(); ++index) {
            const auto &codeLine = codeLines[index];
            if (shadowAlphaFragment) {
                if (codeLine.find("// --- begin import: Lighting ---") != std::string::npos) {
                    skipBlock = true;
                    continue;
                }
                if (skipBlock && codeLine.find("// --- end import: Lighting ---") != std::string::npos) {
                    skipBlock = false;
                    continue;
                }
                if (skipBlock)
                    continue;
            }
            result << codeLine << "\n";
            if (bindlessTextureABI && index == lastImportEnd)
                result << bindlessTextureAliases;
        }
        if (bindlessTextureABI)
            result << bindlessTextureAliasCleanup;
    } else if (target == ShaderCompileTarget::Shadow && desc.isVertexShader && desc.hasVertexFunc &&
               desc.properties.empty()) {
        // A shadow vertex stage without material properties only needs user code.
        for (const auto &codeLine : codeLines) {
            result << codeLine << "\n";
        }
    }

    // ================================================================
    // Auto-generated main() for surface fragment shaders
    // ================================================================
    if (hasSurfaceFunc && !hasMainFunc && desc.isFragmentShader && (desc.hasExplicitType || !userHasLayoutDecls)) {
        if (target == ShaderCompileTarget::Shadow) {
            if (shadowNeedsAlphaClip) {
                // Shadow pass with alpha clip: minimal fragment that only
                // fetches the first (albedo/diffuse) texture for alpha.
                // Running the full surface() would sample ALL textures
                // (normal, roughness, emission, …) which is pure waste
                // for a depth-only pass.  We duplicate only the alpha-
                // relevant logic: UV remap from displayScale/uvRect, then
                // a single texture() fetch.
                //
                // For shaders that rely on complex surface() logic for
                // alpha (procedural cutout, multi-texture blending) this
                // fast path may be inaccurate; authors can override by
                // providing an explicit main() in the shader source.
                if (!desc.textureProperties.empty()) {
                    const std::string &alphaTex = desc.textureProperties[0].name;
                    result << "\nvoid main() {\n";
                    // Check for displayScale/uvRect properties — sprite shaders
                    // use them to remap UVs for aspect-fit sub-rects.
                    bool hasDisplayScale = false, hasUvRect = false;
                    for (const auto &p : desc.properties) {
                        if (p.name == "displayScale")
                            hasDisplayScale = true;
                        if (p.name == "uvRect")
                            hasUvRect = true;
                    }
                    if (hasDisplayScale) {
                        result << "    vec2 dScale = material.displayScale.xy;\n";
                        result << "    vec2 tc = (v_TexCoord - 0.5) / max(dScale, vec2(1e-6)) + 0.5;\n";
                        result << "    if (tc.x < 0.0 || tc.x > 1.0 || tc.y < 0.0 || tc.y > 1.0) discard;\n";
                    } else {
                        result << "    vec2 tc = v_TexCoord;\n";
                    }
                    if (hasUvRect) {
                        result << "    vec2 uv = material.uvRect.xy + tc * material.uvRect.zw;\n";
                    } else {
                        result << "    vec2 uv = tc;\n";
                    }
                    if (bindlessTextureABI) {
                        result << "    float alpha = inxSampleBindlessTexture(_InxMaterialTextureIndices." << alphaTex
                               << ", uv).a";
                    } else {
                        result << "    float alpha = texture(" << alphaTex << ", uv).a";
                    }
                    // Multiply by baseColor.a if the material has a baseColor property
                    for (const auto &p : desc.properties) {
                        if (p.name == "baseColor") {
                            result << " * material.baseColor.a";
                            break;
                        }
                    }
                    result << ";\n";
                    result << "    if (material._AlphaClipThreshold > 0.0 && alpha < material._AlphaClipThreshold) "
                              "discard;\n";
                    result << "}\n";
                } else {
                    // No textures — fallback to full surface() path
                    result << "\nvoid main() {\n";
                    result << "    SurfaceData s = InitSurfaceData();\n";
                    result << "    s.normalWS = normalize(v_Normal);\n";
                    result << "    surface(s);\n";
                    result << "    if (material._AlphaClipThreshold > 0.0 && s.alpha < material._AlphaClipThreshold) "
                              "discard;\n";
                    result << "}\n";
                }
            } else {
                // Shadow pass: depth-only, minimal fragment shader
                result << "\nvoid main() {\n";
                result << "    // Depth written automatically by hardware\n";
                result << "}\n";
            }
        } else if (target == ShaderCompileTarget::Depth || target == ShaderCompileTarget::Picking ||
                   target == ShaderCompileTarget::Motion || target == ShaderCompileTarget::Normal ||
                   target == ShaderCompileTarget::BaseColor) {
            const char *templateName =
                target == ShaderCompileTarget::Picking
                    ? "surface_main_picking.glsl"
                    : (target == ShaderCompileTarget::Motion
                           ? "surface_main_motion.glsl"
                           : (target == ShaderCompileTarget::Normal
                                  ? "surface_main_normal.glsl"
                                  : (target == ShaderCompileTarget::BaseColor ? "surface_main_base_color.glsl"
                                                                              : "surface_main_depth.glsl")));
            std::string mainTpl = LoadTemplate(templateName);
            ReplacePlaceholder(mainTpl, "${SURFACE_CALL}",
                               linkedInterface ? GlslStageInterfaceEmitter::EmitSurfaceCall(*linkedInterface)
                                               : "    surface(s);");
            result << "\n" << mainTpl << "\n";
        } else {
            // The authored shading() function is injected only for Forward
            // targets. GBuffer packing is always engine-generated and records
            // the stable shading-model ID for the Deferred dispatcher.
            if (shadingModel && target != ShaderCompileTarget::GBuffer) {
                const auto *shadingBlock = shadingModel->FindTarget("shading");
                if (shadingBlock && !shadingBlock->code.empty()) {
                    result << "\n// Pipeline-agnostic shading model: " << desc.shadingModel << "\n";
                    result << shadingBlock->code << "\n";
                } else {
                    INXLOG_ERROR("Shading model '", desc.shadingModel, "' does not define shading()");
                }
            } else if (target == ShaderCompileTarget::GBuffer) {
                std::string packing = LoadTemplate("default_gbuffer_evaluate.glsl");
                ReplacePlaceholder(packing, "${SHADING_MODEL_ID}",
                                   std::to_string(ShadingModelId(desc.shadingModel)) + "u");
                result << "\n// Engine-generated canonical GBuffer packing\n" << packing << "\n";
            }

            // Alpha clip is now uniform-based (material._AlphaClipThreshold),
            // baked into the surface_main templates — no placeholder needed.
            if (target == ShaderCompileTarget::GBuffer && hasGBufferTarget) {
                std::string mainTpl = LoadTemplate("surface_main_gbuffer.glsl");
                ReplacePlaceholder(mainTpl, "${SURFACE_CALL}",
                                   linkedInterface ? GlslStageInterfaceEmitter::EmitSurfaceCall(*linkedInterface)
                                                   : "    surface(s);");
                result << "\n" << mainTpl << "\n";
            } else {
                std::string mainTpl =
                    LoadTemplate(particleSpriteDomain ? "particle_sprite_surface_main.glsl" : "surface_main.glsl");
                ReplacePlaceholder(mainTpl, "${SURFACE_CALL}",
                                   linkedInterface ? GlslStageInterfaceEmitter::EmitSurfaceCall(*linkedInterface)
                                                   : "    surface(s);");
                result << "\n" << mainTpl << "\n";
            }
        }
    }

    if (deferredLightingDomain && !deferredShadingRegistry.empty()) {
        result << "\n// Engine-generated deferred shading-model registry\n";
        result << deferredShadingRegistry << '\n';
    }

    // ================================================================
    // Auto-generated main() for vertex shaders
    // ================================================================
    if (desc.isVertexShader && !hasMainFunc && !userHasLayoutDecls) {
        std::string vertexCall;
        if (desc.hasVertexFunc) {
            vertexCall = linkedInterface ? GlslStageInterfaceEmitter::EmitVertexCall(*linkedInterface, desc)
                                         : "    inxVertexEntry(v);\n";
        }
        std::string templateName =
            target == ShaderCompileTarget::Shadow
                ? "shadow_vertex_main.glsl"
                : (particleSpriteDomain ? "particle_sprite_vertex_main.glsl" : "vertex_main.glsl");
        std::string mainTpl = LoadTemplate(templateName);
        ReplacePlaceholder(mainTpl, "${VERTEX_CALL}", vertexCall);
        std::string passVertexOutput;
        if (target == ShaderCompileTarget::Picking) {
            passVertexOutput = "    _inx_ObjectId = instanceAuxData[gl_InstanceIndex].objectId;";
        } else if (target == ShaderCompileTarget::Motion && particleSpriteDomain) {
            passVertexOutput = R"(    vec3 previousWorldPosition = instance.previous_position_history.xyz +
        (v.position - instance.position_size.xyz);
    vec4 previousClip = particleView.previous_view_projection * vec4(previousWorldPosition, 1.0);
    vec2 currentNdc = gl_Position.xy / max(abs(gl_Position.w), 1e-6);
    vec2 previousNdc = previousClip.xy / max(abs(previousClip.w), 1e-6);
    _inx_MotionVector = (currentNdc - previousNdc) * vec2(0.5, -0.5);)";
        } else if (target == ShaderCompileTarget::Motion) {
            passVertexOutput = R"(    InstanceAuxData aux = instanceAuxData[gl_InstanceIndex];
    if ((aux.flags & 1u) != 0u) {
        vec3 previousLocalPosition = inxUnskinnedPosition;
        if ((skin.flags & 1u) != 0u && skin.boneCount > 0u) {
            mat4 previousSkinMat =
                inBoneWeights.x * skinBones[skin.previousBoneOffset + min(inBoneIndices.x, skin.boneCount - 1u)] +
                inBoneWeights.y * skinBones[skin.previousBoneOffset + min(inBoneIndices.y, skin.boneCount - 1u)] +
                inBoneWeights.z * skinBones[skin.previousBoneOffset + min(inBoneIndices.z, skin.boneCount - 1u)] +
                inBoneWeights.w * skinBones[skin.previousBoneOffset + min(inBoneIndices.w, skin.boneCount - 1u)];
            previousLocalPosition = (previousSkinMat * vec4(previousLocalPosition, 1.0)).xyz;
        }
        vec4 previousClip = ubo.previousViewProj * aux.previousModel * vec4(previousLocalPosition, 1.0);
        vec2 currentNdc = gl_Position.xy / max(abs(gl_Position.w), 1e-6);
        vec2 previousNdc = previousClip.xy / max(abs(previousClip.w), 1e-6);
        _inx_MotionVector = (currentNdc - previousNdc) * vec2(0.5, -0.5);
    } else {
        _inx_MotionVector = vec2(0.0);
    })";
        } else if ((target == ShaderCompileTarget::Forward || target == ShaderCompileTarget::ForwardPlus ||
                    target == ShaderCompileTarget::GBuffer) &&
                   !particleSpriteDomain) {
            passVertexOutput = "    _inx_ObjectLayerMask = instanceAuxData[gl_InstanceIndex].layerMask;";
        }
        ReplacePlaceholder(mainTpl, "${PASS_VERTEX_OUTPUT}", passVertexOutput);
        result << "\n" << mainTpl << "\n";
    }

    return result.str();
}

// ============================================================================
// PreprocessShaderSource — full pipeline: parse → import → generate
// ============================================================================

std::string InxShaderLoader::PreprocessShaderSource(const std::string &source, const std::string &filePath,
                                                    ShaderCompileTarget target,
                                                    const ShaderProgramInterfaceArtifact *linkedInterface)
{
    // Stage 1: Parse source into structured descriptor
    ShaderDescriptor desc = ParseShaderSource(source, filePath);

    // Stage 2: resolve structured imports.
    std::string resolvedSource = StripShaderInfoDeclaration(source, ParseShaderInfo(source));
    std::vector<std::string> effectiveImports = desc.imports;
    const ShaderDescriptor *shadingModelPtr = nullptr;
    ShaderDescriptor shadingModelDesc;
    std::string deferredShadingRegistry;

    if (!filePath.empty()) {
        std::filesystem::path shaderPath = ToFsPath(filePath);
        std::string baseDir = FromFsPath(shaderPath.parent_path());
        auto shaderIdMap = BuildShaderIdMap(baseDir);

        std::set<std::string> includeStack;
        if (!desc.shaderId.empty()) {
            includeStack.insert(desc.shaderId);
        }

        // Load the referenced shading model and inject its import dependencies.
        if (!desc.shadingModel.empty() && desc.isFragmentShader && desc.hasSurfaceFunc && !desc.hasMainFunc) {
            shadingModelDesc = LoadShadingModel(desc.shadingModel, shaderIdMap);
            if (shadingModelDesc.errors.empty()) {
                shadingModelPtr = &shadingModelDesc;

                // GBuffer variants only execute surface() and canonical
                // packing. Lighting/model helpers belong to Forward shading
                // or the generated Deferred dispatcher, not geometry capture.
                if (target != ShaderCompileTarget::GBuffer && target != ShaderCompileTarget::BaseColor) {
                    std::set<std::string> existingImports(effectiveImports.begin(), effectiveImports.end());
                    for (const auto &imp : shadingModelDesc.imports) {
                        if (existingImports.find(imp) == existingImports.end()) {
                            effectiveImports.push_back(imp);
                            existingImports.insert(imp);
                        }
                    }
                }
            }
        }

        if (DescriptorHasCapability(desc, "DeferredLighting") && desc.isFragmentShader) {
            std::vector<ShaderDescriptor> deferredModels;
            deferredShadingRegistry = BuildDeferredShadingRegistry(shaderIdMap, deferredModels);
            std::set<std::string> existingImports(effectiveImports.begin(), effectiveImports.end());
            if (existingImports.insert("Surface").second)
                effectiveImports.push_back("Surface");
            for (const auto &model : deferredModels) {
                for (const auto &import : model.imports) {
                    if (existingImports.insert(import).second)
                        effectiveImports.push_back(import);
                }
            }
        }

        // Auto-inject the canonical surface helpers for surface() shaders.
        if (desc.hasSurfaceFunc && !desc.hasMainFunc && desc.isFragmentShader) {
            // Check the parsed import list before adding canonical helpers.
            bool hasSurfaceImport = false;
            bool hasObjectUtilsImport = false;
            bool hasParticleSurfaceUtilsImport = false;
            for (const auto &imp : desc.imports) {
                if (imp == "Surface")
                    hasSurfaceImport = true;
                if (imp == "Lib Object Utils")
                    hasObjectUtilsImport = true;
                if (imp == "Lib Particle Surface Utils")
                    hasParticleSurfaceUtilsImport = true;
            }
            if (!hasSurfaceImport) {
                effectiveImports.push_back("Surface");
            }
            const bool particleSpriteDomain =
                DescriptorHasCapability(desc, "ParticleSprite") ||
                (linkedInterface && linkedInterface->domain == ShaderProgramDomain::ParticleSprite);
            if (particleSpriteDomain && !hasParticleSurfaceUtilsImport) {
                effectiveImports.push_back("Lib Particle Surface Utils");
            } else if (!particleSpriteDomain && !hasObjectUtilsImport) {
                effectiveImports.push_back("Lib Object Utils");
            }
        }

        resolvedSource = ResolveImports(resolvedSource, effectiveImports, shaderIdMap, includeStack, 0);
    }

    // Stage 3: Generate GLSL from descriptor + resolved source + shading model
    return GenerateGLSL(desc, resolvedSource, shadingModelPtr, target, linkedInterface, deferredShadingRegistry);
}

std::shared_ptr<std::vector<char>> InxShaderLoader::Compile(const char *content, size_t contentSize,
                                                            InxResourceMeta &metaData)
{
    const CompilationGuard guard;
    s_lastCompileError.clear();

    if (!content) {
        INXLOG_ERROR("Invalid shader content");
        s_lastCompileError = "Invalid shader content";
        return nullptr;
    }

    std::string filePath = metaData.GetDataAs<std::string>("file_path");
    std::string type = metaData.GetDataAs<std::string>("type");
    INXLOG_DEBUG("InxShaderLoader::Compile - Compiling shader: ", filePath);
    s_compiledVariantCache.erase(filePath);

    EShLanguage shaderType = GetShaderType(type);
    if (shaderType == EShLangCount) {
        INXLOG_ERROR("Invalid shader type: ", type);
        return nullptr;
    }

    const ShaderDescriptor sourceDescriptor = ParseShaderSource(std::string(content, contentSize), filePath);
    if (!sourceDescriptor.errors.empty()) {
        std::ostringstream diagnostics;
        diagnostics << "ShaderInfo validation failed:";
        for (const auto &error : sourceDescriptor.errors)
            diagnostics << "\n" << error;
        s_lastCompileError = diagnostics.str();
        INXLOG_ERROR(s_lastCompileError);
        return nullptr;
    }

    // ---- Forward variant compilation ----
    std::string shaderSource = PreprocessShaderSource(std::string(content), filePath, ShaderCompileTarget::Forward);

    std::vector<char> forwardSpirv;
    if (!CompileGLSL(shaderSource, shaderType, filePath, forwardSpirv)) {
        return nullptr;
    }

    auto compiledData = std::make_shared<std::vector<char>>(std::move(forwardSpirv));

    // ---- Shadow + GBuffer variant compilation for surface fragment shaders ----
    if (type == "fragment") {
        if (sourceDescriptor.hasSurfaceFunc && !sourceDescriptor.hasMainFunc) {
            if (sourceDescriptor.surfaceOptions.castShadows)
                CompileVariant(content, filePath, ShaderCompileTarget::Shadow, "Shadow");

            // ParticleSprite programs are forward-domain outputs. Their
            // lighting contract intentionally depends on particle varyings and
            // particle shadow helpers that a mesh GBuffer stage does not own.
            if (!DescriptorHasCapability(sourceDescriptor, "ParticleSprite") &&
                !EqualsInsensitive(sourceDescriptor.surfaceOptions.surfaceType, "transparent") &&
                !sourceDescriptor.shadingModel.empty() &&
                !EqualsInsensitive(sourceDescriptor.shadingModel, "custom")) {
                CompileVariant(content, filePath, ShaderCompileTarget::GBuffer, "GBuffer");
            }
        }
    }

    // ---- Shadow vertex variant compilation for surface vertex shaders ----
    if (type == "vertex") {
        if (!sourceDescriptor.hasMainFunc && !sourceDescriptor.isLibrary) {
            CompileVariant(content, filePath, ShaderCompileTarget::Shadow, "ShadowVertex", EShLangVertex);
        }
    }

    return compiledData;
}

LinkedShaderProgramCompilation InxShaderLoader::CompileLinkedForward(const std::string &vertexSource,
                                                                     const std::string &vertexPath,
                                                                     const std::string &fragmentSource,
                                                                     const std::string &fragmentPath)
{
    const CompilationGuard guard;
    return CompileLinkedProgram(vertexSource, vertexPath, fragmentSource, fragmentPath, ShaderCompileTarget::Forward);
}

LinkedShaderProgramCompilation InxShaderLoader::CompileLinkedProgram(const std::string &vertexSource,
                                                                     const std::string &vertexPath,
                                                                     const std::string &fragmentSource,
                                                                     const std::string &fragmentPath,
                                                                     ShaderCompileTarget target)
{
    const CompilationGuard guard;
    LinkedShaderProgramCompilation compilation;
    compilation.target = target;
    if (target != ShaderCompileTarget::Forward && target != ShaderCompileTarget::ForwardPlus &&
        target != ShaderCompileTarget::GBuffer && target != ShaderCompileTarget::Shadow &&
        target != ShaderCompileTarget::Depth && target != ShaderCompileTarget::Picking &&
        target != ShaderCompileTarget::Motion && target != ShaderCompileTarget::Normal &&
        target != ShaderCompileTarget::BaseColor) {
        compilation.errors.push_back(std::string(ShaderCompileTargetName(target)) +
                                     " linked shader variant generation is not implemented");
        return compilation;
    }
    const std::string vertexCompilePath = StageQualifiedVirtualPath(vertexPath, "vertex");
    const std::string fragmentCompilePath = StageQualifiedVirtualPath(fragmentPath, "fragment");
    const ShaderDescriptor vertex = ParseShaderSource(vertexSource, vertexCompilePath);
    const ShaderDescriptor fragment = ParseShaderSource(fragmentSource, fragmentCompilePath);
    compilation.interfaceArtifact = ShaderStageLinker::Link(vertex, fragment);
    compilation.errors.insert(compilation.errors.end(), vertex.errors.begin(), vertex.errors.end());
    compilation.errors.insert(compilation.errors.end(), fragment.errors.begin(), fragment.errors.end());
    for (const auto &diagnostic : compilation.interfaceArtifact.diagnostics) {
        if (diagnostic.severity == ShaderLinkDiagnosticSeverity::Error)
            compilation.errors.push_back(diagnostic.message);
    }
    if (!compilation.interfaceArtifact.IsValid() || !compilation.errors.empty())
        return compilation;

    if (target == ShaderCompileTarget::GBuffer && !fragment.shadingModel.empty()) {
        const auto shaderMap = BuildShaderIdMap(FromFsPath(ToFsPath(fragmentCompilePath).parent_path()));
        const ShaderDescriptor model = LoadShadingModel(fragment.shadingModel, shaderMap);
        if (!model.errors.empty()) {
            compilation.errors.insert(compilation.errors.end(), model.errors.begin(), model.errors.end());
            return compilation;
        }
        if (DescriptorMarksUnsupported(model, "Deferred")) {
            compilation.errors.push_back("Shading model '" + fragment.shadingModel +
                                         "' declares Unsupported [Deferred]");
            return compilation;
        }
    }

    return CompileLinkedProgramVariant(vertexSource, vertexCompilePath, fragmentSource, fragmentCompilePath, target,
                                       compilation.interfaceArtifact);
}

LinkedShaderProgramArtifactCompilation InxShaderLoader::CompileLinkedProgramArtifact(const std::string &vertexSource,
                                                                                     const std::string &vertexPath,
                                                                                     const std::string &fragmentSource,
                                                                                     const std::string &fragmentPath)
{
    const CompilationGuard guard;
    LinkedShaderProgramArtifactCompilation result;
    const std::string vertexCompilePath = StageQualifiedVirtualPath(vertexPath, "vertex");
    const std::string fragmentCompilePath = StageQualifiedVirtualPath(fragmentPath, "fragment");
    const ShaderDescriptor vertex = ParseShaderSource(vertexSource, vertexCompilePath);
    const ShaderDescriptor fragment = ParseShaderSource(fragmentSource, fragmentCompilePath);
    result.interfaceArtifact = ShaderStageLinker::Link(vertex, fragment);
    result.errors.insert(result.errors.end(), vertex.errors.begin(), vertex.errors.end());
    result.errors.insert(result.errors.end(), fragment.errors.begin(), fragment.errors.end());
    for (const auto &diagnostic : result.interfaceArtifact.diagnostics) {
        if (diagnostic.severity == ShaderLinkDiagnosticSeverity::Error)
            result.errors.push_back(diagnostic.message);
    }
    if (!result.interfaceArtifact.IsValid() || !result.errors.empty())
        return result;

    bool shadingModelSupportsDeferred = true;
    if (!fragment.shadingModel.empty()) {
        const auto shaderMap = BuildShaderIdMap(FromFsPath(ToFsPath(fragmentCompilePath).parent_path()));
        const ShaderDescriptor model = LoadShadingModel(fragment.shadingModel, shaderMap);
        if (!model.errors.empty()) {
            result.errors.insert(result.errors.end(), model.errors.begin(), model.errors.end());
            return result;
        }
        shadingModelSupportsDeferred = !DescriptorMarksUnsupported(model, "Deferred");
    }
    result.passPlan =
        ShaderPassVariantPlanner::Plan(vertex, fragment, result.interfaceArtifact, shadingModelSupportsDeferred);
    result.errors.insert(result.errors.end(), result.passPlan.diagnostics.begin(), result.passPlan.diagnostics.end());
    if (!result.passPlan.IsValid() || !result.errors.empty())
        return result;

    for (const auto &requirement : result.passPlan.requirements) {
        if (!requirement.enabled)
            continue;

        const bool supported =
            requirement.target == ShaderCompileTarget::Forward ||
            requirement.target == ShaderCompileTarget::ForwardPlus ||
            requirement.target == ShaderCompileTarget::GBuffer || requirement.target == ShaderCompileTarget::Shadow ||
            requirement.target == ShaderCompileTarget::Depth || requirement.target == ShaderCompileTarget::Picking ||
            requirement.target == ShaderCompileTarget::Motion || requirement.target == ShaderCompileTarget::Normal ||
            requirement.target == ShaderCompileTarget::BaseColor;
        if (!supported) {
            result.pendingTargets.push_back(requirement.target);
            continue;
        }

        auto variant = CompileLinkedProgramVariant(vertexSource, vertexCompilePath, fragmentSource, fragmentCompilePath,
                                                   requirement.target, result.interfaceArtifact);
        if (!variant.IsValid()) {
            if (variant.errors.empty()) {
                result.errors.push_back(std::string(ShaderCompileTargetName(requirement.target)) +
                                        ": linked variant compilation failed");
            } else {
                for (const auto &error : variant.errors) {
                    result.errors.push_back(std::string(ShaderCompileTargetName(requirement.target)) + ": " + error);
                }
            }
            continue;
        }
        result.compiledVariants.push_back(std::move(variant));
    }
    return result;
}

LinkedShaderProgramCompilation
InxShaderLoader::CompileLinkedProgramVariant(const std::string &vertexSource, const std::string &vertexPath,
                                             const std::string &fragmentSource, const std::string &fragmentPath,
                                             ShaderCompileTarget target,
                                             const ShaderProgramInterfaceArtifact &interfaceArtifact)
{
    LinkedShaderProgramCompilation compilation;
    compilation.target = target;
    compilation.interfaceArtifact = interfaceArtifact;

    compilation.generatedVertexSource =
        PreprocessShaderSource(vertexSource, vertexPath, target, &compilation.interfaceArtifact);
    compilation.generatedFragmentSource =
        PreprocessShaderSource(fragmentSource, fragmentPath, target, &compilation.interfaceArtifact);
    const ShaderDescriptor fragmentDescriptor = ParseShaderSource(fragmentSource, fragmentPath);
    const bool particleBindlessTarget = interfaceArtifact.domain != ShaderProgramDomain::ParticleSprite ||
                                        target == ShaderCompileTarget::Forward ||
                                        target == ShaderCompileTarget::ForwardPlus;
    const bool shadowAlphaClipTarget = target == ShaderCompileTarget::Shadow &&
                                       fragmentDescriptor.surfaceOptions.alphaClip != "off" &&
                                       !fragmentDescriptor.surfaceOptions.alphaClip.empty();
    const bool bindlessTextureABI =
        DescriptorHasCapability(fragmentDescriptor, "BindlessTextures") && IsBindlessTextureABIEnabled() &&
        (target != ShaderCompileTarget::Shadow || shadowAlphaClipTarget) && particleBindlessTarget;
    compilation.usesBindlessTextureABI = bindlessTextureABI;

    s_lastCompileError.clear();
    if (!CompileGLSL(compilation.generatedVertexSource, EShLangVertex, vertexPath, compilation.vertexSpirv)) {
        compilation.errors.push_back(s_lastCompileError.empty() ? "linked vertex compilation failed"
                                                                : s_lastCompileError);
        return compilation;
    }
    s_lastCompileError.clear();
    if (!CompileGLSL(compilation.generatedFragmentSource, EShLangFragment, fragmentPath, compilation.fragmentSpirv)) {
        compilation.errors.push_back(s_lastCompileError.empty() ? "linked fragment compilation failed"
                                                                : s_lastCompileError);
        return compilation;
    }

    ShaderReflection vertexReflection;
    ShaderReflection fragmentReflection;
    if (!vertexReflection.Reflect(compilation.vertexSpirv, VK_SHADER_STAGE_VERTEX_BIT)) {
        compilation.errors.push_back("failed to reflect linked vertex SPIR-V");
    } else {
        ValidateReflectedVaryings(vertexReflection.GetOutputs(), compilation.interfaceArtifact, "vertex",
                                  compilation.errors);
        ValidateReflectedMaterial(vertexReflection, compilation.interfaceArtifact, ShaderStageVisibility::Vertex,
                                  target, "vertex", false, compilation.errors);
    }
    if (!fragmentReflection.Reflect(compilation.fragmentSpirv, VK_SHADER_STAGE_FRAGMENT_BIT)) {
        compilation.errors.push_back("failed to reflect linked fragment SPIR-V");
    } else {
        ValidateReflectedVaryings(fragmentReflection.GetInputs(), compilation.interfaceArtifact, "fragment",
                                  compilation.errors);
        ValidateReflectedMaterial(fragmentReflection, compilation.interfaceArtifact, ShaderStageVisibility::Fragment,
                                  target, "fragment", bindlessTextureABI, compilation.errors);
    }
    return compilation;
}

std::string InxShaderLoader::TrimShaderSource(const std::string &source)
{
    std::string result = source;
    size_t lastBrace = result.find_last_of('}');
    if (lastBrace != std::string::npos) {
        result = result.substr(0, lastBrace + 1);
    }
    while (!result.empty() && std::isspace(result.back())) {
        result.pop_back();
    }
    return result;
}

bool InxShaderLoader::CompileGLSL(const std::string &glslSource, EShLanguage shaderType, const std::string &filePath,
                                  std::vector<char> &outSpirv)
{
    std::string trimmed = TrimShaderSource(glslSource);

    std::vector<char> buf(trimmed.begin(), trimmed.end());
    buf.push_back('\0');
    const char *strings[1] = {buf.data()};

    glslang::TShader shader(shaderType);
    shader.setStrings(strings, 1);

    constexpr int clientInputSemanticsVersion = 100;
    constexpr auto vulkanClientVersion = glslang::EShTargetVulkan_1_2;
    constexpr auto targetVersion = glslang::EShTargetSpv_1_5;

    shader.setEnvInput(glslang::EShSourceGlsl, shaderType, glslang::EShClientVulkan, clientInputSemanticsVersion);
    shader.setEnvClient(glslang::EShClientVulkan, vulkanClientVersion);
    shader.setEnvTarget(glslang::EShTargetSpv, targetVersion);

    EShMessages messages = (EShMessages)(EShMsgSpvRules | EShMsgVulkanRules);
    if (!shader.parse(&m_builtInResources, 100, false, messages)) {
        s_lastCompileError = std::string("Shader parse failed:\n") + shader.getInfoLog();
        INXLOG_ERROR("Shader parse failed:\n", shader.getInfoLog());
        INXLOG_ERROR("Shader content:\n", trimmed);
        INXLOG_ERROR("Shader file path: ", filePath);
        return false;
    }

    glslang::TProgram program;
    program.addShader(&shader);
    if (!program.link(messages)) {
        s_lastCompileError = std::string("Shader link failed:\n") + program.getInfoLog();
        INXLOG_ERROR("Shader link failed for '", filePath, "':\n", program.getInfoLog());
        return false;
    }

    std::vector<unsigned int> spirv;
    glslang::GlslangToSpv(*program.getIntermediate(shaderType), spirv, &m_options);

    outSpirv.resize(spirv.size() * sizeof(unsigned int));
    std::memcpy(outSpirv.data(), reinterpret_cast<const char *>(spirv.data()), outSpirv.size());
    return true;
}

std::vector<char> InxShaderLoader::CompileComputeGlsl(const std::string &source, const std::string &virtualPath)
{
    const CompilationGuard guard;
    std::vector<char> spirv;
    if (!CompileGLSL(source, EShLangCompute, virtualPath, spirv))
        return {};
    return spirv;
}

std::vector<char> InxShaderLoader::CompileVertexGlsl(const std::string &source, const std::string &virtualPath)
{
    const CompilationGuard guard;
    std::vector<char> spirv;
    if (!CompileGLSL(source, EShLangVertex, virtualPath, spirv))
        return {};
    return spirv;
}

std::vector<char> InxShaderLoader::CompileFragmentGlsl(const std::string &source, const std::string &virtualPath)
{
    const CompilationGuard guard;
    std::vector<char> spirv;
    if (!CompileGLSL(source, EShLangFragment, virtualPath, spirv))
        return {};
    return spirv;
}

void InxShaderLoader::CompileVariant(const char *content, const std::string &filePath, ShaderCompileTarget target,
                                     const std::string &variantName, EShLanguage shaderType)
{
    std::string variantSource = PreprocessShaderSource(std::string(content), filePath, target);

    INXLOG_DEBUG("Compiling ", variantName, " variant for: ", filePath, "\n", variantSource);

    std::vector<char> spirv;
    if (!CompileGLSL(variantSource, shaderType, filePath, spirv)) {
        INXLOG_WARN(variantName, " variant compile failed for '", filePath, "'");
        INXLOG_WARN(variantName, " variant source:\n", TrimShaderSource(variantSource));
        return;
    }

    size_t variantSize = spirv.size();
    s_compiledVariantCache[filePath][target] = std::move(spirv);
    INXLOG_INFO(variantName, " variant compiled for: ", filePath, " (", variantSize, " bytes)");
}

void InxShaderLoader::InitGLSLBuiltResources()
{
    m_builtInResources.maxLights = 32;
    m_builtInResources.maxClipPlanes = 6;
    m_builtInResources.maxTextureUnits = 32;
    m_builtInResources.maxTextureCoords = 32;
    m_builtInResources.maxVertexAttribs = 64;
    m_builtInResources.maxVertexUniformComponents = 4096;
    m_builtInResources.maxVaryingFloats = 64;
    m_builtInResources.maxVertexTextureImageUnits = 32;
    m_builtInResources.maxCombinedTextureImageUnits = 80;
    m_builtInResources.maxTextureImageUnits = 32;
    m_builtInResources.maxFragmentUniformComponents = 4096;
    m_builtInResources.maxDrawBuffers = 32;
    m_builtInResources.maxVertexUniformVectors = 128;
    m_builtInResources.maxVaryingVectors = 16;
    m_builtInResources.maxFragmentUniformVectors = 16;
    m_builtInResources.maxVertexOutputVectors = 16;
    m_builtInResources.maxFragmentInputVectors = 16;
    m_builtInResources.minProgramTexelOffset = -8;
    m_builtInResources.maxProgramTexelOffset = 7;
    m_builtInResources.maxClipDistances = 8;
    m_builtInResources.maxComputeWorkGroupCountX = 65535;
    m_builtInResources.maxComputeWorkGroupCountY = 65535;
    m_builtInResources.maxComputeWorkGroupCountZ = 65535;
    m_builtInResources.maxComputeWorkGroupSizeX = 1024;
    m_builtInResources.maxComputeWorkGroupSizeY = 1024;
    m_builtInResources.maxComputeWorkGroupSizeZ = 64;
    m_builtInResources.maxComputeUniformComponents = 1024;
    m_builtInResources.maxComputeTextureImageUnits = 16;
    m_builtInResources.maxComputeImageUniforms = 8;
    m_builtInResources.maxComputeAtomicCounters = 8;
    m_builtInResources.maxComputeAtomicCounterBuffers = 1;
    m_builtInResources.maxVaryingComponents = 60;
    m_builtInResources.maxVertexOutputComponents = 64;
    m_builtInResources.maxGeometryInputComponents = 64;
    m_builtInResources.maxGeometryOutputComponents = 128;
    m_builtInResources.maxFragmentInputComponents = 128;
    m_builtInResources.maxImageUnits = 8;
    m_builtInResources.maxCombinedImageUnitsAndFragmentOutputs = 8;
    m_builtInResources.maxCombinedShaderOutputResources = 8;
    m_builtInResources.maxImageSamples = 0;
    m_builtInResources.maxVertexImageUniforms = 0;
    m_builtInResources.maxTessControlImageUniforms = 0;
    m_builtInResources.maxTessEvaluationImageUniforms = 0;
    m_builtInResources.maxGeometryImageUniforms = 0;
    m_builtInResources.maxFragmentImageUniforms = 8;
    m_builtInResources.maxCombinedImageUniforms = 8;
    m_builtInResources.maxGeometryTextureImageUnits = 16;
    m_builtInResources.maxGeometryOutputVertices = 256;
    m_builtInResources.maxGeometryTotalOutputComponents = 1024;
    m_builtInResources.maxGeometryUniformComponents = 1024;
    m_builtInResources.maxGeometryVaryingComponents = 64;
    m_builtInResources.maxTessControlInputComponents = 128;
    m_builtInResources.maxTessControlOutputComponents = 128;
    m_builtInResources.maxTessControlTextureImageUnits = 16;
    m_builtInResources.maxTessControlUniformComponents = 1024;
    m_builtInResources.maxTessControlTotalOutputComponents = 4096;
    m_builtInResources.maxTessEvaluationInputComponents = 128;
    m_builtInResources.maxTessEvaluationOutputComponents = 128;
    m_builtInResources.maxTessEvaluationTextureImageUnits = 16;
    m_builtInResources.maxTessEvaluationUniformComponents = 1024;
    m_builtInResources.maxTessPatchComponents = 120;
    m_builtInResources.maxPatchVertices = 32;
    m_builtInResources.maxTessGenLevel = 64;
    m_builtInResources.maxViewports = 16;
    m_builtInResources.maxVertexAtomicCounters = 0;
    m_builtInResources.maxTessControlAtomicCounters = 0;
    m_builtInResources.maxTessEvaluationAtomicCounters = 0;
    m_builtInResources.maxGeometryAtomicCounters = 0;
    m_builtInResources.maxFragmentAtomicCounters = 8;
    m_builtInResources.maxCombinedAtomicCounters = 8;
    m_builtInResources.maxAtomicCounterBindings = 1;
    m_builtInResources.maxVertexAtomicCounterBuffers = 0;
    m_builtInResources.maxTessControlAtomicCounterBuffers = 0;
    m_builtInResources.maxTessEvaluationAtomicCounterBuffers = 0;
    m_builtInResources.maxGeometryAtomicCounterBuffers = 0;
    m_builtInResources.maxFragmentAtomicCounterBuffers = 1;
    m_builtInResources.maxCombinedAtomicCounterBuffers = 1;
    m_builtInResources.maxAtomicCounterBufferSize = 16384;
    m_builtInResources.maxTransformFeedbackBuffers = 4;
    m_builtInResources.maxTransformFeedbackInterleavedComponents = 64;
    m_builtInResources.maxCullDistances = 8;
    m_builtInResources.maxCombinedClipAndCullDistances = 8;
    m_builtInResources.maxSamples = 4;
    m_builtInResources.maxMeshOutputVerticesNV = 256;
    m_builtInResources.maxMeshOutputPrimitivesNV = 512;
    m_builtInResources.maxMeshWorkGroupSizeX_NV = 32;
    m_builtInResources.maxMeshWorkGroupSizeY_NV = 1;
    m_builtInResources.maxMeshWorkGroupSizeZ_NV = 1;
    m_builtInResources.maxTaskWorkGroupSizeX_NV = 32;
    m_builtInResources.maxTaskWorkGroupSizeY_NV = 1;
    m_builtInResources.maxTaskWorkGroupSizeZ_NV = 1;
    m_builtInResources.maxMeshViewCountNV = 4;

    m_builtInResources.limits.nonInductiveForLoops = 1;
    m_builtInResources.limits.whileLoops = 1;
    m_builtInResources.limits.doWhileLoops = 1;
    m_builtInResources.limits.generalUniformIndexing = 1;
    m_builtInResources.limits.generalAttributeMatrixVectorIndexing = 1;
    m_builtInResources.limits.generalVaryingIndexing = 1;
    m_builtInResources.limits.generalSamplerIndexing = 1;
    m_builtInResources.limits.generalVariableIndexing = 1;
    m_builtInResources.limits.generalConstantMatrixVectorIndexing = 1;
}

EShLanguage InxShaderLoader::GetShaderType(const std::string &typeStr)
{
    if (typeStr == "vertex") {
        return EShLangVertex;
    } else if (typeStr == "fragment") {
        return EShLangFragment;
    }
    return EShLangCount;
}

std::unordered_map<std::string, std::string> InxShaderLoader::BuildShaderIdMap(const std::string &dir)
{
    // Return cached result if the directory was already scanned
    auto cacheIt = s_shaderIdMapCache.find(dir);
    if (cacheIt != s_shaderIdMapCache.end())
        return cacheIt->second;

    std::unordered_map<std::string, std::string> idMap;

    // Helper lambda: recursively scan a directory and populate idMap
    auto scanDir = [&](const std::string &scanPath, bool overwrite) {
        std::error_code ec;
        for (const auto &entry : std::filesystem::recursive_directory_iterator(ToFsPath(scanPath), ec)) {
            if (!entry.is_regular_file())
                continue;

            auto ext = FromFsPath(entry.path().extension());
            if (ext != ".vert" && ext != ".frag" && ext != ".glsl" && ext != ".shadingmodel")
                continue;

            // Skip _templates directory
            std::string pathStr = FromFsPath(entry.path());
            if (pathStr.find("_templates") != std::string::npos)
                continue;

            std::ifstream file(entry.path());
            if (!file.is_open())
                continue;

            std::ostringstream source;
            source << file.rdbuf();
            const std::string sourceText = source.str();
            const ShaderInfoDocument structuredInfo = ParseShaderInfo(sourceText);
            if (!structuredInfo.IsValid() || structuredInfo.name.empty())
                continue;
            const ShaderDescriptor descriptor = ParseShaderSource(sourceText, pathStr);
            const std::string &id = descriptor.shaderId;
            if (id.empty())
                continue;

            // Namespace .shadingmodel entries to prevent collision with import resolution.
            const std::string mapKey = (ext == ".shadingmodel") ? ("shadingmodel/" + id) : id;
            if (overwrite || idMap.find(mapKey) == idMap.end()) {
                idMap[mapKey] = ResolveFilesystemPath(pathStr);
            }
        }
    };

    // First, scan additional search paths (engine built-in shaders) as fallback
    for (const auto &searchPath : s_additionalSearchPaths) {
        if (searchPath != dir) {
            scanDir(searchPath, false);
        }
    }

    // Then scan the shader's own directory — these entries take priority
    scanDir(dir, true);

    // Cache the result for subsequent calls with the same directory
    s_shaderIdMapCache[dir] = idMap;

    return idMap;
}

std::string InxShaderLoader::ResolveImports(const std::string &source, const std::vector<std::string> &imports,
                                            const std::unordered_map<std::string, std::string> &shaderIdMap,
                                            std::set<std::string> &includeStack, int depth)
{
    // Guard against excessive recursion (e.g., A imports B imports C imports D ...)
    constexpr int MAX_IMPORT_DEPTH = 16;
    if (depth >= MAX_IMPORT_DEPTH) {
        std::string chain;
        for (const auto &id : includeStack) {
            if (!chain.empty())
                chain += " -> ";
            chain += id;
        }
        INXLOG_ERROR("Shader import depth exceeded maximum of ", MAX_IMPORT_DEPTH,
                     ". Import chain: ", chain.empty() ? "(unknown)" : chain);
        return source;
    }

    std::ostringstream result;
    for (const auto &importId : imports) {
        const auto mapped = shaderIdMap.find(importId);
        if (mapped == shaderIdMap.end()) {
            INXLOG_ERROR("Shader import '", importId, "' was not found in shader search paths");
            result << "// ERROR: shader import not found: " << importId << "\n";
            continue;
        }
        if (!includeStack.insert(importId).second)
            continue;

        std::ifstream importFile = OpenInputFile(mapped->second);
        if (!importFile.is_open()) {
            INXLOG_ERROR("Failed to open shader import: ", mapped->second);
            result << "// ERROR: failed to open shader import: " << importId << "\n";
            continue;
        }
        std::ostringstream importedStream;
        importedStream << importFile.rdbuf();
        const std::string importedSource = importedStream.str();
        const ShaderDescriptor importedDescriptor = ParseShaderSource(importedSource, mapped->second);
        if (!importedDescriptor.errors.empty()) {
            INXLOG_ERROR("Invalid imported ShaderInfo asset: ", mapped->second);
            result << "// ERROR: invalid ShaderInfo import: " << importId << "\n";
            includeStack.erase(importId);
            continue;
        }
        std::string importedCode = StripShaderInfoDeclaration(importedSource, ParseShaderInfo(importedSource));
        std::istringstream lines(importedCode);
        std::ostringstream withoutVersion;
        std::string line;
        while (std::getline(lines, line)) {
            const size_t first = line.find_first_not_of(" \t");
            if (first != std::string::npos && line.compare(first, 8, "#version") == 0)
                continue;
            withoutVersion << line << '\n';
        }
        const std::string resolved =
            ResolveImports(withoutVersion.str(), importedDescriptor.imports, shaderIdMap, includeStack, depth + 1);
        result << "// --- begin import: " << importId << " ---\n" << resolved;
        if (!resolved.empty() && resolved.back() != '\n')
            result << '\n';
        result << "// --- end import: " << importId << " ---\n";
    }
    result << source;
    return result.str();
}
} // namespace infernux
