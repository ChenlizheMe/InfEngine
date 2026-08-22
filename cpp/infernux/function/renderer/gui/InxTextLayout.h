#pragma once

#include <imgui.h>
#include <imgui_internal.h>
#include <platform/filesystem/InxPath.h>

#ifdef DrawText
#undef DrawText
#endif

#include <algorithm>
#include <cassert>
#include <cfloat>
#include <filesystem>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux::textlayout
{

struct TextLayoutParams
{
    std::string text;
    std::string fontPath;
    float fontSize = 0.0f;
    float wrapWidth = 0.0f;
    float lineHeight = 1.0f;
    float letterSpacing = 0.0f;
};

struct TextLine
{
    size_t startOffset = 0;
    size_t endOffset = 0;
    float width = 0.0f;
};

struct TextLayoutResult
{
    std::string text;
    ImFont *font = nullptr;
    float fontSize = 0.0f;
    float lineAdvance = 0.0f;
    float baseLineHeight = 0.0f;
    float totalWidth = 0.0f;
    float totalHeight = 0.0f;
    std::vector<TextLine> lines;
};

// Font cache and missing-font set.  Accessed only from the main/render thread
// (ImGui is single-threaded by design).  Wrapped in accessors to avoid static
// initialization order issues across translation units.
//
// Debug-mode assertion records the first caller's thread ID and asserts on
// subsequent calls from a different thread, catching accidental cross-thread use.

namespace detail
{
inline void AssertMainThread()
{
#ifndef NDEBUG
    static const std::thread::id s_ownerThread = std::this_thread::get_id();
    assert(std::this_thread::get_id() == s_ownerThread && "InxTextLayout font cache accessed from non-owner thread");
#endif
}
} // namespace detail

inline std::unordered_map<std::string, ImFont *> &GetFontCache()
{
    detail::AssertMainThread();
    static std::unordered_map<std::string, ImFont *> cache;
    return cache;
}

inline std::unordered_set<std::string> &GetMissingFonts()
{
    detail::AssertMainThread();
    static std::unordered_set<std::string> missing;
    return missing;
}

inline float ResolveFontSize(float fontSize)
{
    return fontSize > 0.0f ? fontSize : ImGui::GetFontSize();
}

inline std::string NormalizeFontPath(const std::string &fontPath)
{
    return ResolveFilesystemPath(fontPath);
}

inline ImFont *ResolveFont(const std::string &fontPath)
{
    if (fontPath.empty())
        return ImGui::GetFont();

    const std::string normalizedPath = NormalizeFontPath(fontPath);
    if (normalizedPath.empty())
        return ImGui::GetFont();
    const std::string pathKey = FilesystemPathKey(normalizedPath);

    auto &fontCache = GetFontCache();
    auto &missingFonts = GetMissingFonts();

    if (auto it = fontCache.find(pathKey); it != fontCache.end() && it->second != nullptr)
        return it->second;
    if (missingFonts.find(pathKey) != missingFonts.end())
        return ImGui::GetFont();

    std::error_code ec;
    if (!std::filesystem::exists(ToFsPath(normalizedPath), ec) || ec) {
        missingFonts.insert(pathKey);
        return ImGui::GetFont();
    }

    ImFontConfig config{};
    config.FontDataOwnedByAtlas = false;
    ImFont *font = ImGui::GetIO().Fonts->AddFontFromFileTTF(normalizedPath.c_str(), 18.0f, &config);
    if (font == nullptr) {
        missingFonts.insert(pathKey);
        return ImGui::GetFont();
    }

    fontCache[pathKey] = font;
    return font;
}

inline bool IsSpaceLike(ImWchar c)
{
    return c == ' ' || c == '\t';
}

inline bool IsBreakAfterChar(ImWchar c)
{
    switch (c) {
    case '-':
    case '/':
    case '\\':
    case ',':
    case '.':
    case ';':
    case ':':
    case '!':
    case '?':
    case 0x3001:
    case 0x3002:
    case 0xFF0C:
    case 0xFF01:
    case 0xFF1F:
    case 0xFF1A:
    case 0xFF1B:
        return true;
    default:
        return false;
    }
}

inline bool PopBackUtf8Codepoint(std::string &text)
{
    if (text.empty())
        return false;

    size_t codepointStart = text.size() - 1;
    while (codepointStart > 0 && (static_cast<unsigned char>(text[codepointStart]) & 0xC0u) == 0x80u)
        --codepointStart;
    text.erase(codepointStart);
    return true;
}

inline bool EraseFirstUtf8Codepoint(std::string &text)
{
    if (text.empty())
        return false;

    unsigned int codepoint = 0;
    const int consumed = ImTextCharFromUtf8(&codepoint, text.data(), text.data() + text.size());
    text.erase(0, consumed > 0 ? static_cast<size_t>(consumed) : size_t{1});
    return true;
}

inline float MeasureSegmentWidth(ImFont *font, float fontSize, const char *start, const char *end, float letterSpacing)
{
    if (font == nullptr || start == nullptr || end == nullptr || start >= end)
        return 0.0f;

    float width = 0.0f;
    int glyphCount = 0;
    const char *cursor = start;
    while (cursor < end) {
        const char *charStart = cursor;
        unsigned int codepoint = 0;
        int consumed = ImTextCharFromUtf8(&codepoint, cursor, end);
        if (consumed <= 0)
            break;
        cursor += consumed;

        if (codepoint == '\r' || codepoint == '\n')
            continue;

        const float glyphWidth = font->CalcTextSizeA(fontSize, FLT_MAX, 0.0f, charStart, cursor).x;
        if (glyphCount > 0)
            width += letterSpacing;
        width += glyphWidth;
        ++glyphCount;
    }

    return width;
}

inline void PushLine(TextLayoutResult &result, const char *textBegin, const char *start, const char *end, float width)
{
    result.lines.push_back({static_cast<size_t>(start - textBegin), static_cast<size_t>(end - textBegin), width});
    result.totalWidth = std::max(result.totalWidth, width);
}

inline TextLayoutResult LayoutText(const TextLayoutParams &params)
{
    TextLayoutResult result{};
    result.text = params.text;
    result.font = ResolveFont(params.fontPath);
    result.fontSize = ResolveFontSize(params.fontSize);
    result.baseLineHeight =
        result.font != nullptr
            ? std::max(result.font->CalcTextSizeA(result.fontSize, FLT_MAX, 0.0f, "A").y, result.fontSize)
            : result.fontSize;
    result.lineAdvance = result.baseLineHeight * std::max(params.lineHeight, 0.1f);

    if (result.font == nullptr || result.text.empty())
        return result;

    const float wrapWidth = params.wrapWidth > 0.0f ? params.wrapWidth : 0.0f;
    const float letterSpacing = params.letterSpacing;
    const char *textBegin = result.text.c_str();
    const char *textEnd = textBegin + result.text.size();
    const char *lineStart = textBegin;
    const char *cursor = textBegin;
    bool endedWithNewline = false;

    float lineWidth = 0.0f;
    int glyphCount = 0;
    const char *lastBreak = nullptr;
    const char *resumeAfterBreak = nullptr;
    float widthAtBreak = 0.0f;

    while (cursor < textEnd) {
        const char *charStart = cursor;
        unsigned int codepoint = 0;
        int consumed = ImTextCharFromUtf8(&codepoint, cursor, textEnd);
        if (consumed <= 0)
            break;
        cursor += consumed;

        if (codepoint == '\r')
            continue;

        if (codepoint == '\n') {
            PushLine(result, textBegin, lineStart, charStart, lineWidth);
            lineStart = cursor;
            lineWidth = 0.0f;
            glyphCount = 0;
            lastBreak = nullptr;
            resumeAfterBreak = nullptr;
            widthAtBreak = 0.0f;
            endedWithNewline = true;
            continue;
        }

        endedWithNewline = false;

        const float glyphWidth = result.font->CalcTextSizeA(result.fontSize, FLT_MAX, 0.0f, charStart, cursor).x;
        const float nextWidth = lineWidth + (glyphCount > 0 ? letterSpacing : 0.0f) + glyphWidth;

        if (wrapWidth > 0.0f && glyphCount > 0 && nextWidth > wrapWidth) {
            if (lastBreak != nullptr && lastBreak > lineStart) {
                PushLine(result, textBegin, lineStart, lastBreak, widthAtBreak);
                lineStart = resumeAfterBreak != nullptr ? resumeAfterBreak : lastBreak;
                cursor = lineStart;
            } else {
                PushLine(result, textBegin, lineStart, charStart, lineWidth);
                lineStart = charStart;
                cursor = charStart;
            }
            lineWidth = 0.0f;
            glyphCount = 0;
            lastBreak = nullptr;
            resumeAfterBreak = nullptr;
            widthAtBreak = 0.0f;
            continue;
        }

        lineWidth = nextWidth;
        ++glyphCount;

        if (IsSpaceLike(static_cast<ImWchar>(codepoint))) {
            const char *resume = cursor;
            while (resume < textEnd) {
                unsigned int nextCodepoint = 0;
                int nextConsumed = ImTextCharFromUtf8(&nextCodepoint, resume, textEnd);
                if (nextConsumed <= 0 || !IsSpaceLike(static_cast<ImWchar>(nextCodepoint)))
                    break;
                resume += nextConsumed;
            }
            lastBreak = charStart;
            resumeAfterBreak = resume;
            widthAtBreak = lineWidth - ((glyphCount > 1 ? letterSpacing : 0.0f) + glyphWidth);
        } else if (IsBreakAfterChar(static_cast<ImWchar>(codepoint))) {
            lastBreak = cursor;
            resumeAfterBreak = cursor;
            widthAtBreak = lineWidth;
        }
    }

    if (lineStart < textEnd)
        PushLine(result, textBegin, lineStart, textEnd, lineWidth);
    else if (endedWithNewline)
        PushLine(result, textBegin, textEnd, textEnd, 0.0f);

    if (!result.lines.empty())
        result.totalHeight = result.baseLineHeight + result.lineAdvance * static_cast<float>(result.lines.size() - 1);

    return result;
}

inline void RenderLine(ImDrawList *drawList, const TextLayoutResult &layout, const TextLine &line, float x, float y,
                       ImU32 color, float letterSpacing)
{
    if (drawList == nullptr || layout.font == nullptr)
        return;

    const char *textBase = layout.text.c_str();
    const char *start = textBase + line.startOffset;
    const char *end = textBase + line.endOffset;

    float cursorX = x;
    int glyphIndex = 0;
    const char *cursor = start;
    while (cursor < end) {
        const char *charStart = cursor;
        unsigned int codepoint = 0;
        int consumed = ImTextCharFromUtf8(&codepoint, cursor, end);
        if (consumed <= 0)
            break;
        cursor += consumed;

        if (codepoint == '\r' || codepoint == '\n')
            continue;

        const float glyphWidth = layout.font->CalcTextSizeA(layout.fontSize, FLT_MAX, 0.0f, charStart, cursor).x;
        if (glyphIndex > 0)
            cursorX += letterSpacing;
        layout.font->RenderChar(drawList, layout.fontSize, ImVec2(cursorX, y), color, static_cast<ImWchar>(codepoint));
        cursorX += glyphWidth;
        ++glyphIndex;
    }
}

inline void RenderTextBox(ImDrawList *drawList, float minX, float minY, float maxX, float maxY,
                          const TextLayoutResult &layout, ImU32 color, float alignX, float alignY, float letterSpacing)
{
    if (drawList == nullptr || layout.font == nullptr || layout.lines.empty())
        return;

    const float boxWidth = maxX - minX;
    const float boxHeight = maxY - minY;
    const float baseY = minY + (boxHeight - layout.totalHeight) * alignY;

    for (size_t index = 0; index < layout.lines.size(); ++index) {
        const TextLine &line = layout.lines[index];
        const float lineX = minX + (boxWidth - line.width) * alignX;
        const float lineY = baseY + layout.lineAdvance * static_cast<float>(index);
        RenderLine(drawList, layout, line, lineX, lineY, color, letterSpacing);
    }
}

} // namespace infernux::textlayout
