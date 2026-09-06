#include "ParticleGpuDrawRegistry.h"

#include "ParticleGpuCuller.h"
#include "ParticleGpuSorter.h"

#include <algorithm>
#include <atomic>
#include <memory>

namespace infernux::particle
{

namespace
{

bool IsValidEntry(const GpuParticleDrawEntry &entry)
{
    return entry.id != 0 && entry.emitterId != 0 && entry.graphInstanceId != 0 && entry.capacity != 0 &&
           entry.instances.IsValid() && entry.visibility.IsValid() && entry.renderIndices.IsValid() &&
           entry.indirectArguments.IsValid() && entry.bounds.IsValid() && entry.simulationControl.IsValid() &&
           entry.renderer && entry.renderer->IsValid() && entry.renderer->VertexCount() > 0 &&
           entry.renderer->InstanceBuffer() == entry.instances &&
           entry.renderer->RenderIndexBuffer() == entry.renderIndices &&
           (!entry.cullProgram || entry.cullProgram->IsValid()) &&
           (!entry.sortProgram || (entry.sortProgram->IsValid() && entry.cullProgram));
}

const ParticleGpuDrawRegistry::SnapshotHandle &EmptySnapshot()
{
    static const ParticleGpuDrawRegistry::SnapshotHandle empty =
        std::make_shared<const ParticleGpuDrawRegistry::SnapshotEntries>();
    return empty;
}

} // namespace

ParticleGpuDrawRegistry::ParticleGpuDrawRegistry()
{
    auto initialState = std::make_shared<RegistryState>();
    std::atomic_store_explicit(&m_state, std::shared_ptr<const RegistryState>(std::move(initialState)),
                               std::memory_order_release);
    std::atomic_store_explicit(&m_snapshotCache, std::shared_ptr<const SnapshotCache>{}, std::memory_order_release);
}

bool ParticleGpuDrawRegistry::Set(GpuParticleDrawEntry entry)
{
    if (!IsValidEntry(entry))
        return false;

    std::lock_guard<std::mutex> lock(m_mutex);
    const auto currentState = std::atomic_load_explicit(&m_state, std::memory_order_acquire);
    auto nextState = std::make_shared<RegistryState>(*currentState);
    const auto existing = std::find_if(nextState->entries.begin(), nextState->entries.end(),
                                       [&](const auto &candidate) { return candidate.id == entry.id; });
    if (existing == nextState->entries.end())
        nextState->entries.push_back(std::move(entry));
    else
        *existing = std::move(entry);
    ++nextState->revision;
    std::atomic_store_explicit(&m_state, std::shared_ptr<const RegistryState>(std::move(nextState)),
                               std::memory_order_release);
    std::atomic_store_explicit(&m_snapshotCache, std::shared_ptr<const SnapshotCache>{}, std::memory_order_release);
    return true;
}

bool ParticleGpuDrawRegistry::Replace(std::vector<GpuParticleDrawEntry> entries)
{
    if (!std::all_of(entries.begin(), entries.end(), IsValidEntry))
        return false;
    std::sort(entries.begin(), entries.end(), [](const auto &left, const auto &right) { return left.id < right.id; });
    if (std::adjacent_find(entries.begin(), entries.end(),
                           [](const auto &left, const auto &right) { return left.id == right.id; }) != entries.end())
        return false;

    std::lock_guard<std::mutex> lock(m_mutex);
    const auto currentState = std::atomic_load_explicit(&m_state, std::memory_order_acquire);
    auto nextState = std::make_shared<RegistryState>();
    nextState->revision = currentState->revision + 1;
    nextState->entries = std::move(entries);
    std::atomic_store_explicit(&m_state, std::shared_ptr<const RegistryState>(std::move(nextState)),
                               std::memory_order_release);
    std::atomic_store_explicit(&m_snapshotCache, std::shared_ptr<const SnapshotCache>{}, std::memory_order_release);
    return true;
}

bool ParticleGpuDrawRegistry::Remove(uint64_t id)
{
    if (id == 0)
        return false;
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto currentState = std::atomic_load_explicit(&m_state, std::memory_order_acquire);
    auto nextState = std::make_shared<RegistryState>(*currentState);
    const auto previousSize = nextState->entries.size();
    nextState->entries.erase(std::remove_if(nextState->entries.begin(), nextState->entries.end(),
                                            [&](const auto &entry) { return entry.id == id; }),
                             nextState->entries.end());
    if (nextState->entries.size() == previousSize)
        return false;
    ++nextState->revision;
    std::atomic_store_explicit(&m_state, std::shared_ptr<const RegistryState>(std::move(nextState)),
                               std::memory_order_release);
    std::atomic_store_explicit(&m_snapshotCache, std::shared_ptr<const SnapshotCache>{}, std::memory_order_release);
    return true;
}

void ParticleGpuDrawRegistry::Clear()
{
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto currentState = std::atomic_load_explicit(&m_state, std::memory_order_acquire);
    if (currentState->entries.empty())
        return;
    auto nextState = std::make_shared<RegistryState>();
    nextState->revision = currentState->revision + 1;
    std::atomic_store_explicit(&m_state, std::shared_ptr<const RegistryState>(std::move(nextState)),
                               std::memory_order_release);
    std::atomic_store_explicit(&m_snapshotCache, std::shared_ptr<const SnapshotCache>{}, std::memory_order_release);
}

ParticleGpuDrawRegistry::SnapshotHandle ParticleGpuDrawRegistry::SnapshotShared(int32_t queueMin,
                                                                                int32_t queueMax) const
{
    if (queueMin > queueMax)
        return EmptySnapshot();

    const auto state = std::atomic_load_explicit(&m_state, std::memory_order_acquire);
    if (!state || state->entries.empty())
        return EmptySnapshot();

    const SnapshotKey key{queueMin, queueMax};
    const auto cache = std::atomic_load_explicit(&m_snapshotCache, std::memory_order_acquire);
    if (cache) {
        const auto cached = cache->entries.find(key);
        if (cached != cache->entries.end() && cached->second.revision == state->revision &&
            cached->second.queueValues.size() == state->entries.size() && cached->second.entries) {
            bool queueStateMatches = true;
            for (size_t index = 0; index < state->entries.size(); ++index) {
                if (state->entries[index].renderer->RenderQueue() != cached->second.queueValues[index]) {
                    queueStateMatches = false;
                    break;
                }
            }
            if (queueStateMatches)
                return cached->second.entries;
        }
    }

    std::vector<int32_t> queueValues;
    queueValues.reserve(state->entries.size());
    for (const auto &entry : state->entries)
        queueValues.push_back(entry.renderer->RenderQueue());

    std::vector<size_t> sortedIndices;
    sortedIndices.reserve(state->entries.size());
    for (size_t index = 0; index < state->entries.size(); ++index) {
        if (queueValues[index] >= queueMin && queueValues[index] <= queueMax)
            sortedIndices.push_back(index);
    }
    std::sort(sortedIndices.begin(), sortedIndices.end(), [&](size_t leftIndex, size_t rightIndex) {
        const int32_t leftQueue = queueValues[leftIndex];
        const int32_t rightQueue = queueValues[rightIndex];
        const auto &left = state->entries[leftIndex];
        const auto &right = state->entries[rightIndex];
        return leftQueue != rightQueue ? leftQueue < rightQueue : left.id < right.id;
    });

    auto result = std::make_shared<SnapshotEntries>();
    result->reserve(sortedIndices.size());
    for (const size_t index : sortedIndices)
        result->push_back(state->entries[index]);

    SnapshotCacheEntry cacheEntry;
    cacheEntry.revision = state->revision;
    cacheEntry.queueValues = std::move(queueValues);
    cacheEntry.entries = result;

    // Cache publication is copy-on-write. Snapshot readers never wait for a
    // writer and an older cache remains valid for readers holding its shared
    // pointer. Keep only entries belonging to this registry revision.
    auto observed = std::atomic_load_explicit(&m_snapshotCache, std::memory_order_acquire);
    auto nextCache = std::make_shared<SnapshotCache>();
    if (observed) {
        for (const auto &record : observed->entries) {
            if (record.second.revision == state->revision)
                nextCache->entries.emplace(record.first, record.second);
        }
    }
    nextCache->entries.insert_or_assign(key, std::move(cacheEntry));
    std::shared_ptr<const SnapshotCache> publishedCache = std::move(nextCache);
    (void)std::atomic_compare_exchange_weak_explicit(&m_snapshotCache, &observed, publishedCache,
                                                     std::memory_order_release, std::memory_order_acquire);
    return result;
}

uint64_t ParticleGpuDrawRegistry::Revision() const
{
    const auto state = std::atomic_load_explicit(&m_state, std::memory_order_acquire);
    return state ? state->revision : 0;
}

size_t ParticleGpuDrawRegistry::Size() const
{
    const auto state = std::atomic_load_explicit(&m_state, std::memory_order_acquire);
    return state ? state->entries.size() : 0;
}

} // namespace infernux::particle
