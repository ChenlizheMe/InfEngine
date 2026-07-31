#pragma once

#include <function/renderer/rhi/RhiDevice.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>

namespace infernux::particle
{

enum class GpuParticleContactLifecycle : uint32_t
{
    Enter = 0,
    Stay = 1,
    Exit = 2,
};

/// Full contact context retained on the GPU. The record deliberately carries
/// enough data for Exit and resumed Wait/Until work after the source Collider
/// has already left the scene.
struct alignas(16) GpuParticleContactRecord
{
    std::array<uint32_t, 4> identity{};
    std::array<float, 4> pointPenetration{};
    std::array<float, 4> normalSpeed{};
    std::array<float, 4> relativeVelocity{};
    std::array<float, 4> material{};
    std::array<uint32_t, 4> metadata{};
};

struct alignas(16) GpuParticleContactHashSlot
{
    std::array<uint32_t, 4> key{};
    std::array<uint32_t, 4> value{};
};

struct alignas(16) GpuParticleContactWorkItem
{
    std::array<uint32_t, 4> identity{};
    std::array<uint32_t, 4> dispatch{};
};

/// Open-addressed state for a Join that belongs to one exact contact
/// invocation. Unlike ordinary continuation joins, contact joins cannot be
/// indexed by particle alone because one particle may touch several Colliders
/// in the same simulation step.
struct alignas(16) GpuParticleContactJoinState
{
    std::array<uint32_t, 4> identity{};
    std::array<uint32_t, 4> context{};
    std::array<uint32_t, 4> state{};
};

struct alignas(16) GpuParticleContactCounters
{
    uint32_t contactOverflow = 0;
    uint32_t workItemOverflow = 0;
    uint32_t currentSimulationStep = 0;
    uint32_t resetSerial = 0;
    uint32_t currentRecordCount = 0;
    uint32_t workItemCount = 0;
    uint32_t reserved0 = 0;
    uint32_t reserved1 = 0;
};

struct GpuParticleContactResources
{
    rhi::BufferHandle contactRecords;
    rhi::BufferHandle hashSlots;
    rhi::BufferHandle particleRecordIndices;
    rhi::BufferHandle particleStates;
    rhi::BufferHandle workItems;
    rhi::BufferHandle dispatchIndirect;
    rhi::BufferHandle counters;
    rhi::BufferHandle continuationSnapshots;
    rhi::BufferHandle continuationJoinStates;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleContactTelemetry
{
    uint32_t particleCapacity = 0;
    uint32_t contactsPerParticle = 0;
    uint32_t contactRecordCapacity = 0;
    uint32_t contactHashCapacity = 0;
    uint32_t workItemCapacity = 0;
    uint32_t continuationSnapshotCapacity = 0;
    uint32_t continuationJoinCapacity = 0;
    uint32_t resetSerial = 0;
    uint64_t contactBytes = 0;
    uint64_t hashBytes = 0;
    uint64_t particleIndexBytes = 0;
    uint64_t particleStateBytes = 0;
    uint64_t workItemBytes = 0;
    uint64_t continuationSnapshotBytes = 0;
    uint64_t continuationJoinBytes = 0;
    bool gpuResidentOnly = true;
};

/// Bounded per-emitter storage for current/previous contacts and deterministic
/// lifecycle work. The compiler owns interpretation; C++ owns allocation,
/// binding identity and compatible-revision lifetime.
class ParticleGpuContactRuntime
{
  public:
    static constexpr uint32_t DefaultContactsPerParticle = 8;
    static constexpr uint32_t DefaultContactRecordBudget = 1u << 16u;
    static constexpr uint32_t MaximumParticleCapacity = 1u << 24u;

    ParticleGpuContactRuntime();
    ~ParticleGpuContactRuntime();

    ParticleGpuContactRuntime(const ParticleGpuContactRuntime &) = delete;
    ParticleGpuContactRuntime &operator=(const ParticleGpuContactRuntime &) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, uint32_t particleCapacity, uint32_t continuationSnapshotCapacity);
    [[nodiscard]] bool CreateCompatible(rhi::Device &device, uint32_t particleCapacity,
                                        uint32_t continuationSnapshotCapacity,
                                        const ParticleGpuContactRuntime &previous);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] bool SharesStorageWith(const ParticleGpuContactRuntime &other) const noexcept;
    [[nodiscard]] const GpuParticleContactResources &Resources() const noexcept;
    [[nodiscard]] GpuParticleContactTelemetry Telemetry() const noexcept;
    [[nodiscard]] rhi::BindingLayoutHandle Layout() const noexcept;
    [[nodiscard]] rhi::BindGroupHandle Group() const noexcept;

  private:
    struct ResidentStorage;
    [[nodiscard]] bool CreateInternal(rhi::Device &device, uint32_t particleCapacity,
                                      uint32_t continuationSnapshotCapacity, std::shared_ptr<ResidentStorage> storage);

    rhi::Device *m_device = nullptr;
    std::shared_ptr<ResidentStorage> m_storage;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    uint32_t m_resetSerial = 0;
};

static_assert(sizeof(GpuParticleContactRecord) == 96);
static_assert(offsetof(GpuParticleContactRecord, identity) == 0);
static_assert(offsetof(GpuParticleContactRecord, pointPenetration) == 16);
static_assert(offsetof(GpuParticleContactRecord, normalSpeed) == 32);
static_assert(offsetof(GpuParticleContactRecord, relativeVelocity) == 48);
static_assert(offsetof(GpuParticleContactRecord, material) == 64);
static_assert(offsetof(GpuParticleContactRecord, metadata) == 80);
static_assert(sizeof(GpuParticleContactHashSlot) == 32);
static_assert(offsetof(GpuParticleContactHashSlot, value) == 16);
static_assert(sizeof(GpuParticleContactWorkItem) == 32);
static_assert(offsetof(GpuParticleContactWorkItem, dispatch) == 16);
static_assert(sizeof(GpuParticleContactJoinState) == 48);
static_assert(offsetof(GpuParticleContactJoinState, context) == 16);
static_assert(offsetof(GpuParticleContactJoinState, state) == 32);
static_assert(sizeof(GpuParticleContactCounters) == 32);

} // namespace infernux::particle
