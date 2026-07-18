#pragma once

#include <cstdint>
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

struct ShaderProgramArtifact
{
    static constexpr uint32_t CurrentSchemaVersion = 1;

    uint32_t schemaVersion = CurrentSchemaVersion;
    ShaderProgramKey key;
    uint64_t varyingInterfaceSignature = 0;
    uint64_t materialLayoutSignature = 0;
    uint64_t compatibilitySignature = 0;
    std::vector<char> vertexSpirv;
    std::vector<char> fragmentSpirv;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return schemaVersion == CurrentSchemaVersion && key.IsValid() && key.revision != 0 &&
               compatibilitySignature != 0 && !vertexSpirv.empty() && !fragmentSpirv.empty() &&
               vertexSpirv.size() % sizeof(uint32_t) == 0 && fragmentSpirv.size() % sizeof(uint32_t) == 0;
    }
};

[[nodiscard]] uint64_t ComputeShaderProgramRevision(std::string_view generatedVertexSource,
                                                    std::string_view generatedFragmentSource,
                                                    uint64_t compatibilitySignature) noexcept;

} // namespace infernux
