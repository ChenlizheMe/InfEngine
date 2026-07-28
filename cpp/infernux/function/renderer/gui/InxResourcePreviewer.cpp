#include "InxResourcePreviewer.h"
#include "InxGUI.h"
#include "MaterialPreviewRenderer.h"
#include <core/log/InxLog.h>
#include <function/renderer/InxRenderer.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetFormatRegistry.h>
#include <function/resources/AssetImporter/AssetImporter.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxTextureLoader.hpp>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxMesh/InxMesh.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <platform/filesystem/InxPath.h>
#include <sstream>
#include <variant>
#include <vector>

namespace infernux
{

// ============================================================================
// IResourcePreviewer
// ============================================================================

bool IResourcePreviewer::CanPreview(const std::string &filePath) const
{
    // Get extension in lowercase
    std::string ext;
    size_t dotPos = filePath.rfind('.');
    if (dotPos != std::string::npos) {
        ext = filePath.substr(dotPos);
        std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
    }

    auto supported = GetSupportedExtensions();
    return std::find(supported.begin(), supported.end(), ext) != supported.end();
}

// ============================================================================
// ResourcePreviewManager
// ============================================================================

ResourcePreviewManager::ResourcePreviewManager()
{
}

ResourcePreviewManager::~ResourcePreviewManager()
{
    UnloadPreview();
}

void ResourcePreviewManager::SetGUI(InxGUI *gui)
{
    m_gui = gui;

    // Register default previewers
    RegisterPreviewer(std::make_shared<ImagePreviewer>(gui));
    RegisterPreviewer(std::make_shared<TextPreviewer>());
    RegisterPreviewer(std::make_shared<MaterialPreviewer>(gui));
    RegisterPreviewer(std::make_shared<BinaryPreviewer>());
}

void ResourcePreviewManager::RegisterPreviewer(std::shared_ptr<IResourcePreviewer> previewer)
{
    m_previewers.push_back(previewer);

    // Map extensions to this previewer
    for (const auto &ext : previewer->GetSupportedExtensions()) {
        m_extensionMap[ext] = previewer.get();
    }

    std::string extList;
    for (const auto &e : previewer->GetSupportedExtensions()) {
        if (!extList.empty())
            extList += ", ";
        extList += e;
    }
    INXLOG_INFO("Registered ", previewer->GetTypeName(), " previewer for extensions: ", extList);
}

bool ResourcePreviewManager::HasPreviewer(const std::string &extension) const
{
    std::string ext = extension;
    std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
    if (!ext.empty() && ext[0] != '.') {
        ext = "." + ext;
    }
    return m_extensionMap.find(ext) != m_extensionMap.end();
}

std::string ResourcePreviewManager::GetPreviewerTypeName(const std::string &extension) const
{
    std::string ext = extension;
    std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
    if (!ext.empty() && ext[0] != '.') {
        ext = "." + ext;
    }

    auto it = m_extensionMap.find(ext);
    if (it != m_extensionMap.end()) {
        return it->second->GetTypeName();
    }
    return "";
}

std::vector<std::string> ResourcePreviewManager::GetAllSupportedExtensions() const
{
    std::vector<std::string> result;
    for (const auto &pair : m_extensionMap) {
        result.push_back(pair.first);
    }
    std::sort(result.begin(), result.end());
    return result;
}

std::string ResourcePreviewManager::GetExtension(const std::string &filePath) const
{
    size_t dotPos = filePath.rfind('.');
    if (dotPos != std::string::npos) {
        std::string ext = filePath.substr(dotPos);
        std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
        return ext;
    }
    return "";
}

IResourcePreviewer *ResourcePreviewManager::FindPreviewer(const std::string &filePath) const
{
    std::string ext = GetExtension(filePath);
    auto it = m_extensionMap.find(ext);
    if (it != m_extensionMap.end()) {
        return it->second;
    }
    return nullptr;
}

bool ResourcePreviewManager::LoadPreview(const std::string &filePath)
{
    // Find appropriate previewer
    IResourcePreviewer *previewer = FindPreviewer(filePath);
    if (!previewer) {
        INXLOG_WARN("No previewer found for: ", filePath);
        return false;
    }

    // Unload previous if different previewer
    if (m_currentPreviewer && m_currentPreviewer != previewer) {
        m_currentPreviewer->Unload();
    }

    // Load with new previewer
    m_currentPreviewer = previewer;
    m_currentPreviewer->SetPreviewSettings(m_displayMode, m_maxSize, m_srgb);
    if (!m_currentPreviewer->Load(filePath)) {
        INXLOG_ERROR("Failed to load preview for: ", filePath);
        m_currentPreviewer = nullptr;
        return false;
    }

    return true;
}

void ResourcePreviewManager::RenderPreview(InxGUIContext *ctx, float availWidth, float availHeight)
{
    if (m_currentPreviewer && m_currentPreviewer->IsLoaded()) {
        m_currentPreviewer->Render(ctx, availWidth, availHeight);
    }
}

void ResourcePreviewManager::RenderMetadata(InxGUIContext *ctx)
{
    if (m_currentPreviewer && m_currentPreviewer->IsLoaded()) {
        auto metadata = m_currentPreviewer->GetMetadata();
        for (const auto &[key, value] : metadata) {
            ctx->Label(key + ": " + value);
        }
    }
}

void ResourcePreviewManager::UnloadPreview()
{
    if (m_currentPreviewer) {
        m_currentPreviewer->Unload();
        m_currentPreviewer = nullptr;
    }
}

bool ResourcePreviewManager::IsPreviewLoaded() const
{
    return m_currentPreviewer && m_currentPreviewer->IsLoaded();
}

std::string ResourcePreviewManager::GetLoadedPath() const
{
    if (m_currentPreviewer) {
        return m_currentPreviewer->GetLoadedPath();
    }
    return "";
}

std::string ResourcePreviewManager::GetCurrentTypeName() const
{
    if (m_currentPreviewer) {
        return m_currentPreviewer->GetTypeName();
    }
    return "";
}

void ResourcePreviewManager::SetPreviewSettings(int displayMode, int maxSize, bool srgb)
{
    m_displayMode = (displayMode == 1) ? PreviewDisplayMode::NormalMap : PreviewDisplayMode::Default;
    m_maxSize = maxSize;
    m_srgb = srgb;
    if (m_currentPreviewer) {
        m_currentPreviewer->SetPreviewSettings(m_displayMode, m_maxSize, m_srgb);
    }
}

// ============================================================================
// ImagePreviewer
// ============================================================================

ImagePreviewer::ImagePreviewer(InxGUI *gui) : m_gui(gui)
{
}

ImagePreviewer::~ImagePreviewer()
{
    Unload();
}

std::vector<std::string> ImagePreviewer::GetSupportedExtensions() const
{
    return asset_formats::ToVector(asset_formats::kTextureExtensions);
}

bool ImagePreviewer::Load(const std::string &filePath)
{
    if (m_loadedPath == filePath) {
        return true; // Already loaded
    }

    Unload();

    // Get file size
    try {
        m_fileSize = std::filesystem::file_size(ToFsPath(filePath));
    } catch (...) {
        m_fileSize = 0;
    }

    // Load texture using InxTextureLoader
    InxTextureData texData = InxTextureLoader::LoadFromFile(filePath);
    if (!texData.IsValid()) {
        INXLOG_ERROR("Failed to load image: ", filePath);
        return false;
    }

    m_width = texData.width;
    m_height = texData.height;
    m_channels = texData.channels;
    m_originalPixels = texData.pixels; // keep a copy for display-mode re-processing

    m_loadedPath = filePath;
    ApplyPreviewSettings();

    INXLOG_INFO("Loaded image preview: ", filePath, " (", m_width, "x", m_height, ")");
    return true;
}

void ImagePreviewer::Render(InxGUIContext *ctx, float availWidth, float availHeight)
{
    m_textureId = m_gui->GetImGuiTextureId(m_textureName);
    if (m_textureId == 0)
        return;

    // Calculate image size to fit in available space while preserving aspect ratio
    float imgAspect = static_cast<float>(m_width) / static_cast<float>(m_height);
    float areaAspect = availWidth / availHeight;

    float drawWidth, drawHeight;
    if (imgAspect > areaAspect) {
        // Image is wider - fit by width
        drawWidth = availWidth;
        drawHeight = availWidth / imgAspect;
    } else {
        // Image is taller - fit by height
        drawHeight = availHeight;
        drawWidth = availHeight * imgAspect;
    }

    // Center the image
    float offsetX = (availWidth - drawWidth) * 0.5f;
    float offsetY = (availHeight - drawHeight) * 0.5f;

    float cursorX = ctx->GetCursorPosX();
    float cursorY = ctx->GetCursorPosY();

    ctx->SetCursorPosX(cursorX + offsetX);
    ctx->SetCursorPosY(cursorY + offsetY);
    ctx->Image(reinterpret_cast<void *>(m_textureId), drawWidth, drawHeight);
}

void ImagePreviewer::Unload()
{
    if (m_gui && !m_textureName.empty())
        m_gui->RemoveImGuiTexture(m_textureName);
    m_textureId = 0;
    m_textureName.clear();
    m_loadedPath.clear();
    m_width = 0;
    m_height = 0;
    m_channels = 0;
    m_fileSize = 0;
    m_originalPixels.clear();
    m_displayMode = PreviewDisplayMode::Default;
    m_maxSize = 0;
    m_srgb = true;
}

void ImagePreviewer::SetPreviewSettings(PreviewDisplayMode mode, int maxSize, bool srgb)
{
    if (m_displayMode == mode && m_maxSize == maxSize && m_srgb == srgb)
        return;
    m_displayMode = mode;
    m_maxSize = maxSize;
    m_srgb = srgb;
    if (!m_originalPixels.empty() && m_width > 0 && m_height > 0) {
        ApplyPreviewSettings();
    }
}

void ImagePreviewer::ApplyPreviewSettings()
{
    int procW = m_width;
    int procH = m_height;
    std::vector<unsigned char> processed;

    // --- Step 1: display mode (normal-map conversion) ---
    if (m_displayMode == PreviewDisplayMode::NormalMap) {
        const int w = m_width;
        const int h = m_height;
        const float strength = 2.0f;
        processed.resize(static_cast<size_t>(w) * h * 4);

        auto getGray = [&](int px, int py) -> float {
            px = std::clamp(px, 0, w - 1);
            py = std::clamp(py, 0, h - 1);
            size_t idx = (static_cast<size_t>(py) * w + px) * 4;
            return (m_originalPixels[idx] * 0.299f + m_originalPixels[idx + 1] * 0.587f +
                    m_originalPixels[idx + 2] * 0.114f) /
                   255.0f;
        };

        for (int y = 0; y < h; ++y) {
            for (int x = 0; x < w; ++x) {
                float hl = getGray(x - 1, y);
                float hr = getGray(x + 1, y);
                float hu = getGray(x, y - 1);
                float hd = getGray(x, y + 1);

                float nx = (hl - hr) * strength;
                float ny = (hu - hd) * strength;
                float nz = 1.0f;

                float len = std::sqrt(nx * nx + ny * ny + nz * nz);
                nx /= len;
                ny /= len;
                nz /= len;

                size_t idx = (static_cast<size_t>(y) * w + x) * 4;
                processed[idx + 0] = static_cast<unsigned char>((nx * 0.5f + 0.5f) * 255.0f);
                processed[idx + 1] = static_cast<unsigned char>((ny * 0.5f + 0.5f) * 255.0f);
                processed[idx + 2] = static_cast<unsigned char>((nz * 0.5f + 0.5f) * 255.0f);
                processed[idx + 3] = 255;
            }
        }
    } else {
        processed = m_originalPixels;
    }

    // --- Step 2: max-size down-scale (bilinear) ---
    if (m_maxSize > 0 && (procW > m_maxSize || procH > m_maxSize)) {
        float scale = static_cast<float>(m_maxSize) / static_cast<float>(std::max(procW, procH));
        int dstW = std::max(1, static_cast<int>(procW * scale));
        int dstH = std::max(1, static_cast<int>(procH * scale));
        std::vector<unsigned char> resized(static_cast<size_t>(dstW) * dstH * 4);

        for (int dy = 0; dy < dstH; ++dy) {
            for (int dx = 0; dx < dstW; ++dx) {
                float srcXf = (dx + 0.5f) * procW / static_cast<float>(dstW) - 0.5f;
                float srcYf = (dy + 0.5f) * procH / static_cast<float>(dstH) - 0.5f;

                int x0 = std::clamp(static_cast<int>(std::floor(srcXf)), 0, procW - 1);
                int y0 = std::clamp(static_cast<int>(std::floor(srcYf)), 0, procH - 1);
                int x1 = std::clamp(x0 + 1, 0, procW - 1);
                int y1 = std::clamp(y0 + 1, 0, procH - 1);

                float fx = srcXf - std::floor(srcXf);
                float fy = srcYf - std::floor(srcYf);

                for (int c = 0; c < 4; ++c) {
                    float v00 = processed[(static_cast<size_t>(y0) * procW + x0) * 4 + c];
                    float v10 = processed[(static_cast<size_t>(y0) * procW + x1) * 4 + c];
                    float v01 = processed[(static_cast<size_t>(y1) * procW + x0) * 4 + c];
                    float v11 = processed[(static_cast<size_t>(y1) * procW + x1) * 4 + c];
                    float v = v00 * (1 - fx) * (1 - fy) + v10 * fx * (1 - fy) + v01 * (1 - fx) * fy + v11 * fx * fy;
                    resized[(static_cast<size_t>(dy) * dstW + dx) * 4 + c] =
                        static_cast<unsigned char>(std::clamp(v, 0.0f, 255.0f));
                }
            }
        }
        processed = std::move(resized);
        procW = dstW;
        procH = dstH;
    }

    // --- Step 3: sRGB toggle ---
    // sRGB sources are already display-encoded and can be shown as-is. Linear
    // data textures need display encoding because the ImGui upload itself is UNORM.
    if (!m_srgb) {
        for (size_t i = 0; i < processed.size(); i += 4) {
            for (int c = 0; c < 3; ++c) {
                float v = processed[i + c] / 255.0f;
                v = (v <= 0.0031308f) ? v * 12.92f : 1.055f * std::pow(v, 1.0f / 2.4f) - 0.055f;
                processed[i + c] = static_cast<unsigned char>(std::clamp(v * 255.0f, 0.0f, 255.0f));
            }
        }
    }

    // --- Upload to ImGui ---
    const std::string textureName = m_loadedPath + "::prev_" + std::to_string(static_cast<int>(m_displayMode)) + "_" +
                                    std::to_string(m_maxSize) + "_" + (m_srgb ? "1" : "0");
    if (!m_textureName.empty() && m_textureName != textureName)
        m_gui->RemoveImGuiTexture(m_textureName);
    m_textureName = textureName;
    (void)m_gui->SubmitTextureForImGui(m_textureName, processed.data(), processed.size(), procW, procH);
    m_textureId = m_gui->GetImGuiTextureId(m_textureName);
}

std::vector<std::pair<std::string, std::string>> ImagePreviewer::GetMetadata() const
{
    std::vector<std::pair<std::string, std::string>> result;
    result.emplace_back("Type", "Image");
    result.emplace_back("Width", std::to_string(m_width) + " px");
    result.emplace_back("Height", std::to_string(m_height) + " px");
    result.emplace_back("Channels", std::to_string(m_channels));

    // Format file size
    std::string sizeStr;
    if (m_fileSize < 1024) {
        sizeStr = std::to_string(m_fileSize) + " B";
    } else if (m_fileSize < 1024 * 1024) {
        sizeStr = std::to_string(m_fileSize / 1024) + " KB";
    } else {
        sizeStr = std::to_string(m_fileSize / (1024 * 1024)) + " MB";
    }
    result.emplace_back("Size", sizeStr);

    return result;
}

// ============================================================================
// TextPreviewer
// ============================================================================

TextPreviewer::TextPreviewer()
{
}

std::vector<std::string> TextPreviewer::GetSupportedExtensions() const
{
    return {".txt", ".md",   ".json", ".xml",  ".yaml", ".yml",  ".ini",  ".cfg",       ".conf",        ".log",
            ".csv", ".html", ".htm",  ".css",  ".js",   ".ts",   ".py",   ".cpp",       ".c",           ".h",
            ".hpp", ".java", ".cs",   ".go",   ".rs",   ".rb",   ".php",  ".lua",       ".sh",          ".bat",
            ".ps1", ".glsl", ".hlsl", ".vert", ".frag", ".toml", ".lock", ".gitignore", ".editorconfig"};
}

bool TextPreviewer::Load(const std::string &filePath)
{
    if (m_loadedPath == filePath && m_loaded) {
        return true;
    }

    Unload();

    // Get file size
    try {
        m_fileSize = std::filesystem::file_size(ToFsPath(filePath));
    } catch (...) {
        return false;
    }

    // Read file content
    std::ifstream file(ToFsPath(filePath), std::ios::binary);
    if (!file.is_open()) {
        INXLOG_ERROR("Failed to open text file: ", filePath);
        return false;
    }

    // Read up to MAX_PREVIEW_SIZE bytes
    size_t readSize = (std::min)(m_fileSize, MAX_PREVIEW_SIZE);
    m_content.resize(readSize);
    file.read(m_content.data(), readSize);
    m_truncated = m_fileSize > MAX_PREVIEW_SIZE;

    // Split into lines for display
    m_lines.clear();
    std::istringstream stream(m_content);
    std::string line;
    while (std::getline(stream, line)) {
        m_lines.push_back(line);
    }
    m_lineCount = m_lines.size();
    if (m_truncated) {
        // Estimate total lines
        m_lineCount = static_cast<size_t>(m_lineCount * (static_cast<double>(m_fileSize) / readSize));
    }

    m_loadedPath = filePath;
    m_loaded = true;
    INXLOG_INFO("Loaded text preview: ", filePath, " (", m_lines.size(), " lines)");
    return true;
}

void TextPreviewer::Render(InxGUIContext *ctx, float availWidth, float availHeight)
{
    // Use a child window for scrolling
    if (ctx->BeginChild("TextPreview", availWidth, availHeight, false)) {
        for (const auto &line : m_lines) {
            ctx->Label(line);
        }
        if (m_truncated) {
            ctx->Separator();
            ctx->Label("... (File truncated, showing first 64KB)");
        }
    }
    ctx->EndChild();
}

void TextPreviewer::Unload()
{
    m_loadedPath.clear();
    m_content.clear();
    m_lines.clear();
    m_fileSize = 0;
    m_lineCount = 0;
    m_truncated = false;
    m_loaded = false;
}

std::vector<std::pair<std::string, std::string>> TextPreviewer::GetMetadata() const
{
    std::vector<std::pair<std::string, std::string>> result;

    // Get extension for type
    size_t dotPos = m_loadedPath.rfind('.');
    std::string ext = dotPos != std::string::npos ? m_loadedPath.substr(dotPos) : "";
    result.emplace_back("Type", "Text (" + ext + ")");
    result.emplace_back("Lines", std::to_string(m_lineCount) + (m_truncated ? "+" : ""));

    // Format file size
    std::string sizeStr;
    if (m_fileSize < 1024) {
        sizeStr = std::to_string(m_fileSize) + " B";
    } else if (m_fileSize < 1024 * 1024) {
        sizeStr = std::to_string(m_fileSize / 1024) + " KB";
    } else {
        sizeStr = std::to_string(m_fileSize / (1024 * 1024)) + " MB";
    }
    result.emplace_back("Size", sizeStr);

    return result;
}

// ============================================================================
// BinaryPreviewer
// ============================================================================

std::vector<std::string> BinaryPreviewer::GetSupportedExtensions() const
{
    // Common binary files that we know exist but can't preview yet
    return {".exe", ".dll", ".so",  ".a",   ".lib",  ".obj", ".o",    ".pdb", ".zip",
            ".tar", ".gz",  ".7z",  ".rar", ".mp3",  ".wav", ".ogg",  ".mp4", ".avi",
            ".mov", ".mkv", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"};
}

bool BinaryPreviewer::Load(const std::string &filePath)
{
    if (m_loadedPath == filePath && m_loaded) {
        return true;
    }

    Unload();

    try {
        m_fileSize = std::filesystem::file_size(ToFsPath(filePath));
    } catch (...) {
        return false;
    }

    m_loadedPath = filePath;
    m_loaded = true;
    return true;
}

void BinaryPreviewer::Render(InxGUIContext *ctx, float availWidth, float availHeight)
{
    ctx->Label("Binary file - preview not available");
    ctx->Label("Use an external program to open this file.");
}

void BinaryPreviewer::Unload()
{
    m_loadedPath.clear();
    m_fileSize = 0;
    m_loaded = false;
}

std::vector<std::pair<std::string, std::string>> BinaryPreviewer::GetMetadata() const
{
    std::vector<std::pair<std::string, std::string>> result;

    // Get extension for type
    size_t dotPos = m_loadedPath.rfind('.');
    std::string ext = dotPos != std::string::npos ? m_loadedPath.substr(dotPos) : "";
    result.emplace_back("Type", "Binary (" + ext + ")");

    // Format file size
    std::string sizeStr;
    if (m_fileSize < 1024) {
        sizeStr = std::to_string(m_fileSize) + " B";
    } else if (m_fileSize < 1024 * 1024) {
        sizeStr = std::to_string(m_fileSize / 1024) + " KB";
    } else {
        sizeStr = std::to_string(m_fileSize / (1024 * 1024)) + " MB";
    }
    result.emplace_back("Size", sizeStr);

    return result;
}

// ============================================================================
// MaterialPreviewer
// ============================================================================

/// Drop texture bindings whose GUID does not resolve to an on-disk file so GPU/CPU
/// preview still draws (albedo samples as white when a slot is empty).
static void ClearMissingTextureBindings(InxMaterial *mat, AssetDatabase *adb)
{
    if (!mat || !adb)
        return;
    namespace fs = std::filesystem;
    std::vector<std::string> toClear;
    for (const auto &kv : mat->GetAllProperties()) {
        if (kv.second.type != MaterialPropertyType::Texture2D)
            continue;
        if (!std::holds_alternative<std::string>(kv.second.value))
            continue;
        const std::string &guid = std::get<std::string>(kv.second.value);
        if (guid.empty())
            continue;
        const std::string p = adb->GetPathFromGuid(guid);
        if (p.empty()) {
            toClear.push_back(kv.first);
            continue;
        }
        std::error_code ec;
        if (!fs::exists(fs::u8path(p), ec))
            toClear.push_back(kv.first);
    }
    for (const auto &name : toClear)
        mat->ClearTexture(name);
}

/// Downsample RGBA pixels to target size using box filter.
static void DownsampleRGBA(const unsigned char *src, int srcW, int srcH, int dstW, int dstH,
                           std::vector<unsigned char> &dst)
{
    dst.resize(static_cast<size_t>(dstW) * dstH * 4);
    float scaleX = static_cast<float>(srcW) / dstW;
    float scaleY = static_cast<float>(srcH) / dstH;

    for (int dy = 0; dy < dstH; ++dy) {
        for (int dx = 0; dx < dstW; ++dx) {
            int sx = static_cast<int>((dx + 0.5f) * scaleX);
            int sy = static_cast<int>((dy + 0.5f) * scaleY);
            sx = std::clamp(sx, 0, srcW - 1);
            sy = std::clamp(sy, 0, srcH - 1);
            size_t srcIdx = (static_cast<size_t>(sy) * srcW + sx) * 4;
            size_t dstIdx = (static_cast<size_t>(dy) * dstW + dx) * 4;
            dst[dstIdx + 0] = src[srcIdx + 0];
            dst[dstIdx + 1] = src[srcIdx + 1];
            dst[dstIdx + 2] = src[srcIdx + 2];
            dst[dstIdx + 3] = src[srcIdx + 3];
        }
    }
}

/// Build a TextureResolver that uses AssetDatabase to resolve GUIDs → file paths → pixels.
static PreviewTextureResolver MakeTextureResolver(AssetDatabase *adb)
{
    if (!adb)
        return nullptr;

    return [adb](const std::string &guid) -> PreviewTexture {
        std::string texPath = adb->GetPathFromGuid(guid);
        if (texPath.empty())
            return {};

        InxTextureData texData = InxTextureLoader::LoadFromFile(texPath);
        if (!texData.IsValid())
            return {};

        // Downsample to 32x32 for fast sphere sampling
        constexpr int kPreviewTexSize = 32;
        PreviewTexture result;
        result.width = kPreviewTexSize;
        result.height = kPreviewTexSize;

        if (texData.width <= kPreviewTexSize && texData.height <= kPreviewTexSize) {
            result.pixels = std::move(texData.pixels);
            result.width = texData.width;
            result.height = texData.height;
        } else {
            DownsampleRGBA(texData.pixels.data(), texData.width, texData.height, kPreviewTexSize, kPreviewTexSize,
                           result.pixels);
        }
        return result;
    };
}

MaterialPreviewer::MaterialPreviewer(InxGUI *gui, int previewSize) : m_gui(gui), m_previewSize(previewSize)
{
}

MaterialPreviewer::~MaterialPreviewer()
{
    Unload();
}

std::vector<std::string> MaterialPreviewer::GetSupportedExtensions() const
{
    return {".mat"};
}

bool MaterialPreviewer::Load(const std::string &filePath)
{
    if (m_loadedPath == filePath) {
        return true; // Already loaded
    }

    Unload();

    // Read and deserialize the .mat JSON
    std::ifstream file(ToFsPath(filePath));
    if (!file.is_open()) {
        INXLOG_ERROR("MaterialPreviewer: cannot open '", filePath, "'");
        return false;
    }
    std::string jsonStr((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();

    InxMaterial material;
    if (!material.Deserialize(jsonStr)) {
        INXLOG_ERROR("MaterialPreviewer: deserialization failed for '", filePath, "'");
        return false;
    }

    m_materialName = material.GetName();
    m_shaderName = material.GetShaderName();

    // CPU-render the PBR sphere
    std::vector<unsigned char> pixels;
    MaterialPreviewRenderer::RenderPreview(material, m_previewSize, pixels);

    // Upload to ImGui
    m_loadedPath = filePath;
    m_textureName = filePath + "::mat_preview";
    (void)m_gui->SubmitTextureForImGui(m_textureName, pixels.data(), pixels.size(), m_previewSize, m_previewSize);
    m_textureId = m_gui->GetImGuiTextureId(m_textureName);
    INXLOG_INFO("MaterialPreviewer: loaded '", filePath, "'");
    return true;
}

void MaterialPreviewer::Render(InxGUIContext *ctx, float availWidth, float availHeight)
{
    m_textureId = m_gui->GetImGuiTextureId(m_textureName);
    if (m_textureId == 0)
        return;

    // Draw the square sphere preview centered in the available area
    float drawSize = std::min(availWidth, availHeight);
    float offsetX = (availWidth - drawSize) * 0.5f;
    float offsetY = (availHeight - drawSize) * 0.5f;

    float cursorX = ctx->GetCursorPosX();
    float cursorY = ctx->GetCursorPosY();

    ctx->SetCursorPosX(cursorX + offsetX);
    ctx->SetCursorPosY(cursorY + offsetY);
    ctx->Image(reinterpret_cast<void *>(m_textureId), drawSize, drawSize);
}

void MaterialPreviewer::Unload()
{
    if (m_gui && !m_textureName.empty())
        m_gui->RemoveImGuiTexture(m_textureName);
    m_textureId = 0;
    m_textureName.clear();
    m_loadedPath.clear();
    m_materialName.clear();
    m_shaderName.clear();
}

std::vector<std::pair<std::string, std::string>> MaterialPreviewer::GetMetadata() const
{
    std::vector<std::pair<std::string, std::string>> result;
    result.emplace_back("Type", "Material");
    if (!m_materialName.empty())
        result.emplace_back("Name", m_materialName);
    if (!m_shaderName.empty())
        result.emplace_back("Shader", m_shaderName);
    return result;
}

std::shared_ptr<InxMaterial> MaterialPreviewer::BuildPreviewMaterialFromFile(const std::string &matFilePath,
                                                                             AssetDatabase *adb)
{
    std::ifstream file(ToFsPath(matFilePath));
    if (!file.is_open())
        return nullptr;

    std::string jsonStr((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    return BuildPreviewMaterialFromJson(jsonStr, adb);
}

std::shared_ptr<InxMaterial> MaterialPreviewer::BuildPreviewMaterialFromJson(const std::string &materialJson,
                                                                             AssetDatabase *adb)
{
    if (materialJson.empty())
        return nullptr;

    auto sourceMaterial = std::make_shared<InxMaterial>();
    if (!sourceMaterial->Deserialize(materialJson))
        return nullptr;
    ClearMissingTextureBindings(sourceMaterial.get(), adb);
    return sourceMaterial->Clone();
}

void MaterialPreviewer::RenderCpuPreview(const std::shared_ptr<InxMaterial> &material, int size,
                                         std::vector<unsigned char> &outPixels, AssetDatabase *adb)
{
    if (!material || size <= 0) {
        outPixels.clear();
        return;
    }
    PreviewTextureResolver resolver = MakeTextureResolver(adb);
    std::string fragId = material->GetFragShaderName();
    ShaderPreviewMapping mapping = GetShaderPreviewMapping(fragId, adb);
    const ShaderPreviewMapping *pMapping = mapping.IsEmpty() ? nullptr : &mapping;
    MaterialPreviewRenderer::RenderPreview(*material, size, outPixels, resolver, pMapping);
}

std::shared_ptr<InxMaterial> MaterialPreviewer::BuildEmbeddedPreviewMaterial(const std::string &modelPath,
                                                                             uint32_t slotIndex)
{
    if (modelPath.empty())
        return nullptr;

    auto mesh = AssetRegistry::Instance().LoadAssetByPath<InxMesh>(modelPath, ResourceType::Mesh);
    if (!mesh)
        return nullptr;

    const auto &slotDataVec = mesh->GetMaterialSlotData();
    if (slotIndex >= static_cast<uint32_t>(slotDataVec.size()))
        return nullptr;

    const auto &sd = slotDataVec[slotIndex];
    auto defaultMat = AssetRegistry::Instance().GetBuiltinMaterial("DefaultLit");
    auto mat = defaultMat ? defaultMat->Clone() : InxMaterial::CreateDefaultLit();
    if (!mat)
        return nullptr;
    mat->SetColor("baseColor", sd.baseColor);
    mat->SetColor("emissionColor", sd.emissionColor);
    mat->SetFloat("metallic", sd.metallic);
    mat->SetFloat("smoothness", sd.smoothness);

    return mat;
}

} // namespace infernux
