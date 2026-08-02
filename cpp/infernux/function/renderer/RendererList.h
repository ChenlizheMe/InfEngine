#pragma once

#include <function/renderer/InxRenderStruct.h>

#include <cstddef>
#include <utility>
#include <variant>
#include <vector>

namespace infernux
{

enum class RendererListPurpose : uint8_t
{
    Unspecified = 0,
    CameraVisible,
    ShadowCasters,
};

/// A typed draw collection with explicit owned or borrowed storage.
///
/// Borrowed lists avoid DrawCall copies for SceneRenderer frame caches. Owned
/// lists move camera-specific culling results without shared_ptr refcount churn.
/// The owner of a borrowed list must keep its source alive for the list's use.
class RendererList
{
  public:
    using Container = std::vector<DrawCall>;

    RendererList() = default;

    [[nodiscard]] static RendererList Own(Container &&drawCalls, RendererListPurpose purpose, RenderDomainMask domains)
    {
        RendererList list;
        list.m_storage = std::move(drawCalls);
        list.m_purpose = purpose;
        list.m_domains = domains;
        return list;
    }

    [[nodiscard]] static RendererList Borrow(const Container &drawCalls, RendererListPurpose purpose,
                                             RenderDomainMask domains)
    {
        RendererList list;
        list.m_storage = &drawCalls;
        list.m_purpose = purpose;
        list.m_domains = domains;
        return list;
    }

    [[nodiscard]] const Container &DrawCalls() const noexcept
    {
        if (const auto *owned = std::get_if<Container>(&m_storage))
            return *owned;
        const Container *borrowed = std::get<const Container *>(m_storage);
        return borrowed ? *borrowed : EmptyContainer();
    }

    /// Move owned storage or materialize a borrowed list into a new vector.
    [[nodiscard]] Container Consume()
    {
        Container result;
        if (auto *owned = std::get_if<Container>(&m_storage))
            result = std::move(*owned);
        else if (const Container *borrowed = std::get<const Container *>(m_storage))
            result = *borrowed;
        Clear();
        return result;
    }

    [[nodiscard]] size_t Size() const noexcept
    {
        return DrawCalls().size();
    }

    [[nodiscard]] bool Empty() const noexcept
    {
        return DrawCalls().empty();
    }

    [[nodiscard]] bool IsBorrowed() const noexcept
    {
        return std::holds_alternative<const Container *>(m_storage);
    }

    [[nodiscard]] RendererListPurpose Purpose() const noexcept
    {
        return m_purpose;
    }

    [[nodiscard]] RenderDomainMask Domains() const noexcept
    {
        return m_domains;
    }

    [[nodiscard]] bool ContainsDomain(RenderDomain domain) const noexcept
    {
        return (m_domains & RenderDomainBit(domain)) != 0;
    }

    void Clear() noexcept
    {
        m_storage = Container{};
        m_purpose = RendererListPurpose::Unspecified;
        m_domains = 0;
    }

  private:
    [[nodiscard]] static const Container &EmptyContainer() noexcept
    {
        static const Container empty;
        return empty;
    }

    std::variant<Container, const Container *> m_storage{Container{}};
    RendererListPurpose m_purpose = RendererListPurpose::Unspecified;
    RenderDomainMask m_domains = 0;
};

} // namespace infernux
