#include "ComponentDataStore.h"

#include <algorithm>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <type_traits>

/**
 * CDS Debug trace
 */
#include <InxLog.h>
#include <cstdlib>
#include <thread>
#include <unistd.h>

namespace infernux::cds_trace
{
inline bool enabled()
{
    static const bool on = []() {
        const char *e = std::getenv("INFX_CDS_TRACE");
        return e && e[0] != '\0' && e[0] != '0';
    };
    return on;
}
inline thread_local int depth = 0;
inline long tid()
{
#ifdef __linux__
    return static_cast<long>(::gettid());
#else
    return static_cast<long>(std::hash<std::thread::id>{}(std::this_thread::get_id()));
#endif
}
struct Scope
{
    const char *op;
    Scope(const char *o) : op(o)
    {
        if (!enabled())
            return;
        ++depth;
        INXLOG_INFO("[CDS %s -30s] + tid=%ld depth=%d", op, tid(), depth);
        if (depth > 1) {
            INXLOG_INFO("[CDS !!REENTRANT!!] %s at depth %d", op, depth);
        }
    }
    ~Scope()
    {
        if (!enabled())
            return;
        INXLOG_INFO("[CDS %-30s] - tid=%ld depth=%d", op, tid(), depth);
        --depth;
    }
};

} // namespace infernux::cds_trace

namespace infernux
{

ComponentDataStore &ComponentDataStore::Instance()
{
    // Intentionally leaked until EngineServices owns subsystem shutdown order.
    static ComponentDataStore *instance = new ComponentDataStore();
    return *instance;
}

size_t ComponentDataStore::ElementSize(DataType type)
{
    switch (type) {
    case DataType::Float64:
        return sizeof(double);
    case DataType::Int64:
        return sizeof(int64_t);
    case DataType::Bool:
        return sizeof(uint8_t);
    case DataType::Vec2:
        return sizeof(float) * 2;
    case DataType::Vec3:
        return sizeof(float) * 3;
    case DataType::Vec4:
        return sizeof(float) * 4;
    }
    throw std::invalid_argument("ComponentDataStore: invalid field data type");
}

std::byte *ComponentDataStore::FieldStorage::Bytes()
{
    return reinterpret_cast<std::byte *>(data.data());
}

const std::byte *ComponentDataStore::FieldStorage::Bytes() const
{
    return reinterpret_cast<const std::byte *>(data.data());
}

void ComponentDataStore::FieldStorage::Grow(size_t newCapacity)
{
    const size_t byteCount = newCapacity * elementSize;
    const size_t wordCount = (byteCount + sizeof(std::max_align_t) - 1) / sizeof(std::max_align_t);
    data.resize(wordCount);
}

void ComponentDataStore::FieldStorage::ResetSlot(size_t slot)
{
    std::memset(Bytes() + slot * elementSize, 0, elementSize);
}

void ComponentDataStore::ClassStorage::ReleaseStorage() noexcept
{
    std::vector<FieldStorage>().swap(fields);
    std::unordered_map<std::string, uint32_t>().swap(fieldNameToId);
    std::vector<uint8_t>().swap(alive);
    std::vector<uint32_t>().swap(generations);
    std::vector<uint32_t>().swap(nextFree);
    freeHead = UINT32_MAX;
    capacity = 0;
    slotCount = 0;
    aliveCount = 0;
    retired = true;
}

void ComponentDataStore::ClassStorage::GrowTo(size_t newCapacity)
{
    if (newCapacity <= capacity)
        return;
    for (auto &field : fields) {
        field.Grow(newCapacity);
    }
    alive.resize(newCapacity, 0);
    generations.resize(newCapacity, 1);
    nextFree.resize(newCapacity, UINT32_MAX);
    capacity = newCapacity;
}

std::string ComponentDataStore::LayoutIdentity(const std::string &className)
{
    const auto separator = className.rfind('@');
    return separator == std::string::npos ? std::string{} : className.substr(0, separator);
}

void ComponentDataStore::MarkSupersededLayouts(SchemaTransaction &transaction, const std::string &className)
{
    const std::string identity = LayoutIdentity(className);
    if (identity.empty())
        return;

    for (const auto &[publishedName, classId] : m_classNameToId) {
        if (classId >= m_classes.size() || LayoutIdentity(publishedName) != identity)
            continue;
        auto &storage = m_classes[classId];
        if (storage.retired)
            continue;
        storage.retired = true;
        transaction.retiredClassIds.push_back(classId);
    }
}

void ComponentDataStore::ReclaimRetiredClass(uint32_t classId) noexcept
{
    if (classId >= m_classes.size())
        return;
    auto &storage = m_classes[classId];
    if (!storage.retired || storage.aliveCount != 0)
        return;

    storage.ReleaseStorage();
    if (std::find(m_freeClassIds.begin(), m_freeClassIds.end(), classId) == m_freeClassIds.end())
        m_freeClassIds.push_back(classId);
}

ComponentDataStore::ClassStorage &ComponentDataStore::RequireClass(uint32_t classId)
{
    if (classId >= m_classes.size())
        throw std::out_of_range("ComponentDataStore: invalid class id");
    return m_classes[classId];
}

const ComponentDataStore::ClassStorage &ComponentDataStore::RequireClass(uint32_t classId) const
{
    if (classId >= m_classes.size())
        throw std::out_of_range("ComponentDataStore: invalid class id");
    return m_classes[classId];
}

ComponentDataStore::FieldStorage &ComponentDataStore::RequireField(uint32_t classId, uint32_t fieldId,
                                                                   DataType expectedType)
{
    auto &storage = RequireClass(classId);
    if (fieldId >= storage.fields.size())
        throw std::out_of_range("ComponentDataStore: invalid field id");
    auto &field = storage.fields[fieldId];
    if (field.type != expectedType)
        throw std::invalid_argument("ComponentDataStore: field type mismatch");
    return field;
}

const ComponentDataStore::FieldStorage &ComponentDataStore::RequireField(uint32_t classId, uint32_t fieldId,
                                                                         DataType expectedType) const
{
    const auto &storage = RequireClass(classId);
    if (fieldId >= storage.fields.size())
        throw std::out_of_range("ComponentDataStore: invalid field id");
    const auto &field = storage.fields[fieldId];
    if (field.type != expectedType)
        throw std::invalid_argument("ComponentDataStore: field type mismatch");
    return field;
}

void ComponentDataStore::RequireAlive(const ClassStorage &storage, SlotHandle handle)
{
    if (!handle.IsValid() || handle.index >= storage.slotCount || !storage.alive[handle.index] ||
        storage.generations[handle.index] != handle.generation) {
        throw std::runtime_error("ComponentDataStore: stale or invalid slot handle");
    }
}

void ComponentDataStore::RequireAllAlive(const ClassStorage &storage, const SlotHandle *handles, size_t count)
{
    for (size_t i = 0; i < count; ++i)
        RequireAlive(storage, handles[i]);
}

ComponentDataStore::SlotHandle ComponentDataStore::AllocateSlotInStorage(ClassStorage &storage)
{
    if (storage.retired)
        throw std::logic_error("ComponentDataStore: cannot allocate from a retired class layout");
    uint32_t index = UINT32_MAX;
    if (storage.freeHead != UINT32_MAX) {
        index = storage.freeHead;
        storage.freeHead = storage.nextFree[index];
    } else {
        if (storage.slotCount == storage.capacity) {
            const size_t newCapacity = storage.capacity == 0 ? 16 : storage.capacity * 2;
            storage.GrowTo(newCapacity);
        }
        index = static_cast<uint32_t>(storage.slotCount++);
    }

    storage.alive[index] = 1;
    storage.nextFree[index] = UINT32_MAX;
    for (auto &field : storage.fields)
        field.ResetSlot(index);
    ++storage.aliveCount;
    return SlotHandle{index, storage.generations[index]};
}

void ComponentDataStore::ReleaseSlotInStorage(ClassStorage &storage, SlotHandle handle)
{
    RequireAlive(storage, handle);
    storage.alive[handle.index] = 0;
    uint32_t &generation = storage.generations[handle.index];
    if (++generation == 0)
        generation = 1;
    storage.nextFree[handle.index] = storage.freeHead;
    storage.freeHead = handle.index;
    --storage.aliveCount;
}

bool ComponentDataStore::IsAliveInStorage(const ClassStorage &storage, SlotHandle handle) noexcept
{
    return handle.IsValid() && handle.index < storage.slotCount && storage.alive[handle.index] &&
           storage.generations[handle.index] == handle.generation;
}

void ComponentDataStore::RequireNoSchemaTransaction(const char *operation) const
{
    if (m_schemaTransaction) {
        throw std::logic_error(std::string("ComponentDataStore: cannot ") + operation +
                               " while a schema transaction is active");
    }
}

uint32_t ComponentDataStore::RegisterClass(const std::string &className)
{
    RequireNoSchemaTransaction("register a class");
    if (className.empty())
        throw std::invalid_argument("ComponentDataStore: class name cannot be empty");
    const auto it = m_classNameToId.find(className);
    if (it != m_classNameToId.end())
        return it->second;

    const uint32_t id = static_cast<uint32_t>(m_classes.size());
    m_classes.emplace_back();
    m_classNameToId.emplace(className, id);
    return id;
}

uint32_t ComponentDataStore::RegisterField(uint32_t classId, const std::string &fieldName, DataType type)
{
    RequireNoSchemaTransaction("register a field");
    if (fieldName.empty())
        throw std::invalid_argument("ComponentDataStore: field name cannot be empty");
    auto &storage = RequireClass(classId);
    const auto existing = storage.fieldNameToId.find(fieldName);
    if (existing != storage.fieldNameToId.end()) {
        if (storage.fields[existing->second].type != type)
            throw std::invalid_argument("ComponentDataStore: field re-registered with a different type");
        return existing->second;
    }

    FieldStorage field;
    field.type = type;
    field.elementSize = ElementSize(type);
    field.Grow(storage.capacity);
    const uint32_t fieldId = static_cast<uint32_t>(storage.fields.size());
    storage.fields.push_back(std::move(field));
    storage.fieldNameToId.emplace(fieldName, fieldId);
    return fieldId;
}

uint32_t ComponentDataStore::GetClassId(const std::string &className) const
{
    const auto it = m_classNameToId.find(className);
    return it != m_classNameToId.end() ? it->second : UINT32_MAX;
}

uint32_t ComponentDataStore::GetFieldId(uint32_t classId, const std::string &fieldName) const
{
    if (classId >= m_classes.size())
        return UINT32_MAX;
    const auto &storage = m_classes[classId];
    const auto it = storage.fieldNameToId.find(fieldName);
    return it != storage.fieldNameToId.end() ? it->second : UINT32_MAX;
}

size_t ComponentDataStore::GetPublishedClassCount() const noexcept
{
    return static_cast<size_t>(std::count_if(m_classes.begin(), m_classes.end(),
                                             [](const ClassStorage &storage) { return !storage.retired; }));
}

ComponentDataStore::SchemaTransaction &ComponentDataStore::RequireSchemaTransaction(SchemaTransactionId transactionId)
{
    if (!m_schemaTransaction || m_schemaTransaction->id != transactionId)
        throw std::out_of_range("ComponentDataStore: invalid schema transaction id");
    return *m_schemaTransaction;
}

const ComponentDataStore::SchemaTransaction &
ComponentDataStore::RequireSchemaTransaction(SchemaTransactionId transactionId) const
{
    if (!m_schemaTransaction || m_schemaTransaction->id != transactionId)
        throw std::out_of_range("ComponentDataStore: invalid schema transaction id");
    return *m_schemaTransaction;
}

ComponentDataStore::PreparedClass &ComponentDataStore::RequirePreparedClass(SchemaTransactionId transactionId,
                                                                            PreparedClassId preparedClassId,
                                                                            bool requireMutable)
{
    auto &transaction = RequireSchemaTransaction(transactionId);
    if (requireMutable && transaction.sealed)
        throw std::logic_error("ComponentDataStore: sealed schema transaction is immutable");
    if (preparedClassId >= transaction.classes.size() || !transaction.classes[preparedClassId].active)
        throw std::out_of_range("ComponentDataStore: invalid prepared class id");
    return transaction.classes[preparedClassId];
}

const ComponentDataStore::PreparedClass &ComponentDataStore::RequirePreparedClass(SchemaTransactionId transactionId,
                                                                                  PreparedClassId preparedClassId) const
{
    const auto &transaction = RequireSchemaTransaction(transactionId);
    if (preparedClassId >= transaction.classes.size() || !transaction.classes[preparedClassId].active)
        throw std::out_of_range("ComponentDataStore: invalid prepared class id");
    return transaction.classes[preparedClassId];
}

ComponentDataStore::FieldStorage &ComponentDataStore::RequirePreparedField(SchemaTransactionId transactionId,
                                                                           PreparedClassId preparedClassId,
                                                                           uint32_t fieldId, DataType expectedType,
                                                                           bool requireMutable)
{
    auto &prepared = RequirePreparedClass(transactionId, preparedClassId, requireMutable);
    if (fieldId >= prepared.storage.fields.size())
        throw std::out_of_range("ComponentDataStore: invalid prepared field id");
    auto &field = prepared.storage.fields[fieldId];
    if (field.type != expectedType)
        throw std::invalid_argument("ComponentDataStore: prepared field type mismatch");
    return field;
}

const ComponentDataStore::FieldStorage &ComponentDataStore::RequirePreparedField(SchemaTransactionId transactionId,
                                                                                 PreparedClassId preparedClassId,
                                                                                 uint32_t fieldId,
                                                                                 DataType expectedType) const
{
    const auto &prepared = RequirePreparedClass(transactionId, preparedClassId);
    if (fieldId >= prepared.storage.fields.size())
        throw std::out_of_range("ComponentDataStore: invalid prepared field id");
    const auto &field = prepared.storage.fields[fieldId];
    if (field.type != expectedType)
        throw std::invalid_argument("ComponentDataStore: prepared field type mismatch");
    return field;
}

ComponentDataStore::SchemaTransactionId ComponentDataStore::BeginSchemaTransaction()
{
    if (m_schemaTransaction)
        throw std::logic_error("ComponentDataStore: a schema transaction is already active");

    SchemaTransactionId transactionId = m_nextSchemaTransactionId++;
    if (transactionId == 0) {
        transactionId = m_nextSchemaTransactionId++;
    }
    SchemaTransaction transaction;
    transaction.id = transactionId;
    m_schemaTransaction.emplace(std::move(transaction));
    return transactionId;
}

ComponentDataStore::PreparedClassId ComponentDataStore::PrepareClass(SchemaTransactionId transactionId,
                                                                     const std::string &className)
{
    if (className.empty())
        throw std::invalid_argument("ComponentDataStore: prepared class name cannot be empty");
    auto &transaction = RequireSchemaTransaction(transactionId);
    if (transaction.sealed)
        throw std::logic_error("ComponentDataStore: sealed schema transaction is immutable");
    if (m_classNameToId.find(className) != m_classNameToId.end())
        throw std::invalid_argument("ComponentDataStore: prepared class name is already published");
    if (const auto existing = transaction.classNameToId.find(className); existing != transaction.classNameToId.end()) {
        return existing->second;
    }
    if (transaction.classes.size() >= static_cast<size_t>(InvalidPreparedClassId))
        throw std::overflow_error("ComponentDataStore: too many prepared classes");

    const auto preparedClassId = static_cast<PreparedClassId>(transaction.classes.size());
    PreparedClass prepared;
    prepared.name = className;
    transaction.classes.push_back(std::move(prepared));
    try {
        transaction.classNameToId.emplace(className, preparedClassId);
    } catch (...) {
        transaction.classes.pop_back();
        throw;
    }
    return preparedClassId;
}

uint32_t ComponentDataStore::PrepareField(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                          const std::string &fieldName, DataType type)
{
    if (fieldName.empty())
        throw std::invalid_argument("ComponentDataStore: prepared field name cannot be empty");
    auto &storage = RequirePreparedClass(transactionId, preparedClassId, true).storage;
    if (const auto existing = storage.fieldNameToId.find(fieldName); existing != storage.fieldNameToId.end()) {
        if (storage.fields[existing->second].type != type)
            throw std::invalid_argument("ComponentDataStore: prepared field re-registered with a different type");
        return existing->second;
    }

    FieldStorage field;
    field.type = type;
    field.elementSize = ElementSize(type);
    field.Grow(storage.capacity);
    const auto fieldId = static_cast<uint32_t>(storage.fields.size());
    storage.fields.push_back(std::move(field));
    try {
        storage.fieldNameToId.emplace(fieldName, fieldId);
    } catch (...) {
        storage.fields.pop_back();
        throw;
    }
    return fieldId;
}

bool ComponentDataStore::HasPreparedClass(SchemaTransactionId transactionId, PreparedClassId preparedClassId) const
{
    const auto &transaction = RequireSchemaTransaction(transactionId);
    return preparedClassId < transaction.classes.size() && transaction.classes[preparedClassId].active;
}

ComponentDataStore::PreparedClassId ComponentDataStore::FindPreparedClass(SchemaTransactionId transactionId,
                                                                          const std::string &className) const
{
    const auto &transaction = RequireSchemaTransaction(transactionId);
    const auto found = transaction.classNameToId.find(className);
    return found != transaction.classNameToId.end() ? found->second : InvalidPreparedClassId;
}

uint32_t ComponentDataStore::GetPreparedFieldId(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                                const std::string &fieldName) const
{
    const auto &storage = RequirePreparedClass(transactionId, preparedClassId).storage;
    const auto found = storage.fieldNameToId.find(fieldName);
    return found != storage.fieldNameToId.end() ? found->second : UINT32_MAX;
}

bool ComponentDataStore::DiscardPreparedClass(SchemaTransactionId transactionId, PreparedClassId preparedClassId)
{
    auto &transaction = RequireSchemaTransaction(transactionId);
    if (transaction.sealed)
        throw std::logic_error("ComponentDataStore: cannot discard a class after schema transaction seal");
    if (preparedClassId >= transaction.classes.size() || !transaction.classes[preparedClassId].active)
        return false;

    auto &prepared = transaction.classes[preparedClassId];
    transaction.classNameToId.erase(prepared.name);
    prepared.name.clear();
    prepared.storage = ClassStorage{};
    prepared.active = false;
    return true;
}

void ComponentDataStore::ReservePreparedClass(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                              size_t capacity)
{
    RequirePreparedClass(transactionId, preparedClassId, true).storage.GrowTo(capacity);
}

ComponentDataStore::SlotHandle ComponentDataStore::AllocatePreparedSlot(SchemaTransactionId transactionId,
                                                                        PreparedClassId preparedClassId)
{
    return AllocateSlotInStorage(RequirePreparedClass(transactionId, preparedClassId, true).storage);
}

void ComponentDataStore::ReleasePreparedSlot(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                             SlotHandle handle)
{
    ReleaseSlotInStorage(RequirePreparedClass(transactionId, preparedClassId, true).storage, handle);
}

bool ComponentDataStore::IsPreparedSlotAlive(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                             SlotHandle handle) const
{
    return IsAliveInStorage(RequirePreparedClass(transactionId, preparedClassId).storage, handle);
}

ComponentDataStore::SlotHandle
ComponentDataStore::MigrateSlotToPrepared(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                          uint32_t sourceClassId, SlotHandle sourceHandle,
                                          const FieldMigration *fieldMigrations, size_t migrationCount)
{
    auto &destinationStorage = RequirePreparedClass(transactionId, preparedClassId, true).storage;
    const auto &sourceStorage = RequireClass(sourceClassId);
    RequireAlive(sourceStorage, sourceHandle);
    if (migrationCount != 0 && fieldMigrations == nullptr)
        throw std::invalid_argument("ComponentDataStore: migration map cannot be null");

    std::vector<uint8_t> destinationSeen(destinationStorage.fields.size(), 0);
    for (size_t i = 0; i < migrationCount; ++i) {
        const auto &migration = fieldMigrations[i];
        if (migration.sourceFieldId >= sourceStorage.fields.size() ||
            migration.destinationFieldId >= destinationStorage.fields.size()) {
            throw std::out_of_range("ComponentDataStore: migration field id is invalid");
        }
        if (destinationSeen[migration.destinationFieldId] != 0)
            throw std::invalid_argument("ComponentDataStore: migration writes a destination field more than once");
        destinationSeen[migration.destinationFieldId] = 1;
        const auto sourceType = sourceStorage.fields[migration.sourceFieldId].type;
        const auto destinationType = destinationStorage.fields[migration.destinationFieldId].type;
        if (sourceType != destinationType && !(sourceType == DataType::Int64 && destinationType == DataType::Float64)) {
            throw std::invalid_argument("ComponentDataStore: incompatible field migration type");
        }
    }

    const SlotHandle destinationHandle = AllocateSlotInStorage(destinationStorage);
    for (size_t i = 0; i < migrationCount; ++i) {
        const auto &migration = fieldMigrations[i];
        const auto &source = sourceStorage.fields[migration.sourceFieldId];
        auto &destination = destinationStorage.fields[migration.destinationFieldId];
        if (source.type == DataType::Int64 && destination.type == DataType::Float64) {
            destination.At<double>(destinationHandle.index) =
                static_cast<double>(source.At<int64_t>(sourceHandle.index));
            continue;
        }
        switch (source.type) {
        case DataType::Float64:
            destination.At<double>(destinationHandle.index) = source.At<double>(sourceHandle.index);
            break;
        case DataType::Int64:
            destination.At<int64_t>(destinationHandle.index) = source.At<int64_t>(sourceHandle.index);
            break;
        case DataType::Bool:
            destination.At<uint8_t>(destinationHandle.index) = source.At<uint8_t>(sourceHandle.index);
            break;
        case DataType::Vec2:
            std::copy_n(source.FloatsAt(sourceHandle.index), 2, destination.FloatsAt(destinationHandle.index));
            break;
        case DataType::Vec3:
            std::copy_n(source.FloatsAt(sourceHandle.index), 3, destination.FloatsAt(destinationHandle.index));
            break;
        case DataType::Vec4:
            std::copy_n(source.FloatsAt(sourceHandle.index), 4, destination.FloatsAt(destinationHandle.index));
            break;
        }
    }
    return destinationHandle;
}

#define INX_CDS_PREPARED_SCALAR_ACCESSORS(Name, CppType, StoreType)                                                    \
    CppType ComponentDataStore::GetPrepared##Name(SchemaTransactionId transactionId, PreparedClassId preparedClassId,  \
                                                  uint32_t fieldId, SlotHandle handle) const                           \
    {                                                                                                                  \
        const auto &storage = RequirePreparedClass(transactionId, preparedClassId).storage;                            \
        RequireAlive(storage, handle);                                                                                 \
        return RequirePreparedField(transactionId, preparedClassId, fieldId, DataType::StoreType)                      \
            .At<CppType>(handle.index);                                                                                \
    }                                                                                                                  \
    void ComponentDataStore::SetPrepared##Name(SchemaTransactionId transactionId, PreparedClassId preparedClassId,     \
                                               uint32_t fieldId, SlotHandle handle, CppType value)                     \
    {                                                                                                                  \
        auto &storage = RequirePreparedClass(transactionId, preparedClassId, true).storage;                            \
        RequireAlive(storage, handle);                                                                                 \
        RequirePreparedField(transactionId, preparedClassId, fieldId, DataType::StoreType, true)                       \
            .At<CppType>(handle.index) = value;                                                                        \
    }

INX_CDS_PREPARED_SCALAR_ACCESSORS(Float, double, Float64)
INX_CDS_PREPARED_SCALAR_ACCESSORS(Int, int64_t, Int64)

#undef INX_CDS_PREPARED_SCALAR_ACCESSORS

bool ComponentDataStore::GetPreparedBool(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                         uint32_t fieldId, SlotHandle handle) const
{
    const auto &storage = RequirePreparedClass(transactionId, preparedClassId).storage;
    RequireAlive(storage, handle);
    return RequirePreparedField(transactionId, preparedClassId, fieldId, DataType::Bool).At<uint8_t>(handle.index) != 0;
}

void ComponentDataStore::SetPreparedBool(SchemaTransactionId transactionId, PreparedClassId preparedClassId,
                                         uint32_t fieldId, SlotHandle handle, bool value)
{
    auto &storage = RequirePreparedClass(transactionId, preparedClassId, true).storage;
    RequireAlive(storage, handle);
    RequirePreparedField(transactionId, preparedClassId, fieldId, DataType::Bool, true).At<uint8_t>(handle.index) =
        value ? 1 : 0;
}

#define INX_CDS_PREPARED_VECTOR_ACCESSORS(Dim, StoreType)                                                              \
    void ComponentDataStore::GetPreparedVec##Dim(SchemaTransactionId transactionId, PreparedClassId preparedClassId,   \
                                                 uint32_t fieldId, SlotHandle handle, float out[Dim]) const            \
    {                                                                                                                  \
        const auto &storage = RequirePreparedClass(transactionId, preparedClassId).storage;                            \
        RequireAlive(storage, handle);                                                                                 \
        const float *source =                                                                                          \
            RequirePreparedField(transactionId, preparedClassId, fieldId, DataType::StoreType).FloatsAt(handle.index); \
        std::copy_n(source, Dim, out);                                                                                 \
    }                                                                                                                  \
    void ComponentDataStore::SetPreparedVec##Dim(SchemaTransactionId transactionId, PreparedClassId preparedClassId,   \
                                                 uint32_t fieldId, SlotHandle handle, const float in[Dim])             \
    {                                                                                                                  \
        auto &storage = RequirePreparedClass(transactionId, preparedClassId, true).storage;                            \
        RequireAlive(storage, handle);                                                                                 \
        float *destination = RequirePreparedField(transactionId, preparedClassId, fieldId, DataType::StoreType, true)  \
                                 .FloatsAt(handle.index);                                                              \
        std::copy_n(in, Dim, destination);                                                                             \
    }

INX_CDS_PREPARED_VECTOR_ACCESSORS(2, Vec2)
INX_CDS_PREPARED_VECTOR_ACCESSORS(3, Vec3)
INX_CDS_PREPARED_VECTOR_ACCESSORS(4, Vec4)

#undef INX_CDS_PREPARED_VECTOR_ACCESSORS

ComponentDataStore::SchemaCommitMap ComponentDataStore::SealSchemaTransaction(SchemaTransactionId transactionId)
{
    auto &transaction = RequireSchemaTransaction(transactionId);
    if (transaction.sealed)
        return transaction.commitMap;

    size_t activeCount = 0;
    for (const auto &prepared : transaction.classes)
        activeCount += prepared.active ? 1U : 0U;
    if (activeCount > static_cast<size_t>(std::numeric_limits<uint32_t>::max()) - m_classes.size())
        throw std::overflow_error("ComponentDataStore: published class id space exhausted");

    auto publishedNameMap = m_classNameToId;
    publishedNameMap.reserve(publishedNameMap.size() + activeCount);
    SchemaCommitMap commitMap;
    commitMap.reserve(activeCount);
    std::vector<uint32_t> reusableClassIds = m_freeClassIds;
    std::sort(reusableClassIds.begin(), reusableClassIds.end());
    size_t reusableIndex = 0;
    uint32_t nextClassId = static_cast<uint32_t>(m_classes.size());
    for (PreparedClassId preparedClassId = 0; preparedClassId < transaction.classes.size(); ++preparedClassId) {
        const auto &prepared = transaction.classes[preparedClassId];
        if (!prepared.active)
            continue;
        const uint32_t classId =
            reusableIndex < reusableClassIds.size() ? reusableClassIds[reusableIndex++] : nextClassId++;
        if (!publishedNameMap.emplace(prepared.name, classId).second)
            throw std::invalid_argument("ComponentDataStore: prepared class conflicts with a published class");
        commitMap.emplace(preparedClassId, classId);
    }

    m_classes.reserve(std::max(m_classes.size(), static_cast<size_t>(nextClassId)));
    transaction.publishedNameMap = std::move(publishedNameMap);
    transaction.commitMap = std::move(commitMap);
    transaction.sealed = true;
    return transaction.commitMap;
}

uint32_t ComponentDataStore::GetPreparedFinalClassId(SchemaTransactionId transactionId,
                                                     PreparedClassId preparedClassId) const
{
    const auto &transaction = RequireSchemaTransaction(transactionId);
    if (!transaction.sealed)
        return UINT32_MAX;
    const auto found = transaction.commitMap.find(preparedClassId);
    return found != transaction.commitMap.end() ? found->second : UINT32_MAX;
}

ComponentDataStore::SchemaCommitMap ComponentDataStore::CommitSchemaTransaction(SchemaTransactionId transactionId)
{
    SchemaCommitMap result = SealSchemaTransaction(transactionId);
    auto &transaction = RequireSchemaTransaction(transactionId);
    if (transaction.committed)
        return result;
    static_assert(std::is_nothrow_move_constructible_v<ClassStorage>);

    transaction.classCountBeforeCommit = m_classes.size();
    transaction.freeClassIdsBeforeCommit = m_freeClassIds;
    std::vector<uint8_t> reusedClassIds(m_classes.size(), 0);
    for (PreparedClassId preparedClassId = 0; preparedClassId < transaction.classes.size(); ++preparedClassId) {
        const auto &prepared = transaction.classes[preparedClassId];
        if (!prepared.active)
            continue;
        const auto committed = transaction.commitMap.find(preparedClassId);
        if (committed == transaction.commitMap.end())
            throw std::logic_error("ComponentDataStore: sealed schema publication order changed");
        if (committed->second < m_classes.size()) {
            if (std::find(m_freeClassIds.begin(), m_freeClassIds.end(), committed->second) == m_freeClassIds.end()) {
                throw std::logic_error("ComponentDataStore: sealed schema reused a live class id");
            }
            reusedClassIds[committed->second] = 1;
        }
    }

    uint32_t largestClassId = static_cast<uint32_t>(m_classes.size());
    for (const auto &[preparedClassId, classId] : transaction.commitMap) {
        (void)preparedClassId;
        largestClassId = std::max(largestClassId, classId + 1);
    }
    m_classes.resize(largestClassId);
    for (auto &prepared : transaction.classes) {
        if (!prepared.active)
            continue;
        const auto committed =
            transaction.commitMap.find(static_cast<PreparedClassId>(&prepared - transaction.classes.data()));
        if (committed == transaction.commitMap.end())
            throw std::logic_error("ComponentDataStore: prepared schema publication order changed");
        m_classes[committed->second] = std::move(prepared.storage);
        transaction.committedClassIds.push_back(committed->second);
        ++transaction.committedClassCount;
    }
    m_freeClassIds.erase(std::remove_if(m_freeClassIds.begin(), m_freeClassIds.end(),
                                        [&](uint32_t classId) {
                                            return classId < reusedClassIds.size() && reusedClassIds[classId] != 0;
                                        }),
                         m_freeClassIds.end());
    for (const auto &prepared : transaction.classes) {
        if (prepared.active)
            MarkSupersededLayouts(transaction, prepared.name);
    }
    m_classNameToId.swap(transaction.publishedNameMap);
    transaction.committed = true;
    return result;
}

bool ComponentDataStore::FinalizeSchemaTransaction(SchemaTransactionId transactionId)
{
    auto &transaction = RequireSchemaTransaction(transactionId);
    if (!transaction.committed)
        throw std::logic_error("ComponentDataStore: cannot finalize an uncommitted schema transaction");
    for (auto iterator = m_classNameToId.begin(); iterator != m_classNameToId.end();) {
        if (std::find(transaction.retiredClassIds.begin(), transaction.retiredClassIds.end(), iterator->second) !=
            transaction.retiredClassIds.end()) {
            iterator = m_classNameToId.erase(iterator);
        } else {
            ++iterator;
        }
    }
    for (const uint32_t classId : transaction.retiredClassIds)
        ReclaimRetiredClass(classId);
    m_schemaTransaction.reset();
    return true;
}

bool ComponentDataStore::RollbackSchemaTransaction(SchemaTransactionId transactionId) noexcept
{
    if (!m_schemaTransaction || m_schemaTransaction->id != transactionId)
        return false;
    if (m_schemaTransaction->committed) {
        auto &transaction = *m_schemaTransaction;
        for (const auto &[preparedClassId, classId] : transaction.commitMap) {
            if (classId >= transaction.classCountBeforeCommit)
                continue;
            if (preparedClassId >= transaction.classes.size() || !transaction.classes[preparedClassId].active)
                return false;
            m_classes[classId] = std::move(transaction.classes[preparedClassId].storage);
            m_classes[classId].retired = true;
        }
        if (transaction.classCountBeforeCommit > m_classes.size())
            return false;
        m_classes.resize(transaction.classCountBeforeCommit);
        m_freeClassIds = transaction.freeClassIdsBeforeCommit;
        for (const uint32_t classId : transaction.retiredClassIds) {
            if (classId < m_classes.size())
                m_classes[classId].retired = false;
        }
        m_classNameToId.swap(transaction.publishedNameMap);
    }
    m_schemaTransaction.reset();
    return true;
}

bool ComponentDataStore::IsSchemaTransactionActive(SchemaTransactionId transactionId) const noexcept
{
    return m_schemaTransaction && m_schemaTransaction->id == transactionId;
}

ComponentDataStore::SlotHandle ComponentDataStore::AllocateSlot(uint32_t classId)
{
    return AllocateSlotInStorage(RequireClass(classId));
}

void ComponentDataStore::ReleaseSlot(uint32_t classId, SlotHandle handle)
{
    auto &storage = RequireClass(classId);
    ReleaseSlotInStorage(storage, handle);
    if (storage.retired && storage.aliveCount == 0 && !m_schemaTransaction)
        ReclaimRetiredClass(classId);
}

void ComponentDataStore::ReserveClass(uint32_t classId, size_t capacity)
{
    RequireClass(classId).GrowTo(capacity);
}

size_t ComponentDataStore::GetClassCapacity(uint32_t classId) const
{
    return RequireClass(classId).capacity;
}

size_t ComponentDataStore::GetClassAliveCount(uint32_t classId) const
{
    return RequireClass(classId).aliveCount;
}

bool ComponentDataStore::IsAlive(uint32_t classId, SlotHandle handle) const
{
    return IsAliveInStorage(RequireClass(classId), handle);
}

#define INX_CDS_SCALAR_ACCESSORS(Name, CppType, StoreType)                                                             \
    CppType ComponentDataStore::Get##Name(uint32_t classId, uint32_t fieldId, SlotHandle handle) const                 \
    {                                                                                                                  \
        const auto &storage = RequireClass(classId);                                                                   \
        RequireAlive(storage, handle);                                                                                 \
        return RequireField(classId, fieldId, DataType::StoreType).At<CppType>(handle.index);                          \
    }                                                                                                                  \
    void ComponentDataStore::Set##Name(uint32_t classId, uint32_t fieldId, SlotHandle handle, CppType value)           \
    {                                                                                                                  \
        auto &storage = RequireClass(classId);                                                                         \
        RequireAlive(storage, handle);                                                                                 \
        RequireField(classId, fieldId, DataType::StoreType).At<CppType>(handle.index) = value;                         \
    }

INX_CDS_SCALAR_ACCESSORS(Float, double, Float64)
INX_CDS_SCALAR_ACCESSORS(Int, int64_t, Int64)

bool ComponentDataStore::GetBool(uint32_t classId, uint32_t fieldId, SlotHandle handle) const
{
    const auto &storage = RequireClass(classId);
    RequireAlive(storage, handle);
    return RequireField(classId, fieldId, DataType::Bool).At<uint8_t>(handle.index) != 0;
}

void ComponentDataStore::SetBool(uint32_t classId, uint32_t fieldId, SlotHandle handle, bool value)
{
    auto &storage = RequireClass(classId);
    RequireAlive(storage, handle);
    RequireField(classId, fieldId, DataType::Bool).At<uint8_t>(handle.index) = value ? 1 : 0;
}

#undef INX_CDS_SCALAR_ACCESSORS

#define INX_CDS_VECTOR_ACCESSORS(Dim, StoreType)                                                                       \
    void ComponentDataStore::GetVec##Dim(uint32_t classId, uint32_t fieldId, SlotHandle handle, float out[Dim]) const  \
    {                                                                                                                  \
        const auto &storage = RequireClass(classId);                                                                   \
        RequireAlive(storage, handle);                                                                                 \
        const float *source = RequireField(classId, fieldId, DataType::StoreType).FloatsAt(handle.index);              \
        std::copy_n(source, Dim, out);                                                                                 \
    }                                                                                                                  \
    void ComponentDataStore::SetVec##Dim(uint32_t classId, uint32_t fieldId, SlotHandle handle, const float in[Dim])   \
    {                                                                                                                  \
        auto &storage = RequireClass(classId);                                                                         \
        RequireAlive(storage, handle);                                                                                 \
        float *destination = RequireField(classId, fieldId, DataType::StoreType).FloatsAt(handle.index);               \
        std::copy_n(in, Dim, destination);                                                                             \
    }

INX_CDS_VECTOR_ACCESSORS(2, Vec2)
INX_CDS_VECTOR_ACCESSORS(3, Vec3)
INX_CDS_VECTOR_ACCESSORS(4, Vec4)

#undef INX_CDS_VECTOR_ACCESSORS

#define INX_CDS_SCALAR_BATCH(Name, CppType, StoreType)                                                                 \
    void ComponentDataStore::Gather##Name(uint32_t classId, uint32_t fieldId, const SlotHandle *handles, size_t count, \
                                          CppType *out) const                                                          \
    {                                                                                                                  \
        const auto &storage = RequireClass(classId);                                                                   \
        const auto &field = RequireField(classId, fieldId, DataType::StoreType);                                       \
        RequireAllAlive(storage, handles, count);                                                                      \
        for (size_t i = 0; i < count; ++i) {                                                                           \
            out[i] = field.At<CppType>(handles[i].index);                                                              \
        }                                                                                                              \
    }                                                                                                                  \
    void ComponentDataStore::Scatter##Name(uint32_t classId, uint32_t fieldId, const SlotHandle *handles,              \
                                           size_t count, const CppType *in)                                            \
    {                                                                                                                  \
        auto &storage = RequireClass(classId);                                                                         \
        auto &field = RequireField(classId, fieldId, DataType::StoreType);                                             \
        RequireAllAlive(storage, handles, count);                                                                      \
        for (size_t i = 0; i < count; ++i) {                                                                           \
            field.At<CppType>(handles[i].index) = in[i];                                                               \
        }                                                                                                              \
    }

INX_CDS_SCALAR_BATCH(Float, double, Float64)
INX_CDS_SCALAR_BATCH(Int, int64_t, Int64)
INX_CDS_SCALAR_BATCH(Bool, uint8_t, Bool)

#undef INX_CDS_SCALAR_BATCH

#define INX_CDS_VECTOR_BATCH(Dim, StoreType)                                                                           \
    void ComponentDataStore::GatherVec##Dim(uint32_t classId, uint32_t fieldId, const SlotHandle *handles,             \
                                            size_t count, float *out) const                                            \
    {                                                                                                                  \
        const auto &storage = RequireClass(classId);                                                                   \
        const auto &field = RequireField(classId, fieldId, DataType::StoreType);                                       \
        RequireAllAlive(storage, handles, count);                                                                      \
        for (size_t i = 0; i < count; ++i) {                                                                           \
            std::copy_n(field.FloatsAt(handles[i].index), Dim, out + i * Dim);                                         \
        }                                                                                                              \
    }                                                                                                                  \
    void ComponentDataStore::ScatterVec##Dim(uint32_t classId, uint32_t fieldId, const SlotHandle *handles,            \
                                             size_t count, const float *in)                                            \
    {                                                                                                                  \
        auto &storage = RequireClass(classId);                                                                         \
        auto &field = RequireField(classId, fieldId, DataType::StoreType);                                             \
        RequireAllAlive(storage, handles, count);                                                                      \
        for (size_t i = 0; i < count; ++i) {                                                                           \
            std::copy_n(in + i * Dim, Dim, field.FloatsAt(handles[i].index));                                          \
        }                                                                                                              \
    }

INX_CDS_VECTOR_BATCH(2, Vec2)
INX_CDS_VECTOR_BATCH(3, Vec3)
INX_CDS_VECTOR_BATCH(4, Vec4)

#undef INX_CDS_VECTOR_BATCH

void ComponentDataStore::Clear()
{
    m_schemaTransaction.reset();
    m_classes.clear();
    m_classNameToId.clear();
    m_freeClassIds.clear();
}

} // namespace infernux
