#pragma once

#include "../GpuResidency.h"

#include "InxVkCoreModular.h"
#include "gui/EditorGuiFrameScheduler.h"
#include "gui/InxGUIContext.h"
#include "gui/InxGUIRenderable.h"
#include "gui/InxResourcePreviewer.h"

#include <SDL3/SDL.h>
#include <backends/imgui_impl_sdl3.h>
#include <backends/imgui_impl_vulkan.h>
#include <imgui.h>
#include <unordered_map>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux
{

namespace vk
{
class TextureUploadTicket;
} // namespace vk

class InxGUI
{
  public:
    InxGUI(InxVkCoreModular *vkCore);
    ~InxGUI();

    void Init(SDL_Window *window);

    void SetGUIFont(const char *fontPath, float fontSize);
    float GetDisplayScale() const
    {
        return m_dpiScale;
    }
    void BuildFrame();
    [[nodiscard]] bool BuildFrameIfDue(bool force = false);
    void RequestFrame() noexcept;
    [[nodiscard]] uint64_t GetFrameCounter() const noexcept
    {
        return m_guiFrameCounter;
    }
    [[nodiscard]] EditorGuiFrameScheduler::Snapshot GetFrameSchedulerSnapshot() const noexcept
    {
        return m_editorFrameScheduler.Inspect();
    }

    void RecordCommand(VkCommandBuffer cmdBuf);
    void Shutdown();

    void SetPlayerMode(bool enabled)
    {
        m_playerMode = enabled;
    }

    [[nodiscard]] const std::unordered_map<std::string, double> &GetLastPanelTimesMs() const
    {
        return m_lastPanelTimesMs;
    }

    [[nodiscard]] const std::vector<std::string> &GetRenderableOrder() const
    {
        return m_renderableOrder;
    }

    [[nodiscard]] const std::unordered_map<std::string, std::unordered_map<std::string, double>> &
    GetLastPanelSubTimesMs() const
    {
        return m_lastPanelSubTimesMs;
    }

    void Register(const std::string &name, std::shared_ptr<InxGUIRenderable> renderable, int priority = 0);
    void Unregister(const std::string &name);
    void QueueDockTabSelection(const std::string &windowId, bool allowDuringModal = false);

    /// @brief Submit texture data for asynchronous GPU upload.
    /// @param name Unique identifier for the texture
    /// @param pixels RGBA pixel data
    /// @param width Texture width
    /// @param height Texture height
    /// @param filter VK_FILTER_LINEAR (default) or VK_FILTER_NEAREST for point sampling
    /// @return A monotonic submission version. Poll GetImGuiTextureVersion before consuming a replacement.
    uint64_t SubmitTextureForImGui(const std::string &name, const unsigned char *pixels, size_t byteCount, int width,
                                   int height, VkFilter filter = VK_FILTER_LINEAR, bool pinned = false);

    /// @brief Remove a previously uploaded ImGui texture
    /// @param name Texture identifier
    void RemoveImGuiTexture(const std::string &name);

    /// @brief Check if a texture is already uploaded
    /// @param name Texture identifier
    /// @return true if texture exists
    bool HasImGuiTexture(const std::string &name) const;

    /// @brief Get texture ID for an already uploaded texture
    /// @param name Texture identifier
    /// @return Texture ID or 0 if not found
    uint64_t GetImGuiTextureId(const std::string &name);
    /// @brief Validate and mark a descriptor-backed ImGui texture as used by cached native UI commands.
    /// @return false when the descriptor is no longer owned by the live texture registry.
    bool TouchImGuiTextureId(uint64_t textureId);
    [[nodiscard]] uint64_t GetImGuiTextureVersion(const std::string &name) const;
    [[nodiscard]] uint64_t GetFailedImGuiTextureVersion(const std::string &name) const;

    void SetImGuiTextureBudgetBytes(uint64_t bytes);
    [[nodiscard]] size_t TrimImGuiTextureBudget();
    [[nodiscard]] uint64_t GetImGuiTextureBudgetBytes() const noexcept
    {
        return m_textureBudgetBytes;
    }
    [[nodiscard]] uint64_t GetImGuiTextureResidentBytes() const noexcept
    {
        return m_textureResidentBytes;
    }
    [[nodiscard]] size_t GetImGuiTextureEntryCount() const noexcept
    {
        return m_textures_umap.size();
    }
    [[nodiscard]] size_t GetPendingImGuiTextureUploadCount() const noexcept
    {
        return m_pendingTextureUploads.size();
    }
    [[nodiscard]] uint64_t GetPendingImGuiTextureUploadBytes() const noexcept
    {
        return m_pendingTextureUploadBytes;
    }
    [[nodiscard]] uint64_t GetSubmittedImGuiTextureUploadCount() const noexcept
    {
        return m_submittedTextureUploadCount;
    }
    [[nodiscard]] uint64_t GetCompletedImGuiTextureUploadCount() const noexcept
    {
        return m_completedTextureUploadCount;
    }
    [[nodiscard]] uint64_t GetAsyncImGuiTextureUploadCount() const noexcept
    {
        return m_asyncTextureUploadCount;
    }
    [[nodiscard]] uint64_t GetImGuiTextureEvictionCount() const noexcept
    {
        return m_textureEvictionCount;
    }
    [[nodiscard]] uint64_t GetScheduledTextureReleaseBytes() const noexcept;
    [[nodiscard]] GpuEvictionCandidate PeekOldestImGuiTextureEvictable() const noexcept;
    [[nodiscard]] uint64_t EvictOldestImGuiTexture();

    /// @brief Get the resource preview manager
    ResourcePreviewManager &GetResourcePreviewManager()
    {
        return m_resourcePreviewManager;
    }

  private:
    struct ImGuiTextureResource
    {
        std::shared_ptr<rhi::TextureResource> texture;
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
        uint64_t residentBytes = 0;
        uint64_t lastUsedFrame = 0;
        uint64_t uploadGeneration = 0;
        bool pinned = false;
    };

    struct DeferredTextureRelease
    {
        ImGuiTextureResource resource;
        uint64_t releaseFrame = 0;
    };

    struct PendingTextureUpload
    {
        std::string name;
        uint64_t generation = 0;
        bool pinned = false;
        std::shared_ptr<vk::TextureUploadTicket> ticket;
    };

    InxVkCoreModular *m_vkCore_ptr = nullptr;
    SDL_Window *m_window_ptr = nullptr;
    ImGuiContext *m_imguiContext_ptr = nullptr;
    float m_dpiScale = 1.0f;
    VkDescriptorPool m_descriptorPool_vk = VK_NULL_HANDLE;
    VkRenderPass m_imguiRenderPass = VK_NULL_HANDLE;

    std::unordered_map<std::string, std::shared_ptr<InxGUIRenderable>> m_renderables_umap;
    std::vector<std::string> m_renderableOrder;
    std::unordered_map<std::string, int> m_renderablePriorities;
    struct PendingDockTabSelection
    {
        std::string windowId;
        bool allowDuringModal = false;
    };
    std::vector<PendingDockTabSelection> m_pendingDockTabSelections;
    std::unordered_map<std::string, double> m_lastPanelTimesMs;
    std::unordered_map<std::string, std::unordered_map<std::string, double>> m_lastPanelSubTimesMs;
    std::unordered_map<std::string, ImGuiTextureResource> m_textures_umap;
    std::unordered_map<VkDescriptorSet, std::string> m_textureNamesByDescriptor;
    std::unordered_map<std::string, uint64_t> m_textureUploadGenerations;
    std::unordered_map<std::string, uint64_t> m_failedTextureUploadVersions;
    std::vector<PendingTextureUpload> m_pendingTextureUploads;
    std::vector<std::string> m_pendingTextureRemovals;
    std::vector<DeferredTextureRelease> m_deferredTextureReleases;
    uint64_t m_guiFrameCounter = 0;
    uint64_t m_textureBudgetBytes = 128ULL * 1024ULL * 1024ULL;
    uint64_t m_textureResidentBytes = 0;
    uint64_t m_pendingTextureUploadBytes = 0;
    uint64_t m_submittedTextureUploadCount = 0;
    uint64_t m_completedTextureUploadCount = 0;
    uint64_t m_asyncTextureUploadCount = 0;
    uint64_t m_textureEvictionCount = 0;
    ResourcePreviewManager m_resourcePreviewManager;
    bool m_playerMode = false;
    bool m_hasDrawData = false;
    EditorGuiFrameScheduler m_editorFrameScheduler;

    void BuildFrameInternal();
    void ApplyPendingDockTabSelections();
    void PromoteActiveModal();
    void PumpTextureUploads();
    void DeferTextureRelease(ImGuiTextureResource resource);
    void ReleaseTextureResource(ImGuiTextureResource &resource);
};

} // namespace infernux
