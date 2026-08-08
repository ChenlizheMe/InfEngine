#pragma once

#include "RhiDevice.h"

#include <cstdint>
#include <memory>

namespace infernux::rhi
{

/// Owns an RHI buffer registration and, optionally, the backend allocation
/// that registration refers to.
class BufferResource final
{
  public:
    BufferResource(Device &device, BufferHandle buffer, uint64_t byteSize,
                   std::shared_ptr<void> backendAllocation = {}) noexcept
        : m_device(&device), m_lifetime(device.GetLifetime()), m_buffer(buffer), m_byteSize(byteSize),
          m_backendAllocation(std::move(backendAllocation))
    {
    }

    ~BufferResource()
    {
        Reset();
    }

    BufferResource(const BufferResource &) = delete;
    BufferResource &operator=(const BufferResource &) = delete;
    BufferResource(BufferResource &&) = delete;
    BufferResource &operator=(BufferResource &&) = delete;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return m_device && (!m_lifetime || m_lifetime->alive.load(std::memory_order_acquire)) && m_buffer.IsValid() &&
               m_byteSize > 0;
    }
    [[nodiscard]] BufferHandle GetBuffer() const noexcept
    {
        return m_buffer;
    }
    [[nodiscard]] uint64_t GetByteSize() const noexcept
    {
        return m_byteSize;
    }

  private:
    void Reset() noexcept
    {
        if (!m_device)
            return;
        if (m_lifetime) {
            std::shared_lock lock(m_lifetime->gate);
            if (m_lifetime->alive.load(std::memory_order_acquire))
                m_device->Release(m_buffer);
        } else {
            m_device->Release(m_buffer);
        }
        m_device = nullptr;
        m_lifetime.reset();
        m_buffer = {};
        m_byteSize = 0;
        m_backendAllocation.reset();
    }

    Device *m_device = nullptr;
    std::shared_ptr<DeviceLifetime> m_lifetime;
    BufferHandle m_buffer;
    uint64_t m_byteSize = 0;
    std::shared_ptr<void> m_backendAllocation;
};

} // namespace infernux::rhi
