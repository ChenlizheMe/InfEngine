#pragma once

#include <function/renderer/rhi/RenderViewContext.h>
#include <function/renderer/vk/VkDescriptorManager.h>
#include <function/renderer/vk/VkHandle.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxMesh/InxMesh.h>
#include <glm/glm.hpp>
#include <memory>
#include <vector>

namespace infernux
{

namespace vk
{
class GraphicsSubmissionTicket;
class ImageReadbackTicket;
} // namespace vk

class InxVkCoreModular;
class AssetDatabase;

/// @brief GPU-based mesh preview renderer.
///
/// Renders an arbitrary InxMesh (with per-submesh materials) into a small
/// offscreen framebuffer and reads back RGBA8 pixels for editor thumbnails.
/// Camera is auto-fitted to the mesh AABB.
///
/// Reuses the same render-pass format as GPUMaterialPreview so material
/// pipelines created for either previewer are compatible.
class GPUMeshPreview
{
  public:
    explicit GPUMeshPreview(InxVkCoreModular *vkCore);
    ~GPUMeshPreview();

    GPUMeshPreview(const GPUMeshPreview &) = delete;
    GPUMeshPreview &operator=(const GPUMeshPreview &) = delete;

    [[nodiscard]] std::shared_ptr<vk::ImageReadbackTicket>
    BeginRenderToPixels(const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials, int size);
    [[nodiscard]] std::shared_ptr<vk::ImageReadbackTicket>
    BeginRenderToPixelsCamera(const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials, int size,
                              const glm::mat4 &view, const glm::mat4 &proj, const glm::vec3 &cameraPos,
                              bool cloneMaterials = true);
    bool TryCompleteRenderToPixels(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                   std::vector<unsigned char> &outPixels);

    /// @brief Live editor preview: render directly into a GPU image and return an
    ///        ImGui texture id.  Avoids CPU readback + re-upload every frame
    ///        (same idea as SceneRenderTarget).  Uses scene MSAA/format settings.
    uint64_t RenderToImGuiTextureCamera(const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials,
                                        int size, const glm::mat4 &view, const glm::mat4 &proj,
                                        const glm::vec3 &cameraPos, bool cloneMaterials = false);

    /// @brief Currently-published ImGui display descriptor, 0 when absent.
    ///
    /// The display target is destroyed and recreated on size/format changes,
    /// so any texture id previously returned by RenderToImGuiTextureCamera
    /// must be validated against this before being drawn again.
    [[nodiscard]] uint64_t GetDisplayTextureId() const
    {
        return static_cast<uint64_t>(reinterpret_cast<uintptr_t>(m_displayDescriptorSet));
    }

    [[nodiscard]] const rhi::RenderViewContext &GetRenderViewContext() const noexcept
    {
        return m_renderView;
    }

  private:
    bool EnsureResources(int size);
    bool EnsureViewResources();
    void DestroyViewResources();
    void EnsureImGuiDisplayDescriptor();
    void DestroyImGuiDisplayDescriptor();
    void CreateRenderPass();
    void CreateFramebuffer(int size);
    void DestroyFramebuffer();
    void PublishRenderView();
    void UnpublishRenderView();

    InxVkCoreModular *m_vkCore = nullptr;
    rhi::RenderViewContext m_renderView;
    int m_currentSize = 0;

    VkRenderPass m_renderPass = VK_NULL_HANDLE;

    vk::VkImageHandle m_msaaColor;
    vk::VkImageHandle m_resolveColor;
    vk::VkImageHandle m_depth;

    VkFramebuffer m_framebuffer = VK_NULL_HANDLE;

    VkDescriptorSet m_fallbackShadowDescSet = VK_NULL_HANDLE;
    vk::DescriptorLease m_fallbackShadowDescLease;

    std::unique_ptr<vk::VkBufferHandle> m_previewSceneUbo;
    std::unique_ptr<vk::VkBufferHandle> m_previewLightingUbo;
    std::unique_ptr<vk::VkBufferHandle> m_previewGlobalsUbo;
    std::unique_ptr<vk::VkBufferHandle> m_previewInstanceBuffer;
    std::unique_ptr<vk::VkBufferHandle> m_previewSkinInstanceBuffer;
    std::unique_ptr<vk::VkBufferHandle> m_previewSkinPaletteBuffer;
    std::unique_ptr<vk::VkBufferHandle> m_previewInstanceAuxBuffer;
    VkDescriptorSet m_previewGlobalsSet = VK_NULL_HANDLE;
    vk::DescriptorLease m_previewGlobalsLease;
    std::shared_ptr<vk::GraphicsSubmissionTicket> m_activeSubmission;
    std::shared_ptr<vk::ImageReadbackTicket> m_activeReadback;

    VkSampler m_displaySampler = VK_NULL_HANDLE;
    VkDescriptorSet m_displayDescriptorSet = VK_NULL_HANDLE;
    bool m_displayImageShaderReady = false;

    VkFormat m_colorFormat = VK_FORMAT_UNDEFINED;
    VkFormat m_depthFormat = VK_FORMAT_UNDEFINED;
    VkSampleCountFlagBits m_sampleCount = VK_SAMPLE_COUNT_1_BIT;
};

} // namespace infernux
