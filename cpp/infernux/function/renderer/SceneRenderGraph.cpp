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
#include "particle/ParticleGpuDrawRegistry.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkDeviceContext.h"
#include "vk/VkPipelineManager.h"
#include "vk/VkRenderUtils.h"
#include <algorithm>
#include <cmath>
#include <core/config/EngineConfig.h>
#include <core/error/InxError.h>
#include <cstring>
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

bool BufferDescEquals(const GraphBufferDesc &a, const GraphBufferDesc &b)
{
    return a.name == b.name && a.byteSize == b.byteSize && a.usage == b.usage;
}

bool BufferAccessEquals(const GraphBufferAccessDesc &a, const GraphBufferAccessDesc &b)
{
    return a.resource == b.resource && a.type == b.type;
}

bool CommandDescEquals(const GraphCommandDesc &a, const GraphCommandDesc &b)
{
    const auto parameterLayoutEquals = [](const GraphCommandDesc &lhs, const GraphCommandDesc &rhs) {
        if (lhs.parameterBlock.empty())
            return lhs.pushConstants == rhs.pushConstants;
        if (lhs.pushConstants.size() != rhs.pushConstants.size())
            return false;
        for (size_t i = 0; i < lhs.pushConstants.size(); ++i) {
            if (lhs.pushConstants[i].first != rhs.pushConstants[i].first)
                return false;
        }
        return true;
    };
    return a.type == b.type && a.shaderTarget == b.shaderTarget && a.queueMin == b.queueMin &&
           a.queueMax == b.queueMax && a.sortMode == b.sortMode && a.passTag == b.passTag &&
           a.overrideMaterial == b.overrideMaterial && a.lightIndex == b.lightIndex &&
           a.screenUIList == b.screenUIList && a.shaderName == b.shaderName && a.parameterBlock == b.parameterBlock &&
           parameterLayoutEquals(a, b) && a.inputBindings == b.inputBindings && a.sourceResource == b.sourceResource &&
           a.destinationResource == b.destinationResource && a.copyBytes == b.copyBytes;
}

bool CommandListEquals(const std::vector<GraphCommandDesc> &a, const std::vector<GraphCommandDesc> &b)
{
    return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), [](const auto &lhs, const auto &rhs) {
               return CommandDescEquals(lhs, rhs);
           });
}

GraphCommandDesc LegacyCommand(const GraphPassDesc &pass)
{
    GraphCommandDesc command;
    switch (pass.action) {
    case GraphPassActionType::DrawRenderers:
        command.type = GraphCommandType::DrawRenderers;
        break;
    case GraphPassActionType::DrawSkybox:
        command.type = GraphCommandType::DrawSkybox;
        break;
    case GraphPassActionType::DrawShadowCasters:
        command.type = GraphCommandType::DrawShadowCasters;
        break;
    case GraphPassActionType::DrawScreenUI:
        command.type = GraphCommandType::DrawScreenUI;
        break;
    case GraphPassActionType::FullscreenQuad:
        command.type = GraphCommandType::FullscreenQuad;
        break;
    default:
        return {};
    }
    command.shaderTarget = pass.shaderTarget;
    command.queueMin = pass.queueMin;
    command.queueMax = pass.queueMax;
    command.sortMode = pass.sortMode;
    command.passTag = pass.passTag;
    command.overrideMaterial = pass.overrideMaterial;
    command.lightIndex = pass.lightIndex;
    command.screenUIList = pass.screenUIList;
    command.shaderName = pass.shaderName;
    command.parameterBlock.clear();
    command.pushConstants = pass.pushConstants;
    command.inputBindings = pass.inputBindings;
    return command;
}

RenderGraphDescription NormalizeGraphCommands(const RenderGraphDescription &source)
{
    RenderGraphDescription normalized = source;
    for (auto &pass : normalized.passes) {
        if (!pass.commands.empty() || pass.action == GraphPassActionType::None ||
            pass.action == GraphPassActionType::Custom) {
            continue;
        }
        pass.commands.push_back(LegacyCommand(pass));
    }
    return normalized;
}

const GraphCommandDesc *PrimaryCommand(const GraphPassDesc &pass)
{
    return pass.commands.empty() ? nullptr : &pass.commands.front();
}

VkBufferUsageFlags ToVkBufferUsage(uint32_t usage)
{
    VkBufferUsageFlags result = 0;
    if ((usage & static_cast<uint32_t>(GraphBufferUsage::Storage)) != 0)
        result |= VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    if ((usage & static_cast<uint32_t>(GraphBufferUsage::Indirect)) != 0)
        result |= VK_BUFFER_USAGE_INDIRECT_BUFFER_BIT;
    if ((usage & static_cast<uint32_t>(GraphBufferUsage::TransferSource)) != 0)
        result |= VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    if ((usage & static_cast<uint32_t>(GraphBufferUsage::TransferDestination)) != 0)
        result |= VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    return result;
}

bool PassDescEquals(const GraphPassDesc &a, const GraphPassDesc &b)
{
    return a.name == b.name && a.type == b.type && a.readTextures == b.readTextures && a.writeColors == b.writeColors &&
           a.writeDepth == b.writeDepth && a.clearColor == b.clearColor && a.clearDepth == b.clearDepth &&
           a.clearColorR == b.clearColorR && a.clearColorG == b.clearColorG && a.clearColorB == b.clearColorB &&
           a.clearColorA == b.clearColorA && a.clearDepthValue == b.clearDepthValue && a.sideEffect == b.sideEffect &&
           a.bufferAccesses.size() == b.bufferAccesses.size() &&
           std::equal(a.bufferAccesses.begin(), a.bufferAccesses.end(), b.bufferAccesses.begin(), BufferAccessEquals) &&
           CommandListEquals(a.commands, b.commands);
}

bool GraphDescEquals(const RenderGraphDescription &a, const RenderGraphDescription &b)
{
    if (a.name != b.name || a.outputTexture != b.outputTexture || a.msaaSamples != b.msaaSamples ||
        a.textures.size() != b.textures.size() || a.buffers.size() != b.buffers.size() ||
        a.passes.size() != b.passes.size()) {
        return false;
    }

    for (size_t i = 0; i < a.buffers.size(); ++i) {
        if (!BufferDescEquals(a.buffers[i], b.buffers[i])) {
            return false;
        }
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

    constexpr uint32_t kKnownBufferUsages = static_cast<uint32_t>(GraphBufferUsage::Storage) |
                                            static_cast<uint32_t>(GraphBufferUsage::Indirect) |
                                            static_cast<uint32_t>(GraphBufferUsage::TransferSource) |
                                            static_cast<uint32_t>(GraphBufferUsage::TransferDestination);
    std::unordered_map<std::string, const GraphBufferDesc *> buffers;
    buffers.reserve(desc.buffers.size());
    for (const auto &buffer : desc.buffers) {
        if (buffer.name.empty() || textures.find(buffer.name) != textures.end()) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: invalid or duplicate buffer name '", buffer.name, "'");
            return false;
        }
        if (!buffers.emplace(buffer.name, &buffer).second || buffer.byteSize == 0 || buffer.usage == 0 ||
            (buffer.usage & ~kKnownBufferUsages) != 0) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: invalid buffer description for '", buffer.name, "'");
            return false;
        }
    }

    std::unordered_set<std::string> passNames;
    passNames.reserve(desc.passes.size());
    std::unordered_set<std::string> parameterBlockIds;
    for (const auto &pass : desc.passes) {
        if (pass.name.empty()) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass name cannot be empty");
            return false;
        }
        if (!passNames.insert(pass.name).second) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: duplicate pass '", pass.name, "'");
            return false;
        }
        if (pass.commands.size() > 1) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                         "' records multiple commands; command-list execution is not enabled yet");
            return false;
        }
        const GraphCommandDesc *command = PrimaryCommand(pass);
        if (command) {
            if (command->pushConstants.size() > 32) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                             "' exceeds the 32-float push constant limit");
                return false;
            }
            std::unordered_set<std::string> parameterNames;
            for (const auto &[name, value] : command->pushConstants) {
                if (name.empty() || !parameterNames.insert(name).second || !std::isfinite(value)) {
                    INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                                 "' has an invalid push constant layout");
                    return false;
                }
            }
            if (!command->parameterBlock.empty()) {
                if (command->type != GraphCommandType::FullscreenQuad ||
                    !parameterBlockIds.insert(command->parameterBlock).second) {
                    INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                                 "' has an invalid or duplicate runtime parameter block '", command->parameterBlock,
                                 "'");
                    return false;
                }
            }
        }
        const bool rasterCommand =
            command &&
            (command->type == GraphCommandType::DrawRenderers || command->type == GraphCommandType::DrawSkybox ||
             command->type == GraphCommandType::DrawShadowCasters || command->type == GraphCommandType::DrawScreenUI ||
             command->type == GraphCommandType::FullscreenQuad);
        const bool copyCommand = command && (command->type == GraphCommandType::CopyTexture ||
                                             command->type == GraphCommandType::CopyBuffer);
        if ((pass.type == GraphPassType::Raster && command && !rasterCommand) ||
            (pass.type == GraphPassType::Compute && command) || (pass.type == GraphPassType::Copy && !copyCommand) ||
            (pass.type == GraphPassType::Present && (!command || command->type != GraphCommandType::Present))) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                         "' command does not match its execution domain");
            return false;
        }
        if (pass.type != GraphPassType::Raster &&
            (!pass.writeColors.empty() || !pass.writeDepth.empty() || pass.clearColor || pass.clearDepth)) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: non-raster pass '", pass.name,
                         "' declares raster attachments");
            return false;
        }
        for (const auto &access : pass.bufferAccesses) {
            const auto buffer = buffers.find(access.resource);
            if (buffer == buffers.end()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name, "' references unknown buffer '",
                             access.resource, "'");
                return false;
            }
            uint32_t requiredUsage = 0;
            switch (access.type) {
            case GraphBufferAccessType::StorageRead:
            case GraphBufferAccessType::StorageWrite:
                requiredUsage = static_cast<uint32_t>(GraphBufferUsage::Storage);
                break;
            case GraphBufferAccessType::IndirectRead:
                requiredUsage = static_cast<uint32_t>(GraphBufferUsage::Indirect);
                break;
            case GraphBufferAccessType::TransferRead:
                requiredUsage = static_cast<uint32_t>(GraphBufferUsage::TransferSource);
                break;
            case GraphBufferAccessType::TransferWrite:
                requiredUsage = static_cast<uint32_t>(GraphBufferUsage::TransferDestination);
                break;
            }
            if ((buffer->second->usage & requiredUsage) == 0) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name, "' buffer '", access.resource,
                             "' does not declare the required usage");
                return false;
            }
        }
        if (command && command->type == GraphCommandType::CopyTexture) {
            const auto source = textures.find(command->sourceResource);
            const auto destination = textures.find(command->destinationResource);
            if (source == textures.end() || destination == textures.end() || source == destination ||
                source->second->isBackbuffer || destination->second->isBackbuffer ||
                source->second->format != destination->second->format) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture copy pass '", pass.name,
                             "' has incompatible resources");
                return false;
            }
        } else if (command && command->type == GraphCommandType::CopyBuffer) {
            const auto source = buffers.find(command->sourceResource);
            const auto destination = buffers.find(command->destinationResource);
            if (source == buffers.end() || destination == buffers.end() ||
                command->sourceResource == command->destinationResource ||
                command->copyBytes > std::min(source->second->byteSize, destination->second->byteSize)) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: buffer copy pass '", pass.name,
                             "' has incompatible resources");
                return false;
            }
            const uint32_t transferSource = static_cast<uint32_t>(GraphBufferUsage::TransferSource);
            const uint32_t transferDestination = static_cast<uint32_t>(GraphBufferUsage::TransferDestination);
            if ((source->second->usage & transferSource) == 0 ||
                (destination->second->usage & transferDestination) == 0) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: buffer copy pass '", pass.name,
                             "' resources do not declare transfer usage");
                return false;
            }
        } else if (command && command->type == GraphCommandType::Present &&
                   textures.find(command->sourceResource) == textures.end()) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: present pass '", pass.name,
                         "' references an unknown texture");
            return false;
        } else if (command && command->type == GraphCommandType::Present &&
                   textures.at(command->sourceResource)->isDepth) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: present pass '", pass.name,
                         "' cannot export a depth texture");
            return false;
        }
        if (command && command->type == GraphCommandType::DrawShadowCasters) {
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

        const bool depthOnlyMaterialPass = command && command->type == GraphCommandType::DrawRenderers &&
                                           (command->shaderTarget == ShaderCompileTarget::Depth ||
                                            command->shaderTarget == ShaderCompileTarget::Shadow);
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
            if (command && command->type == GraphCommandType::DrawRenderers) {
                const bool singleSampleDepth =
                    (texIt->second->width > 0 && texIt->second->height > 0) || texIt->second->sizeDivisor > 1;
                writesSingleSampleTarget |= singleSampleDepth;
                writesBackbuffer |= !singleSampleDepth;
            }
        }

        if (command && command->type == GraphCommandType::DrawRenderers) {
            for (const auto &textureName : pass.readTextures) {
                const bool sampledInput =
                    std::any_of(command->inputBindings.begin(), command->inputBindings.end(),
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

        if (!command)
            continue;
        for (const auto &[samplerName, textureName] : command->inputBindings) {
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
    m_renderGraph->Initialize(&vkCore->GetDeviceContext(), &vkCore->GetPipelineManager(), &vkCore->GetDeletionQueue());

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
    m_parameterBlocks.clear();

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

    RenderGraphDescription normalizedDesc = NormalizeGraphCommands(desc);
    const VkSampleCountFlagBits callbackSamples = m_vkCore->GetMaterialPipelineManager().GetSampleCount();
    const bool topologyChanged = !m_hasPythonGraph || !GraphDescEquals(normalizedDesc, m_pythonGraphDesc);
    const bool callbackContractChanged = m_pythonCallbackSamples != callbackSamples;
    if (!topologyChanged && !callbackContractChanged) {
        std::vector<GraphParameterBlockUpdate> updates;
        for (const auto &pass : normalizedDesc.passes) {
            const GraphCommandDesc *command = PrimaryCommand(pass);
            if (command && !command->parameterBlock.empty()) {
                updates.push_back({command->parameterBlock, 0, command->pushConstants});
            }
        }
        UpdateParameterBlocks(updates);
        if (desc.sourceRevision != 0) {
            m_pythonGraphSourceRevision = desc.sourceRevision;
            m_pythonGraphDesc.sourceRevision = desc.sourceRevision;
        }
        return;
    }

    if (topologyChanged && !ValidatePythonGraphDescription(normalizedDesc)) {
        return;
    }

    if (topologyChanged) {
        std::unordered_map<std::string, RuntimeParameterBlock> blocks;
        for (const auto &pass : normalizedDesc.passes) {
            const GraphCommandDesc *command = PrimaryCommand(pass);
            if (!command || command->parameterBlock.empty())
                continue;
            RuntimeParameterBlock block;
            block.names.reserve(command->pushConstants.size());
            for (size_t i = 0; i < command->pushConstants.size(); ++i) {
                block.names.push_back(command->pushConstants[i].first);
                block.values.values[i] = command->pushConstants[i].second;
            }
            block.byteSize = static_cast<uint32_t>(command->pushConstants.size() * sizeof(float));
            blocks.emplace(command->parameterBlock, std::move(block));
        }
        m_parameterBlocks = std::move(blocks);
    } else if (desc.sourceRevision != m_pythonGraphSourceRevision) {
        std::vector<GraphParameterBlockUpdate> updates;
        for (const auto &pass : normalizedDesc.passes) {
            const GraphCommandDesc *command = PrimaryCommand(pass);
            if (command && !command->parameterBlock.empty())
                updates.push_back({command->parameterBlock, 0, command->pushConstants});
        }
        UpdateParameterBlocks(updates);
    }

    m_pythonCallbacks.clear();
    m_pythonMaterialPasses.clear();
    m_hasShadowCasterPass = false;

    InxVkCoreModular *vkCore = m_vkCore;

    for (const auto &passDesc : normalizedDesc.passes) {
        const GraphCommandDesc *command = PrimaryCommand(passDesc);
        if (!command) {
            m_pythonCallbacks[passDesc.name] = [](vk::RenderContext &, uint32_t, uint32_t) {};
            continue;
        }
        if (passDesc.type != GraphPassType::Raster) {
            m_pythonCallbacks[passDesc.name] = [](vk::RenderContext &, uint32_t, uint32_t) {};
            continue;
        }
        const auto commandType = command->type;
        if (commandType == GraphCommandType::DrawShadowCasters) {
            m_hasShadowCasterPass = true;
        }
        const int queueMin = command->queueMin;
        const int queueMax = command->queueMax;
        const int screenUIListIndex = command->screenUIList;
        const int lightIndex = command->lightIndex;
        const std::string sortMode = command->sortMode;
        const std::string overrideMaterial = command->overrideMaterial;
        const std::string passTag = command->passTag;
        MaterialPassPipelineDescriptor materialPass =
            vkCore->GetMaterialPipelineManager().GetDefaultPassPipelineDescriptor(command->shaderTarget);
        if (commandType == GraphCommandType::DrawRenderers) {
            materialPass.colorFormats.clear();
            bool writesBackbuffer = passDesc.writeColors.empty() &&
                                    command->shaderTarget != ShaderCompileTarget::Depth &&
                                    command->shaderTarget != ShaderCompileTarget::Shadow;
            bool writesSingleSampleTarget = false;
            auto colorOutputs = passDesc.writeColors;
            std::sort(colorOutputs.begin(), colorOutputs.end(),
                      [](const auto &lhs, const auto &rhs) { return lhs.first < rhs.first; });
            for (const auto &[slot, textureName] : colorOutputs) {
                (void)slot;
                const auto texture = std::find_if(
                    normalizedDesc.textures.begin(), normalizedDesc.textures.end(),
                    [&textureName](const GraphTextureDesc &textureDesc) { return textureDesc.name == textureName; });
                if (texture != normalizedDesc.textures.end()) {
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
                const auto depth = std::find_if(
                    normalizedDesc.textures.begin(), normalizedDesc.textures.end(),
                    [&passDesc](const GraphTextureDesc &desc) { return desc.name == passDesc.writeDepth; });
                materialPass.depthFormat =
                    depth != normalizedDesc.textures.end() ? depth->format : rhi::PixelFormat::Undefined;
                if (depth != normalizedDesc.textures.end()) {
                    const bool singleSampleDepth = (depth->width > 0 && depth->height > 0) || depth->sizeDivisor > 1;
                    writesSingleSampleTarget |= singleSampleDepth;
                    writesBackbuffer |= !singleSampleDepth;
                }
            } else {
                materialPass.depthFormat = rhi::PixelFormat::Undefined;
                for (const std::string &textureName : passDesc.readTextures) {
                    const bool sampledInput =
                        std::any_of(command->inputBindings.begin(), command->inputBindings.end(),
                                    [&textureName](const auto &binding) { return binding.second == textureName; });
                    if (sampledInput)
                        continue;
                    const auto depth = std::find_if(normalizedDesc.textures.begin(), normalizedDesc.textures.end(),
                                                    [&textureName](const GraphTextureDesc &desc) {
                                                        return desc.name == textureName && desc.isDepth;
                                                    });
                    if (depth != normalizedDesc.textures.end()) {
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
            m_pythonMaterialPasses[passDesc.name] = materialPass;
        }

        m_pythonCallbacks[passDesc.name] = [this, vkCore, commandType, queueMin, queueMax, screenUIListIndex,
                                            lightIndex, sortMode, overrideMaterial, passTag,
                                            materialPass](vk::RenderContext &ctx, uint32_t w, uint32_t h) {
            switch (commandType) {
            case GraphCommandType::DrawRenderers:
                vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, queueMin, queueMax, sortMode, overrideMaterial,
                                          passTag, &materialPass);
                break;
            case GraphCommandType::DrawSkybox: {
                const int32_t skyboxQueue = EngineConfig::Get().skyboxQueue;
                vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, skyboxQueue, skyboxQueue);
                break;
            }
            case GraphCommandType::DrawShadowCasters:
                // Shadow caster pass: draw filtered objects using shadow pipeline
                // with lightVP from SceneLightCollector. The shadow pipeline is
                // lazily created inside DrawShadowCasters().
                vkCore->DrawShadowCasters(ctx.GetCommandBuffer(), w, h, queueMin, queueMax, lightIndex);
                break;
            case GraphCommandType::DrawScreenUI:
                if (m_screenUIRenderer) {
                    auto list = (screenUIListIndex == 0) ? ScreenUIList::Camera : ScreenUIList::Overlay;
                    m_screenUIRenderer->Render(ctx.GetCommandBuffer(), list, w, h);
                }
                break;
            case GraphCommandType::FullscreenQuad:
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
        m_pythonGraphDesc = std::move(normalizedDesc);
    }
    m_hasPythonGraph = true;
    m_pythonGraphSourceRevision = desc.sourceRevision;
    m_pythonCallbackSamples = callbackSamples;
    if (topologyChanged || callbackContractChanged) {
        m_needsRebuild = true;
    }
}

void SceneRenderGraph::UpdateParameterBlocks(const std::vector<GraphParameterBlockUpdate> &updates)
{
    for (const auto &update : updates) {
        auto blockIt = m_parameterBlocks.find(update.id);
        if (blockIt == m_parameterBlocks.end())
            continue;

        RuntimeParameterBlock &block = blockIt->second;
        if (update.revision != 0 && update.revision == block.revision)
            continue;
        if (update.values.size() != block.names.size()) {
            INXLOG_ERROR("SceneRenderGraph::UpdateParameterBlocks: block '", update.id,
                         "' value count does not match its compiled layout");
            continue;
        }

        bool valid = true;
        FullscreenPushConstants values{};
        for (size_t i = 0; i < update.values.size(); ++i) {
            if (update.values[i].first != block.names[i] || !std::isfinite(update.values[i].second)) {
                valid = false;
                break;
            }
            values.values[i] = update.values[i].second;
        }
        if (!valid) {
            INXLOG_ERROR("SceneRenderGraph::UpdateParameterBlocks: block '", update.id,
                         "' does not match its compiled parameter names");
            continue;
        }

        block.values = values;
        block.byteSize = static_cast<uint32_t>(update.values.size() * sizeof(float));
        block.revision = update.revision;
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

    if (m_particleDrawRegistry) {
        const uint64_t revision = m_particleDrawRegistry->Revision();
        if (revision != m_particleDrawRegistryRevision)
            m_needsRebuild = true;
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
            m_renderGraph->SetResourceInitialState(m_importedColorTarget, rhi::TextureLayout::ColorAttachment,
                                                   rhi::Access::ColorWrite, rhi::PipelineStage::ColorOutput);
        } else {
            m_renderGraph->SetResourceInitialState(m_importedColorTarget, rhi::TextureLayout::ShaderReadOnly,
                                                   rhi::Access::ShaderRead, rhi::PipelineStage::FragmentShader);
        }
    }

    if (m_importedResolveTarget.IsValid()) {
        m_renderGraph->SetResourceInitialState(m_importedResolveTarget, rhi::TextureLayout::ShaderReadOnly,
                                               rhi::Access::ShaderRead, rhi::PipelineStage::FragmentShader);
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
        m_width, m_height, m_sceneTarget->GetMsaaSampleCount(), rhi::TextureLayout::Undefined);

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
    const vk::ResourceHandle rendererListHandle = m_visibleRendererList;
    InxVkCoreModular *vkCore = m_vkCore;
    vk::ResourceHandle writtenColor;
    m_renderGraph->AddPass(name, [=, &writtenColor](vk::PassBuilder &builder) {
        writtenColor = builder.WriteColor(colorTarget, 0);
        if (depthTarget.IsValid()) {
            builder.ReadDepth(depthTarget);
        }
        if (rendererListHandle.IsValid()) {
            builder.ReadRendererList(rendererListHandle);
            builder.SkipCallbackWhenRendererListsEmpty();
        }
        builder.SetRenderArea(width, height);

        return [callback, width, height, rendererListHandle, vkCore](vk::RenderContext &ctx) {
            if (rendererListHandle.IsValid()) {
                const RendererList *rendererList = ctx.GetRendererList(rendererListHandle);
                const auto *drawCalls = rendererList ? &rendererList->DrawCalls() : nullptr;
                if (!vkCore->UsesDrawCalls(drawCalls))
                    vkCore->SetDrawCalls(drawCalls);
            }
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

    m_renderGraph->Reset();
    // The renderer has waited only for the current frame slot. Leave the
    // other descriptor set untouched until its own fence is observed; the
    // old graph resources remain alive in the deferred deletion queue.
    if (VkDescriptorSet currentPerViewSet = GetPerViewDescriptorSet(); currentPerViewSet != VK_NULL_HANDLE)
        m_vkCore->ClearPerViewShadowMap(currentPerViewSet);

    m_vkCore->GetMaterialPipelineManager().InvalidateAllMaterialPipelines();

    m_graphBuilt = false;
    m_shadowMapInputHandle = {};
    m_shadowMapInputIsDepth = false;
    m_visibleRendererList = {};
    m_shadowRendererList = {};

    if (!m_hasPythonGraph) {
        INXLOG_DEBUG("SceneRenderGraph::BuildRenderGraph - No Python graph configured");
        return;
    }

    ImportSceneTargetResources();
    m_visibleRendererList = m_renderGraph->ImportRendererList("VisibleRenderers", &m_cachedRenderers);
    m_shadowRendererList = m_renderGraph->ImportRendererList("ShadowRenderers", &m_cachedShadowRenderers);

    std::unordered_map<std::string, vk::ResourceHandle> customRTHandles;
    std::unordered_map<std::string, vk::ResourceHandle> bufferHandles;
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
        for (const auto &buffer : m_pythonGraphDesc.buffers) {
            bufferHandles[buffer.name] =
                m_renderGraph->RegisterTransientBuffer(buffer.name, buffer.byteSize, ToVkBufferUsage(buffer.usage));
        }

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
            for (auto &[name, current] : bufferHandles) {
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
            const GraphCommandDesc *command = PrimaryCommand(passDesc);
            static const std::vector<std::pair<std::string, std::string>> kNoInputBindings;
            const auto &commandInputBindings = command ? command->inputBindings : kNoInputBindings;
            // Look up render callback from the Python callbacks map
            auto callbackIt = m_pythonCallbacks.find(passDesc.name);
            if (callbackIt == m_pythonCallbacks.end()) {
                INXLOG_WARN("SceneRenderGraph: Pass '", passDesc.name,
                            "' has no render callback — skipping. "
                            "This usually means ApplyPythonGraph() was not called or validation failed.");
                continue;
            }
            auto callback = callbackIt->second;
            std::vector<particle::GpuParticleDrawEntry> particleEntries;
            MaterialPassPipelineDescriptor particlePass;
            if (m_particleDrawRegistry && command && command->type == GraphCommandType::DrawRenderers &&
                command->shaderTarget == ShaderCompileTarget::Forward) {
                particleEntries = m_particleDrawRegistry->Snapshot(command->queueMin, command->queueMax);
                const auto particlePassIt = m_pythonMaterialPasses.find(passDesc.name);
                if (particlePassIt != m_pythonMaterialPasses.end())
                    particlePass = particlePassIt->second;
                else
                    particleEntries.clear();
            }

            auto resolveTextureHandle = [&](const std::string &name) -> vk::ResourceHandle {
                const auto texture = texDescMap.find(name);
                if (texture == texDescMap.end())
                    return {};
                if (texture->second->isBackbuffer)
                    return m_importedColorTarget;
                const auto custom = customRTHandles.find(name);
                if (custom != customRTHandles.end())
                    return custom->second;
                return texture->second->isDepth ? sharedDepth : vk::ResourceHandle{};
            };
            auto textureExtent = [&](const std::string &name) -> VkExtent3D {
                const auto texture = texDescMap.find(name);
                if (texture == texDescMap.end())
                    return {0, 0, 1};
                uint32_t resourceWidth = texture->second->width > 0 ? texture->second->width : width;
                uint32_t resourceHeight = texture->second->height > 0 ? texture->second->height : height;
                if (texture->second->sizeDivisor > 1) {
                    resourceWidth = std::max(1u, width / texture->second->sizeDivisor);
                    resourceHeight = std::max(1u, height / texture->second->sizeDivisor);
                }
                return {resourceWidth, resourceHeight, 1};
            };

            if (passDesc.type == GraphPassType::Compute) {
                m_renderGraph->AddComputePass(passDesc.name, [&](vk::PassBuilder &builder) {
                    for (const auto &textureName : passDesc.readTextures) {
                        const auto handle = resolveTextureHandle(textureName);
                        if (!handle.IsValid())
                            continue;
                        const auto texture = texDescMap.find(textureName);
                        if (texture != texDescMap.end() && texture->second->isDepth)
                            builder.ReadSampledDepth(handle, rhi::PipelineStage::ComputeShader);
                        else
                            builder.Read(handle, rhi::PipelineStage::ComputeShader);
                    }
                    for (const auto &access : passDesc.bufferAccesses) {
                        auto buffer = bufferHandles.find(access.resource);
                        if (buffer == bufferHandles.end())
                            continue;
                        switch (access.type) {
                        case GraphBufferAccessType::StorageRead:
                            builder.ReadStorageBuffer(buffer->second);
                            break;
                        case GraphBufferAccessType::StorageWrite:
                            buffer->second = builder.WriteStorageBuffer(buffer->second);
                            break;
                        case GraphBufferAccessType::IndirectRead:
                            builder.ReadIndirectBuffer(buffer->second);
                            break;
                        case GraphBufferAccessType::TransferRead:
                            builder.TransferRead(buffer->second);
                            break;
                        case GraphBufferAccessType::TransferWrite:
                            buffer->second = builder.TransferWrite(buffer->second);
                            break;
                        }
                    }
                    builder.SetSideEffect(passDesc.sideEffect);
                    return [](vk::RenderContext &) {};
                });
                continue;
            }

            if (passDesc.type == GraphPassType::Copy && command) {
                if (command->type == GraphCommandType::CopyBuffer) {
                    auto source = bufferHandles.find(command->sourceResource);
                    auto destination = bufferHandles.find(command->destinationResource);
                    if (source == bufferHandles.end() || destination == bufferHandles.end())
                        continue;
                    const auto sourceDesc = std::find_if(
                        m_pythonGraphDesc.buffers.begin(), m_pythonGraphDesc.buffers.end(),
                        [&](const GraphBufferDesc &buffer) { return buffer.name == command->sourceResource; });
                    const auto destinationDesc = std::find_if(
                        m_pythonGraphDesc.buffers.begin(), m_pythonGraphDesc.buffers.end(),
                        [&](const GraphBufferDesc &buffer) { return buffer.name == command->destinationResource; });
                    const VkDeviceSize copyBytes = command->copyBytes > 0
                                                       ? command->copyBytes
                                                       : std::min(sourceDesc->byteSize, destinationDesc->byteSize);
                    vk::ResourceHandle written;
                    const auto sourceHandle = source->second;
                    const auto destinationHandle = destination->second;
                    m_renderGraph->AddTransferPass(passDesc.name, [&](vk::PassBuilder &builder) {
                        builder.TransferRead(sourceHandle);
                        written = builder.TransferWrite(destinationHandle);
                        builder.SetSideEffect(passDesc.sideEffect);
                        return [sourceHandle, written, copyBytes](vk::RenderContext &ctx) {
                            ctx.GetTransferCommandEncoder().CopyBuffer(ctx.GetBufferHandle(sourceHandle),
                                                                       ctx.GetBufferHandle(written), {0, 0, copyBytes});
                        };
                    });
                    publishResourceVersion(written);
                    continue;
                }

                if (command->type == GraphCommandType::CopyTexture) {
                    const auto sourceHandle = resolveTextureHandle(command->sourceResource);
                    const auto destinationHandle = resolveTextureHandle(command->destinationResource);
                    if (!sourceHandle.IsValid() || !destinationHandle.IsValid())
                        continue;
                    const auto sourceDesc = texDescMap.at(command->sourceResource);
                    const VkExtent3D sourceExtent = textureExtent(command->sourceResource);
                    const VkExtent3D destinationExtent = textureExtent(command->destinationResource);
                    const VkExtent3D copyExtent{std::min(sourceExtent.width, destinationExtent.width),
                                                std::min(sourceExtent.height, destinationExtent.height), 1};
                    const rhi::TextureAspect aspect =
                        sourceDesc->isDepth ? rhi::TextureAspect::Depth : rhi::TextureAspect::Color;
                    vk::ResourceHandle written;
                    m_renderGraph->AddTransferPass(passDesc.name, [&](vk::PassBuilder &builder) {
                        builder.TransferRead(sourceHandle);
                        written = builder.TransferWrite(destinationHandle);
                        builder.SetSideEffect(passDesc.sideEffect);
                        return [sourceHandle, written, copyExtent, aspect](vk::RenderContext &ctx) {
                            ctx.GetTransferCommandEncoder().CopyTexture(
                                ctx.GetTextureHandle(sourceHandle), ctx.GetTextureHandle(written),
                                {aspect, 0, 0, 0, 0, copyExtent.width, copyExtent.height, copyExtent.depth});
                        };
                    });
                    publishResourceVersion(written);
                    continue;
                }
            }

            if (passDesc.type == GraphPassType::Present && command) {
                const auto source = resolveTextureHandle(command->sourceResource);
                if (!source.IsValid())
                    continue;
                m_renderGraph->AddPresentPass(passDesc.name, [&](vk::PassBuilder &builder) {
                    builder.Read(source, rhi::PipelineStage::FragmentShader);
                    builder.SetSideEffect();
                    return [](vk::RenderContext &) {};
                });
                continue;
            }

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
            bool isShadowPassAction = command && command->type == GraphCommandType::DrawShadowCasters;
            const bool isDepthOnlyMaterialPass = command && command->type == GraphCommandType::DrawRenderers &&
                                                 (command->shaderTarget == ShaderCompileTarget::Depth ||
                                                  command->shaderTarget == ShaderCompileTarget::Shadow);
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
                            std::any_of(commandInputBindings.begin(), commandInputBindings.end(),
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
            for (const auto &[samplerName, textureName] : commandInputBindings) {
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
            if (command && command->type == GraphCommandType::FullscreenQuad) {

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
                std::string shaderName = command->shaderName;
                std::string parameterBlock = command->parameterBlock;
                FullscreenPushConstants packedPushConstants{};
                uint32_t packedPushConstantSize = 0;
                for (const auto &[name, value] : command->pushConstants) {
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
                if (!commandInputBindings.empty()) {
                    // Use inputBindings order for deterministic sampler→binding mapping
                    for (const auto &[samplerName, textureName] : commandInputBindings) {
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

                        // Dynamic blocks replace only values. Shader, parameter
                        // order, and byte size remain part of compiled topology.
                        FullscreenPushConstants drawPushConstants = packedPushConstants;
                        uint32_t drawPushConstantSize = packedPushConstantSize;
                        if (!parameterBlock.empty()) {
                            const auto blockIt = m_parameterBlocks.find(parameterBlock);
                            if (blockIt != m_parameterBlocks.end()) {
                                drawPushConstants = blockIt->second.values;
                                drawPushConstantSize = blockIt->second.byteSize;
                            }
                        }

                        fsRenderer->Draw(ctx.GetGraphicsCommandEncoder(), entry, bindGroup, drawPushConstants,
                                         drawPushConstantSize);
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
            const bool usesShadowRendererList = command && command->type == GraphCommandType::DrawShadowCasters;
            const bool usesVisibleRendererList = command && (command->type == GraphCommandType::DrawRenderers ||
                                                             command->type == GraphCommandType::DrawSkybox);
            const vk::ResourceHandle rendererListHandle =
                usesShadowRendererList ? m_shadowRendererList
                                       : (usesVisibleRendererList ? m_visibleRendererList : vk::ResourceHandle{});
            m_renderGraph->AddPass(passDesc.name, [=, &sharedDepth, &writtenColorVersions, &writtenDepthVersion,
                                                   &writtenResolveVersion](vk::PassBuilder &builder) {
                // Local alias to make vkCore capturable by nested lambdas (MSVC C3481)
                InxVkCoreModular *localVkCore = vkCore;

                struct ParticlePacket
                {
                    std::shared_ptr<particle::ParticleGpuBillboardRenderer> renderer;
                    vk::ResourceHandle instances;
                    vk::ResourceHandle indirectArguments;
                };
                std::vector<ParticlePacket> particlePackets;
                particlePackets.reserve(particleEntries.size());
                for (const auto &entry : particleEntries) {
                    const std::string prefix = "GpuParticle/" + std::to_string(entry.id);
                    const auto instances = builder.ImportBuffer(prefix + "/Instances", entry.instances,
                                                                static_cast<uint64_t>(entry.capacity) *
                                                                    particle::ParticleGpuRuntime::RenderInstanceStride);
                    const auto indirectArguments =
                        builder.ImportBuffer(prefix + "/Indirect", entry.indirectArguments, 16);
                    if (!instances.IsValid() || !indirectArguments.IsValid())
                        continue;
                    m_renderGraph->SetResourceInitialState(instances, rhi::TextureLayout::Undefined,
                                                           rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                    m_renderGraph->SetResourceInitialState(indirectArguments, rhi::TextureLayout::Undefined,
                                                           rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                    builder.ReadStorageBuffer(instances, rhi::PipelineStage::VertexShader);
                    builder.ReadIndirectBuffer(indirectArguments);
                    particlePackets.push_back({entry.renderer, instances, indirectArguments});
                }

                if (rendererListHandle.IsValid()) {
                    builder.ReadRendererList(rendererListHandle);
                    builder.SkipCallbackWhenRendererListsEmpty(particlePackets.empty());
                }

                // ----- Determine pass dimensions -----
                // Shadow caster passes may use custom-sized depth textures.
                // Determine the actual pass dimensions from the depth target.
                uint32_t passWidth = width;
                uint32_t passHeight = height;
                bool isShadowPass = command && command->type == GraphCommandType::DrawShadowCasters;

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

                return [this, callback, passWidth, passHeight, inputBindingHandles, isShadowPass, rendererListHandle,
                        usesShadowRendererList, localVkCore, particlePackets, particlePass,
                        passName = passDesc.name](vk::RenderContext &ctx) {
                    if (rendererListHandle.IsValid()) {
                        const RendererList *rendererList = ctx.GetRendererList(rendererListHandle);
                        if (usesShadowRendererList) {
                            localVkCore->SetShadowDrawCalls(rendererList ? &rendererList->DrawCalls() : nullptr);
                        } else {
                            const auto *drawCalls = rendererList ? &rendererList->DrawCalls() : nullptr;
                            if (!localVkCore->UsesDrawCalls(drawCalls))
                                localVkCore->SetDrawCalls(drawCalls);
                        }
                    }
                    if (callback)
                        callback(ctx, passWidth, passHeight);
                    if (!particlePackets.empty()) {
                        particle::GpuBillboardViewConstants view;
                        const glm::mat4 viewProjection = m_cachedProj * m_cachedView;
                        const glm::mat4 inverseView = glm::inverse(m_cachedView);
                        std::memcpy(view.viewProjection.data(), &viewProjection[0][0], sizeof(viewProjection));
                        std::memcpy(view.cameraRight.data(), &inverseView[0][0], sizeof(glm::vec4));
                        std::memcpy(view.cameraUp.data(), &inverseView[1][0], sizeof(glm::vec4));
                        const auto renderTargetLayout = m_renderGraph->GetPassRenderTargetLayout(passName);
                        auto &encoder = ctx.GetGraphicsCommandEncoder();
                        for (const auto &packet : particlePackets) {
                            [[maybe_unused]] const bool recorded =
                                packet.renderer->RecordDraw(encoder, renderTargetLayout, particlePass,
                                                            ctx.GetBufferHandle(packet.indirectArguments), view);
                        }
                    }
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
    m_particleDrawRegistryRevision = m_particleDrawRegistry ? m_particleDrawRegistry->Revision() : 0;
    m_graphBuilt = true;
}

} // namespace infernux
