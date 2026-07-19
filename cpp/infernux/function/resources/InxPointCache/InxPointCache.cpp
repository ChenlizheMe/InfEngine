#include "InxPointCache.h"

namespace infernux
{

size_t InxPointCache::GetRuntimeMemoryBytes() const noexcept
{
    size_t bytes = sizeof(*this) + m_guid.capacity() + m_filePath.capacity() + m_name.capacity();
    if (!m_cpuData)
        return bytes;
    bytes += sizeof(PointCacheCpuData) + m_cpuData->stableId.capacity() + m_cpuData->name.capacity() +
             m_cpuData->bakeBasis.capacity() + m_cpuData->channels.capacity() * sizeof(PointCacheChannel) +
             m_cpuData->bytes.capacity() + m_cpuData->idLookup.capacity() * sizeof(PointCacheIdLookupEntry);
    for (const auto &channel : m_cpuData->channels)
        bytes += channel.name.capacity();
    return bytes;
}

} // namespace infernux
