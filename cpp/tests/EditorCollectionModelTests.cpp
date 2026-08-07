#include <function/editor/interaction/EditorCollectionModel.h>

#include <iostream>
#include <stdexcept>
#include <string>
#include <unordered_set>

namespace
{
bool Check(bool condition, const char *message)
{
    if (!condition)
        std::cerr << "EditorCollectionModelTests: " << message << '\n';
    return condition;
}
} // namespace

int main()
{
    using infernux::EditorCollectionInteractionModel;
    using infernux::EditorTreeProjectionModel;

    EditorCollectionInteractionModel<std::string> collection;
    if (!Check(collection.SetItems({"a", "b", "c", "d"}), "initial items were not accepted"))
        return 1;
    collection.Activate("b");
    const auto &range = collection.Activate("d", false, true);
    if (!Check(range.selectedIds == std::vector<std::string>{"b", "c", "d"}, "range selection order is wrong"))
        return 1;
    if (!Check(range.anchorId && *range.anchorId == "b", "range selection lost its anchor"))
        return 1;
    const auto &toggled = collection.Activate("c", true, false);
    if (!Check(toggled.selectedIds == std::vector<std::string>{"b", "d"}, "toggle selection order is wrong"))
        return 1;

    collection.BeginRename("b", "Before");
    if (!Check(collection.ConsumeRenameFocus(), "rename focus was not requested"))
        return 1;
    if (!Check(!collection.ConsumeRenameFocus(), "rename focus was consumed twice"))
        return 1;
    if (!Check(collection.UpdateRename("After"), "rename buffer was not updated"))
        return 1;
    if (!Check(collection.Rename() && collection.Rename()->buffer == "After", "rename buffer was not retained"))
        return 1;
    if (!Check(collection.SetItems({"a", "c", "d"}), "item removal was not accepted"))
        return 1;
    if (!Check(!collection.Rename(), "rename survived removal of its item"))
        return 1;

    bool duplicateRejected = false;
    try {
        collection.SetItems({"a", "a"});
    } catch (const std::invalid_argument &) {
        duplicateRejected = true;
    }
    if (!Check(duplicateRejected, "duplicate stable IDs were accepted"))
        return 1;

    EditorTreeProjectionModel<uint64_t> tree;
    if (!Check(tree.SetExpanded(10, true) && tree.SetExpanded(20, true), "tree expansion was not recorded"))
        return 1;
    if (!Check(tree.IsExpanded(10), "expanded tree item is not visible"))
        return 1;
    if (!Check(tree.Reconcile(std::unordered_set<uint64_t>{10, 30}), "tree reconciliation reported no change"))
        return 1;
    if (!Check(tree.IsExpanded(10) && !tree.IsExpanded(20), "tree reconciliation removed the wrong item"))
        return 1;
    if (!Check(!tree.SetExpanded(10, true), "idempotent expansion reported a change"))
        return 1;
    if (!Check(tree.ReplaceExpanded(std::unordered_set<uint64_t>{20, 30}), "tree expansion snapshot was not replaced"))
        return 1;
    if (!Check(tree.IsExpanded(20) && tree.IsExpanded(30) && !tree.IsExpanded(10),
               "tree expansion snapshot contains the wrong items"))
        return 1;
    if (!Check(!tree.ReplaceExpanded(std::unordered_set<uint64_t>{30, 20}),
               "idempotent tree snapshot replacement reported a change"))
        return 1;
    if (!Check(!tree.Toggle(20) && !tree.IsExpanded(20), "tree toggle did not collapse the item"))
        return 1;
    return 0;
}
