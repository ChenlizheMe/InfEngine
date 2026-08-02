#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <utility>

namespace infernux
{

struct RenderGraphScopeId
{
    uint64_t value = 0;

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return value != 0;
    }

    [[nodiscard]] constexpr uint32_t GraphInstance() const noexcept
    {
        return static_cast<uint32_t>(value >> 32);
    }

    [[nodiscard]] constexpr uint32_t Epoch() const noexcept
    {
        return static_cast<uint32_t>(value);
    }

    friend constexpr bool operator==(RenderGraphScopeId lhs, RenderGraphScopeId rhs) noexcept
    {
        return lhs.value == rhs.value;
    }

    friend constexpr bool operator!=(RenderGraphScopeId lhs, RenderGraphScopeId rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

/// Owns the identity scope used by one RenderGraph instance.
/// Moving transfers the scope so already-issued handles follow the graph data.
class RenderGraphIdentitySource
{
  public:
    RenderGraphIdentitySource() noexcept : m_graphInstance(AllocateGraphInstance())
    {
    }

    RenderGraphIdentitySource(const RenderGraphIdentitySource &) = delete;
    RenderGraphIdentitySource &operator=(const RenderGraphIdentitySource &) = delete;

    RenderGraphIdentitySource(RenderGraphIdentitySource &&other) noexcept
        : m_graphInstance(std::exchange(other.m_graphInstance, AllocateGraphInstance())),
          m_epoch(std::exchange(other.m_epoch, 1))
    {
    }

    RenderGraphIdentitySource &operator=(RenderGraphIdentitySource &&other) noexcept
    {
        if (this != &other) {
            m_graphInstance = std::exchange(other.m_graphInstance, AllocateGraphInstance());
            m_epoch = std::exchange(other.m_epoch, 1);
        }
        return *this;
    }

    [[nodiscard]] RenderGraphScopeId Current() const noexcept
    {
        return {static_cast<uint64_t>(m_graphInstance) << 32 | m_epoch};
    }

    void AdvanceEpoch() noexcept
    {
        ++m_epoch;
        if (m_epoch == 0) {
            m_graphInstance = AllocateGraphInstance();
            m_epoch = 1;
        }
    }

  private:
    static uint32_t AllocateGraphInstance() noexcept
    {
        static std::atomic<uint32_t> next{1};
        uint32_t id = next.fetch_add(1, std::memory_order_relaxed);
        while (id == 0) {
            id = next.fetch_add(1, std::memory_order_relaxed);
        }
        return id;
    }

    uint32_t m_graphInstance = 0;
    uint32_t m_epoch = 1;
};

struct GraphResourceHandle
{
    static constexpr uint32_t InvalidId = std::numeric_limits<uint32_t>::max();

    RenderGraphScopeId scope;
    uint32_t id = InvalidId;
    uint32_t version = 0;

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return scope.IsValid() && id != InvalidId;
    }

    friend constexpr bool operator==(GraphResourceHandle lhs, GraphResourceHandle rhs) noexcept
    {
        return lhs.scope == rhs.scope && lhs.id == rhs.id && lhs.version == rhs.version;
    }

    friend constexpr bool operator!=(GraphResourceHandle lhs, GraphResourceHandle rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

struct GraphPassHandle
{
    static constexpr uint32_t InvalidId = std::numeric_limits<uint32_t>::max();

    RenderGraphScopeId scope;
    uint32_t id = InvalidId;

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return scope.IsValid() && id != InvalidId;
    }

    friend constexpr bool operator==(GraphPassHandle lhs, GraphPassHandle rhs) noexcept
    {
        return lhs.scope == rhs.scope && lhs.id == rhs.id;
    }

    friend constexpr bool operator!=(GraphPassHandle lhs, GraphPassHandle rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

struct GraphResourceHandleHash
{
    [[nodiscard]] size_t operator()(GraphResourceHandle handle) const noexcept
    {
        size_t seed = std::hash<uint64_t>{}(handle.scope.value);
        const uint64_t local = static_cast<uint64_t>(handle.id) << 32 | handle.version;
        seed ^= std::hash<uint64_t>{}(local) + 0x9e3779b9U + (seed << 6) + (seed >> 2);
        return seed;
    }
};

struct GraphPassHandleHash
{
    [[nodiscard]] size_t operator()(GraphPassHandle handle) const noexcept
    {
        size_t seed = std::hash<uint64_t>{}(handle.scope.value);
        seed ^= std::hash<uint32_t>{}(handle.id) + 0x9e3779b9U + (seed << 6) + (seed >> 2);
        return seed;
    }
};

static_assert(sizeof(GraphResourceHandle) == 16);
static_assert(sizeof(GraphPassHandle) == 16);

} // namespace infernux
