/**
 * @file OutlineRenderer.h
 * @brief Post-process selection outline renderer (Blender/Unity style)
 *
 * Extracted from InxVkCoreModular during editor/renderer separation.
 * This class owns all Vulkan resources
 * related to the screen-space selection outline:
 *   - Mask render pass / framebuffer / pipeline  (renders selected object as white silhouette)
 *   - Composite render pass / framebuffer / pipeline (edge detection + alpha blend on scene color)
 *
 * Lifecycle is managed by InxRenderer; the actual Vulkan command recording
 * is injected into the frame via InxVkCoreModular::SetPostSceneRenderCallback().
 */

#pragma once

#include "InxRenderStruct.h"
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
 *   3. RecordCommands() is called inside the post-scene-render callback
 *   4. OnResize() whenever the scene render target is resized
 *   5. Cleanup() or destructor releases all Vulkan resources
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
    void Cleanup();

    /// @brief Recreate framebuffers after scene render target resize.
    void OnResize(uint32_t width, uint32_t height);

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

    /// @brief Record outline mask + composite commands into the given command buffer.
    ///
    /// This renders the full outline pipeline:
    ///   1. Mask pass — renders selected object as white silhouette
    ///   2. Composite pass — edge detection on mask, alpha-blend onto scene color
    ///
    /// If no outline is active (objectId == 0) or resources aren't ready, this is a no-op.
    ///
    /// @param cmdBuf Active Vulkan command buffer
    /// @param drawCalls Current frame's draw calls (to find the selected object)
    /// @return true if commands were actually recorded, false if skipped (resources not ready)
    bool RecordCommands(VkCommandBuffer cmdBuf, const std::vector<DrawCall> &drawCalls);

    /// @brief Finalize scene-color state when outline is inactive.
    ///
    /// The scene render graph already leaves the sampled scene color in
    /// SHADER_READ_ONLY_OPTIMAL, so this is intentionally a no-op.
    void RecordNoOutlineBarrier(VkCommandBuffer cmdBuf);

  private:
    // ========================================================================
    // Internal Vulkan Resource Creation
    // ========================================================================

    void CreateOutlineMaskRenderPass();
    void CreateOutlineCompositeRenderPass();
    void CreateOutlineFramebuffers();
    void CreateOutlineDescriptorResources();
    void CreateOutlinePipelines();

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

    void RenderOutlineMask(VkCommandBuffer cmdBuf, const std::vector<DrawCall> &drawCalls);
    void RenderOutlineComposite(VkCommandBuffer cmdBuf);
    [[nodiscard]] bool IsOutlinedObject(uint64_t objectId) const
    {
        return objectId != 0 && m_outlineObjectIdSet.count(objectId) != 0;
    }

    /// Begin a render pass with a full-viewport and scissor covering the scene target.
    void BeginRenderPassWithFullViewport(VkCommandBuffer cmdBuf, VkRenderPass rp, VkFramebuffer fb,
                                         const VkClearValue &clearVal);

    // ========================================================================
    // References (non-owning)
    // ========================================================================

    InxVkCoreModular *m_core = nullptr;
    SceneRenderTarget *m_sceneRenderTarget = nullptr;

    // ========================================================================
    // Vulkan Resources (owned)
    // ========================================================================

    // Render passes
    VkRenderPass m_outlineMaskRenderPass = VK_NULL_HANDLE;
    VkRenderPass m_outlineCompositeRenderPass = VK_NULL_HANDLE;

    // Framebuffers
    VkFramebuffer m_outlineMaskFramebuffer = VK_NULL_HANDLE;
    VkFramebuffer m_outlineCompositeFramebuffer = VK_NULL_HANDLE;

    // Mask pipeline (renders selected object as white silhouette)
    VkPipeline m_outlineMaskPipeline = VK_NULL_HANDLE;
    VkPipelineLayout m_outlineMaskPipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_outlineMaskDescSetLayout = VK_NULL_HANDLE;
    VkDescriptorSet m_outlineMaskDescSet = VK_NULL_HANDLE;

    // Composite pipeline (fullscreen edge detection + blend)
    VkPipeline m_outlineCompositePipeline = VK_NULL_HANDLE;
    VkPipelineLayout m_outlineCompositePipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_outlineCompositeDescSetLayout = VK_NULL_HANDLE;
    VkDescriptorSet m_outlineCompositeDescSet = VK_NULL_HANDLE;

    // Shared descriptor pool for outline
    VkDescriptorPool m_outlineDescPool = VK_NULL_HANDLE;

    // ========================================================================
    // Per-material outline mask pipeline resources
    // ========================================================================

    // Pipeline layout: set 0 (scene UBO + vert mat UBO), set 1 (active camera view),
    // set 2 (globals + instance SSBO)
    VkPipelineLayout m_outlineMtlPipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_outlineMtlSet0Layout = VK_NULL_HANDLE;
    VkDescriptorPool m_outlineMtlDescPool = VK_NULL_HANDLE;

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

    // Cached per-material outline mask pipelines (key = material name)
    std::unordered_map<std::string, VkPipeline> m_perMtlOutlinePipelines;

    // Cached per-material set 0 descriptor sets (scene UBO + vertex material UBO)
    std::unordered_map<std::string, VkDescriptorSet> m_perMtlOutlineDescSets;

    // ========================================================================
    // Outline Parameters
    // ========================================================================

    uint64_t m_outlineObjectId = 0;
    std::vector<uint64_t> m_outlineObjectIds;
    std::unordered_set<uint64_t> m_outlineObjectIdSet;
    glm::vec4 m_outlineColor{1.0f, 0.5f, 0.0f, 1.0f}; // Bright orange
    float m_outlinePixelWidth = 3.0f;
    bool m_resourcesReady = false;
};

} // namespace infernux
