#include "VkDescriptorManager.h"

#include <algorithm>
#include <core/log/InxLog.h>

namespace infernux::vk
{

VkDescriptorManager::VkDescriptorManager(VkDevice device, rhi::DeviceId deviceId) noexcept
    : m_device(device), m_deviceId(deviceId)
{
}

VkDescriptorManager::~VkDescriptorManager()
{
    Destroy();
}

void VkDescriptorManager::Reset(VkDevice device, rhi::DeviceId deviceId) noexcept
{
    Destroy();
    std::lock_guard lock(m_mutex);
    m_device = device;
    m_deviceId = deviceId;
}

DescriptorLease VkDescriptorManager::Allocate(VkDescriptorSetLayout layout, DescriptorArena arena)
{
    std::lock_guard lock(m_mutex);
    if (m_device == VK_NULL_HANDLE || m_deviceId == rhi::InvalidDeviceId || layout == VK_NULL_HANDLE ||
        arena == DescriptorArena::Count)
        return {};

    auto &pages = m_pools[ArenaIndex(arena)];
    VkDescriptorSetAllocateInfo allocateInfo{};
    allocateInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    allocateInfo.descriptorSetCount = 1;
    allocateInfo.pSetLayouts = &layout;

    PoolPage *owner = nullptr;
    VkDescriptorSet set = VK_NULL_HANDLE;
    for (auto &page : pages) {
        allocateInfo.descriptorPool = page.pool;
        if (vkAllocateDescriptorSets(m_device, &allocateInfo, &set) == VK_SUCCESS) {
            owner = &page;
            break;
        }
    }

    if (!owner) {
        const VkDescriptorPool pool = CreatePool(arena);
        if (pool == VK_NULL_HANDLE) {
            ++m_allocationFailures;
            return {};
        }
        pages.push_back({pool, m_nextPoolGeneration++, 0});
        owner = &pages.back();
        allocateInfo.descriptorPool = pool;
        if (vkAllocateDescriptorSets(m_device, &allocateInfo, &set) != VK_SUCCESS) {
            ++m_allocationFailures;
            return {};
        }
    }

    DescriptorLease lease;
    lease.id = m_nextLeaseId++;
    lease.device = m_deviceId;
    lease.arena = arena;
    lease.poolGeneration = owner->generation;
    lease.setGeneration = m_nextSetGeneration++;
    lease.set = set;
    lease.pool = owner->pool;
    lease.layout = layout;
    ++owner->liveSets;
    m_leases.emplace(lease.id, LeaseState{lease});
    m_nativeLeaseIds.emplace(reinterpret_cast<uint64_t>(set), lease.id);
    m_peakLiveSets = (std::max)(m_peakLiveSets, m_leases.size());
    return lease;
}

DescriptorLease VkDescriptorManager::AllocateBindlessTextureSet(VkDescriptorSetLayout layout, uint32_t descriptorCount)
{
    std::lock_guard lock(m_mutex);
    if (m_device == VK_NULL_HANDLE || m_deviceId == rhi::InvalidDeviceId || layout == VK_NULL_HANDLE ||
        descriptorCount == 0)
        return {};

    auto &pages = m_pools[ArenaIndex(DescriptorArena::BindlessGlobal)];
    // The global table is deliberately one dedicated manager page. This keeps
    // its variable-count capacity independent from ordinary material/view
    // descriptor pressure while retaining normal lease retirement semantics.
    const VkDescriptorPool pool = CreatePool(DescriptorArena::BindlessGlobal, descriptorCount);
    if (pool == VK_NULL_HANDLE) {
        ++m_allocationFailures;
        return {};
    }
    pages.push_back({pool, m_nextPoolGeneration++, 0});
    auto &page = pages.back();

    VkDescriptorSetVariableDescriptorCountAllocateInfo variableCount{};
    variableCount.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_VARIABLE_DESCRIPTOR_COUNT_ALLOCATE_INFO;
    variableCount.descriptorSetCount = 1;
    variableCount.pDescriptorCounts = &descriptorCount;
    VkDescriptorSetAllocateInfo allocateInfo{};
    allocateInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    allocateInfo.pNext = &variableCount;
    allocateInfo.descriptorPool = pool;
    allocateInfo.descriptorSetCount = 1;
    allocateInfo.pSetLayouts = &layout;

    VkDescriptorSet set = VK_NULL_HANDLE;
    if (vkAllocateDescriptorSets(m_device, &allocateInfo, &set) != VK_SUCCESS) {
        ++m_allocationFailures;
        return {};
    }

    DescriptorLease lease;
    lease.id = m_nextLeaseId++;
    lease.device = m_deviceId;
    lease.arena = DescriptorArena::BindlessGlobal;
    lease.poolGeneration = page.generation;
    lease.setGeneration = m_nextSetGeneration++;
    lease.set = set;
    lease.pool = pool;
    lease.layout = layout;
    ++page.liveSets;
    m_leases.emplace(lease.id, LeaseState{lease});
    m_nativeLeaseIds.emplace(reinterpret_cast<uint64_t>(set), lease.id);
    m_peakLiveSets = (std::max)(m_peakLiveSets, m_leases.size());
    return lease;
}

VkDescriptorPool VkDescriptorManager::AcquireExternalPool(DescriptorArena arena)
{
    std::lock_guard lock(m_mutex);
    if (m_device == VK_NULL_HANDLE || arena != DescriptorArena::ImGuiExternal)
        return VK_NULL_HANDLE;
    auto &pages = m_pools[ArenaIndex(arena)];
    if (!pages.empty())
        return pages.front().pool;
    const VkDescriptorPool pool = CreatePool(arena);
    if (pool == VK_NULL_HANDLE) {
        ++m_allocationFailures;
        return VK_NULL_HANDLE;
    }
    pages.push_back({pool, m_nextPoolGeneration++, 0});
    return pool;
}

void VkDescriptorManager::MarkUsed(VkDescriptorSet set, rhi::SubmissionSerial serial) noexcept
{
    if (set == VK_NULL_HANDLE || serial == rhi::InvalidSubmissionSerial)
        return;
    std::lock_guard lock(m_mutex);
    const auto native = m_nativeLeaseIds.find(reinterpret_cast<uint64_t>(set));
    if (native == m_nativeLeaseIds.end())
        return;
    const auto found = m_leases.find(native->second);
    if (found == m_leases.end())
        return;
    found->second.lastUse = (std::max)(found->second.lastUse, serial);
    if (found->second.retired)
        found->second.retireAfter = (std::max)(found->second.retireAfter, serial);
}

void VkDescriptorManager::UseSubmissionSerials(std::function<rhi::SubmissionSerial()> retirementSerialSource)
{
    std::lock_guard lock(m_mutex);
    m_retirementSerialSource = std::move(retirementSerialSource);
}

void VkDescriptorManager::MarkUsed(const DescriptorLease &lease, rhi::SubmissionSerial serial) noexcept
{
    if (!lease.IsValid() || serial == rhi::InvalidSubmissionSerial)
        return;
    std::lock_guard lock(m_mutex);
    const auto found = m_leases.find(lease.id);
    if (found == m_leases.end() || found->second.lease.device != lease.device ||
        found->second.lease.setGeneration != lease.setGeneration)
        return;
    found->second.lastUse = (std::max)(found->second.lastUse, serial);
    if (found->second.retired)
        found->second.retireAfter = (std::max)(found->second.retireAfter, serial);
}

void VkDescriptorManager::Retire(const DescriptorLease &lease, rhi::SubmissionSerial retireAfter) noexcept
{
    if (!lease.IsValid())
        return;
    std::lock_guard lock(m_mutex);
    const auto found = m_leases.find(lease.id);
    if (found == m_leases.end() || found->second.lease.device != m_deviceId ||
        found->second.lease.setGeneration != lease.setGeneration)
        return;
    found->second.retired = true;
    const auto currentSerial = m_retirementSerialSource ? m_retirementSerialSource() : 0;
    found->second.retireAfter = (std::max)({found->second.lastUse, retireAfter, currentSerial});
    if (found->second.retireAfter == 0) {
        FreeLease(found->second);
        m_leases.erase(found);
    }
}

size_t VkDescriptorManager::Collect(rhi::SubmissionSerial completedSerial) noexcept
{
    std::lock_guard lock(m_mutex);
    size_t collected = 0;
    for (auto it = m_leases.begin(); it != m_leases.end();) {
        if (it->second.retired && it->second.retireAfter <= completedSerial) {
#if INFERNUX_VULKAN_VALIDATION_LAYERS
            // INXLOG_INFO("[DescriptorLifetime] free set=", reinterpret_cast<uint64_t>(it->second.lease.set),
            //             " lease=", it->second.lease.id, " arena=", static_cast<uint32_t>(it->second.lease.arena),
            //             " generation=", it->second.lease.setGeneration, " last_use=", it->second.lastUse,
            //             " retire_after=", it->second.retireAfter, " completed=", completedSerial);
#endif
            FreeLease(it->second);
            it = m_leases.erase(it);
            ++collected;
        } else {
            ++it;
        }
    }
    return collected;
}

void VkDescriptorManager::Destroy() noexcept
{
    std::lock_guard lock(m_mutex);
    m_leases.clear();
    m_nativeLeaseIds.clear();
    if (m_device != VK_NULL_HANDLE) {
        for (auto &arena : m_pools) {
            for (auto &page : arena) {
                if (page.pool != VK_NULL_HANDLE)
                    vkDestroyDescriptorPool(m_device, page.pool, nullptr);
            }
        }
    }
    for (auto &arena : m_pools)
        arena.clear();
    m_device = VK_NULL_HANDLE;
    m_deviceId = rhi::InvalidDeviceId;
    m_nextLeaseId = 1;
    m_nextPoolGeneration = 1;
    m_nextSetGeneration = 1;
    m_peakLiveSets = 0;
    m_allocationFailures = 0;
    m_retirementSerialSource = {};
}

bool VkDescriptorManager::Owns(const DescriptorLease &lease) const noexcept
{
    std::lock_guard lock(m_mutex);
    const auto found = m_leases.find(lease.id);
    return found != m_leases.end() && found->second.lease.device == m_deviceId &&
           found->second.lease.setGeneration == lease.setGeneration;
}

VkDescriptorManager::Stats VkDescriptorManager::GetStats() const noexcept
{
    std::lock_guard lock(m_mutex);
    Stats stats;
    for (const auto &arena : m_pools)
        stats.poolCount += arena.size();
    for (const auto &[id, lease] : m_leases) {
        (void)id;
        lease.retired ? ++stats.retiredSets : ++stats.liveSets;
    }
    stats.peakLiveSets = m_peakLiveSets;
    stats.allocationFailures = m_allocationFailures;
    return stats;
}

VkDescriptorPool VkDescriptorManager::CreatePool(DescriptorArena arena, uint32_t bindlessDescriptorCount) const
{
    if (arena == DescriptorArena::ImGuiExternal) {
        const std::array<VkDescriptorPoolSize, 11> sizes = {
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_SAMPLER, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_UNIFORM_TEXEL_BUFFER, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER_DYNAMIC, 1000},
            VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT, 1000},
        };
        VkDescriptorPoolCreateInfo createInfo{};
        createInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
        createInfo.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
        createInfo.maxSets = 1000u * static_cast<uint32_t>(sizes.size());
        createInfo.poolSizeCount = static_cast<uint32_t>(sizes.size());
        createInfo.pPoolSizes = sizes.data();
        VkDescriptorPool pool = VK_NULL_HANDLE;
        return vkCreateDescriptorPool(m_device, &createInfo, nullptr, &pool) == VK_SUCCESS ? pool : VK_NULL_HANDLE;
    }

    if (arena == DescriptorArena::BindlessGlobal) {
        if (bindlessDescriptorCount == 0)
            return VK_NULL_HANDLE;
        const VkDescriptorPoolSize size{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, bindlessDescriptorCount};
        VkDescriptorPoolCreateInfo createInfo{};
        createInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
        createInfo.flags =
            VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT | VK_DESCRIPTOR_POOL_CREATE_UPDATE_AFTER_BIND_BIT;
        createInfo.maxSets = 1;
        createInfo.poolSizeCount = 1;
        createInfo.pPoolSizes = &size;
        VkDescriptorPool pool = VK_NULL_HANDLE;
        return vkCreateDescriptorPool(m_device, &createInfo, nullptr, &pool) == VK_SUCCESS ? pool : VK_NULL_HANDLE;
    }

    const std::array<VkDescriptorPoolSize, 6> sizes = {
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 4096},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1024},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE, 1024},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 1024},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_SAMPLER, 1024},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 4096},
    };
    VkDescriptorPoolCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    createInfo.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    if (arena == DescriptorArena::UpdateAfterBind)
        createInfo.flags |= VK_DESCRIPTOR_POOL_CREATE_UPDATE_AFTER_BIND_BIT;
    createInfo.maxSets = arena == DescriptorArena::FrameTransient ? 2048u : 512u;
    createInfo.poolSizeCount = static_cast<uint32_t>(sizes.size());
    createInfo.pPoolSizes = sizes.data();
    VkDescriptorPool pool = VK_NULL_HANDLE;
    return vkCreateDescriptorPool(m_device, &createInfo, nullptr, &pool) == VK_SUCCESS ? pool : VK_NULL_HANDLE;
}

void VkDescriptorManager::FreeLease(LeaseState &state) noexcept
{
    if (m_device == VK_NULL_HANDLE || state.lease.set == VK_NULL_HANDLE || state.lease.pool == VK_NULL_HANDLE)
        return;
    m_nativeLeaseIds.erase(reinterpret_cast<uint64_t>(state.lease.set));
    (void)vkFreeDescriptorSets(m_device, state.lease.pool, 1, &state.lease.set);
    auto &pages = m_pools[ArenaIndex(state.lease.arena)];
    const auto page = std::find_if(pages.begin(), pages.end(), [&](const PoolPage &candidate) {
        return candidate.pool == state.lease.pool && candidate.generation == state.lease.poolGeneration;
    });
    if (page != pages.end() && page->liveSets > 0)
        --page->liveSets;
    state.lease.set = VK_NULL_HANDLE;
}

} // namespace infernux::vk
