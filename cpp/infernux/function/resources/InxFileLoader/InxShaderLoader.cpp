#include "InxShaderLoader.hpp"

#include <SPIRV/GlslangToSpv.h>
#include <glslang/Public/ShaderLang.h>

#include <algorithm>
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

// Static members
std::vector<std::string> InxShaderLoader::s_additionalSearchPaths;
std::unordered_map<std::string, std::string> InxShaderLoader::s_templateCache;
std::unordered_map<std::string, ShaderDescriptor> InxShaderLoader::s_shadingModelCache;
std::unordered_map<std::string, std::vector<char>> InxShaderLoader::s_shadowVariantCache;
std::unordered_map<std::string, std::vector<char>> InxShaderLoader::s_shadowVertexVariantCache;
std::unordered_map<std::string, std::vector<char>> InxShaderLoader::s_gbufferVariantCache;
std::string InxShaderLoader::s_lastCompileError;
std::unordered_map<std::string, std::unordered_map<std::string, std::string>> InxShaderLoader::s_shaderIdMapCache;

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
                               ShaderStageVisibility stage, const std::string &stageName,
                               std::vector<std::string> &errors)
{
    if (artifact.materialBufferSize > 0) {
        const auto uniformBuffer =
            std::find_if(reflection.GetUniformBuffers().begin(), reflection.GetUniformBuffers().end(),
                         [](const UniformBufferInfo &buffer) { return buffer.name == "MaterialProperties"; });
        if (uniformBuffer == reflection.GetUniformBuffers().end()) {
            errors.push_back(stageName + " SPIR-V reflection is missing the linked MaterialProperties block");
        } else {
            if (uniformBuffer->set != GlslStageInterfaceEmitter::MaterialDescriptorSet ||
                uniformBuffer->binding != GlslStageInterfaceEmitter::MaterialBufferBinding) {
                errors.push_back(stageName + " MaterialProperties reflected at an unexpected descriptor binding");
            }
            if (uniformBuffer->size != artifact.materialBufferSize) {
                errors.push_back(stageName + " MaterialProperties size does not match the linked material layout");
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
                if (property.bufferOffset)
                    validateMember(property.schema.name, *property.bufferOffset);
            }
            if (artifact.alphaClipThresholdOffset)
                validateMember("_AlphaClipThreshold", *artifact.alphaClipThresholdOffset);
        }
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
        const uint32_t expectedBinding = GlslStageInterfaceEmitter::FirstTextureBinding + *property.textureSlot;
        if (image->set != GlslStageInterfaceEmitter::MaterialDescriptorSet || image->binding != expectedBinding) {
            errors.push_back(stageName + " texture '" + property.schema.name +
                             "' reflected at an unexpected descriptor binding");
        }
    }
}
} // namespace

void InxShaderLoader::InvalidateDirectoryCache(const std::string &dir)
{
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
    if (!content) {
        INXLOG_ERROR("Invalid shader content for metadata creation");
        return;
    }
    metaData.Init(content, contentSize, filePath, ResourceType::Shader);

    // Parse shader into structured descriptor (single pass)
    auto desc = ParseShaderSource(std::string(content, contentSize), filePath);

    // ----------------------------------------------------------------
    // Apply Surface Options defaults.
    // @surface_type determines default render state when the user has not
    // explicitly overridden individual settings.
    // ----------------------------------------------------------------
    if (desc.surfaceOptions.surfaceType == "transparent") {
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
    metaData.AddMetadata("shader_schema_version", static_cast<int>(desc.schemaVersion));
    metaData.AddMetadata("shader_schema_format",
                         desc.usesStructuredInfo ? std::string("ShaderInfo") : std::string("LegacyAnnotations"));
    metaData.AddMetadata("shader_inputs", serializeVaryings(desc.inputs));
    metaData.AddMetadata("shader_outputs", serializeVaryings(desc.outputs));
    metaData.AddMetadata("shader_capabilities", nlohmann::json(desc.capabilities).dump());
    metaData.AddMetadata("shader_imports", nlohmann::json(desc.imports).dump());
    nlohmann::json entries = nlohmann::json::object();
    for (const auto &entry : desc.entries)
        entries[entry.role] = entry.function;
    metaData.AddMetadata("shader_entries", entries.dump());
    metaData.AddMetadata("shader_lighting_type", desc.shadingModel.empty() ? "unlit" : desc.shadingModel);
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
// ParseAnnotation — unified single-line annotation parser
// ============================================================================

std::optional<std::pair<std::string, std::string>> InxShaderLoader::ParseAnnotation(const std::string &line)
{
    // Find the effective start (skip leading whitespace and optional "// " prefix)
    size_t pos = line.find_first_not_of(" \t");
    if (pos == std::string::npos)
        return std::nullopt;

    std::string trimmed = line.substr(pos);

    // Handle "// @key: value" format (already-commented annotations)
    if (trimmed.rfind("// @", 0) == 0) {
        trimmed = trimmed.substr(3); // skip "// "
    }

    // Must start with '@'
    if (trimmed.empty() || trimmed[0] != '@')
        return std::nullopt;

    // Find colon separator
    size_t colonPos = trimmed.find(':');

    // Handle annotations without value (e.g. "@hidden")
    if (colonPos == std::string::npos) {
        std::string key = trimmed.substr(1); // skip '@'
        // Trim trailing whitespace from key
        size_t keyEnd = key.find_last_not_of(" \t\r\n");
        if (keyEnd != std::string::npos)
            key = key.substr(0, keyEnd + 1);
        return std::make_pair(key, std::string{});
    }

    // Extract key (between '@' and ':')
    std::string key = trimmed.substr(1, colonPos - 1);
    size_t keyEnd = key.find_last_not_of(" \t");
    if (keyEnd != std::string::npos)
        key = key.substr(0, keyEnd + 1);

    // Extract value (after ':')
    std::string value = trimmed.substr(colonPos + 1);
    size_t valStart = value.find_first_not_of(" \t");
    size_t valEnd = value.find_last_not_of(" \t\r\n");
    if (valStart != std::string::npos && valEnd != std::string::npos)
        value = value.substr(valStart, valEnd - valStart + 1);
    else
        value.clear();

    return std::make_pair(key, value);
}

// ============================================================================
// ParseShaderSource — build ShaderDescriptor from raw source text
// ============================================================================

ShaderDescriptor InxShaderLoader::ParseShaderSource(const std::string &source, const std::string &filePath) const
{
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
    std::string sourceForLegacyScan = source;
    if (shaderInfo.foundDeclaration) {
        desc.usesStructuredInfo = true;
        desc.schemaVersion = shaderInfo.formatVersion;
        sourceForLegacyScan = StripShaderInfoDeclaration(source, shaderInfo);
        if (!shaderInfo.name.empty())
            desc.shaderId = shaderInfo.name;
        if (!shaderInfo.shadingModel.empty()) {
            desc.shadingModel = toLower(shaderInfo.shadingModel);
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
        desc.capabilities = shaderInfo.capabilities;
        desc.entries = shaderInfo.entries;
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

    const ShaderEntryPointSet entryPoints = DetectShaderEntryPoints(sourceForLegacyScan);
    desc.hasSurfaceFunc = entryPoints.surface;
    desc.hasMainFunc = entryPoints.main;
    desc.hasVertexFunc = entryPoints.vertex;

    // Property parser helper
    auto parseProperty = [&](const std::string &value) {
        size_t firstComma = value.find(',');
        size_t secondComma = (firstComma != std::string::npos) ? value.find(',', firstComma + 1) : std::string::npos;
        if (firstComma == std::string::npos || secondComma == std::string::npos)
            return;

        auto trim = [](std::string s) {
            size_t st = s.find_first_not_of(" \t");
            size_t en = s.find_last_not_of(" \t\r\n");
            return (st != std::string::npos && en != std::string::npos) ? s.substr(st, en - st + 1) : std::string{};
        };

        std::string name = trim(value.substr(0, firstComma));
        std::string propType = trim(value.substr(firstComma + 1, secondComma - firstComma - 1));
        std::string defaultVal = trim(value.substr(secondComma + 1));

        ShaderProperty prop;
        prop.name = name;
        prop.type = propType;
        prop.defaultValue = defaultVal;

        if (propType == "Texture2D") {
            prop.isTexture = true;
            prop.textureDefault = defaultVal;
            desc.textureProperties.push_back(prop);
        } else {
            auto it = typeMap.find(propType);
            if (it != typeMap.end()) {
                prop.glslType = it->second;
                desc.properties.push_back(prop);
            }
        }
    };

    // Current target context (only used for .shadingmodel files)
    std::string currentTargetName;
    std::unordered_map<std::string, std::vector<std::string>> targetCodeSections;

    // Annotation dispatch table
    using Handler = std::function<void(const std::string &)>;
    std::unordered_map<std::string, Handler> handlers = {
        {"shading_model",
         [&](const std::string &v) {
             desc.shadingModel = toLower(v);
             desc.hasExplicitType = true;
         }},
        {"shader_id", [&](const std::string &v) { desc.shaderId = v; }},
        {"property", [&](const std::string &v) { parseProperty(v); }},
        {"cull", [&](const std::string &v) { desc.surfaceOptions.cullMode = toLower(v); }},
        {"depth_write", [&](const std::string &v) { desc.depthWrite = toLower(v); }},
        {"depth_test", [&](const std::string &v) { desc.depthTest = toLower(v); }},
        {"blend", [&](const std::string &v) { desc.surfaceOptions.blendMode = toLower(v); }},
        {"queue",
         [&](const std::string &v) {
             try {
                 desc.renderQueue = std::stoi(v);
             } catch (...) {
                 INXLOG_WARN("[ShaderLoader] Invalid @queue value: '", v, "'");
             }
         }},
        {"pass_tag", [&](const std::string &v) { desc.passTag = toLower(v); }},
        {"stencil", [&](const std::string &v) { desc.stencil = toLower(v); }},
        {"hidden", [&](const std::string &) { desc.hidden = true; }},
        {"alpha_test", [&](const std::string &v) { desc.surfaceOptions.alphaClip = v; }},
        {"alpha_clip", [&](const std::string &v) { desc.surfaceOptions.alphaClip = v; }},
        {"surface_type", [&](const std::string &v) { desc.surfaceOptions.surfaceType = toLower(v); }},
        {"blend_mode", [&](const std::string &v) { desc.surfaceOptions.blendMode = toLower(v); }},
        {"receive_shadows", [&](const std::string &v) { desc.surfaceOptions.receiveShadows = (toLower(v) != "off"); }},
        {"cast_shadows", [&](const std::string &v) { desc.surfaceOptions.castShadows = (toLower(v) != "off"); }},
        {"import", [&](const std::string &v) { desc.imports.push_back(v); }},
        {"target", [&](const std::string &v) { currentTargetName = toLower(v); }},
    };

    // Scan source line by line
    std::istringstream stream(sourceForLegacyScan);
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

        // Try parsing as annotation
        auto annotation = ParseAnnotation(line);
        if (annotation) {
            if (desc.usesStructuredInfo) {
                desc.errors.push_back(filePath + ":" + std::to_string(lineNumber) +
                                      ":1: ShaderInfo assets cannot mix legacy @ annotations");
                continue;
            }
            auto it = handlers.find(annotation->first);
            if (it != handlers.end()) {
                it->second(annotation->second);
            }
            continue;
        }

        // For .shadingmodel files, collect code into target sections
        if (desc.isShadingModel && !currentTargetName.empty()) {
            targetCodeSections[currentTargetName].push_back(line);
        }
    }

    // Build target blocks for .shadingmodel files
    if (desc.isShadingModel) {
        for (auto &[name, lines] : targetCodeSections) {
            ShaderDescriptor::TargetBlock block;
            block.name = name;
            std::ostringstream codeStream;
            for (const auto &l : lines)
                codeStream << l << "\n";
            block.code = codeStream.str();
            desc.targets.push_back(std::move(block));
        }
    }

    return desc;
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

    // Look up via namespaced key ("shadingmodel/<id>") to avoid collision with @import resolution
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

    // Parse using the same ParseShaderSource (which handles @target blocks for .shadingmodel)
    ShaderDescriptor desc = ParseShaderSource(content.str(), filePath);

    // Cache the result
    s_shadingModelCache[modelName] = desc;

    INXLOG_DEBUG("Loaded shading model '", modelName, "' from ", filePath, " with ", desc.targets.size(), " targets");

    return desc;
}

// ============================================================================
// GenerateGLSL — produce compilable GLSL from descriptor + resolved source
// ============================================================================

std::string InxShaderLoader::GenerateGLSL(const ShaderDescriptor &desc, const std::string &resolvedSource,
                                          const ShaderDescriptor *shadingModel, ShaderCompileTarget target,
                                          const ShaderProgramInterfaceArtifact *linkedInterface) const
{
    // Separate resolved source into: version line, annotation lines, code lines
    std::istringstream stream(resolvedSource);
    std::string line;
    std::string versionLine;
    std::vector<std::string> annotationLines;
    std::vector<std::string> codeLines;

    while (std::getline(stream, line)) {
        size_t start = line.find_first_not_of(" \t");
        std::string trimmedLine = (start != std::string::npos) ? line.substr(start) : "";

        if (!trimmedLine.empty() && trimmedLine[0] == '@') {
            annotationLines.push_back("// " + line);
        } else if (!trimmedLine.empty() && trimmedLine.rfind("// @", 0) == 0) {
            annotationLines.push_back(line);
        } else if (trimmedLine.rfind("#version", 0) == 0) {
            versionLine = line;
        } else {
            codeLines.push_back(line);
        }
    }

    std::ostringstream codeSourceStream;
    for (const auto &codeLine : codeLines)
        codeSourceStream << codeLine << '\n';
    const std::string codeSource = codeSourceStream.str();
    const bool userHasLayoutDecls = FindShaderLayoutDeclaration(codeSource).has_value();
    const ShaderEntryPointSet generatedEntries = DetectShaderEntryPoints(codeSource);
    const bool hasSurfaceFunc = generatedEntries.surface;
    const bool hasMainFunc = generatedEntries.main;

    std::ostringstream result;

    // #version must be first line
    result << (versionLine.empty() ? "#version 450" : versionLine) << "\n";

    // Annotation lines as comments
    for (const auto &ann : annotationLines) {
        result << ann << "\n";
    }

    // ================================================================
    // Determine shading model capabilities
    // ================================================================
    bool needsLightingUBO = false;
    bool hasGBufferTarget = false;
    // Shadow alpha-clip: when @alpha_clip is active, the shadow pass needs
    // texture samplers, MaterialProperties UBO, and user surface() code
    // so it can sample alpha and discard transparent fragments.
    bool shadowNeedsAlphaClip = false;
    if (shadingModel) {
        // Shadow pass never needs LightingUBO — depth only
        if (target != ShaderCompileTarget::Shadow) {
            needsLightingUBO = shadingModel->NeedsLightingUBO();
        }
        // GBuffer target: always enabled — engine provides default packing
        // when the shadingmodel has no explicit @target: gbuffer block.
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
    else if (target == ShaderCompileTarget::GBuffer)
        result << "#define INX_GBUFFER_PASS 1\n";
    else if (target == ShaderCompileTarget::Shadow)
        result << "#define INX_SHADOW_PASS 1\n";

    // ================================================================
    // Inject engine globals UBO — always available except shadow
    // For shadow vertex with vertex(), inject at set 1 (shadow globals set)
    // For shadow fragment with alpha clip, inject at set 1 (libraries need _Globals)
    // ================================================================
    bool shadowVertexNeedsGlobals =
        (target == ShaderCompileTarget::Shadow && desc.isVertexShader && desc.hasVertexFunc);
    bool shadowFragmentNeedsGlobals =
        (target == ShaderCompileTarget::Shadow && desc.isFragmentShader && shadowNeedsAlphaClip);
    if (target != ShaderCompileTarget::Shadow) {
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
    if (!userHasLayoutDecls) {
        if (desc.isVertexShader && target == ShaderCompileTarget::Shadow) {
            // Shadow vertex variant: use shadow-specific builtins (shadow UBO at set 0)
            result << "\n// Auto-generated shadow vertex builtins\n";
            result << LoadTemplate("shadow_vertex_builtins.glsl") << "\n";
        } else if (desc.isFragmentShader && target == ShaderCompileTarget::Shadow) {
            // Shadow fragment variant: only fragment varyings for interface matching
            // No InxGlobals (set 2) — shadow pipeline layout only provides set 0
            result << "\n// Auto-generated fragment varyings (shadow — interface match)\n";
            result << LoadTemplate("fragment_varyings.glsl") << "\n";
        } else {
            if (desc.isVertexShader) {
                // Unified vertex builtins for all shading models
                result << "\n// Auto-generated vertex builtins (unified)\n";
                result << LoadTemplate("vertex_builtins.glsl") << "\n";
                if (linkedInterface && !desc.outputs.empty())
                    result << GlslStageInterfaceEmitter::EmitVertexDeclarations(*linkedInterface, desc);
            } else if (desc.isFragmentShader && (desc.hasExplicitType || hasSurfaceFunc)) {
                // Forward / GBuffer: full varying + output injection
                // LightingUBO — only when the shading model requires it
                if (needsLightingUBO) {
                    result << "\n// Auto-generated LightingUBO (required by shading model)\n";
                    result << LoadTemplate("lighting_ubo.glsl") << "\n";
                }

                // Unified fragment varying inputs
                result << "\n// Auto-generated fragment varyings (unified)\n";
                result << LoadTemplate("fragment_varyings.glsl") << "\n";
                if (linkedInterface && !desc.inputs.empty())
                    result << GlslStageInterfaceEmitter::EmitFragmentDeclarations(*linkedInterface);

                // Fragment output declarations
                if (target == ShaderCompileTarget::GBuffer && hasGBufferTarget) {
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

    // ================================================================
    // Texture sampler declarations  (skip for shadow unless alpha clip)
    // ================================================================
    int texBaseBinding = needsLightingUBO ? 2 : 1;
    // Shadow alpha-clip fragment: pack textures + material UBO into set 2
    // (the per-material shadow descriptor set), starting from binding 0.
    const bool shadowAlphaFragment =
        (target == ShaderCompileTarget::Shadow && shadowNeedsAlphaClip && desc.isFragmentShader);
    int shadowTexBaseBinding = 0; // textures start at binding 0 in set 2
    if (target != ShaderCompileTarget::Shadow || shadowNeedsAlphaClip) {
        if (linkedInterface && target == ShaderCompileTarget::Forward) {
            const ShaderStageVisibility stage =
                desc.isVertexShader ? ShaderStageVisibility::Vertex : ShaderStageVisibility::Fragment;
            const std::string declarations =
                GlslStageInterfaceEmitter::EmitTextureDeclarations(*linkedInterface, stage);
            if (!declarations.empty())
                result << "\n// Linked material texture bindings\n" << declarations << "\n";
        } else if (!desc.textureProperties.empty() && desc.isFragmentShader) {
            result << "\n// Auto-generated texture samplers from @property annotations\n";
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
    bool needsMaterialUBO =
        linkedInterface ? linkedInterface->materialBufferSize > 0 : (!desc.properties.empty() || isSurfaceFragment);
    // Vertex shaders with vertex() may need material properties in shadow (as constants)
    bool shadowVertexNeedsMaterial = (target == ShaderCompileTarget::Shadow && desc.isVertexShader &&
                                      desc.hasVertexFunc && !desc.properties.empty());
    if (target != ShaderCompileTarget::Shadow || shadowNeedsAlphaClip || shadowVertexNeedsMaterial) {
        if (needsMaterialUBO) {
            // Vertex shader MaterialProperties gets a dedicated high binding (14) to
            // avoid collision with fragment-side bindings (lighting UBO, textures, etc.)
            int materialBinding;
            if (linkedInterface && target == ShaderCompileTarget::Forward) {
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
                materialBinding = texBaseBinding + static_cast<int>(desc.textureProperties.size());
            }
            result << "\n// Auto-generated MaterialProperties UBO from @property annotations\n";
            if (linkedInterface && target == ShaderCompileTarget::Forward) {
                result << "layout(std140, set = " << GlslStageInterfaceEmitter::MaterialDescriptorSet
                       << ", binding = " << materialBinding << ") uniform MaterialProperties {\n";
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
        // For shadow alpha-clip fragments, skip import blocks that reference
        // UBOs not available in the shadow pipeline (LightingUBO, shadowMap).
        // The lighting.glsl import depends on the LightingUBO declaration
        // which is (correctly) not injected for shadow — strip its block.
        bool skipBlock = false;
        for (const auto &codeLine : codeLines) {
            if (shadowAlphaFragment) {
                if (codeLine.find("// --- begin @import: lighting ---") != std::string::npos) {
                    skipBlock = true;
                    continue;
                }
                if (skipBlock && codeLine.find("// --- end @import: lighting ---") != std::string::npos) {
                    skipBlock = false;
                    continue;
                }
                if (skipBlock)
                    continue;
            }
            result << codeLine << "\n";
        }
    } else if (target == ShaderCompileTarget::Shadow && desc.isVertexShader && desc.hasVertexFunc &&
               desc.properties.empty()) {
        // Shadow vertex with vertex() but no @property — just include user code
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
                    result << "    float alpha = texture(" << alphaTex << ", uv).a";
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
        } else {
            // Forward / GBuffer: inject evaluate() from shading model
            std::string targetName = (target == ShaderCompileTarget::GBuffer) ? "gbuffer" : "forward";
            if (shadingModel) {
                const auto *targetBlock = shadingModel->FindTarget(targetName);
                if (targetBlock && !targetBlock->code.empty()) {
                    result << "\n// evaluate() from shading model: " << desc.shadingModel << " (@target: " << targetName
                           << ")\n";
                    result << targetBlock->code << "\n";
                } else if (target == ShaderCompileTarget::GBuffer) {
                    // No custom @target: gbuffer — use engine default packing
                    result << "\n// Default GBuffer packing (engine-provided)\n";
                    result << LoadTemplate("default_gbuffer_evaluate.glsl") << "\n";
                } else {
                    INXLOG_WARN("Shading model '", desc.shadingModel, "' has no @target: ", targetName, " block");
                }
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
                std::string mainTpl = LoadTemplate("surface_main.glsl");
                ReplacePlaceholder(mainTpl, "${SURFACE_CALL}",
                                   linkedInterface ? GlslStageInterfaceEmitter::EmitSurfaceCall(*linkedInterface)
                                                   : "    surface(s);");
                result << "\n" << mainTpl << "\n";
            }
        }
    }

    // ================================================================
    // Auto-generated main() for vertex shaders
    // ================================================================
    if (desc.isVertexShader && !hasMainFunc && !userHasLayoutDecls) {
        std::string vertexCall;
        if (desc.hasVertexFunc) {
            vertexCall = linkedInterface ? GlslStageInterfaceEmitter::EmitVertexCall(*linkedInterface, desc)
                                         : "    vertex(v);\n";
        }
        std::string templateName =
            (target == ShaderCompileTarget::Shadow) ? "shadow_vertex_main.glsl" : "vertex_main.glsl";
        std::string mainTpl = LoadTemplate(templateName);
        ReplacePlaceholder(mainTpl, "${VERTEX_CALL}", vertexCall);
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

    // Stage 2: Resolve @import directives
    std::string resolvedSource =
        desc.usesStructuredInfo ? StripShaderInfoDeclaration(source, ParseShaderInfo(source)) : source;
    if (desc.usesStructuredInfo) {
        for (auto import = desc.imports.rbegin(); import != desc.imports.rend(); ++import)
            resolvedSource = "@import: " + *import + "\n" + resolvedSource;
    }
    const ShaderDescriptor *shadingModelPtr = nullptr;
    ShaderDescriptor shadingModelDesc;

    if (!filePath.empty()) {
        std::filesystem::path shaderPath = ToFsPath(filePath);
        std::string baseDir = FromFsPath(shaderPath.parent_path());
        auto shaderIdMap = BuildShaderIdMap(baseDir);

        std::set<std::string> includeStack;
        if (!desc.shaderId.empty()) {
            includeStack.insert(desc.shaderId);
        }

        // Load the referenced .shadingmodel (if any) and auto-inject its @import dependencies
        if (!desc.shadingModel.empty() && desc.isFragmentShader && desc.hasSurfaceFunc && !desc.hasMainFunc) {
            shadingModelDesc = LoadShadingModel(desc.shadingModel, shaderIdMap);
            if (shadingModelDesc.errors.empty()) {
                shadingModelPtr = &shadingModelDesc;

                // Auto-inject the shading model's @import dependencies into the source
                // using the IR imports list for dedup instead of regex on source text
                std::set<std::string> existingImports(desc.imports.begin(), desc.imports.end());
                for (const auto &imp : shadingModelDesc.imports) {
                    if (existingImports.find(imp) == existingImports.end()) {
                        resolvedSource = "@import: " + imp + "\n" + resolvedSource;
                        existingImports.insert(imp);
                    }
                }
            }
        }

        // Auto-inject @import: surface for surface() shader model
        if (desc.hasSurfaceFunc && !desc.hasMainFunc && desc.isFragmentShader) {
            // Check via IR: desc.imports already parsed all @import from source
            bool hasSurfaceImport = false;
            bool hasObjectUtilsImport = false;
            for (const auto &imp : desc.imports) {
                if (imp == "surface")
                    hasSurfaceImport = true;
                if (imp == "lib/object_utils")
                    hasObjectUtilsImport = true;
            }
            if (!hasSurfaceImport) {
                resolvedSource = "@import: surface\n" + resolvedSource;
            }
            if (!hasObjectUtilsImport) {
                resolvedSource = "@import: lib/object_utils\n" + resolvedSource;
            }
        }

        resolvedSource = ResolveImports(resolvedSource, shaderIdMap, includeStack, 0);
    }

    // Stage 3: Generate GLSL from descriptor + resolved source + shading model
    return GenerateGLSL(desc, resolvedSource, shadingModelPtr, target, linkedInterface);
}

std::shared_ptr<std::vector<char>> InxShaderLoader::Compile(const char *content, size_t contentSize,
                                                            InxResourceMeta &metaData)
{
    s_lastCompileError.clear();

    if (!content) {
        INXLOG_ERROR("Invalid shader content");
        s_lastCompileError = "Invalid shader content";
        return nullptr;
    }

    std::string filePath = metaData.GetDataAs<std::string>("file_path");
    std::string type = metaData.GetDataAs<std::string>("type");
    INXLOG_DEBUG("InxShaderLoader::Compile - Compiling shader: ", filePath);

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
            CompileVariant(content, filePath, ShaderCompileTarget::Shadow, "Shadow", s_shadowVariantCache);

            if (!sourceDescriptor.shadingModel.empty() && sourceDescriptor.shadingModel != "custom") {
                CompileVariant(content, filePath, ShaderCompileTarget::GBuffer, "GBuffer", s_gbufferVariantCache);
            }
        }
    }

    // ---- Shadow vertex variant compilation for surface vertex shaders ----
    if (type == "vertex") {
        if (!sourceDescriptor.hasMainFunc && !sourceDescriptor.isLibrary) {
            CompileVariant(content, filePath, ShaderCompileTarget::Shadow, "ShadowVertex", s_shadowVertexVariantCache,
                           EShLangVertex);
        }
    }

    return compiledData;
}

LinkedShaderProgramCompilation InxShaderLoader::CompileLinkedForward(const std::string &vertexSource,
                                                                     const std::string &vertexPath,
                                                                     const std::string &fragmentSource,
                                                                     const std::string &fragmentPath)
{
    LinkedShaderProgramCompilation compilation;
    const ShaderDescriptor vertex = ParseShaderSource(vertexSource, vertexPath);
    const ShaderDescriptor fragment = ParseShaderSource(fragmentSource, fragmentPath);
    compilation.interfaceArtifact = ShaderStageLinker::Link(vertex, fragment);
    compilation.errors.insert(compilation.errors.end(), vertex.errors.begin(), vertex.errors.end());
    compilation.errors.insert(compilation.errors.end(), fragment.errors.begin(), fragment.errors.end());
    if (!compilation.interfaceArtifact.IsValid() || !compilation.errors.empty())
        return compilation;

    compilation.generatedVertexSource =
        PreprocessShaderSource(vertexSource, vertexPath, ShaderCompileTarget::Forward, &compilation.interfaceArtifact);
    compilation.generatedFragmentSource = PreprocessShaderSource(
        fragmentSource, fragmentPath, ShaderCompileTarget::Forward, &compilation.interfaceArtifact);

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
                                  "vertex", compilation.errors);
    }
    if (!fragmentReflection.Reflect(compilation.fragmentSpirv, VK_SHADER_STAGE_FRAGMENT_BIT)) {
        compilation.errors.push_back("failed to reflect linked fragment SPIR-V");
    } else {
        ValidateReflectedVaryings(fragmentReflection.GetInputs(), compilation.interfaceArtifact, "fragment",
                                  compilation.errors);
        ValidateReflectedMaterial(fragmentReflection, compilation.interfaceArtifact, ShaderStageVisibility::Fragment,
                                  "fragment", compilation.errors);
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

void InxShaderLoader::CompileVariant(const char *content, const std::string &filePath, ShaderCompileTarget target,
                                     const std::string &variantName,
                                     std::unordered_map<std::string, std::vector<char>> &cache, EShLanguage shaderType)
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
    cache[filePath] = std::move(spirv);
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
    m_builtInResources.maxVaryingVectors = 8;
    m_builtInResources.maxFragmentUniformVectors = 16;
    m_builtInResources.maxVertexOutputVectors = 16;
    m_builtInResources.maxFragmentInputVectors = 15;
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
            bool hasAuthoredId = structuredInfo.foundDeclaration && !structuredInfo.name.empty();
            if (!hasAuthoredId) {
                std::istringstream annotations(sourceText);
                std::string line;
                for (int lineCount = 0; lineCount < 20 && std::getline(annotations, line); ++lineCount) {
                    const auto annotation = ParseAnnotation(line);
                    if (annotation && annotation->first == "shader_id" && !annotation->second.empty()) {
                        hasAuthoredId = true;
                        break;
                    }
                }
            }
            if (!hasAuthoredId)
                continue;
            const ShaderDescriptor descriptor = ParseShaderSource(sourceText, pathStr);
            const std::string &id = descriptor.shaderId;
            if (id.empty())
                continue;

            // Namespace .shadingmodel entries to prevent collision with import resolution.
            const std::string mapKey = (ext == ".shadingmodel") ? ("shadingmodel/" + id) : id;
            if (overwrite || idMap.find(mapKey) == idMap.end()) {
                std::error_code canonicalError;
                std::string canonicalPath = FromFsPath(std::filesystem::canonical(entry.path(), canonicalError));
                if (!canonicalError)
                    idMap[mapKey] = canonicalPath;
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

std::string InxShaderLoader::ResolveImports(const std::string &source,
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
        INXLOG_ERROR("Shader @import depth exceeded maximum of ", MAX_IMPORT_DEPTH,
                     ". Import chain: ", chain.empty() ? "(unknown)" : chain);
        return source;
    }

    std::istringstream stream(source);
    std::ostringstream result;
    std::string line;

    while (std::getline(stream, line)) {
        auto annotation = ParseAnnotation(line);
        if (annotation && annotation->first == "import") {
            std::string importId = annotation->second;

            // Look up the shader_id in the map
            auto it = shaderIdMap.find(importId);
            if (it == shaderIdMap.end()) {
                INXLOG_ERROR("Shader @import: shader_id '", importId, "' not found in shaders directory");
                result << "// ERROR: @import shader_id not found: " << importId << "\n";
                continue;
            }

            const std::string &importPath = it->second;

            // Diamond-dedup: file already imported via another path (e.g. A→B→D + A→C→D)
            if (includeStack.count(importId) > 0) {
                INXLOG_DEBUG("@import dedup, already included: ", importId);
                result << "// @import dedup: " << importId << " (already included)\n";
                continue;
            }

            // Read the imported file
            std::ifstream importFile = OpenInputFile(importPath);
            if (!importFile.is_open()) {
                INXLOG_ERROR("Failed to open @import file: ", importPath);
                result << "// ERROR: failed to open @import: " << importId << "\n";
                continue;
            }

            std::ostringstream importContent;
            importContent << importFile.rdbuf();
            importFile.close();

            // Strip #version directive from imported content (the parent file's #version takes precedence)
            std::string content = importContent.str();
            std::istringstream contentStream(content);
            std::ostringstream strippedContent;
            std::string contentLine;
            while (std::getline(contentStream, contentLine)) {
                size_t firstChar = contentLine.find_first_not_of(" \t");
                if (firstChar != std::string::npos && contentLine.compare(firstChar, 8, "#version") == 0)
                    continue;
                strippedContent << contentLine << "\n";
            }

            // Recursively resolve imports in the imported file
            includeStack.insert(importId);
            std::string resolvedContent = ResolveImports(strippedContent.str(), shaderIdMap, includeStack, depth + 1);
            // Do NOT erase importId: keep it in the set to prevent diamond
            // imports from including the same file twice (A→B→D + A→C→D
            // would otherwise inline D twice, causing GLSL redefinitions).

            // Insert the resolved content (with markers for debugging)
            result << "// --- begin @import: " << importId << " ---\n";
            result << resolvedContent;
            // Ensure newline at end of imported content
            if (!resolvedContent.empty() && resolvedContent.back() != '\n') {
                result << "\n";
            }
            result << "// --- end @import: " << importId << " ---\n";
        } else {
            result << line << "\n";
        }
    }

    return result.str();
}
} // namespace infernux
