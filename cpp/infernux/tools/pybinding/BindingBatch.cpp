#include <function/scene/ComponentDataStore.h>
#include <function/scene/GameObject.h>
#include <function/scene/Scene.h>
#include <function/scene/Transform.h>
#include <function/scene/TransformECSStore.h>
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <vector>

namespace py = pybind11;
using namespace infernux;

// ── helpers ──────────────────────────────────────────────────────────────

/// Extract a C-contiguous vector of Transform* from a Python list.
static std::vector<Transform *> ExtractTransforms(const py::list &pyList)
{
    const size_t n = pyList.size();
    std::vector<Transform *> out;
    out.reserve(n);
    for (size_t i = 0; i < n; ++i) {
        out.push_back(pyList[i].cast<Transform *>());
    }
    return out;
}

// ── TransformBatchHandle ─────────────────────────────────────────────────

struct TransformBatchHandle
{
    enum class Mode : uint8_t
    {
        Strict,
        Compact,
    };

    std::vector<TransformECSStore::Handle> handles;
    std::vector<uint64_t> worldIds;
    Mode mode = Mode::Strict;
    mutable std::vector<Transform *> resolved;
    mutable std::vector<size_t> resolvedIndices;
    mutable std::vector<uint8_t> validMask;
    mutable std::vector<float> valueScratch;
    mutable uint64_t resolvedStructuralVersion = UINT64_MAX;

    explicit TransformBatchHandle(std::vector<Transform *> transforms, Mode validationMode = Mode::Strict)
        : mode(validationMode)
    {
        handles.reserve(transforms.size());
        worldIds.reserve(transforms.size());
        resolved.reserve(transforms.size());
        resolvedIndices.reserve(transforms.size());
        validMask.resize(transforms.size());
        valueScratch.reserve(transforms.size() * 4);
        for (const Transform *transform : transforms) {
            handles.push_back(transform->GetECSHandle());
            worldIds.push_back(transform->GetHandle().worldId);
        }
    }

    explicit TransformBatchHandle(const py::list &pyList, Mode validationMode = Mode::Strict)
        : TransformBatchHandle(ExtractTransforms(pyList), validationMode)
    {
    }

    [[nodiscard]] size_t size() const
    {
        return handles.size();
    }

    [[nodiscard]] bool IsCompact() const noexcept
    {
        return mode == Mode::Compact;
    }

    Transform *const *Resolve() const
    {
        auto &store = TransformECSStore::Instance();
        const uint64_t structuralVersion = store.GetStructuralVersion();
        if (resolvedStructuralVersion == structuralVersion)
            return resolved.data();

        resolved.clear();
        resolvedIndices.clear();
        std::fill(validMask.begin(), validMask.end(), uint8_t{0});
        for (size_t i = 0; i < handles.size(); ++i) {
            Transform *owner = store.IsValid(handles[i]) ? store.GetOwner(handles[i]) : nullptr;
            const bool valid = owner && owner->GetHandle().worldId == worldIds[i];
            if (!valid) {
                if (mode == Mode::Strict)
                    throw std::runtime_error("TransformBatchHandle contains a stale transform");
                continue;
            }
            validMask[i] = 1;
            resolved.push_back(owner);
            resolvedIndices.push_back(i);
        }
        resolvedStructuralVersion = structuralVersion;
        return resolved.data();
    }
};

// ── Transform batch read/write ───────────────────────────────────────────

using GatherVec3Fn = void (TransformECSStore::*)(Transform *const *, float *, size_t) const;
using ScatterVec3Fn = void (TransformECSStore::*)(Transform *const *, const float *, size_t);
using GatherQuatFn = void (TransformECSStore::*)(Transform *const *, float *, size_t) const;
using ScatterQuatFn = void (TransformECSStore::*)(Transform *const *, const float *, size_t);

/// batch_read for vec3 properties → numpy (N, 3) float32
static py::array_t<float> BatchReadVec3(const py::list &targets, GatherVec3Fn gatherFn)
{
    auto transforms = ExtractTransforms(targets);
    const size_t n = transforms.size();
    auto result = py::array_t<float>({static_cast<py::ssize_t>(n), py::ssize_t(3)});
    auto buf = result.mutable_unchecked<2>();
    float *outPtr = buf.mutable_data(0, 0);
    Transform *const *tPtr = transforms.data();
    {
        py::gil_scoped_release release;
        (TransformECSStore::Instance().*gatherFn)(tPtr, outPtr, n);
    }
    return result;
}

/// batch_write for vec3 properties from numpy (N, 3) float32
static void BatchWriteVec3(const py::list &targets, py::array data, ScatterVec3Fn scatterFn)
{
    // Accept any numeric dtype and convert to contiguous float32 on demand.
    auto fdata = py::array_t<float, py::array::c_style>::ensure(data);
    if (!fdata) {
        throw py::type_error("data must be convertible to float32 array");
    }
    auto transforms = ExtractTransforms(targets);
    const size_t n = transforms.size();
    auto buf = fdata.unchecked<2>();
    if (static_cast<size_t>(buf.shape(0)) < n) {
        throw py::value_error("data array has fewer rows than targets");
    }
    const float *inPtr = buf.data(0, 0);
    Transform *const *tPtr = transforms.data();
    {
        py::gil_scoped_release release;
        (TransformECSStore::Instance().*scatterFn)(tPtr, inPtr, n);
    }
}

/// batch_read for quaternion properties → numpy (N, 4) float32
static py::array_t<float> BatchReadQuat(const py::list &targets, GatherQuatFn gatherFn)
{
    auto transforms = ExtractTransforms(targets);
    const size_t n = transforms.size();
    auto result = py::array_t<float>({static_cast<py::ssize_t>(n), py::ssize_t(4)});
    auto buf = result.mutable_unchecked<2>();
    float *outPtr = buf.mutable_data(0, 0);
    Transform *const *tPtr = transforms.data();
    {
        py::gil_scoped_release release;
        (TransformECSStore::Instance().*gatherFn)(tPtr, outPtr, n);
    }
    return result;
}

/// batch_write for quaternion properties from numpy (N, 4) float32
static void BatchWriteQuat(const py::list &targets, py::array data, ScatterQuatFn scatterFn)
{
    auto fdata = py::array_t<float, py::array::c_style>::ensure(data);
    if (!fdata) {
        throw py::type_error("data must be convertible to float32 array");
    }
    auto transforms = ExtractTransforms(targets);
    const size_t n = transforms.size();
    auto buf = fdata.unchecked<2>();
    if (static_cast<size_t>(buf.shape(0)) < n) {
        throw py::value_error("data array has fewer rows than targets");
    }
    const float *inPtr = buf.data(0, 0);
    Transform *const *tPtr = transforms.data();
    {
        py::gil_scoped_release release;
        (TransformECSStore::Instance().*scatterFn)(tPtr, inPtr, n);
    }
}

// ── Dispatch table: property name → gather/scatter function ──────────────

struct Vec3BatchOps
{
    GatherVec3Fn gather;
    ScatterVec3Fn scatter;
};

struct QuatBatchOps
{
    GatherQuatFn gather;
    ScatterQuatFn scatter;
};

static const std::unordered_map<std::string, Vec3BatchOps> kTransformVec3Ops = {
    {"local_position", {&TransformECSStore::GatherLocalPositions, &TransformECSStore::ScatterLocalPositions}},
    {"local_scale", {&TransformECSStore::GatherLocalScales, &TransformECSStore::ScatterLocalScales}},
    {"local_euler_angles", {&TransformECSStore::GatherLocalEulerAngles, &TransformECSStore::ScatterLocalEulerAngles}},
    {"position", {&TransformECSStore::GatherWorldPositions, &TransformECSStore::ScatterWorldPositions}},
    {"euler_angles", {&TransformECSStore::GatherWorldEulerAngles, &TransformECSStore::ScatterWorldEulerAngles}},
};

static const std::unordered_map<std::string, QuatBatchOps> kTransformQuatOps = {
    {"local_rotation", {&TransformECSStore::GatherLocalRotations, &TransformECSStore::ScatterLocalRotations}},
    {"rotation", {&TransformECSStore::GatherWorldRotations, &TransformECSStore::ScatterWorldRotations}},
};

// ── Python-facing free functions ─────────────────────────────────────────

static py::object TransformBatchRead(const py::list &targets, const std::string &prop)
{
    {
        auto it = kTransformVec3Ops.find(prop);
        if (it != kTransformVec3Ops.end()) {
            return BatchReadVec3(targets, it->second.gather);
        }
    }
    {
        auto it = kTransformQuatOps.find(prop);
        if (it != kTransformQuatOps.end()) {
            return BatchReadQuat(targets, it->second.gather);
        }
    }
    throw py::value_error("Unknown Transform property: '" + prop + "'");
}

static void TransformBatchWrite(const py::list &targets, py::array data, const std::string &prop)
{
    {
        auto it = kTransformVec3Ops.find(prop);
        if (it != kTransformVec3Ops.end()) {
            BatchWriteVec3(targets, data, it->second.scatter);
            return;
        }
    }
    {
        auto it = kTransformQuatOps.find(prop);
        if (it != kTransformQuatOps.end()) {
            BatchWriteQuat(targets, data, it->second.scatter);
            return;
        }
    }
    throw py::value_error("Unknown Transform property: '" + prop + "'");
}

// ── Handle-based batch read/write (avoids repeated ExtractTransforms) ──

static py::array_t<bool> BuildBatchMask(const TransformBatchHandle &handle)
{
    auto mask = py::array_t<bool>(static_cast<py::ssize_t>(handle.validMask.size()));
    auto output = mask.mutable_unchecked<1>();
    for (size_t i = 0; i < handle.validMask.size(); ++i)
        output(static_cast<py::ssize_t>(i)) = handle.validMask[i] != 0;
    return mask;
}

static py::object HandleBatchRead(const TransformBatchHandle &handle, const std::string &prop)
{
    Transform *const *tPtr = handle.Resolve();
    const size_t n = handle.resolved.size();
    {
        auto it = kTransformVec3Ops.find(prop);
        if (it != kTransformVec3Ops.end()) {
            auto result = py::array_t<float>({static_cast<py::ssize_t>(n), py::ssize_t(3)});
            float *outPtr = result.mutable_unchecked<2>().mutable_data(0, 0);
            {
                py::gil_scoped_release release;
                (TransformECSStore::Instance().*(it->second.gather))(tPtr, outPtr, n);
            }
            if (handle.IsCompact())
                return py::make_tuple(std::move(result), BuildBatchMask(handle));
            return std::move(result);
        }
    }
    {
        auto it = kTransformQuatOps.find(prop);
        if (it != kTransformQuatOps.end()) {
            auto result = py::array_t<float>({static_cast<py::ssize_t>(n), py::ssize_t(4)});
            float *outPtr = result.mutable_unchecked<2>().mutable_data(0, 0);
            {
                py::gil_scoped_release release;
                (TransformECSStore::Instance().*(it->second.gather))(tPtr, outPtr, n);
            }
            if (handle.IsCompact())
                return py::make_tuple(std::move(result), BuildBatchMask(handle));
            return std::move(result);
        }
    }
    throw py::value_error("Unknown Transform property: '" + prop + "'");
}

static py::object HandleBatchWrite(const TransformBatchHandle &handle, py::array data, const std::string &prop)
{
    auto fdata = py::array_t<float, py::array::c_style>::ensure(data);
    if (!fdata) {
        throw py::type_error("data must be convertible to float32 array");
    }
    Transform *const *tPtr = handle.Resolve();
    const size_t n = handle.resolved.size();
    const size_t inputRows = handle.IsCompact() ? handle.size() : n;
    {
        auto it = kTransformVec3Ops.find(prop);
        if (it != kTransformVec3Ops.end()) {
            auto buf = fdata.unchecked<2>();
            if (static_cast<size_t>(buf.shape(0)) < inputRows) {
                throw py::value_error("data array has fewer rows than targets");
            }
            const float *inPtr = buf.data(0, 0);
            if (handle.IsCompact()) {
                handle.valueScratch.resize(n * 3);
                for (size_t i = 0; i < n; ++i) {
                    const float *source = inPtr + handle.resolvedIndices[i] * 3;
                    std::copy_n(source, 3, handle.valueScratch.data() + i * 3);
                }
                inPtr = handle.valueScratch.data();
            }
            {
                py::gil_scoped_release release;
                (TransformECSStore::Instance().*(it->second.scatter))(tPtr, inPtr, n);
            }
            if (handle.IsCompact())
                return BuildBatchMask(handle);
            return py::none();
        }
    }
    {
        auto it = kTransformQuatOps.find(prop);
        if (it != kTransformQuatOps.end()) {
            auto buf = fdata.unchecked<2>();
            if (static_cast<size_t>(buf.shape(0)) < inputRows) {
                throw py::value_error("data array has fewer rows than targets");
            }
            const float *inPtr = buf.data(0, 0);
            if (handle.IsCompact()) {
                handle.valueScratch.resize(n * 4);
                for (size_t i = 0; i < n; ++i) {
                    const float *source = inPtr + handle.resolvedIndices[i] * 4;
                    std::copy_n(source, 4, handle.valueScratch.data() + i * 4);
                }
                inPtr = handle.valueScratch.data();
            }
            {
                py::gil_scoped_release release;
                (TransformECSStore::Instance().*(it->second.scatter))(tPtr, inPtr, n);
            }
            if (handle.IsCompact())
                return BuildBatchMask(handle);
            return py::none();
        }
    }
    throw py::value_error("Unknown Transform property: '" + prop + "'");
}

// ── ComponentDataStore bindings ───────────────────────────────────────────

static ComponentDataStore::DataType CDS_ParseDataType(int typeCode)
{
    if (typeCode < static_cast<int>(ComponentDataStore::DataType::Float64) ||
        typeCode > static_cast<int>(ComponentDataStore::DataType::Vec4)) {
        throw py::value_error("Invalid ComponentDataStore type code");
    }
    return static_cast<ComponentDataStore::DataType>(typeCode);
}

static uint32_t CDS_RegisterClass(const std::string &name)
{
    return ComponentDataStore::Instance().RegisterClass(name);
}

static uint32_t CDS_RegisterField(uint32_t classId, const std::string &name, int typeCode)
{
    return ComponentDataStore::Instance().RegisterField(classId, name, CDS_ParseDataType(typeCode));
}

static py::object CDS_OptionalPreparedClass(ComponentDataStore::PreparedClassId preparedClassId)
{
    if (preparedClassId == ComponentDataStore::InvalidPreparedClassId)
        return py::none();
    return py::cast(preparedClassId);
}

static py::object CDS_OptionalPublishedClass(uint32_t classId)
{
    if (classId == UINT32_MAX)
        return py::none();
    return py::cast(classId);
}

static ComponentDataStore::SlotHandle CDS_ParseHandle(py::handle value)
{
    if (!py::isinstance<py::tuple>(value) && !py::isinstance<py::list>(value))
        throw py::type_error("CDS slot handle must be a two-item (index, generation) sequence");
    const py::sequence sequence = py::reinterpret_borrow<py::sequence>(value);
    if (sequence.size() != 2)
        throw py::value_error("CDS slot handle must contain exactly index and generation");
    return {sequence[0].cast<uint32_t>(), sequence[1].cast<uint32_t>()};
}

static py::tuple CDS_AllocSlot(uint32_t classId)
{
    const auto handle = ComponentDataStore::Instance().AllocateSlot(classId);
    return py::make_tuple(handle.index, handle.generation);
}

static void CDS_FreeSlot(uint32_t classId, py::handle handle)
{
    ComponentDataStore::Instance().ReleaseSlot(classId, CDS_ParseHandle(handle));
}

static bool CDS_IsAlive(uint32_t classId, py::handle handle)
{
    return ComponentDataStore::Instance().IsAlive(classId, CDS_ParseHandle(handle));
}

static void CDS_ReserveClass(uint32_t classId, size_t capacity)
{
    ComponentDataStore::Instance().ReserveClass(classId, capacity);
}

static py::tuple CDS_SchemaAllocSlot(ComponentDataStore::SchemaTransactionId transactionId,
                                     ComponentDataStore::PreparedClassId preparedClassId)
{
    const auto handle = ComponentDataStore::Instance().AllocatePreparedSlot(transactionId, preparedClassId);
    return py::make_tuple(handle.index, handle.generation);
}

static py::object CDS_SchemaGet(ComponentDataStore::SchemaTransactionId transactionId,
                                ComponentDataStore::PreparedClassId preparedClassId, uint32_t fieldId, py::handle slot,
                                int typeCode)
{
    auto &store = ComponentDataStore::Instance();
    const auto type = CDS_ParseDataType(typeCode);
    const auto handle = CDS_ParseHandle(slot);
    switch (type) {
    case ComponentDataStore::DataType::Float64:
        return py::cast(store.GetPreparedFloat(transactionId, preparedClassId, fieldId, handle));
    case ComponentDataStore::DataType::Int64:
        return py::cast(store.GetPreparedInt(transactionId, preparedClassId, fieldId, handle));
    case ComponentDataStore::DataType::Bool:
        return py::cast(store.GetPreparedBool(transactionId, preparedClassId, fieldId, handle));
    case ComponentDataStore::DataType::Vec2: {
        float value[2];
        store.GetPreparedVec2(transactionId, preparedClassId, fieldId, handle, value);
        return py::make_tuple(value[0], value[1]);
    }
    case ComponentDataStore::DataType::Vec3: {
        float value[3];
        store.GetPreparedVec3(transactionId, preparedClassId, fieldId, handle, value);
        return py::make_tuple(value[0], value[1], value[2]);
    }
    case ComponentDataStore::DataType::Vec4: {
        float value[4];
        store.GetPreparedVec4(transactionId, preparedClassId, fieldId, handle, value);
        return py::make_tuple(value[0], value[1], value[2], value[3]);
    }
    }
    throw py::value_error("Invalid ComponentDataStore type code");
}

static void CDS_SchemaSet(ComponentDataStore::SchemaTransactionId transactionId,
                          ComponentDataStore::PreparedClassId preparedClassId, uint32_t fieldId, py::handle slot,
                          int typeCode, py::object value)
{
    auto &store = ComponentDataStore::Instance();
    const auto type = CDS_ParseDataType(typeCode);
    const auto handle = CDS_ParseHandle(slot);
    switch (type) {
    case ComponentDataStore::DataType::Float64:
        store.SetPreparedFloat(transactionId, preparedClassId, fieldId, handle, value.cast<double>());
        return;
    case ComponentDataStore::DataType::Int64:
        store.SetPreparedInt(transactionId, preparedClassId, fieldId, handle, value.cast<int64_t>());
        return;
    case ComponentDataStore::DataType::Bool:
        store.SetPreparedBool(transactionId, preparedClassId, fieldId, handle, value.cast<bool>());
        return;
    case ComponentDataStore::DataType::Vec2: {
        float data[2];
        if (py::hasattr(value, "x")) {
            data[0] = value.attr("x").cast<float>();
            data[1] = value.attr("y").cast<float>();
        } else {
            const auto sequence = value.cast<py::sequence>();
            if (sequence.size() != 2)
                throw py::value_error("CDS Vec2 value must contain two elements");
            data[0] = sequence[0].cast<float>();
            data[1] = sequence[1].cast<float>();
        }
        store.SetPreparedVec2(transactionId, preparedClassId, fieldId, handle, data);
        return;
    }
    case ComponentDataStore::DataType::Vec3: {
        float data[3];
        if (py::hasattr(value, "x")) {
            data[0] = value.attr("x").cast<float>();
            data[1] = value.attr("y").cast<float>();
            data[2] = value.attr("z").cast<float>();
        } else {
            const auto sequence = value.cast<py::sequence>();
            if (sequence.size() != 3)
                throw py::value_error("CDS Vec3 value must contain three elements");
            data[0] = sequence[0].cast<float>();
            data[1] = sequence[1].cast<float>();
            data[2] = sequence[2].cast<float>();
        }
        store.SetPreparedVec3(transactionId, preparedClassId, fieldId, handle, data);
        return;
    }
    case ComponentDataStore::DataType::Vec4: {
        float data[4];
        if (py::hasattr(value, "x")) {
            data[0] = value.attr("x").cast<float>();
            data[1] = value.attr("y").cast<float>();
            data[2] = value.attr("z").cast<float>();
            data[3] = value.attr("w").cast<float>();
        } else {
            const auto sequence = value.cast<py::sequence>();
            if (sequence.size() != 4)
                throw py::value_error("CDS Vec4 value must contain four elements");
            data[0] = sequence[0].cast<float>();
            data[1] = sequence[1].cast<float>();
            data[2] = sequence[2].cast<float>();
            data[3] = sequence[3].cast<float>();
        }
        store.SetPreparedVec4(transactionId, preparedClassId, fieldId, handle, data);
        return;
    }
    }
    throw py::value_error("Invalid ComponentDataStore type code");
}

static py::tuple CDS_SchemaMigrateSlot(ComponentDataStore::SchemaTransactionId transactionId,
                                       ComponentDataStore::PreparedClassId preparedClassId, uint32_t sourceClassId,
                                       py::handle sourceSlot,
                                       const std::vector<std::pair<uint32_t, uint32_t>> &fieldMap)
{
    std::vector<ComponentDataStore::FieldMigration> migrations;
    migrations.reserve(fieldMap.size());
    for (const auto &[sourceFieldId, destinationFieldId] : fieldMap)
        migrations.push_back({sourceFieldId, destinationFieldId});
    const auto handle = ComponentDataStore::Instance().MigrateSlotToPrepared(transactionId, preparedClassId,
                                                                             sourceClassId, CDS_ParseHandle(sourceSlot),
                                                                             migrations.data(), migrations.size());
    return py::make_tuple(handle.index, handle.generation);
}

static std::vector<ComponentDataStore::SlotHandle>
CDS_ParseHandles(const py::array_t<uint32_t, py::array::c_style> &handles)
{
    if (handles.ndim() != 2 || handles.shape(1) != 2)
        throw py::value_error("CDS handles must have shape (N, 2)");
    const auto input = handles.unchecked<2>();
    std::vector<ComponentDataStore::SlotHandle> parsed(static_cast<size_t>(input.shape(0)));
    for (py::ssize_t i = 0; i < input.shape(0); ++i)
        parsed[static_cast<size_t>(i)] = {input(i, 0), input(i, 1)};
    return parsed;
}

// ── per-element accessors ────────────────────────────────────────────────

static py::object CDS_Get(uint32_t classId, uint32_t fieldId, py::handle slot, int typeCode)
{
    auto &store = ComponentDataStore::Instance();
    auto type = CDS_ParseDataType(typeCode);
    const auto handle = CDS_ParseHandle(slot);
    switch (type) {
    case ComponentDataStore::DataType::Float64:
        return py::cast(store.GetFloat(classId, fieldId, handle));
    case ComponentDataStore::DataType::Int64:
        return py::cast(store.GetInt(classId, fieldId, handle));
    case ComponentDataStore::DataType::Bool:
        return py::cast(store.GetBool(classId, fieldId, handle));
    case ComponentDataStore::DataType::Vec2: {
        float v[2];
        store.GetVec2(classId, fieldId, handle, v);
        return py::make_tuple(v[0], v[1]);
    }
    case ComponentDataStore::DataType::Vec3: {
        float v[3];
        store.GetVec3(classId, fieldId, handle, v);
        return py::make_tuple(v[0], v[1], v[2]);
    }
    case ComponentDataStore::DataType::Vec4: {
        float v[4];
        store.GetVec4(classId, fieldId, handle, v);
        return py::make_tuple(v[0], v[1], v[2], v[3]);
    }
    }
    throw py::value_error("Invalid ComponentDataStore type code");
}

static void CDS_Set(uint32_t classId, uint32_t fieldId, py::handle slot, int typeCode, py::object value)
{
    auto &store = ComponentDataStore::Instance();
    auto type = CDS_ParseDataType(typeCode);
    const auto handle = CDS_ParseHandle(slot);
    switch (type) {
    case ComponentDataStore::DataType::Float64:
        store.SetFloat(classId, fieldId, handle, value.cast<double>());
        break;
    case ComponentDataStore::DataType::Int64:
        store.SetInt(classId, fieldId, handle, value.cast<int64_t>());
        break;
    case ComponentDataStore::DataType::Bool:
        store.SetBool(classId, fieldId, handle, value.cast<bool>());
        break;
    case ComponentDataStore::DataType::Vec2: {
        // Accept anything with .x, .y (Vector2, tuple)
        float v[2];
        if (py::hasattr(value, "x")) {
            v[0] = value.attr("x").cast<float>();
            v[1] = value.attr("y").cast<float>();
        } else {
            auto t = value.cast<py::tuple>();
            v[0] = t[0].cast<float>();
            v[1] = t[1].cast<float>();
        }
        store.SetVec2(classId, fieldId, handle, v);
        break;
    }
    case ComponentDataStore::DataType::Vec3: {
        float v[3];
        if (py::hasattr(value, "x")) {
            v[0] = value.attr("x").cast<float>();
            v[1] = value.attr("y").cast<float>();
            v[2] = value.attr("z").cast<float>();
        } else {
            auto t = value.cast<py::tuple>();
            v[0] = t[0].cast<float>();
            v[1] = t[1].cast<float>();
            v[2] = t[2].cast<float>();
        }
        store.SetVec3(classId, fieldId, handle, v);
        break;
    }
    case ComponentDataStore::DataType::Vec4: {
        float v[4];
        if (py::hasattr(value, "x")) {
            v[0] = value.attr("x").cast<float>();
            v[1] = value.attr("y").cast<float>();
            v[2] = value.attr("z").cast<float>();
            v[3] = value.attr("w").cast<float>();
        } else {
            auto t = value.cast<py::tuple>();
            v[0] = t[0].cast<float>();
            v[1] = t[1].cast<float>();
            v[2] = t[2].cast<float>();
            v[3] = t[3].cast<float>();
        }
        store.SetVec4(classId, fieldId, handle, v);
        break;
    }
    }
}

// ── batch gather/scatter for ComponentDataStore ──────────────────────────

static py::array CDS_BatchGather(uint32_t classId, uint32_t fieldId, int typeCode,
                                 py::array_t<uint32_t, py::array::c_style> slots)
{
    auto &store = ComponentDataStore::Instance();
    auto type = CDS_ParseDataType(typeCode);
    const auto handles = CDS_ParseHandles(slots);
    const size_t n = handles.size();

    switch (type) {
    case ComponentDataStore::DataType::Float64: {
        auto out = py::array_t<double>({static_cast<py::ssize_t>(n)});
        store.GatherFloat(classId, fieldId, handles.data(), n, out.mutable_data());
        return out;
    }
    case ComponentDataStore::DataType::Int64: {
        auto out = py::array_t<int64_t>({static_cast<py::ssize_t>(n)});
        store.GatherInt(classId, fieldId, handles.data(), n, out.mutable_data());
        return out;
    }
    case ComponentDataStore::DataType::Bool: {
        auto out = py::array_t<uint8_t>({static_cast<py::ssize_t>(n)});
        store.GatherBool(classId, fieldId, handles.data(), n, out.mutable_data());
        return out;
    }
    case ComponentDataStore::DataType::Vec2: {
        auto out = py::array_t<float>({static_cast<py::ssize_t>(n), py::ssize_t(2)});
        store.GatherVec2(classId, fieldId, handles.data(), n, out.mutable_data());
        return out;
    }
    case ComponentDataStore::DataType::Vec3: {
        auto out = py::array_t<float>({static_cast<py::ssize_t>(n), py::ssize_t(3)});
        store.GatherVec3(classId, fieldId, handles.data(), n, out.mutable_data());
        return out;
    }
    case ComponentDataStore::DataType::Vec4: {
        auto out = py::array_t<float>({static_cast<py::ssize_t>(n), py::ssize_t(4)});
        store.GatherVec4(classId, fieldId, handles.data(), n, out.mutable_data());
        return out;
    }
    }
    throw py::value_error("Invalid ComponentDataStore type code");
}

static void CDS_BatchScatter(uint32_t classId, uint32_t fieldId, int typeCode,
                             py::array_t<uint32_t, py::array::c_style> slots, py::array data)
{
    auto &store = ComponentDataStore::Instance();
    auto type = CDS_ParseDataType(typeCode);
    const auto handles = CDS_ParseHandles(slots);
    const size_t n = handles.size();

    switch (type) {
    case ComponentDataStore::DataType::Float64: {
        auto d = py::array_t<double, py::array::c_style>::ensure(data);
        if (!d || d.ndim() != 1 || static_cast<size_t>(d.shape(0)) != n)
            throw py::value_error("CDS Float64 scatter data must have shape (N,)");
        store.ScatterFloat(classId, fieldId, handles.data(), n, d.data());
        break;
    }
    case ComponentDataStore::DataType::Int64: {
        auto d = py::array_t<int64_t, py::array::c_style>::ensure(data);
        if (!d || d.ndim() != 1 || static_cast<size_t>(d.shape(0)) != n)
            throw py::value_error("CDS Int64 scatter data must have shape (N,)");
        store.ScatterInt(classId, fieldId, handles.data(), n, d.data());
        break;
    }
    case ComponentDataStore::DataType::Bool: {
        auto d = py::array_t<uint8_t, py::array::c_style>::ensure(data);
        if (!d || d.ndim() != 1 || static_cast<size_t>(d.shape(0)) != n)
            throw py::value_error("CDS Bool scatter data must have shape (N,)");
        store.ScatterBool(classId, fieldId, handles.data(), n, d.data());
        break;
    }
    case ComponentDataStore::DataType::Vec2: {
        auto d = py::array_t<float, py::array::c_style>::ensure(data);
        if (!d || d.ndim() != 2 || static_cast<size_t>(d.shape(0)) != n || d.shape(1) != 2)
            throw py::value_error("CDS Vec2 scatter data must have shape (N, 2)");
        store.ScatterVec2(classId, fieldId, handles.data(), n, d.data());
        break;
    }
    case ComponentDataStore::DataType::Vec3: {
        auto d = py::array_t<float, py::array::c_style>::ensure(data);
        if (!d || d.ndim() != 2 || static_cast<size_t>(d.shape(0)) != n || d.shape(1) != 3)
            throw py::value_error("CDS Vec3 scatter data must have shape (N, 3)");
        store.ScatterVec3(classId, fieldId, handles.data(), n, d.data());
        break;
    }
    case ComponentDataStore::DataType::Vec4: {
        auto d = py::array_t<float, py::array::c_style>::ensure(data);
        if (!d || d.ndim() != 2 || static_cast<size_t>(d.shape(0)) != n || d.shape(1) != 4)
            throw py::value_error("CDS Vec4 scatter data must have shape (N, 4)");
        store.ScatterVec4(classId, fieldId, handles.data(), n, d.data());
        break;
    }
    }
}

// ── Module registration ──────────────────────────────────────────────────

namespace infernux
{

void RegisterBatchBindings(py::module_ &m)
{
    m.def("_transform_batch_read", &TransformBatchRead, py::arg("targets"), py::arg("property"),
          "Read a Transform property from all targets into a numpy array.\n"
          "Supported properties: 'position', 'local_position', 'local_scale',\n"
          "'euler_angles', 'local_euler_angles', 'rotation', 'local_rotation'.");

    m.def("_transform_batch_write", &TransformBatchWrite, py::arg("targets"), py::arg("data"), py::arg("property"),
          "Write a numpy array back to a Transform property on all targets.\n"
          "data.shape[0] must be >= len(targets).");

    py::enum_<TransformBatchHandle::Mode>(m, "TransformBatchMode")
        .value("STRICT", TransformBatchHandle::Mode::Strict)
        .value("COMPACT", TransformBatchHandle::Mode::Compact);

    py::class_<TransformBatchHandle>(m, "TransformBatchHandle")
        .def(py::init<const py::list &, TransformBatchHandle::Mode>(), py::arg("targets"),
             py::arg("mode") = TransformBatchHandle::Mode::Strict)
        .def("__len__", &TransformBatchHandle::size)
        .def_property_readonly("is_compact", &TransformBatchHandle::IsCompact);

    m.def(
        "_create_scene_transform_batch_handle",
        [](Scene &scene, const std::string &namePrefix, TransformBatchHandle::Mode mode) {
            std::vector<Transform *> transforms;
            const auto objects = scene.GetAllObjects();
            transforms.reserve(objects.size());
            for (GameObject *object : objects) {
                if (!object || (!namePrefix.empty() && object->GetName().rfind(namePrefix, 0) != 0))
                    continue;
                transforms.push_back(object->GetTransform());
            }
            return TransformBatchHandle(std::move(transforms), mode);
        },
        py::arg("scene"), py::arg("name_prefix") = "", py::arg("mode") = TransformBatchHandle::Mode::Strict,
        "Build a Transform batch in native code without materializing GameObjects in Python.");

    m.def("_transform_batch_read", &HandleBatchRead, py::arg("handle"), py::arg("property"),
          "Read through generational handles; compact mode returns (values, valid_mask).");

    m.def("_transform_batch_write", &HandleBatchWrite, py::arg("handle"), py::arg("data"), py::arg("property"),
          "Write through generational handles; compact mode returns valid_mask.");

    // ── ComponentDataStore ──
    m.def("_cds_register_class", &CDS_RegisterClass, py::arg("name"));
    m.def("_cds_register_field", &CDS_RegisterField, py::arg("class_id"), py::arg("name"), py::arg("type_code"));

    m.def("_cds_schema_begin", [] { return ComponentDataStore::Instance().BeginSchemaTransaction(); });
    m.def(
        "_cds_schema_prepare_class",
        [](ComponentDataStore::SchemaTransactionId transactionId, const std::string &name) {
            return ComponentDataStore::Instance().PrepareClass(transactionId, name);
        },
        py::arg("transaction_id"), py::arg("name"));
    m.def(
        "_cds_schema_prepare_field",
        [](ComponentDataStore::SchemaTransactionId transactionId, ComponentDataStore::PreparedClassId preparedClassId,
           const std::string &name, int typeCode) {
            return ComponentDataStore::Instance().PrepareField(transactionId, preparedClassId, name,
                                                               CDS_ParseDataType(typeCode));
        },
        py::arg("transaction_id"), py::arg("candidate_id"), py::arg("name"), py::arg("type_code"));
    m.def(
        "_cds_schema_has_class",
        [](ComponentDataStore::SchemaTransactionId transactionId, ComponentDataStore::PreparedClassId preparedClassId) {
            return ComponentDataStore::Instance().HasPreparedClass(transactionId, preparedClassId);
        },
        py::arg("transaction_id"), py::arg("candidate_id"));
    m.def(
        "_cds_schema_find_class",
        [](ComponentDataStore::SchemaTransactionId transactionId, const std::string &name) {
            return CDS_OptionalPreparedClass(ComponentDataStore::Instance().FindPreparedClass(transactionId, name));
        },
        py::arg("transaction_id"), py::arg("name"));
    m.def(
        "_cds_schema_get_field_id",
        [](ComponentDataStore::SchemaTransactionId transactionId, ComponentDataStore::PreparedClassId preparedClassId,
           const std::string &name) {
            const uint32_t fieldId =
                ComponentDataStore::Instance().GetPreparedFieldId(transactionId, preparedClassId, name);
            return CDS_OptionalPublishedClass(fieldId);
        },
        py::arg("transaction_id"), py::arg("candidate_id"), py::arg("name"));
    m.def(
        "_cds_schema_discard_class",
        [](ComponentDataStore::SchemaTransactionId transactionId, ComponentDataStore::PreparedClassId preparedClassId) {
            return ComponentDataStore::Instance().DiscardPreparedClass(transactionId, preparedClassId);
        },
        py::arg("transaction_id"), py::arg("candidate_id"));
    m.def(
        "_cds_schema_reserve",
        [](ComponentDataStore::SchemaTransactionId transactionId, ComponentDataStore::PreparedClassId preparedClassId,
           size_t capacity) {
            ComponentDataStore::Instance().ReservePreparedClass(transactionId, preparedClassId, capacity);
        },
        py::arg("transaction_id"), py::arg("candidate_id"), py::arg("capacity"));
    m.def("_cds_schema_alloc", &CDS_SchemaAllocSlot, py::arg("transaction_id"), py::arg("candidate_id"));
    m.def(
        "_cds_schema_free",
        [](ComponentDataStore::SchemaTransactionId transactionId, ComponentDataStore::PreparedClassId preparedClassId,
           py::handle slot) {
            ComponentDataStore::Instance().ReleasePreparedSlot(transactionId, preparedClassId, CDS_ParseHandle(slot));
        },
        py::arg("transaction_id"), py::arg("candidate_id"), py::arg("slot"));
    m.def(
        "_cds_schema_is_alive",
        [](ComponentDataStore::SchemaTransactionId transactionId, ComponentDataStore::PreparedClassId preparedClassId,
           py::handle slot) {
            return ComponentDataStore::Instance().IsPreparedSlotAlive(transactionId, preparedClassId,
                                                                      CDS_ParseHandle(slot));
        },
        py::arg("transaction_id"), py::arg("candidate_id"), py::arg("slot"));
    m.def("_cds_schema_get", &CDS_SchemaGet, py::arg("transaction_id"), py::arg("candidate_id"), py::arg("field_id"),
          py::arg("slot"), py::arg("type_code"));
    m.def("_cds_schema_set", &CDS_SchemaSet, py::arg("transaction_id"), py::arg("candidate_id"), py::arg("field_id"),
          py::arg("slot"), py::arg("type_code"), py::arg("value"));
    m.def("_cds_schema_migrate_slot", &CDS_SchemaMigrateSlot, py::arg("transaction_id"), py::arg("candidate_id"),
          py::arg("source_class_id"), py::arg("source_slot"), py::arg("field_map"));
    m.def(
        "_cds_schema_seal",
        [](ComponentDataStore::SchemaTransactionId transactionId) {
            return ComponentDataStore::Instance().SealSchemaTransaction(transactionId);
        },
        py::arg("transaction_id"));
    m.def(
        "_cds_schema_final_class_id",
        [](ComponentDataStore::SchemaTransactionId transactionId, ComponentDataStore::PreparedClassId preparedClassId) {
            return CDS_OptionalPublishedClass(
                ComponentDataStore::Instance().GetPreparedFinalClassId(transactionId, preparedClassId));
        },
        py::arg("transaction_id"), py::arg("candidate_id"));
    m.def(
        "_cds_schema_commit",
        [](ComponentDataStore::SchemaTransactionId transactionId) {
            return ComponentDataStore::Instance().CommitSchemaTransaction(transactionId);
        },
        py::arg("transaction_id"));
    m.def(
        "_cds_schema_finalize",
        [](ComponentDataStore::SchemaTransactionId transactionId) {
            return ComponentDataStore::Instance().FinalizeSchemaTransaction(transactionId);
        },
        py::arg("transaction_id"));
    m.def(
        "_cds_schema_rollback",
        [](ComponentDataStore::SchemaTransactionId transactionId) {
            return ComponentDataStore::Instance().RollbackSchemaTransaction(transactionId);
        },
        py::arg("transaction_id"));
    m.def(
        "_cds_schema_active",
        [](ComponentDataStore::SchemaTransactionId transactionId) {
            return ComponentDataStore::Instance().IsSchemaTransactionActive(transactionId);
        },
        py::arg("transaction_id"));

    m.def("_cds_alloc", &CDS_AllocSlot, py::arg("class_id"));
    m.def("_cds_free", &CDS_FreeSlot, py::arg("class_id"), py::arg("slot"));
    m.def("_cds_is_alive", &CDS_IsAlive, py::arg("class_id"), py::arg("slot"));
    m.def("_cds_reserve", &CDS_ReserveClass, py::arg("class_id"), py::arg("capacity"));
    m.def(
        "_cds_capacity", [](uint32_t classId) { return ComponentDataStore::Instance().GetClassCapacity(classId); },
        py::arg("class_id"));
    m.def(
        "_cds_alive_count", [](uint32_t classId) { return ComponentDataStore::Instance().GetClassAliveCount(classId); },
        py::arg("class_id"));
    m.def("_cds_get", &CDS_Get, py::arg("class_id"), py::arg("field_id"), py::arg("slot"), py::arg("type_code"));
    m.def("_cds_set", &CDS_Set, py::arg("class_id"), py::arg("field_id"), py::arg("slot"), py::arg("type_code"),
          py::arg("value"));
    m.def("_cds_batch_gather", &CDS_BatchGather, py::arg("class_id"), py::arg("field_id"), py::arg("type_code"),
          py::arg("slots"));
    m.def("_cds_batch_scatter", &CDS_BatchScatter, py::arg("class_id"), py::arg("field_id"), py::arg("type_code"),
          py::arg("slots"), py::arg("data"));
}

} // namespace infernux
