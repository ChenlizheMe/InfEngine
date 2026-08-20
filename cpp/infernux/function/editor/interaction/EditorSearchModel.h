#pragma once

#include <cctype>
#include <cstdint>
#include <string>
#include <string_view>

namespace infernux
{

struct EditorSearchToken
{
    uint64_t queryRevision = 0;
    uint64_t sourceGeneration = 0;
    std::string scopeKey;

    [[nodiscard]] bool operator==(const EditorSearchToken &other) const noexcept
    {
        return queryRevision == other.queryRevision && sourceGeneration == other.sourceGeneration &&
               scopeKey == other.scopeKey;
    }

    [[nodiscard]] bool operator!=(const EditorSearchToken &other) const noexcept
    {
        return !(*this == other);
    }
};

/// Shared high-frequency search state for native editor surfaces.
///
/// Panels retain their own result projection and rendering, while query
/// normalization, revisions, and stale-completion tokens have one contract.
class EditorSearchModel
{
  public:
    [[nodiscard]] bool SetQuery(std::string_view query)
    {
        std::string raw(query);
        std::string normalized = Normalize(query);
        if (raw == m_query && normalized == m_normalized)
            return false;
        m_query = std::move(raw);
        m_normalized = std::move(normalized);
        ++m_revision;
        return true;
    }

    [[nodiscard]] bool Clear()
    {
        return SetQuery({});
    }

    [[nodiscard]] bool IsActive() const noexcept
    {
        return !m_normalized.empty();
    }

    [[nodiscard]] const std::string &Query() const noexcept
    {
        return m_query;
    }

    [[nodiscard]] const std::string &NormalizedQuery() const noexcept
    {
        return m_normalized;
    }

    [[nodiscard]] uint64_t Revision() const noexcept
    {
        return m_revision;
    }

    [[nodiscard]] bool Matches(std::string_view candidate) const
    {
        return !IsActive() || Normalize(candidate).find(m_normalized) != std::string::npos;
    }

    [[nodiscard]] bool MatchesNormalized(std::string_view normalizedCandidate) const noexcept
    {
        return !IsActive() || normalizedCandidate.find(m_normalized) != std::string_view::npos;
    }

    [[nodiscard]] EditorSearchToken MakeToken(uint64_t sourceGeneration = 0, std::string scopeKey = {}) const
    {
        return {m_revision, sourceGeneration, std::move(scopeKey)};
    }

    [[nodiscard]] bool Accepts(const EditorSearchToken &token, uint64_t sourceGeneration = 0,
                               std::string_view scopeKey = {}) const noexcept
    {
        return token.queryRevision == m_revision && token.sourceGeneration == sourceGeneration &&
               token.scopeKey == scopeKey;
    }

    [[nodiscard]] static std::string Normalize(std::string_view value)
    {
        size_t begin = 0;
        size_t end = value.size();
        while (begin < end && std::isspace(static_cast<unsigned char>(value[begin])) != 0)
            ++begin;
        while (end > begin && std::isspace(static_cast<unsigned char>(value[end - 1])) != 0)
            --end;

        std::string result(value.substr(begin, end - begin));
        for (char &ch : result) {
            const unsigned char byte = static_cast<unsigned char>(ch);
            if (byte >= static_cast<unsigned char>('A') && byte <= static_cast<unsigned char>('Z'))
                ch = static_cast<char>(byte + ('a' - 'A'));
        }
        return result;
    }

  private:
    std::string m_query;
    std::string m_normalized;
    uint64_t m_revision = 0;
};

} // namespace infernux
