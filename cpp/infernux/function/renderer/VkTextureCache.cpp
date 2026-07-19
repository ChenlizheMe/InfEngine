/**
 * @file VkTextureCache.cpp
 * @brief Implementation of VkTextureCache — simple GPU texture CRUD.
 */

#include "VkTextureCache.h"
#include "InxError.h"
#include "TextureUploadBuilder.h"
#include "vk/VkResourceManager.h"

#include <function/resources/InxTexture/TextureDecoder.h>

#include <limits>
#include <stdexcept>

namespace infernux
{

// ============================================================================
// Simple Loaders
// ============================================================================

void VkTextureCache::CreateDefaultWhiteTexture(const std::string &name, vk::VkResourceManager &rm)
{
    CreateSolidColorTexture(name, 255, 255, 255, 255, rm);
    INXLOG_INFO("VkTextureCache: created default white texture: ", name);
}

void VkTextureCache::CreateSolidColorTexture(const std::string &name, uint8_t r, uint8_t g, uint8_t b, uint8_t a,
                                             vk::VkResourceManager &rm)
{
    const uint8_t pixel[] = {r, g, b, a};
    const auto cpuData = TextureDecoder::CreateRgba8(pixel, sizeof(pixel), 1, 1, false);
    rhi::SamplerDesc sampler;
    TextureUploadBatch upload(*cpuData, sampler);
    auto ticket = rm.BeginTextureUpload(upload.GetRequest());
    if (!rm.TryPublishTextureUpload(ticket)) {
        rm.DrainBufferUploads();
        if (!rm.TryPublishTextureUpload(ticket))
            throw std::runtime_error("bootstrap RHI texture upload did not complete");
    }
    (void)Insert(name, ticket->GetTexture(), 0, true, {}, 0);
}

// ============================================================================
// Cache Operations
// ============================================================================

std::shared_ptr<rhi::TextureResource> VkTextureCache::Insert(const std::string &key,
                                                             std::shared_ptr<rhi::TextureResource> texture,
                                                             uint64_t lastUsedFrame, bool permanentlyPinned,
                                                             std::string assetGuid, uint64_t runtimeVersion)
{
    if (key.empty() || !texture || !texture->IsValid() || texture->GetResidentBytes() == 0)
        throw std::invalid_argument("VkTextureCache requires a valid keyed texture with resident bytes");
    if (assetGuid.empty() != (runtimeVersion == 0))
        throw std::invalid_argument("GPU texture asset identity requires GUID and runtime version together");
    auto sharedTexture = std::move(texture);
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        SweepRetiredLeasesLocked();
        auto existing = m_textures.find(key);
        if (existing != m_textures.end()) {
            Entry retired = std::move(existing->second);
            m_textures.erase(existing);
            RetireEntryLocked(std::move(retired));
        }
        const uint64_t residentBytes = sharedTexture->GetResidentBytes();
        if (residentBytes > std::numeric_limits<uint64_t>::max() - m_residentBytes)
            throw std::overflow_error("GPU texture residency byte counter overflow");
        m_textures.emplace(key, Entry{sharedTexture, residentBytes, lastUsedFrame, permanentlyPinned,
                                      std::move(assetGuid), runtimeVersion});
        m_residentBytes += residentBytes;
    }
    (void)TrimToBudget();
    return sharedTexture;
}

std::shared_ptr<rhi::TextureResource> VkTextureCache::FindAsset(const std::string &key, const std::string &assetGuid,
                                                                uint64_t runtimeVersion, uint64_t frame)
{
    if (assetGuid.empty() || runtimeVersion == 0)
        throw std::invalid_argument("GPU texture lookup requires a published asset identity");
    std::lock_guard<std::mutex> lock(m_mutex);
    auto entry = m_textures.find(key);
    if (entry == m_textures.end())
        return {};
    if (entry->second.assetGuid != assetGuid || entry->second.runtimeVersion != runtimeVersion) {
        Entry retired = std::move(entry->second);
        m_textures.erase(entry);
        RetireEntryLocked(std::move(retired));
        ++m_evictionCount;
        return {};
    }
    entry->second.lastUsedFrame = frame;
    return entry->second.texture;
}

std::shared_ptr<rhi::TextureResource> VkTextureCache::Find(const std::string &key, uint64_t frame)
{
    std::lock_guard<std::mutex> lock(m_mutex);
    auto it = m_textures.find(key);
    if (it == m_textures.end() || !it->second.texture)
        return {};
    it->second.lastUsedFrame = frame;
    return it->second.texture;
}

size_t VkTextureCache::EvictByPrefix(const std::string &prefix)
{
    std::lock_guard<std::mutex> lock(m_mutex);
    SweepRetiredLeasesLocked();
    std::vector<std::string> keysToRemove;
    for (const auto &[key, entry] : m_textures) {
        (void)entry;
        if (key == prefix || key.rfind(prefix + "::", 0) == 0) {
            keysToRemove.push_back(key);
        }
    }
    for (const auto &key : keysToRemove) {
        INXLOG_DEBUG("VkTextureCache: evicting: ", key);
        const auto found = m_textures.find(key);
        Entry retired = std::move(found->second);
        m_textures.erase(found);
        RetireEntryLocked(std::move(retired));
        ++m_evictionCount;
    }
    return keysToRemove.size();
}

void VkTextureCache::Clear()
{
    std::lock_guard<std::mutex> lock(m_mutex);
    for (auto &[key, entry] : m_textures) {
        (void)key;
        RetireEntryLocked(std::move(entry));
    }
    m_textures.clear();
    SweepRetiredLeasesLocked();
}

void VkTextureCache::SetBudgetBytes(uint64_t bytes)
{
    if (bytes == 0)
        throw std::invalid_argument("GPU texture budget must be greater than zero");
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_budgetBytes = bytes;
    }
    (void)TrimToBudget();
}

size_t VkTextureCache::TrimToBudget()
{
    std::lock_guard<std::mutex> lock(m_mutex);
    SweepRetiredLeasesLocked();
    size_t evicted = 0;
    while (m_residentBytes > m_budgetBytes) {
        auto candidate = m_textures.end();
        for (auto entry = m_textures.begin(); entry != m_textures.end(); ++entry) {
            if (entry->second.permanentlyPinned || !entry->second.texture || entry->second.texture.use_count() != 1)
                continue;
            if (candidate == m_textures.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
                candidate = entry;
        }
        if (candidate == m_textures.end())
            break;
        m_residentBytes -= candidate->second.residentBytes;
        m_textures.erase(candidate);
        ++evicted;
        ++m_evictionCount;
    }
    return evicted;
}

uint64_t VkTextureCache::GetBudgetBytes() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_budgetBytes;
}

uint64_t VkTextureCache::GetResidentBytes() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    SweepRetiredLeasesLocked();
    return m_residentBytes;
}

size_t VkTextureCache::GetEntryCount() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_textures.size();
}

size_t VkTextureCache::GetRetiredLeaseCount() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    SweepRetiredLeasesLocked();
    return m_retiredLeases.size();
}

uint64_t VkTextureCache::GetEvictionCount() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_evictionCount;
}

uint64_t VkTextureCache::GetRetiredLeaseBytes() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    SweepRetiredLeasesLocked();
    uint64_t bytes = 0;
    for (const auto &lease : m_retiredLeases)
        bytes += lease.residentBytes;
    return bytes;
}

GpuEvictionCandidate VkTextureCache::PeekOldestEvictable() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    auto candidate = m_textures.end();
    for (auto entry = m_textures.begin(); entry != m_textures.end(); ++entry) {
        if (entry->second.permanentlyPinned || !entry->second.texture || entry->second.texture.use_count() != 1)
            continue;
        if (candidate == m_textures.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
            candidate = entry;
    }
    if (candidate == m_textures.end())
        return {};
    return {candidate->second.lastUsedFrame, candidate->second.residentBytes, true};
}

uint64_t VkTextureCache::EvictOldest()
{
    std::lock_guard<std::mutex> lock(m_mutex);
    auto candidate = m_textures.end();
    for (auto entry = m_textures.begin(); entry != m_textures.end(); ++entry) {
        if (entry->second.permanentlyPinned || !entry->second.texture || entry->second.texture.use_count() != 1)
            continue;
        if (candidate == m_textures.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
            candidate = entry;
    }
    if (candidate == m_textures.end())
        return 0;
    const uint64_t bytes = candidate->second.residentBytes;
    if (bytes > m_residentBytes)
        throw std::logic_error("GPU texture residency byte counter underflow");
    m_residentBytes -= bytes;
    m_textures.erase(candidate);
    ++m_evictionCount;
    return bytes;
}

std::vector<GpuAssetResidencyRecord> VkTextureCache::GetAssetResidency() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    std::vector<GpuAssetResidencyRecord> records;
    records.reserve(m_textures.size());
    for (const auto &[key, entry] : m_textures) {
        (void)key;
        if (entry.assetGuid.empty())
            continue;
        records.push_back({entry.assetGuid, entry.runtimeVersion, GpuAssetDomain::Texture, entry.residentBytes,
                           entry.lastUsedFrame, false, entry.permanentlyPinned || entry.texture.use_count() != 1});
    }
    return records;
}

void VkTextureCache::RetireEntryLocked(Entry entry)
{
    if (!entry.texture)
        throw std::logic_error("VkTextureCache cannot retire an empty entry");
    if (entry.residentBytes > m_residentBytes)
        throw std::logic_error("GPU texture residency byte counter underflow");
    if (entry.texture.use_count() == 1) {
        m_residentBytes -= entry.residentBytes;
        return;
    }
    m_retiredLeases.push_back({entry.texture, entry.residentBytes});
}

void VkTextureCache::SweepRetiredLeasesLocked() const
{
    size_t writeIndex = 0;
    for (size_t index = 0; index < m_retiredLeases.size(); ++index) {
        if (m_retiredLeases[index].texture.expired()) {
            if (m_retiredLeases[index].residentBytes > m_residentBytes)
                throw std::logic_error("GPU texture residency byte counter underflow");
            m_residentBytes -= m_retiredLeases[index].residentBytes;
            continue;
        }
        if (writeIndex != index)
            m_retiredLeases[writeIndex] = std::move(m_retiredLeases[index]);
        ++writeIndex;
    }
    m_retiredLeases.resize(writeIndex);
}

} // namespace infernux
