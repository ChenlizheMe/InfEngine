#include "ShaderProgramArtifact.h"

#include <iomanip>
#include <sstream>

namespace infernux
{
namespace
{
constexpr uint64_t FnvOffset = 14695981039346656037ull;
constexpr uint64_t FnvPrime = 1099511628211ull;

uint64_t AppendBytes(uint64_t hash, const void *data, size_t size) noexcept
{
    const auto *bytes = static_cast<const unsigned char *>(data);
    for (size_t index = 0; index < size; ++index) {
        hash ^= bytes[index];
        hash *= FnvPrime;
    }
    return hash;
}

uint64_t AppendString(uint64_t hash, std::string_view value) noexcept
{
    hash = AppendBytes(hash, value.data(), value.size());
    const unsigned char separator = 0xff;
    return AppendBytes(hash, &separator, sizeof(separator));
}
} // namespace

size_t ShaderStagePairHash::operator()(const ShaderStagePair &pair) const noexcept
{
    uint64_t hash = AppendString(FnvOffset, pair.vertexShaderId);
    hash = AppendString(hash, pair.fragmentShaderId);
    return static_cast<size_t>(hash);
}

std::string ShaderProgramKey::ToString() const
{
    std::ostringstream stream;
    stream << stages.ToString();
    if (revision != 0)
        stream << "@" << std::hex << std::setw(16) << std::setfill('0') << revision;
    return stream.str();
}

size_t ShaderProgramKeyHash::operator()(const ShaderProgramKey &key) const noexcept
{
    uint64_t hash = static_cast<uint64_t>(ShaderStagePairHash{}(key.stages));
    hash = AppendBytes(hash, &key.revision, sizeof(key.revision));
    return static_cast<size_t>(hash);
}

uint64_t ComputeShaderProgramRevision(std::string_view generatedVertexSource, std::string_view generatedFragmentSource,
                                      uint64_t compatibilitySignature) noexcept
{
    uint64_t hash = AppendString(FnvOffset, generatedVertexSource);
    hash = AppendString(hash, generatedFragmentSource);
    hash = AppendBytes(hash, &compatibilitySignature, sizeof(compatibilitySignature));
    const uint32_t artifactSchema = ShaderProgramArtifact::CurrentSchemaVersion;
    hash = AppendBytes(hash, &artifactSchema, sizeof(artifactSchema));
    return hash == 0 ? 1 : hash;
}

} // namespace infernux
