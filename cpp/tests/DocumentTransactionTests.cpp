#include <platform/filesystem/DocumentTransaction.h>

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

using infernux::DocumentTransaction;
using infernux::DocumentTransactionEntry;

namespace
{
void Require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}

std::string ReadText(const std::filesystem::path &path)
{
    std::ifstream file(path, std::ios::in | std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
}

void WriteText(const std::filesystem::path &path, const std::string &content)
{
    std::ofstream file(path, std::ios::out | std::ios::trunc | std::ios::binary);
    file << content;
    if (!file)
        throw std::runtime_error("failed to create transaction test fixture");
}

void TestCommitWritesAllTargetsAndInvalidatesDerivedIndex(const std::filesystem::path &root)
{
    const auto journal = root / "Library" / "AssetRefresh.transaction";
    const auto first = root / "Assets" / "first.txt.meta";
    const auto second = root / "Assets" / "second.txt.meta";
    const auto index = root / "Library" / "AssetIndex.json";
    std::filesystem::create_directories(first.parent_path());
    std::filesystem::create_directories(index.parent_path());
    WriteText(index, "stale index");

    DocumentTransaction::Commit(root.u8string(), journal.u8string(),
                                {{first.u8string(), "first metadata"}, {second.u8string(), "second metadata"}},
                                {index.u8string()});

    Require(ReadText(first) == "first metadata", "transaction wrote the wrong first payload");
    Require(ReadText(second) == "second metadata", "transaction wrote the wrong second payload");
    Require(!std::filesystem::exists(index), "transaction left the stale derived index visible");
    Require(!std::filesystem::exists(journal), "successful transaction left its journal behind");
}

void TestCommitCreatesMissingTargetDirectories(const std::filesystem::path &root)
{
    const auto journal = root / "Library" / "Nested.transaction";
    const auto target = root / "Library" / "Artifacts" / "Mesh" / "mesh.inxmesh";
    std::filesystem::remove_all(root / "Library" / "Artifacts");

    DocumentTransaction::Commit(root.u8string(), journal.u8string(), {{target.u8string(), "mesh-artifact"}}, {});

    Require(ReadText(target) == "mesh-artifact", "transaction did not write a target in a missing nested directory");
    Require(!std::filesystem::exists(journal), "nested-directory transaction left its journal behind");
}

void TestRecoveryReplaysInterruptedTransaction(const std::filesystem::path &root)
{
    const auto journal = root / "Library" / "Recovery.transaction";
    const auto blocker = root / "Assets" / "blocked";
    const auto target = blocker / "recovered.meta";
    const auto index = root / "Library" / "RecoveryIndex.json";
    WriteText(blocker, "not a directory");
    WriteText(index, "stale index");

    bool failed = false;
    try {
        DocumentTransaction::Commit(root.u8string(), journal.u8string(), {{target.u8string(), "recovered"}},
                                    {index.u8string()});
    } catch (const std::exception &) {
        failed = true;
    }
    Require(failed, "transaction failure fixture unexpectedly committed");
    Require(std::filesystem::exists(journal), "failed transaction did not retain its journal");
    Require(std::filesystem::exists(index), "failed transaction invalidated the index before applying all targets");

    const std::string validJournal = ReadText(journal);
    std::string corruptJournal = validJournal;
    corruptJournal.back() ^= 0x5a;
    WriteText(journal, corruptJournal);
    bool checksumRejected = false;
    try {
        (void)DocumentTransaction::Recover(root.u8string(), journal.u8string());
    } catch (const std::invalid_argument &) {
        checksumRejected = true;
    }
    Require(checksumRejected, "corrupt compressed journal payload was accepted");
    WriteText(journal, validJournal);

    std::filesystem::remove(blocker);
    std::filesystem::create_directories(blocker);
    Require(DocumentTransaction::Recover(root.u8string(), journal.u8string()), "journal recovery was not detected");
    Require(ReadText(target) == "recovered", "journal recovery wrote the wrong payload");
    Require(!std::filesystem::exists(index), "journal recovery did not invalidate the stale index");
    Require(!std::filesystem::exists(journal), "journal recovery did not clear the journal");
    Require(!DocumentTransaction::Recover(root.u8string(), journal.u8string()),
            "missing journal was reported as recovered");
}

void TestRecoveryUsesProjectRelativePathsAfterMove(const std::filesystem::path &root)
{
    const auto originalRoot = root / "move-source";
    const auto movedRoot = root / "move-destination";
    const auto blocker = originalRoot / "Assets" / "blocked";
    const auto target = blocker / "moved.meta";
    const auto journal = originalRoot / "Library" / "Move.transaction";
    const auto index = originalRoot / "Library" / "AssetIndex.json";
    std::filesystem::create_directories(blocker.parent_path());
    std::filesystem::create_directories(journal.parent_path());
    WriteText(blocker, "not a directory");
    WriteText(index, "stale index");

    bool failed = false;
    try {
        DocumentTransaction::Commit(originalRoot.u8string(), journal.u8string(), {{target.u8string(), "moved"}},
                                    {index.u8string()});
    } catch (const std::exception &) {
        failed = true;
    }
    Require(failed, "movable transaction failure fixture unexpectedly committed");
    Require(std::filesystem::exists(journal), "movable transaction did not retain its journal");

    std::filesystem::rename(originalRoot, movedRoot);
    const auto movedBlocker = movedRoot / "Assets" / "blocked";
    const auto movedTarget = movedBlocker / "moved.meta";
    const auto movedJournal = movedRoot / "Library" / "Move.transaction";
    const auto movedIndex = movedRoot / "Library" / "AssetIndex.json";
    std::filesystem::remove(movedBlocker);
    std::filesystem::create_directories(movedBlocker);
    Require(DocumentTransaction::Recover(movedRoot.u8string(), movedJournal.u8string()),
            "moved project journal was not recovered");
    Require(ReadText(movedTarget) == "moved", "moved project recovery used the original absolute path");
    Require(!std::filesystem::exists(movedIndex), "moved project recovery left its stale index visible");
    std::filesystem::remove_all(movedRoot);
}

void TestCorruptJournalAndEscapingPathAreRejected(const std::filesystem::path &root)
{
    const auto journal = root / "Library" / "Corrupt.transaction";
    WriteText(journal, "not a transaction");
    bool corruptRejected = false;
    try {
        (void)DocumentTransaction::Recover(root.u8string(), journal.u8string());
    } catch (const std::invalid_argument &) {
        corruptRejected = true;
    }
    Require(corruptRejected, "corrupt transaction journal was accepted");
    Require(std::filesystem::exists(journal), "corrupt journal was silently deleted");
    std::filesystem::remove(journal);

    const auto outside = root.parent_path() / "outside.meta";
    bool escapeRejected = false;
    try {
        DocumentTransaction::Commit(root.u8string(), journal.u8string(), {{outside.u8string(), "escape"}}, {});
    } catch (const std::invalid_argument &) {
        escapeRejected = true;
    }
    Require(escapeRejected, "transaction accepted a path outside the project root");
    Require(!std::filesystem::exists(journal), "rejected transaction persisted a journal");

    bool selfTargetRejected = false;
    try {
        DocumentTransaction::Commit(root.u8string(), journal.u8string(), {{journal.u8string(), "self"}}, {});
    } catch (const std::invalid_argument &) {
        selfTargetRejected = true;
    }
    Require(selfTargetRejected, "transaction accepted its journal as a target");
    Require(!std::filesystem::exists(journal), "self-target rejection persisted a journal");
}
} // namespace

int main()
{
    const auto suffix = std::chrono::steady_clock::now().time_since_epoch().count();
    const auto root = std::filesystem::temp_directory_path() /
                      std::filesystem::u8path("infernux_document_transaction_可靠_" + std::to_string(suffix));
    std::filesystem::create_directories(root / "Assets");
    std::filesystem::create_directories(root / "Library");
    try {
        TestCommitWritesAllTargetsAndInvalidatesDerivedIndex(root);
        TestCommitCreatesMissingTargetDirectories(root);
        TestRecoveryReplaysInterruptedTransaction(root);
        TestRecoveryUsesProjectRelativePathsAfterMove(root);
        TestCorruptJournalAndEscapingPathAreRejected(root);
        std::filesystem::remove_all(root);
        std::cout << "DocumentTransaction tests passed\n";
        return 0;
    } catch (const std::exception &error) {
        std::filesystem::remove_all(root);
        std::cerr << "DocumentTransaction test failure: " << error.what() << '\n';
        return 1;
    }
}
