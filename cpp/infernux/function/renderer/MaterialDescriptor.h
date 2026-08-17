#pragma once

#include "rhi/GpuRetirementQueue.h"
#include "rhi/RhiTexture.h"
#include "shader/ShaderProgram.h"
#include "vk/VkDescriptorManager.h"
#include <array>
#include <atomic>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <functional>
#include <glm/glm.hpp>
#include <memory>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <vk_mem_alloc.h>
#include <vulkan/vulkan.h>

namespace infernux
{
// Forward declaration
class InxVkResourceManager;

/**
 * @brief MaterialUBO - GPU uniform buffer for material properties
 *
 * This class manages the GPU-side uniform buffer for material properties.
 * It automatically maps material property values to the UBO layout
 * as defined by shader reflection.
 */
class MaterialUBO
{
  public:
    MaterialUBO() = default;
    ~MaterialUBO();

    // Non-copyable
    MaterialUBO(const MaterialUBO &) = delete;
    MaterialUBO &operator=(const MaterialUBO &) = delete;

    /**
     * @brief Create UBO from material layout
     * @param allocator VMA allocator
     * @param device Vulkan device
     * @param layout The UBO layout from shader reflection
     * @return true if creation succeeded
     */
    bool Create(VmaAllocator allocator, VkDevice device, const MaterialUBOLayout &layout);

    /**
     * @brief Destroy UBO resources
     */
    void Destroy();

    /**
     * @brief Update UBO from material properties
     * @param material The material containing property values
     */
    void Update(const InxMaterial &material);

    /**
     * @brief Update a specific property in the UBO
     */
    void SetFloat(const std::string &name, float value);
    void SetVec2(const std::string &name, const glm::vec2 &value);
    void SetVec3(const std::string &name, const glm::vec3 &value);
    void SetVec4(const std::string &name, const glm::vec4 &value);
    void SetInt(const std::string &name, int value);
    void SetMat4(const std::string &name, const glm::mat4 &value);

    /// Update the fixed bindless material-index ABI (set 0, binding 15).
    void UpdateTextureIndices(const std::array<uint32_t, ShaderProgram::MaterialTextureIndexCapacity> &indices);
    void UpdateTextureIndex(std::string_view name, uint32_t index);

    /**
     * @brief Get buffer for binding
     */
    [[nodiscard]] VkBuffer GetBuffer() const
    {
        return m_buffer;
    }

    /**
     * @brief Get buffer size
     */
    [[nodiscard]] uint32_t GetSize() const
    {
        return m_size;
    }

    /**
     * @brief Check if valid
     */
    [[nodiscard]] bool IsValid() const
    {
        return m_buffer != VK_NULL_HANDLE;
    }

    [[nodiscard]] const MaterialUBOLayout &GetLayout() const noexcept
    {
        return m_layout;
    }

  private:
    VmaAllocator m_allocator = VK_NULL_HANDLE;
    VkDevice m_device = VK_NULL_HANDLE;
    VkBuffer m_buffer = VK_NULL_HANDLE;
    VmaAllocation m_allocation = VK_NULL_HANDLE;
    void *m_mappedData = nullptr;
    uint32_t m_size = 0;

    MaterialUBOLayout m_layout;

    /**
     * @brief Write data to a specific offset in the buffer
     */
    void WriteData(uint32_t offset, const void *data, uint32_t size);
};

/**
 * @brief MaterialDescriptorSet - Per-material descriptor set
 *
 * Contains all resources needed for a material:
 * - Scene UBO (shared)
 * - Material UBO (per-material)
 * - Textures (per-material)
 */
struct MaterialDescriptorSet
{
    VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
    VkDescriptorSetLayout layout = VK_NULL_HANDLE; // Track which layout was used to create this set
    vk::DescriptorLease descriptorLease;
    std::unique_ptr<MaterialUBO> materialUBO;
    std::unique_ptr<MaterialUBO> vertexMaterialUBO; // Vertex-stage material UBO (binding 14)
    std::unique_ptr<MaterialUBO> textureIndexUBO;   // Bindless ABI (binding 15)
    std::vector<MergedDescriptorBinding> bindings;
    std::unordered_map<uint32_t, VkDescriptorBufferInfo> bufferBindings;

    // Texture bindings (binding -> imageView, sampler)
    struct TextureBinding
    {
        VkImageView imageView = VK_NULL_HANDLE;
        VkSampler sampler = VK_NULL_HANDLE;
        std::shared_ptr<rhi::TextureGpuViewSlot> gpuSlot;
        std::shared_ptr<const rhi::TextureGpuView> gpuView;
        rhi::ResourceIndex resourceIndex{};
    };
    std::unordered_map<uint32_t, TextureBinding> textureBindings;
    std::vector<rhi::ResourceIndex> bindlessTextureIndices;

    bool isValid = false;
    bool hasPendingTextures = false;
    bool usesBindlessTextureABI = false;
};

enum class TextureResolveStatus
{
    Ready,
    Pending,
    Failed,
};

struct TextureResolveResult
{
    TextureResolveStatus status = TextureResolveStatus::Failed;
    MaterialDescriptorSet::TextureBinding binding;
};

/**
 * @brief Callback type for resolving texture paths to GPU resources
 *
 * Given a texture asset GUID (from material Texture2D properties) and the
 * binding name from shader reflection (e.g.
 * "normalMap", "albedoTex"), returns the VkImageView and VkSampler for that texture. The callback should handle
 * caching, format selection (e.g. UNORM for normal maps), and GPU upload internally. Pending is distinct from
 *
 * failure so asynchronous residency does not produce a false error while the default texture is bound.
 */
using TextureResolver =
    std::function<TextureResolveResult(const std::string &textureGuid, const std::string &bindingName)>;

using BindlessTextureResolver =
    std::function<rhi::ResourceIndex(const std::shared_ptr<const rhi::TextureGpuView> &view)>;

/**
 * @brief MaterialDescriptorManager - Manages material-specific descriptor sets
 *
 * This class handles:
 * - Creating descriptor sets from shader program layouts
 * - Managing material UBOs
 * - Binding textures to materials
 * - Updating descriptor sets when material properties change
 */
class MaterialDescriptorManager
{
  public:
    MaterialDescriptorManager() = default;
    ~MaterialDescriptorManager();

    // Non-copyable
    MaterialDescriptorManager(const MaterialDescriptorManager &) = delete;
    MaterialDescriptorManager &operator=(const MaterialDescriptorManager &) = delete;

    /**
     * @brief Initialize the manager
     */
    void Initialize(VmaAllocator allocator, VkDevice device, VkPhysicalDevice physicalDevice,
                    vk::VkDescriptorManager *descriptorManager);

    /**
     * @brief Shutdown and cleanup
     */
    void Shutdown();

    /**
     * @brief Set a texture resolver callback for loading material textures
     *
     * When a material has Texture2D properties, this callback is used to
     * resolve asset GUIDs to VkImageView + VkSampler pairs. If not set,
     * all texture slots fall back to the
     * default texture.
     */
    void SetTextureResolver(TextureResolver resolver)
    {
        m_textureResolver = std::move(resolver);
    }

    void SetBindlessTextureResolver(BindlessTextureResolver resolver)
    {
        m_bindlessTextureResolver = std::move(resolver);
    }

    void SetBindlessMaterialMode(bool enabled) noexcept
    {
        m_bindlessMaterialMode = enabled;
    }

    [[nodiscard]] bool IsBindlessMaterialMode() const noexcept
    {
        return m_bindlessMaterialMode;
    }

    [[nodiscard]] const std::vector<rhi::ResourceIndex> *
    GetBindlessTextureIndices(const std::string &materialName) const noexcept;

    /**
     * @brief Get or create descriptor set for a material
     * @param material The material
     * @param program The shader program (for layout info)
     * @return Pointer to descriptor set info, or nullptr on failure
     */
    MaterialDescriptorSet *GetOrCreateDescriptorSet(const InxMaterial &material, const ShaderProgram &program);

    /**
     * @brief Update descriptor set with new material values
     */
    void UpdateMaterialUBO(const std::string &materialName, const InxMaterial &material);

    /**
     * @brief Re-resolve Texture2D properties for an existing descriptor set
     *
     * Called when material texture properties change (via set_texture).
     * Uses the TextureResolver to update sampler bindings.
     */
    void ResolveTextureProperties(const std::string &materialName, const InxMaterial &material,
                                  const ShaderProgram &program);

    [[nodiscard]] bool HasPendingTextureProperties(const std::string &materialName) const;

    /**
     * @brief Bind texture to material
     * @param materialName Material name
     * @param binding Texture binding slot
     * @param imageView Texture image view
     * @param sampler Texture sampler
     */
    void BindTexture(const std::string &materialName, uint32_t binding, VkImageView imageView, VkSampler sampler);

  private:
    /// Publish a fully written replacement set, then retire the previous
    /// lease after its last possible submission. Active sets are immutable.
    [[nodiscard]] bool PublishDescriptorReplacement(
        MaterialDescriptorSet &descriptorSet,
        const std::unordered_map<uint32_t, MaterialDescriptorSet::TextureBinding> &textureBindings);

  public:
    /// Bind submission-serial retirement for descriptor-owned buffers.
    void SetRetirementQueue(GpuRetirementQueue *queue)
    {
        m_deletionQueue = queue;
    }

    /**
     * @brief Select the Vulkan 1.2 UPDATE_AFTER_BIND descriptor arena.
     *
     * Must be called BEFORE Initialize(). Material updates still use immutable
     * copy-on-write publication, so
     * correctness does not depend on this feature.
     * Both the device and matching ShaderProgram layouts must be
     * configured
     * with descriptor-indexing flags — see
     * VkDeviceContext::CreateLogicalDevice and
     * ShaderProgram::SetUpdateAfterBindEnabled.
     */
    void SetUpdateAfterBindEnabled(bool enabled)
    {
        m_updateAfterBindEnabled = enabled;
    }

    /**
     * @brief Remove descriptor set for a material
     */
    void RemoveDescriptorSet(const std::string &materialName);

    /**
     * @brief Clear all descriptor sets
     */
    void Clear();

    [[nodiscard]] size_t GetDescriptorSetCount() const noexcept
    {
        return m_descriptorSets.size();
    }
    [[nodiscard]] size_t GetPendingTextureDescriptorSetCount() const noexcept
    {
        size_t count = 0;
        for (const auto &[name, descriptorSet] : m_descriptorSets) {
            (void)name;
            if (descriptorSet && descriptorSet->isValid && descriptorSet->hasPendingTextures) {
                ++count;
            }
        }
        return count;
    }
    [[nodiscard]] size_t GetRetiredDescriptorSetCount() const noexcept
    {
        return m_pendingDescriptorSetReleases->load(std::memory_order_relaxed);
    }
    [[nodiscard]] size_t GetDescriptorPoolCount() const noexcept
    {
        return m_descriptorManager ? m_descriptorManager->GetStats().poolCount : 0;
    }

    /**
     * @brief Set default texture for fallback
     */
    void SetDefaultTexture(VkImageView imageView, VkSampler sampler,
                           std::shared_ptr<const rhi::TextureGpuView> gpuView);

    /**
     * @brief Set default normal map texture (flat normal = 0.5, 0.5, 1.0)
     * Used as fallback for sampler bindings whose name contains "normal"
     */
    void SetDefaultNormalTexture(VkImageView imageView, VkSampler sampler,
                                 std::shared_ptr<const rhi::TextureGpuView> gpuView);

  private:
    VmaAllocator m_vmaAllocator = VK_NULL_HANDLE;
    VkDevice m_device = VK_NULL_HANDLE;
    VkPhysicalDevice m_physicalDevice = VK_NULL_HANDLE;

    vk::VkDescriptorManager *m_descriptorManager = nullptr;

    std::unordered_map<std::string, std::unique_ptr<MaterialDescriptorSet>> m_descriptorSets;
    std::shared_ptr<std::atomic_size_t> m_pendingDescriptorSetReleases = std::make_shared<std::atomic_size_t>(0);

    /// Tracks currently active descriptor sets referenced by m_descriptorSets.
    /// Retired sets are removed from this set immediately so draw-time checks
    /// can detect stale material handles and refresh pipelines safely.
    std::unordered_set<uint64_t> m_liveDescriptorHandles;

    // Default texture for fallback
    VkImageView m_defaultImageView = VK_NULL_HANDLE;
    VkSampler m_defaultSampler = VK_NULL_HANDLE;
    std::shared_ptr<const rhi::TextureGpuView> m_defaultGpuView;

    // Default flat normal map texture for fallback (0.5, 0.5, 1.0 encoded)
    VkImageView m_defaultNormalImageView = VK_NULL_HANDLE;
    VkSampler m_defaultNormalSampler = VK_NULL_HANDLE;
    std::shared_ptr<const rhi::TextureGpuView> m_defaultNormalGpuView;

  public:
    /// @brief Get default texture image view (for per-view descriptor fallback)
    [[nodiscard]] VkImageView GetDefaultImageView() const
    {
        return m_defaultImageView;
    }
    /// @brief Get default texture sampler (for per-view descriptor fallback)
    [[nodiscard]] VkSampler GetDefaultSampler() const
    {
        return m_defaultSampler;
    }

  private:
    /**
     * @brief Returns true if the given handle was allocated from this manager's
     *        pools AND has not been invalidated by a pool reset (Clear/Shutdown).
     *
     * Use this before vkCmdBindDescriptorSets to validate material descriptor
     * sets that come from this manager, without needing to free individual sets.
     */
  public:
    [[nodiscard]] bool IsDescriptorSetLive(VkDescriptorSet ds) const
    {
        if (ds == VK_NULL_HANDLE)
            return false;
        return m_liveDescriptorHandles.count(reinterpret_cast<uint64_t>(ds)) > 0;
    }

  private:
    // Texture resolver callback (set via SetTextureResolver)
    TextureResolver m_textureResolver;
    BindlessTextureResolver m_bindlessTextureResolver;

    // Optional submission-serial queue for descriptor-owned resource cleanup.
    GpuRetirementQueue *m_deletionQueue = nullptr;

    /// True when the device supports descriptor-indexing UPDATE_AFTER_BIND
    /// and the layouts/pool were created with the matching flags.
    bool m_updateAfterBindEnabled = false;
    bool m_bindlessMaterialMode = false;

    void RetireDescriptorSet(std::shared_ptr<MaterialDescriptorSet> descriptorSet);

    [[nodiscard]] bool IsPlaceholderTexturePath(std::string_view texturePath) const;

    [[nodiscard]] bool IsNormalBindingName(std::string_view bindingName) const;

    [[nodiscard]] bool TryGetDefaultTextureBinding(std::string_view bindingName,
                                                   MaterialDescriptorSet::TextureBinding &outBinding) const;

    [[nodiscard]] bool ResolveBindlessIndex(MaterialDescriptorSet::TextureBinding &binding) const;

    [[nodiscard]] TextureResolveStatus
    ResolveExplicitTextureBinding(const std::string &texturePath, const std::string &bindingName,
                                  MaterialDescriptorSet::TextureBinding &outBinding) const;

    /**
     * @brief Update descriptor set bindings
     */
    [[nodiscard]] bool UpdateDescriptorBindings(MaterialDescriptorSet &matDescSet, const ShaderProgram &program);
};

} // namespace infernux
