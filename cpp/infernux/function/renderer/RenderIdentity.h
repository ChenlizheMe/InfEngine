#pragma once

#include <function/scene/ObjectHandle.h>

#include <cstddef>
#include <cstdint>

namespace infernux
{

/// Identifies which subsystem owns a render proxy.
enum class RenderDomain : uint8_t
{
    Unknown = 0,
    SceneGeometry,
    Particle,
    ComponentGizmo,
    EditorGizmo,
    EditorTool,
    ScreenUI,
    Skybox,
};

using RenderDomainMask = uint32_t;

[[nodiscard]] constexpr RenderDomainMask RenderDomainBit(RenderDomain domain) noexcept
{
    return domain == RenderDomain::Unknown ? 0u : (1u << static_cast<uint8_t>(domain));
}

/// Compact identity copied through draw-call sorting and filtering hot paths.
/// The proxy lifetime is process-local and changes whenever its source
/// component is replaced, even if serialized component IDs are reused.
struct RenderDrawIdentity
{
    uint64_t proxyLifetime = 0;
    uint32_t primitiveIndex = 0;
    RenderDomain domain = RenderDomain::Unknown;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return proxyLifetime != 0 && domain != RenderDomain::Unknown;
    }

    bool operator==(const RenderDrawIdentity &rhs) const noexcept
    {
        return proxyLifetime == rhs.proxyLifetime && primitiveIndex == rhs.primitiveIndex && domain == rhs.domain;
    }

    bool operator!=(const RenderDrawIdentity &rhs) const noexcept
    {
        return !(*this == rhs);
    }
};

static_assert(sizeof(RenderDrawIdentity) <= 16, "RenderDrawIdentity must remain cheap to copy in draw-call hot paths");

struct RenderDrawIdentityHash
{
    [[nodiscard]] size_t operator()(const RenderDrawIdentity &identity) const noexcept
    {
        uint64_t hash = identity.proxyLifetime;
        hash ^= static_cast<uint64_t>(identity.primitiveIndex) + 0x9e3779b97f4a7c15ull + (hash << 6u) + (hash >> 2u);
        hash ^= static_cast<uint64_t>(identity.domain) + 0x9e3779b97f4a7c15ull + (hash << 6u) + (hash >> 2u);
        return static_cast<size_t>(hash);
    }
};

/// Stable identity of one source that can produce render primitives.
///
/// Scene-backed proxies retain both the owning GameObject lifetime and the
/// renderer Component lifetime. Synthetic proxies use a domain-local ID.
struct RenderProxyHandle
{
    ObjectHandle owner;
    ObjectHandle source;
    uint64_t syntheticId = 0;
    RenderDomain domain = RenderDomain::Unknown;

    [[nodiscard]] static RenderProxyHandle FromScene(const ObjectHandle &ownerHandle,
                                                     const ObjectHandle &sourceHandle) noexcept
    {
        return RenderProxyHandle{ownerHandle, sourceHandle, 0, RenderDomain::SceneGeometry};
    }

    [[nodiscard]] static RenderProxyHandle Synthetic(RenderDomain proxyDomain, uint64_t id) noexcept
    {
        return RenderProxyHandle{{}, {}, id, proxyDomain};
    }

    [[nodiscard]] bool IsValid() const noexcept
    {
        if (domain == RenderDomain::Unknown)
            return false;
        if (domain == RenderDomain::SceneGeometry)
            return owner.IsValid() && source.IsValid() && owner.worldId == source.worldId;
        return syntheticId != 0;
    }

    [[nodiscard]] bool IsSceneBacked() const noexcept
    {
        return domain == RenderDomain::SceneGeometry;
    }

    [[nodiscard]] RenderDrawIdentity MakeDrawIdentity(uint32_t primitiveIndex = 0) const noexcept
    {
        if (!IsValid())
            return {};
        const uint64_t lifetime = IsSceneBacked() ? SceneLifetimeIdentity() : syntheticId;
        return RenderDrawIdentity{lifetime, primitiveIndex, domain};
    }

    bool operator==(const RenderProxyHandle &rhs) const noexcept
    {
        return owner == rhs.owner && source == rhs.source && syntheticId == rhs.syntheticId && domain == rhs.domain;
    }

    bool operator!=(const RenderProxyHandle &rhs) const noexcept
    {
        return !(*this == rhs);
    }

  private:
    [[nodiscard]] uint64_t SceneLifetimeIdentity() const noexcept
    {
        constexpr uint64_t offset = 14695981039346656037ull;
        constexpr uint64_t prime = 1099511628211ull;
        uint64_t hash = offset;
        const auto append = [&](uint64_t value) {
            for (uint32_t shift = 0; shift < 64; shift += 8) {
                hash ^= static_cast<uint8_t>(value >> shift);
                hash *= prime;
            }
        };
        append(owner.id);
        append(owner.generation);
        append(owner.worldId);
        append(source.id);
        append(source.generation);
        append(source.worldId);
        return hash == 0 ? 1 : hash;
    }
};

} // namespace infernux
