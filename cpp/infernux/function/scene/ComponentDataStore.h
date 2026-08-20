#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace infernux
{

/// SoA data store for Python InxComponent numeric fields.
///
/// Each registered component class gets its own set of parallel arrays
/// (one per numeric field).  Per-element access is O(1) via slot index.
/// Batch gather/scatter enables efficient numpy ↔ engine data transfer.
class ComponentDataStore
{
  public:
    using SchemaTransactionId = uint64_t;
    using PreparedClassId = uint32_t;
    using SchemaCommitMap = std::unordered_map<PreparedClassId, uint32_t>;

    static constexpr PreparedClassId InvalidPreparedClassId = UINT32_MAX;

    struct SlotHandle
    {
        uint32_t index = UINT32_MAX;
        uint32_t generation = 0;

        [[nodiscard]] bool IsValid() const noexcept
        {
            return index != UINT32_MAX && generation != 0;
        }
    };

    enum class DataType : uint8_t
    {
        Float64, // Python float → double
        Int64,   // Python int → int64_t
        Bool,    // Python bool → uint8_t
        Vec2,    // Vector2 → 2 × float
        Vec3,    // Vector3 → 3 × float
        Vec4,    // vec4f   → 4 × float
    };

    struct FieldMigration
    {
        uint32_t sourceFieldId = UINT32_MAX;
        uint32_t destinationFieldId = UINT32_MAX;
    };

    static ComponentDataStore &Instance();

    // ── class / field registration ──

    uint32_t RegisterClass(const std::string &className);
    uint32_t RegisterField(uint32_t classId, const std::string &fieldName, DataType type);
    uint32_t GetClassId(const std::string &className) const;
    uint32_t GetFieldId(uint32_t classId, const std::string &fieldName) const;
    [[nodiscard]] size_t GetPublishedClassCount() const noexcept;

    // -- transactional schema publication --

    SchemaTransactionId BeginSchemaTransaction();
    PreparedClassId PrepareClass(SchemaTransactionId transactionId, const std::string &className);
    uint32_t PrepareField(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                          const std::string &fieldName, DataType type);
    [[nodiscard]] bool HasPreparedClass(SchemaTransactionId transactionId, PreparedClassId preparedClassId) const;
    [[nodiscard]] PreparedClassId FindPreparedClass(SchemaTransactionId transactionId,
                                                    const std::string &className) const;
    [[nodiscard]] uint32_t GetPreparedFieldId(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                              const std::string &fieldName) const;
    bool DiscardPreparedClass(SchemaTransactionId transactionId, PreparedClassId preparedClassId);

    void ReservePreparedClass(SchemaTransactionId transactionId, PreparedClassId preparedClassId, size_t capacity);
    SlotHandle AllocatePreparedSlot(SchemaTransactionId transactionId, PreparedClassId preparedClassId);
    void ReleasePreparedSlot(SchemaTransactionId transactionId, PreparedClassId preparedClassId, SlotHandle handle);
    [[nodiscard]] bool IsPreparedSlotAlive(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                           SlotHandle handle) const;

    SlotHandle MigrateSlotToPrepared(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                     uint32_t sourceClassId, SlotHandle sourceHandle,
                                     const FieldMigration *fieldMigrations, size_t migrationCount);

    double GetPreparedFloat(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                            SlotHandle handle) const;
    void SetPreparedFloat(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                          SlotHandle handle, double value);
    int64_t GetPreparedInt(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                           SlotHandle handle) const;
    void SetPreparedInt(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                        SlotHandle handle, int64_t value);
    bool GetPreparedBool(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                         SlotHandle handle) const;
    void SetPreparedBool(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                         SlotHandle handle, bool value);
    void GetPreparedVec2(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                         SlotHandle handle, float out[2]) const;
    void SetPreparedVec2(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                         SlotHandle handle, const float in[2]);
    void GetPreparedVec3(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                         SlotHandle handle, float out[3]) const;
    void SetPreparedVec3(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                         SlotHandle handle, const float in[3]);
    void GetPreparedVec4(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                         SlotHandle handle, float out[4]) const;
    void SetPreparedVec4(SchemaTransactionId transactionId, PreparedClassId preparedClassId, uint32_t fieldId,
                         SlotHandle handle, const float in[4]);

    SchemaCommitMap SealSchemaTransaction(SchemaTransactionId transactionId);
    [[nodiscard]] uint32_t GetPreparedFinalClassId(SchemaTransactionId transactionId,
                                                   PreparedClassId preparedClassId) const;
    SchemaCommitMap CommitSchemaTransaction(SchemaTransactionId transactionId);
    bool FinalizeSchemaTransaction(SchemaTransactionId transactionId);
    bool RollbackSchemaTransaction(SchemaTransactionId transactionId) noexcept;
    [[nodiscard]] bool IsSchemaTransactionActive(SchemaTransactionId transactionId) const noexcept;

    // ── slot lifecycle ──

    SlotHandle AllocateSlot(uint32_t classId);
    void ReleaseSlot(uint32_t classId, SlotHandle handle);
    void ReserveClass(uint32_t classId, size_t capacity);
    [[nodiscard]] size_t GetClassCapacity(uint32_t classId) const;
    [[nodiscard]] size_t GetClassAliveCount(uint32_t classId) const;
    [[nodiscard]] bool IsAlive(uint32_t classId, SlotHandle handle) const;

    // ── per-element scalar access ──

    double GetFloat(uint32_t classId, uint32_t fieldId, SlotHandle handle) const;
    void SetFloat(uint32_t classId, uint32_t fieldId, SlotHandle handle, double value);

    int64_t GetInt(uint32_t classId, uint32_t fieldId, SlotHandle handle) const;
    void SetInt(uint32_t classId, uint32_t fieldId, SlotHandle handle, int64_t value);

    bool GetBool(uint32_t classId, uint32_t fieldId, SlotHandle handle) const;
    void SetBool(uint32_t classId, uint32_t fieldId, SlotHandle handle, bool value);

    // ── per-element vector access ──

    void GetVec2(uint32_t classId, uint32_t fieldId, SlotHandle handle, float out[2]) const;
    void SetVec2(uint32_t classId, uint32_t fieldId, SlotHandle handle, const float in[2]);

    void GetVec3(uint32_t classId, uint32_t fieldId, SlotHandle handle, float out[3]) const;
    void SetVec3(uint32_t classId, uint32_t fieldId, SlotHandle handle, const float in[3]);

    void GetVec4(uint32_t classId, uint32_t fieldId, SlotHandle handle, float out[4]) const;
    void SetVec4(uint32_t classId, uint32_t fieldId, SlotHandle handle, const float in[4]);

    // ── batch gather/scatter ──

    void GatherFloat(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, double *out) const;
    void ScatterFloat(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, const double *in);

    void GatherInt(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, int64_t *out) const;
    void ScatterInt(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, const int64_t *in);

    void GatherBool(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, uint8_t *out) const;
    void ScatterBool(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, const uint8_t *in);

    void GatherVec3(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, float *out) const;
    void ScatterVec3(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, const float *in);

    void GatherVec2(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, float *out) const;
    void ScatterVec2(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, const float *in);

    void GatherVec4(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, float *out) const;
    void ScatterVec4(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, const float *in);

    /// Reset everything (e.g. scene unload).
    void Clear();

  private:
    ComponentDataStore() = default;

    static size_t ElementSize(DataType type);

    struct FieldStorage
    {
        DataType type{};
        std::vector<std::max_align_t> data;
        size_t elementSize = 0;

        void Grow(size_t newCapacity);
        void ResetSlot(size_t slot);

        template <typename T> T &At(size_t slot)
        {
            return *reinterpret_cast<T *>(Bytes() + slot * elementSize);
        }
        template <typename T> const T &At(size_t slot) const
        {
            return *reinterpret_cast<const T *>(Bytes() + slot * elementSize);
        }
        float *FloatsAt(size_t slot)
        {
            return reinterpret_cast<float *>(Bytes() + slot * elementSize);
        }
        const float *FloatsAt(size_t slot) const
        {
            return reinterpret_cast<const float *>(Bytes() + slot * elementSize);
        }

      private:
        std::byte *Bytes();
        const std::byte *Bytes() const;
    };

    struct ClassStorage
    {
        ClassStorage() = default;
        ClassStorage(const ClassStorage &) = delete;
        ClassStorage &operator=(const ClassStorage &) = delete;

        ClassStorage(ClassStorage &&other) noexcept
        {
            Swap(other);
        }

        ClassStorage &operator=(ClassStorage &&other) noexcept
        {
            if (this != &other)
                Swap(other);
            return *this;
        }

        std::vector<FieldStorage> fields;
        std::unordered_map<std::string, uint32_t> fieldNameToId;
        std::vector<uint8_t> alive;
        std::vector<uint32_t> generations;
        std::vector<uint32_t> nextFree;
        uint32_t freeHead = UINT32_MAX;
        size_t capacity = 0;
        size_t slotCount = 0;
        size_t aliveCount = 0;
        bool retired = false;

        void GrowTo(size_t newCapacity);
        void ReleaseStorage() noexcept;

      private:
        void Swap(ClassStorage &other) noexcept
        {
            fields.swap(other.fields);
            fieldNameToId.swap(other.fieldNameToId);
            alive.swap(other.alive);
            generations.swap(other.generations);
            nextFree.swap(other.nextFree);
            std::swap(freeHead, other.freeHead);
            std::swap(capacity, other.capacity);
            std::swap(slotCount, other.slotCount);
            std::swap(aliveCount, other.aliveCount);
            std::swap(retired, other.retired);
        }
    };

    struct PreparedClass
    {
        std::string name;
        ClassStorage storage;
        bool active = true;
    };

    struct SchemaTransaction
    {
        SchemaTransactionId id = 0;
        bool sealed = false;
        bool committed = false;
        size_t classCountBeforeCommit = 0;
        size_t committedClassCount = 0;
        std::vector<PreparedClass> classes;
        std::unordered_map<std::string, PreparedClassId> classNameToId;
        SchemaCommitMap commitMap;
        std::unordered_map<std::string, uint32_t> publishedNameMap;
        std::vector<uint32_t> retiredClassIds;
        std::vector<uint32_t> committedClassIds;
        std::vector<uint32_t> freeClassIdsBeforeCommit;
    };

    std::vector<ClassStorage> m_classes;
    std::unordered_map<std::string, uint32_t> m_classNameToId;
    std::optional<SchemaTransaction> m_schemaTransaction;
    SchemaTransactionId m_nextSchemaTransactionId = 1;
    std::vector<uint32_t> m_freeClassIds;

    ClassStorage &RequireClass(uint32_t classId);
    const ClassStorage &RequireClass(uint32_t classId) const;
    FieldStorage &RequireField(uint32_t classId, uint32_t fieldId, DataType expectedType);
    const FieldStorage &RequireField(uint32_t classId, uint32_t fieldId, DataType expectedType) const;
    static void RequireAlive(const ClassStorage &storage, SlotHandle handle);
    static void RequireAllAlive(const ClassStorage &storage, const SlotHandle *handles, size_t count);
    static SlotHandle AllocateSlotInStorage(ClassStorage &storage);
    static void ReleaseSlotInStorage(ClassStorage &storage, SlotHandle handle);
    static bool IsAliveInStorage(const ClassStorage &storage, SlotHandle handle) noexcept;
    static std::string LayoutIdentity(const std::string &className);
    void MarkSupersededLayouts(SchemaTransaction &transaction, const std::string &className);
    void ReclaimRetiredClass(uint32_t classId) noexcept;

    SchemaTransaction &RequireSchemaTransaction(SchemaTransactionId transactionId);
    const SchemaTransaction &RequireSchemaTransaction(SchemaTransactionId transactionId) const;
    PreparedClass &RequirePreparedClass(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                        bool requireMutable = false);
    const PreparedClass &RequirePreparedClass(SchemaTransactionId transactionId, PreparedClassId preparedClassId) const;
    FieldStorage &RequirePreparedField(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                       uint32_t fieldId, DataType expectedType, bool requireMutable = false);
    const FieldStorage &RequirePreparedField(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                             uint32_t fieldId, DataType expectedType) const;
    void RequireNoSchemaTransaction(const char *operation) const;
};

} // namespace infernux
