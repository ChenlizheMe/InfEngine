/**
 * @file SceneRenderGraph.cpp
 * @brief Implementation of RenderGraph-based scene rendering
 *
 * This implementation fully utilizes vk::RenderGraph for all rendering.
 * No more imperative BeginRenderPass/EndRenderPass calls.
 */

#include "SceneRenderGraph.h"
#include "Frustum.h"
#include "FullscreenRenderer.h"
#include "InxVkCoreModular.h"
#include "MsaaPolicy.h"
#include "OutlineRenderer.h"
#include "SceneRenderTarget.h"
#include "gui/InxScreenUIRenderer.h"
#include "particle/ParticleGpuBounds.h"
#include "particle/ParticleGpuCuller.h"
#include "particle/ParticleGpuDrawRegistry.h"
#include "particle/ParticleGpuSorter.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkDeviceContext.h"
#include "vk/VkPipelineManager.h"
#include "vk/VkRenderUtils.h"
#include "vk/VulkanRhiDevice.h"
#include <algorithm>
#include <cmath>
#include <core/config/EngineConfig.h>
#include <core/error/InxError.h>
#include <core/types/ColorSpace.h>
#include <cstring>
#include <function/renderer/rhi/RhiBuffer.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/Camera.h>
#include <function/scene/LightingData.h>
#include <iterator>
#include <limits>
#include <memory>
#include <type_traits>
#include <unordered_set>

namespace infernux
{

namespace
{

constexpr uint64_t kShadowHashOffset = 1469598103934665603ull;
constexpr uint64_t kShadowHashPrime = 1099511628211ull;

void HashShadowBytes(uint64_t &hash, const void *data, size_t size)
{
    const auto *bytes = static_cast<const uint8_t *>(data);
    for (size_t index = 0; index < size; ++index) {
        hash ^= bytes[index];
        hash *= kShadowHashPrime;
    }
}

template <typename T> void HashShadowValue(uint64_t &hash, const T &value)
{
    static_assert(std::is_trivially_copyable_v<T>);
    HashShadowBytes(hash, &value, sizeof(T));
}

lighting::ShadowDepthRange VisibleShadowDepthRange(const Camera *camera, const std::vector<DrawCall> &drawCalls)
{
    lighting::ShadowDepthRange result{};
    if (!camera || !camera->GetTransform())
        return result;

    const glm::vec3 cameraPosition = camera->GetTransform()->GetWorldPosition();
    const glm::vec3 cameraForward = glm::normalize(camera->GetTransform()->GetWorldForward());
    float nearest = std::numeric_limits<float>::max();
    float farthest = 0.0f;
    for (const DrawCall &drawCall : drawCalls) {
        if (!drawCall.frustumVisible || !drawCall.worldBounds.IsValid())
            continue;
        const glm::vec3 center = (drawCall.worldBounds.min + drawCall.worldBounds.max) * 0.5f;
        const glm::vec3 extent = (drawCall.worldBounds.max - drawCall.worldBounds.min) * 0.5f;
        const float centerDepth = glm::dot(center - cameraPosition, cameraForward);
        const float depthRadius = glm::dot(glm::abs(cameraForward), extent);
        const float objectFar = centerDepth + depthRadius;
        if (objectFar <= 0.0f)
            continue;

        // A huge receiver crossing the camera should not collapse the entire
        // logarithmic distribution onto the near clip plane.
        const float boundedRadius = std::min(depthRadius, std::max(centerDepth * 0.5f, 0.0f));
        nearest = std::min(nearest, std::max(centerDepth - boundedRadius, camera->GetNearClip()));
        farthest = std::max(farthest, objectFar);
    }
    if (farthest <= 0.0f || nearest == std::numeric_limits<float>::max())
        return result;

    const float span = std::max(farthest - nearest, camera->GetNearClip());
    result.nearDepth = nearest;
    result.farDepth = std::min(farthest + span * 0.05f, camera->GetFarClip());
    return result;
}

bool TextureDescEquals(const GraphTextureDesc &a, const GraphTextureDesc &b)
{
    return a.name == b.name && a.format == b.format && a.isBackbuffer == b.isBackbuffer && a.isDepth == b.isDepth &&
           a.width == b.width && a.height == b.height && a.sizeDivisor == b.sizeDivisor && a.samples == b.samples &&
           a.role == b.role && a.temporalKey == b.temporalKey;
}

uint32_t EffectiveTextureSamples(const GraphTextureDesc &texture, uint32_t frameSamples)
{
    return texture.isBackbuffer || texture.samples == 0 ? frameSamples : texture.samples;
}

bool TextureExtentsMatch(const GraphTextureDesc &a, const GraphTextureDesc &b)
{
    return a.width == b.width && a.height == b.height && a.sizeDivisor == b.sizeDivisor;
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
    return a.type == b.type && a.shaderTarget == b.shaderTarget && a.materialFilter == b.materialFilter &&
           a.queueMin == b.queueMin && a.queueMax == b.queueMax && a.sortMode == b.sortMode && a.passTag == b.passTag &&
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
           a.writeDepth == b.writeDepth && a.resolveColor == b.resolveColor && a.clearColor == b.clearColor &&
           a.clearDepth == b.clearDepth && a.clearColorR == b.clearColorR && a.clearColorG == b.clearColorG &&
           a.clearColorB == b.clearColorB && a.clearColorA == b.clearColorA && a.clearDepthValue == b.clearDepthValue &&
           a.sideEffect == b.sideEffect && a.bufferAccesses.size() == b.bufferAccesses.size() &&
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

bool ValidatePythonGraphDescription(const RenderGraphDescription &desc, uint32_t activeFrameSamples)
{
    const uint32_t frameSamples = desc.msaaSamples > 0 ? static_cast<uint32_t>(desc.msaaSamples) : activeFrameSamples;
    std::unordered_map<std::string, const GraphTextureDesc *> textures;
    textures.reserve(desc.textures.size());
    struct TemporalPair
    {
        const GraphTextureDesc *read = nullptr;
        const GraphTextureDesc *write = nullptr;
    };
    std::unordered_map<std::string, TemporalPair> temporalPairs;

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
        if (tex.samples != 0 && !IsValidMsaaSampleCount(static_cast<int>(tex.samples))) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: texture '", tex.name, "' has invalid sample count ",
                         tex.samples);
            return false;
        }
        if (tex.isBackbuffer && tex.samples != 0) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: backbuffer texture '", tex.name,
                         "' must inherit the frame sample count");
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
        if (tex.role == GraphTextureRole::Transient) {
            if (!tex.temporalKey.empty()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: transient texture '", tex.name,
                             "' cannot declare a temporal key");
                return false;
            }
            continue;
        }
        if (tex.temporalKey.empty() || tex.isBackbuffer || tex.isDepth || tex.width != 0 || tex.height != 0 ||
            tex.sizeDivisor != 0 || tex.samples != 1) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: temporal texture '", tex.name,
                         "' must be a scene-sized, single-sample color texture with a temporal key");
            return false;
        }
        auto &pair = temporalPairs[tex.temporalKey];
        const GraphTextureDesc **slot = tex.role == GraphTextureRole::TemporalRead ? &pair.read : &pair.write;
        if (*slot != nullptr) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: duplicate temporal role for '", tex.temporalKey, "'");
            return false;
        }
        *slot = &tex;
    }
    for (const auto &[key, pair] : temporalPairs) {
        if (!pair.read || !pair.write || pair.read->format != pair.write->format) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: temporal history '", key,
                         "' requires one matching read/write pair");
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
            (pass.type == GraphPassType::Copy && !copyCommand) ||
            (pass.type == GraphPassType::Present && (!command || command->type != GraphCommandType::Present))) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                         "' command does not match its execution domain");
            return false;
        }
        if (pass.type != GraphPassType::Raster && (!pass.writeColors.empty() || !pass.writeDepth.empty() ||
                                                   !pass.resolveColor.empty() || pass.clearColor || pass.clearDepth)) {
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
            if (command->shaderTarget != ShaderCompileTarget::Shadow) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: shadow pass '", pass.name,
                             "' requires the Shadow shader target");
                return false;
            }
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
        const bool motionMaterialPass = command && command->type == GraphCommandType::DrawRenderers &&
                                        command->shaderTarget == ShaderCompileTarget::Motion;
        if (motionMaterialPass) {
            const auto color =
                pass.writeColors.size() == 1 ? textures.find(pass.writeColors.front().second) : textures.end();
            const bool hasReadableDepth =
                std::any_of(pass.readTextures.begin(), pass.readTextures.end(), [&](const std::string &textureName) {
                    const auto texture = textures.find(textureName);
                    const bool sampled =
                        std::any_of(command->inputBindings.begin(), command->inputBindings.end(),
                                    [&](const auto &binding) { return binding.second == textureName; });
                    return texture != textures.end() && texture->second->isDepth && !sampled;
                });
            if (color == textures.end() || pass.writeColors.front().first != 0 ||
                color->second->format != rhi::PixelFormat::RG16SFloat || !hasReadableDepth ||
                !pass.writeDepth.empty()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: motion pass '", pass.name,
                             "' requires one RG16SFloat color[0], one readable depth attachment, and no depth write");
                return false;
            }
        }
        const bool rgbaGeometryMaterialPass = command && command->type == GraphCommandType::DrawRenderers &&
                                              (command->shaderTarget == ShaderCompileTarget::Normal ||
                                               command->shaderTarget == ShaderCompileTarget::BaseColor);
        if (rgbaGeometryMaterialPass) {
            const auto color =
                pass.writeColors.size() == 1 ? textures.find(pass.writeColors.front().second) : textures.end();
            const bool hasReadableDepth =
                std::any_of(pass.readTextures.begin(), pass.readTextures.end(), [&](const std::string &textureName) {
                    const auto texture = textures.find(textureName);
                    const bool sampled =
                        std::any_of(command->inputBindings.begin(), command->inputBindings.end(),
                                    [&](const auto &binding) { return binding.second == textureName; });
                    return texture != textures.end() && texture->second->isDepth && !sampled;
                });
            if (color == textures.end() || pass.writeColors.front().first != 0 ||
                color->second->format != rhi::PixelFormat::RGBA16SFloat || !hasReadableDepth ||
                !pass.writeDepth.empty()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: ", ShaderCompileTargetName(command->shaderTarget),
                             " pass '", pass.name,
                             "' requires one RGBA16SFloat color[0], one readable depth attachment, and no depth write");
                return false;
            }
        }

        std::unordered_set<int> colorSlots;
        uint32_t attachmentSamples = 0;
        const auto acceptAttachmentSamples = [&](const GraphTextureDesc &texture) {
            const uint32_t samples = EffectiveTextureSamples(texture, frameSamples);
            if (attachmentSamples == 0) {
                attachmentSamples = samples;
                return true;
            }
            return attachmentSamples == samples;
        };

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
            if (!acceptAttachmentSamples(*texIt->second)) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                             "' uses color and depth attachments with different sample counts");
                return false;
            }
        }
        for (int slot = 0; slot < static_cast<int>(colorSlots.size()); ++slot) {
            if (colorSlots.find(slot) == colorSlots.end()) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                             "' must use contiguous color slots starting at zero");
                return false;
            }
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
            if (!acceptAttachmentSamples(*texIt->second)) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                             "' uses color and depth attachments with different sample counts");
                return false;
            }
        }

        uint32_t readOnlyDepthAttachmentCount = 0;
        if (command && command->type != GraphCommandType::FullscreenQuad) {
            for (const auto &textureName : pass.readTextures) {
                const bool sampledInput =
                    std::any_of(command->inputBindings.begin(), command->inputBindings.end(),
                                [&textureName](const auto &binding) { return binding.second == textureName; });
                if (sampledInput)
                    continue;
                auto texIt = textures.find(textureName);
                if (texIt == textures.end() || !texIt->second->isDepth)
                    continue;
                ++readOnlyDepthAttachmentCount;
                if (!acceptAttachmentSamples(*texIt->second)) {
                    INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                                 "' uses color and depth attachments with different sample counts");
                    return false;
                }
            }
        }
        if (readOnlyDepthAttachmentCount > 1 || (readOnlyDepthAttachmentCount != 0 && !pass.writeDepth.empty())) {
            INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                         "' must declare exactly one depth attachment as either read-only or writable");
            return false;
        }

        if (!pass.resolveColor.empty()) {
            const auto resolve = textures.find(pass.resolveColor);
            const auto source = pass.writeColors.size() == 1 && pass.writeColors.front().first == 0
                                    ? textures.find(pass.writeColors.front().second)
                                    : textures.end();
            if (resolve == textures.end() || source == textures.end() || resolve->second->isBackbuffer ||
                resolve->second->isDepth || pass.resolveColor == pass.writeColors.front().second ||
                EffectiveTextureSamples(*source->second, frameSamples) <= 1 ||
                EffectiveTextureSamples(*resolve->second, frameSamples) != 1 ||
                source->second->format != resolve->second->format ||
                !TextureExtentsMatch(*source->second, *resolve->second)) {
                INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: pass '", pass.name,
                             "' has an invalid MSAA color resolve contract");
                return false;
            }
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

bool SceneRenderGraph::ValidateGraphDescription(const RenderGraphDescription &desc, uint32_t activeFrameSamples)
{
    return ValidatePythonGraphDescription(desc, activeFrameSamples);
}

// ============================================================================
// Constructor / Destructor
// ============================================================================

SceneRenderGraph::SceneRenderGraph()
    : m_particleViewDiagnosticState(std::make_shared<ParticleViewDiagnosticState>()),
      m_renderGraph(std::make_unique<vk::RenderGraph>())
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

bool SceneRenderGraph::Initialize(InxVkCoreModular *vkCore, SceneRenderTarget *sceneTarget,
                                  rhi::RenderViewKind viewKind)
{
    if (!vkCore || !sceneTarget) {
        INXLOG_ERROR("SceneRenderGraph::Initialize: Invalid parameters");
        return false;
    }

    m_vkCore = vkCore;
    m_sceneTarget = sceneTarget;
    m_width = sceneTarget->GetWidth();
    m_height = sceneTarget->GetHeight();

    m_renderView.id = rhi::AllocateRenderViewId();
    m_renderView.device = vkCore->GetDeviceContext().GetDeviceId();
    m_renderView.kind = viewKind;
    m_renderView.output = rhi::RenderOutputKind::OffscreenTexture;
    m_renderView.width = m_width;
    m_renderView.height = m_height;
    m_renderView.colorFormat = rhi::FromVkFormat(sceneTarget->GetColorFormat());
    m_renderView.depthFormat = rhi::FromVkFormat(sceneTarget->GetDepthFormat());
    m_renderView.samples = rhi::FromVkSampleCount(sceneTarget->GetMsaaSampleCount());
    m_renderView.revision = 1;

    // Initialize the underlying RenderGraph with device context and pipeline manager
    m_renderGraph->Initialize(&vkCore->GetDeviceContext(), &vkCore->GetPipelineManager(), &vkCore->GetRetirementQueue(),
                              &vkCore->GetBackendContext().Queues());
    m_renderGraph->SetRenderView(m_renderView);

    // Allocate a complete camera-local lighting domain. Geometry and particles
    // share this camera's atlas and LightingUBO, but Scene, Game and future
    // camera graphs never overwrite one another's shadow state.
    auto &rhiDevice = vkCore->GetDeviceContext().GetRhiDevice();
    rhi::BufferDesc lightingBufferDesc{};
    lightingBufferDesc.byteSize = sizeof(ShaderLightingUBO);
    lightingBufferDesc.usage = rhi::BufferUsageFlags::Uniform;
    lightingBufferDesc.memory = rhi::BufferMemory::Upload;
    rhi::BufferDesc cameraBufferDesc{};
    cameraBufferDesc.byteSize = sizeof(UniformBufferObject);
    cameraBufferDesc.usage = rhi::BufferUsageFlags::Uniform;
    cameraBufferDesc.memory = rhi::BufferMemory::Upload;
    for (auto &frame : m_perViewFrames) {
        frame.cameraMatrix = rhiDevice.CreateBuffer(cameraBufferDesc);
        if (!frame.cameraMatrix.IsValid()) {
            INXLOG_ERROR("SceneRenderGraph: failed to allocate camera-local matrix UBO");
            return false;
        }
    }
    for (auto &frame : m_perViewFrames) {
        frame.lighting = rhiDevice.CreateBuffer(lightingBufferDesc);
        if (!frame.lighting.IsValid()) {
            INXLOG_ERROR("SceneRenderGraph: failed to allocate camera-local LightingUBO");
            return false;
        }
    }
    if (!m_cameraCanonicalLights.Initialize(rhiDevice, kMaxFramesInFlight)) {
        INXLOG_ERROR("SceneRenderGraph: failed to allocate camera-local canonical light buffers");
        return false;
    }
    m_shadowCameraResourceId = vkCore->CreateShadowCameraResources();
    if (m_shadowCameraResourceId == 0) {
        INXLOG_ERROR("SceneRenderGraph: failed to allocate camera-local shadow resource identity");
        return false;
    }
    m_perViewLayout = rhiDevice.RegisterBindingLayout(vkCore->GetPerViewDescSetLayout());
    if (!m_perViewLayout.IsValid()) {
        INXLOG_ERROR("SceneRenderGraph: failed to register the canonical per-view descriptor layout");
        return false;
    }
    for (uint32_t i = 0; i < kMaxFramesInFlight; ++i) {
        auto &frame = m_perViewFrames[i];
        frame.geometryDescriptor = vkCore->AllocatePerViewDescriptorLease();
        frame.particleDescriptor = vkCore->AllocatePerViewDescriptorLease();
        frame.geometryGroup = rhiDevice.RegisterBindGroup(frame.GeometrySet());
        frame.particleGroup = rhiDevice.RegisterBindGroup(frame.ParticleSet());
        if (!frame.geometryDescriptor.IsValid() || !frame.particleDescriptor.IsValid() ||
            !frame.geometryGroup.IsValid() || !frame.particleGroup.IsValid()) {
            INXLOG_ERROR("SceneRenderGraph: failed to allocate the canonical per-view descriptor resources [", i, "]");
            return false;
        }
        const VkBuffer lightingBuffer = rhiDevice.Resolve(frame.lighting);
        const VkBuffer cameraBuffer = rhiDevice.Resolve(frame.cameraMatrix);
        vkCore->UpdatePerViewLightingBuffer(frame.GeometrySet(), lightingBuffer, sizeof(ShaderLightingUBO));
        vkCore->UpdatePerViewLightingBuffer(frame.ParticleSet(), lightingBuffer, sizeof(ShaderLightingUBO));
        vkCore->UpdatePerViewCameraBuffer(frame.GeometrySet(), cameraBuffer, sizeof(UniformBufferObject));
        vkCore->UpdatePerViewCameraBuffer(frame.ParticleSet(), cameraBuffer, sizeof(UniformBufferObject));
    }

    // Initialize fullscreen effect renderer for FullscreenQuad passes
    m_fullscreenRenderer.Initialize(vkCore);

    const auto depthResolveSupport = vkCore->GetDeviceContext().GetCapabilities().CheckFormat(
        rhi::PixelFormat::R32SFloat, rhi::FormatFeature::Sampled | rhi::FormatFeature::Storage);
    if (depthResolveSupport.IsSupported()) {
        InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
        const auto bytes = compiler.CompileComputeGlsl(std::string(SceneDepthResolver::ShaderSource()),
                                                       "Infernux/SceneDepthResolve.comp");
        if (bytes.size() >= 5 * sizeof(uint32_t) && bytes.size() % sizeof(uint32_t) == 0) {
            std::vector<uint32_t> spirv(bytes.size() / sizeof(uint32_t));
            std::memcpy(spirv.data(), bytes.data(), bytes.size());
            if (!m_sceneDepthResolver.Initialize(vkCore->GetDeviceContext().GetRhiDevice(), spirv.data(),
                                                 spirv.size())) {
                INXLOG_ERROR("SceneRenderGraph: failed to initialize the RHI scene-depth resolver");
            }
        } else {
            INXLOG_ERROR("SceneRenderGraph: failed to compile the RHI scene-depth resolve shader");
        }
    } else {
        INXLOG_WARN("SceneRenderGraph: R32SFloat sampled-storage textures are unavailable; MSAA soft particles are "
                    "disabled on this adapter");
    }

    {
        InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
        const auto bytes = compiler.CompileComputeGlsl(std::string(lighting::ForwardPlusLightGrid::ShaderSource()),
                                                       "Infernux/ForwardPlusLightGrid.comp");
        if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0) {
            INXLOG_ERROR("SceneRenderGraph: failed to compile the Forward+ tiled-light shader");
        } else {
            std::vector<uint32_t> spirv(bytes.size() / sizeof(uint32_t));
            std::memcpy(spirv.data(), bytes.data(), bytes.size());
            if (!m_forwardPlusGeometryGrid.Initialize(rhiDevice, kMaxFramesInFlight, {spirv.data(), spirv.size()})) {
                INXLOG_ERROR("SceneRenderGraph: failed to initialize the RHI Forward+ tiled-light builder");
            } else if (!m_perViewLayout.IsValid() || !m_forwardPlusParticleGrid.Initialize(
                                                         rhiDevice, kMaxFramesInFlight, {spirv.data(), spirv.size()})) {
                INXLOG_ERROR("SceneRenderGraph: failed to initialize the particle Forward+ tiled-light builder");
            } else {
                for (uint32_t frameIndex = 0; frameIndex < kMaxFramesInFlight; ++frameIndex) {
                    const auto &lights = m_cameraCanonicalLights.Frame(frameIndex);
                    if (lights.buffer.IsValid()) {
                        (void)m_forwardPlusGeometryGrid.PrepareFrame(frameIndex, m_width, m_height, lights.localCount,
                                                                     CanonicalLightAffectsGeometry, lights.buffer);
                        (void)m_forwardPlusParticleGrid.PrepareFrame(frameIndex, m_width, m_height, lights.localCount,
                                                                     CanonicalLightAffectsParticles, lights.buffer);
                    }
                }
            }
        }
    }

    return true;
}

void SceneRenderGraph::RetireFramebuffersBeforeTargetReplacement(rhi::SubmissionSerial retirementSerial)
{
    if (m_renderGraph)
        m_renderGraph->RetireFramebufferCacheAfter(retirementSerial);
    m_graphBuilt = false;
    m_needsCompile = true;
}

void SceneRenderGraph::ReplaceSceneTarget(SceneRenderTarget *sceneTarget)
{
    if (!sceneTarget)
        return;

    m_sceneTarget = sceneTarget;
    m_width = sceneTarget->GetWidth();
    m_height = sceneTarget->GetHeight();
    m_renderView.width = m_width;
    m_renderView.height = m_height;
    m_renderView.colorFormat = rhi::FromVkFormat(sceneTarget->GetColorFormat());
    m_renderView.depthFormat = rhi::FromVkFormat(sceneTarget->GetDepthFormat());
    m_renderView.samples = rhi::FromVkSampleCount(sceneTarget->GetMsaaSampleCount());
    ++m_renderView.revision;
    if (m_renderGraph)
        m_renderGraph->SetRenderView(m_renderView);
    m_graphBuilt = false;
    m_needsRebuild = true;
    m_needsCompile = true;
    m_importedColorTarget = {};
    m_importedResolveTarget = {};
    m_importedDepthTarget = {};
    m_previousViewProj = glm::mat4(1.0f);
    m_cameraHistoryValid = false;
    RetireTemporalHistoryResources();
}

void SceneRenderGraph::InvalidateTemporalHistory()
{
    m_cameraHistoryValid = false;
    m_temporalSampleIndex = 0;
    m_temporalJitterNdc = glm::vec2(0.0f);
    for (auto &[key, history] : m_temporalHistories) {
        (void)key;
        history.valid = false;
        history.readIndex = 0;
    }
    m_renderView.history = {};
}

uint64_t SceneRenderGraph::CurrentParticleDrawRegistryRevision() const noexcept
{
    return m_particleDrawRegistry ? m_particleDrawRegistry->Revision() : 0;
}

void SceneRenderGraph::InvalidateParticleViews()
{
    const auto retireCuller = [this](std::shared_ptr<particle::ParticleGpuCuller> culler) {
        if (!culler)
            return;
        if (m_vkCore) {
            m_vkCore->GetRetirementQueue().Retire([culler = std::move(culler)]() mutable { culler.reset(); });
            return;
        }
        culler.reset();
    };
    const auto retireSorter = [this](std::shared_ptr<particle::ParticleGpuSorter> sorter) {
        if (!sorter)
            return;
        if (m_vkCore) {
            m_vkCore->GetRetirementQueue().Retire([sorter = std::move(sorter)]() mutable { sorter.reset(); });
            return;
        }
        sorter.reset();
    };
    for (auto &[id, culler] : m_particleCullers) {
        (void)id;
        retireCuller(std::move(culler));
    }
    m_particleCullers.clear();
    for (auto &[id, sorter] : m_particleSorters) {
        (void)id;
        retireSorter(std::move(sorter));
    }
    m_particleSorters.clear();
    m_pendingParticleViewDiagnostics.clear();
    m_particleDrawRegistryRevision = 0;
    m_needsRebuild = true;
    m_graphBuilt = false;
}

bool SceneRenderGraph::UsesTemporalHistory() const
{
    return std::any_of(m_pythonGraphDesc.textures.begin(), m_pythonGraphDesc.textures.end(),
                       [](const GraphTextureDesc &texture) { return texture.role != GraphTextureRole::Transient; });
}

void SceneRenderGraph::RetireTemporalHistoryResources()
{
    if (m_temporalHistories.empty())
        return;
    auto retired = std::move(m_temporalHistories);
    m_temporalHistories.clear();
    m_renderView.history = {};
    if (!m_vkCore)
        return;
    rhi::Device *device = &m_vkCore->GetDeviceContext().GetRhiDevice();
    m_vkCore->GetRetirementQueue().Retire([device, retired = std::move(retired)]() mutable {
        for (auto &[key, history] : retired) {
            (void)key;
            for (const auto view : history.views)
                device->Release(view);
            for (const auto texture : history.textures)
                device->Release(texture);
        }
    });
}

void SceneRenderGraph::Destroy()
{
    if (m_particleViewDiagnosticState) {
        std::scoped_lock lock(m_particleViewDiagnosticState->mutex);
        for (const auto &request : m_pendingParticleViewDiagnostics) {
            auto &snapshot = m_particleViewDiagnosticState->snapshots[request.requestId];
            if (snapshot.status == particle::GpuParticleViewDiagnosticStatus::Pending) {
                snapshot.status = particle::GpuParticleViewDiagnosticStatus::Failed;
                snapshot.error = "Particle view render graph was destroyed before diagnostics were recorded";
            }
        }
    }
    m_pendingParticleViewDiagnostics.clear();
    m_particleCullers.clear();
    m_particleSorters.clear();
    m_fullscreenRenderer.Destroy();
    RetireTemporalHistoryResources();
    m_sceneDepthResolver.Destroy();
    m_forwardPlusGeometryGrid.Shutdown();
    m_forwardPlusParticleGrid.Shutdown();
    if (m_vkCore) {
        auto &rhiDevice = m_vkCore->GetDeviceContext().GetRhiDevice();
        auto &descriptorManager = rhiDevice.GetDescriptorManager();
        for (auto &frame : m_perViewFrames) {
            rhiDevice.Release(frame.geometryGroup);
            rhiDevice.Release(frame.particleGroup);
            descriptorManager.Retire(frame.geometryDescriptor);
            descriptorManager.Retire(frame.particleDescriptor);
            rhiDevice.Release(frame.lighting);
            rhiDevice.Release(frame.cameraMatrix);
            frame = {};
        }
        rhiDevice.Release(m_perViewLayout);
        m_cameraCanonicalLights.Shutdown();
        m_vkCore->DestroyShadowCameraResources(m_shadowCameraResourceId);
    }
    m_perViewLayout = {};
    m_shadowCameraResourceId = 0;
    m_transientResources.clear();
    m_parameterBlocks.clear();

    if (m_renderGraph) {
        m_renderGraph->Destroy();
    }
    m_importedColorTarget = {};
    m_importedDepthTarget = {};
    m_outlineRenderer = nullptr;
    m_outlinePassesEnabled = false;
    m_outlinePipelineFailureReported = false;
    m_graphBuilt = false;
    m_vkCore = nullptr;
    m_sceneTarget = nullptr;
}

uint64_t SceneRenderGraph::RequestParticleViewDiagnostics(uint64_t graphInstanceId)
{
    if (!m_particleViewDiagnosticState)
        return 0;
    uint64_t requestId = m_nextParticleViewDiagnosticRequestId++;
    if (requestId == 0)
        requestId = m_nextParticleViewDiagnosticRequestId++;

    particle::GpuParticleViewDiagnosticSnapshot snapshot;
    snapshot.requestId = requestId;
    snapshot.graphInstanceId = graphInstanceId;
    if (!m_vkCore || !m_particleDrawRegistry || graphInstanceId == 0) {
        snapshot.status = particle::GpuParticleViewDiagnosticStatus::Failed;
        snapshot.error = "Particle view diagnostics require an initialized render graph and a resident particle graph";
    } else {
        snapshot.status = particle::GpuParticleViewDiagnosticStatus::Pending;
        m_pendingParticleViewDiagnostics.push_back({requestId, graphInstanceId});
    }

    std::scoped_lock lock(m_particleViewDiagnosticState->mutex);
    if (m_particleViewDiagnosticState->snapshots.size() >= 128) {
        auto oldest = m_particleViewDiagnosticState->snapshots.end();
        for (auto it = m_particleViewDiagnosticState->snapshots.begin();
             it != m_particleViewDiagnosticState->snapshots.end(); ++it) {
            if (it->second.status == particle::GpuParticleViewDiagnosticStatus::Pending)
                continue;
            if (oldest == m_particleViewDiagnosticState->snapshots.end() || it->first < oldest->first)
                oldest = it;
        }
        if (oldest != m_particleViewDiagnosticState->snapshots.end())
            m_particleViewDiagnosticState->snapshots.erase(oldest);
    }
    m_particleViewDiagnosticState->snapshots[requestId] = std::move(snapshot);
    return requestId;
}

particle::GpuParticleViewDiagnosticSnapshot SceneRenderGraph::QueryParticleViewDiagnostics(uint64_t requestId) const
{
    if (!m_particleViewDiagnosticState)
        return {};
    std::scoped_lock lock(m_particleViewDiagnosticState->mutex);
    const auto found = m_particleViewDiagnosticState->snapshots.find(requestId);
    return found != m_particleViewDiagnosticState->snapshots.end() ? found->second
                                                                   : particle::GpuParticleViewDiagnosticSnapshot{};
}

void SceneRenderGraph::RecordParticleViewDiagnostics(VkCommandBuffer commandBuffer)
{
    if (m_pendingParticleViewDiagnostics.empty() || !m_vkCore || !m_particleDrawRegistry ||
        commandBuffer == VK_NULL_HANDLE) {
        return;
    }

    auto requests = std::move(m_pendingParticleViewDiagnostics);
    m_pendingParticleViewDiagnostics.clear();
    auto &device = m_vkCore->GetDeviceContext().GetRhiDevice();
    vk::VulkanTransferCommandContext transferContext;
    const auto transfer = device.MakeTransferCommandEncoder(transferContext, commandBuffer);
    const auto entries =
        m_particleDrawRegistry->Snapshot(std::numeric_limits<int32_t>::min(), std::numeric_limits<int32_t>::max());

    VkMemoryBarrier barrier{};
    barrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
    barrier.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
    barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1,
                         &barrier, 0, nullptr, 0, nullptr);

    for (const auto &request : requests) {
        struct OutputCapture
        {
            particle::GpuParticleViewOutputDiagnostic diagnostic;
            uint64_t dispatchOffset = 0;
            uint64_t drawOffset = 0;
            std::shared_ptr<particle::ParticleGpuCuller> culler;
        };

        std::vector<OutputCapture> captures;
        for (const auto &entry : entries) {
            if (entry.graphInstanceId != request.graphInstanceId)
                continue;
            const auto culler = m_particleCullers.find(entry.id);
            if (culler == m_particleCullers.end() || !culler->second || !culler->second->IsValid())
                continue;
            OutputCapture capture;
            capture.diagnostic.outputId = entry.id;
            capture.diagnostic.emitterId = entry.emitterId;
            capture.diagnostic.emitterIndex = entry.emitterIndex;
            capture.diagnostic.outputStableId = entry.outputStableId;
            capture.diagnostic.capacity = entry.capacity;
            capture.diagnostic.cullMode = entry.cullMode;
            capture.diagnostic.sortMode = entry.semantics.sortMode;
            const auto sorter = m_particleSorters.find(entry.id);
            capture.diagnostic.sorterAllocated =
                sorter != m_particleSorters.end() && sorter->second && sorter->second->IsValid();
            capture.culler = culler->second;
            captures.push_back(std::move(capture));
        }
        std::sort(captures.begin(), captures.end(), [](const auto &lhs, const auto &rhs) {
            if (lhs.diagnostic.emitterIndex != rhs.diagnostic.emitterIndex)
                return lhs.diagnostic.emitterIndex < rhs.diagnostic.emitterIndex;
            return lhs.diagnostic.outputStableId < rhs.diagnostic.outputStableId;
        });

        if (captures.empty()) {
            std::scoped_lock lock(m_particleViewDiagnosticState->mutex);
            auto &snapshot = m_particleViewDiagnosticState->snapshots[request.requestId];
            snapshot.status = particle::GpuParticleViewDiagnosticStatus::Failed;
            snapshot.error = "Particle view contains no resident outputs for the requested graph";
            continue;
        }

        constexpr uint64_t BytesPerOutput = sizeof(particle::GpuParticleCullDispatchState) + 16u;
        const uint64_t totalBytes = captures.size() * BytesPerOutput;
        const auto readbackHandle =
            device.CreateBuffer({totalBytes, rhi::BufferUsageFlags::TransferDestination, rhi::BufferMemory::Readback});
        if (!readbackHandle.IsValid()) {
            std::scoped_lock lock(m_particleViewDiagnosticState->mutex);
            auto &snapshot = m_particleViewDiagnosticState->snapshots[request.requestId];
            snapshot.status = particle::GpuParticleViewDiagnosticStatus::Failed;
            snapshot.error = "Failed to allocate particle view diagnostic readback buffer";
            continue;
        }
        auto readback = std::make_shared<rhi::BufferResource>(device, readbackHandle, totalBytes);
        uint64_t offset = 0;
        for (auto &capture : captures) {
            capture.dispatchOffset = offset;
            transfer.CopyBuffer(capture.culler->SortDispatchBuffer(), readbackHandle,
                                {0, offset, sizeof(particle::GpuParticleCullDispatchState)});
            offset += sizeof(particle::GpuParticleCullDispatchState);
            capture.drawOffset = offset;
            transfer.CopyBuffer(capture.culler->DrawIndirectBuffer(), readbackHandle, {0, offset, 16});
            offset += 16;
        }

        const auto state = m_particleViewDiagnosticState;
        auto *readbackDevice = static_cast<rhi::Device *>(&device);
        m_vkCore->GetRetirementQueue().Retire([state, readback, captures = std::move(captures), request,
                                               readbackDevice]() mutable {
            std::vector<uint8_t> bytes(static_cast<size_t>(readback->GetByteSize()));
            particle::GpuParticleViewDiagnosticSnapshot result;
            result.requestId = request.requestId;
            result.graphInstanceId = request.graphInstanceId;
            if (!readbackDevice->ReadBuffer(readback->GetBuffer(), 0, bytes.data(), bytes.size())) {
                result.status = particle::GpuParticleViewDiagnosticStatus::Failed;
                result.error = "Failed to read completed particle view diagnostic buffer";
            } else {
                result.status = particle::GpuParticleViewDiagnosticStatus::Completed;
                for (auto &capture : captures) {
                    particle::GpuParticleCullDispatchState dispatch{};
                    std::array<uint32_t, 4> draw{};
                    std::memcpy(&dispatch, bytes.data() + capture.dispatchOffset, sizeof(dispatch));
                    std::memcpy(draw.data(), bytes.data() + capture.drawOffset, sizeof(draw));
                    capture.diagnostic.sourceCount = dispatch.sourceCount;
                    capture.diagnostic.visibleCount = draw[1];
                    capture.diagnostic.sortGroupCountX = dispatch.groupCountX;
                    capture.diagnostic.boundsValid = (dispatch.flags & 1u) != 0u;
                    capture.diagnostic.coarseRejected = capture.diagnostic.boundsValid && (dispatch.flags & 2u) == 0u;
                    capture.diagnostic.cullMode = (dispatch.flags & 4u) != 0u
                                                      ? particle::GpuParticleCullMode::RibbonSegments
                                                      : particle::GpuParticleCullMode::Instances;
                    capture.diagnostic.drawVertexCount = draw[0];
                    capture.diagnostic.drawInstanceCount = draw[1];
                    result.outputs.push_back(std::move(capture.diagnostic));
                }
            }
            std::scoped_lock lock(state->mutex);
            const auto found = state->snapshots.find(request.requestId);
            if (found != state->snapshots.end() &&
                found->second.status == particle::GpuParticleViewDiagnosticStatus::Pending) {
                found->second = std::move(result);
            }
        });
    }
}

VkDescriptorSet SceneRenderGraph::GetPerViewDescriptorSet() const
{
    if (!m_vkCore)
        return VK_NULL_HANDLE;
    uint32_t frameIdx = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
    return m_perViewFrames[frameIdx].GeometrySet();
}

rhi::BindGroupHandle SceneRenderGraph::GetPerViewBindGroup() const
{
    if (!m_vkCore)
        return {};
    const uint32_t frameIndex = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
    return m_perViewFrames[frameIndex].geometryGroup;
}

void SceneRenderGraph::SetCachedCameraVP(const Camera *camera, const glm::mat4 &view, const glm::mat4 &proj)
{
    bool cameraCut = m_hasCachedCameraVP && camera != m_cachedCamera;
    if (m_hasCachedCameraVP && !cameraCut) {
        const glm::mat4 previousInverseView = glm::inverse(m_cachedView);
        const glm::mat4 currentInverseView = glm::inverse(view);
        const glm::vec3 previousPosition = glm::vec3(previousInverseView[3]);
        const glm::vec3 currentPosition = glm::vec3(currentInverseView[3]);
        const glm::vec3 previousForward = -glm::normalize(glm::vec3(previousInverseView[2]));
        const glm::vec3 currentForward = -glm::normalize(glm::vec3(currentInverseView[2]));

        float projectionDelta = 0.0f;
        for (glm::length_t column = 0; column < 4; ++column) {
            for (glm::length_t row = 0; row < 4; ++row) {
                projectionDelta =
                    std::max(projectionDelta, std::abs(proj[column][row] - m_cachedUnjitteredProj[column][row]));
            }
        }

        constexpr float kCameraCutDistance = 10.0f;
        constexpr float kCameraCutDirectionCosine = 0.70710678f;
        const glm::vec3 cameraDelta = currentPosition - previousPosition;
        cameraCut = glm::dot(cameraDelta, cameraDelta) > kCameraCutDistance * kCameraCutDistance ||
                    glm::dot(previousForward, currentForward) < kCameraCutDirectionCosine || projectionDelta > 1e-3f;
    }
    if (cameraCut) {
        m_cameraHistoryValid = false;
        InvalidateTemporalHistory();
    }

    m_cachedCamera = camera;
    m_cachedView = view;
    m_drawView = view;
    m_cachedUnjitteredProj = proj;
    m_temporalJitterNdc =
        UsesTemporalHistory() ? ComputeTemporalJitterNdc(m_temporalSampleIndex, m_width, m_height) : glm::vec2(0.0f);
    m_cachedProj = ApplyTemporalJitter(proj, m_temporalJitterNdc);
    m_hasCachedCameraVP = true;
    (void)StageCameraMatrices(view, m_cachedProj);
}

glm::vec2 SceneRenderGraph::ComputeTemporalJitterNdc(uint32_t sampleIndex, uint32_t width, uint32_t height)
{
    if (width == 0 || height == 0)
        return glm::vec2(0.0f);

    const auto halton = [](uint32_t index, uint32_t base) {
        float value = 0.0f;
        float fraction = 1.0f;
        while (index > 0) {
            fraction /= static_cast<float>(base);
            value += fraction * static_cast<float>(index % base);
            index /= base;
        }
        return value;
    };

    const uint32_t sequenceIndex = sampleIndex % kTemporalJitterSampleCount + 1u;
    const glm::vec2 pixelOffset{halton(sequenceIndex, 2u) - 0.5f, halton(sequenceIndex, 3u) - 0.5f};
    return {2.0f * pixelOffset.x / static_cast<float>(width), 2.0f * pixelOffset.y / static_cast<float>(height)};
}

glm::mat4 SceneRenderGraph::ApplyTemporalJitter(const glm::mat4 &projection, const glm::vec2 &jitterNdc)
{
    glm::mat4 result = projection;
    for (glm::length_t column = 0; column < 4; ++column) {
        result[column][0] += jitterNdc.x * projection[column][3];
        result[column][1] += jitterNdc.y * projection[column][3];
    }
    return result;
}

bool SceneRenderGraph::StageCameraMatrices(const glm::mat4 &view, const glm::mat4 &proj,
                                           const glm::mat4 *previousViewProj)
{
    if (!m_vkCore)
        return false;
    const uint32_t frameIndex = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
    auto &frame = m_perViewFrames[frameIndex];
    if (!frame.cameraMatrix.IsValid())
        return false;

    UniformBufferObject camera{};
    camera.model = glm::mat4(1.0f);
    camera.view = view;
    camera.proj = proj;
    camera.previousViewProj =
        previousViewProj ? *previousViewProj : (m_cameraHistoryValid ? m_previousViewProj : proj * view);
    camera.inverseViewProj = glm::inverse(proj * view);
    const float nearClip = m_cachedCamera ? std::max(m_cachedCamera->GetNearClip(), 1.0e-5f) : 0.01f;
    const float farClip = m_cachedCamera ? std::max(m_cachedCamera->GetFarClip(), nearClip + 1.0e-4f) : 5000.0f;
    const float farOverNear = farClip / nearClip;
    camera.projectionParams = glm::vec4(nearClip, farClip, 1.0f / farClip, nearClip / farClip);
    camera.zBufferParams =
        glm::vec4(1.0f - farOverNear, farOverNear, (1.0f - farOverNear) / farClip, farOverNear / farClip);
    auto &rhiDevice = m_vkCore->GetDeviceContext().GetRhiDevice();
    if (!rhiDevice.WriteBuffer(frame.cameraMatrix, 0, &camera, sizeof(camera))) {
        INXLOG_ERROR("SceneRenderGraph: failed to upload camera-local matrix UBO");
        return false;
    }
    return true;
}

void SceneRenderGraph::StageCameraLighting(Scene *scene, Camera *camera, const glm::vec3 &cameraPosition,
                                           const ShaderLightingUBO &environmentLighting)
{
    if (!m_vkCore || !scene)
        return;

    m_cameraLightCollector.CollectLights(scene, cameraPosition);
    // environmentLighting values are already linear (converted once by the
    // scene collector's SetAmbient* setters) — copy them verbatim to avoid a
    // second sRGB -> linear conversion.
    m_cameraLightCollector.SetAmbientLinear(environmentLighting.ambientSkyColor,
                                            environmentLighting.ambientEquatorColor,
                                            environmentLighting.ambientGroundColor);
    const lighting::ShadowDepthRange visibleDepthRange =
        m_hasCachedDrawCalls ? VisibleShadowDepthRange(camera, m_cachedRenderers.DrawCalls())
                             : lighting::ShadowDepthRange{};
    m_cameraLightCollector.ComputeShadowVP(scene, cameraPosition, static_cast<float>(GetShadowMapResolution()), camera,
                                           visibleDepthRange);
    m_cameraLightCollector.BuildShaderLightingUBO();

    const uint32_t frameIndex = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
    auto &frame = m_perViewFrames[frameIndex];
    auto &rhiDevice = m_vkCore->GetDeviceContext().GetRhiDevice();
    const ShaderLightingUBO &lighting = m_cameraLightCollector.GetShaderLightingUBO();
    if (!frame.lighting.IsValid() || !rhiDevice.WriteBuffer(frame.lighting, 0, &lighting, sizeof(lighting))) {
        INXLOG_ERROR("SceneRenderGraph: failed to upload camera-local LightingUBO");
    }
    if (!m_cameraCanonicalLights.Update(frameIndex, m_cameraLightCollector.GetCanonicalLightSnapshot())) {
        INXLOG_ERROR("SceneRenderGraph: failed to upload camera-local canonical lights");
    }
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

void SceneRenderGraph::SetOutlineRenderer(OutlineRenderer *renderer)
{
    const bool enabled = m_renderView.kind == rhi::RenderViewKind::Scene && renderer && renderer->IsReady() &&
                         renderer->HasActiveOutline();
    if (m_outlineRenderer != renderer)
        m_outlinePipelineFailureReported = false;
    m_outlineRenderer = renderer;
    if (m_outlinePassesEnabled == enabled)
        return;

    m_outlinePassesEnabled = enabled;
    m_outlinePipelineFailureReported = false;
    m_needsRebuild = true;
}

MaterialPassPipelineDescriptor SceneRenderGraph::GetEditorOverlayMaterialPass() const
{
    auto descriptor =
        m_vkCore->GetMaterialPipelineManager().GetDefaultPassPipelineDescriptor(ShaderCompileTarget::Forward);
    if (!m_renderGraph || !m_renderGraph->SupportsDynamicRendering() || !m_sceneTarget)
        return descriptor;

    descriptor.colorFormats = {rhi::FromVkFormat(m_sceneTarget->GetColorFormat())};
    descriptor.depthFormat = rhi::FromVkFormat(m_sceneTarget->GetDepthFormat());
    descriptor.samples = rhi::FromVkSampleCount(m_sceneTarget->GetMsaaSampleCount());
    descriptor.depthReadOnly = descriptor.depthFormat != rhi::PixelFormat::Undefined;
    descriptor.renderingMode = MaterialPassRenderingMode::DynamicRendering;
    return descriptor;
}

void SceneRenderGraph::ApplyPythonGraph(const RenderGraphDescription &desc)
{
    if (!m_vkCore || !m_sceneTarget) {
        INXLOG_ERROR("SceneRenderGraph::ApplyPythonGraph: Not initialized");
        return;
    }

    RenderGraphDescription normalizedDesc = desc;
    const VkSampleCountFlagBits callbackSamples = m_vkCore->GetMaterialPipelineManager().GetSampleCount();
    const bool topologyChanged = !m_hasPythonGraph || !GraphDescEquals(normalizedDesc, m_pythonGraphDesc);
    const bool callbackContractChanged = m_pythonCallbackSamples != callbackSamples;
    if (!topologyChanged && !callbackContractChanged) {
        // Replaying an already-applied description must not clobber live
        // parameter blocks: effect edits arrive through UpdateParameterBlocks
        // *after* the description was built, so the description's push
        // constants are only authoritative when a genuinely new Python build
        // (fresh source revision) is being applied.
        if (desc.sourceRevision == 0 || desc.sourceRevision != m_pythonGraphSourceRevision) {
            std::vector<GraphParameterBlockUpdate> updates;
            for (const auto &pass : normalizedDesc.passes) {
                const GraphCommandDesc *command = PrimaryCommand(pass);
                if (command && !command->parameterBlock.empty()) {
                    updates.push_back({command->parameterBlock, 0, command->pushConstants});
                }
            }
            UpdateParameterBlocks(updates);
        }
        if (desc.sourceRevision != 0) {
            m_pythonGraphSourceRevision = desc.sourceRevision;
            m_pythonGraphDesc.sourceRevision = desc.sourceRevision;
        }
        return;
    }

    if (topologyChanged && !ValidatePythonGraphDescription(normalizedDesc, static_cast<uint32_t>(callbackSamples))) {
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
    const uint32_t graphFrameSamples = normalizedDesc.msaaSamples > 0
                                           ? static_cast<uint32_t>(normalizedDesc.msaaSamples)
                                           : static_cast<uint32_t>(callbackSamples);
    const bool dynamicGeometryAvailable = m_renderGraph && m_renderGraph->SupportsDynamicRendering();

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
        const GraphMaterialFilter materialFilter = command->materialFilter;
        MaterialPassPipelineDescriptor materialPass =
            vkCore->GetMaterialPipelineManager().GetDefaultPassPipelineDescriptor(command->shaderTarget);
        if (commandType == GraphCommandType::DrawRenderers || commandType == GraphCommandType::DrawShadowCasters) {
            materialPass.colorFormats.clear();
            uint32_t passSamples = graphFrameSamples;
            auto colorOutputs = passDesc.writeColors;
            std::sort(colorOutputs.begin(), colorOutputs.end(),
                      [](const auto &lhs, const auto &rhs) { return lhs.first < rhs.first; });
            for (const auto &[slot, textureName] : colorOutputs) {
                (void)slot;
                const auto texture = std::find_if(
                    normalizedDesc.textures.begin(), normalizedDesc.textures.end(),
                    [&textureName](const GraphTextureDesc &textureDesc) { return textureDesc.name == textureName; });
                if (texture != normalizedDesc.textures.end()) {
                    passSamples = EffectiveTextureSamples(*texture, graphFrameSamples);
                    materialPass.colorFormats.push_back(
                        texture->isBackbuffer ? rhi::FromVkFormat(vkCore->GetMaterialPipelineManager().GetColorFormat())
                                              : texture->format);
                }
            }
            if (colorOutputs.empty() && command->shaderTarget != ShaderCompileTarget::Depth &&
                command->shaderTarget != ShaderCompileTarget::Shadow) {
                materialPass.colorFormats.push_back(
                    rhi::FromVkFormat(vkCore->GetMaterialPipelineManager().GetColorFormat()));
            }
            if (!passDesc.writeDepth.empty()) {
                const auto depth = std::find_if(
                    normalizedDesc.textures.begin(), normalizedDesc.textures.end(),
                    [&passDesc](const GraphTextureDesc &desc) { return desc.name == passDesc.writeDepth; });
                materialPass.depthFormat =
                    depth != normalizedDesc.textures.end() ? depth->format : rhi::PixelFormat::Undefined;
                if (depth != normalizedDesc.textures.end())
                    passSamples = EffectiveTextureSamples(*depth, graphFrameSamples);
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
                        passSamples = EffectiveTextureSamples(*depth, graphFrameSamples);
                        break;
                    }
                }
            }
            materialPass.samples = ToRhiSampleCount(static_cast<int>(passSamples));
            materialPass.depthReadOnly =
                passDesc.writeDepth.empty() && materialPass.depthFormat != rhi::PixelFormat::Undefined;
            const bool dynamicGeometryTarget = command->shaderTarget == ShaderCompileTarget::Forward ||
                                               command->shaderTarget == ShaderCompileTarget::ForwardPlus ||
                                               command->shaderTarget == ShaderCompileTarget::GBuffer ||
                                               command->shaderTarget == ShaderCompileTarget::Motion ||
                                               command->shaderTarget == ShaderCompileTarget::Normal ||
                                               command->shaderTarget == ShaderCompileTarget::BaseColor ||
                                               command->shaderTarget == ShaderCompileTarget::Picking ||
                                               command->shaderTarget == ShaderCompileTarget::Shadow;
            if (dynamicGeometryAvailable && dynamicGeometryTarget &&
                (commandType == GraphCommandType::DrawRenderers ||
                 commandType == GraphCommandType::DrawShadowCasters)) {
                materialPass.renderingMode = MaterialPassRenderingMode::DynamicRendering;
            }
            m_pythonMaterialPasses[passDesc.name] = materialPass;
        }

        m_pythonCallbacks[passDesc.name] = [this, vkCore, commandType, queueMin, queueMax, screenUIListIndex,
                                            lightIndex, sortMode, overrideMaterial, passTag, materialFilter,
                                            materialPass](vk::RenderContext &ctx, uint32_t w, uint32_t h) {
            switch (commandType) {
            case GraphCommandType::DrawRenderers:
                vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, GetPerViewBindGroup(), m_drawView, queueMin,
                                          queueMax, sortMode, overrideMaterial, passTag, &materialPass, materialFilter);
                break;
            case GraphCommandType::DrawSkybox: {
                const int32_t skyboxQueue = EngineConfig::Get().skyboxQueue;
                vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, GetPerViewBindGroup(), m_drawView, skyboxQueue,
                                          skyboxQueue, "", "", "__infernux_internal_skybox");
                break;
            }
            case GraphCommandType::DrawShadowCasters:
                // Shadow caster pass: draw filtered objects using shadow pipeline
                // with lightVP from SceneLightCollector. The shadow pipeline is
                // lazily created inside DrawShadowCasters().
                vkCore->DrawShadowCasters(ctx.GetCommandBuffer(), w, h, queueMin, queueMax, m_shadowCameraResourceId,
                                          m_cameraLightCollector.GetShadowFrame(), lightIndex);
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
    const auto editorOverlayPass = GetEditorOverlayMaterialPass();
    m_pythonCallbacks[kComponentGizmosPassName] = [this, vkCore, editorOverlayPass](vk::RenderContext &ctx, uint32_t w,
                                                                                    uint32_t h) {
        vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, GetPerViewBindGroup(), m_drawView, COMP_GIZMO_QUEUE_MIN,
                                  COMP_GIZMO_QUEUE_MAX, "", "", "", &editorOverlayPass);
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
    m_pythonCallbacks[kEditorGizmosPassName] = [this, vkCore, editorOverlayPass](vk::RenderContext &ctx, uint32_t w,
                                                                                 uint32_t h) {
        vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, GetPerViewBindGroup(), m_drawView, GIZMO_QUEUE_MIN,
                                  GIZMO_QUEUE_MAX, "", "", "", &editorOverlayPass);
    };

    // ========================================================================
    // Auto-append editor tools pass (queue 25001-30000).
    // Translation/rotation/scale handles rendered on top of everything
    // (no depth test). In game view, no draw calls exist in this range.
    // ========================================================================
    static constexpr int TOOLS_QUEUE_MIN = 25001;
    static constexpr int TOOLS_QUEUE_MAX = 30000;
    static const std::string kEditorToolsPassName = "_EditorTools";
    m_pythonCallbacks[kEditorToolsPassName] = [this, vkCore, editorOverlayPass](vk::RenderContext &ctx, uint32_t w,
                                                                                uint32_t h) {
        vkCore->DrawSceneFiltered(ctx.GetCommandBuffer(), w, h, GetPerViewBindGroup(), m_drawView, TOOLS_QUEUE_MIN,
                                  TOOLS_QUEUE_MAX, "preserve", "", "", &editorOverlayPass);
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

uint32_t SceneRenderGraph::GetShadowMapResolution() const
{
    for (const auto &pass : m_pythonGraphDesc.passes) {
        const bool drawsShadows =
            std::any_of(pass.commands.begin(), pass.commands.end(), [](const GraphCommandDesc &command) {
                return command.type == GraphCommandType::DrawShadowCasters;
            });
        if (!drawsShadows || pass.writeDepth.empty())
            continue;

        const auto texture =
            std::find_if(m_pythonGraphDesc.textures.begin(), m_pythonGraphDesc.textures.end(),
                         [&](const GraphTextureDesc &candidate) { return candidate.name == pass.writeDepth; });
        if (texture == m_pythonGraphDesc.textures.end())
            return 0;

        uint32_t width = texture->width;
        uint32_t height = texture->height;
        if (texture->sizeDivisor > 0u) {
            width = std::max(m_width / texture->sizeDivisor, 1u);
            height = std::max(m_height / texture->sizeDivisor, 1u);
        } else {
            width = width > 0u ? width : m_width;
            height = height > 0u ? height : m_height;
        }
        return width == height ? width : 0u;
    }
    return 0;
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

    if (m_needsRebuild)
        RefreshForwardPlusParticleRequirement();

    if (UsesForwardPlus() && !PrepareForwardPlusFrame()) {
        INXLOG_ERROR("SceneRenderGraph: Forward+ resources are unavailable for the current view");
        m_graphBuilt = false;
        return;
    }

    if (m_needsRebuild) {
        // RenderGraph rebuilds can retire and recreate the transient shadow
        // image view. Vulkan may reuse the same raw handle value, so cached
        // handle equality cannot prove an existing descriptor still refers
        // to the new object. Force every frame-local descriptor generation
        // to be republished after the rebuild.
        InvalidatePerViewShadowBindings();
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
        const vk::ResourceHandle displayTarget =
            m_importedResolveTarget.IsValid() ? m_importedResolveTarget : m_importedColorTarget;
        m_renderView.color = m_renderGraph->ResolveRhiTextureView(displayTarget);
        m_renderView.depth = m_renderGraph->ResolveRhiTextureView(m_importedDepthTarget);
        m_needsCompile = false;
    }

    if (m_graphBuilt && m_outlinePassesEnabled) {
        const auto maskContract = m_renderGraph->GetPassRenderingContract("__EditorOutlineMask");
        const auto compositeContract = m_renderGraph->GetPassRenderingContract("__EditorOutlineComposite");
        const bool outlineReady = m_outlineRenderer && m_outlineRenderer->EnsureGraphPipelines(
                                                           m_renderGraph->GetPassRenderPass("__EditorOutlineMask"),
                                                           m_renderGraph->GetPassRenderPass("__EditorOutlineComposite"),
                                                           m_sceneTarget->GetMsaaSampleCount(),
                                                           maskContract.attachments, compositeContract.attachments);
        if (outlineReady) {
            m_outlinePipelineFailureReported = false;
        } else if (!m_outlinePipelineFailureReported) {
            INXLOG_ERROR("SceneRenderGraph: failed to prepare graph-compatible editor outline pipelines");
            m_outlinePipelineFailureReported = true;
        }
        // Outline is an editor aid. Keep the otherwise valid scene graph
        // executable and retry pipeline publication on a later frame instead
        // of blacking out the Scene view after a transient creation failure.
    }

    if (m_graphBuilt) {
        RefreshPerViewShadowDescriptor();
    }
}

bool SceneRenderGraph::UsesForwardPlus() const
{
    if (!m_hasPythonGraph)
        return false;
    for (const auto &pass : m_pythonGraphDesc.passes) {
        for (const auto &command : pass.commands) {
            if (command.type == GraphCommandType::DrawRenderers &&
                command.shaderTarget == ShaderCompileTarget::ForwardPlus) {
                return true;
            }
        }
    }
    return false;
}

void SceneRenderGraph::RefreshForwardPlusParticleRequirement()
{
    m_forwardPlusParticlesRequired = false;
    if (!m_hasPythonGraph || !m_particleDrawRegistry)
        return;
    for (const auto &pass : m_pythonGraphDesc.passes) {
        for (const auto &command : pass.commands) {
            if (command.type != GraphCommandType::DrawRenderers ||
                command.shaderTarget != ShaderCompileTarget::ForwardPlus) {
                continue;
            }
            const auto entries = m_particleDrawRegistry->Snapshot(command.queueMin, command.queueMax);
            if (std::any_of(entries.begin(), entries.end(),
                            [](const auto &entry) { return entry.semantics.receiveSceneLighting; })) {
                m_forwardPlusParticlesRequired = true;
                return;
            }
        }
    }
}

bool SceneRenderGraph::PrepareForwardPlusFrame()
{
    if (!m_vkCore || !m_forwardPlusGeometryGrid.IsValid() ||
        (m_forwardPlusParticlesRequired && !m_forwardPlusParticleGrid.IsValid()))
        return false;
    const uint32_t frameIndex = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
    const auto &lights = m_cameraCanonicalLights.Frame(frameIndex);
    if (!lights.buffer.IsValid())
        return false;

    const auto previousHeaders = m_forwardPlusGeometryGrid.Frame(frameIndex).headers;
    const auto previousMasks = m_forwardPlusGeometryGrid.Frame(frameIndex).lightMasks;
    const auto previousLights = m_forwardPlusGeometryGrid.Frame(frameIndex).canonicalLights;
    if (!m_forwardPlusGeometryGrid.PrepareFrame(frameIndex, m_width, m_height, lights.localCount,
                                                CanonicalLightAffectsGeometry, lights.buffer)) {
        return false;
    }
    const auto &frame = m_forwardPlusGeometryGrid.Frame(frameIndex);
    if (frame.headers != previousHeaders || frame.lightMasks != previousMasks ||
        frame.canonicalLights != previousLights)
        m_needsRebuild = true;

    if (m_forwardPlusParticlesRequired) {
        const auto previousParticleHeaders = m_forwardPlusParticleGrid.Frame(frameIndex).headers;
        const auto previousParticleMasks = m_forwardPlusParticleGrid.Frame(frameIndex).lightMasks;
        const auto previousParticleLights = m_forwardPlusParticleGrid.Frame(frameIndex).canonicalLights;
        if (!m_forwardPlusParticleGrid.PrepareFrame(frameIndex, m_width, m_height, lights.localCount,
                                                    CanonicalLightAffectsParticles, lights.buffer)) {
            return false;
        }
        const auto &particleFrame = m_forwardPlusParticleGrid.Frame(frameIndex);
        if (particleFrame.headers != previousParticleHeaders || particleFrame.lightMasks != previousParticleMasks ||
            particleFrame.canonicalLights != previousParticleLights) {
            m_needsRebuild = true;
        }
    }

    RetireForwardPlusResources();
    auto &viewFrame = m_perViewFrames[frameIndex];
    const VkDescriptorSet descriptorSet = viewFrame.GeometrySet();
    if (descriptorSet == VK_NULL_HANDLE)
        return false;
    auto &rhiDevice = m_vkCore->GetDeviceContext().GetRhiDevice();
    const PerViewBufferBindingState geometryBindings{rhiDevice.Resolve(lights.buffer),
                                                     rhiDevice.Resolve(frame.headers),
                                                     rhiDevice.Resolve(frame.lightMasks),
                                                     rhiDevice.Resolve(viewFrame.lighting),
                                                     lights.dataBytes,
                                                     frame.config.headerBytes,
                                                     frame.config.maskBytes,
                                                     true};
    auto bindingsDiffer = [](const PerViewBufferBindingState &left, const PerViewBufferBindingState &right) {
        return !left.initialized || left.canonicalLights != right.canonicalLights ||
               left.tileHeaders != right.tileHeaders || left.tileLightMasks != right.tileLightMasks ||
               left.lighting != right.lighting || left.canonicalBytes != right.canonicalBytes ||
               left.tileHeaderBytes != right.tileHeaderBytes || left.tileLightMaskBytes != right.tileLightMaskBytes;
    };
    const bool geometryBindingsDirty = bindingsDiffer(viewFrame.geometryBindings, geometryBindings);
    bool particleBindingsDirty = false;
    PerViewBufferBindingState particleBindings{};
    if (m_forwardPlusParticlesRequired) {
        const auto &particleFrame = m_forwardPlusParticleGrid.Frame(frameIndex);
        particleBindings = {rhiDevice.Resolve(lights.buffer),
                            rhiDevice.Resolve(particleFrame.headers),
                            rhiDevice.Resolve(particleFrame.lightMasks),
                            rhiDevice.Resolve(viewFrame.lighting),
                            lights.dataBytes,
                            particleFrame.config.headerBytes,
                            particleFrame.config.maskBytes,
                            true};
        particleBindingsDirty = bindingsDiffer(viewFrame.particleBindings, particleBindings);
    }
    // InxRenderer waits for this frame slot's graphics fence before any
    // SceneRenderGraph update. Each slot owns distinct per-view descriptor
    // sets, so publishing new buffer bindings here cannot race a command
    // buffer that still consumes the same sets. A device-wide idle would
    // unnecessarily drain the other frame slot and all independent queues.
    if (geometryBindingsDirty) {
        m_vkCore->UpdatePerViewForwardPlusBuffers(descriptorSet, lights.buffer, lights.dataBytes, frame.headers,
                                                  frame.config.headerBytes, frame.lightMasks, frame.config.maskBytes,
                                                  viewFrame.lighting, sizeof(ShaderLightingUBO));
        viewFrame.geometryBindings = geometryBindings;
    }
    if (m_forwardPlusParticlesRequired) {
        if (viewFrame.ParticleSet() == VK_NULL_HANDLE || !m_perViewLayout.IsValid() ||
            !viewFrame.particleGroup.IsValid() || !viewFrame.lighting.IsValid()) {
            return false;
        }
        if (particleBindingsDirty) {
            const auto &particleFrame = m_forwardPlusParticleGrid.Frame(frameIndex);
            m_vkCore->UpdatePerViewForwardPlusBuffers(viewFrame.ParticleSet(), lights.buffer, lights.dataBytes,
                                                      particleFrame.headers, particleFrame.config.headerBytes,
                                                      particleFrame.lightMasks, particleFrame.config.maskBytes,
                                                      viewFrame.lighting, sizeof(ShaderLightingUBO));
            viewFrame.particleBindings = particleBindings;
        }
    }
    return true;
}

void SceneRenderGraph::RetireForwardPlusResources()
{
    auto retired = m_forwardPlusGeometryGrid.TakeRetiredResources();
    auto retiredParticles = m_forwardPlusParticleGrid.TakeRetiredResources();
    retired.insert(retired.end(), std::make_move_iterator(retiredParticles.begin()),
                   std::make_move_iterator(retiredParticles.end()));
    if (retired.empty() || !m_vkCore)
        return;
    rhi::Device *device = &m_vkCore->GetDeviceContext().GetRhiDevice();
    m_vkCore->GetRetirementQueue().Retire([device, retired = std::move(retired)]() mutable {
        for (const auto &resource : retired) {
            device->Release(resource.bindGroup);
            device->Release(resource.consumerBindGroup);
            device->Release(resource.lightMasks);
            device->Release(resource.headers);
        }
    });
}

bool SceneRenderGraph::PrepareSubmissionExecution()
{
    if (!m_sceneTarget || !m_sceneTarget->IsReady() || !m_renderGraph) {
        return false;
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

    if (m_importedDepthTarget.IsValid()) {
        m_renderGraph->SetResourceInitialState(m_importedDepthTarget, rhi::TextureLayout::DepthStencilAttachment,
                                               rhi::Access::DepthRead | rhi::Access::DepthWrite,
                                               rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth);
    }

    if (!m_graphBuilt)
        return false;

    RefreshShadowAtlasUpdateState();

    BindTemporalHistoryResources();

    if (m_hasCameraClearOverride && !m_mainClearPassName.empty()) {
        if (m_cameraClearFlags == CameraClearFlags::SolidColor) {
            m_renderGraph->UpdatePassClearColor(m_mainClearPassName, m_cameraBgColor.r, m_cameraBgColor.g,
                                                m_cameraBgColor.b, m_cameraBgColor.a);
        }
    }

    m_prevClearStateValid = true;
    m_prevCameraClearFlags = m_cameraClearFlags;
    m_prevCameraBgColor = m_cameraBgColor;

    m_fullscreenRenderer.ResetPool();

    if (!m_particleCullers.empty()) {
        Frustum frustum;
        frustum.ExtractFromMatrix(m_cachedProj * m_cachedView);
        for (uint32_t index = 0; index < particle::ParticleGpuCuller::PlaneCount; ++index) {
            const auto &plane = frustum.GetPlane(static_cast<Frustum::PlaneIndex>(index));
            m_particleFrustumPlanes[index * 4 + 0] = plane.normal.x;
            m_particleFrustumPlanes[index * 4 + 1] = plane.normal.y;
            m_particleFrustumPlanes[index * 4 + 2] = plane.normal.z;
            m_particleFrustumPlanes[index * 4 + 3] = plane.distance;
        }
    }
    return true;
}

uint64_t SceneRenderGraph::ComputeShadowContentSignature(bool &hasDynamicCaster) const
{
    hasDynamicCaster = false;
    uint64_t hash = kShadowHashOffset;
    HashShadowValue(hash, m_cachedSubmissionSignature);
    HashShadowValue(hash, m_cachedObjectBufferRevision);
    if (m_vkCore) {
        const uint64_t materialGeneration = m_vkCore->GetMaterialPipelineManager().GetPublicationGeneration();
        HashShadowValue(hash, materialGeneration);
    }

    const auto &shadowFrame = m_cameraLightCollector.GetShadowFrame();
    HashShadowValue(hash, shadowFrame.atlasSize);
    const uint64_t viewCount = shadowFrame.views.size();
    const uint64_t assignmentCount = shadowFrame.assignments.size();
    HashShadowValue(hash, viewCount);
    HashShadowValue(hash, assignmentCount);
    for (const auto &view : shadowFrame.views) {
        HashShadowValue(hash, view.lightId);
        HashShadowValue(hash, view.type);
        HashShadowValue(hash, view.subView);
        HashShadowValue(hash, view.viewProjection);
        HashShadowValue(hash, view.atlas);
        HashShadowValue(hash, view.nearPlane);
        HashShadowValue(hash, view.farPlane);
        HashShadowValue(hash, view.worldUnitsPerTexel);
        HashShadowValue(hash, view.filterRadiusTexels);
        HashShadowValue(hash, view.splitNear);
        HashShadowValue(hash, view.splitFar);
        HashShadowValue(hash, view.lightVector);
        HashShadowValue(hash, view.lightVectorIsPosition);
        HashShadowValue(hash, view.depthBiasTexels);
        HashShadowValue(hash, view.normalBiasTexels);
        HashShadowValue(hash, view.viewRight);
        HashShadowValue(hash, view.viewUp);
        HashShadowValue(hash, view.cullingMask);
    }
    for (const auto &assignment : shadowFrame.assignments) {
        HashShadowValue(hash, assignment.lightId);
        HashShadowValue(hash, assignment.firstView);
        HashShadowValue(hash, assignment.viewCount);
    }

    // Transform revisions cover ordinary moving renderers. GPU skinning and
    // shadow-casting particles mutate device buffers without necessarily
    // changing that revision, so those paths deliberately remain dynamic.
    for (const DrawCall &drawCall : m_cachedShadowRenderers.DrawCalls()) {
        if (drawCall.skinBoneMatrices != nullptr) {
            hasDynamicCaster = true;
            break;
        }
    }
    if (!hasDynamicCaster && m_particleDrawRegistry) {
        const auto particles = m_particleDrawRegistry->SnapshotShared(std::numeric_limits<int32_t>::min(),
                                                                      std::numeric_limits<int32_t>::max());
        if (particles) {
            hasDynamicCaster = std::any_of(particles->begin(), particles->end(), [](const auto &entry) {
                return entry.semantics.castShadows && entry.renderer && entry.renderer->CanCastShadows();
            });
        }
    }
    return hash == 0 ? 1 : hash;
}

void SceneRenderGraph::RefreshShadowAtlasUpdateState()
{
    if (!m_hasShadowCasterPass || m_cameraLightCollector.GetShadowFrame().views.empty()) {
        m_pendingShadowContentSignature = 0;
        m_shadowAtlasUpdateRequired = false;
        return;
    }
    bool hasDynamicCaster = false;
    m_pendingShadowContentSignature = ComputeShadowContentSignature(hasDynamicCaster);
    m_shadowAtlasUpdateRequired =
        hasDynamicCaster || !m_shadowAtlasValid || m_pendingShadowContentSignature != m_committedShadowContentSignature;
}

void SceneRenderGraph::CommitShadowAtlasUpdate()
{
    m_committedShadowContentSignature = m_pendingShadowContentSignature;
    m_shadowAtlasValid = true;
    m_shadowAtlasUpdateRequired = false;
}

bool SceneRenderGraph::CompleteSubmissionExecution(VkCommandBuffer commandBuffer)
{
    if (!m_graphBuilt || !m_renderGraph || commandBuffer == VK_NULL_HANDLE)
        return false;

    RecordParticleViewDiagnostics(commandBuffer);
    CommitTemporalHistory();
    ++m_executionCount;
    m_lastExecutedBuildRevision = m_graphBuildRevision;
    return true;
}

void SceneRenderGraph::Execute(VkCommandBuffer commandBuffer)
{
    if (!PrepareSubmissionExecution())
        return;
    m_renderGraph->Execute(commandBuffer);
    (void)CompleteSubmissionExecution(commandBuffer);
}

void SceneRenderGraph::RefreshPerViewShadowDescriptor()
{
    if (!m_vkCore) {
        return;
    }

    VkDescriptorSet graphShadowDesc = GetPerViewDescriptorSet();
    const uint32_t frameIndex = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
    auto &viewFrame = m_perViewFrames[frameIndex];
    VkDescriptorSet particleShadowDesc = viewFrame.ParticleSet();
    if (graphShadowDesc == VK_NULL_HANDLE || particleShadowDesc == VK_NULL_HANDLE) {
        return;
    }

    if (!m_shadowMapInputHandle.IsValid() || !m_renderGraph) {
        static int s_missingShadowInputWarnCount = 0;
        if (s_missingShadowInputWarnCount++ < 8) {
            INXLOG_WARN("SceneRenderGraph: no valid shadowMap input handle for per-view descriptor; binding fallback "
                        "white texture");
        }
        if (!viewFrame.shadowBinding.fallback) {
            m_vkCore->ClearPerViewShadowMap(graphShadowDesc);
            m_vkCore->ClearPerViewShadowMap(particleShadowDesc);
            viewFrame.shadowBinding = {};
        }
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
        if (!viewFrame.shadowBinding.fallback) {
            m_vkCore->ClearPerViewShadowMap(graphShadowDesc);
            m_vkCore->ClearPerViewShadowMap(particleShadowDesc);
            viewFrame.shadowBinding = {};
        }
        return;
    }

    const VkImageLayout imageLayout = m_shadowMapInputIsDepth ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL
                                                              : VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    const auto &bound = viewFrame.shadowBinding;
    if (!bound.fallback && bound.imageView == view && bound.sampler == shadowSampler && bound.layout == imageLayout)
        return;
    // The renderer has already waited for this frame slot. Geometry and
    // particles publish the camera-local shadow binding together so neither
    // domain can observe a half-updated per-view generation.
    m_vkCore->UpdatePerViewShadowMap(graphShadowDesc, view, shadowSampler, imageLayout);
    m_vkCore->UpdatePerViewShadowMap(particleShadowDesc, view, shadowSampler, imageLayout);
    viewFrame.shadowBinding = {view, shadowSampler, imageLayout, false};
}

void SceneRenderGraph::InvalidatePerViewShadowBindings() noexcept
{
    for (auto &frame : m_perViewFrames)
        frame.shadowBinding = {};
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
        if (m_vkCore) {
            std::vector<std::string> fullscreenShaders;
            for (const auto &pass : m_pythonGraphDesc.passes) {
                for (const auto &command : pass.commands) {
                    if (command.type == GraphCommandType::FullscreenQuad && !command.shaderName.empty() &&
                        std::find(fullscreenShaders.begin(), fullscreenShaders.end(), command.shaderName) ==
                            fullscreenShaders.end()) {
                        fullscreenShaders.push_back(command.shaderName);
                    }
                }
            }
            if (!fullscreenShaders.empty()) {
                std::sort(fullscreenShaders.begin(), fullscreenShaders.end());
                result += "Fullscreen shader fingerprints (resident SPIR-V):\n";
                for (const auto &shader : fullscreenShaders) {
                    result += "  " + shader + "=" +
                              std::to_string(m_vkCore->GetShaderCodeFingerprint(shader, "fragment")) + "\n";
                }
            }
        }
        if (!m_parameterBlocks.empty()) {
            result += "Parameter blocks (authored defaults, before native runtime overrides) (" +
                      std::to_string(m_parameterBlocks.size()) + "):\n";
            for (const auto &[id, block] : m_parameterBlocks) {
                result += "  " + id + " [";
                for (size_t index = 0; index < block.names.size(); ++index) {
                    if (index != 0)
                        result += ", ";
                    result += block.names[index] + "=" + std::to_string(block.values.values[index]);
                }
                result += "]\n";
            }
        }
    }

    if (!m_temporalHistories.empty()) {
        std::vector<std::string> temporalKeys;
        temporalKeys.reserve(m_temporalHistories.size());
        for (const auto &[key, history] : m_temporalHistories) {
            (void)history;
            temporalKeys.push_back(key);
        }
        std::sort(temporalKeys.begin(), temporalKeys.end());

        result += "Temporal histories (" + std::to_string(temporalKeys.size()) + "):\n";
        for (const auto &key : temporalKeys) {
            const auto &history = m_temporalHistories.at(key);
            result += "  " + key + " [" + std::to_string(history.width) + "x" + std::to_string(history.height) +
                      ", valid=" + (history.valid ? "Yes" : "No") + ", read=" + std::to_string(history.readIndex) +
                      ", write=" + std::to_string(history.readIndex ^ 1u) + "]\n";
        }
        result += "Temporal jitter: sample=" + std::to_string(m_temporalSampleIndex) + "/" +
                  std::to_string(kTemporalJitterSampleCount) + ", ndc=(" + std::to_string(m_temporalJitterNdc.x) +
                  ", " + std::to_string(m_temporalJitterNdc.y) + ")\n";
    }

    // Add underlying RenderGraph debug info
    if (m_renderGraph) {
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

    // Color and depth belong to the same persistent output. Importing the
    // target-owned depth image lets ordered camera graphs preserve or clear a
    // shared depth domain according to CameraClearFlags instead of allocating
    // a private depth image per graph.
    m_importedDepthTarget = m_renderGraph->ImportTexture(
        "SceneDepth", m_sceneTarget->GetDepthImage(), m_sceneTarget->GetDepthImageView(),
        m_sceneTarget->GetDepthFormat(), m_width, m_height, m_sceneTarget->GetMsaaSampleCount());
}

void SceneRenderGraph::ImportTemporalHistoryResources(std::unordered_map<std::string, vk::ResourceHandle> &handles)
{
    if (!m_vkCore || !m_renderGraph || m_width == 0 || m_height == 0)
        return;

    struct RequestedHistory
    {
        const GraphTextureDesc *read = nullptr;
        const GraphTextureDesc *write = nullptr;
    };
    std::unordered_map<std::string, RequestedHistory> requested;
    for (const auto &texture : m_pythonGraphDesc.textures) {
        if (texture.role == GraphTextureRole::Transient)
            continue;
        auto &pair = requested[texture.temporalKey];
        if (texture.role == GraphTextureRole::TemporalRead)
            pair.read = &texture;
        else
            pair.write = &texture;
    }

    auto &device = m_vkCore->GetDeviceContext().GetRhiDevice();
    for (auto it = m_temporalHistories.begin(); it != m_temporalHistories.end();) {
        const auto request = requested.find(it->first);
        const bool reusable = request != requested.end() && request->second.read && request->second.write &&
                              it->second.width == m_width && it->second.height == m_height &&
                              it->second.format == request->second.read->format;
        if (reusable) {
            ++it;
            continue;
        }
        TemporalHistoryResource retired = std::move(it->second);
        it = m_temporalHistories.erase(it);
        rhi::Device *retiredDevice = &device;
        m_vkCore->GetRetirementQueue().Retire([retiredDevice, retired = std::move(retired)]() mutable {
            for (const auto view : retired.views)
                retiredDevice->Release(view);
            for (const auto texture : retired.textures)
                retiredDevice->Release(texture);
        });
    }

    for (const auto &[key, request] : requested) {
        if (!request.read || !request.write)
            continue;
        auto [it, inserted] = m_temporalHistories.try_emplace(key);
        auto &history = it->second;
        if (inserted) {
            rhi::TextureDesc textureDesc;
            textureDesc.width = m_width;
            textureDesc.height = m_height;
            textureDesc.format = request.read->format;
            textureDesc.samples = rhi::SampleCount::One;
            textureDesc.usage = rhi::TextureUsageFlags::Sampled | rhi::TextureUsageFlags::TransferSource |
                                rhi::TextureUsageFlags::TransferDestination;
            bool created = true;
            for (uint32_t index = 0; index < 2; ++index) {
                history.textures[index] = device.CreateTexture(textureDesc);
                if (!history.textures[index].IsValid()) {
                    created = false;
                    break;
                }
                rhi::TextureViewDesc viewDesc;
                viewDesc.texture = history.textures[index];
                viewDesc.format = textureDesc.format;
                history.views[index] = device.CreateTextureView(viewDesc);
                if (!history.views[index].IsValid()) {
                    created = false;
                    break;
                }
            }
            if (!created) {
                for (const auto view : history.views)
                    device.Release(view);
                for (const auto texture : history.textures)
                    device.Release(texture);
                m_temporalHistories.erase(it);
                INXLOG_ERROR("SceneRenderGraph: failed to allocate temporal history '", key, "'");
                continue;
            }
            history.format = textureDesc.format;
            history.width = m_width;
            history.height = m_height;
        }

        history.readName = request.read->name;
        history.writeName = request.write->name;
        const uint32_t writeIndex = history.readIndex ^ 1u;
        history.readHandle = m_renderGraph->ImportTexture(
            history.readName, history.textures[history.readIndex], history.views[history.readIndex],
            rhi::ToVkFormat(history.format), history.width, history.height);
        history.writeHandle =
            m_renderGraph->ImportTexture(history.writeName, history.textures[writeIndex], history.views[writeIndex],
                                         rhi::ToVkFormat(history.format), history.width, history.height);
        handles[history.readName] = history.readHandle;
        handles[history.writeName] = history.writeHandle;
        if (!m_renderView.history.IsValid())
            m_renderView.history = history.views[history.readIndex];
    }
}

void SceneRenderGraph::BindTemporalHistoryResources()
{
    if (!m_vkCore || !m_renderGraph)
        return;
    auto &device = m_vkCore->GetDeviceContext().GetRhiDevice();
    m_renderView.history = {};
    for (auto &[key, history] : m_temporalHistories) {
        (void)key;
        const uint32_t writeIndex = history.readIndex ^ 1u;
        if (!m_renderGraph->UpdateImportedTexture(history.readHandle,
                                                  device.Resolve(history.textures[history.readIndex]),
                                                  device.Resolve(history.views[history.readIndex])) ||
            !m_renderGraph->UpdateImportedTexture(history.writeHandle, device.Resolve(history.textures[writeIndex]),
                                                  device.Resolve(history.views[writeIndex]))) {
            history.valid = false;
            continue;
        }
        if (history.valid) {
            m_renderGraph->SetResourceInitialState(history.readHandle, rhi::TextureLayout::TransferDestination,
                                                   rhi::Access::TransferWrite, rhi::PipelineStage::Transfer);
            m_renderGraph->SetResourceInitialState(history.writeHandle, rhi::TextureLayout::ShaderReadOnly,
                                                   rhi::Access::ShaderRead, rhi::PipelineStage::FragmentShader);
        } else {
            m_renderGraph->SetResourceInitialState(history.readHandle, rhi::TextureLayout::Undefined, rhi::Access::None,
                                                   rhi::PipelineStage::Top);
            m_renderGraph->SetResourceInitialState(history.writeHandle, rhi::TextureLayout::Undefined,
                                                   rhi::Access::None, rhi::PipelineStage::Top);
        }
        if (!m_renderView.history.IsValid())
            m_renderView.history = history.views[history.readIndex];
    }
}

void SceneRenderGraph::CommitTemporalHistory()
{
    for (auto &[key, history] : m_temporalHistories) {
        (void)key;
        history.valid = true;
        history.readIndex ^= 1u;
    }
}

void SceneRenderGraph::UpdateMainPassClearSettings(CameraClearFlags clearFlags, const glm::vec4 &bgColor,
                                                   bool dithering, bool stopNaNs)
{
    m_hasCameraClearOverride = true;
    m_cameraClearFlags = clearFlags;
    // Authored camera background color is sRGB; the scene color target is
    // linear (encoded for display by the display encode pass).
    m_cameraBgColor = inx::color::SrgbToLinear(bgColor);
    m_cameraDithering = dithering;
    m_cameraStopNaNs = stopNaNs;

    if (m_prevClearStateValid && m_prevCameraClearFlags != clearFlags) {
        m_needsRebuild = true;
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
        if (tex.role == GraphTextureRole::Transient && !tex.isBackbuffer && !tex.isDepth) {
            uint32_t texW = (tex.width > 0) ? tex.width : width;
            uint32_t texH = (tex.height > 0) ? tex.height : height;
            if (tex.sizeDivisor > 1) {
                texW = std::max(1u, width / tex.sizeDivisor);
                texH = std::max(1u, height / tex.sizeDivisor);
            }
            const uint32_t requestedSamples =
                tex.samples == 0 ? static_cast<uint32_t>(m_sceneTarget->GetMsaaSampleCount()) : tex.samples;
            vk::ResourceHandle handle = m_renderGraph->RegisterTransientTexture(
                tex.name, texW, texH, rhi::ToVkFormat(tex.format),
                rhi::ToVkSampleCount(ToRhiSampleCount(static_cast<int>(requestedSamples))), true);
            customRTHandles[tex.name] = handle;
        }
    }

    // Custom-size depth textures (shadow maps and offscreen depth targets)
    for (const auto &tex : m_pythonGraphDesc.textures) {
        if (tex.role == GraphTextureRole::Transient && tex.isDepth &&
            ((tex.width > 0 && tex.height > 0) || tex.sizeDivisor > 1)) {
            uint32_t texW = tex.width > 0 ? tex.width : std::max(1u, width / tex.sizeDivisor);
            uint32_t texH = tex.height > 0 ? tex.height : std::max(1u, height / tex.sizeDivisor);
            const uint32_t requestedSamples =
                tex.samples == 0 ? static_cast<uint32_t>(m_sceneTarget->GetMsaaSampleCount()) : tex.samples;
            vk::ResourceHandle handle = m_renderGraph->RegisterTransientTexture(
                tex.name, texW, texH, rhi::ToVkFormat(tex.format),
                rhi::ToVkSampleCount(ToRhiSampleCount(static_cast<int>(requestedSamples))), true);
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
        if (m_renderGraph->SupportsDynamicRendering())
            builder.UseDynamicRendering();

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

vk::ResourceHandle SceneRenderGraph::AppendEditorOutline(vk::ResourceHandle displayTarget)
{
    if (!m_outlinePassesEnabled || !m_outlineRenderer || !displayTarget.IsValid() || !m_visibleRendererList.IsValid() ||
        !m_sceneTarget || !m_sceneTarget->IsReady()) {
        return displayTarget;
    }

    const uint32_t width = m_width;
    const uint32_t height = m_height;
    const vk::ResourceHandle rendererList = m_visibleRendererList;
    vk::ResourceHandle mask;
    vk::ResourceHandle writtenMask;
    m_renderGraph->AddPass("__EditorOutlineMask", [this, width, height, rendererList, &mask,
                                                   &writtenMask](vk::PassBuilder &builder) {
        mask = builder.ImportTexture("__EditorOutlineMaskTexture", m_sceneTarget->GetOutlineMaskImage(),
                                     m_sceneTarget->GetOutlineMaskImageView(), VK_FORMAT_R8G8B8A8_UNORM, width, height);
        if (mask.IsValid()) {
            m_renderGraph->SetResourceInitialState(mask, rhi::TextureLayout::ShaderReadOnly, rhi::Access::ShaderRead,
                                                   rhi::PipelineStage::FragmentShader);
            writtenMask = builder.WriteColor(mask, 0);
            builder.SetClearColor(0.0f, 0.0f, 0.0f, 0.0f);
        }
        builder.ReadRendererList(rendererList);
        builder.SetDepthTest(false);
        builder.SetRenderArea(width, height);
        if (m_renderGraph->SupportsDynamicRendering())
            builder.UseDynamicRendering();

        return [this, rendererList](vk::RenderContext &context) {
            if (!m_outlineRenderer)
                return;
            const RendererList *list = context.GetRendererList(rendererList);
            if (list)
                m_outlineRenderer->RecordMaskDraws(context.GetCommandBuffer(), list->DrawCalls(),
                                                   GetPerViewBindGroup());
        };
    });

    if (!writtenMask.IsValid())
        return displayTarget;

    vk::ResourceHandle composited;
    m_renderGraph->AddPass("__EditorOutlineComposite",
                           [this, width, height, displayTarget, writtenMask, &composited](vk::PassBuilder &builder) {
                               builder.Read(writtenMask, rhi::PipelineStage::FragmentShader);
                               composited = builder.WriteColor(displayTarget, 0);
                               builder.SetDepthTest(false);
                               builder.SetRenderArea(width, height);
                               if (m_renderGraph->SupportsDynamicRendering())
                                   builder.UseDynamicRendering();
                               return [this](vk::RenderContext &context) {
                                   if (m_outlineRenderer)
                                       m_outlineRenderer->RecordCompositeDraw(context.GetCommandBuffer());
                               };
                           });
    return composited.IsValid() ? composited : displayTarget;
}

void SceneRenderGraph::FinalizeGraphOutput(const std::unordered_map<std::string, vk::ResourceHandle> &customRTHandles)
{
    vk::ResourceHandle graphOutput;
    if (m_hasPythonGraph && !m_pythonGraphDesc.outputTexture.empty()) {
        auto texIt = std::find_if(m_pythonGraphDesc.textures.begin(), m_pythonGraphDesc.textures.end(),
                                  [&](const GraphTextureDesc &t) { return t.name == m_pythonGraphDesc.outputTexture; });
        if (texIt != m_pythonGraphDesc.textures.end()) {
            if (!texIt->isBackbuffer && !texIt->isDepth) {
                auto rtIt = customRTHandles.find(m_pythonGraphDesc.outputTexture);
                if (rtIt != customRTHandles.end())
                    graphOutput = rtIt->second;
            }
        }
    }

    vk::ResourceHandle displayTarget = m_importedColorTarget;
    const vk::ResourceHandle msaaSource = m_importedColorTarget;
    if (m_sceneTarget->IsMsaaEnabled() && msaaSource.IsValid() && m_importedResolveTarget.IsValid()) {
        const auto resolveDestination = m_importedResolveTarget;
        const uint32_t width = m_width;
        const uint32_t height = m_height;
        vk::ResourceHandle resolvedVersion;
        m_renderGraph->AddTransferPass("__SceneFinalMsaaResolve", [=, &resolvedVersion](vk::PassBuilder &builder) {
            // Vulkan image resolve requires a graphics-capable command pool,
            // while its resource accesses remain transfer operations.
            builder.SetQueueRole(rhi::QueueRole::Graphics);
            builder.TransferRead(msaaSource);
            resolvedVersion = builder.TransferWrite(resolveDestination);
            builder.SetRenderArea(width, height);
            return [=](vk::RenderContext &context) {
                context.GetTransferCommandEncoder().ResolveTexture(
                    context.GetTextureHandle(msaaSource), context.GetTextureHandle(resolvedVersion),
                    {rhi::TextureAspect::Color, 0, 0, 0, 0, width, height, 1});
            };
        });
        if (resolvedVersion.IsValid()) {
            m_importedResolveTarget = resolvedVersion;
            displayTarget = resolvedVersion;
        }
    }

    if (!graphOutput.IsValid())
        graphOutput = displayTarget;

    if (displayTarget.IsValid()) {
        const bool preserveMsaaAttachment = m_sceneTarget->IsMsaaEnabled() && msaaSource.IsValid();
        m_renderGraph->AddPresentPass("__SceneOutputExport", [=](vk::PassBuilder &builder) {
            // Scene/Game targets are sampled by ImGui, capture and later view
            // consumers. The graph, not an out-of-band Vulkan barrier, owns
            // their published layout.
            builder.Read(displayTarget, rhi::PipelineStage::FragmentShader);
            if (graphOutput.IsValid() && graphOutput != displayTarget)
                builder.Read(graphOutput, rhi::PipelineStage::FragmentShader);
            if (preserveMsaaAttachment)
                builder.PrepareColorAttachment(msaaSource);
            if (m_importedDepthTarget.IsValid())
                builder.PrepareDepthStencilAttachment(m_importedDepthTarget);
            builder.SetSideEffect();
            return [](vk::RenderContext &) {};
        });
    }

    if (graphOutput.IsValid())
        m_renderGraph->SetOutput(graphOutput);
}

void SceneRenderGraph::BuildRenderGraph()
{
    if (!m_renderGraph || !m_sceneTarget || !m_vkCore) {
        INXLOG_WARN("SceneRenderGraph::BuildRenderGraph - Missing required components");
        return;
    }

    auto retiredDepthResolveGroups = m_sceneDepthResolver.TakeBindGroups();
    if (!retiredDepthResolveGroups.empty()) {
        rhi::Device *device = &m_vkCore->GetDeviceContext().GetRhiDevice();
        m_vkCore->GetRetirementQueue().Retire([device, groups = std::move(retiredDepthResolveGroups)]() mutable {
            for (const auto group : groups)
                device->Release(group);
        });
    }
    m_renderGraph->Reset();
    m_committedShadowContentSignature = 0;
    m_pendingShadowContentSignature = 0;
    m_shadowAtlasValid = false;
    m_shadowAtlasUpdateRequired = true;
    InvalidateTemporalHistory();
    m_renderView.color = {};
    m_renderView.depth = {};
    m_renderView.motion = {};
    m_renderView.history = {};
    // The renderer has waited only for the current frame slot. Clear both
    // geometry and particle views for that slot before retiring graph-owned
    // shadow images; the other slot is republished after its own fence.
    const uint32_t currentFrame = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
    auto &currentViewFrame = m_perViewFrames[currentFrame];
    if (currentViewFrame.GeometrySet() != VK_NULL_HANDLE)
        m_vkCore->ClearPerViewShadowMap(currentViewFrame.GeometrySet());
    if (currentViewFrame.ParticleSet() != VK_NULL_HANDLE)
        m_vkCore->ClearPerViewShadowMap(currentViewFrame.ParticleSet());
    currentViewFrame.shadowBinding = {};

    // Graph topology and material pipeline compatibility are independent.
    // Effect-stack edits commonly rebuild transient graph resources while the
    // attachment formats and sample count remain unchanged. Material pass
    // descriptors already key their cache by compile target, formats, samples
    // and render state; invalidating every material here discarded valid GPU
    // pipelines and made a runtime effect/pipeline switch compile the whole
    // scene again. True compatibility changes (currently MSAA publication)
    // are handled transactionally by MaterialPipelineManager itself.

    m_graphBuilt = false;
    m_shadowMapInputHandle = {};
    m_shadowMapInputIsDepth = false;
    m_visibleRendererList = {};
    m_shadowRendererList = {};

    const auto retireCuller = [this](std::shared_ptr<particle::ParticleGpuCuller> culler) {
        if (!culler)
            return;
        m_vkCore->GetRetirementQueue().Retire([culler = std::move(culler)]() mutable { culler.reset(); });
    };
    const auto retireSorter = [this](std::shared_ptr<particle::ParticleGpuSorter> sorter) {
        if (sorter)
            m_vkCore->GetRetirementQueue().Retire([sorter = std::move(sorter)]() mutable { sorter.reset(); });
    };
    const auto particleSnapshot =
        m_particleDrawRegistry
            ? m_particleDrawRegistry->Snapshot(std::numeric_limits<int32_t>::min(), std::numeric_limits<int32_t>::max())
            : std::vector<particle::GpuParticleDrawEntry>{};
    std::unordered_set<uint64_t> activeCullers;
    activeCullers.reserve(particleSnapshot.size());
    for (const auto &entry : particleSnapshot) {
        if (!entry.cullProgram || !entry.cullProgram->IsValid())
            continue;
        activeCullers.insert(entry.id);
        auto existing = m_particleCullers.find(entry.id);
        if (existing != m_particleCullers.end() && existing->second && existing->second->Capacity() == entry.capacity &&
            existing->second->VertexCount() == entry.renderer->VertexCount() &&
            existing->second->VisibilityBuffer() == entry.visibility &&
            existing->second->SourceIndirectBuffer() == entry.indirectArguments &&
            existing->second->SourceIndexBuffer() == entry.renderIndices &&
            existing->second->BoundsBuffer() == entry.bounds &&
            existing->second->SimulationControlBuffer() == entry.simulationControl &&
            existing->second->Mode() == entry.cullMode) {
            continue;
        }

        auto culler = std::make_shared<particle::ParticleGpuCuller>();
        particle::GpuParticleCullerDesc desc;
        desc.capacity = entry.capacity;
        desc.vertexCount = entry.renderer->VertexCount();
        desc.visibility = entry.visibility;
        desc.ribbonInstances = entry.instances;
        desc.sourceIndirectArguments = entry.indirectArguments;
        desc.sourceIndices = entry.renderIndices;
        desc.bounds = entry.bounds;
        desc.simulationControl = entry.simulationControl;
        desc.mode = entry.cullMode;
        desc.program = entry.cullProgram->View();
        if (!culler->Create(m_vkCore->GetDeviceContext().GetRhiDevice(), desc)) {
            INXLOG_ERROR("SceneRenderGraph: failed to create per-view culler for GPU particle output ", entry.id);
            activeCullers.erase(entry.id);
            continue;
        }
        if (existing != m_particleCullers.end()) {
            retireCuller(std::move(existing->second));
            existing->second = std::move(culler);
        } else {
            m_particleCullers.emplace(entry.id, std::move(culler));
        }
    }
    for (auto it = m_particleCullers.begin(); it != m_particleCullers.end();) {
        if (activeCullers.find(it->first) != activeCullers.end()) {
            ++it;
            continue;
        }
        retireCuller(std::move(it->second));
        it = m_particleCullers.erase(it);
    }

    std::unordered_set<uint64_t> activeSorters;
    activeSorters.reserve(particleSnapshot.size());
    for (const auto &entry : particleSnapshot) {
        if (entry.semantics.sortMode == particle::ParticleSortMode::None)
            continue;
        const auto cullerIt = m_particleCullers.find(entry.id);
        if (cullerIt == m_particleCullers.end() || !cullerIt->second || !cullerIt->second->IsValid()) {
            INXLOG_ERROR("SceneRenderGraph: sorted GPU particle output ", entry.id, " has no valid per-view culler");
            continue;
        }
        if (!entry.sortProgram || !entry.sortProgram->IsValid()) {
            INXLOG_ERROR("SceneRenderGraph: sorted GPU particle output ", entry.id, " has no valid sorting program");
            continue;
        }
        const auto &culler = cullerIt->second;
        activeSorters.insert(entry.id);
        auto existing = m_particleSorters.find(entry.id);
        if (existing != m_particleSorters.end() && existing->second && existing->second->Capacity() == entry.capacity &&
            existing->second->VisibilityBuffer() == entry.visibility &&
            existing->second->IndirectBuffer() == culler->DrawIndirectBuffer() &&
            existing->second->SourceIndexBuffer() == culler->VisibleIndexBuffer() &&
            existing->second->DispatchBuffer() == culler->SortDispatchBuffer()) {
            continue;
        }

        auto sorter = std::make_shared<particle::ParticleGpuSorter>();
        particle::GpuParticleSorterDesc desc;
        desc.capacity = entry.capacity;
        desc.visibility = entry.visibility;
        desc.indirectArguments = culler->DrawIndirectBuffer();
        desc.sourceIndices = culler->VisibleIndexBuffer();
        desc.dispatchArguments = culler->SortDispatchBuffer();
        desc.program = entry.sortProgram->View();
        if (!sorter->Create(m_vkCore->GetDeviceContext().GetRhiDevice(), desc)) {
            INXLOG_ERROR("SceneRenderGraph: failed to create per-view sorter for GPU particle output ", entry.id);
            activeSorters.erase(entry.id);
            continue;
        }
        if (existing != m_particleSorters.end()) {
            retireSorter(std::move(existing->second));
            existing->second = std::move(sorter);
        } else {
            m_particleSorters.emplace(entry.id, std::move(sorter));
        }
    }
    for (auto it = m_particleSorters.begin(); it != m_particleSorters.end();) {
        if (activeSorters.find(it->first) != activeSorters.end()) {
            ++it;
            continue;
        }
        retireSorter(std::move(it->second));
        it = m_particleSorters.erase(it);
    }

    if (!m_hasPythonGraph) {
        INXLOG_DEBUG("SceneRenderGraph::BuildRenderGraph - No Python graph configured");
        return;
    }

    ImportSceneTargetResources();
    m_visibleRendererList = m_renderGraph->ImportRendererList("VisibleRenderers", &m_cachedRenderers);
    m_shadowRendererList = m_renderGraph->ImportRendererList("ShadowRenderers", &m_cachedShadowRenderers);

    std::unordered_map<std::string, vk::ResourceHandle> customRTHandles;
    std::unordered_map<std::string, vk::ResourceHandle> bufferHandles;
    ImportTemporalHistoryResources(customRTHandles);
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

        // All camera graphs targeting this output import the same persistent
        // depth attachment. The first camera normally clears it; later
        // cameras may clear or preserve it through their clear flags.
        vk::ResourceHandle sharedDepth = m_importedDepthTarget;

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

        struct ParticleGraphResources
        {
            vk::ResourceHandle instances;
            vk::ResourceHandle visibility;
            vk::ResourceHandle sourceIndirectArguments;
            vk::ResourceHandle sourceRenderIndices;
            vk::ResourceHandle bounds;
            vk::ResourceHandle simulationControl;
            vk::ResourceHandle renderIndices;
            vk::ResourceHandle indirectArguments;
            vk::ResourceHandle sortDispatchArguments;
            std::array<vk::ResourceHandle, 2> keys;
            std::array<vk::ResourceHandle, 2> indices;
            vk::ResourceHandle histogram;
            vk::ResourceHandle blockOffsets;
            vk::ResourceHandle globalOffsets;
            rhi::BufferHandle drawRenderIndices;
        };
        std::unordered_map<uint64_t, ParticleGraphResources> particleGraphResources;
        particleGraphResources.reserve(m_particleCullers.size());
        for (const auto &entry : particleSnapshot) {
            const auto cullerIt = m_particleCullers.find(entry.id);
            if (cullerIt == m_particleCullers.end() || !cullerIt->second || !cullerIt->second->IsValid())
                continue;

            auto culler = cullerIt->second;
            ParticleGraphResources resources;
            resources.drawRenderIndices = culler->VisibleIndexBuffer();
            const std::string prefix = "GpuParticleCull/" + std::to_string(entry.id);
            m_renderGraph->AddComputePass(prefix + "/Reset", [&, culler, entry, prefix](vk::PassBuilder &builder) {
                const uint64_t elementBytes = static_cast<uint64_t>(entry.capacity) * sizeof(uint32_t);
                resources.instances = builder.ImportBuffer(prefix + "/Instances", entry.instances,
                                                           static_cast<uint64_t>(entry.capacity) *
                                                               particle::ParticleGpuRuntime::RenderInstanceStride);
                resources.visibility = builder.ImportBuffer(prefix + "/Visibility", entry.visibility,
                                                            static_cast<uint64_t>(entry.capacity) *
                                                                particle::ParticleGpuRuntime::VisibilityInstanceStride);
                resources.sourceIndirectArguments =
                    builder.ImportBuffer(prefix + "/SourceIndirect", entry.indirectArguments, 16);
                resources.sourceRenderIndices =
                    builder.ImportBuffer(prefix + "/SourceIndices", entry.renderIndices, elementBytes);
                resources.bounds = builder.ImportBuffer(prefix + "/Bounds", entry.bounds,
                                                        particle::ParticleGpuBounds::BoundsBufferBytes);
                resources.simulationControl =
                    builder.ImportBuffer(prefix + "/SimulationControl", entry.simulationControl,
                                         sizeof(particle::GpuParticleSimulationControl));
                resources.renderIndices =
                    builder.ImportBuffer(prefix + "/VisibleIndices", culler->VisibleIndexBuffer(), elementBytes);
                resources.indirectArguments =
                    builder.ImportBuffer(prefix + "/DrawIndirect", culler->DrawIndirectBuffer(), 16);
                resources.sortDispatchArguments =
                    builder.ImportBuffer(prefix + "/SortDispatch", culler->SortDispatchBuffer(),
                                         sizeof(particle::GpuParticleCullDispatchState));

                m_renderGraph->SetResourceInitialState(resources.instances, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader,
                                                       rhi::QueueRole::Compute);
                m_renderGraph->SetResourceInitialState(resources.visibility, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderRead, rhi::PipelineStage::ComputeShader,
                                                       rhi::QueueRole::Compute);
                m_renderGraph->SetResourceInitialState(resources.sourceIndirectArguments, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader,
                                                       rhi::QueueRole::Compute);
                m_renderGraph->SetResourceInitialState(resources.sourceRenderIndices, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader,
                                                       rhi::QueueRole::Compute);
                m_renderGraph->SetResourceInitialState(resources.bounds, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader,
                                                       rhi::QueueRole::Compute);
                m_renderGraph->SetResourceInitialState(resources.simulationControl, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader,
                                                       rhi::QueueRole::Compute);
                const auto visibleIndexLastStage = entry.semantics.sortMode == particle::ParticleSortMode::None
                                                       ? rhi::PipelineStage::VertexShader
                                                       : rhi::PipelineStage::ComputeShader;
                const auto visibleIndexLastQueue = entry.semantics.sortMode == particle::ParticleSortMode::None
                                                       ? rhi::QueueRole::Graphics
                                                       : rhi::QueueRole::Compute;
                m_renderGraph->SetResourceInitialState(resources.renderIndices, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderRead, visibleIndexLastStage,
                                                       visibleIndexLastQueue);
                m_renderGraph->SetResourceInitialState(resources.indirectArguments, rhi::TextureLayout::Undefined,
                                                       rhi::Access::IndirectRead, rhi::PipelineStage::DrawIndirect,
                                                       rhi::QueueRole::Graphics);
                const auto sortDispatchLastAccess = entry.semantics.sortMode == particle::ParticleSortMode::None
                                                        ? rhi::Access::ShaderWrite
                                                        : rhi::Access::IndirectRead;
                const auto sortDispatchLastStage = entry.semantics.sortMode == particle::ParticleSortMode::None
                                                       ? rhi::PipelineStage::ComputeShader
                                                       : rhi::PipelineStage::DrawIndirect;
                m_renderGraph->SetResourceInitialState(resources.sortDispatchArguments, rhi::TextureLayout::Undefined,
                                                       sortDispatchLastAccess, sortDispatchLastStage,
                                                       rhi::QueueRole::Compute);
                builder.ReadStorageBuffer(resources.sourceIndirectArguments);
                builder.ReadStorageBuffer(resources.bounds);
                resources.simulationControl =
                    builder.ReadWrite(resources.simulationControl, rhi::PipelineStage::ComputeShader);
                resources.indirectArguments = builder.WriteStorageBuffer(resources.indirectArguments);
                resources.sortDispatchArguments = builder.WriteStorageBuffer(resources.sortDispatchArguments);
                return [this, culler](vk::RenderContext &ctx) {
                    culler->RecordReset(ctx.GetComputeCommandEncoder(), m_particleFrustumPlanes);
                    ctx.RecordComputeDispatch(1, 1, 1, culler->Capacity(), false);
                };
            });
            m_renderGraph->AddComputePass(prefix + "/Cull", [&, culler](vk::PassBuilder &builder) {
                builder.ReadStorageBuffer(resources.visibility);
                builder.ReadStorageBuffer(resources.sourceIndirectArguments);
                builder.ReadStorageBuffer(resources.sourceRenderIndices);
                builder.ReadIndirectBuffer(resources.sortDispatchArguments);
                resources.renderIndices = builder.WriteStorageBuffer(resources.renderIndices);
                resources.indirectArguments =
                    builder.ReadWrite(resources.indirectArguments, rhi::PipelineStage::ComputeShader);
                return [this, culler](vk::RenderContext &ctx) {
                    culler->RecordCull(ctx.GetComputeCommandEncoder(), m_particleFrustumPlanes);
                    ctx.RecordComputeDispatch(0, 0, 0, culler->Capacity(), true);
                };
            });
            m_renderGraph->AddComputePass(prefix + "/Finalize", [&, culler](vk::PassBuilder &builder) {
                resources.indirectArguments =
                    builder.ReadWrite(resources.indirectArguments, rhi::PipelineStage::ComputeShader);
                resources.sortDispatchArguments = builder.WriteStorageBuffer(resources.sortDispatchArguments);
                return [culler](vk::RenderContext &ctx) {
                    culler->RecordFinalize(ctx.GetComputeCommandEncoder());
                    ctx.RecordComputeDispatch(1, 1, 1, culler->Capacity(), false);
                };
            });
            if (resources.instances.IsValid() && resources.visibility.IsValid() && resources.bounds.IsValid() &&
                resources.simulationControl.IsValid() && resources.renderIndices.IsValid() &&
                resources.indirectArguments.IsValid() && resources.sortDispatchArguments.IsValid()) {
                particleGraphResources.emplace(entry.id, resources);
            }
        }

        for (const auto &entry : particleSnapshot) {
            if (entry.semantics.sortMode == particle::ParticleSortMode::None)
                continue;
            auto resourcesIt = particleGraphResources.find(entry.id);
            if (resourcesIt == particleGraphResources.end())
                continue;
            const auto sorterIt = m_particleSorters.find(entry.id);
            if (sorterIt == m_particleSorters.end() || !sorterIt->second || !sorterIt->second->IsValid())
                continue;

            auto sorter = sorterIt->second;
            auto &resources = resourcesIt->second;
            const std::string prefix = "GpuParticleSort/" + std::to_string(entry.id);
            if (sorter->UsesSmallSort()) {
                m_renderGraph->AddComputePass(prefix + "/Small", [&, sorter, entry, prefix](vk::PassBuilder &builder) {
                    const uint64_t elementBytes = static_cast<uint64_t>(entry.capacity) * sizeof(uint32_t);
                    resources.indices[0] =
                        builder.ImportBuffer(prefix + "/Indices0", sorter->IndexBuffer(0), elementBytes);
                    m_renderGraph->SetResourceInitialState(resources.indices[0], rhi::TextureLayout::Undefined,
                                                           rhi::Access::ShaderRead, rhi::PipelineStage::VertexShader);

                    builder.ReadStorageBuffer(resources.visibility);
                    builder.ReadStorageBuffer(resources.indirectArguments);
                    builder.ReadStorageBuffer(resources.renderIndices);
                    // Finalize publishes the current visible count and this
                    // indirect token. Keep the direct small-sort dispatch
                    // ordered after it just like the radix path.
                    builder.ReadIndirectBuffer(resources.sortDispatchArguments);
                    resources.indices[0] = builder.WriteStorageBuffer(resources.indices[0]);

                    const auto mode = entry.semantics.sortMode;
                    return [this, sorter, mode](vk::RenderContext &ctx) {
                        std::array<float, 16> view{};
                        std::memcpy(view.data(), &m_cachedView[0][0], sizeof(m_cachedView));
                        sorter->RecordSmall(ctx.GetComputeCommandEncoder(), view, mode);
                        ctx.RecordComputeDispatch(1, 1, 1, sorter->Capacity(), false);
                    };
                });
                resources.renderIndices = resources.indices[0];
                resources.drawRenderIndices = sorter->SortedIndices();
                continue;
            }
            m_renderGraph->AddComputePass(prefix + "/Generate", [&, sorter, entry, prefix](vk::PassBuilder &builder) {
                const uint64_t elementBytes = static_cast<uint64_t>(entry.capacity) * sizeof(uint32_t);
                const uint64_t keyBytes =
                    static_cast<uint64_t>(entry.capacity) * particle::ParticleGpuSorter::PackedKeyStride;
                const uint64_t blockBytes =
                    static_cast<uint64_t>(sorter->BlockCount()) * particle::ParticleGpuSorter::Radix * sizeof(uint32_t);
                resources.keys = {
                    builder.ImportBuffer(prefix + "/Keys0", sorter->KeyBuffer(0), keyBytes),
                    builder.ImportBuffer(prefix + "/Keys1", sorter->KeyBuffer(1), keyBytes),
                };
                resources.indices = {
                    builder.ImportBuffer(prefix + "/Indices0", sorter->IndexBuffer(0), elementBytes),
                    builder.ImportBuffer(prefix + "/Indices1", sorter->IndexBuffer(1), elementBytes),
                };
                resources.histogram =
                    builder.ImportBuffer(prefix + "/Histogram", sorter->HistogramBuffer(), blockBytes);
                resources.blockOffsets =
                    builder.ImportBuffer(prefix + "/BlockOffsets", sorter->BlockOffsetBuffer(), blockBytes);
                resources.globalOffsets = builder.ImportBuffer(prefix + "/GlobalOffsets", sorter->GlobalOffsetBuffer(),
                                                               particle::ParticleGpuSorter::Radix * sizeof(uint32_t));

                m_renderGraph->SetResourceInitialState(resources.keys[0], rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                m_renderGraph->SetResourceInitialState(resources.keys[1], rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderRead, rhi::PipelineStage::ComputeShader);
                m_renderGraph->SetResourceInitialState(resources.indices[0], rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderRead, rhi::PipelineStage::VertexShader);
                m_renderGraph->SetResourceInitialState(resources.indices[1], rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderRead, rhi::PipelineStage::ComputeShader);
                m_renderGraph->SetResourceInitialState(resources.histogram, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderRead, rhi::PipelineStage::ComputeShader);
                m_renderGraph->SetResourceInitialState(resources.blockOffsets, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderRead, rhi::PipelineStage::ComputeShader);
                m_renderGraph->SetResourceInitialState(resources.globalOffsets, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderRead, rhi::PipelineStage::ComputeShader);

                builder.ReadStorageBuffer(resources.visibility);
                builder.ReadStorageBuffer(resources.indirectArguments);
                builder.ReadStorageBuffer(resources.renderIndices);
                builder.ReadIndirectBuffer(resources.sortDispatchArguments);
                resources.keys[0] = builder.WriteStorageBuffer(resources.keys[0]);
                resources.indices[0] = builder.WriteStorageBuffer(resources.indices[0]);

                const auto mode = entry.semantics.sortMode;
                return [this, sorter, mode](vk::RenderContext &ctx) {
                    std::array<float, 16> view{};
                    std::memcpy(view.data(), &m_cachedView[0][0], sizeof(m_cachedView));
                    sorter->RecordGenerate(ctx.GetComputeCommandEncoder(), view, mode);
                    ctx.RecordComputeDispatch(0, 0, 0, sorter->Capacity(), true);
                };
            });
            for (uint32_t passIndex = 0; passIndex < particle::ParticleGpuSorter::PassCount; ++passIndex) {
                const uint32_t input = passIndex % 2u;
                const uint32_t output = 1u - input;
                const std::string radixPrefix = prefix + "/Radix" + std::to_string(passIndex);
                m_renderGraph->AddComputePass(
                    radixPrefix + "/Histogram", [&, sorter, passIndex, input](vk::PassBuilder &builder) {
                        builder.ReadStorageBuffer(resources.indirectArguments);
                        builder.ReadStorageBuffer(resources.keys[input]);
                        builder.ReadIndirectBuffer(resources.sortDispatchArguments);
                        resources.histogram = builder.WriteStorageBuffer(resources.histogram);
                        return [sorter, passIndex](vk::RenderContext &ctx) {
                            sorter->RecordHistogram(ctx.GetComputeCommandEncoder(), passIndex);
                            ctx.RecordComputeDispatch(0, 0, 0, sorter->Capacity(), true);
                        };
                    });
                m_renderGraph->AddComputePass(radixPrefix + "/Scan", [&, sorter, passIndex](vk::PassBuilder &builder) {
                    builder.ReadStorageBuffer(resources.histogram);
                    builder.ReadStorageBuffer(resources.sortDispatchArguments);
                    resources.blockOffsets = builder.WriteStorageBuffer(resources.blockOffsets);
                    resources.globalOffsets = builder.WriteStorageBuffer(resources.globalOffsets);
                    return [sorter, passIndex](vk::RenderContext &ctx) {
                        sorter->RecordScan(ctx.GetComputeCommandEncoder(), passIndex);
                        ctx.RecordComputeDispatch(1, 1, 1, sorter->BlockCount(), false);
                    };
                });
                m_renderGraph->AddComputePass(
                    radixPrefix + "/Scatter", [&, sorter, passIndex, input, output](vk::PassBuilder &builder) {
                        builder.ReadStorageBuffer(resources.indirectArguments);
                        builder.ReadStorageBuffer(resources.keys[input]);
                        builder.ReadStorageBuffer(resources.indices[input]);
                        builder.ReadStorageBuffer(resources.blockOffsets);
                        builder.ReadStorageBuffer(resources.globalOffsets);
                        builder.ReadIndirectBuffer(resources.sortDispatchArguments);
                        resources.keys[output] = builder.WriteStorageBuffer(resources.keys[output]);
                        resources.indices[output] = builder.WriteStorageBuffer(resources.indices[output]);
                        return [sorter, passIndex](vk::RenderContext &ctx) {
                            sorter->RecordScatter(ctx.GetComputeCommandEncoder(), passIndex);
                            ctx.RecordComputeDispatch(0, 0, 0, sorter->Capacity(), true);
                        };
                    });
            }
            resources.renderIndices = resources.indices[0];
            resources.drawRenderIndices = sorter->SortedIndices();
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
        uint32_t depthResolvePassCounter = 0;
        vk::ResourceHandle resolvedParticleSceneDepth;
        vk::ResourceHandle resolvedParticleDepthSource;

        struct ForwardPlusGraphResources
        {
            vk::ResourceHandle canonicalLights;
            vk::ResourceHandle headers;
            vk::ResourceHandle lightMasks;
        };
        std::array<ForwardPlusGraphResources, kMaxFramesInFlight> forwardPlusResources{};
        std::array<ForwardPlusGraphResources, kMaxFramesInFlight> particleForwardPlusResources{};
        if (UsesForwardPlus()) {
            for (uint32_t frameIndex = 0; frameIndex < kMaxFramesInFlight; ++frameIndex) {
                const auto &lights = m_cameraCanonicalLights.Frame(frameIndex);
                const auto &gridFrame = m_forwardPlusGeometryGrid.Frame(frameIndex);
                if (!lights.buffer.IsValid() || !gridFrame.config.IsValid())
                    continue;
                const std::string prefix = "ForwardPlus/Geometry/Frame" + std::to_string(frameIndex);
                m_renderGraph->AddComputePass(prefix + "/Build", [&, frameIndex, prefix](vk::PassBuilder &builder) {
                    auto &resources = forwardPlusResources[frameIndex];
                    resources.canonicalLights =
                        builder.ImportBuffer(prefix + "/CanonicalLights", lights.buffer, lights.capacityBytes);
                    resources.headers =
                        builder.ImportBuffer(prefix + "/TileHeaders", gridFrame.headers, gridFrame.headerCapacityBytes);
                    resources.lightMasks = builder.ImportBuffer(prefix + "/TileLightMasks", gridFrame.lightMasks,
                                                                gridFrame.maskCapacityBytes);
                    m_renderGraph->SetResourceInitialState(resources.canonicalLights, rhi::TextureLayout::Undefined,
                                                           rhi::Access::HostWrite, rhi::PipelineStage::Host);
                    m_renderGraph->SetResourceInitialState(resources.headers, rhi::TextureLayout::Undefined,
                                                           rhi::Access::ShaderRead, rhi::PipelineStage::FragmentShader);
                    m_renderGraph->SetResourceInitialState(resources.lightMasks, rhi::TextureLayout::Undefined,
                                                           rhi::Access::ShaderRead, rhi::PipelineStage::FragmentShader);
                    builder.ReadStorageBuffer(resources.canonicalLights);
                    resources.headers = builder.WriteStorageBuffer(resources.headers);
                    resources.lightMasks = builder.WriteStorageBuffer(resources.lightMasks);
                    return [this, frameIndex](vk::RenderContext &ctx) {
                        const uint32_t activeFrame = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
                        if (activeFrame != frameIndex)
                            return;
                        lighting::ForwardPlusGridConstants constants{};
                        const glm::mat4 viewProjection = m_cachedProj * m_cachedView;
                        std::memcpy(constants.viewProjection, &viewProjection[0][0], sizeof(viewProjection));
                        constants.viewportAndProjectionScale[2] = std::abs(m_cachedProj[0][0]);
                        constants.viewportAndProjectionScale[3] = std::abs(m_cachedProj[1][1]);
                        m_forwardPlusGeometryGrid.Record(frameIndex, ctx.GetComputeCommandEncoder(), constants);
                    };
                });
            }
            if (m_forwardPlusParticlesRequired) {
                for (uint32_t frameIndex = 0; frameIndex < kMaxFramesInFlight; ++frameIndex) {
                    const auto &lights = m_cameraCanonicalLights.Frame(frameIndex);
                    const auto &gridFrame = m_forwardPlusParticleGrid.Frame(frameIndex);
                    if (!lights.buffer.IsValid() || !gridFrame.config.IsValid())
                        continue;
                    const std::string prefix = "ForwardPlus/Particles/Frame" + std::to_string(frameIndex);
                    m_renderGraph->AddComputePass(prefix + "/Build", [&, frameIndex, prefix](vk::PassBuilder &builder) {
                        auto &resources = particleForwardPlusResources[frameIndex];
                        resources.canonicalLights =
                            builder.ImportBuffer(prefix + "/CanonicalLights", lights.buffer, lights.capacityBytes);
                        resources.headers = builder.ImportBuffer(prefix + "/TileHeaders", gridFrame.headers,
                                                                 gridFrame.headerCapacityBytes);
                        resources.lightMasks = builder.ImportBuffer(prefix + "/TileLightMasks", gridFrame.lightMasks,
                                                                    gridFrame.maskCapacityBytes);
                        m_renderGraph->SetResourceInitialState(resources.canonicalLights, rhi::TextureLayout::Undefined,
                                                               rhi::Access::HostWrite, rhi::PipelineStage::Host);
                        m_renderGraph->SetResourceInitialState(resources.headers, rhi::TextureLayout::Undefined,
                                                               rhi::Access::ShaderRead,
                                                               rhi::PipelineStage::FragmentShader);
                        m_renderGraph->SetResourceInitialState(resources.lightMasks, rhi::TextureLayout::Undefined,
                                                               rhi::Access::ShaderRead,
                                                               rhi::PipelineStage::FragmentShader);
                        builder.ReadStorageBuffer(resources.canonicalLights);
                        resources.headers = builder.WriteStorageBuffer(resources.headers);
                        resources.lightMasks = builder.WriteStorageBuffer(resources.lightMasks);
                        return [this, frameIndex](vk::RenderContext &ctx) {
                            const uint32_t activeFrame = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
                            if (activeFrame != frameIndex)
                                return;
                            lighting::ForwardPlusGridConstants constants{};
                            const glm::mat4 viewProjection = m_cachedProj * m_cachedView;
                            std::memcpy(constants.viewProjection, &viewProjection[0][0], sizeof(viewProjection));
                            constants.viewportAndProjectionScale[2] = std::abs(m_cachedProj[0][0]);
                            constants.viewportAndProjectionScale[3] = std::abs(m_cachedProj[1][1]);
                            m_forwardPlusParticleGrid.Record(frameIndex, ctx.GetComputeCommandEncoder(), constants);
                        };
                    });
                }
            }
        }

        // Fullscreen lighting shaders consume the camera-local shadow map
        // through the per-view descriptor set rather than their explicit
        // set-0 texture list. Discover that graph resource up front so those
        // passes still participate in dependency and layout tracking.
        std::string perViewShadowTextureName;
        for (const auto &candidatePass : sortedPasses) {
            const GraphCommandDesc *candidateCommand = PrimaryCommand(candidatePass);
            if (!candidateCommand)
                continue;
            const auto shadowBinding =
                std::find_if(candidateCommand->inputBindings.begin(), candidateCommand->inputBindings.end(),
                             [](const auto &binding) { return binding.first == "shadowMap"; });
            if (shadowBinding != candidateCommand->inputBindings.end()) {
                perViewShadowTextureName = shadowBinding->second;
                break;
            }
        }
        bool fullscreenShadowDependencyDeclared = false;
        // Camera clear settings remain active across frames. Consumption is
        // local to this graph build so only the first color-clearing pass is
        // overridden without disabling later per-frame color updates.
        bool cameraClearOverridePending = m_hasCameraClearOverride;
        m_mainClearPassName.clear();

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
            const bool particleSurfacePass = command && command->type == GraphCommandType::DrawRenderers &&
                                             (command->shaderTarget == ShaderCompileTarget::Forward ||
                                              command->shaderTarget == ShaderCompileTarget::ForwardPlus ||
                                              command->shaderTarget == ShaderCompileTarget::Motion);
            const bool particleShadowPass = command && command->type == GraphCommandType::DrawShadowCasters &&
                                            command->shaderTarget == ShaderCompileTarget::Shadow;
            if (m_particleDrawRegistry && (particleSurfacePass || particleShadowPass)) {
                particleEntries = m_particleDrawRegistry->Snapshot(command->queueMin, command->queueMax);
                const auto particlePassIt = m_pythonMaterialPasses.find(passDesc.name);
                if (particlePassIt != m_pythonMaterialPasses.end())
                    particlePass = particlePassIt->second;
                else
                    particleEntries.clear();
                particleEntries.erase(
                    std::remove_if(particleEntries.begin(), particleEntries.end(),
                                   [&](const auto &entry) {
                                       if (particleShadowPass)
                                           return !entry.semantics.castShadows || !entry.renderer->CanCastShadows();
                                       return entry.semantics.sortMode != particle::ParticleSortMode::None &&
                                              particleGraphResources.find(entry.id) == particleGraphResources.end();
                                   }),
                    particleEntries.end());
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
            vk::ResourceHandle declaredReadOnlyDepth;
            for (const auto &readTex : passDesc.readTextures) {
                auto texIt = texDescMap.find(readTex);
                if (texIt != texDescMap.end()) {
                    if (texIt->second->isDepth) {
                        const bool sampledInput =
                            std::any_of(commandInputBindings.begin(), commandInputBindings.end(),
                                        [&readTex](const auto &binding) { return binding.second == readTex; });
                        if (!sampledInput) {
                            readsDepth = true;
                            const auto depthIt = customRTHandles.find(readTex);
                            if (depthIt != customRTHandles.end())
                                declaredReadOnlyDepth = depthIt->second;
                        }
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
            if (cameraClearOverridePending && passDesc.clearColor) {
                switch (m_cameraClearFlags) {
                case CameraClearFlags::Skybox:
                    clearColor = true;
                    clearDepth = true;
                    // Preserve the graph-authored clear color. Declarative
                    // pipelines use transparent scene accumulators and place
                    // the sky underneath them later; forcing opaque black here
                    // makes the real sky impossible to composite back in.
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
                cameraClearOverridePending = false;
            }

            // Capture depth state for the lambda (by value — sharedDepth
            // is updated between iterations so we capture the CURRENT value)
            vk::ResourceHandle depthForThisPass = declaredReadOnlyDepth.IsValid() ? declaredReadOnlyDepth : sharedDepth;
            bool needsCreateDepth = writesDepth && !sharedDepth.IsValid();
            bool passReadsDepth = readsDepth && !writesDepth;
            vk::ResourceHandle particleSceneDepth;
            bool particleSceneDepthIsDepth = true;
            const bool hasSoftParticleOutputs =
                std::any_of(particleEntries.begin(), particleEntries.end(),
                            [](const auto &entry) { return entry.semantics.softParticles; });
            if (hasSoftParticleOutputs) {
                const bool hasDepthContract = passReadsDepth && depthForThisPass.IsValid() &&
                                              particlePass.depthFormat != rhi::PixelFormat::Undefined;
                if (hasDepthContract && msaaSamples == VK_SAMPLE_COUNT_1_BIT) {
                    particleSceneDepth = depthForThisPass;
                } else if (hasDepthContract && m_sceneDepthResolver.IsValid()) {
                    if (!resolvedParticleSceneDepth.IsValid() || resolvedParticleDepthSource != depthForThisPass) {
                        const auto sourceDepth = depthForThisPass;
                        const std::string resolvePassName =
                            "_SceneDepthResolve/" + std::to_string(depthResolvePassCounter++);
                        m_renderGraph->AddComputePass(resolvePassName, [&, sourceDepth](vk::PassBuilder &builder) {
                            builder.ReadSampledDepth(sourceDepth, rhi::PipelineStage::ComputeShader);
                            auto target = builder.CreateTexture("SceneDepthResolved", width, height,
                                                                VK_FORMAT_R32_SFLOAT, VK_SAMPLE_COUNT_1_BIT);
                            resolvedParticleSceneDepth = builder.WriteStorageTexture(target);
                            const auto outputDepth = resolvedParticleSceneDepth;
                            return [this, sourceDepth, outputDepth, width, height,
                                    sampleCount = static_cast<uint32_t>(msaaSamples)](vk::RenderContext &ctx) {
                                const bool recorded = m_sceneDepthResolver.Record(
                                    ctx.GetComputeCommandEncoder(), ctx.GetTextureView(sourceDepth),
                                    ctx.GetTextureView(outputDepth), width, height, sampleCount);
                                if (!recorded)
                                    INXLOG_ERROR("SceneRenderGraph: failed to record the MSAA scene-depth resolve");
                            };
                        });
                        resolvedParticleDepthSource = sourceDepth;
                    }
                    particleSceneDepth = resolvedParticleSceneDepth;
                    particleSceneDepthIsDepth = false;
                } else {
                    if (hasDepthContract && msaaSamples != VK_SAMPLE_COUNT_1_BIT && !m_sceneDepthResolver.IsValid()) {
                        INXLOG_ERROR("SceneRenderGraph: soft particle depth resolve is unavailable for pass '",
                                     passDesc.name, "'");
                    }
                    // A queue range may deliberately overlap multiple surface
                    // passes. Soft particles can only run after opaque depth is
                    // complete, so an earlier pass is an ineligible route, not
                    // a graph failure. Leave the entries for a later pass whose
                    // contract reads the scene depth.
                    particleEntries.erase(
                        std::remove_if(particleEntries.begin(), particleEntries.end(),
                                       [](const auto &entry) { return entry.semantics.softParticles; }),
                        particleEntries.end());
                }
            }

            vk::ResourceHandle resolveTarget;
            if (!passDesc.resolveColor.empty()) {
                const auto resolveIt = customRTHandles.find(passDesc.resolveColor);
                if (resolveIt != customRTHandles.end())
                    resolveTarget = resolveIt->second;
            }

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
                        uint32_t resolveW = width;
                        uint32_t resolveH = height;
                        std::string resolvePassName =
                            "__MSAA_resolve_pre_fs_" + std::to_string(msaaResolvePassCounter++);
                        vk::ResourceHandle resolvedVersion;

                        m_renderGraph->AddTransferPass(resolvePassName, [importedColor, importedResolve, resolveW,
                                                                         resolveH,
                                                                         &resolvedVersion](vk::PassBuilder &builder) {
                            builder.SetQueueRole(rhi::QueueRole::Graphics);
                            builder.TransferRead(importedColor);
                            resolvedVersion = builder.TransferWrite(importedResolve);
                            builder.SetRenderArea(resolveW, resolveH);

                            return [importedColor, resolvedVersion, resolveW, resolveH](vk::RenderContext &ctx) {
                                ctx.GetTransferCommandEncoder().ResolveTexture(
                                    ctx.GetTextureHandle(importedColor), ctx.GetTextureHandle(resolvedVersion),
                                    {rhi::TextureAspect::Color, 0, 0, 0, 0, resolveW, resolveH, 1});
                            };
                        });
                        publishResourceVersion(resolvedVersion);
                        backbufferDirtySinceResolve = false;
                    }
                }

                vk::ResourceHandle fullscreenResolvedDepth;
                bool sampledDepthResolveUnavailable = false;
                if (msaaSamples > VK_SAMPLE_COUNT_1_BIT) {
                    for (const auto &[samplerName, textureName] : commandInputBindings) {
                        (void)samplerName;
                        const auto texture = texDescMap.find(textureName);
                        if (texture == texDescMap.end() || !texture->second->isDepth)
                            continue;
                        const auto source = customRTHandles.find(textureName);
                        if (source == customRTHandles.end() || !source->second.IsValid())
                            continue;
                        if (!m_sceneDepthResolver.IsValid()) {
                            INXLOG_ERROR("SceneRenderGraph: sampled MSAA depth resolve is unavailable for pass '",
                                         passDesc.name, "'");
                            sampledDepthResolveUnavailable = true;
                            break;
                        }
                        if (!resolvedParticleSceneDepth.IsValid() || resolvedParticleDepthSource != source->second) {
                            const auto sourceDepth = source->second;
                            const std::string resolvePassName =
                                "_SceneDepthResolve/" + std::to_string(depthResolvePassCounter++);
                            m_renderGraph->AddComputePass(resolvePassName, [&, sourceDepth](vk::PassBuilder &builder) {
                                builder.ReadSampledDepth(sourceDepth, rhi::PipelineStage::ComputeShader);
                                auto target = builder.CreateTexture("SceneDepthResolved", width, height,
                                                                    VK_FORMAT_R32_SFLOAT, VK_SAMPLE_COUNT_1_BIT);
                                resolvedParticleSceneDepth = builder.WriteStorageTexture(target);
                                const auto outputDepth = resolvedParticleSceneDepth;
                                return [this, sourceDepth, outputDepth, width, height,
                                        sampleCount = static_cast<uint32_t>(msaaSamples)](vk::RenderContext &ctx) {
                                    if (!m_sceneDepthResolver.Record(
                                            ctx.GetComputeCommandEncoder(), ctx.GetTextureView(sourceDepth),
                                            ctx.GetTextureView(outputDepth), width, height, sampleCount)) {
                                        INXLOG_ERROR("SceneRenderGraph: failed to record the MSAA scene-depth resolve");
                                    }
                                };
                            });
                            resolvedParticleDepthSource = sourceDepth;
                        }
                        fullscreenResolvedDepth = resolvedParticleSceneDepth;
                        break;
                    }
                }
                if (sampledDepthResolveUnavailable)
                    continue;

                // Capture references for the execute lambda
                FullscreenRenderer *fsRenderer = &m_fullscreenRenderer;
                vk::RenderGraph *renderGraphPtr = m_renderGraph.get();
                std::string shaderName = command->shaderName;
                std::string parameterBlock = command->parameterBlock;
                FullscreenPushConstants packedPushConstants{};
                uint32_t packedPushConstantSize = 0;
                int32_t historyValidParameterIndex = -1;
                int32_t cameraDitheringParameterIndex = -1;
                int32_t cameraStopNaNsParameterIndex = -1;
                for (const auto &[name, value] : command->pushConstants) {
                    if (packedPushConstantSize / sizeof(float) < 32) {
                        const auto parameterIndex = static_cast<int32_t>(packedPushConstantSize / sizeof(float));
                        if (name == "_InfernuxHistoryValid")
                            historyValidParameterIndex = parameterIndex;
                        if (shaderName == "Display Encode" && name == "dithering")
                            cameraDitheringParameterIndex = parameterIndex;
                        if (shaderName == "Display Encode" && name == "stopNaNs")
                            cameraStopNaNsParameterIndex = parameterIndex;
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
                struct FullscreenReadResource
                {
                    vk::ResourceHandle handle;
                    rhi::PixelFormat format = rhi::PixelFormat::Undefined;
                    bool depthRead = false;
                };
                std::vector<FullscreenReadResource> fsReadInputs;
                std::string temporalHistoryKey;
                if (!commandInputBindings.empty()) {
                    // Use inputBindings order for deterministic sampler→binding mapping
                    for (const auto &[samplerName, textureName] : commandInputBindings) {
                        auto texIt = texDescMap.find(textureName);
                        if (texIt == texDescMap.end())
                            continue;
                        if (texIt->second->role == GraphTextureRole::TemporalRead)
                            temporalHistoryKey = texIt->second->temporalKey;
                        if (texIt->second->isBackbuffer) {
                            if (msaaSamples > VK_SAMPLE_COUNT_1_BIT && m_importedResolveTarget.IsValid()) {
                                fsReadInputs.push_back({m_importedResolveTarget,
                                                        rhi::FromVkFormat(m_sceneTarget->GetColorFormat()), false});
                            } else {
                                fsReadInputs.push_back(
                                    {m_importedColorTarget, rhi::FromVkFormat(m_sceneTarget->GetColorFormat()), false});
                            }
                        } else if (texIt->second->isDepth && fullscreenResolvedDepth.IsValid()) {
                            fsReadInputs.push_back({fullscreenResolvedDepth, rhi::PixelFormat::R32SFloat, false});
                        } else {
                            // Allow both color and depth textures as sampler inputs
                            // for fullscreen effects (e.g. SSAO reads depth as sampler2D).
                            // Always use Read() (→ SHADER_READ_ONLY_OPTIMAL) since these
                            // are sampled textures, NOT depth attachments.  Shadow maps
                            // and other depth-formatted textures are read with a regular
                            // combined-image-sampler descriptor, not as depth attachments.
                            auto rtIt = customRTHandles.find(textureName);
                            if (rtIt != customRTHandles.end()) {
                                fsReadInputs.push_back({rtIt->second, texIt->second->format, texIt->second->isDepth});
                            }
                        }
                    }
                } else {
                    // Default path: use readTextures order (colorReadHandles + backbuffer)
                    // for simple single-input effects that call read() without explicit inputBindings.
                    for (const auto &readTex : passDesc.readTextures) {
                        const auto texIt = texDescMap.find(readTex);
                        if (texIt == texDescMap.end() || texIt->second->isDepth || texIt->second->isBackbuffer)
                            continue;
                        const auto rtIt = customRTHandles.find(readTex);
                        if (rtIt != customRTHandles.end())
                            fsReadInputs.push_back({rtIt->second, texIt->second->format, false});
                    }
                    for (const auto &readTex : passDesc.readTextures) {
                        auto texIt = texDescMap.find(readTex);
                        if (texIt != texDescMap.end() && texIt->second->isBackbuffer) {
                            if (msaaSamples > VK_SAMPLE_COUNT_1_BIT && m_importedResolveTarget.IsValid()) {
                                fsReadInputs.push_back({m_importedResolveTarget,
                                                        rhi::FromVkFormat(m_sceneTarget->GetColorFormat()), false});
                            } else {
                                fsReadInputs.push_back(
                                    {m_importedColorTarget, rhi::FromVkFormat(m_sceneTarget->GetColorFormat()), false});
                            }
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
                            const uint32_t outputSamples =
                                EffectiveTextureSamples(*texIt->second, static_cast<uint32_t>(msaaSamples));
                            fsSamples = ToRhiSampleCount(static_cast<int>(outputSamples));
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

                const bool useDynamicFullscreen = m_renderGraph->SupportsDynamicRendering();
                vk::ResourceHandle fullscreenShadowInput;
                if (!fullscreenShadowDependencyDeclared && !perViewShadowTextureName.empty()) {
                    const auto shadow = customRTHandles.find(perViewShadowTextureName);
                    if (shadow != customRTHandles.end())
                        fullscreenShadowInput = shadow->second;
                }
                m_renderGraph->AddPass(passDesc.name, [=, &fsWrittenVersion](vk::PassBuilder &builder) {
                    // Declare read dependencies for DAG edges + barriers
                    for (const auto &input : fsReadInputs) {
                        if (input.depthRead) {
                            builder.ReadSampledDepth(input.handle);
                        } else {
                            builder.Read(input.handle);
                        }
                    }
                    if (fullscreenShadowInput.IsValid() &&
                        std::none_of(fsReadInputs.begin(), fsReadInputs.end(),
                                     [&](const auto &input) { return input.handle == fullscreenShadowInput; })) {
                        builder.ReadSampledDepth(fullscreenShadowInput);
                    }
                    // Declare color output
                    fsWrittenVersion = builder.WriteColor(fsOutputTarget, 0);
                    builder.SetRenderArea(fsPassWidth, fsPassHeight);
                    if (useDynamicFullscreen)
                        builder.UseDynamicRendering();

                    return [=, cachedRenderTarget = rhi::RenderTargetLayoutHandle{}](vk::RenderContext &ctx) mutable {
                        if (!useDynamicFullscreen && !cachedRenderTarget.IsValid()) {
                            cachedRenderTarget = renderGraphPtr->GetPassRenderTargetLayout(passDesc.name);
                        }
                        if (!useDynamicFullscreen && !cachedRenderTarget.IsValid())
                            return;

                        // Resolve input texture views using a stack path for the common case.
                        FullscreenTextureInput inputsStack[8] = {};
                        std::vector<FullscreenTextureInput> inputsHeap;
                        FullscreenTextureInput *inputs = inputsStack;
                        if (fsReadInputs.size() > 8) {
                            inputsHeap.resize(fsReadInputs.size());
                            inputs = inputsHeap.data();
                        }
                        const uint32_t inputCount = static_cast<uint32_t>(fsReadInputs.size());
                        for (uint32_t i = 0; i < inputCount; ++i) {
                            inputs[i].view = ctx.GetTextureView(fsReadInputs[i].handle);
                            inputs[i].format = fsReadInputs[i].format;
                            inputs[i].depthRead = fsReadInputs[i].depthRead;
                            if (!inputs[i].view.IsValid()) {
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
                        key.inputTextureCount = inputCount;
                        key.useDynamicRendering = useDynamicFullscreen;

                        const auto &entry = fsRenderer->EnsurePipeline(key);
                        if (!entry.pipeline.IsValid())
                            return;

                        // Allocate descriptor set for input textures
                        const auto bindGroup = fsRenderer->AllocateBindGroup(entry.inputLayout, inputs, inputCount,
                                                                             fsRenderer->GetLinearSampler());
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
                        if (historyValidParameterIndex >= 0 &&
                            static_cast<uint32_t>(historyValidParameterIndex) < drawPushConstantSize / sizeof(float)) {
                            const auto history = m_temporalHistories.find(temporalHistoryKey);
                            drawPushConstants.values[historyValidParameterIndex] =
                                history != m_temporalHistories.end() && history->second.valid ? 1.0f : 0.0f;
                        }
                        if (cameraDitheringParameterIndex >= 0 && static_cast<uint32_t>(cameraDitheringParameterIndex) <
                                                                      drawPushConstantSize / sizeof(float)) {
                            drawPushConstants.values[cameraDitheringParameterIndex] = m_cameraDithering ? 1.0f : 0.0f;
                        }
                        if (cameraStopNaNsParameterIndex >= 0 && static_cast<uint32_t>(cameraStopNaNsParameterIndex) <
                                                                     drawPushConstantSize / sizeof(float)) {
                            drawPushConstants.values[cameraStopNaNsParameterIndex] = m_cameraStopNaNs ? 1.0f : 0.0f;
                        }

                        fsRenderer->Draw(ctx.GetGraphicsCommandEncoder(), entry, bindGroup, GetPerViewBindGroup(),
                                         drawPushConstants, drawPushConstantSize);
                    };
                });
                if (fullscreenShadowInput.IsValid()) {
                    m_shadowMapInputHandle = fullscreenShadowInput;
                    m_shadowMapInputIsDepth = true;
                    fullscreenShadowDependencyDeclared = true;
                }
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
            const bool usesDynamicScreenUI = command && command->type == GraphCommandType::DrawScreenUI &&
                                             m_screenUIRenderer && m_screenUIRenderer->UsesDynamicRendering() &&
                                             m_renderGraph->SupportsDynamicRendering();
            const auto geometryPassIt = m_pythonMaterialPasses.find(passDesc.name);
            const bool usesDynamicGeometry =
                geometryPassIt != m_pythonMaterialPasses.end() && geometryPassIt->second.UsesDynamicRendering();
            const bool usesDynamicShadow = usesShadowRendererList && m_renderGraph->SupportsDynamicRendering() &&
                                           geometryPassIt != m_pythonMaterialPasses.end() &&
                                           geometryPassIt->second.UsesDynamicRendering();
            VkFormat shadowDepthFormat = VK_FORMAT_D32_SFLOAT;
            if (usesShadowRendererList && !passDesc.writeDepth.empty()) {
                const auto shadowDepthIt = texDescMap.find(passDesc.writeDepth);
                if (shadowDepthIt != texDescMap.end())
                    shadowDepthFormat = rhi::ToVkFormat(shadowDepthIt->second->format);
            }
            const int shadowQueueMin = command ? command->queueMin : 0;
            const int shadowQueueMax = command ? command->queueMax : 2999;
            const int shadowLightIndex = command ? command->lightIndex : 0;
            const vk::ResourceHandle rendererListHandle =
                usesShadowRendererList ? m_shadowRendererList
                                       : (usesVisibleRendererList ? m_visibleRendererList : vk::ResourceHandle{});
            m_renderGraph->AddPass(passDesc.name, [=, &sharedDepth, &writtenColorVersions, &writtenDepthVersion,
                                                   &writtenResolveVersion](vk::PassBuilder &builder) {
                // Local alias to make vkCore capturable by nested lambdas (MSVC C3481)
                InxVkCoreModular *localVkCore = vkCore;

                struct ParticlePacket
                {
                    std::shared_ptr<particle::ParticleGpuOutputRenderer> renderer;
                    vk::ResourceHandle instances;
                    vk::ResourceHandle renderIndices;
                    vk::ResourceHandle indirectArguments;
                    rhi::BufferHandle drawRenderIndices;
                    uint32_t ownerLayerMask = 1u;
                };
                std::vector<ParticlePacket> particlePackets;
                particlePackets.reserve(particleEntries.size());

                if (command && command->type == GraphCommandType::DrawRenderers &&
                    command->shaderTarget == ShaderCompileTarget::ForwardPlus) {
                    for (const auto &resources : forwardPlusResources) {
                        if (!resources.canonicalLights.IsValid() || !resources.headers.IsValid() ||
                            !resources.lightMasks.IsValid()) {
                            continue;
                        }
                        builder.ReadStorageBuffer(resources.canonicalLights, rhi::PipelineStage::FragmentShader);
                        builder.ReadStorageBuffer(resources.headers, rhi::PipelineStage::FragmentShader);
                        builder.ReadStorageBuffer(resources.lightMasks, rhi::PipelineStage::FragmentShader);
                    }
                    const bool hasLitParticles =
                        std::any_of(particleEntries.begin(), particleEntries.end(),
                                    [](const auto &entry) { return entry.semantics.receiveSceneLighting; });
                    if (hasLitParticles) {
                        for (const auto &resources : particleForwardPlusResources) {
                            if (!resources.canonicalLights.IsValid() || !resources.headers.IsValid() ||
                                !resources.lightMasks.IsValid()) {
                                continue;
                            }
                            builder.ReadStorageBuffer(resources.canonicalLights, rhi::PipelineStage::FragmentShader);
                            builder.ReadStorageBuffer(resources.headers, rhi::PipelineStage::FragmentShader);
                            builder.ReadStorageBuffer(resources.lightMasks, rhi::PipelineStage::FragmentShader);
                        }
                    }
                }
                for (const auto &entry : particleEntries) {
                    vk::ResourceHandle instances;
                    vk::ResourceHandle renderIndices;
                    vk::ResourceHandle indirectArguments;
                    rhi::BufferHandle drawRenderIndices = entry.renderIndices;
                    const auto sorted = particleGraphResources.find(entry.id);
                    if (!usesShadowRendererList && sorted != particleGraphResources.end()) {
                        instances = sorted->second.instances;
                        renderIndices = sorted->second.renderIndices;
                        indirectArguments = sorted->second.indirectArguments;
                        drawRenderIndices = sorted->second.drawRenderIndices;
                    } else {
                        const std::string prefix = "GpuParticle/" + std::to_string(entry.id);
                        instances = builder.ImportBuffer(prefix + "/Instances", entry.instances,
                                                         static_cast<uint64_t>(entry.capacity) *
                                                             particle::ParticleGpuRuntime::RenderInstanceStride);
                        renderIndices = builder.ImportBuffer(prefix + "/RenderIndices", entry.renderIndices,
                                                             static_cast<uint64_t>(entry.capacity) * sizeof(uint32_t));
                        indirectArguments = builder.ImportBuffer(prefix + "/Indirect", entry.indirectArguments, 16);
                        m_renderGraph->SetResourceInitialState(
                            instances, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                            rhi::PipelineStage::ComputeShader, rhi::QueueRole::Compute);
                        m_renderGraph->SetResourceInitialState(
                            indirectArguments, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                            rhi::PipelineStage::ComputeShader, rhi::QueueRole::Compute);
                        m_renderGraph->SetResourceInitialState(
                            renderIndices, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                            rhi::PipelineStage::ComputeShader, rhi::QueueRole::Compute);
                    }
                    if (!instances.IsValid() || !renderIndices.IsValid() || !indirectArguments.IsValid())
                        continue;
                    builder.ReadStorageBuffer(instances, rhi::PipelineStage::VertexShader);
                    builder.ReadStorageBuffer(renderIndices, rhi::PipelineStage::VertexShader);
                    if (entry.cullMode == particle::GpuParticleCullMode::RibbonSegments &&
                        sorted != particleGraphResources.end() && sorted->second.sourceRenderIndices.IsValid() &&
                        sorted->second.sourceRenderIndices.id != renderIndices.id) {
                        builder.ReadStorageBuffer(sorted->second.sourceRenderIndices, rhi::PipelineStage::VertexShader);
                    }
                    uint32_t staticBufferIndex = 0;
                    for (const auto &staticBuffer : entry.renderer->StaticVertexStorageBuffers()) {
                        const std::string name = "GpuParticle/" + std::to_string(entry.id) + "/StaticVertex" +
                                                 std::to_string(staticBufferIndex++);
                        const auto handle = builder.ImportBuffer(name, staticBuffer.buffer, staticBuffer.byteSize);
                        if (!handle.IsValid())
                            continue;
                        m_renderGraph->SetResourceInitialState(handle, rhi::TextureLayout::Undefined,
                                                               rhi::Access::TransferWrite,
                                                               rhi::PipelineStage::Transfer);
                        builder.ReadStorageBuffer(handle, rhi::PipelineStage::VertexShader);
                    }
                    builder.ReadIndirectBuffer(indirectArguments);
                    particlePackets.push_back({entry.renderer, instances, renderIndices, indirectArguments,
                                               drawRenderIndices, entry.ownerLayerMask});
                }

                if (rendererListHandle.IsValid()) {
                    builder.ReadRendererList(rendererListHandle);
                    // A shadow pass with no casters must still clear a
                    // previously populated persistent atlas.
                    builder.SkipCallbackWhenRendererListsEmpty(!usesShadowRendererList && particlePackets.empty());
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
                        depth = builder.CreateDepthStencil(passDesc.writeDepth, passWidth, passHeight,
                                                           shadowDepthFormat, VK_SAMPLE_COUNT_1_BIT);
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
                } else if (!depth.IsValid() && passReadsDepth && depthForThisPass.IsValid()) {
                    // Pass reads depth (e.g., skybox, transparent) — attach as read-only
                    builder.ReadDepth(depthForThisPass);
                }
                if (particleSceneDepth.IsValid()) {
                    if (particleSceneDepthIsDepth)
                        builder.ReadSampledDepth(particleSceneDepth, rhi::PipelineStage::FragmentShader);
                    else
                        builder.Read(particleSceneDepth, rhi::PipelineStage::FragmentShader);
                }

                // Scene-sized depth textures are pre-registered in customRTHandles,
                // so later writers commonly take the first branch above. Keep the
                // shared read-only alias on the same SSA version; otherwise skybox
                // and editor passes keep reading the initial depth after it has been
                // overwritten, which creates a real physical-resource scheduling
                // cycle in the versioned RenderGraph.
                if (sharedDepth.IsValid() && writtenDepthVersion.IsValid() &&
                    sharedDepth.id == writtenDepthVersion.id) {
                    sharedDepth = writtenDepthVersion;
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
                if (usesDynamicScreenUI || usesDynamicGeometry || usesDynamicShadow)
                    builder.UseDynamicRendering();

                // ----- Clear values -----
                if (clearColor) {
                    builder.SetClearColor(clearColorR, clearColorG, clearColorB, clearColorA);
                }
                if (clearDepth && !isShadowPass) {
                    builder.SetClearDepth(clearDepthVal, 0);
                }

                for (const auto &binding : inputBindingHandles) {
                    if (binding.samplerName == "shadowMap" && !m_shadowMapInputHandle.IsValid()) {
                        m_shadowMapInputHandle = binding.handle;
                        m_shadowMapInputIsDepth = binding.isDepth;
                    }
                }

                return [this, callback, passWidth, passHeight, inputBindingHandles, isShadowPass, rendererListHandle,
                        usesShadowRendererList, usesDynamicShadow, shadowDepthFormat, localVkCore, particlePackets,
                        particlePass, particleSceneDepth, particleSceneDepthIsDepth, shadowQueueMin, shadowQueueMax,
                        shadowLightIndex, passName = passDesc.name](vk::RenderContext &ctx) {
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
                    if (isShadowPass) {
                        if (!m_shadowAtlasUpdateRequired)
                            return;
                        VkClearAttachment clearAttachment{};
                        clearAttachment.aspectMask = VK_IMAGE_ASPECT_DEPTH_BIT;
                        clearAttachment.clearValue.depthStencil = {1.0f, 0};
                        VkClearRect clearRect{};
                        clearRect.rect.offset = {0, 0};
                        clearRect.rect.extent = {passWidth, passHeight};
                        clearRect.baseArrayLayer = 0;
                        clearRect.layerCount = 1;
                        vkCmdClearAttachments(ctx.GetCommandBuffer(), 1, &clearAttachment, 1, &clearRect);
                        const VkRenderPass compatibleRenderPass =
                            usesDynamicShadow ? VK_NULL_HANDLE : m_renderGraph->GetPassRenderPass(passName);
                        const auto renderTargetLayout = m_renderGraph->GetPassRenderTargetLayout(passName);
                        auto &encoder = ctx.GetGraphicsCommandEncoder();
                        localVkCore->DrawShadowCasters(
                            ctx.GetCommandBuffer(), passWidth, passHeight, shadowQueueMin, shadowQueueMax,
                            m_shadowCameraResourceId, m_cameraLightCollector.GetShadowFrame(), shadowLightIndex,
                            compatibleRenderPass, shadowDepthFormat,
                            [&](uint32_t, const lighting::ShadowView &activeShadowView) {
                                particle::GpuParticleViewConstants shadowView;
                                std::memcpy(shadowView.viewProjection.data(), &activeShadowView.viewProjection[0][0],
                                            sizeof(activeShadowView.viewProjection));
                                shadowView.previousViewProjection = shadowView.viewProjection;
                                // Particle billboards expand along these axes in
                                // the vertex shader; the caster pass must face
                                // the light, not the camera, or the quads
                                // collapse into degenerate shadow silhouettes.
                                shadowView.cameraRight = {
                                    activeShadowView.viewRight.x,
                                    activeShadowView.viewRight.y,
                                    activeShadowView.viewRight.z,
                                    0.0f,
                                };
                                shadowView.cameraUp = {
                                    activeShadowView.viewUp.x,
                                    activeShadowView.viewUp.y,
                                    activeShadowView.viewUp.z,
                                    0.0f,
                                };
                                for (const auto &packet : particlePackets) {
                                    [[maybe_unused]] const bool recorded =
                                        packet.renderer->RecordDraw(encoder, renderTargetLayout, particlePass,
                                                                    ctx.GetBufferHandle(packet.indirectArguments),
                                                                    shadowView, packet.drawRenderIndices);
                                    ctx.RecordParticleDraw(true);
                                }
                            });
                        CommitShadowAtlasUpdate();
                        return;
                    }
                    if (callback)
                        callback(ctx, passWidth, passHeight);
                    if (!particlePackets.empty()) {
                        particle::GpuParticleViewConstants view;
                        const glm::mat4 viewProjection = m_cachedProj * m_cachedView;
                        const glm::mat4 previousViewProjection = GetPreviousViewProj();
                        const glm::mat4 inverseView = glm::inverse(m_cachedView);
                        std::memcpy(view.viewProjection.data(), &viewProjection[0][0], sizeof(viewProjection));
                        std::memcpy(view.previousViewProjection.data(), &previousViewProjection[0][0],
                                    sizeof(previousViewProjection));
                        std::memcpy(view.cameraRight.data(), &inverseView[0][0], sizeof(glm::vec4));
                        std::memcpy(view.cameraUp.data(), &inverseView[1][0], sizeof(glm::vec4));
                        std::memcpy(view.alignmentReference.data(), &inverseView[3][0], sizeof(glm::vec4));
                        view.depthReconstruct = {m_cachedProj[2][2], m_cachedProj[3][2], m_cachedProj[2][3],
                                                 m_cachedProj[3][3]};
                        const auto renderTargetLayout = m_renderGraph->GetPassRenderTargetLayout(passName);
                        auto &encoder = ctx.GetGraphicsCommandEncoder();
                        const auto sceneDepth = particleSceneDepth.IsValid() ? ctx.GetTextureView(particleSceneDepth)
                                                                             : rhi::TextureViewHandle{};
                        particle::GpuParticlePerViewBindings particlePerView;
                        const uint32_t frameIndex = m_vkCore->GetCurrentFrameSlot() % kMaxFramesInFlight;
                        const auto &viewFrame = m_perViewFrames[frameIndex];
                        if (m_perViewLayout.IsValid() && viewFrame.particleGroup.IsValid()) {
                            particlePerView.layout = m_perViewLayout;
                            particlePerView.group = viewFrame.particleGroup;
                        }
                        for (const auto &packet : particlePackets) {
                            auto packetView = view;
                            std::memcpy(&packetView.lightingControl[3], &packet.ownerLayerMask,
                                        sizeof(packet.ownerLayerMask));
                            [[maybe_unused]] const bool recorded = packet.renderer->RecordDraw(
                                encoder, renderTargetLayout, particlePass,
                                ctx.GetBufferHandle(packet.indirectArguments), packetView, packet.drawRenderIndices,
                                packet.renderer->RequiresSceneDepth() ? sceneDepth : rhi::TextureViewHandle{},
                                particleSceneDepthIsDepth, particlePerView);
                            ctx.RecordParticleDraw(true);
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
                m_importedDepthTarget = sharedDepth;
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
        // Editor overlays are a Scene-view concern only.  A standalone Player
        // must export the exact user pipeline result: adding editor-only
        // queues to a Game graph creates extra color-target versions after
        // post processing and can invalidate the MSAA/display output chain.
        // ====================================================================
        if (m_renderView.kind == rhi::RenderViewKind::Scene) {
            m_importedColorTarget =
                AppendAutoPass("_ComponentGizmos", m_importedColorTarget, sharedDepth, width, height);
            m_importedColorTarget = AppendAutoPass("_EditorGizmos", m_importedColorTarget, sharedDepth, width, height);
            m_importedColorTarget = AppendEditorOutline(m_importedColorTarget);
            m_importedColorTarget = AppendAutoPass("_EditorTools", m_importedColorTarget, sharedDepth, width, height);
        }
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
