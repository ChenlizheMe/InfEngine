#pragma once

#include "RhiDevice.h"
#include "RhiResourceIndex.h"

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>

namespace infernux::rhi
{

class TextureResource final
{
  public:
    TextureResource(Device &device, TextureHandle texture, TextureViewHandle view, SamplerHandle sampler,
                    uint64_t residentBytes, PixelFormat format = PixelFormat::Undefined,
                    TextureViewDesc viewDesc = {}) noexcept
        : m_device(&device), m_lifetime(device.GetLifetime()), m_texture(texture), m_view(view), m_sampler(sampler),
          m_format(format), m_viewDesc(viewDesc), m_residentBytes(residentBytes)
    {
        m_viewDesc.texture = texture;
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
        return m_device && (!m_lifetime || m_lifetime->alive.load(std::memory_order_acquire)) && m_texture.IsValid() &&
               m_view.IsValid() && m_sampler.IsValid() && m_residentBytes > 0;
    }
    [[nodiscard]] TextureHandle GetTexture() const noexcept
    {
        return m_texture;
    }
    [[nodiscard]] TextureViewHandle GetView() const noexcept
    {
        return m_view;
    }
    [[nodiscard]] PixelFormat GetFormat() const noexcept
    {
        return m_format;
    }
    [[nodiscard]] SamplerHandle GetSampler() const noexcept
    {
        return m_sampler;
    }
    [[nodiscard]] const TextureViewDesc &GetViewDesc() const noexcept
    {
        return m_viewDesc;
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
        if (m_lifetime) {
            std::shared_lock lock(m_lifetime->gate);
            if (m_lifetime->alive.load(std::memory_order_acquire)) {
                m_device->Release(m_sampler);
                m_device->Release(m_view);
                m_device->Release(m_texture);
            }
        } else {
            m_device->Release(m_sampler);
            m_device->Release(m_view);
            m_device->Release(m_texture);
        }
        m_device = nullptr;
        m_lifetime.reset();
        m_texture = {};
        m_view = {};
        m_format = PixelFormat::Undefined;
        m_viewDesc = {};
        m_sampler = {};
        m_residentBytes = 0;
    }

    Device *m_device = nullptr;
    std::shared_ptr<DeviceLifetime> m_lifetime;
    TextureHandle m_texture;
    TextureViewHandle m_view;
    SamplerHandle m_sampler;
    PixelFormat m_format = PixelFormat::Undefined;
    TextureViewDesc m_viewDesc{};
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
          m_format(resource ? resource->GetFormat() : PixelFormat::Undefined),
          m_viewDesc(resource ? resource->GetViewDesc() : TextureViewDesc{}),
          m_residentBytes(resource ? resource->GetResidentBytes() : 0), m_owner(std::move(resource))
    {
    }

    TextureGpuView(std::string sourceId, uint64_t revision, TextureHandle texture, TextureViewHandle view,
                   SamplerHandle sampler, uint64_t residentBytes, std::shared_ptr<const void> owner,
                   PixelFormat format = PixelFormat::Undefined, TextureViewDesc viewDesc = {})
        : m_sourceId(std::move(sourceId)), m_revision(revision), m_texture(texture), m_view(view), m_sampler(sampler),
          m_format(format), m_viewDesc(viewDesc), m_residentBytes(residentBytes), m_owner(std::move(owner))
    {
        m_viewDesc.texture = texture;
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
    [[nodiscard]] PixelFormat GetFormat() const noexcept
    {
        return m_format;
    }
    [[nodiscard]] SamplerHandle GetSampler() const noexcept
    {
        return m_sampler;
    }
    [[nodiscard]] const TextureViewDesc &GetViewDesc() const noexcept
    {
        return m_viewDesc;
    }
    [[nodiscard]] uint64_t GetResidentBytes() const noexcept
    {
        return m_residentBytes;
    }
    [[nodiscard]] const std::shared_ptr<const void> &GetOwner() const noexcept
    {
        return m_owner;
    }

    /// Auxiliary device-global residency metadata for this immutable GPU
    /// publication. The texture handles and revision remain immutable; the
    /// backend may assign this slot after the view is published.
    [[nodiscard]] ResourceIndex GetBindlessResourceIndex(uint64_t tableEpoch) const noexcept
    {
        if (tableEpoch == 0)
            return {};
        std::lock_guard lock(m_bindlessMutex);
        return m_bindlessTableEpoch == tableEpoch ? m_bindlessIndex : ResourceIndex{};
    }

    /// Publish the current table slot. Repeating the same assignment is
    /// harmless. A different slot is allowed when a new device/table is being
    /// brought up; the immutable handles still belong to this view, while the
    /// index is only device-local residency metadata.
    bool SetBindlessResourceIndex(uint64_t tableEpoch, ResourceIndex resource) const noexcept
    {
        if (tableEpoch == 0 || !resource.IsValid())
            return false;
        std::lock_guard lock(m_bindlessMutex);
        m_bindlessTableEpoch = tableEpoch;
        m_bindlessIndex = resource;
        return true;
    }

  private:
    std::string m_sourceId;
    uint64_t m_revision = 0;
    TextureHandle m_texture;
    TextureViewHandle m_view;
    SamplerHandle m_sampler;
    PixelFormat m_format = PixelFormat::Undefined;
    TextureViewDesc m_viewDesc{};
    uint64_t m_residentBytes = 0;
    std::shared_ptr<const void> m_owner;
    mutable std::mutex m_bindlessMutex;
    mutable uint64_t m_bindlessTableEpoch = 0;
    mutable ResourceIndex m_bindlessIndex{};
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

    [[nodiscard]] bool TryPublish(std::shared_ptr<const TextureGpuView> next,
                                  std::shared_ptr<const TextureGpuView> *previous = nullptr)
    {
        if (!next || !next->IsValid())
            return false;
        RequestRevision(next->GetRevision());
        auto current = Acquire();
        for (;;) {
            if (current && next->GetRevision() < current->GetRevision())
                return false;
            if (std::atomic_compare_exchange_weak_explicit(&m_current, &current, next, std::memory_order_acq_rel,
                                                           std::memory_order_acquire)) {
                if (previous)
                    *previous = std::move(current);
                return true;
            }
        }
    }

  private:
    std::string m_key;
    mutable std::shared_ptr<const TextureGpuView> m_current;
    std::atomic<uint64_t> m_requestedRevision{0};
};

} // namespace infernux::rhi
