#pragma once

#include "RhiDevice.h"

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>

namespace infernux::rhi
{

class TextureResource final
{
  public:
    TextureResource(Device &device, TextureHandle texture, TextureViewHandle view, SamplerHandle sampler,
                    uint64_t residentBytes) noexcept
        : m_device(&device), m_texture(texture), m_view(view), m_sampler(sampler), m_residentBytes(residentBytes)
    {
    }

    ~TextureResource()
    {
        Reset();
    }

    TextureResource(const TextureResource &) = delete;
    TextureResource &operator=(const TextureResource &) = delete;
    TextureResource(TextureResource &&) = delete;
    TextureResource &operator=(TextureResource &&) = delete;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return m_device && m_texture.IsValid() && m_view.IsValid() && m_sampler.IsValid() && m_residentBytes > 0;
    }
    [[nodiscard]] TextureHandle GetTexture() const noexcept
    {
        return m_texture;
    }
    [[nodiscard]] TextureViewHandle GetView() const noexcept
    {
        return m_view;
    }
    [[nodiscard]] SamplerHandle GetSampler() const noexcept
    {
        return m_sampler;
    }
    [[nodiscard]] uint64_t GetResidentBytes() const noexcept
    {
        return m_residentBytes;
    }

  private:
    void Reset() noexcept
    {
        if (!m_device)
            return;
        m_device->Release(m_sampler);
        m_device->Release(m_view);
        m_device->Release(m_texture);
        m_device = nullptr;
        m_texture = {};
        m_view = {};
        m_sampler = {};
        m_residentBytes = 0;
    }

    Device *m_device = nullptr;
    TextureHandle m_texture;
    TextureViewHandle m_view;
    SamplerHandle m_sampler;
    uint64_t m_residentBytes = 0;
};

/// Immutable revisioned publication of one resident GPU texture. Consumers
/// retain this object rather than caching independent handles and querying the
/// asset registry again for a revision. Replacing a texture publishes a new
/// object; the previous resource remains alive until every consumer releases
/// its publication.
class TextureGpuView final
{
  public:
    TextureGpuView(std::string sourceId, uint64_t revision, std::shared_ptr<TextureResource> resource)
        : m_sourceId(std::move(sourceId)), m_revision(revision),
          m_texture(resource ? resource->GetTexture() : TextureHandle{}),
          m_view(resource ? resource->GetView() : TextureViewHandle{}),
          m_sampler(resource ? resource->GetSampler() : SamplerHandle{}),
          m_residentBytes(resource ? resource->GetResidentBytes() : 0), m_owner(std::move(resource))
    {
    }

    TextureGpuView(std::string sourceId, uint64_t revision, TextureHandle texture, TextureViewHandle view,
                   SamplerHandle sampler, uint64_t residentBytes, std::shared_ptr<const void> owner)
        : m_sourceId(std::move(sourceId)), m_revision(revision), m_texture(texture), m_view(view), m_sampler(sampler),
          m_residentBytes(residentBytes), m_owner(std::move(owner))
    {
    }

    [[nodiscard]] bool IsValid() const noexcept
    {
        return !m_sourceId.empty() && m_revision != 0 && m_view.IsValid() && m_sampler.IsValid() && m_owner;
    }
    [[nodiscard]] const std::string &GetSourceId() const noexcept
    {
        return m_sourceId;
    }
    [[nodiscard]] uint64_t GetRevision() const noexcept
    {
        return m_revision;
    }
    [[nodiscard]] TextureHandle GetTexture() const noexcept
    {
        return m_texture;
    }
    [[nodiscard]] TextureViewHandle GetView() const noexcept
    {
        return m_view;
    }
    [[nodiscard]] SamplerHandle GetSampler() const noexcept
    {
        return m_sampler;
    }
    [[nodiscard]] uint64_t GetResidentBytes() const noexcept
    {
        return m_residentBytes;
    }
    [[nodiscard]] const std::shared_ptr<const void> &GetOwner() const noexcept
    {
        return m_owner;
    }

  private:
    std::string m_sourceId;
    uint64_t m_revision = 0;
    TextureHandle m_texture;
    TextureViewHandle m_view;
    SamplerHandle m_sampler;
    uint64_t m_residentBytes = 0;
    std::shared_ptr<const void> m_owner;
};

/// Stable indirection owned by the residency cache and consumers. Asset reload
/// publishes a complete TextureGpuView atomically; readers never query an
/// unrelated version source and never observe handles from different revisions.
class TextureGpuViewSlot final
{
  public:
    explicit TextureGpuViewSlot(std::string key) : m_key(std::move(key))
    {
    }

    [[nodiscard]] const std::string &GetKey() const noexcept
    {
        return m_key;
    }

    [[nodiscard]] std::shared_ptr<const TextureGpuView> Acquire() const noexcept
    {
        return std::atomic_load_explicit(&m_current, std::memory_order_acquire);
    }

    void RequestRevision(uint64_t revision) noexcept
    {
        uint64_t current = m_requestedRevision.load(std::memory_order_relaxed);
        while (current < revision && !m_requestedRevision.compare_exchange_weak(
                                         current, revision, std::memory_order_release, std::memory_order_relaxed)) {
        }
    }

    [[nodiscard]] uint64_t GetRequestedRevision() const noexcept
    {
        return m_requestedRevision.load(std::memory_order_acquire);
    }

    [[nodiscard]] bool NeedsRefresh() const noexcept
    {
        const uint64_t requested = GetRequestedRevision();
        const auto published = Acquire();
        return requested != 0 && (!published || published->GetRevision() < requested);
    }

    [[nodiscard]] std::shared_ptr<const TextureGpuView> Publish(std::shared_ptr<const TextureGpuView> next)
    {
        if (!next || !next->IsValid())
            return {};
        RequestRevision(next->GetRevision());
        return std::atomic_exchange_explicit(&m_current, std::move(next), std::memory_order_acq_rel);
    }

  private:
    std::string m_key;
    mutable std::shared_ptr<const TextureGpuView> m_current;
    std::atomic<uint64_t> m_requestedRevision{0};
};

} // namespace infernux::rhi
