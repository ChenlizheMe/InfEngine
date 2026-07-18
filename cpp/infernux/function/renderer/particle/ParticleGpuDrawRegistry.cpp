#include "ParticleGpuDrawRegistry.h"

#include <algorithm>

namespace infernux::particle
{

bool ParticleGpuDrawRegistry::Set(GpuParticleDrawEntry entry)
{
    if (entry.id == 0 || entry.capacity == 0 || !entry.instances.IsValid() || !entry.indirectArguments.IsValid() ||
        !entry.renderer || !entry.renderer->IsValid() || entry.renderer->InstanceBuffer() != entry.instances)
        return false;

    std::lock_guard<std::mutex> lock(m_mutex);
    const auto existing = std::find_if(m_entries.begin(), m_entries.end(),
                                       [&](const auto &candidate) { return candidate.id == entry.id; });
    if (existing == m_entries.end())
        m_entries.push_back(std::move(entry));
    else
        *existing = std::move(entry);
    ++m_revision;
    return true;
}

bool ParticleGpuDrawRegistry::Remove(uint64_t id)
{
    if (id == 0)
        return false;
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto previousSize = m_entries.size();
    m_entries.erase(
        std::remove_if(m_entries.begin(), m_entries.end(), [&](const auto &entry) { return entry.id == id; }),
        m_entries.end());
    if (m_entries.size() == previousSize)
        return false;
    ++m_revision;
    return true;
}

void ParticleGpuDrawRegistry::Clear()
{
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_entries.empty())
        return;
    m_entries.clear();
    ++m_revision;
}

std::vector<GpuParticleDrawEntry> ParticleGpuDrawRegistry::Snapshot(int32_t queueMin, int32_t queueMax) const
{
    if (queueMin > queueMax)
        return {};
    std::lock_guard<std::mutex> lock(m_mutex);
    std::vector<GpuParticleDrawEntry> result;
    result.reserve(m_entries.size());
    for (const auto &entry : m_entries) {
        const int32_t queue = entry.renderer->RenderQueue();
        if (queue >= queueMin && queue <= queueMax)
            result.push_back(entry);
    }
    std::sort(result.begin(), result.end(), [](const auto &left, const auto &right) {
        const int32_t leftQueue = left.renderer->RenderQueue();
        const int32_t rightQueue = right.renderer->RenderQueue();
        return leftQueue != rightQueue ? leftQueue < rightQueue : left.id < right.id;
    });
    return result;
}

uint64_t ParticleGpuDrawRegistry::Revision() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_revision;
}

size_t ParticleGpuDrawRegistry::Size() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_entries.size();
}

} // namespace infernux::particle
