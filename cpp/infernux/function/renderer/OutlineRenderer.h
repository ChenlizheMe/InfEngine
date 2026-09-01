/**
 * @file OutlineRenderer.h
 * @brief Post-process selection outline renderer (Blender/Unity style)
 *
 * Extracted from InxVkCoreModular during editor/renderer separation.
 * This class owns the pipelines and descriptor resources used by the
 * screen-space selection outline. RenderGraph
 * owns attachment contracts, image layouts and ordering.
 */

#pragma once

#include "InxRenderStruct.h"
#include "rhi/RhiDescriptors.h"
#include "rhi/RhiHandles.h"
#include "vk/VkDescriptorManager.h"
#include <cstdint>
#include <glm/glm.hpp>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <vulkan/vulkan.h>

// VMA forward declaration
struct VmaAllocator_T;
typedef struct VmaAllocator_T *VmaAllocator;
struct VmaAllocation_T;
typedef struct VmaAllocation_T *VmaAllocation;

namespace infernux
{

// Forward declarations
class InxVkCoreModular;
class SceneRenderTarget;
class InxMaterial;
class ShaderProgram;
class ShaderReflection;

/**
 * @brief Self-contained post-process selection outline renderer.
 *
 * Usage:
 *   1. Initialize() after InxVkCoreModular + SceneRenderTarget are ready
 *   2. SetOutlineObjectId() each frame from InxRenderer
 *   3. EnsureGraphPipelines() is called after the graph compiles
 *   4. RecordMaskDraws()/RecordCompositeDraw() are
 * called by graph passes
 *   5. Recreate the owner for a newly published SceneRenderTarget generation
 *   6.
 * Cleanup() or destructor
 * releases all Vulkan resources
 */
class OutlineRenderer
{
  public:
    OutlineRenderer() = default;
    ~OutlineRenderer();

    // Non-copyable
    OutlineRenderer(const OutlineRenderer &) = delete;
    OutlineRenderer &operator=(const OutlineRenderer &) = delete;

    // ========================================================================
    // Lifecycle
    // ========================================================================

    /// @brief Initialize outline Vulkan resources.
    /// @param core Pointer to the Vulkan core (for device, shaders, UBO access)
    /// @param sceneTarget Pointer to the scene render target (mask image + scene color)
    /// @return true if initialization succeeded, false otherwise
    bool Initialize(InxVkCoreModular *core, SceneRenderTarget *sceneTarget);

    /// @brief Release all Vulkan resources.
    /// @param waitForIdle Wait for pending GPU work before destroying resources.
    void Cleanup(bool waitForIdle = true);

    /// @brief Refresh target-dependent descriptors after a resize.
    /// @brief Check if outline resources are ready for rendering.
    [[nodiscard]] bool IsReady() const
    {
        return m_resourcesReady;
    }

    // ========================================================================
    // State
    // ========================================================================

    /// @brief Set the object ID to outline (0 = no outline).
    void SetOutlineObjectId(uint64_t objectId)
    {
        m_outlineObjectId = objectId;
        m_outlineObjectIds.clear();
        m_outlineObjectIdSet.clear();
        if (objectId != 0) {
            m_outlineObjectIds.push_back(objectId);
            m_outlineObjectIdSet.insert(objectId);
        }
    }

    /// @brief Set all object IDs to outline. The first ID remains the legacy primary ID.
    void SetOutlineObjectIds(const std::vector<uint64_t> &objectIds)
    {
        m_outlineObjectIds.clear();
        m_outlineObjectIdSet.clear();
        for (uint64_t objectId : objectIds) {
            if (objectId == 0 || m_outlineObjectIdSet.count(objectId) != 0)
                continue;
            m_outlineObjectIds.push_back(objectId);
            m_outlineObjectIdSet.insert(objectId);
        }
        m_outlineObjectId = m_outlineObjectIds.empty() ? 0 : m_outlineObjectIds.front();
    }

    /// @brief Get the current outline object ID.
    [[nodiscard]] uint64_t GetOutlineObjectId() const
    {
        return m_outlineObjectId;
    }

    /// @brief Check if there is an active outline to render.
    [[nodiscard]] bool HasActiveOutline() const
    {
        return !m_outlineObjectIds.empty();
    }

    /// @brief Set outline color (default bright orange).
    void SetOutlineColor(float r, float g, float b, float a)
    {
        m_outlineColor = glm::vec4(r, g, b, a);
    }

    /// @brief Set outline color from vec4.
    void SetOutlineColor(const glm::vec4 &color)
    {
        m_outlineColor = color;
    }

    /// @brief Set outline width in pixels (default 3.0).
    void SetOutlinePixelWidth(float width)
    {
        m_outlinePixelWidth = width;
    }

    // ========================================================================
    // Rendering
    // ========================================================================

    /// @brief Build pipelines against the graph's compiled attachment contracts.
    bool EnsureGraphPipelines(const rhi::GraphicsRenderingSignature &maskSignature,
                              const rhi::GraphicsRenderingSignature &compositeSignature);

    /// @brief Record selected-object draws inside the active graph mask pass.
    void RecordMaskDraws(VkCommandBuffer cmdBuf, const std::vector<DrawCall> &drawCalls,
                         rhi::BindGroupHandle perViewGroup);

    /// @brief Record the fullscreen edge composite inside the active graph pass.
    void RecordCompositeDraw(VkCommandBuffer cmdBuf);

  private:
    // ========================================================================
    // Internal Vulkan Resource Creation
    // ========================================================================

    void CreateOutlineDescriptorResources();
    void CreateOutlinePipelineLayouts();
    bool CreateOutlinePipelines();
    void DestroyOutlinePipelines();

    // ========================================================================
    // Per-material outline pipeline support
    // ========================================================================

    void CreateOutlineMaterialResources();
    VkPipeline CreateMaskPipeline(const VkPipelineShaderStageCreateInfo stages[2], VkPipelineLayout layout,
                                  const ShaderReflection &vertexReflection);
    VkPipeline GetOrCreateMtlOutlinePipeline(InxMaterial *material);
    VkDescriptorSet GetOrCreateMtlOutlineDescSet(InxMaterial *material);
    void EnsureOutlineSkinBufferCapacity(uint32_t frameIndex, size_t boneMatrixCount);

    // ========================================================================
    // Internal Rendering
    // ========================================================================

    [[nodiscard]] bool IsOutlinedObject(uint64_t objectId) const
    {
        return objectId != 0 && m_outlineObjectIdSet.count(objectId) != 0;
    }

    // ========================================================================
    // References (non-owning)
    // ========================================================================

    InxVkCoreModular *m_core = nullptr;
    SceneRenderTarget *m_sceneRenderTarget = nullptr;

    // ========================================================================
    // Vulkan Resources (owned)
    // ========================================================================

    rhi::GraphicsRenderingSignature m_outlineMaskRenderingSignature;
    rhi::GraphicsRenderingSignature m_outlineCompositeRenderingSignature;

    // Mask pipeline (renders selected object as white silhouette)
    VkPipeline m_outlineMaskPipeline = VK_NULL_HANDLE;
    VkPipelineLayout m_outlineMaskPipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_outlineMaskDescSetLayout = VK_NULL_HANDLE;

    // Composite pipeline (fullscreen edge detection + blend)
    VkPipeline m_outlineCompositePipeline = VK_NULL_HANDLE;
    VkPipelineLayout m_outlineCompositePipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_outlineCompositeDescSetLayout = VK_NULL_HANDLE;
    VkDescriptorSet m_outlineCompositeDescSet = VK_NULL_HANDLE;
    vk::DescriptorLease m_outlineCompositeDescLease;

    // ========================================================================
    // Per-material outline mask pipeline resources
    // ========================================================================

    // Pipeline layout: set 0 (vertex material properties), set 1 (active camera view),
    // set 2 (globals + instance SSBO)
    VkPipelineLayout m_outlineMtlPipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_outlineMtlSet0Layout = VK_NULL_HANDLE;

    // Per-frame single-instance buffer (1 mat4, for outline object transform)
    struct OutlineInstanceBuf
    {
        VkBuffer buffer = VK_NULL_HANDLE;
        VmaAllocation allocation = VK_NULL_HANDLE;
        void *mapped = nullptr;
    };
    std::vector<OutlineInstanceBuf> m_outlineInstanceBufs;

    struct OutlineSkinBuf
    {
        VkBuffer buffer = VK_NULL_HANDLE;
        VmaAllocation allocation = VK_NULL_HANDLE;
        void *mapped = nullptr;
        size_t capacity = 0;
    };
    std::vector<OutlineSkinBuf> m_outlineSkinInstanceBufs;
    std::vector<OutlineSkinBuf> m_outlineSkinPaletteBufs;
    std::vector<OutlineSkinBuf> m_outlineInstanceAuxBufs;

    // Per-frame outline globals descriptor sets (binding 0 = globals UBO, binding 1 = instance buf,
    // binding 2 = one selected skin instance, binding 3 = selected skin palette,
    // binding 4 = selected instance identity/layer data)
    std::vector<VkDescriptorSet> m_outlineGlobalsDescSets;
    std::vector<vk::DescriptorLease> m_outlineGlobalsDescLeases;

    // Cached per-material outline mask pipelines (key = material name)
    std::unordered_map<std::string, VkPipeline> m_perMtlOutlinePipelines;

    // Cached per-material set 0 descriptor sets (scene UBO + vertex material UBO)
    std::unordered_map<std::string, VkDescriptorSet> m_perMtlOutlineDescSets;
    std::unordered_map<std::string, vk::DescriptorLease> m_perMtlOutlineDescLeases;

    // ========================================================================
    // Outline Parameters
    // ========================================================================

    uint64_t m_outlineObjectId = 0;
    std::vector<uint64_t> m_outlineObjectIds;
    std::unordered_set<uint64_t> m_outlineObjectIdSet;
    glm::vec4 m_outlineColor{1.0f, 0.5f, 0.0f, 1.0f}; // Bright orange
    float m_outlinePixelWidth = 3.0f;
    bool m_resourcesReady = false;
    bool m_missingShadersReported = false;
};

} // namespace infernux
