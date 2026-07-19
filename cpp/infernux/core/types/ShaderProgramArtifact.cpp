#include "ShaderProgramArtifact.h"

#include <algorithm>
#include <array>
#include <iomanip>
#include <sstream>
#include <unordered_set>

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

std::string ShaderProgramVariantKey::ToString() const
{
    return program.ToString() + ":" + ShaderCompileTargetName(target);
}

size_t ShaderProgramVariantKeyHash::operator()(const ShaderProgramVariantKey &key) const noexcept
{
    uint64_t hash = static_cast<uint64_t>(ShaderProgramKeyHash{}(key.program));
    const auto target = static_cast<int32_t>(key.target);
    hash = AppendBytes(hash, &target, sizeof(target));
    return static_cast<size_t>(hash);
}

bool ShaderProgramPropertyBinding::IsValid(uint32_t materialBufferSize) const noexcept
{
    if (name.empty() || type.empty() || stages == ShaderProgramStageMask::None)
        return false;
    if (bufferOffset.has_value() == textureSlot.has_value())
        return false;
    if (textureSlot)
        return byteSize == 0 && byteAlignment == 0;
    if (byteSize == 0 || byteAlignment == 0 || (*bufferOffset % byteAlignment) != 0)
        return false;
    return *bufferOffset <= materialBufferSize && byteSize <= materialBufferSize - *bufferOffset;
}

const ShaderProgramArtifact::PassVariant *ShaderProgramArtifact::FindVariant(ShaderCompileTarget target) const noexcept
{
    for (const auto &variant : variants) {
        if (variant.target == target)
            return &variant;
    }
    return nullptr;
}

bool ShaderProgramArtifact::IsValid() const noexcept
{
    if (!key.IsValid() || key.revision == 0 || compatibilitySignature == 0 || domain >= ShaderProgramDomain::Count ||
        variants.empty() || materialBufferSize % 16 != 0) {
        return false;
    }

    if (alphaClipThresholdOffset &&
        (*alphaClipThresholdOffset > materialBufferSize || 4 > materialBufferSize - *alphaClipThresholdOffset)) {
        return false;
    }
    std::unordered_set<std::string> propertyNames;
    std::unordered_set<uint32_t> textureSlots;
    propertyNames.reserve(properties.size());
    textureSlots.reserve(properties.size());
    for (const auto &property : properties) {
        if (!property.IsValid(materialBufferSize) || !propertyNames.insert(property.name).second ||
            (property.textureSlot && !textureSlots.insert(*property.textureSlot).second)) {
            return false;
        }
    }

    static_assert(static_cast<int>(ShaderCompileTarget::Count) <= 64);
    uint64_t targets = 0;
    for (const auto &variant : variants) {
        const int targetIndex = static_cast<int>(variant.target);
        if (targetIndex < 0 || targetIndex >= static_cast<int>(ShaderCompileTarget::Count) || !variant.IsValid() ||
            variant.compatibilitySignature != compatibilitySignature) {
            return false;
        }
        const uint64_t targetBit = 1ull << static_cast<uint32_t>(targetIndex);
        if ((targets & targetBit) != 0)
            return false;
        targets |= targetBit;
    }
    return true;
}

uint64_t ComputeShaderProgramRevision(std::string_view generatedVertexSource, std::string_view generatedFragmentSource,
                                      ShaderCompileTarget target, uint64_t compatibilitySignature) noexcept
{
    uint64_t hash = AppendString(FnvOffset, generatedVertexSource);
    hash = AppendString(hash, generatedFragmentSource);
    const auto targetValue = static_cast<int32_t>(target);
    hash = AppendBytes(hash, &targetValue, sizeof(targetValue));
    hash = AppendBytes(hash, &compatibilitySignature, sizeof(compatibilitySignature));
    return hash == 0 ? 1 : hash;
}

uint64_t ComputeShaderProgramArtifactRevision(const ShaderProgramArtifact &artifact) noexcept
{
    uint64_t hash = AppendString(FnvOffset, artifact.key.stages.vertexShaderId);
    hash = AppendString(hash, artifact.key.stages.fragmentShaderId);
    const auto domain = static_cast<uint8_t>(artifact.domain);
    hash = AppendBytes(hash, &domain, sizeof(domain));
    hash = AppendString(hash, artifact.shadingModel);
    hash = AppendBytes(hash, &artifact.materialBufferSize, sizeof(artifact.materialBufferSize));
    const bool hasAlphaClipThreshold = artifact.alphaClipThresholdOffset.has_value();
    hash = AppendBytes(hash, &hasAlphaClipThreshold, sizeof(hasAlphaClipThreshold));
    if (artifact.alphaClipThresholdOffset)
        hash = AppendBytes(hash, &*artifact.alphaClipThresholdOffset, sizeof(*artifact.alphaClipThresholdOffset));
    const uint64_t propertyCount = artifact.properties.size();
    hash = AppendBytes(hash, &propertyCount, sizeof(propertyCount));
    for (const auto &property : artifact.properties) {
        hash = AppendString(hash, property.name);
        hash = AppendString(hash, property.type);
        hash = AppendString(hash, property.defaultValue);
        hash = AppendString(hash, property.textureDefault);
        const auto stages = static_cast<uint8_t>(property.stages);
        hash = AppendBytes(hash, &stages, sizeof(stages));
        hash = AppendBytes(hash, &property.hdr, sizeof(property.hdr));
        const bool hasRange = property.range.has_value();
        hash = AppendBytes(hash, &hasRange, sizeof(hasRange));
        if (property.range)
            hash = AppendBytes(hash, property.range->data(), sizeof(double) * property.range->size());
        const bool hasBufferOffset = property.bufferOffset.has_value();
        hash = AppendBytes(hash, &hasBufferOffset, sizeof(hasBufferOffset));
        if (property.bufferOffset)
            hash = AppendBytes(hash, &*property.bufferOffset, sizeof(*property.bufferOffset));
        const bool hasTextureSlot = property.textureSlot.has_value();
        hash = AppendBytes(hash, &hasTextureSlot, sizeof(hasTextureSlot));
        if (property.textureSlot)
            hash = AppendBytes(hash, &*property.textureSlot, sizeof(*property.textureSlot));
        hash = AppendBytes(hash, &property.byteSize, sizeof(property.byteSize));
        hash = AppendBytes(hash, &property.byteAlignment, sizeof(property.byteAlignment));
    }
    hash = AppendBytes(hash, &artifact.varyingInterfaceSignature, sizeof(artifact.varyingInterfaceSignature));
    hash = AppendBytes(hash, &artifact.materialLayoutSignature, sizeof(artifact.materialLayoutSignature));
    hash = AppendBytes(hash, &artifact.compatibilitySignature, sizeof(artifact.compatibilitySignature));
    std::array<const ShaderProgramArtifact::PassVariant *, static_cast<size_t>(ShaderCompileTarget::Count)> ordered{};
    if (artifact.variants.size() > ordered.size())
        return 1;
    size_t variantCount = 0;
    for (const auto &variant : artifact.variants)
        ordered[variantCount++] = &variant;
    std::sort(ordered.begin(), ordered.begin() + variantCount, [](const auto *lhs, const auto *rhs) {
        return static_cast<int>(lhs->target) < static_cast<int>(rhs->target);
    });

    const uint64_t serializedVariantCount = variantCount;
    hash = AppendBytes(hash, &serializedVariantCount, sizeof(serializedVariantCount));
    for (size_t index = 0; index < variantCount; ++index) {
        const auto *variant = ordered[index];
        const auto target = static_cast<int32_t>(variant->target);
        hash = AppendBytes(hash, &target, sizeof(target));
        hash = AppendBytes(hash, &variant->compatibilitySignature, sizeof(variant->compatibilitySignature));
        const uint64_t vertexSize = variant->vertexSpirv.size();
        hash = AppendBytes(hash, &vertexSize, sizeof(vertexSize));
        hash = AppendBytes(hash, variant->vertexSpirv.data(), variant->vertexSpirv.size());
        const uint64_t fragmentSize = variant->fragmentSpirv.size();
        hash = AppendBytes(hash, &fragmentSize, sizeof(fragmentSize));
        hash = AppendBytes(hash, variant->fragmentSpirv.data(), variant->fragmentSpirv.size());
    }
    return hash == 0 ? 1 : hash;
}

} // namespace infernux
