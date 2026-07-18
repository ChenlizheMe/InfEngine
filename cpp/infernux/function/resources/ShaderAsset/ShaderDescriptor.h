#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <function/resources/ShaderAsset/ShaderInfoSchema.h>
#include <optional>
#include <string>
#include <vector>

namespace infernux
{

struct ShaderProperty
{
    std::string name;
    std::string type;
    std::string glslType;
    std::string defaultValue;
    bool isTexture = false;
    std::string textureDefault;
    bool hdr = false;
    std::optional<std::array<double, 2>> range;
    ShaderSourceRange source;

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept
    {
        return sizeof(*this) + name.capacity() + type.capacity() + glslType.capacity() + defaultValue.capacity() +
               textureDefault.capacity();
    }
};

struct ShaderVarying
{
    std::string interpolation;
    std::string type;
    std::string name;
    std::string semantic;
    std::string space;
    ShaderSourceRange source;

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept
    {
        return sizeof(*this) + interpolation.capacity() + type.capacity() + name.capacity() + semantic.capacity() +
               space.capacity();
    }
};

struct SurfaceOptions
{
    std::string surfaceType = "opaque";
    std::string alphaClip = "off";
    std::string cullMode = "back";
    std::string blendMode = "off";
    bool receiveShadows = true;
    bool castShadows = true;

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept
    {
        return sizeof(*this) + surfaceType.capacity() + alphaClip.capacity() + cullMode.capacity() +
               blendMode.capacity();
    }
};

struct ShaderDescriptor
{
    uint32_t schemaVersion = 0;
    bool usesStructuredInfo = false;

    std::string shaderId;
    std::string filePath;
    std::string fileExtension;

    bool isVertexShader = false;
    bool isFragmentShader = false;
    bool isLibrary = false;
    bool isShadingModel = false;

    std::string shadingModel;
    bool hasExplicitType = false;
    bool hasSurfaceFunc = false;
    bool hasMainFunc = false;
    bool hasVertexFunc = false;

    SurfaceOptions surfaceOptions;
    int renderQueue = -1;
    std::string passTag;
    std::string depthWrite;
    std::string depthTest;
    std::string stencil;
    bool hidden = false;

    std::vector<ShaderProperty> properties;
    std::vector<ShaderProperty> textureProperties;
    std::vector<ShaderVarying> inputs;
    std::vector<ShaderVarying> outputs;
    std::vector<std::string> capabilities;
    std::vector<ShaderInfoEntry> entries;
    std::vector<std::string> imports;
    std::string versionDirective;

    struct TargetBlock
    {
        std::string name;
        std::string code;
    };
    std::vector<TargetBlock> targets;

    [[nodiscard]] const TargetBlock *FindTarget(const std::string &name) const
    {
        for (const auto &target : targets) {
            if (target.name == name)
                return &target;
        }
        return nullptr;
    }

    [[nodiscard]] bool NeedsLightingUBO() const
    {
        for (const auto &import : imports) {
            if (import == "lighting")
                return true;
        }
        return false;
    }

    [[nodiscard]] bool HasGBufferTarget() const
    {
        return FindTarget("gbuffer") != nullptr;
    }

    std::vector<std::string> errors;
    std::vector<std::string> warnings;

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept
    {
        size_t bytes = sizeof(*this) + shaderId.capacity() + filePath.capacity() + fileExtension.capacity() +
                       shadingModel.capacity() + surfaceOptions.GetRuntimeMemoryBytes() - sizeof(surfaceOptions) +
                       passTag.capacity() + depthWrite.capacity() + depthTest.capacity() + stencil.capacity() +
                       versionDirective.capacity();
        for (const auto &property : properties)
            bytes += property.GetRuntimeMemoryBytes();
        for (const auto &property : textureProperties)
            bytes += property.GetRuntimeMemoryBytes();
        for (const auto &varying : inputs)
            bytes += varying.GetRuntimeMemoryBytes();
        for (const auto &varying : outputs)
            bytes += varying.GetRuntimeMemoryBytes();
        for (const auto &value : capabilities)
            bytes += sizeof(value) + value.capacity();
        for (const auto &entry : entries)
            bytes += sizeof(entry) + entry.role.capacity() + entry.function.capacity();
        for (const auto &value : imports)
            bytes += sizeof(value) + value.capacity();
        for (const auto &target : targets)
            bytes += sizeof(target) + target.name.capacity() + target.code.capacity();
        for (const auto &error : errors)
            bytes += sizeof(error) + error.capacity();
        for (const auto &warning : warnings)
            bytes += sizeof(warning) + warning.capacity();
        return bytes;
    }
};

} // namespace infernux
