#pragma once

#include <function/scene/ObjectHandle.h>

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
        const uint64_t lifetime = IsSceneBacked() ? source.generation : syntheticId;
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
};

} // namespace infernux
