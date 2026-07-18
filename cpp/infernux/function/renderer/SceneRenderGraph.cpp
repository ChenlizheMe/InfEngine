/**
 * @file SceneRenderGraph.cpp
 * @brief Implementation of RenderGraph-based scene rendering
 *
 * This implementation fully utilizes vk::RenderGraph for all rendering.
 * No more imperative BeginRenderPass/EndRenderPass calls.
 */

#include "SceneRenderGraph.h"
#include "FullscreenRenderer.h"
#include "InxVkCoreModular.h"
#include "SceneRenderTarget.h"
#include "gui/InxScreenUIRenderer.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkDeviceContext.h"
#include "vk/VkPipelineManager.h"
#include "vk/VkRenderUtils.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <core/config/EngineConfig.h>
#include <core/error/InxError.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/Camera.h>
#include <memory>

namespace infernux
{

namespace
{

bool TextureDescEquals(const GraphTextureDesc &a, const GraphTextureDesc &b)
{
    return a.name == b.name && a.format == b.format && a.isBackbuffer == b.isBackbuffer && a.isDepth == b.isDepth &&
           a.width == b.width && a.height == b.height && a.sizeDivisor == b.sizeDivisor;
}

bool PassDescEquals(const GraphPassDesc &a, const GraphPassDesc &b)
{
    return a.name == b.name && a.readTextures == b.readTextures && a.writeColors == b.writeColors &&
           a.writeDepth == b.writeDepth && a.clearColor == b.clearColor && a.clearDepth == b.clearDepth &&
           a.clearColorR == b.clearColorR && a.clearColorG == b.clearColorG && a.clearColorB == b.clearColorB &&
           a.clearColorA == b.clearColorA && a.clearDepthValue == b.clearDepthValue && a.action == b.action &&
           a.shaderTarget == b.shaderTarget && a.queueMin == b.queueMin && a.queueMax == b.queueMax &&
           a.sortMode == b.sortMode && a.passTag == b.passTag && a.overrideMaterial == b.overrideMaterial &&
           a.lightIndex == b.lightIndex && a.screenUIList == b.screenUIList && a.shaderName == b.shaderName &&
           a.pushConstants == b.pushConstants && a.inputBindings == b.inputBindings;
}

bool GraphDescEquals(const RenderGraphDescription &a, const RenderGraphDescription &b)
{
    if (a.name != b.name || a.outputTexture != b.outputTexture || a.msaaSamples != b.msaaSamples ||
        a.textures.size() != b.textures.size() || a.passes.size() != b.passes.size()) {
        return false;
    }

    for (size_t i = 0; i < a.textures.size(); ++i) {
        if (!TextureDescEquals(a.textures[i], b.textures[i])) {
            return false;
        }
    }

    for (size_t i = 0; i < a.passes.size(); ++i) {
        if (!PassDescEquals(a.passes[i], b.passes[i])) {
            return false;
        }
    }

    return true;
}

bool ValidatePythonGraphDescription(const RenderGraphDescription &desc)
{
    std::unordered_map<std::string, const GraphTextureDesc *> textures;
    textures.reserve(desc.textures.size());

    for (const auto &tex : desc.textures) {
        if (tex.name.empty()) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture name cannot be empty");
            return false;
        }
        if (!textures.emplace(tex.name, &tex).second) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: duplicate texture '", tex.name, "'");
            return false;
        }
        if (tex.sizeDivisor == 1) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture '", tex.name,
                         "' uses sizeDivisor=1; use 0 or >1");
            return false;
        }
        if (tex.width > 0 && tex.height > 0 && tex.sizeDivisor > 0) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture '", tex.name,
                         "' cannot use both explicit size and sizeDivisor");
            return false;
        }
        if ((tex.width == 0) != (tex.height == 0)) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture '", tex.name,
                         "' must specify both width and height together");
            return false;
        }
        if (tex.isBackbuffer && tex.isDepth) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture '", tex.name,
                         "' cannot be both backbuffer and depth");
            return false;
        }
        if (!tex.isBackbuffer && !rhi::IsValidPixelFormat(tex.format)) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture '", tex.name, "' has an undefined pixel format");
            return false;
        }
        if (!tex.isBackbuffer && tex.isDepth != rhi::IsDepthFormat(tex.format)) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture '", tex.name,
                         "' depth flag does not match its pixel format");
            return false;
        }
    }

    std::unordered_set<std::string> passNames;
    passNames.reserve(desc.passes.size());
    for (const auto &pass : desc.passes) {
        if (pass.name.empty()) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass name cannot be empty");
            return false;
        }
        if (!passNames.insert(pass.name).second) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: duplicate pass '", pass.name, "'");
            return false;
        }
        if (pass.action == GraphPassActionType::DrawShadowCasters) {
            if (!pass.writeColors.empty()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: shadow pass '", pass.name,
                             "' cannot write color targets");
                return false;
            }
            if (pass.writeDepth.empty()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: shadow pass '", pass.name,
                             "' requires a depth output");
                return false;
            }
        }
        if (pass.clearDepth && pass.writeDepth.empty()) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                         "' clears depth but has no depth output");
            return false;
        }

        const bool depthOnlyMaterialPass =
            pass.action == GraphPassActionType::DrawRenderers &&
            (pass.shaderTarget == ShaderCompileTarget::Depth || pass.shaderTarget == ShaderCompileTarget::Shadow);
        if (depthOnlyMaterialPass && (!pass.writeColors.empty() || pass.writeDepth.empty())) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: depth-only material pass '", pass.name,
                         "' requires one depth output and no color outputs");
            return false;
        }

        std::unordered_set<int> colorSlots;
        bool writesBackbuffer = false;
        bool writesSingleSampleTarget = false;

        for (const auto &[slot, textureName] : pass.writeColors) {
            if (slot < 0 || slot >= 8 || !colorSlots.insert(slot).second) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                             "' has an invalid or duplicate color slot ", slot);
                return false;
            }
            auto texIt = textures.find(textureName);
            if (texIt == textures.end()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name, "' writes unknown color target '",
                             textureName, "'");
                return false;
            }
            if (texIt->second->isDepth) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name, "' writes depth texture '",
                             textureName, "' as color slot ", slot);
                return false;
            }
            writesBackbuffer |= texIt->second->isBackbuffer;
            writesSingleSampleTarget |= !texIt->second->isBackbuffer;
        }

        if (!pass.writeDepth.empty()) {
            auto texIt = textures.find(pass.writeDepth);
            if (texIt == textures.end()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name, "' writes unknown depth target '",
                             pass.writeDepth, "'");
                return false;
            }
            if (!texIt->second->isDepth) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name, "' writes color texture '",
                             pass.writeDepth, "' as depth");
                return false;
            }
            if (pass.action == GraphPassActionType::DrawRenderers) {
                const bool singleSampleDepth =
                    (texIt->second->width > 0 && texIt->second->height > 0) || texIt->second->sizeDivisor > 1;
                writesSingleSampleTarget |= singleSampleDepth;
                writesBackbuffer |= !singleSampleDepth;
            }
        }

        if (pass.action == GraphPassActionType::DrawRenderers) {
            for (const auto &textureName : pass.readTextures) {
                const bool sampledInput =
                    std::any_of(pass.inputBindings.begin(), pass.inputBindings.end(),
                                [&textureName](const auto &binding) { return binding.second == textureName; });
                if (sampledInput)
                    continue;
                auto texIt = textures.find(textureName);
                if (texIt == textures.end() || !texIt->second->isDepth)
                    continue;
                const bool singleSampleDepth =
                    (texIt->second->width > 0 && texIt->second->height > 0) || texIt->second->sizeDivisor > 1;
                writesSingleSampleTarget |= singleSampleDepth;
                writesBackbuffer |= !singleSampleDepth;
            }
        }

        if (writesBackbuffer && writesSingleSampleTarget) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                         "' mixes backbuffer-sampled and single-sample transient attachments");
            return false;
        }

        for (const auto &textureName : pass.readTextures) {
            if (textures.find(textureName) == textures.end()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name, "' reads unknown texture '",
                             textureName, "'");
                return false;
            }
        }

        for (const auto &[samplerName, textureName] : pass.inputBindings) {
            if (textures.find(textureName) == textures.end()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name, "' input '", samplerName,
                             "' references unknown texture '", textureName, "'");
                return false;
            }
        }
    }

    if (!desc.outputTexture.empty() && textures.find(desc.outputTexture) == textures.end()) {
        INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: output texture '", desc.outputTexture, "' is not declared");
        return false;
    }

    return true;
}

} // namespace

// ============================================================================
// Constructor / Destructor
// ============================================================================

SceneRenderGraph::SceneRenderGraph() : m_renderGraph(std::make_unique<vk::RenderGraph>())
{
}

SceneRenderGraph::~SceneRenderGraph()
{
    Destroy();
}

uint64_t SceneRenderGraph::GetTransientResidentBytes() const
{
    return m_renderGraph ? m_renderGraph->GetTransientResidentBytes() : 0;
}

// ============================================================================
// Initialization
// ============================================================================

bool SceneRenderGraph::Initialize(InxVkCoreModular *vkCore, SceneRenderTarget *sceneTarget)
{
    if (!vkCore || !sceneTarget) {
        INXLOG_ERROR("SceneRenderGraph::Initialize: Invalid parameters");
        return false;
    }

    m_vkCore = vkCore;
    m_sceneTarget = sceneTarget;
    m_width = sceneTarget->GetWidth();
    m_height = sceneTarget->GetHeight();

    // Initialize the underlying RenderGraph with device context and pipeline manager
    m_renderGraph->Initialize(&vkCore->GetDeviceContext(), &vkCore->GetPipelineManager());

    // Allocate per-graph shadow descriptor sets (one per frame-in-flight)
    // for multi-camera isolation without host/device descriptor races.
    for (uint32_t i = 0; i < kMaxFramesInFlight; ++i) {
        m_perViewDescSets[i] = vkCore->AllocatePerViewDescriptorSet();
        if (m_perViewDescSets[i] == VK_NULL_HANDLE) {
            INXLOG_WARN("SceneRenderGraph: Failed to allocate per-view descriptor set [", i, "]");
        }
    }

    // Initialize fullscreen effect renderer for FullscreenQuad passes
    m_fullscreenRenderer.Initialize(vkCore);

    return true;
}

void SceneRenderGraph::ReplaceSceneTarget(SceneRenderTarget *sceneTarget)
{
    if (!sceneTarget)
        return;

    m_sceneTarget = sceneTarget;
    m_width = sceneTarget->GetWidth();
    m_height = sceneTarget->GetHeight();
    m_graphBuilt = false;
    m_needsRebuild = true;
    m_needsCompile = true;
    m_importedColorTarget = {};
    m_importedResolveTarget = {};
    m_importedDepthTarget = {};
    m_previousViewProj = glm::mat4(1.0f);
    m_cameraHistoryValid = false;
}

void SceneRenderGraph::Destroy()
{
    m_fullscreenRenderer.Destroy();
    m_transientResources.clear();

    if (m_renderGraph) {
        m_renderGraph->Destroy();
    }
    m_importedColorTarget = {};
    m_importedDepthTarget = {};
    m_graphBuilt = false;
    m_vkCore = nullptr;
    m_sceneTarget = nullptr;
}

VkDescriptorSet SceneRenderGraph::GetPerViewDescriptorSet() const
{
    if (!m_vkCore)
        return VK_NULL_HANDLE;
    uint32_t frameIdx = m_vkCore->GetSwapchain().GetCurrentFrame() % kMaxFramesInFlight;
    return m_perViewDescSets[frameIdx];
}

// ============================================================================
// Resource management
// ============================================================================

vk::ResourceHandle SceneRenderGraph::CreateTransientTexture(const std::string &name, uint32_t width, uint32_t height,
                                                            VkFormat format, bool isTransient)
{
    if (!m_renderGraph) {
        INXLOG_ERROR("SceneRenderGraph::CreateTransientTexture: RenderGraph not initialized");
        return {};
    }

    // Check if resource already exists
    auto it = m_transientResources.find(name);
    if (it != m_transientResources.end()) {
        INXLOG_WARN("SceneRenderGraph::CreateTransientTexture: Resource '", name,
                    "' already exists, returning existing handle");
        return it->second;
    }

    // ========================================================================
    // Allocate a real ResourceData entry in the underlying RenderGraph so
    // the returned handle can be resolved by ResolveTextureView().
    // ========================================================================
    vk::ResourceHandle handle =
        m_renderGraph->RegisterTransientTexture(name, width, height, format, VK_SAMPLE_COUNT_1_BIT, isTransient);

    m_transientResources[name] = handle;
    m_needsRebuild = true;

    INXLOG_DEBUG("SceneRenderGraph: Created transient texture '", name, "' id=", handle.id, " (", width, "x", height,
                 ", format ", static_cast<int>(format), ")");

    return handle;
}

// ============================================================================
// RenderGraph topology defined from Python
// ============================================================================

void SceneRenderGraph::ApplyPythonGraph(const RenderGraphDescription &desc)
{
    if (!m_vkCore || !m_sceneTarget) {
        INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: Not initialized");
        return;
    }

    const VkSampleCountFlagBits callbackSamples = m_vkCore->GetMaterialPipelineManager().GetSampleCount();
    const bool topologyChanged = !m_hasPythonGraph || !GraphDescEquals(desc, m_pythonGraphDesc);
    const bool callbackContractChanged = m_pythonCallbackSamples != callbackSamples;
    if (!topologyChanged && !callbackContractChanged) {
        if (desc.sourceRevision != 0) {
            m_pythonGraphSourceRevision = desc.sourceRevision;
            m_pythonGraphDesc.sourceRevision = desc.sourceRevision;
        }
        return;
    }

    if (topologyChanged && !ValidatePythonGraphDescription(desc)) {
        return;
    }

    m_pythonCallbacks.clear();
    m_hasShadowCasterPass = false;

    InxVkCoreModular *vkCore = m_vkCore;

    for (const auto &passDesc : desc.passes) {
        // Build the render callback directly from the pass action.
        const auto graphPassAction = passDesc.action;
        if (graphPassAction == GraphPassActionType::DrawShadowCasters) {
            m_hasShadowCasterPass = true;
        }
        const int queueMin = passDesc.queueMin;
        const int queueMax = passDesc.queueMax;
        const int screenUIListIndex = passDesc.screenUIList;
        const int lightIndex = passDesc.lightIndex;
        const std::string sortMode = passDesc.sortMode;
        const std::string overrideMaterial = passDesc.overrideMaterial;
        const std::string passTag = passDesc.passTag;
        MaterialPassPipelineDescriptor materialPass =
            vkCore->GetMaterialPipelineManager().GetDefaultPassPipelineDescriptor(passDesc.shaderTarget);
        if (graphPassAction == GraphPassActionType::DrawRenderers) {
            materialPass.colorFormats.clear();
            bool writesBackbuffer = passDesc.writeColors.empty() &&
                                    passDesc.shaderTarget != ShaderCompileTarget::Depth &&
                                    passDesc.shaderTarget != ShaderCompileTarget::Shadow;
            bool writesSingleSampleTarget = false;
            auto colorOutputs = passDesc.writeColors;
            std::sort(colorOutputs.begin(), colorOutputs.end(),
                      [](const auto &lhs, const auto &rhs) { return lhs.first < rhs.first; });
            for (const auto &[slot, textureName] : colorOutputs) {
                (void)slot;
                const auto texture = std::find_if(
                    desc.textures.begin(), desc.textures.end(),
                    [&textureName](const GraphTextureDesc &textureDesc) { return textureDesc.name == textureName; });
                if (texture != desc.textures.end()) {
                    writesBackbuffer |= texture->isBackbuffer;
                    writesSingleSampleTarget |= !texture->isBackbuffer;
                    materialPass.colorFormats.push_back(
                        texture->isBackbuffer ? rhi::FromVkFormat(vkCore->GetMaterialPipelineManager().GetColorFormat())
                                              : texture->format);
                }
            }
            if (colorOutputs.empty() && writesBackbuffer) {
                materialPass.colorFormats.push_back(
                    rhi::FromVkFormat(vkCore->GetMaterialPipelineManager().GetColorFormat()));
            }
            if (!passDesc.writeDepth.empty()) {
                const auto depth =
                    std::find_if(desc.textures.begin(), desc.textures.end(), [&passDesc](const GraphTextureDesc &desc) {
                        return desc.name == passDesc.writeDepth;
                    });
                materialPass.depthFormat = depth != desc.textures.end() ? depth->format : rhi::PixelFormat::Undefined;
                if (depth != desc.textures.end()) {
                    const bool singleSampleDepth = (depth->width > 0 && depth->height > 0) || depth->sizeDivisor > 1;
                    writesSingleSampleTarget |= singleSampleDepth;
                    writesBackbuffer |= !singleSampleDepth;
                }
            } else {
                materialPass.depthFormat = rhi::PixelFormat::Undefined;
                for (const std::string &textureName : passDesc.readTextures) {
                    const bool sampledInput =
                        std::any_of(passDesc.inputBindings.begin(), passDesc.inputBindings.end(),
                                    [&textureName](const auto &binding) { return binding.second == textureName; });
                    if (sampledInput)
                        continue;
                    const auto depth = std::find_if(desc.textures.begin(), desc.textures.end(),
                                                    [&textureName](const GraphTextureDesc &desc) {
                                                        return desc.name == textureName && desc.isDepth;
                                                    });
                    if (depth != desc.textures.end()) {
                        materialPass.depthFormat = depth->format;
                        const bool singleSampleDepth =
                            (depth->width > 0 && depth->height > 0) || depth->sizeDivisor > 1;
                        writesSingleSampleTarget |= singleSampleDepth;
                        writesBackbuffer |= !singleSampleDepth;
                        break;
                    }
                }
            }
            materialPass.samples = writesSingleSampleTarget && !writesBackbuffer
                                       ? rhi::SampleCount::One
                                       : rhi::FromVkSampleCount(callbackSamples);
        }

        // Capture input bindings for passes that need graph texture access
        // (e.g. reading shadow map as a sampled texture).
        const auto inputBindings = passDesc.inputBindings;

        m_pythonCallbacks[passDesc.name] = [this, vkCore, graphPassAction, queueMin, queueMax, screenUIListIndex,
                                            inputBindings, lightIndex, sortMode, overrideMaterial, passTag,
                                            materialPass](vk::RenderContext &ctx, uint32_t w, uint32_t h) {
            switch (graphPassAction) {
            case GraphPassActionType::DrawRenderers:
                vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, queueMin, queueMax, sortMode, overrideMaterial,
                                          passTag, &materialPass);
                break;
            case GraphPassActionType::DrawSkybox: {
                const int32_t skyboxQueue = EngineConfig::Get().skyboxQueue;
                vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, skyboxQueue, skyboxQueue);
                break;
            }
            case GraphPassActionType::DrawShadowCasters:
                // Shadow caster pass: draw filtered objects using shadow pipeline
                // with lightVP from SceneLightCollector. The shadow pipeline is
                // lazily created inside DrawShadowCasters().
                vkCore->DrawShadowCasters(ctx.GetCommandBuffer(), w, h, queueMin, queueMax, lightIndex);
                break;
            case GraphPassActionType::DrawScreenUI:
                if (m_screenUIRenderer) {
                    auto list = (screenUIListIndex == 0) ? ScreenUIList::Camera : ScreenUIList::Overlay;
                    m_screenUIRenderer->Render(ctx.GetCommandBuffer(), list, w, h);
                }
                break;
            case GraphPassActionType::FullscreenQuad:
                // FullscreenQuad passes are handled entirely inside
                // BuildRenderGraph's execute lambda — the callback is a
                // no-op placeholder so the pass entry exists in m_pythonCallbacks.
                break;
            default:
                break;
            }
        };
    }

    // ========================================================================
    // Auto-append _ComponentGizmos pass (queue 10000-20000).
    // Python-defined per-component gizmos, rendered with depth testing
    // against existing scene geometry. Runs before editor gizmos.
    // ========================================================================
    static constexpr int COMP_GIZMO_QUEUE_MIN = 10000;
    static constexpr int COMP_GIZMO_QUEUE_MAX = 20000;
    static const std::string kComponentGizmosPassName = "_ComponentGizmos";
    m_pythonCallbacks[kComponentGizmosPassName] = [vkCore](vk::RenderContext &ctx, uint32_t w, uint32_t h) {
        vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, COMP_GIZMO_QUEUE_MIN, COMP_GIZMO_QUEUE_MAX);
    };

    // ========================================================================
    // Auto-append editor gizmos pass (queue 20001-25000).
    // This ensures grid/gizmos always render after all user-defined passes,
    // regardless of what queue ranges the user pipeline declares.
    // In game view (no gizmo draw calls), DrawSceneFiltered finds nothing
    // in this range and the pass is effectively a no-op.
    // ========================================================================
    static constexpr int GIZMO_QUEUE_MIN = 20001;
    static constexpr int GIZMO_QUEUE_MAX = 25000;
    static const std::string kEditorGizmosPassName = "_EditorGizmos";
    m_pythonCallbacks[kEditorGizmosPassName] = [vkCore](vk::RenderContext &ctx, uint32_t w, uint32_t h) {
        vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, GIZMO_QUEUE_MIN, GIZMO_QUEUE_MAX);
    };

    // ========================================================================
    // Auto-append editor tools pass (queue 25001-30000).
    // Translation/rotation/scale handles rendered on top of everything
    // (no depth test). In game view, no draw calls exist in this range.
    // ========================================================================
    static constexpr int TOOLS_QUEUE_MIN = 25001;
    static constexpr int TOOLS_QUEUE_MAX = 30000;
    static const std::string kEditorToolsPassName = "_EditorTools";
    m_pythonCallbacks[kEditorToolsPassName] = [vkCore](vk::RenderContext &ctx, uint32_t w, uint32_t h) {
        vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, TOOLS_QUEUE_MIN, TOOLS_QUEUE_MAX, "preserve");
    };

    // Store description for BuildRenderGraph()'s topology traversal. Exact
    // repeats return before validation/callback construction above.
    if (topologyChanged) {
        m_pythonGraphDesc = desc;
    }
    m_hasPythonGraph = true;
    m_pythonGraphSourceRevision = desc.sourceRevision;
    m_pythonCallbackSamples = callbackSamples;
    if (topologyChanged || callbackContractChanged) {
        m_needsRebuild = true;
    }
}

bool SceneRenderGraph::IsPythonGraphCurrent(uint64_t sourceRevision) const
{
    return sourceRevision != 0 && m_hasPythonGraph && m_pythonGraphSourceRevision == sourceRevision && m_vkCore &&
           m_pythonCallbackSamples == m_vkCore->GetMaterialPipelineManager().GetSampleCount();
}

// ============================================================================
// Execution (Pure RenderGraph)
// ============================================================================

void SceneRenderGraph::EnsureGraphBuilt()
{
    if (!m_sceneTarget || !m_sceneTarget->IsReady() || !m_renderGraph) {
        return;
    }

    // ========================================================================
    // MSAA mismatch guard: if the Python pipeline requested a different MSAA
    // sample count than the scene target currently has, skip this frame.
    // InxRenderer::DrawFrame() will detect the mismatch on the NEXT frame
    // (via GetRequestedMsaaSamples()) and call SetMsaaSamples() to recreate
    if (m_hasPythonGraph && m_pythonGraphDesc.msaaSamples > 0) {
        auto currentMsaa = static_cast<int>(m_sceneTarget->GetMsaaSampleCount());
        const int effectiveMsaa = m_effectiveMsaaSamples > 0 ? m_effectiveMsaaSamples : m_pythonGraphDesc.msaaSamples;
        if (effectiveMsaa != currentMsaa) {
            INXLOG_DEBUG("SceneRenderGraph: MSAA mismatch (validated request is ", effectiveMsaa,
                         "x, scene target has ", currentMsaa, "x) — skipping frame, waiting for resize");
            m_needsRebuild = true;
            // Prevent Execute() from running the stale compiled graph
            // whose render passes reference images with the old sample
            // count.  Without this, a single frame of execution with
            // mismatched MSAA resources can cause a Vulkan error or hang.
            m_graphBuilt = false;
            return;
        }
    }

    // Update dimensions if changed
    if (m_width != m_sceneTarget->GetWidth() || m_height != m_sceneTarget->GetHeight()) {
        m_width = m_sceneTarget->GetWidth();
        m_height = m_sceneTarget->GetHeight();
        m_needsRebuild = true;
    }

    if (m_needsRebuild) {
        BuildRenderGraph();
        m_needsRebuild = false;
        m_needsCompile = true; // Need to compile after rebuild
    }

    if (m_graphBuilt && m_needsCompile) {
        if (!m_renderGraph->Compile()) {
            INXLOG_ERROR("SceneRenderGraph: Failed to compile render graph — disabling graph until next rebuild");
            m_graphBuilt = false;
            return;
        }
        m_needsCompile = false;
    }

    if (m_graphBuilt) {
        RefreshPerViewShadowDescriptor();
    }
}

void SceneRenderGraph::Execute(VkCommandBuffer commandBuffer)
{
    if (!m_sceneTarget || !m_sceneTarget->IsReady() || !m_renderGraph) {
        return;
    }

    if (m_importedColorTarget.IsValid()) {
        if (m_sceneTarget->IsMsaaEnabled()) {
            m_renderGraph->SetResourceInitialState(m_importedColorTarget, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
                                                   VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
                                                   VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT);
        } else {
            m_renderGraph->SetResourceInitialState(m_importedColorTarget, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                                                   VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
        }
    }

    if (m_importedResolveTarget.IsValid()) {
        m_renderGraph->SetResourceInitialState(m_importedResolveTarget, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                                               VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
    }

    if (m_graphBuilt) {
        if (m_hasCameraClearOverride && !m_mainClearPassName.empty()) {
            if (m_cameraClearFlags == CameraClearFlags::Skybox) {
                m_renderGraph->UpdatePassClearColor(m_mainClearPassName, 0.0f, 0.0f, 0.0f, 1.0f);
            } else if (m_cameraClearFlags == CameraClearFlags::SolidColor) {
                m_renderGraph->UpdatePassClearColor(m_mainClearPassName, m_cameraBgColor.r, m_cameraBgColor.g,
                                                    m_cameraBgColor.b, m_cameraBgColor.a);
            }
        }

        m_prevClearStateValid = true;
        m_prevCameraClearFlags = m_cameraClearFlags;
        m_prevCameraBgColor = m_cameraBgColor;

        m_fullscreenRenderer.ResetPool();

        m_renderGraph->Execute(commandBuffer);
        ++m_executionCount;
        m_lastExecutedBuildRevision = m_graphBuildRevision;

        // Non-MSAA scene/game targets are sampled by ImGui after the render
        // graph finishes. The graph leaves offscreen color outputs in
        // COLOR_ATTACHMENT_OPTIMAL, so transition them back here before any
        // descriptor-based sampling occurs later in the frame.
        if (!m_sceneTarget->IsMsaaEnabled() && m_importedColorTarget.IsValid()) {
            VkImageMemoryBarrier barrier =
                vkrender::MakeImageBarrier(m_sceneTarget->GetColorImage(), VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
                                           VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_ASPECT_COLOR_BIT,
                                           VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);

            vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                                 VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0, nullptr, 0, nullptr, 1, &barrier);
        }
    }
}

void SceneRenderGraph::RefreshPerViewShadowDescriptor()
{
    if (!m_vkCore) {
        return;
    }

    VkDescriptorSet graphShadowDesc = GetPerViewDescriptorSet();
    if (graphShadowDesc == VK_NULL_HANDLE) {
        return;
    }

    if (!m_shadowMapInputHandle.IsValid() || !m_renderGraph) {
        static int s_missingShadowInputWarnCount = 0;
        if (s_missingShadowInputWarnCount++ < 8) {
            INXLOG_WARN("SceneRenderGraph: no valid shadowMap input handle for per-view descriptor; binding fallback "
                        "white texture");
        }
        m_vkCore->ClearPerViewShadowMap(graphShadowDesc);
        return;
    }

    VkImageView view = m_renderGraph->ResolveTextureView(m_shadowMapInputHandle);
    VkSampler shadowSampler = m_vkCore->GetShadowDepthSampler();
    if (view == VK_NULL_HANDLE || shadowSampler == VK_NULL_HANDLE) {
        static int s_nullShadowViewWarnCount = 0;
        if (s_nullShadowViewWarnCount++ < 8) {
            INXLOG_WARN(
                "SceneRenderGraph: shadow map view/sampler unavailable (view=", view == VK_NULL_HANDLE ? "null" : "ok",
                ", sampler=", shadowSampler == VK_NULL_HANDLE ? "null" : "ok", "); binding fallback white texture");
        }
        m_vkCore->ClearPerViewShadowMap(graphShadowDesc);
        return;
    }

    m_vkCore->UpdatePerViewShadowMap(graphShadowDesc, view, shadowSampler,
                                     m_shadowMapInputIsDepth ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL
                                                             : VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
}

void SceneRenderGraph::OnResize(uint32_t width, uint32_t height)
{
    if (m_width != width || m_height != height) {
        m_width = width;
        m_height = height;
        m_needsRebuild = true;
        m_graphBuilt = false; // Force complete rebuild

        INXLOG_DEBUG("SceneRenderGraph: Resized to ", width, "x", height);
    }
}

// ============================================================================
// Debug
// ============================================================================

std::string SceneRenderGraph::GetDebugString() const
{
    std::string result =
        "SceneRenderGraph [RenderGraph Mode] (" + std::to_string(m_width) + "x" + std::to_string(m_height) + ")\n";
    result += "Graph Built: " + std::string(m_graphBuilt ? "Yes" : "No") + "\n";
    result += "Python Graph: " + std::string(m_hasPythonGraph ? "Yes" : "No") + "\n";
    if (m_hasPythonGraph) {
        result += "Passes (" + std::to_string(m_pythonGraphDesc.passes.size()) + "):\n";
        for (const auto &pass : m_pythonGraphDesc.passes) {
            result += "  " + pass.name + "\n";
        }
    }

    // Add underlying RenderGraph debug info
    if (m_renderGraph && m_graphBuilt) {
        result += "\nUnderlying RenderGraph:\n";
        result += m_renderGraph->GetDebugString();
    }

    return result;
}

// ============================================================================
// Pass Output Access
// ============================================================================

// ============================================================================
// Private Methods
// ============================================================================

void SceneRenderGraph::ImportSceneTargetResources()
{
    if (!m_sceneTarget || !m_renderGraph) {
        return;
    }

    m_importedColorTarget = m_renderGraph->SetBackbuffer(
        m_sceneTarget->GetMsaaColorImage(), m_sceneTarget->GetMsaaColorImageView(), m_sceneTarget->GetColorFormat(),
        m_width, m_height, m_sceneTarget->GetMsaaSampleCount(), VK_IMAGE_LAYOUT_UNDEFINED);

    if (m_sceneTarget->IsMsaaEnabled()) {
        m_importedResolveTarget =
            m_renderGraph->ImportResolveTarget(m_sceneTarget->GetColorImage(), m_sceneTarget->GetColorImageView(),
                                               m_sceneTarget->GetColorFormat(), m_width, m_height);
    } else {
        m_importedResolveTarget = {}; // Clear — no separate resolve target needed
    }
}

void SceneRenderGraph::UpdateMainPassClearSettings(CameraClearFlags clearFlags, const glm::vec4 &bgColor)
{
    m_hasCameraClearOverride = true;
    m_cameraClearFlags = clearFlags;
    m_cameraBgColor = bgColor;

    if (m_prevClearStateValid && m_prevCameraClearFlags != clearFlags) {
        m_needsRebuild = true;
    }
}

void SceneRenderGraph::ResolveSceneMsaa(VkCommandBuffer commandBuffer)
{
    if (!m_sceneTarget) {
        return;
    }

    if (!m_sceneTarget->IsMsaaEnabled()) {
        return;
    }

    VkImage msaaImage = m_sceneTarget->GetMsaaColorImage();
    VkImage resolveImage = m_sceneTarget->GetColorImage();

    {
        VkImageMemoryBarrier barriers[2] = {
            // MSAA source
            vkrender::MakeImageBarrier(msaaImage, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
                                       VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_ASPECT_COLOR_BIT,
                                       VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT),
            // 1x resolve destination
            vkrender::MakeImageBarrier(resolveImage, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                                       VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_ASPECT_COLOR_BIT,
                                       VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_WRITE_BIT),
        };

        vkCmdPipelineBarrier(commandBuffer,
                             VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                             VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 2, barriers);
    }

    VkImageResolve resolveRegion{};
    resolveRegion.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    resolveRegion.srcOffset = {0, 0, 0};
    resolveRegion.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    resolveRegion.dstOffset = {0, 0, 0};
    resolveRegion.extent = {m_width, m_height, 1};

    vkCmdResolveImage(commandBuffer, msaaImage, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, resolveImage,
                      VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &resolveRegion);

    {
        VkImageMemoryBarrier barriers[2] = {
            // MSAA source: restore to COLOR_ATTACHMENT_OPTIMAL
            vkrender::MakeImageBarrier(msaaImage, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                                       VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_ASPECT_COLOR_BIT,
                                       VK_ACCESS_TRANSFER_READ_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT),
            // 1x resolve destination: ready for outline / ImGui sampling
            vkrender::MakeImageBarrier(resolveImage, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                                       VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_ASPECT_COLOR_BIT,
                                       VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT),
        };

        vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_TRANSFER_BIT,
                             VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0,
                             0, nullptr, 0, nullptr, 2, barriers);
    }
}

// ---------------------------------------------------------------------------
// BuildRenderGraph helpers
// ---------------------------------------------------------------------------

void SceneRenderGraph::RegisterTransientTextures(uint32_t width, uint32_t height,
                                                 std::unordered_map<std::string, vk::ResourceHandle> &customRTHandles)
{
    // Non-backbuffer, non-depth color textures
    for (const auto &tex : m_pythonGraphDesc.textures) {
        if (!tex.isBackbuffer && !tex.isDepth) {
            uint32_t texW = (tex.width > 0) ? tex.width : width;
            uint32_t texH = (tex.height > 0) ? tex.height : height;
            if (tex.sizeDivisor > 1) {
                texW = std::max(1u, width / tex.sizeDivisor);
                texH = std::max(1u, height / tex.sizeDivisor);
            }
            vk::ResourceHandle handle = m_renderGraph->RegisterTransientTexture(
                tex.name, texW, texH, rhi::ToVkFormat(tex.format), VK_SAMPLE_COUNT_1_BIT, true);
            customRTHandles[tex.name] = handle;
        }
    }

    // Custom-size depth textures (shadow maps and offscreen depth targets)
    for (const auto &tex : m_pythonGraphDesc.textures) {
        if (tex.isDepth && ((tex.width > 0 && tex.height > 0) || tex.sizeDivisor > 1)) {
            uint32_t texW = tex.width > 0 ? tex.width : std::max(1u, width / tex.sizeDivisor);
            uint32_t texH = tex.height > 0 ? tex.height : std::max(1u, height / tex.sizeDivisor);
            vk::ResourceHandle handle = m_renderGraph->RegisterTransientTexture(
                tex.name, texW, texH, rhi::ToVkFormat(tex.format), VK_SAMPLE_COUNT_1_BIT, true);
            customRTHandles[tex.name] = handle;
        }
    }
}

vk::ResourceHandle SceneRenderGraph::AppendAutoPass(const std::string &name, vk::ResourceHandle colorTarget,
                                                    vk::ResourceHandle depthTarget, uint32_t width, uint32_t height)
{
    auto callbackIt = m_pythonCallbacks.find(name);
    if (callbackIt == m_pythonCallbacks.end())
        return colorTarget;

    auto callback = callbackIt->second;
    vk::ResourceHandle writtenColor;
    m_renderGraph->AddPass(name, [=, &writtenColor](vk::PassBuilder &builder) {
        writtenColor = builder.WriteColor(colorTarget, 0);
        if (depthTarget.IsValid()) {
            builder.ReadDepth(depthTarget);
        }
        builder.SetRenderArea(width, height);

        return [callback, width, height](vk::RenderContext &ctx) {
            if (callback) {
                callback(ctx, width, height);
            }
        };
    });
    return writtenColor.IsValid() ? writtenColor : colorTarget;
}

void SceneRenderGraph::FinalizeGraphOutput(const std::unordered_map<std::string, vk::ResourceHandle> &customRTHandles)
{
    bool outputSet = false;
    if (m_hasPythonGraph && !m_pythonGraphDesc.outputTexture.empty()) {
        auto texIt = std::find_if(m_pythonGraphDesc.textures.begin(), m_pythonGraphDesc.textures.end(),
                                  [&](const GraphTextureDesc &t) { return t.name == m_pythonGraphDesc.outputTexture; });
        if (texIt != m_pythonGraphDesc.textures.end()) {
            if (!texIt->isBackbuffer && !texIt->isDepth) {
                auto rtIt = customRTHandles.find(m_pythonGraphDesc.outputTexture);
                if (rtIt != customRTHandles.end()) {
                    m_renderGraph->SetOutput(rtIt->second);
                    outputSet = true;
                }
            }
        }
    }
    if (!outputSet && m_importedColorTarget.IsValid()) {
        m_renderGraph->SetOutput(m_importedColorTarget);
    }
}

void SceneRenderGraph::BuildRenderGraph()
{
    if (!m_renderGraph || !m_sceneTarget || !m_vkCore) {
        INXLOG_WARN("SceneRenderGraph::BuildRenderGraph - Missing required components");
        return;
    }

    m_vkCore->GetDeviceContext().WaitIdle();

    // Keep the OS message pump alive during graph rebuilds that follow a
    // scene switch (each BuildRenderGraph call includes a full device drain).
    SDL_PumpEvents();

    m_renderGraph->Reset();
    for (uint32_t i = 0; i < kMaxFramesInFlight; ++i) {
        if (m_perViewDescSets[i] != VK_NULL_HANDLE) {
            m_vkCore->ClearPerViewShadowMap(m_perViewDescSets[i]);
        }
    }

    m_vkCore->GetMaterialPipelineManager().InvalidateAllMaterialPipelines();

    m_graphBuilt = false;
    m_shadowMapInputHandle = {};
    m_shadowMapInputIsDepth = false;

    if (!m_hasPythonGraph) {
        INXLOG_DEBUG("SceneRenderGraph::BuildRenderGraph - No Python graph configured");
        return;
    }

    ImportSceneTargetResources();

    std::unordered_map<std::string, vk::ResourceHandle> customRTHandles;
    if (!m_pythonGraphDesc.passes.empty()) {
        std::unordered_map<std::string, const GraphTextureDesc *> texDescMap;
        for (const auto &tex : m_pythonGraphDesc.textures) {
            texDescMap[tex.name] = &tex;
        }

        const auto &sortedPasses = m_pythonGraphDesc.passes;

        uint32_t width = m_width;
        uint32_t height = m_height;
        VkFormat depthFormat = m_sceneTarget->GetDepthFormat();
        VkSampleCountFlagBits msaaSamples = m_sceneTarget->GetMsaaSampleCount();

        // Capture vkCore for pass lambdas (avoids capturing 'this')
        InxVkCoreModular *vkCore = m_vkCore;

        // Shared depth handle — created by the first pass that writes depth,
        // referenced by later passes via ReadDepth().
        vk::ResourceHandle sharedDepth;

        // =================================================================
        // Custom RT tracking: Non-backbuffer color textures get a transient
        // resource created by the first pass that writes to them. Later
        // passes can read them via builder.Read() for proper DAG edges.
        // =================================================================

        // Pre-register transient textures so their ResourceHandles are
        // available before passes reference them.
        RegisterTransientTextures(width, height, customRTHandles);

        auto publishResourceVersion = [&](vk::ResourceHandle handle) {
            if (!handle.IsValid())
                return;
            if (m_importedColorTarget.IsValid() && handle.id == m_importedColorTarget.id) {
                m_importedColorTarget = handle;
                return;
            }
            if (m_importedResolveTarget.IsValid() && handle.id == m_importedResolveTarget.id) {
                m_importedResolveTarget = handle;
                return;
            }
            for (auto &[name, current] : customRTHandles) {
                if (current.id == handle.id) {
                    current = handle;
                    return;
                }
            }
        };

        // Track whether the multisampled backbuffer has been written since the
        // last explicit resolve. FullscreenQuad passes that sample the backbuffer
        // must resolve it again whenever a preceding pass wrote new MSAA data.
        bool backbufferDirtySinceResolve = false;
        uint32_t msaaResolvePassCounter = 0;

        for (const auto &passDesc : sortedPasses) {
            // Look up render callback from the Python callbacks map
            auto callbackIt = m_pythonCallbacks.find(passDesc.name);
            if (callbackIt == m_pythonCallbacks.end()) {
                INXLOG_WARN("SceneRenderGraph: Pass '", passDesc.name,
                            "' has no render callback — skipping. "
                            "This usually means ApplyPythonGraph() was not called or validation failed.");
                continue;
            }
            auto callback = callbackIt->second;

            // Determine color targets (MRT support).
            // Build a map of slot → ResourceHandle for all declared color outputs.
            // Slot 0 defaults to the MSAA backbuffer if not specified.
            std::map<int, vk::ResourceHandle> colorTargets;
            for (const auto &[slot, texName] : passDesc.writeColors) {
                if (texName.empty()) {
                    continue;
                }
                auto texIt = texDescMap.find(texName);
                if (texIt != texDescMap.end() && texIt->second->isBackbuffer) {
                    colorTargets[slot] = m_importedColorTarget;
                } else {
                    // Non-backbuffer texture: look up pre-registered transient handle
                    auto rtIt = customRTHandles.find(texName);
                    if (rtIt != customRTHandles.end()) {
                        colorTargets[slot] = rtIt->second;
                    }
                }
            }
            // Default: if no color outputs declared and not a depth-only pass,
            // write to MSAA backbuffer at slot 0.
            // Shadow and semantic Depth passes have no color attachments.
            bool isShadowPassAction = (passDesc.action == GraphPassActionType::DrawShadowCasters);
            const bool isDepthOnlyMaterialPass = passDesc.action == GraphPassActionType::DrawRenderers &&
                                                 (passDesc.shaderTarget == ShaderCompileTarget::Depth ||
                                                  passDesc.shaderTarget == ShaderCompileTarget::Shadow);
            if (colorTargets.empty() && !isShadowPassAction && !isDepthOnlyMaterialPass) {
                colorTargets[0] = m_importedColorTarget;
            }
            // Primary color target (slot 0) — used for MSAA resolve and compute fallback
            vk::ResourceHandle primaryColorTarget = colorTargets.count(0) ? colorTargets[0] : vk::ResourceHandle{};
            const bool writesBackbuffer = primaryColorTarget.IsValid() && m_importedColorTarget.IsValid() &&
                                          primaryColorTarget.id == m_importedColorTarget.id;

            // Collect non-depth read texture handles for builder.Read()
            // This creates proper DAG edges and Vulkan barriers for
            // color texture dependencies between passes.
            std::vector<vk::ResourceHandle> colorReadHandles;
            bool readsDepth = false;
            for (const auto &readTex : passDesc.readTextures) {
                auto texIt = texDescMap.find(readTex);
                if (texIt != texDescMap.end()) {
                    if (texIt->second->isDepth) {
                        const bool sampledInput =
                            std::any_of(passDesc.inputBindings.begin(), passDesc.inputBindings.end(),
                                        [&readTex](const auto &binding) { return binding.second == readTex; });
                        readsDepth |= !sampledInput;
                    } else if (!texIt->second->isBackbuffer) {
                        // Non-depth, non-backbuffer read: look up custom RT handle
                        auto rtIt = customRTHandles.find(readTex);
                        if (rtIt != customRTHandles.end()) {
                            colorReadHandles.push_back(rtIt->second);
                        }
                    }
                }
            }

            // Build input binding handles: map sampler name → ResourceHandle
            // so the execute lambda can resolve VkImageViews at runtime.
            struct InputBindingHandle
            {
                std::string samplerName;
                vk::ResourceHandle handle;
                bool isDepth = false;
            };
            std::vector<InputBindingHandle> inputBindingHandles;
            for (const auto &[samplerName, textureName] : passDesc.inputBindings) {
                auto texIt = texDescMap.find(textureName);
                if (texIt != texDescMap.end() && texIt->second->isBackbuffer) {
                    // Backbuffer texture — use the imported color target
                    inputBindingHandles.push_back({samplerName, m_importedColorTarget, false});
                } else {
                    auto rtIt = customRTHandles.find(textureName);
                    if (rtIt != customRTHandles.end()) {
                        bool isDepthInput = (texIt != texDescMap.end()) ? texIt->second->isDepth : false;
                        inputBindingHandles.push_back({samplerName, rtIt->second, isDepthInput});
                    } else {
                        INXLOG_WARN("SceneRenderGraph: Input binding '", samplerName, "' references unknown texture '",
                                    textureName, "'");
                    }
                }
            }

            // Determine depth relationship
            bool writesDepth = !passDesc.writeDepth.empty();

            // Read clear values from the Python graph description, but
            // allow camera ClearFlags to override the first color-clearing pass.
            bool clearColor = passDesc.clearColor;
            bool clearDepth = passDesc.clearDepth;
            float clearColorR = passDesc.clearColorR;
            float clearColorG = passDesc.clearColorG;
            float clearColorB = passDesc.clearColorB;
            float clearColorA = passDesc.clearColorA;
            float clearDepthVal = passDesc.clearDepthValue;

            // Apply camera-driven clear overrides to the first pass that clears color.
            if (m_hasCameraClearOverride && passDesc.clearColor) {
                switch (m_cameraClearFlags) {
                case CameraClearFlags::Skybox:
                    clearColor = true;
                    clearDepth = true;
                    clearColorR = 0.0f;
                    clearColorG = 0.0f;
                    clearColorB = 0.0f;
                    clearColorA = 1.0f;
                    break;
                case CameraClearFlags::SolidColor:
                    clearColor = true;
                    clearDepth = true;
                    clearColorR = m_cameraBgColor.r;
                    clearColorG = m_cameraBgColor.g;
                    clearColorB = m_cameraBgColor.b;
                    clearColorA = m_cameraBgColor.a;
                    break;
                case CameraClearFlags::DepthOnly:
                    clearColor = false;
                    clearDepth = true;
                    break;
                case CameraClearFlags::DontClear:
                    clearColor = false;
                    clearDepth = false;
                    break;
                }
                // Record the pass name for per-frame clear-value updates.
                // Execute() uses this to update clear values without
                // rebuilding the graph.
                m_mainClearPassName = passDesc.name;
                // Only override the first eligible pass
                m_hasCameraClearOverride = false;
            }

            // Capture depth state for the lambda (by value — sharedDepth
            // is updated between iterations so we capture the CURRENT value)
            vk::ResourceHandle depthForThisPass = sharedDepth;
            bool needsCreateDepth = writesDepth && !sharedDepth.IsValid();
            bool passReadsDepth = readsDepth && !writesDepth;

            // MSAA resolve is performed explicitly after graph execution
            // (ResolveSceneMsaa) to keep ALL passes compatible with
            // m_internalRenderPass (which has no resolve attachment).
            // Using subpass resolve would add a resolve attachment to this
            // pass's VkRenderPass, making it incompatible with pipelines
            // created against m_internalRenderPass (attachment count mismatch).
            vk::ResourceHandle resolveTarget;

            // =================================================================
            // FullscreenQuad passes: fullscreen triangle with named shader,
            // push constants, and input texture sampling.
            // Uses FullscreenRenderer to manage pipeline cache + draw.
            //
            // MSAA handling:
            //   - Reading the MSAA backbuffer: a multisample image cannot be
            //     sampled by a regular sampler2D.  When MSAA is active, an
            //     automatic transfer pass resolves the backbuffer to the 1x
            //     resolve target before the first FullscreenQuad that reads
            //     it.  Subsequent reads reference the 1x resolve target.
            //   - Writing to the MSAA backbuffer: the pipeline sample count
            //     must match the render pass attachment. We propagate the
            //     actual MSAA sample count into the FullscreenPipelineKey.
            // =================================================================
            if (passDesc.action == GraphPassActionType::FullscreenQuad) {

                // ------ MSAA auto-resolve for backbuffer reads ------
                if (msaaSamples > VK_SAMPLE_COUNT_1_BIT && m_importedResolveTarget.IsValid()) {
                    bool readsBackbuffer = false;
                    for (const auto &readTex : passDesc.readTextures) {
                        auto texIt = texDescMap.find(readTex);
                        if (texIt != texDescMap.end() && texIt->second->isBackbuffer) {
                            readsBackbuffer = true;
                            break;
                        }
                    }
                    if (readsBackbuffer && backbufferDirtySinceResolve) {
                        // Insert a transfer pass that resolves MSAA → 1x.
                        // The render graph handles layout transitions via
                        // TransferRead / TransferWrite declarations.
                        auto importedColor = m_importedColorTarget;
                        auto importedResolve = m_importedResolveTarget;
                        VkImage msaaImage = m_sceneTarget->GetMsaaColorImage();
                        VkImage resolveImage = m_sceneTarget->GetColorImage();
                        uint32_t resolveW = width;
                        uint32_t resolveH = height;
                        std::string resolvePassName =
                            "__MSAA_resolve_pre_fs_" + std::to_string(msaaResolvePassCounter++);
                        vk::ResourceHandle resolvedVersion;

                        m_renderGraph->AddTransferPass(resolvePassName, [importedColor, importedResolve, resolveW,
                                                                         resolveH, msaaImage, resolveImage,
                                                                         &resolvedVersion](vk::PassBuilder &builder) {
                            builder.TransferRead(importedColor);
                            resolvedVersion = builder.TransferWrite(importedResolve);
                            builder.SetRenderArea(resolveW, resolveH);

                            return [msaaImage, resolveImage, resolveW, resolveH](vk::RenderContext &ctx) {
                                VkImageResolve region{};
                                region.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
                                region.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
                                region.extent = {resolveW, resolveH, 1};

                                vkCmdResolveImage(ctx.GetCommandBuffer(), msaaImage,
                                                  VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, resolveImage,
                                                  VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
                            };
                        });
                        publishResourceVersion(resolvedVersion);
                        backbufferDirtySinceResolve = false;
                    }
                }

                // Capture references for the execute lambda
                FullscreenRenderer *fsRenderer = &m_fullscreenRenderer;
                vk::RenderGraph *renderGraphPtr = m_renderGraph.get();
                std::string shaderName = passDesc.shaderName;
                FullscreenPushConstants packedPushConstants{};
                uint32_t packedPushConstantSize = 0;
                for (const auto &[name, value] : passDesc.pushConstants) {
                    if (packedPushConstantSize / sizeof(float) < 32) {
                        packedPushConstants.values[packedPushConstantSize / sizeof(float)] = value;
                        packedPushConstantSize += sizeof(float);
                    } else {
                        INXLOG_ERROR("FullscreenQuad '", shaderName,
                                     "': push constants exceed 128 bytes (32 floats), truncating '", name, "'");
                        break;
                    }
                }

                // Input textures for FullscreenQuad sampling.
                // When inputBindings are specified, use them to determine
                // binding order (binding 0 = first inputBinding, etc.).
                // This ensures named sampler→texture mappings align with
                // the descriptor set layout.  Fall back to readTextures
                // order when no inputBindings are declared (single-input
                // effects that just call read()).
                std::vector<vk::ResourceHandle> fsReadHandles;
                std::vector<bool> fsIsDepthInputs;
                if (!passDesc.inputBindings.empty()) {
                    // Use inputBindings order for deterministic sampler→binding mapping
                    for (const auto &[samplerName, textureName] : passDesc.inputBindings) {
                        auto texIt = texDescMap.find(textureName);
                        if (texIt == texDescMap.end())
                            continue;
                        if (texIt->second->isBackbuffer) {
                            if (msaaSamples > VK_SAMPLE_COUNT_1_BIT && m_importedResolveTarget.IsValid()) {
                                fsReadHandles.push_back(m_importedResolveTarget);
                            } else {
                                fsReadHandles.push_back(m_importedColorTarget);
                            }
                            fsIsDepthInputs.push_back(false);
                        } else {
                            // Allow both color and depth textures as sampler inputs
                            // for fullscreen effects (e.g. SSAO reads depth as sampler2D).
                            // Always use Read() (→ SHADER_READ_ONLY_OPTIMAL) since these
                            // are sampled textures, NOT depth attachments.  Shadow maps
                            // and other depth-formatted textures are read with a regular
                            // combined-image-sampler descriptor, not as depth attachments.
                            auto rtIt = customRTHandles.find(textureName);
                            if (rtIt != customRTHandles.end()) {
                                fsReadHandles.push_back(rtIt->second);
                                fsIsDepthInputs.push_back(false);
                            }
                        }
                    }
                } else {
                    // Default path: use readTextures order (colorReadHandles + backbuffer)
                    // for simple single-input effects that call read() without explicit inputBindings.
                    fsReadHandles = colorReadHandles;
                    fsIsDepthInputs.assign(fsReadHandles.size(), false);
                    for (const auto &readTex : passDesc.readTextures) {
                        auto texIt = texDescMap.find(readTex);
                        if (texIt != texDescMap.end() && texIt->second->isBackbuffer) {
                            if (msaaSamples > VK_SAMPLE_COUNT_1_BIT && m_importedResolveTarget.IsValid()) {
                                fsReadHandles.push_back(m_importedResolveTarget);
                            } else {
                                fsReadHandles.push_back(m_importedColorTarget);
                            }
                            fsIsDepthInputs.push_back(false);
                        }
                    }
                }

                // Determine output target (primary color)
                vk::ResourceHandle fsOutputTarget = primaryColorTarget;
                vk::ResourceHandle fsWrittenVersion;

                // Determine MSAA sample count and output format.
                // When writing to the MSAA backbuffer the pipeline sample
                // count must match the render pass attachment.
                rhi::SampleCount fsSamples = rhi::SampleCount::One;
                rhi::PixelFormat fsColorFormat = rhi::FromVkFormat(m_sceneTarget->GetColorFormat());
                for (const auto &[slot, texName] : passDesc.writeColors) {
                    if (slot == 0 && !texName.empty()) {
                        auto texIt = texDescMap.find(texName);
                        if (texIt != texDescMap.end()) {
                            if (texIt->second->isBackbuffer && msaaSamples > VK_SAMPLE_COUNT_1_BIT) {
                                fsSamples = rhi::FromVkSampleCount(msaaSamples);
                            }
                            if (!texIt->second->isBackbuffer && texIt->second->format != rhi::PixelFormat::Undefined) {
                                fsColorFormat = texIt->second->format;
                            }
                        }
                    }
                }

                // Determine pass dimensions (check output texture sizeDivisor)
                uint32_t fsPassWidth = width;
                uint32_t fsPassHeight = height;
                for (const auto &[slot, texName] : passDesc.writeColors) {
                    if (slot == 0 && !texName.empty()) {
                        auto texIt = texDescMap.find(texName);
                        if (texIt != texDescMap.end() && texIt->second->sizeDivisor > 1) {
                            fsPassWidth = std::max(1u, width / texIt->second->sizeDivisor);
                            fsPassHeight = std::max(1u, height / texIt->second->sizeDivisor);
                        }
                    }
                }

                m_renderGraph->AddPass(passDesc.name, [=, &fsWrittenVersion](vk::PassBuilder &builder) {
                    // Declare read dependencies for DAG edges + barriers
                    for (size_t i = 0; i < fsReadHandles.size(); ++i) {
                        if (i < fsIsDepthInputs.size() && fsIsDepthInputs[i]) {
                            builder.ReadSampledDepth(fsReadHandles[i]);
                        } else {
                            builder.Read(fsReadHandles[i]);
                        }
                    }
                    // Declare color output
                    fsWrittenVersion = builder.WriteColor(fsOutputTarget, 0);
                    builder.SetRenderArea(fsPassWidth, fsPassHeight);

                    return [=, cachedRenderTarget = rhi::RenderTargetLayoutHandle{}](vk::RenderContext &ctx) mutable {
                        if (!cachedRenderTarget.IsValid()) {
                            cachedRenderTarget = renderGraphPtr->GetPassRenderTargetLayout(passDesc.name);
                        }
                        if (!cachedRenderTarget.IsValid())
                            return;

                        // Resolve input texture views using a stack path for the common case.
                        rhi::TextureViewHandle inputViewsStack[8] = {};
                        bool depthInputsStack[8] = {};
                        std::vector<rhi::TextureViewHandle> inputViewsHeap;
                        rhi::TextureViewHandle *inputViews = inputViewsStack;
                        bool *depthInputs = depthInputsStack;
                        if (fsReadHandles.size() > 8) {
                            inputViewsHeap.resize(fsReadHandles.size());
                            inputViews = inputViewsHeap.data();
                        }
                        for (size_t i = 0; i < std::min<size_t>(fsIsDepthInputs.size(), 8); ++i) {
                            depthInputsStack[i] = fsIsDepthInputs[i];
                        }
                        const uint32_t inputViewCount = static_cast<uint32_t>(fsReadHandles.size());
                        for (uint32_t i = 0; i < inputViewCount; ++i) {
                            inputViews[i] = ctx.GetTextureView(fsReadHandles[i]);
                            if (!inputViews[i].IsValid()) {
                                INXLOG_ERROR("FullscreenQuad '", shaderName,
                                             "': input texture view is unavailable at binding ", i);
                                return;
                            }
                        }

                        // Build pipeline key and ensure pipeline exists
                        FullscreenPipelineKey key;
                        key.shaderName = shaderName;
                        key.renderTargetLayout = cachedRenderTarget;
                        key.samples = fsSamples;
                        key.colorFormat = fsColorFormat;
                        key.inputTextureCount = inputViewCount;

                        const auto &entry = fsRenderer->EnsurePipeline(key);
                        if (!entry.pipeline.IsValid())
                            return;

                        // Allocate descriptor set for input textures
                        std::unique_ptr<bool[]> depthInputsOwned;
                        if (fsReadHandles.size() > 8) {
                            depthInputsOwned = std::make_unique<bool[]>(fsReadHandles.size());
                            depthInputs = depthInputsOwned.get();
                            for (size_t i = 0; i < fsIsDepthInputs.size(); ++i) {
                                depthInputs[i] = fsIsDepthInputs[i];
                            }
                        }

                        const auto bindGroup = fsRenderer->AllocateBindGroup(
                            entry.inputLayout, inputViews, inputViewCount,
                            fsIsDepthInputs.empty() ? nullptr : depthInputs, fsRenderer->GetLinearSampler());
                        if (!bindGroup.IsValid()) {
                            INXLOG_ERROR("FullscreenQuad '", shaderName, "': descriptor pool exhausted, skipping pass");
                            return;
                        }

                        // Draw fullscreen triangle
                        fsRenderer->Draw(ctx.GetGraphicsCommandEncoder(), entry, bindGroup, packedPushConstants,
                                         packedPushConstantSize);
                    };
                });
                publishResourceVersion(fsWrittenVersion);

                if (writesBackbuffer) {
                    backbufferDirtySinceResolve = true;
                }
                continue;
            }

            std::vector<vk::ResourceHandle> writtenColorVersions;
            vk::ResourceHandle writtenDepthVersion;
            vk::ResourceHandle writtenResolveVersion;
            m_renderGraph->AddPass(passDesc.name, [=, &sharedDepth, &writtenColorVersions, &writtenDepthVersion,
                                                   &writtenResolveVersion](vk::PassBuilder &builder) {
                // Local alias to make vkCore capturable by nested lambdas (MSVC C3481)
                InxVkCoreModular *localVkCore = vkCore;

                // ----- Determine pass dimensions -----
                // Shadow caster passes may use custom-sized depth textures.
                // Determine the actual pass dimensions from the depth target.
                uint32_t passWidth = width;
                uint32_t passHeight = height;
                bool isShadowPass = (passDesc.action == GraphPassActionType::DrawShadowCasters);

                // Use the primary output's declared extent for offscreen passes.
                if (!passDesc.writeColors.empty()) {
                    const auto primary =
                        std::min_element(passDesc.writeColors.begin(), passDesc.writeColors.end(),
                                         [](const auto &left, const auto &right) { return left.first < right.first; });
                    auto colorTexIt = texDescMap.find(primary->second);
                    if (colorTexIt != texDescMap.end() && !colorTexIt->second->isBackbuffer) {
                        if (colorTexIt->second->width > 0 && colorTexIt->second->height > 0) {
                            passWidth = colorTexIt->second->width;
                            passHeight = colorTexIt->second->height;
                        } else if (colorTexIt->second->sizeDivisor > 1) {
                            passWidth = std::max(1u, width / colorTexIt->second->sizeDivisor);
                            passHeight = std::max(1u, height / colorTexIt->second->sizeDivisor);
                        }
                    }
                } else if (!passDesc.writeDepth.empty()) {
                    auto depthTexIt = texDescMap.find(passDesc.writeDepth);
                    if (depthTexIt != texDescMap.end()) {
                        if (depthTexIt->second->width > 0)
                            passWidth = depthTexIt->second->width;
                        if (depthTexIt->second->height > 0)
                            passHeight = depthTexIt->second->height;
                        if (depthTexIt->second->sizeDivisor > 1) {
                            passWidth = std::max(1u, width / depthTexIt->second->sizeDivisor);
                            passHeight = std::max(1u, height / depthTexIt->second->sizeDivisor);
                        }
                    }
                }

                // ----- Depth -----
                vk::ResourceHandle depth;
                if (!passDesc.writeDepth.empty()) {
                    // Fixed-size and divided depth targets are pre-registered as 1x resources.
                    auto rtIt = customRTHandles.find(passDesc.writeDepth);
                    if (rtIt != customRTHandles.end()) {
                        depth = rtIt->second;
                        writtenDepthVersion = builder.WriteDepth(depth);
                        depth = writtenDepthVersion;
                    } else if (isShadowPass) {
                        // Fallback: create inline
                        auto depthTexIt = texDescMap.find(passDesc.writeDepth);
                        VkFormat shadowDepthFmt = depthTexIt != texDescMap.end()
                                                      ? rhi::ToVkFormat(depthTexIt->second->format)
                                                      : VK_FORMAT_D32_SFLOAT;
                        depth = builder.CreateDepthStencil(passDesc.writeDepth, passWidth, passHeight, shadowDepthFmt,
                                                           VK_SAMPLE_COUNT_1_BIT);
                        writtenDepthVersion = builder.WriteDepth(depth);
                        depth = writtenDepthVersion;
                    }
                }
                if (!depth.IsValid() && needsCreateDepth) {
                    // First pass that writes depth: create the shared resource
                    depth = builder.CreateDepthStencil("SceneDepth", width, height, depthFormat, msaaSamples);
                    writtenDepthVersion = builder.WriteDepth(depth);
                    depth = writtenDepthVersion;
                    // Store for subsequent passes (captured by ref)
                    sharedDepth = depth;
                } else if (!depth.IsValid() && writesDepth && depthForThisPass.IsValid()) {
                    // Later pass that also writes depth (rare)
                    writtenDepthVersion = builder.WriteDepth(depthForThisPass);
                    if (sharedDepth.IsValid() && writtenDepthVersion.IsValid() &&
                        sharedDepth.id == writtenDepthVersion.id) {
                        sharedDepth = writtenDepthVersion;
                    }
                } else if (!depth.IsValid() && passReadsDepth && depthForThisPass.IsValid()) {
                    // Pass reads depth (e.g., skybox, transparent) — attach as read-only
                    builder.ReadDepth(depthForThisPass);
                }

                // ----- Color reads (non-depth textures) -----
                // Declare Read() for each color texture this pass reads.
                // This creates proper DAG edges and Vulkan barriers.
                for (const auto &readHandle : colorReadHandles) {
                    builder.Read(readHandle);
                }

                // ----- Input binding reads (sampled textures, e.g. shadow map) -----
                // Input bindings reference textures by name for descriptor
                // binding at draw time. We also need DAG edges here so that:
                //   1. The writer pass is not dead-pass-culled.
                //   2. Vulkan barriers transition the texture for shader read.
                for (const auto &binding : inputBindingHandles) {
                    if (binding.isDepth) {
                        builder.ReadSampledDepth(binding.handle);
                    } else {
                        builder.Read(binding.handle);
                    }
                }

                // ----- Color outputs (MRT) -----
                // Write all declared color targets at their respective slots.
                for (const auto &[slot, handle] : colorTargets) {
                    auto written = builder.WriteColor(handle, slot);
                    if (written.IsValid())
                        writtenColorVersions.push_back(written);
                }

                // ----- MSAA Resolve (only on the last backbuffer pass) -----
                if (resolveTarget.IsValid()) {
                    writtenResolveVersion = builder.WriteResolve(resolveTarget);
                }

                // ----- Render area -----
                builder.SetRenderArea(passWidth, passHeight);

                // ----- Clear values -----
                if (clearColor) {
                    builder.SetClearColor(clearColorR, clearColorG, clearColorB, clearColorA);
                }
                if (clearDepth) {
                    builder.SetClearDepth(clearDepthVal, 0);
                }

                for (const auto &binding : inputBindingHandles) {
                    if (binding.samplerName == "shadowMap" && !m_shadowMapInputHandle.IsValid()) {
                        m_shadowMapInputHandle = binding.handle;
                        m_shadowMapInputIsDepth = binding.isDepth;
                    }
                }

                return [callback, passWidth, passHeight, inputBindingHandles, isShadowPass](vk::RenderContext &ctx) {
                    if (callback)
                        callback(ctx, passWidth, passHeight);
                };
            });

            for (const auto &written : writtenColorVersions)
                publishResourceVersion(written);
            publishResourceVersion(writtenDepthVersion);
            publishResourceVersion(writtenResolveVersion);

            // After scene-pass AddPass completes (setup lambda ran synchronously),
            // sharedDepth is now valid.  Register it in customRTHandles under
            // all scene-size depth texture names so subsequent fullscreen quad
            // passes can reference depth via inputBindings / set_input().
            if (sharedDepth.IsValid()) {
                for (const auto &tex : m_pythonGraphDesc.textures) {
                    if (tex.isDepth && tex.width == 0 && tex.height == 0 &&
                        customRTHandles.find(tex.name) == customRTHandles.end()) {
                        customRTHandles[tex.name] = sharedDepth;
                    }
                }
            }

            if (writesBackbuffer) {
                backbufferDirtySinceResolve = true;
            }
        }

        // ====================================================================
        // Auto-append system passes: component gizmos, editor gizmos,
        // and editor tools — all draw into the backbuffer with depth testing.
        // ====================================================================
        m_importedColorTarget = AppendAutoPass("_ComponentGizmos", m_importedColorTarget, sharedDepth, width, height);
        m_importedColorTarget = AppendAutoPass("_EditorGizmos", m_importedColorTarget, sharedDepth, width, height);
        m_importedColorTarget = AppendAutoPass("_EditorTools", m_importedColorTarget, sharedDepth, width, height);
    }

    // Set output for proper resource tracking and dead-pass culling.
    FinalizeGraphOutput(customRTHandles);

    // Debug: Log the passes added to the render graph
    INXLOG_DEBUG("SceneRenderGraph::BuildRenderGraph - Built ", m_renderGraph->GetPassCount(), " passes from ",
                 m_pythonGraphDesc.passes.size(),
                 " Python passes + editor auto-appended passes. "
                 "Output: ",
                 m_pythonGraphDesc.outputTexture.empty() ? "(backbuffer)" : m_pythonGraphDesc.outputTexture);

    ++m_graphBuildRevision;
    m_graphBuilt = true;
}

} // namespace infernux
