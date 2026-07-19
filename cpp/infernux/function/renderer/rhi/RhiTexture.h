#pragma once

#include "RhiDevice.h"

#include <cstdint>

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

} // namespace infernux::rhi
