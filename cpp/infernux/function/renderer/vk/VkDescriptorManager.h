#pragma once

#include <function/renderer/rhi/RhiSubmission.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <mutex>
#include <unordered_map>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux::vk
{

enum class DescriptorArena : uint8_t
{
    Persistent,
    UpdateAfterBind,
    FrameTransient,
    ViewPersistent,
    ImGuiExternal,
    Count,
};

/// Stable ownership record for a descriptor set. Vulkan handles are never
/// treated as identity: pool/set generations and DeviceId survive handle reuse.
struct DescriptorLease
{
    uint64_t id = 0;
    rhi::DeviceId device = rhi::InvalidDeviceId;
    DescriptorArena arena = DescriptorArena::Persistent;
    uint32_t poolGeneration = 0;
    uint32_t setGeneration = 0;
    VkDescriptorSet set = VK_NULL_HANDLE;
    VkDescriptorPool pool = VK_NULL_HANDLE;
    VkDescriptorSetLayout layout = VK_NULL_HANDLE;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return id != 0 && device != rhi::InvalidDeviceId && set != VK_NULL_HANDLE && pool != VK_NULL_HANDLE &&
               layout != VK_NULL_HANDLE;
    }
};

class VkDescriptorManager
{
  public:
    struct Stats
    {
        size_t poolCount = 0;
        size_t liveSets = 0;
        size_t retiredSets = 0;
        size_t peakLiveSets = 0;
        uint64_t allocationFailures = 0;
    };

    VkDescriptorManager() = default;
    VkDescriptorManager(VkDevice device, rhi::DeviceId deviceId) noexcept;
    ~VkDescriptorManager();

    VkDescriptorManager(const VkDescriptorManager &) = delete;
    VkDescriptorManager &operator=(const VkDescriptorManager &) = delete;

    void Reset(VkDevice device = VK_NULL_HANDLE, rhi::DeviceId deviceId = rhi::InvalidDeviceId) noexcept;
    [[nodiscard]] DescriptorLease Allocate(VkDescriptorSetLayout layout,
                                           DescriptorArena arena = DescriptorArena::Persistent);
    /// Returns a manager-owned pool for a third-party allocator such as ImGui.
    /// Sets allocated from this pool remain externally owned and are not leases.
    [[nodiscard]] VkDescriptorPool AcquireExternalPool(DescriptorArena arena);
    void MarkUsed(const DescriptorLease &lease, rhi::SubmissionSerial serial) noexcept;
    void MarkUsed(VkDescriptorSet set, rhi::SubmissionSerial serial) noexcept;
    void UseSubmissionSerials(std::function<rhi::SubmissionSerial()> retirementSerialSource);
    void Retire(const DescriptorLease &lease, rhi::SubmissionSerial retireAfter = 0) noexcept;
    size_t Collect(rhi::SubmissionSerial completedSerial) noexcept;
    void Destroy() noexcept;

    [[nodiscard]] bool Owns(const DescriptorLease &lease) const noexcept;
    [[nodiscard]] Stats GetStats() const noexcept;

  private:
    struct PoolPage
    {
        VkDescriptorPool pool = VK_NULL_HANDLE;
        uint32_t generation = 0;
        size_t liveSets = 0;
    };

    struct LeaseState
    {
        DescriptorLease lease;
        rhi::SubmissionSerial lastUse = 0;
        rhi::SubmissionSerial retireAfter = 0;
        bool retired = false;
    };

    [[nodiscard]] VkDescriptorPool CreatePool(DescriptorArena arena) const;
    void FreeLease(LeaseState &state) noexcept;
    [[nodiscard]] static constexpr size_t ArenaIndex(DescriptorArena arena) noexcept
    {
        return static_cast<size_t>(arena);
    }

    mutable std::mutex m_mutex;
    VkDevice m_device = VK_NULL_HANDLE;
    rhi::DeviceId m_deviceId = rhi::InvalidDeviceId;
    uint64_t m_nextLeaseId = 1;
    uint32_t m_nextPoolGeneration = 1;
    uint32_t m_nextSetGeneration = 1;
    std::array<std::vector<PoolPage>, static_cast<size_t>(DescriptorArena::Count)> m_pools;
    std::unordered_map<uint64_t, LeaseState> m_leases;
    std::unordered_map<uint64_t, uint64_t> m_nativeLeaseIds;
    size_t m_peakLiveSets = 0;
    uint64_t m_allocationFailures = 0;
    std::function<rhi::SubmissionSerial()> m_retirementSerialSource;
};

} // namespace infernux::vk
