#include <function/scene/ComponentDataStore.h>

#include <cassert>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <vector>

using infernux::ComponentDataStore;

template <typename Fn> void ExpectFailure(Fn &&fn)
{
    bool failed = false;
    try {
        fn();
    } catch (const std::exception &) {
        failed = true;
    }
    assert(failed);
}

int main()
{
    auto &store = ComponentDataStore::Instance();
    store.Clear();

    const uint32_t classId = store.RegisterClass("tests:Motion");
    const uint32_t speedField = store.RegisterField(classId, "speed", ComponentDataStore::DataType::Float64);
    const uint32_t countField = store.RegisterField(classId, "count", ComponentDataStore::DataType::Int64);
    const uint32_t positionField = store.RegisterField(classId, "position", ComponentDataStore::DataType::Vec3);
    assert(store.RegisterField(classId, "speed", ComponentDataStore::DataType::Float64) == speedField);
    ExpectFailure([&] { store.RegisterField(classId, "speed", ComponentDataStore::DataType::Int64); });

    store.ReserveClass(classId, 128);
    std::vector<ComponentDataStore::SlotHandle> handles;
    handles.reserve(100);
    for (int64_t i = 0; i < 100; ++i) {
        const auto handle = store.AllocateSlot(classId);
        handles.push_back(handle);
        store.SetFloat(classId, speedField, handle, static_cast<double>(i) + 0.5);
        store.SetInt(classId, countField, handle, i);
        const float position[3] = {static_cast<float>(i), 2.0F, 3.0F};
        store.SetVec3(classId, positionField, handle, position);
    }
    assert(store.GetFloat(classId, speedField, handles[77]) == 77.5);
    assert(store.GetInt(classId, countField, handles[77]) == 77);

    const auto stale = handles[12];
    store.ReleaseSlot(classId, stale);
    assert(!store.IsAlive(classId, stale));
    const auto replacement = store.AllocateSlot(classId);
    assert(replacement.index == stale.index);
    assert(replacement.generation != stale.generation);
    ExpectFailure([&] { store.GetFloat(classId, speedField, stale); });
    ExpectFailure([&] { store.ReleaseSlot(classId, stale); });
    ExpectFailure([&] { store.GetInt(classId, speedField, replacement); });

    const ComponentDataStore::SlotHandle batchHandles[] = {handles[0], replacement, handles[99]};
    const double input[] = {10.0, 20.0, 30.0};
    double output[3] = {};
    store.ScatterFloat(classId, speedField, batchHandles, 3, input);
    store.GatherFloat(classId, speedField, batchHandles, 3, output);
    assert(output[0] == 10.0 && output[1] == 20.0 && output[2] == 30.0);

    const ComponentDataStore::SlotHandle invalidBatch[] = {handles[0], stale};
    ExpectFailure([&] { store.GatherFloat(classId, speedField, invalidBatch, 2, output); });
    const double beforeFailedScatter = store.GetFloat(classId, speedField, handles[0]);
    const double invalidInput[] = {111.0, 222.0};
    ExpectFailure([&] { store.ScatterFloat(classId, speedField, invalidBatch, 2, invalidInput); });
    assert(store.GetFloat(classId, speedField, handles[0]) == beforeFailedScatter);

    // Prepared schemas, fields and slots remain private through seal. Rolling
    // back a sealed transaction must leave the published layout untouched.
    const size_t publishedBeforeRollback = store.GetPublishedClassCount();
    const auto rollbackTransaction = store.BeginSchemaTransaction();
    const auto rollbackMotion = store.PrepareClass(rollbackTransaction, "tests:MotionRollback");
    const auto rollbackCount =
        store.PrepareField(rollbackTransaction, rollbackMotion, "count", ComponentDataStore::DataType::Float64);
    const auto rollbackSpeed =
        store.PrepareField(rollbackTransaction, rollbackMotion, "speed", ComponentDataStore::DataType::Float64);
    const auto rollbackEnabled =
        store.PrepareField(rollbackTransaction, rollbackMotion, "enabled", ComponentDataStore::DataType::Bool);
    assert(store.FindPreparedClass(rollbackTransaction, "tests:MotionRollback") == rollbackMotion);
    assert(store.GetPreparedFieldId(rollbackTransaction, rollbackMotion, "speed") == rollbackSpeed);
    assert(store.GetClassId("tests:MotionRollback") == UINT32_MAX);
    ExpectFailure([&] { store.RegisterClass("tests:BlockedDuringTransaction"); });

    const ComponentDataStore::FieldMigration rollbackMap[] = {{countField, rollbackCount}, {speedField, rollbackSpeed}};
    const auto rollbackSlot =
        store.MigrateSlotToPrepared(rollbackTransaction, rollbackMotion, classId, handles[0], rollbackMap, 2);
    assert(store.GetPreparedFloat(rollbackTransaction, rollbackMotion, rollbackCount, rollbackSlot) == 0.0);
    assert(store.GetPreparedFloat(rollbackTransaction, rollbackMotion, rollbackSpeed, rollbackSlot) ==
           beforeFailedScatter);
    store.SetPreparedBool(rollbackTransaction, rollbackMotion, rollbackEnabled, rollbackSlot, true);

    const ComponentDataStore::FieldMigration incompatibleMap[] = {{speedField, rollbackEnabled}};
    ExpectFailure([&] {
        store.MigrateSlotToPrepared(rollbackTransaction, rollbackMotion, classId, handles[1], incompatibleMap, 1);
    });
    const auto slotAfterFailedMigration = store.AllocatePreparedSlot(rollbackTransaction, rollbackMotion);
    assert(slotAfterFailedMigration.index == rollbackSlot.index + 1);

    const auto discarded = store.PrepareClass(rollbackTransaction, "tests:Discarded");
    store.PrepareField(rollbackTransaction, discarded, "value", ComponentDataStore::DataType::Int64);
    store.AllocatePreparedSlot(rollbackTransaction, discarded);
    const bool discardedPreparedClass = store.DiscardPreparedClass(rollbackTransaction, discarded);
    assert(discardedPreparedClass);
    assert(!store.HasPreparedClass(rollbackTransaction, discarded));
    assert(store.FindPreparedClass(rollbackTransaction, "tests:Discarded") ==
           ComponentDataStore::InvalidPreparedClassId);

    const auto rollbackCommitMap = store.SealSchemaTransaction(rollbackTransaction);
    assert(rollbackCommitMap.size() == 1);
    assert(store.GetPreparedFinalClassId(rollbackTransaction, rollbackMotion) == rollbackCommitMap.at(rollbackMotion));
    assert(store.GetClassId("tests:MotionRollback") == UINT32_MAX);
    ExpectFailure(
        [&] { store.PrepareField(rollbackTransaction, rollbackMotion, "late", ComponentDataStore::DataType::Bool); });
    const bool rolledBackSealedTransaction = store.RollbackSchemaTransaction(rollbackTransaction);
    assert(rolledBackSealedTransaction);
    assert(!store.IsSchemaTransactionActive(rollbackTransaction));
    assert(store.GetPublishedClassCount() == publishedBeforeRollback);
    assert(store.GetClassId("tests:MotionRollback") == UINT32_MAX);
    assert(store.GetFloat(classId, speedField, handles[0]) == beforeFailedScatter);
    assert(store.GetInt(classId, countField, handles[0]) == 0);

    // Failure in the second class of a multi-class prepare is recoverable and
    // does not publish the first class or mutate the old layout.
    const size_t publishedBeforeMidFailure = store.GetPublishedClassCount();
    const auto failureTransaction = store.BeginSchemaTransaction();
    const auto firstCandidate = store.PrepareClass(failureTransaction, "tests:CandidateA");
    store.PrepareField(failureTransaction, firstCandidate, "value", ComponentDataStore::DataType::Float64);
    store.AllocatePreparedSlot(failureTransaction, firstCandidate);
    const auto secondCandidate = store.PrepareClass(failureTransaction, "tests:CandidateB");
    store.PrepareField(failureTransaction, secondCandidate, "value", ComponentDataStore::DataType::Int64);
    ExpectFailure(
        [&] { store.PrepareField(failureTransaction, secondCandidate, "value", ComponentDataStore::DataType::Vec3); });
    const bool rolledBackFailureTransaction = store.RollbackSchemaTransaction(failureTransaction);
    assert(rolledBackFailureTransaction);
    assert(store.GetPublishedClassCount() == publishedBeforeMidFailure);
    assert(store.GetClassId("tests:CandidateA") == UINT32_MAX);
    assert(store.GetClassId("tests:CandidateB") == UINT32_MAX);
    assert(store.GetFloat(classId, speedField, handles[0]) == beforeFailedScatter);

    // Commit publishes all classes together. Candidate slots retain their
    // generation handles under the committed class IDs while the old class
    // and its slots remain valid for the Python-side grace period.
    const auto commitTransaction = store.BeginSchemaTransaction();
    const auto committedMotion = store.PrepareClass(commitTransaction, "tests:MotionCommitted");
    const auto committedCount =
        store.PrepareField(commitTransaction, committedMotion, "count", ComponentDataStore::DataType::Float64);
    const auto committedSpeed =
        store.PrepareField(commitTransaction, committedMotion, "speed", ComponentDataStore::DataType::Float64);
    const auto committedAux = store.PrepareClass(commitTransaction, "tests:AuxCommitted");
    const auto committedFlag =
        store.PrepareField(commitTransaction, committedAux, "flag", ComponentDataStore::DataType::Bool);
    const ComponentDataStore::FieldMigration commitMigrations[] = {{countField, committedCount},
                                                                   {speedField, committedSpeed}};
    const auto committedMotionSlot =
        store.MigrateSlotToPrepared(commitTransaction, committedMotion, classId, handles[1], commitMigrations, 2);
    const auto committedAuxSlot = store.AllocatePreparedSlot(commitTransaction, committedAux);
    store.SetPreparedBool(commitTransaction, committedAux, committedFlag, committedAuxSlot, true);

    const auto sealedIds = store.SealSchemaTransaction(commitTransaction);
    assert(store.GetClassId("tests:MotionCommitted") == UINT32_MAX);
    assert(store.GetClassId("tests:AuxCommitted") == UINT32_MAX);
    const auto committedIds = store.CommitSchemaTransaction(commitTransaction);
    assert(committedIds == sealedIds);
    const uint32_t committedMotionId = committedIds.at(committedMotion);
    const uint32_t committedAuxId = committedIds.at(committedAux);
    assert(store.GetClassId("tests:MotionCommitted") == committedMotionId);
    assert(store.GetClassId("tests:AuxCommitted") == committedAuxId);
    assert(store.IsAlive(committedMotionId, committedMotionSlot));
    assert(store.IsAlive(committedAuxId, committedAuxSlot));
    assert(store.GetFloat(committedMotionId, committedCount, committedMotionSlot) == 1.0);
    assert(store.GetFloat(committedMotionId, committedSpeed, committedMotionSlot) == 1.5);
    assert(store.GetBool(committedAuxId, committedFlag, committedAuxSlot));
    assert(store.IsAlive(classId, handles[1]));
    assert(store.GetInt(classId, countField, handles[1]) == 1);
    assert(store.GetFloat(classId, speedField, handles[1]) == 1.5);
    assert(store.IsSchemaTransactionActive(commitTransaction));
    const bool finalizedCommitTransaction = store.FinalizeSchemaTransaction(commitTransaction);
    assert(finalizedCommitTransaction);
    assert(!store.IsSchemaTransactionActive(commitTransaction));

    // A committed schema stays reversible until the owner publishes all
    // Python registry/module/descriptor state and explicitly finalizes it.
    const size_t publishedBeforeCommittedRollback = store.GetPublishedClassCount();
    const auto committedRollbackTransaction = store.BeginSchemaTransaction();
    const auto committedRollbackClass =
        store.PrepareClass(committedRollbackTransaction, "tests:CommittedRollback");
    store.PrepareField(committedRollbackTransaction, committedRollbackClass, "value",
                       ComponentDataStore::DataType::Float64);
    store.AllocatePreparedSlot(committedRollbackTransaction, committedRollbackClass);
    store.CommitSchemaTransaction(committedRollbackTransaction);
    assert(store.GetClassId("tests:CommittedRollback") != UINT32_MAX);
    const bool rolledBackCommittedTransaction = store.RollbackSchemaTransaction(committedRollbackTransaction);
    assert(rolledBackCommittedTransaction);
    assert(store.GetPublishedClassCount() == publishedBeforeCommittedRollback);
    assert(store.GetClassId("tests:CommittedRollback") == UINT32_MAX);

    // A stable type may publish several layouts.  The old class remains
    // addressable while its slot is live, then its storage is reclaimed and
    // the class id becomes reusable after the durable finalize edge.
    store.Clear();
    const uint32_t oldLayout = store.RegisterClass("tests:Reloadable@old");
    const uint32_t oldValue = store.RegisterField(oldLayout, "value", ComponentDataStore::DataType::Float64);
    const auto oldLayoutSlot = store.AllocateSlot(oldLayout);
    store.SetFloat(oldLayout, oldValue, oldLayoutSlot, 42.0);

    const auto layoutTransaction = store.BeginSchemaTransaction();
    const auto newLayout = store.PrepareClass(layoutTransaction, "tests:Reloadable@new");
    const auto newValue =
        store.PrepareField(layoutTransaction, newLayout, "value", ComponentDataStore::DataType::Float64);
    const ComponentDataStore::FieldMigration layoutMigration[] = {{oldValue, newValue}};
    const auto newLayoutSlot = store.MigrateSlotToPrepared(
        layoutTransaction, newLayout, oldLayout, oldLayoutSlot, layoutMigration, 1);
    const auto layoutIds = store.CommitSchemaTransaction(layoutTransaction);
    const uint32_t newLayoutId = layoutIds.at(newLayout);
    assert(newLayoutId != oldLayout);
    assert(store.IsAlive(oldLayout, oldLayoutSlot));
    assert(store.IsAlive(newLayoutId, newLayoutSlot));
    assert(store.GetFloat(newLayoutId, newValue, newLayoutSlot) == 42.0);
    assert(store.GetPublishedClassCount() == 1);
    store.FinalizeSchemaTransaction(layoutTransaction);
    assert(store.GetClassId("tests:Reloadable@old") == UINT32_MAX);
    assert(store.IsAlive(oldLayout, oldLayoutSlot));
    store.ReleaseSlot(oldLayout, oldLayoutSlot);
    assert(!store.IsAlive(oldLayout, oldLayoutSlot));

    const auto reuseTransaction = store.BeginSchemaTransaction();
    const auto reusedClass = store.PrepareClass(reuseTransaction, "tests:Reused@layout");
    store.PrepareField(reuseTransaction, reusedClass, "value", ComponentDataStore::DataType::Float64);
    const auto reusedIds = store.CommitSchemaTransaction(reuseTransaction);
    assert(reusedIds.at(reusedClass) == oldLayout);
    store.FinalizeSchemaTransaction(reuseTransaction);

    store.Clear();
    constexpr size_t benchmarkCount = 100000;
    const auto benchmarkStart = std::chrono::steady_clock::now();
    const uint32_t benchmarkClass = store.RegisterClass("tests:Batch100k");
    const uint32_t benchmarkField = store.RegisterField(benchmarkClass, "position", ComponentDataStore::DataType::Vec3);
    store.ReserveClass(benchmarkClass, benchmarkCount);
    std::vector<ComponentDataStore::SlotHandle> benchmarkHandles;
    benchmarkHandles.reserve(benchmarkCount);
    for (size_t i = 0; i < benchmarkCount; ++i)
        benchmarkHandles.push_back(store.AllocateSlot(benchmarkClass));
    std::vector<float> benchmarkInput(benchmarkCount * 3);
    std::vector<float> benchmarkOutput(benchmarkCount * 3);
    for (size_t i = 0; i < benchmarkCount; ++i) {
        benchmarkInput[i * 3] = static_cast<float>(i);
        benchmarkInput[i * 3 + 1] = 2.0F;
        benchmarkInput[i * 3 + 2] = 3.0F;
    }
    store.ScatterVec3(benchmarkClass, benchmarkField, benchmarkHandles.data(), benchmarkHandles.size(),
                      benchmarkInput.data());
    store.GatherVec3(benchmarkClass, benchmarkField, benchmarkHandles.data(), benchmarkHandles.size(),
                     benchmarkOutput.data());
    assert(benchmarkOutput == benchmarkInput);
    const double benchmarkSeconds =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - benchmarkStart).count();
    std::cout << "ComponentDataStore 100k reserve/allocate/scatter/gather: " << benchmarkSeconds << " s\n";
    assert(benchmarkSeconds < 5.0);

    store.Clear();
    return 0;
}
