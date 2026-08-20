#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace infernux
{

template <typename StableId> struct EditorCollectionSelection
{
    std::vector<StableId> selectedIds;
    std::optional<StableId> primaryId;
    std::optional<StableId> anchorId;
    std::optional<StableId> cursorId;

    bool operator==(const EditorCollectionSelection &other) const
    {
        return selectedIds == other.selectedIds && primaryId == other.primaryId && anchorId == other.anchorId &&
               cursorId == other.cursorId;
    }
};

template <typename StableId> struct EditorCollectionRenameSession
{
    StableId itemId;
    std::string originalValue;
    std::string buffer;
    bool focusPending = true;
};

template <typename StableId, typename Hash = std::hash<StableId>> class EditorCollectionInteractionModel
{
  public:
    using Selection = EditorCollectionSelection<StableId>;
    using RenameSession = EditorCollectionRenameSession<StableId>;

    [[nodiscard]] uint64_t Revision() const
    {
        return m_revision;
    }

    [[nodiscard]] const std::vector<StableId> &OrderedIds() const
    {
        return m_orderedIds;
    }

    [[nodiscard]] const Selection &CurrentSelection() const
    {
        return m_selection;
    }

    [[nodiscard]] const std::optional<RenameSession> &Rename() const
    {
        return m_rename;
    }

    [[nodiscard]] bool Contains(const StableId &id) const
    {
        return m_index.find(id) != m_index.end();
    }

    bool SetItems(const std::vector<StableId> &ids)
    {
        ValidateUnique(ids);
        if (ids == m_orderedIds)
            return false;
        m_orderedIds = ids;
        RebuildIndex();

        m_selection.selectedIds.erase(std::remove_if(m_selection.selectedIds.begin(), m_selection.selectedIds.end(),
                                                     [this](const StableId &id) { return !Contains(id); }),
                                      m_selection.selectedIds.end());
        if (m_selection.primaryId && std::find(m_selection.selectedIds.begin(), m_selection.selectedIds.end(),
                                               *m_selection.primaryId) == m_selection.selectedIds.end()) {
            m_selection.primaryId = m_selection.selectedIds.empty()
                                        ? std::nullopt
                                        : std::optional<StableId>(m_selection.selectedIds.back());
        }
        if (m_selection.anchorId && !Contains(*m_selection.anchorId))
            m_selection.anchorId = m_selection.primaryId;
        if (m_selection.cursorId && !Contains(*m_selection.cursorId))
            m_selection.cursorId = m_selection.primaryId;
        if (m_rename && !Contains(m_rename->itemId))
            m_rename.reset();
        ++m_revision;
        return true;
    }

    bool ProjectSelection(const std::vector<StableId> &ids, const std::optional<StableId> &primary = std::nullopt,
                          bool preserveAnchor = true)
    {
        ValidateUnique(ids);
        std::unordered_set<StableId, Hash> requested(ids.begin(), ids.end());
        for (const StableId &id : ids) {
            if (!Contains(id))
                throw std::out_of_range("collection selection references an unknown stable ID");
        }
        if (primary && requested.find(*primary) == requested.end())
            throw std::invalid_argument("collection primary must be selected");

        Selection projected;
        for (const StableId &id : m_orderedIds) {
            if (requested.find(id) != requested.end())
                projected.selectedIds.push_back(id);
        }
        projected.primaryId = primary;
        if (!projected.primaryId && !projected.selectedIds.empty())
            projected.primaryId = projected.selectedIds.back();
        projected.anchorId = preserveAnchor ? m_selection.anchorId : std::nullopt;
        if (projected.anchorId && !Contains(*projected.anchorId))
            projected.anchorId = projected.primaryId;
        if (!projected.anchorId)
            projected.anchorId = projected.primaryId;
        projected.cursorId = projected.primaryId ? projected.primaryId : m_selection.cursorId;
        if (projected.cursorId && !Contains(*projected.cursorId))
            projected.cursorId.reset();
        if (projected == m_selection)
            return false;
        m_selection = std::move(projected);
        ++m_revision;
        return true;
    }

    const Selection &Activate(const StableId &id, bool toggle = false, bool extend = false)
    {
        if (toggle && extend)
            throw std::invalid_argument("collection selection cannot toggle and extend together");
        const size_t targetIndex = RequireIndex(id);
        Selection updated;
        if (extend) {
            const StableId anchor =
                m_selection.anchorId && Contains(*m_selection.anchorId)
                    ? *m_selection.anchorId
                    : (m_selection.primaryId && Contains(*m_selection.primaryId) ? *m_selection.primaryId : id);
            const size_t anchorIndex = RequireIndex(anchor);
            const size_t first = std::min(anchorIndex, targetIndex);
            const size_t last = std::max(anchorIndex, targetIndex);
            updated.selectedIds.assign(m_orderedIds.begin() + static_cast<std::ptrdiff_t>(first),
                                       m_orderedIds.begin() + static_cast<std::ptrdiff_t>(last + 1));
            updated.primaryId = id;
            updated.anchorId = anchor;
            updated.cursorId = id;
        } else if (toggle) {
            std::unordered_set<StableId, Hash> selected(m_selection.selectedIds.begin(), m_selection.selectedIds.end());
            if (!selected.erase(id))
                selected.insert(id);
            for (const StableId &candidate : m_orderedIds) {
                if (selected.find(candidate) != selected.end())
                    updated.selectedIds.push_back(candidate);
            }
            updated.primaryId =
                selected.find(id) != selected.end()
                    ? std::optional<StableId>(id)
                    : (updated.selectedIds.empty() ? std::nullopt
                                                   : std::optional<StableId>(updated.selectedIds.back()));
            updated.anchorId = selected.find(id) != selected.end() ? std::optional<StableId>(id) : m_selection.anchorId;
            if (updated.anchorId && !Contains(*updated.anchorId))
                updated.anchorId = updated.primaryId;
            updated.cursorId = id;
        } else {
            updated.selectedIds = {id};
            updated.primaryId = id;
            updated.anchorId = id;
            updated.cursorId = id;
        }
        if (!(updated == m_selection)) {
            m_selection = std::move(updated);
            ++m_revision;
        }
        return m_selection;
    }

    const Selection &MoveCursor(int offset, bool extend = false, bool wrap = false)
    {
        if (m_orderedIds.empty() || offset == 0)
            return m_selection;
        size_t current = offset > 0 ? 0 : m_orderedIds.size() - 1;
        if (m_selection.cursorId && Contains(*m_selection.cursorId))
            current = RequireIndex(*m_selection.cursorId);
        long long target = static_cast<long long>(current) + offset;
        if (wrap) {
            const long long count = static_cast<long long>(m_orderedIds.size());
            target = (target % count + count) % count;
        } else {
            target = std::clamp(target, 0LL, static_cast<long long>(m_orderedIds.size() - 1));
        }
        return Activate(m_orderedIds[static_cast<size_t>(target)], false, extend);
    }

    RenameSession &BeginRename(const StableId &id, const std::string &value)
    {
        (void)RequireIndex(id);
        m_rename = RenameSession{id, value, value, true};
        ++m_revision;
        return *m_rename;
    }

    bool UpdateRename(const std::string &value)
    {
        if (!m_rename || m_rename->buffer == value)
            return false;
        m_rename->buffer = value;
        ++m_revision;
        return true;
    }

    bool ConsumeRenameFocus()
    {
        if (!m_rename || !m_rename->focusPending)
            return false;
        m_rename->focusPending = false;
        return true;
    }

    bool CancelRename()
    {
        if (!m_rename)
            return false;
        m_rename.reset();
        ++m_revision;
        return true;
    }

  private:
    static void ValidateUnique(const std::vector<StableId> &ids)
    {
        std::unordered_set<StableId, Hash> seen;
        for (const StableId &id : ids) {
            if (!seen.insert(id).second)
                throw std::invalid_argument("collection stable IDs must be unique");
        }
    }

    void RebuildIndex()
    {
        m_index.clear();
        for (size_t index = 0; index < m_orderedIds.size(); ++index)
            m_index.emplace(m_orderedIds[index], index);
    }

    [[nodiscard]] size_t RequireIndex(const StableId &id) const
    {
        const auto it = m_index.find(id);
        if (it == m_index.end())
            throw std::out_of_range("unknown collection stable ID");
        return it->second;
    }

    std::vector<StableId> m_orderedIds;
    std::unordered_map<StableId, size_t, Hash> m_index;
    Selection m_selection;
    std::optional<RenameSession> m_rename;
    uint64_t m_revision = 0;
};

template <typename StableId, typename Hash = std::hash<StableId>> class EditorTreeProjectionModel
{
  public:
    [[nodiscard]] uint64_t Revision() const
    {
        return m_revision;
    }

    [[nodiscard]] bool Empty() const
    {
        return m_expandedIds.empty();
    }

    [[nodiscard]] bool IsExpanded(const StableId &id) const
    {
        return m_expandedIds.find(id) != m_expandedIds.end();
    }

    [[nodiscard]] const std::unordered_set<StableId, Hash> &ExpandedIds() const
    {
        return m_expandedIds;
    }

    bool SetExpanded(const StableId &id, bool expanded)
    {
        const bool changed = expanded ? m_expandedIds.insert(id).second : m_expandedIds.erase(id) > 0;
        if (changed)
            ++m_revision;
        return changed;
    }

    bool ReplaceExpanded(const std::unordered_set<StableId, Hash> &expandedIds)
    {
        if (m_expandedIds == expandedIds)
            return false;
        m_expandedIds = expandedIds;
        ++m_revision;
        return true;
    }

    bool Toggle(const StableId &id)
    {
        SetExpanded(id, !IsExpanded(id));
        return IsExpanded(id);
    }

    bool Reconcile(const std::unordered_set<StableId, Hash> &validIds)
    {
        const size_t before = m_expandedIds.size();
        for (auto it = m_expandedIds.begin(); it != m_expandedIds.end();) {
            if (validIds.find(*it) == validIds.end())
                it = m_expandedIds.erase(it);
            else
                ++it;
        }
        if (m_expandedIds.size() == before)
            return false;
        ++m_revision;
        return true;
    }

    void Clear()
    {
        if (m_expandedIds.empty())
            return;
        m_expandedIds.clear();
        ++m_revision;
    }

  private:
    std::unordered_set<StableId, Hash> m_expandedIds;
    uint64_t m_revision = 0;
};

} // namespace infernux
