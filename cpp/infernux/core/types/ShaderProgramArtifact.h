#pragma once

#include <core/types/ShaderTypes.h>

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace infernux
{

struct ShaderStagePair
{
    std::string vertexShaderId;
    std::string fragmentShaderId;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return !vertexShaderId.empty() && !fragmentShaderId.empty();
    }

    [[nodiscard]] std::string ToString() const
    {
        return vertexShaderId + "|" + fragmentShaderId;
    }

    [[nodiscard]] bool UsesShader(std::string_view shaderId) const noexcept
    {
        return vertexShaderId == shaderId || fragmentShaderId == shaderId;
    }

    friend bool operator==(const ShaderStagePair &lhs, const ShaderStagePair &rhs) noexcept
    {
        return lhs.vertexShaderId == rhs.vertexShaderId && lhs.fragmentShaderId == rhs.fragmentShaderId;
    }

    friend bool operator!=(const ShaderStagePair &lhs, const ShaderStagePair &rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

struct ShaderStagePairHash
{
    [[nodiscard]] size_t operator()(const ShaderStagePair &pair) const noexcept;
};

struct ShaderProgramKey
{
    ShaderStagePair stages;
    uint64_t revision = 0;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return stages.IsValid();
    }

    [[nodiscard]] std::string ToString() const;

    friend bool operator==(const ShaderProgramKey &lhs, const ShaderProgramKey &rhs) noexcept
    {
        return lhs.stages == rhs.stages && lhs.revision == rhs.revision;
    }

    friend bool operator!=(const ShaderProgramKey &lhs, const ShaderProgramKey &rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

struct ShaderProgramKeyHash
{
    [[nodiscard]] size_t operator()(const ShaderProgramKey &key) const noexcept;
};

struct ShaderProgramVariantKey
{
    ShaderProgramKey program;
    ShaderCompileTarget target = ShaderCompileTarget::Forward;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return program.IsValid() && target >= ShaderCompileTarget::Forward && target < ShaderCompileTarget::Count;
    }

    [[nodiscard]] std::string ToString() const;

    friend bool operator==(const ShaderProgramVariantKey &lhs, const ShaderProgramVariantKey &rhs) noexcept
    {
        return lhs.program == rhs.program && lhs.target == rhs.target;
    }

    friend bool operator!=(const ShaderProgramVariantKey &lhs, const ShaderProgramVariantKey &rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

struct ShaderProgramVariantKeyHash
{
    [[nodiscard]] size_t operator()(const ShaderProgramVariantKey &key) const noexcept;
};

enum class ShaderProgramDomain : uint8_t
{
    Mesh = 0,
    ParticleSprite,

    Count,
};

enum class ShaderProgramStageMask : uint8_t
{
    None = 0,
    Vertex = 1 << 0,
    Fragment = 1 << 1,
};

constexpr ShaderProgramStageMask operator|(ShaderProgramStageMask lhs, ShaderProgramStageMask rhs) noexcept
{
    return static_cast<ShaderProgramStageMask>(static_cast<uint8_t>(lhs) | static_cast<uint8_t>(rhs));
}

constexpr bool HasStage(ShaderProgramStageMask value, ShaderProgramStageMask stage) noexcept
{
    return (static_cast<uint8_t>(value) & static_cast<uint8_t>(stage)) != 0;
}

struct ShaderProgramPropertyBinding
{
    std::string name;
    std::string type;
    std::string defaultValue;
    std::string textureDefault;
    ShaderProgramStageMask stages = ShaderProgramStageMask::None;
    bool hdr = false;
    std::optional<std::array<double, 2>> range;
    std::optional<uint32_t> bufferOffset;
    std::optional<uint32_t> textureSlot;
    uint32_t byteSize = 0;
    uint32_t byteAlignment = 0;

    [[nodiscard]] bool IsTexture() const noexcept
    {
        return textureSlot.has_value();
    }

    [[nodiscard]] bool IsValid(uint32_t materialBufferSize) const noexcept;
};

struct ShaderProgramArtifact
{
    static constexpr uint32_t CurrentSchemaVersion = 3;

    uint32_t schemaVersion = CurrentSchemaVersion;
    ShaderProgramKey key;
    ShaderProgramDomain domain = ShaderProgramDomain::Mesh;
    std::string shadingModel;
    uint32_t materialBufferSize = 0;
    std::optional<uint32_t> alphaClipThresholdOffset;
    std::vector<ShaderProgramPropertyBinding> properties;
    uint64_t varyingInterfaceSignature = 0;
    uint64_t materialLayoutSignature = 0;
    uint64_t compatibilitySignature = 0;
    struct PassVariant
    {
        ShaderCompileTarget target = ShaderCompileTarget::Forward;
        uint64_t compatibilitySignature = 0;
        std::vector<char> vertexSpirv;
        std::vector<char> fragmentSpirv;

        [[nodiscard]] bool IsValid() const noexcept
        {
            return compatibilitySignature != 0 && !vertexSpirv.empty() && !fragmentSpirv.empty() &&
                   vertexSpirv.size() % sizeof(uint32_t) == 0 && fragmentSpirv.size() % sizeof(uint32_t) == 0;
        }
    };
    std::vector<PassVariant> variants;

    [[nodiscard]] const PassVariant *FindVariant(ShaderCompileTarget target) const noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
};

[[nodiscard]] uint64_t ComputeShaderProgramRevision(std::string_view generatedVertexSource,
                                                    std::string_view generatedFragmentSource,
                                                    ShaderCompileTarget target,
                                                    uint64_t compatibilitySignature) noexcept;

/// Computes the runtime identity of the complete, ordered pass-variant set.
/// key.revision is deliberately ignored so this can initialize that field.
[[nodiscard]] uint64_t ComputeShaderProgramArtifactRevision(const ShaderProgramArtifact &artifact) noexcept;

} // namespace infernux
