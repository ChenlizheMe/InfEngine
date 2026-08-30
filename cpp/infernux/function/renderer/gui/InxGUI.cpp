#include "InxGUI.h"
#include "../ProfileConfig.h"
#include "ImGuiVulkanExtensions.h"
#include "InxGUIContext.h"
#include "InxGUISemantics.h"
#include <function/editor/EditorTheme.h>
#include <function/editor/EditorThemeRegistry.h>
#include <function/renderer/TextureUploadBuilder.h>
#include <function/renderer/vk/RhiVulkanTypes.h>
#include <function/renderer/vk/VkRenderUtils.h>
#include <function/renderer/vk/VkResourceManager.h>
#include <function/resources/InxTexture/TextureDecoder.h>

#include <SDL3/SDL.h>
#include <algorithm>
#include <backends/imgui_impl_sdl3.h>
#include <backends/imgui_impl_vulkan.h>
#include <chrono>
#include <cmath>
#include <core/log/InxLog.h>
#include <imgui.h>
#include <imgui_internal.h>
#include <limits>
#include <memory>
#include <platform/input/InputManager.h>
#include <stdexcept>
#include <string>

namespace infernux
{
namespace
{

struct EditorDpiState
{
    ImGuiStyle baseStyle;
    std::string fontPath;
    float fontSize = 18.0f;
};

EditorDpiState &GetEditorDpiState()
{
    static EditorDpiState state;
    return state;
}

class ImGuiBuildFrameGuard
{
  public:
    ImGuiBuildFrameGuard() = default;
    ImGuiBuildFrameGuard(const ImGuiBuildFrameGuard &) = delete;
    ImGuiBuildFrameGuard &operator=(const ImGuiBuildFrameGuard &) = delete;

    ~ImGuiBuildFrameGuard()
    {
        if (!m_active)
            return;

        InxGUISemantics::AbortFrame();
        ImGuiContext *context = ImGui::GetCurrentContext();
        if (context != nullptr && context->WithinFrameScope)
            ImGui::EndFrame();
    }

    void Complete()
    {
        InxGUISemantics::EndFrame();
        m_active = false;
    }

  private:
    bool m_active = true;
};

void BringDockTreeToDisplayFront(ImGuiWindow *window)
{
    if (window == nullptr)
        return;

    ImGuiWindow *root = window->RootWindowDockTree != nullptr ? window->RootWindowDockTree : window;
    ImGuiContext &imgui = *ImGui::GetCurrentContext();

    // Dear ImGui's BringWindowToDisplayFront() moves only the supplied root
    // pointer. A dock tree is represented by several entries in g.Windows;
    // moving only its root destroys their established relative order and can
    // leave a DockNode host above a sibling such as the editor toolbar. Move
    // the complete presentation group instead, preserving its internal order.
    std::stable_partition(imgui.Windows.begin(), imgui.Windows.end(), [root](ImGuiWindow *candidate) {
        return candidate == nullptr || candidate->RootWindowDockTree != root;
    });
}

void ConfigureEditorStyleDimensions(ImGuiStyle &style)
{
    style.WindowPadding = ImVec2(10.0f, 10.0f);
    style.FramePadding = ImVec2(8.0f, 3.0f);
    style.CellPadding = ImVec2(4.0f, 4.0f);
    style.ItemSpacing = ImVec2(8.0f, 6.0f);
    style.ItemInnerSpacing = ImVec2(6.0f, 4.0f);
    style.IndentSpacing = 18.0f;
    style.ScrollbarSize = 8.0f;
    style.GrabMinSize = 6.0f;

    style.WindowBorderSize = 1.0f;
    style.ChildBorderSize = 1.0f;
    style.PopupBorderSize = 1.0f;
    style.FrameBorderSize = 1.0f;
    style.TabBorderSize = 0.0f;
    style.TabBarBorderSize = 1.0f;

    style.WindowRounding = 0.0f;
    style.ChildRounding = 0.0f;
    style.FrameRounding = 0.0f;
    style.PopupRounding = 0.0f;
    style.ScrollbarRounding = 0.0f;
    style.GrabRounding = 0.0f;
    style.TabRounding = 0.0f;

    style.AntiAliasedLines = true;
    style.AntiAliasedFill = true;
}

} // namespace

InxGUI::InxGUI(InxVkCoreModular *vkCore) : m_vkCore_ptr(vkCore)
{
}

InxGUI::~InxGUI()
{
    Shutdown();

    ImGui::DestroyContext(m_imguiContext_ptr);
    m_imguiContext_ptr = nullptr;
}

void InxGUI::Init(SDL_Window *window)
{
    m_window_ptr = window;
    GetEditorDpiState() = {};

    // Detect display DPI scale (e.g. 2.0 for 200% Windows scaling)
    m_dpiScale = SDL_GetWindowDisplayScale(window);
    if (m_dpiScale <= 0.0f)
        m_dpiScale = 1.0f;
    InxGUIContext::s_dpiScale = m_dpiScale;
    INXLOG_DEBUG("Display scale: ", m_dpiScale);

    IMGUI_CHECKVERSION();
    m_imguiContext_ptr = ImGui::CreateContext();
    ImGui::SetCurrentContext(m_imguiContext_ptr);
    m_imguiContext_ptr->ErrorCallback = [](ImGuiContext *context, void *, const char *message) {
        static thread_local bool handlingError = false;
        if (handlingError)
            return;
        handlingError = true;
        const char *windowName =
            context != nullptr && context->CurrentWindow != nullptr ? context->CurrentWindow->Name : "<no window>";
        INXLOG_ERROR("[ImGui] [Window: ", windowName, "] ", message ? message : "unknown recoverable error");
        handlingError = false;
    };
    ImGui::StyleColorsDark();

    // =========================================================================
    // Notion-style dark theme — matches launcher palette (style.py)
    // bg_base=#191919  bg_surface=#202020  bg_hover=#2a2a2a
    // bg_selected=#333333  border=#2f2f2f  text=#cfcfcf
    // text_secondary=#707070  text_muted=#555555  accent=white
    //
    // NOTE: Swapchain is VK_FORMAT_B8G8R8A8_UNORM — no hardware sRGB
    // encoding. ImGui colours are already in display (sRGB) space and
    // written directly to the framebuffer.
    // =========================================================================
    {
        ImGuiStyle &style = ImGui::GetStyle();
        // Editor palette is composed from the active theme (single source of
        // truth: EditorThemeRegistry / EditorThemeTable.inl). This themes every
        // built-in widget at once — C++ AND Python panels — and is what theme
        // switching re-applies. See EditorThemeRegistry::ApplyImGuiColors().
        EditorThemeRegistry::ApplyImGuiColors();

        // =====================================================================
        // Style dimensions — Notion-style clean, modern spacing
        // =====================================================================
        ConfigureEditorStyleDimensions(style);
        GetEditorDpiState().baseStyle = style;

        // Scale all style dimensions for high-DPI displays
        if (std::abs(m_dpiScale - 1.0f) >= 0.01f) {
            style.ScaleAllSizes(m_dpiScale);
        }
    }

    ImGuiIO &io = ImGui::GetIO();
    // Python panel callbacks may throw through an unfinished ImGui window.
    // Recover the stack and report through the engine console without opening
    // ImGui's own error tooltip, which can recursively fail on the same stack.
    io.ConfigErrorRecovery = true;
    io.ConfigErrorRecoveryEnableAssert = false;
    io.ConfigErrorRecoveryEnableDebugLog = false;
    io.ConfigErrorRecoveryEnableTooltip = false;
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    io.ConfigFlags |= ImGuiConfigFlags_DockingEnable; // Enable Docking
    // io.ConfigFlags |= ImGuiConfigFlags_ViewportsEnable; // Enable Multi-Viewport (optional, can cause issues)

    // Docking configuration
    io.ConfigDockingWithShift = false;    // Dock without holding shift
    io.ConfigDockingAlwaysTabBar = true;  // Always show tab bar for docked windows
    io.ConfigDragClickToInputText = true; // Single click-release on DragFloat → text input
    // Graph nodes and other canvas tools use draw-list hit testing rather than
    // native ImGui items. Restrict floating-window movement to the title bar
    // so those content gestures cannot also move their parent window.
    io.ConfigWindowsMoveFromTitleBarOnly = true;

    ImGui_ImplSDL3_InitForVulkan(window);

    VkDevice device = m_vkCore_ptr->GetDevice();
    const auto &deviceContext = m_vkCore_ptr->GetDeviceContext();
    const bool dynamicCommandsAvailable = rhi::ResolveDynamicRenderingCommands(device).IsValid();
    const bool useDynamicRendering = rhi::SelectDynamicRenderingPath(
        deviceContext.GetRhiDevice().GetCapabilityState().dynamicRendering.enabled, dynamicCommandsAvailable, false);
    m_descriptorPool_vk = m_vkCore_ptr->GetDeviceContext().GetRhiDevice().GetDescriptorManager().AcquireExternalPool(
        vk::DescriptorArena::ImGuiExternal);
    if (m_descriptorPool_vk == VK_NULL_HANDLE) {
        INXLOG_FATAL("Failed to create descriptor pool for ImGui.");
        return;
    }

    // Legacy fallback for devices without dynamic rendering.
    if (!useDynamicRendering) {
        VkAttachmentDescription colorAttachment{};
        colorAttachment.format = m_vkCore_ptr->GetSwapchainFormat();
        colorAttachment.samples = VK_SAMPLE_COUNT_1_BIT;
        colorAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD; // Preserve previous content
        colorAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        colorAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        colorAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        colorAttachment.initialLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        colorAttachment.finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

        VkAttachmentReference colorRef{};
        colorRef.attachment = 0;
        colorRef.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

        VkSubpassDescription subpass{};
        subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
        subpass.colorAttachmentCount = 1;
        subpass.pColorAttachments = &colorRef;

        const VkSubpassDependency dependency = vkrender::MakePipelineCompatibleSubpassDependency();

        VkRenderPassCreateInfo rpInfo{};
        rpInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
        rpInfo.attachmentCount = 1;
        rpInfo.pAttachments = &colorAttachment;
        rpInfo.subpassCount = 1;
        rpInfo.pSubpasses = &subpass;
        rpInfo.dependencyCount = 1;
        rpInfo.pDependencies = &dependency;

        if (vkCreateRenderPass(device, &rpInfo, nullptr, &m_imguiRenderPass) != VK_SUCCESS) {
            INXLOG_FATAL("Failed to create ImGui render pass.");
            return;
        }
    }

    ImGui_ImplVulkan_InitInfo initInfo{};
    initInfo.ApiVersion = deviceContext.GetInstanceApiVersion();
    initInfo.Instance = m_vkCore_ptr->GetInstance();
    initInfo.PhysicalDevice = m_vkCore_ptr->GetPhysicalDevice();
    initInfo.Device = device;
    initInfo.QueueFamily = m_vkCore_ptr->GetDeviceContext().GetQueueIndices().graphicsFamily.value();
    initInfo.Queue = m_vkCore_ptr->GetGraphicsQueue();
    initInfo.DescriptorPool = m_descriptorPool_vk;
    initInfo.MinImageCount = m_vkCore_ptr->GetSwapchainImageCount();
    initInfo.ImageCount = m_vkCore_ptr->GetSwapchainImageCount();
    initInfo.Allocator = nullptr;
    initInfo.CheckVkResultFn = nullptr;

    initInfo.PipelineInfoMain.Subpass = 0;
    initInfo.PipelineInfoMain.MSAASamples = VK_SAMPLE_COUNT_1_BIT;

    VkFormat guiColorFormat = m_vkCore_ptr->GetSwapchainFormat();
    if (useDynamicRendering) {
        initInfo.UseDynamicRendering = true;
        initInfo.PipelineInfoMain.PipelineRenderingCreateInfo.sType =
            VK_STRUCTURE_TYPE_PIPELINE_RENDERING_CREATE_INFO_KHR;
        initInfo.PipelineInfoMain.PipelineRenderingCreateInfo.colorAttachmentCount = 1;
        initInfo.PipelineInfoMain.PipelineRenderingCreateInfo.pColorAttachmentFormats = &guiColorFormat;
    } else {
        initInfo.PipelineInfoMain.RenderPass = m_imguiRenderPass;
    }

    if (!ImGui_ImplVulkan_Init(&initInfo)) {
        INXLOG_FATAL("Failed to initialize ImGui Vulkan implementation.");
        return;
    }

    // Font texture is now created automatically by the backend

    // Initialize resource preview manager
    m_resourcePreviewManager.SetGUI(this);
}

void InxGUI::SetGUIFont(const char *fontPath, float fontSize)
{
    if (fontPath == nullptr || *fontPath == '\0' || fontSize <= 0.0f) {
        INXLOG_WARN("InxGUI::SetGUIFont(): Invalid font configuration");
        return;
    }

    EditorDpiState &dpiState = GetEditorDpiState();
    dpiState.fontPath = fontPath;
    dpiState.fontSize = fontSize;
    ReloadGUIFont();
}

void InxGUI::ReloadGUIFont()
{
    EditorDpiState &dpiState = GetEditorDpiState();
    if (dpiState.fontPath.empty() || ImGui::GetCurrentContext() == nullptr)
        return;

    ImGuiIO &io = ImGui::GetIO();
    io.Fonts->Clear();

    // Scale font size by display DPI (e.g. 14px * 2.0 = 28px on 200% display)
    float scaledSize = dpiState.fontSize * m_dpiScale;
    INXLOG_DEBUG("Loading font at ", scaledSize, "px (base ", dpiState.fontSize, " x scale ", m_dpiScale, ")");

    ImFontConfig fontConfig;
    fontConfig.FontDataOwnedByAtlas = false;

    // Since ImGui 1.92+ with RendererHasTextures, glyph ranges are no longer
    // needed. Glyphs are loaded on-demand at any requested size, so the atlas
    // grows incrementally instead of pre-baking all CJK glyphs up-front.
    ImFont *font = io.Fonts->AddFontFromFileTTF(dpiState.fontPath.c_str(), scaledSize, &fontConfig);
    if (font == nullptr) {
        INXLOG_WARN("InxGUI::SetGUIFont(): Failed to load font from ", fontPath);
        return;
    }

    // Font texture is now created automatically by the backend
    // No need to manually call ImGui_ImplVulkan_CreateFontsTexture()
}

void InxGUI::RefreshDisplayScale()
{
    if (m_window_ptr == nullptr || ImGui::GetCurrentContext() == nullptr)
        return;

    float nextScale = SDL_GetWindowDisplayScale(m_window_ptr);
    if (nextScale <= 0.0f)
        nextScale = 1.0f;
    if (std::abs(nextScale - m_dpiScale) < 0.01f)
        return;

    const float previousScale = m_dpiScale;
    m_dpiScale = nextScale;
    InxGUIContext::s_dpiScale = nextScale;

    ImGuiStyle &style = ImGui::GetStyle();
    ImVec4 activeColors[ImGuiCol_COUNT];
    std::copy_n(style.Colors, ImGuiCol_COUNT, activeColors);
    style = GetEditorDpiState().baseStyle;
    std::copy_n(activeColors, ImGuiCol_COUNT, style.Colors);
    style.ScaleAllSizes(nextScale);
    ReloadGUIFont();
    m_editorFrameScheduler.Request();
    INXLOG_INFO("Display scale changed from ", previousScale, " to ", nextScale);
}

void InxGUI::ReleaseTextureResource(ImGuiTextureResource &resource)
{
    if (resource.residentBytes > m_textureResidentBytes)
        throw std::logic_error("ImGui texture residency byte counter underflow");
    m_textureResidentBytes -= resource.residentBytes;

    // GUI frame age only guarantees that panels stopped publishing the old
    // TexID. It says nothing about completion of Vulkan command buffers which
    // already consumed that draw data. Keep the descriptor and its backing
    // texture alive until the exact GPU completion epoch has retired.
    ImGuiTextureResource retired = std::move(resource);
    resource = {};
    m_vkCore_ptr->RetireGpuResource([retired = std::move(retired)]() mutable {
        if (retired.descriptorSet != VK_NULL_HANDLE && ImGui::GetCurrentContext() != nullptr &&
            ImGui::GetIO().BackendRendererUserData != nullptr)
            ImGui_ImplVulkan_RemoveTexture(retired.descriptorSet);
        retired = {};
    });
}

void InxGUI::DeferTextureRelease(ImGuiTextureResource resource)
{
    constexpr uint64_t TextureReleaseGraceFrames = 8;
    if ((!resource.texture && !resource.externalView) || resource.descriptorSet == VK_NULL_HANDLE)
        throw std::logic_error("cannot defer an invalid ImGui texture resource");
    m_deferredTextureReleases.push_back(
        DeferredTextureRelease{std::move(resource), m_guiFrameCounter + TextureReleaseGraceFrames});
}

void InxGUI::PumpTextureUploads()
{
    auto &resourceManager = m_vkCore_ptr->GetResourceManager();
    size_t writeIndex = 0;
    for (size_t index = 0; index < m_pendingTextureUploads.size(); ++index) {
        auto &pending = m_pendingTextureUploads[index];
        // Do not expose an ImGui descriptor until the transfer queue has
        // actually finished writing the texture. Timeline publication is
        // sufficient for renderer submissions that carry the matching wait,
        // but editor draw data can outlive the frame where it was built. A
        // descriptor published at that earlier point can therefore display
        // incomplete preview contents until a later authoring refresh uploads
        // the same pixels again.
        if (pending.ticket->IsAsync() && !pending.ticket->IsComplete()) {
            if (writeIndex != index)
                m_pendingTextureUploads[writeIndex] = std::move(pending);
            ++writeIndex;
            RequestFrame();
            continue;
        }
        bool complete = false;
        bool failed = false;
        try {
            complete = resourceManager.TryPublishTextureUpload(pending.ticket);
        } catch (const std::exception &error) {
            INXLOG_ERROR("ImGui texture upload failed for '", pending.name, "': ", error.what());
            complete = true;
            failed = true;
        }
        if (!complete) {
            if (writeIndex != index)
                m_pendingTextureUploads[writeIndex] = std::move(pending);
            ++writeIndex;
            continue;
        }

        ++m_completedTextureUploadCount;
        const uint64_t pendingBytes = pending.ticket->GetResidentBytes();
        if (pendingBytes > m_pendingTextureUploadBytes)
            throw std::logic_error("pending ImGui texture byte counter underflow");
        m_pendingTextureUploadBytes -= pendingBytes;
        if (failed || !pending.ticket->IsPublished()) {
            m_failedTextureUploadVersions[pending.name] =
                (std::max)(m_failedTextureUploadVersions[pending.name], pending.generation);
            continue;
        }
        const auto generation = m_textureUploadGenerations.find(pending.name);
        if (generation == m_textureUploadGenerations.end() || generation->second != pending.generation)
            continue;

        auto texture = pending.ticket->GetTexture();
        if (!texture) {
            m_failedTextureUploadVersions[pending.name] = pending.generation;
            continue;
        }
        auto &rhiDevice = m_vkCore_ptr->GetDeviceContext().GetRhiDevice();
        const VkSampler sampler = rhiDevice.Resolve(texture->GetSampler());
        const VkImageView view = rhiDevice.Resolve(texture->GetView());
        const VkDescriptorSet descriptor =
            ImGui_ImplVulkan_AddTexture(sampler, view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
        if (descriptor == VK_NULL_HANDLE) {
            INXLOG_ERROR("Failed to allocate ImGui texture descriptor for '", pending.name, "'");
            m_failedTextureUploadVersions[pending.name] = pending.generation;
            continue;
        }

        auto existing = m_textures_umap.find(pending.name);
        if (existing != m_textures_umap.end()) {
            m_textureNamesByDescriptor.erase(existing->second.descriptorSet);
            DeferTextureRelease(std::move(existing->second));
            m_textures_umap.erase(existing);
        }

        const uint64_t residentBytes = texture->GetResidentBytes();
        if (residentBytes > std::numeric_limits<uint64_t>::max() - m_textureResidentBytes) {
            ImGui_ImplVulkan_RemoveTexture(descriptor);
            throw std::overflow_error("ImGui texture residency byte counter overflow");
        }
        m_textureResidentBytes += residentBytes;
        ImGuiTextureResource resource;
        resource.texture = std::move(texture);
        resource.descriptorSet = descriptor;
        resource.residentBytes = residentBytes;
        resource.lastUsedFrame = m_guiFrameCounter;
        resource.uploadGeneration = pending.generation;
        resource.pinned = pending.pinned;
        m_textures_umap.emplace(pending.name, std::move(resource));
        m_textureNamesByDescriptor[descriptor] = pending.name;
    }
    m_pendingTextureUploads.resize(writeIndex);
}

void InxGUI::BuildFrame()
{
    (void)m_editorFrameScheduler.Consume(EditorGuiFrameScheduler::Clock::now(), true);
    BuildFrameInternal();
}

bool InxGUI::BuildFrameIfDue(bool force)
{
    const auto now = EditorGuiFrameScheduler::Clock::now();
    if (m_playerMode) {
        (void)m_editorFrameScheduler.ConsumeUnthrottled(now, true);
    } else if (!m_editorFrameScheduler.Consume(now, force)) {
        return false;
    }

    BuildFrameInternal();
    return true;
}

void InxGUI::RequestFrame() noexcept
{
    m_editorFrameScheduler.Request();
}

void InxGUI::RequestSyntheticInputFrame() noexcept
{
    m_syntheticInputRearm.BeginBatch();
    m_editorFrameScheduler.Request();
}

void InxGUI::BuildFrameInternal()
{
    static auto ctx = std::make_unique<InxGUIContext>();
    ++m_guiFrameCounter;

    // ImGui invalidates the previous ImDrawData as soon as NewFrame() starts.
    // Do not let a render-graph submission reuse the stale publication while
    // this frame is being rebuilt (notably after a throttled editor refresh).
    m_hasDrawData = false;

    // SDL reports per-monitor scale changes as the window crosses displays.
    // Poll here as well as processing the event so a throttled editor frame or
    // platform-specific event ordering cannot leave the UI at the old scale.
    RefreshDisplayScale();

    PumpTextureUploads();

    // Queue removals first, then release after a grace window.
    // Some panels may still emit one or two frames with stale cached TexID;
    // delaying descriptor destruction prevents invalid VkDescriptorSet binds.
    if (!m_pendingTextureRemovals.empty()) {
        for (const auto &name : m_pendingTextureRemovals) {
            auto it = m_textures_umap.find(name);
            if (it == m_textures_umap.end())
                continue;

            m_textureNamesByDescriptor.erase(it->second.descriptorSet);
            DeferTextureRelease(std::move(it->second));
            m_textures_umap.erase(it);
        }
        m_pendingTextureRemovals.clear();
    }

    if (!m_deferredTextureReleases.empty()) {
        std::vector<DeferredTextureRelease> stillDeferred;
        stillDeferred.reserve(m_deferredTextureReleases.size());

        for (auto &entry : m_deferredTextureReleases) {
            if (entry.releaseFrame > m_guiFrameCounter) {
                stillDeferred.push_back(std::move(entry));
                continue;
            }
            ReleaseTextureResource(entry.resource);
        }

        m_deferredTextureReleases.swap(stillDeferred);
    }

    ImGui_ImplSDL3_NewFrame();
    ImGui_ImplVulkan_NewFrame();

    // ImGui's SDL backend may append a physical-cursor fallback position while
    // starting a frame. Replay the trusted automation position afterwards so a
    // synthetic mouse release lands on the same widget as its press.
    float syntheticMouseX = 0.0f;
    float syntheticMouseY = 0.0f;
    if (InputManager::Instance().GetSyntheticMousePositionForFrame(syntheticMouseX, syntheticMouseY)) {
        ImGui::GetIO().AddMousePosEvent(syntheticMouseX, syntheticMouseY);
    }
    ImGui::NewFrame();
    // Dear ImGui may leave later transitions in InputEventsQueue. Only a synthetic input
    // batch may re-arm the scheduler, and it has a fixed four-build budget;
    // physical mouse movement cannot keep the editor permanently unthrottled.
    ImGuiContext *imguiContext = ImGui::GetCurrentContext();
    if (imguiContext != nullptr) {
        const bool hasPendingTransitions = !imguiContext->InputEventsQueue.empty();
        if (m_syntheticInputRearm.AfterBuild(hasPendingTransitions))
            m_editorFrameScheduler.Request();
    }
    ctx->BeginFrameInteractionState();
    InxGUISemantics::BeginFrame(m_guiFrameCounter);
    ImGuiBuildFrameGuard frameGuard;

    // When the cursor is locked (game mode), suppress all mouse input from
    // reaching ImGui so editor panels (Inspector, Hierarchy, etc.) don't
    // react to invisible cursor movement — matching Unity behaviour.
    if (InputManager::Instance().IsCursorLocked()) {
        ImGuiIO &io = ImGui::GetIO();
        io.MousePos = ImVec2(-FLT_MAX, -FLT_MAX);
        for (int i = 0; i < IM_ARRAYSIZE(io.MouseDown); ++i)
            io.MouseDown[i] = false;
        io.MouseWheel = 0.0f;
        io.MouseWheelH = 0.0f;
    }

    // In player mode, skip DockSpace/DockBuilder entirely — they are only
    // needed for the editor's multi-panel layout.  The player registers a
    // single full-screen renderable (PlayerGUI), so docking is wasted work.
    if (!m_playerMode) {
        // Create a full-screen DockSpace (reserve bottom strip for the Python status bar)
        const float kStatusBarHeight = 24.0f * m_dpiScale; // must match _HEIGHT in status_bar.py
        ImGuiViewport *viewport = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(viewport->WorkPos);
        ImGui::SetNextWindowSize(ImVec2(viewport->WorkSize.x, viewport->WorkSize.y - kStatusBarHeight));
        ImGui::SetNextWindowViewport(viewport->ID);

        ImGuiWindowFlags dockSpaceFlags = ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoTitleBar |
                                          ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoResize |
                                          ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoBringToFrontOnFocus |
                                          ImGuiWindowFlags_NoNavFocus | ImGuiWindowFlags_NoBackground;

        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));

        ImGui::Begin("DockSpaceWindow", nullptr, dockSpaceFlags);
        ImGui::PopStyleVar(3);

        // Check whether a saved layout already exists BEFORE DockSpace()
        // creates the node.  If the node doesn't exist yet (first launch or
        // imgui.ini was deleted by the Python layout-version mechanism), we
        // need to build the default Unity-style layout.
        ImGuiID dockspaceId = ImGui::GetID("MainDockSpace");
        bool needsDefaultLayout = (ImGui::DockBuilderGetNode(dockspaceId) == nullptr);

        ImGui::DockSpace(dockspaceId, ImVec2(0.0f, 0.0f), ImGuiDockNodeFlags_None);

        // Setup default Unity-style layout only when no saved layout exists.
        // This preserves user customizations across restarts while still
        // providing the correct initial tab arrangement on first launch
        // (or after a layout-version bump that deletes imgui.ini).
        if (needsDefaultLayout) {

            ImGui::DockBuilderRemoveNode(dockspaceId);
            ImGui::DockBuilderAddNode(dockspaceId, ImGuiDockNodeFlags_DockSpace);
            ImGui::DockBuilderSetNodeSize(dockspaceId,
                                          ImVec2(viewport->WorkSize.x, viewport->WorkSize.y - kStatusBarHeight));

            // Split: Main area | Right panel (Inspector)
            ImGuiID dockMain;
            ImGuiID dockRight;
            ImGui::DockBuilderSplitNode(dockspaceId, ImGuiDir_Right, 0.25f, &dockRight, &dockMain);

            // Split main: Top area (Hierarchy+Scene) | Bottom (Console/Project)
            ImGuiID dockTop;
            ImGuiID dockBottom;
            ImGui::DockBuilderSplitNode(dockMain, ImGuiDir_Down, 0.30f, &dockBottom, &dockTop);

            // Split top: Left (Hierarchy) | Center-top (Toolbar+Scene)
            ImGuiID dockLeft;
            ImGuiID dockCenterTop;
            ImGui::DockBuilderSplitNode(dockTop, ImGuiDir_Left, 0.20f, &dockLeft, &dockCenterTop);

            // Split center-top: Toolbar (thin strip) | Scene/Game
            ImGuiID dockToolbar;
            ImGuiID dockScene;
            ImGui::DockBuilderSplitNode(dockCenterTop, ImGuiDir_Up, 0.04f, &dockToolbar, &dockScene);

            // Set a fixed size for the toolbar node so it doesn't stretch
            ImGui::DockBuilderSetNodeSize(dockToolbar, ImVec2(viewport->WorkSize.x, 36));

            // Hide tab bar on toolbar node — it should be locked in place
            ImGuiDockNode *toolbarNode = ImGui::DockBuilderGetNode(dockToolbar);
            if (toolbarNode) {
                toolbarNode->SetLocalFlags(toolbarNode->LocalFlags | ImGuiDockNodeFlags_NoTabBar |
                                           ImGuiDockNodeFlags_NoDockingSplit | ImGuiDockNodeFlags_NoResize |
                                           ImGuiDockNodeFlags_NoUndocking);
            }

            // Dock windows to their positions.
            // Window IDs use the ### separator so the docking layout is
            // independent of the displayed (localised) title.  The text
            // before ### is ignored for ID purposes; only the part after
            // ### must match what the Python panel passes to ImGui::Begin.
            ImGui::DockBuilderDockWindow("###hierarchy", dockLeft);
            ImGui::DockBuilderDockWindow("###inspector", dockRight);
            ImGui::DockBuilderDockWindow("###toolbar", dockToolbar);
            ImGui::DockBuilderDockWindow("###game_view", dockScene);
            ImGui::DockBuilderDockWindow("###ui_editor", dockScene);
            ImGui::DockBuilderDockWindow("###animclip2d_editor", dockScene);
            ImGui::DockBuilderDockWindow("###animfsm_editor", dockScene);
            ImGui::DockBuilderDockWindow("###animtimeline_editor", dockScene);
            ImGui::DockBuilderDockWindow("###particle_graph_editor", dockScene);
            // Dock Scene last so it is the selected central tab on a fresh
            // workspace. UI Editor remains available as another view while
            // Hierarchy keeps one stable scene tree for every central tab.
            ImGui::DockBuilderDockWindow("###scene_view", dockScene);
            ImGui::DockBuilderDockWindow("###console", dockBottom);
            ImGui::DockBuilderDockWindow("###project", dockBottom);

            ImGui::DockBuilderFinish(dockspaceId);
        }

        ImGui::End();
    } // !m_playerMode

    using hrc = std::chrono::high_resolution_clock;
    m_lastPanelTimesMs.clear();
#if INFERNUX_FRAME_PROFILE
    m_lastPanelSubTimesMs.clear();
#endif

    // Render against a stable snapshot so Register/Unregister calls that
    // happen during panel rendering do not invalidate the active iteration.
    const auto renderableOrderSnapshot = m_renderableOrder;
    for (const auto &name : renderableOrderSnapshot) {
        auto it = m_renderables_umap.find(name);
        if (it == m_renderables_umap.end() || !it->second) {
            continue;
        }

        auto renderable = it->second;
        auto t0 = hrc::now();
        renderable->OnRender(ctx.get());
        auto t1 = hrc::now();
        m_lastPanelTimesMs[name] = std::chrono::duration<double, std::milli>(t1 - t0).count();
#if INFERNUX_FRAME_PROFILE
        auto subTimes = renderable->ConsumeSubTimings();
        if (!subTimes.empty())
            m_lastPanelSubTimesMs.emplace(name, std::move(subTimes));
#endif
    }

    ApplyPendingDockTabSelections();
    PromoteActiveModal();

#ifndef IMGUI_DISABLE_DEBUG_TOOLS
    // Dear ImGui only shows duplicate-ID diagnostics in an on-screen tooltip.
    // Mirror the hovered conflict into the engine log so UI regressions remain
    // actionable in automated Editor validation runs.
    {
        static ImGuiID lastConflictId = 0;
        static std::string lastConflictWindow;
        ImGuiContext &imgui = *m_imguiContext_ptr;
        const ImGuiID conflictId = imgui.HoveredIdPreviousFrameItemCount > 1 ? imgui.HoveredIdPreviousFrame : 0;
        const char *windowName = imgui.HoveredWindow != nullptr ? imgui.HoveredWindow->Name : "<no window>";
        if (conflictId != 0 && (conflictId != lastConflictId || lastConflictWindow != windowName)) {
            INXLOG_ERROR("[ImGui] duplicate visible item ID=", conflictId,
                         " count=", imgui.HoveredIdPreviousFrameItemCount, " window='", windowName, "'");
            lastConflictId = conflictId;
            lastConflictWindow = windowName;
        } else if (conflictId == 0) {
            lastConflictId = 0;
            lastConflictWindow.clear();
        }
    }
#endif

    // ImGui normally renders modals in the regular window layer and relies on
    // root-window ordering alone. A floating dock host can still be emitted
    // over a close confirmation in that model. Use ImGui's overlay layer only
    // while draw data is assembled, then restore the semantic window flags so
    // modal layout and input behavior remain unchanged on the next frame.
    ImGuiWindow *activeModal = ImGui::GetTopMostPopupModal();
    const ImGuiWindowFlags activeModalFlags = activeModal != nullptr ? activeModal->Flags : ImGuiWindowFlags_None;
    if (activeModal != nullptr)
        activeModal->Flags |= ImGuiWindowFlags_Tooltip;

    frameGuard.Complete();
    ImGui::Render();
    if (activeModal != nullptr)
        activeModal->Flags = activeModalFlags;
    const ImDrawData *drawData = ImGui::GetDrawData();
    m_hasDrawData = drawData != nullptr && drawData->Valid;

    // Reclaim only after panels have touched every texture referenced by this
    // frame. Resources are retired through the existing grace queue, so draw
    // data assembled above remains valid through GPU submission.
    (void)TrimImGuiTextureBudget();
}

void InxGUI::QueueDockTabSelection(const std::string &windowId, bool allowDuringModal)
{
    if (windowId.empty()) {
        return;
    }
    auto existing =
        std::find_if(m_pendingDockTabSelections.begin(), m_pendingDockTabSelections.end(),
                     [&windowId](const PendingDockTabSelection &selection) { return selection.windowId == windowId; });
    if (existing == m_pendingDockTabSelections.end()) {
        m_pendingDockTabSelections.push_back({windowId, allowDuringModal});
    } else if (allowDuringModal) {
        existing->allowDuringModal = true;
    }
    RequestFrame();
}

void InxGUI::ApplyPendingDockTabSelections()
{
    if (m_pendingDockTabSelections.empty()) {
        return;
    }

    const bool modalOpen = ImGui::GetTopMostPopupModal() != nullptr;
    std::vector<PendingDockTabSelection> pending;
    pending.swap(m_pendingDockTabSelections);

    for (const auto &selection : pending) {
        if (modalOpen && !selection.allowDuringModal) {
            m_pendingDockTabSelections.push_back(selection);
            continue;
        }
        const auto &windowId = selection.windowId;
        const std::string imguiName = "###" + windowId;
        ImGuiWindow *window = ImGui::FindWindowByName(imguiName.c_str());
        if (window == nullptr) {
            m_pendingDockTabSelections.push_back(selection);
            continue;
        }

        ImGuiDockNode *dockNode = window->DockNode;
        if (dockNode == nullptr) {
            // Ordinary floating editor windows have no dock node.  Treat the
            // same request as a presentation request and raise the root window
            // instead of retrying forever.  A docked tab whose close is being
            // vetoed can temporarily lose DockNode while retaining DockId;
            // that case still waits for the next Begin() to reattach it.
            if (window->DockId == 0) {
                ImGuiWindow *rootWindow = window->RootWindow != nullptr ? window->RootWindow : window;
                ImGui::FocusWindow(window);
                ImGui::BringWindowToFocusFront(rootWindow);
                ImGui::BringWindowToDisplayFront(rootWindow);
                continue;
            }
            // A title-bar close removes the window from its dock node for the
            // remainder of that frame even when the application vetoes the
            // close. Keep the request alive until the next Begin() attaches
            // the restored panel again; consuming it here leaves a sibling
            // tab visible underneath the confirmation modal.
            m_pendingDockTabSelections.push_back(selection);
            continue;
        }

        if (selection.allowDuringModal) {
            // Docking records a title-bar close in WantCloseTabId and applies
            // it at the start of the next frame. Setting the caller-owned
            // p_open value back to true only vetoes Begin() for the current
            // frame; without clearing this native intent the tab is still
            // removed before its next Begin(). Confirmation sources use this
            // flag to complete the veto on both sides of the API boundary.
            if (dockNode->WantCloseTabId == window->TabId)
                dockNode->WantCloseTabId = 0;
            window->DockTabWantClose = false;
        }

        dockNode->SelectedTabId = window->TabId;
        dockNode->VisibleWindow = window;
        if (dockNode->TabBar != nullptr) {
            dockNode->TabBar->SelectedTabId = window->TabId;
            dockNode->TabBar->NextSelectedTabId = window->TabId;
            dockNode->TabBar->VisibleTabId = window->TabId;
        }
        ImGui::MarkIniSettingsDirty(window);

        // Selecting a dock tab is also a presentation request. A detached
        // editor window can overlap the main dock host, and DockSpaceWindow
        // deliberately carries NoBringToFrontOnFocus. FocusWindow() therefore
        // updates navigation focus without necessarily changing the visible
        // Z order. Raise the dock tree explicitly so logical focus and the
        // pixels presented to the user cannot disagree.
        //
        // A close-confirmation source only needs its dock tab restored. The
        // modal is promoted immediately after this pass and remains the final
        // keyboard/input focus owner.
        if (!modalOpen) {
            ImGui::FocusWindow(window);
            BringDockTreeToDisplayFront(window);
        }
    }

    if (!m_pendingDockTabSelections.empty())
        RequestFrame();
}

void InxGUI::PromoteActiveModal()
{
    ImGuiWindow *modal = ImGui::GetTopMostPopupModal();
    if (modal == nullptr)
        return;

    // This is deliberately the final window-order operation before
    // ImGui::Render(). It makes modal ordering independent of renderable
    // registration order and of floating/docked panel focus transitions.
    ImGui::FocusWindow(modal);
    ImGui::BringWindowToFocusFront(modal->RootWindow);
    ImGui::BringWindowToDisplayFront(modal);
}

void InxGUI::RecordCommand(VkCommandBuffer cmdBuf)
{
    ImDrawData *drawData = ImGui::GetDrawData();
    if (m_hasDrawData && drawData != nullptr && drawData->Valid)
        ImGui_ImplVulkan_RenderDrawData(drawData, cmdBuf);
}

void InxGUI::Shutdown()
{
    VkDevice device = m_vkCore_ptr->GetDevice();
    vkDeviceWaitIdle(device);

    m_pendingTextureUploads.clear();
    m_pendingTextureUploadBytes = 0;
    for (auto &entry : m_deferredTextureReleases) {
        ReleaseTextureResource(entry.resource);
    }
    m_deferredTextureReleases.clear();
    m_pendingTextureRemovals.clear();
    m_textureUploadGenerations.clear();
    m_failedTextureUploadVersions.clear();

    for (auto &[name, tex] : m_textures_umap) {
        (void)name;
        ReleaseTextureResource(tex);
    }
    m_textures_umap.clear();
    m_textureNamesByDescriptor.clear();

    // The device is idle, so queued ImGui descriptor retirements are safe to
    // execute now. They must run before ImGui_ImplVulkan_Shutdown tears down
    // the backend state used by ImGui_ImplVulkan_RemoveTexture.
    m_vkCore_ptr->FlushRetiredGpuResources();

    // Shut down ImGui backends BEFORE destroying the descriptor pool —
    // ImGui_ImplVulkan_Shutdown() internally frees descriptor sets and
    // other resources that were allocated from m_descriptorPool_vk.
    ImGui_ImplVulkan_Shutdown();
    ImGui_ImplSDL3_Shutdown();

    // The central descriptor manager owns the external pool. ImGui has
    // released its sets above; the pool survives until backend shutdown.
    m_descriptorPool_vk = VK_NULL_HANDLE;

    if (m_imguiRenderPass != VK_NULL_HANDLE) {
        vkDestroyRenderPass(m_vkCore_ptr->GetDevice(), m_imguiRenderPass, nullptr);
        m_imguiRenderPass = VK_NULL_HANDLE;
    }
}

void InxGUI::Register(const std::string &name, std::shared_ptr<InxGUIRenderable> renderable, int priority)
{
    auto existing = m_renderables_umap.find(name);
    if (existing != m_renderables_umap.end()) {
        INXLOG_WARN("InxGUI::Register(): Renderable with name '", name, "' already exists. Overwriting.");
        const auto oldPriority = m_renderablePriorities.find(name);
        if (oldPriority != m_renderablePriorities.end() && oldPriority->second == priority) {
            existing->second = std::move(renderable);
            RequestFrame();
            return;
        }
        m_renderableOrder.erase(std::remove(m_renderableOrder.begin(), m_renderableOrder.end(), name),
                                m_renderableOrder.end());
    }

    // Submit normal panels first and global overlays last. Insertion remains
    // stable within one priority so dock/tab behavior is deterministic.
    const auto insertion = std::find_if(m_renderableOrder.begin(), m_renderableOrder.end(), [&](const auto &entry) {
        const auto found = m_renderablePriorities.find(entry);
        return found != m_renderablePriorities.end() && found->second > priority;
    });
    m_renderableOrder.insert(insertion, name);
    m_renderablePriorities[name] = priority;
    m_renderables_umap[name] = std::move(renderable);
    RequestFrame();
}

void InxGUI::Unregister(const std::string &name)
{
    auto it = m_renderables_umap.find(name);
    if (it != m_renderables_umap.end()) {
        m_renderables_umap.erase(it);
        m_renderableOrder.erase(std::remove(m_renderableOrder.begin(), m_renderableOrder.end(), name),
                                m_renderableOrder.end());
        m_renderablePriorities.erase(name);
        RequestFrame();
    } else {
        INXLOG_WARN("InxGUI::Unregister(): Renderable with name '", name, "' does not exist.");
    }
}

uint64_t InxGUI::SubmitTextureForImGui(const std::string &name, const unsigned char *pixels, size_t byteCount,
                                       int width, int height, VkFilter filter, bool pinned)
{
    if (name.empty())
        throw std::invalid_argument("ImGui texture name cannot be empty");
    if (width <= 0 || height <= 0)
        throw std::invalid_argument("ImGui texture dimensions must be positive");
    if (filter != VK_FILTER_LINEAR && filter != VK_FILTER_NEAREST)
        throw std::invalid_argument("ImGui texture filter must be linear or nearest");
    const auto generationIt = m_textureUploadGenerations.find(name);
    const uint64_t previousGeneration = generationIt == m_textureUploadGenerations.end() ? 0 : generationIt->second;
    if (previousGeneration == std::numeric_limits<uint64_t>::max())
        throw std::overflow_error("ImGui texture upload version overflow");
    const uint64_t generation = previousGeneration + 1;

    // Editor previews are already rendered at their presentation resolution.
    // Keeping them single-mip makes the Inspector and the smaller Project-grid
    // thumbnail sample the exact same validated pixels. Runtime textures keep
    // their authored mip policy on the separate asset-texture upload path.
    const auto cpuData = TextureDecoder::CreateRgba8(pixels, byteCount, static_cast<uint32_t>(width),
                                                     static_cast<uint32_t>(height), false);
    rhi::SamplerDesc sampler;
    sampler.minFilter = sampler.magFilter = sampler.mipFilter =
        filter == VK_FILTER_NEAREST ? rhi::FilterMode::Nearest : rhi::FilterMode::Linear;
    sampler.addressU = sampler.addressV = sampler.addressW = rhi::AddressMode::ClampToEdge;
    sampler.maxLod = 0.0f;
    TextureUploadBatch upload(*cpuData, sampler);
    auto ticket = m_vkCore_ptr->GetResourceManager().BeginTextureUpload(upload.GetRequest());
    const uint64_t pendingBytes = ticket->GetResidentBytes();
    if (pendingBytes > std::numeric_limits<uint64_t>::max() - m_pendingTextureUploadBytes)
        throw std::overflow_error("pending ImGui texture byte counter overflow");

    m_textureUploadGenerations[name] = generation;
    m_pendingTextureUploads.push_back(PendingTextureUpload{name, generation, pinned, std::move(ticket)});
    m_pendingTextureUploadBytes += pendingBytes;
    ++m_submittedTextureUploadCount;
    if (m_pendingTextureUploads.back().ticket->IsAsync())
        ++m_asyncTextureUploadCount;

    m_pendingTextureRemovals.erase(std::remove(m_pendingTextureRemovals.begin(), m_pendingTextureRemovals.end(), name),
                                   m_pendingTextureRemovals.end());
    RequestFrame();
    return generation;
}

uint64_t InxGUI::PublishTextureViewForImGui(const std::string &name, std::shared_ptr<const rhi::TextureGpuView> texture,
                                            bool pinned)
{
    if (name.empty())
        throw std::invalid_argument("ImGui texture name cannot be empty");
    if (!texture || !texture->IsValid())
        return 0;

    auto existing = m_textures_umap.find(name);
    if (existing != m_textures_umap.end() && existing->second.externalView &&
        existing->second.externalView->GetSourceId() == texture->GetSourceId() &&
        existing->second.externalView->GetRevision() == texture->GetRevision() &&
        existing->second.externalView->GetView() == texture->GetView() &&
        existing->second.externalView->GetSampler() == texture->GetSampler() &&
        existing->second.externalView->GetFormat() == texture->GetFormat()) {
        ImGui_ImplVulkan_SetTextureLinearColor(existing->second.descriptorSet,
                                               existing->second.requiresDisplayEncoding);
        existing->second.lastUsedFrame = m_guiFrameCounter;
        return reinterpret_cast<uint64_t>(existing->second.descriptorSet);
    }

    auto &rhiDevice = m_vkCore_ptr->GetDeviceContext().GetRhiDevice();
    const VkSampler sampler = rhiDevice.Resolve(texture->GetSampler());
    const VkImageView view = rhiDevice.Resolve(texture->GetView());
    const rhi::PixelFormat format = texture->GetFormat();
    const bool requiresDisplayEncoding =
        format != rhi::PixelFormat::Undefined && !rhi::IsDepthFormat(format) && !rhi::IsIntegerFormat(format);

    // Preview the exact authored view used by materials. In particular, sRGB
    // decoding must happen before filtering and mip interpolation; sampling a
    // compatible UNORM alias would interpolate encoded values and diverge from
    // scene rendering. ImGui writes to a UNORM display target, so encode the
    // sampled linear color once in its fragment shader.
    const VkDescriptorSet descriptor =
        ImGui_ImplVulkan_AddTexture(sampler, view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
    if (descriptor == VK_NULL_HANDLE) {
        throw std::runtime_error("failed to allocate ImGui descriptor for resident texture");
    }
    ImGui_ImplVulkan_SetTextureLinearColor(descriptor, requiresDisplayEncoding);

    SupersedePendingImGuiTextureUploads(name);
    auto &generation = m_textureUploadGenerations[name];
    if (generation == std::numeric_limits<uint64_t>::max()) {
        ImGui_ImplVulkan_RemoveTexture(descriptor);
        throw std::overflow_error("ImGui texture publication version overflow");
    }
    ++generation;

    if (existing != m_textures_umap.end()) {
        m_textureNamesByDescriptor.erase(existing->second.descriptorSet);
        DeferTextureRelease(std::move(existing->second));
        m_textures_umap.erase(existing);
    }

    ImGuiTextureResource resource;
    resource.descriptorSet = descriptor;
    resource.lastUsedFrame = m_guiFrameCounter;
    resource.uploadGeneration = generation;
    resource.pinned = pinned;
    resource.requiresDisplayEncoding = requiresDisplayEncoding;
    resource.externalView = std::move(texture);
    m_textureNamesByDescriptor[descriptor] = name;
    m_textures_umap.emplace(name, std::move(resource));
    m_pendingTextureRemovals.erase(std::remove(m_pendingTextureRemovals.begin(), m_pendingTextureRemovals.end(), name),
                                   m_pendingTextureRemovals.end());
    RequestFrame();
    return reinterpret_cast<uint64_t>(descriptor);
}

void InxGUI::SupersedePendingImGuiTextureUploads(const std::string &name)
{
    if (name.empty())
        throw std::invalid_argument("ImGui texture name cannot be empty");
    auto found = m_textureUploadGenerations.find(name);
    if (found == m_textureUploadGenerations.end())
        return;
    if (found->second == std::numeric_limits<uint64_t>::max())
        throw std::overflow_error("ImGui texture upload version overflow");
    ++found->second;
}

void InxGUI::RemoveImGuiTexture(const std::string &name)
{
    if (name.empty())
        throw std::invalid_argument("ImGui texture name cannot be empty");
    auto &generation = m_textureUploadGenerations[name];
    if (generation == std::numeric_limits<uint64_t>::max())
        throw std::overflow_error("ImGui texture upload version overflow");
    ++generation;

    if (m_textures_umap.find(name) != m_textures_umap.end() &&
        std::find(m_pendingTextureRemovals.begin(), m_pendingTextureRemovals.end(), name) ==
            m_pendingTextureRemovals.end())
        m_pendingTextureRemovals.push_back(name);
    RequestFrame();
}

bool InxGUI::HasImGuiTexture(const std::string &name) const
{
    if (m_textures_umap.find(name) == m_textures_umap.end())
        return false;
    // Treat pending-removal textures as absent
    for (const auto &pending : m_pendingTextureRemovals) {
        if (pending == name)
            return false;
    }
    return true;
}

uint64_t InxGUI::GetImGuiTextureId(const std::string &name)
{
    // Treat pending-removal textures as absent
    for (const auto &pending : m_pendingTextureRemovals) {
        if (pending == name)
            return 0;
    }
    auto it = m_textures_umap.find(name);
    if (it != m_textures_umap.end()) {
        ImGui_ImplVulkan_SetTextureLinearColor(it->second.descriptorSet, it->second.requiresDisplayEncoding);
        it->second.lastUsedFrame = m_guiFrameCounter;
        return reinterpret_cast<uint64_t>(it->second.descriptorSet);
    }
    return 0;
}

bool InxGUI::TouchImGuiTextureId(uint64_t textureId)
{
    const auto descriptor = reinterpret_cast<VkDescriptorSet>(static_cast<uintptr_t>(textureId));
    if (descriptor == VK_NULL_HANDLE)
        return false;

    const auto owner = m_textureNamesByDescriptor.find(descriptor);
    if (owner == m_textureNamesByDescriptor.end())
        return false;
    const auto resource = m_textures_umap.find(owner->second);
    if (resource == m_textures_umap.end() || resource->second.descriptorSet != descriptor)
        return false;
    if (std::find(m_pendingTextureRemovals.begin(), m_pendingTextureRemovals.end(), owner->second) !=
        m_pendingTextureRemovals.end())
        return false;

    ImGui_ImplVulkan_SetTextureLinearColor(descriptor, resource->second.requiresDisplayEncoding);
    resource->second.lastUsedFrame = m_guiFrameCounter;
    return true;
}

bool InxGUI::IsTextureReferencedByCurrentDrawData(uint64_t textureId) const
{
    if (textureId == 0 || !m_hasDrawData || m_imguiContext_ptr == nullptr)
        return false;

    const auto referencesTexture = [textureId](const ImDrawData *drawData) {
        if (drawData == nullptr || !drawData->Valid)
            return false;
        for (const ImDrawList *drawList : drawData->CmdLists) {
            if (drawList == nullptr)
                continue;
            for (const ImDrawCmd &command : drawList->CmdBuffer) {
                if (static_cast<uint64_t>(command.GetTexID()) == textureId)
                    return true;
            }
        }
        return false;
    };

    const ImGuiPlatformIO &platform = ImGui::GetPlatformIO();
    for (const ImGuiViewport *viewport : platform.Viewports) {
        if (viewport != nullptr && referencesTexture(viewport->DrawData))
            return true;
    }
    return false;
}

uint64_t InxGUI::GetImGuiTextureVersion(const std::string &name) const
{
    if (std::find(m_pendingTextureRemovals.begin(), m_pendingTextureRemovals.end(), name) !=
        m_pendingTextureRemovals.end())
        return 0;
    auto it = m_textures_umap.find(name);
    if (it == m_textures_umap.end())
        return 0;
    return it->second.uploadGeneration;
}

uint64_t InxGUI::GetFailedImGuiTextureVersion(const std::string &name) const
{
    auto it = m_failedTextureUploadVersions.find(name);
    return it == m_failedTextureUploadVersions.end() ? 0 : it->second;
}

void InxGUI::SetImGuiTextureBudgetBytes(uint64_t bytes)
{
    if (bytes == 0)
        throw std::invalid_argument("ImGui texture budget must be greater than zero");
    m_textureBudgetBytes = bytes;
    (void)TrimImGuiTextureBudget();
}

size_t InxGUI::TrimImGuiTextureBudget()
{
    size_t evicted = 0;
    while (m_textureResidentBytes > m_textureBudgetBytes) {
        auto candidate = m_textures_umap.end();
        for (auto entry = m_textures_umap.begin(); entry != m_textures_umap.end(); ++entry) {
            if (entry->second.pinned || entry->second.lastUsedFrame >= m_guiFrameCounter)
                continue;
            if (candidate == m_textures_umap.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
                candidate = entry;
        }
        if (candidate == m_textures_umap.end())
            break;
        m_textureNamesByDescriptor.erase(candidate->second.descriptorSet);
        DeferTextureRelease(std::move(candidate->second));
        m_textures_umap.erase(candidate);
        ++evicted;
        ++m_textureEvictionCount;
    }
    return evicted;
}

uint64_t InxGUI::GetScheduledTextureReleaseBytes() const noexcept
{
    uint64_t bytes = 0;
    for (const auto &release : m_deferredTextureReleases)
        bytes += release.resource.residentBytes;
    return bytes;
}

GpuEvictionCandidate InxGUI::PeekOldestImGuiTextureEvictable() const noexcept
{
    auto candidate = m_textures_umap.end();
    for (auto entry = m_textures_umap.begin(); entry != m_textures_umap.end(); ++entry) {
        if (entry->second.pinned || entry->second.lastUsedFrame >= m_guiFrameCounter)
            continue;
        if (candidate == m_textures_umap.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
            candidate = entry;
    }
    if (candidate == m_textures_umap.end())
        return {};
    return {candidate->second.lastUsedFrame, candidate->second.residentBytes, true};
}

uint64_t InxGUI::EvictOldestImGuiTexture()
{
    auto candidate = m_textures_umap.end();
    for (auto entry = m_textures_umap.begin(); entry != m_textures_umap.end(); ++entry) {
        if (entry->second.pinned || entry->second.lastUsedFrame >= m_guiFrameCounter)
            continue;
        if (candidate == m_textures_umap.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
            candidate = entry;
    }
    if (candidate == m_textures_umap.end())
        return 0;
    const uint64_t bytes = candidate->second.residentBytes;
    m_textureNamesByDescriptor.erase(candidate->second.descriptorSet);
    DeferTextureRelease(std::move(candidate->second));
    m_textures_umap.erase(candidate);
    ++m_textureEvictionCount;
    return bytes;
}

} // namespace infernux
