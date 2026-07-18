#pragma once

#include "FrameDeletionQueue.h"
#include "GpuResidency.h"
#include "MaterialDescriptor.h"
#include "MaterialPassPipeline.h"
#include "shader/ShaderProgram.h"
#include <function/resources/InxMaterial/InxMaterial.h>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux
{

// Forward declaration
class ShaderProgram;

/**
 * @brief MaterialRenderData - Runtime render data for a material
 *
 * Contains the Vulkan resources needed to render with a material.
 */
struct MaterialRenderData
{
    std::shared_ptr<InxMaterial> material;
    VkPipeline pipeline = VK_NULL_HANDLE;
    VkPipelineLayout pipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
    VkShaderModule vertModule = VK_NULL_HANDLE;
    VkShaderModule fragModule = VK_NULL_HANDLE;
    ShaderProgram *shaderProgram = nullptr; // Reference to cached shader program
    ShaderProgramKey programKey;
    MaterialDescriptorSet *materialDescSet = nullptr; // Per-material descriptor set
    size_t pipelineHash = 0;
    bool isValid = false;
};

struct MaterialPassRenderDataKey
{
    std::string materialKey;
    ShaderProgramVariantKey programKey;
    MaterialPassPipelineDescriptor pipeline;
    size_t renderStateHash = 0;

    friend bool operator==(const MaterialPassRenderDataKey &lhs, const MaterialPassRenderDataKey &rhs) noexcept
    {
        return lhs.materialKey == rhs.materialKey && lhs.programKey == rhs.programKey && lhs.pipeline == rhs.pipeline &&
               lhs.renderStateHash == rhs.renderStateHash;
    }
};

struct MaterialPassRenderDataKeyHash
{
    [[nodiscard]] size_t operator()(const MaterialPassRenderDataKey &key) const noexcept;
};

/// Pipeline state for a semantic material pass. Descriptor ownership remains
/// with the material's Forward render data; linked variants must preserve the
/// same set-0 ABI and can therefore reuse it without replacing the cache entry.
struct MaterialPassRenderData
{
    MaterialPassRenderDataKey key;
    std::shared_ptr<InxMaterial> material;
    VkPipeline pipeline = VK_NULL_HANDLE;
    VkPipelineLayout pipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
    ShaderProgram *shaderProgram = nullptr;
    size_t pipelineHash = 0;
    bool isValid = false;
};

/**
 * @brief MaterialPipelineManager - Manages material-to-pipeline mappings
 *
 * This class handles:
 * - Creating Vulkan pipelines for materials
 * - Caching pipelines by material configuration hash
 * - Shader module management for materials
 * - Descriptor set creation for material properties
 */
class MaterialPipelineManager
{
  public:
    MaterialPipelineManager() = default;
    ~MaterialPipelineManager();

    // Non-copyable
    MaterialPipelineManager(const MaterialPipelineManager &) = delete;
    MaterialPipelineManager &operator=(const MaterialPipelineManager &) = delete;

    /**
     * @brief Initialize the manager
     * @param device Vulkan device
     * @param physicalDevice For memory allocation
     * @param colorFormat Color attachment format for pipeline-compatible render pass
     * @param depthFormat Depth attachment format for pipeline-compatible render pass
     * @param sampleCount MSAA sample count
     * @param shaderProgramCache Externally owned ShaderProgramCache instance
     */
    void Initialize(VmaAllocator allocator, VkDevice device, VkPhysicalDevice physicalDevice, VkFormat colorFormat,
                    VkFormat depthFormat, VkSampleCountFlagBits sampleCount, ShaderProgramCache &shaderProgramCache,
                    FrameDeletionQueue *deletionQueue = nullptr, bool descriptorIndexingEnabled = false);

    /**
     * @brief Cleanup all resources
     * @param skipWaitIdle If true, skip vkDeviceWaitIdle (caller already drained GPU)
     */
    void Shutdown(bool skipWaitIdle = false);

    /**
     * @brief Get or create render data for a material (new API with shader reflection)
     *
     * This version uses shader reflection to automatically create descriptor sets
     * and pipeline layouts.
     *
     * @param material The material to get render data for
     * @param vertShaderCode SPIR-V code for vertex shader
     * @param fragShaderCode SPIR-V code for fragment shader
     * @param programKey Typed vertex/fragment pair and immutable artifact revision
     * @param sceneUBO Scene uniform
     * buffer for binding (binding=0)
     * @param sceneUBOSize Size of scene UBO
     * @param lightingUBO Lighting uniform buffer for binding (binding=1)
     * @param lightingUBOSize Size of lighting UBO
     * @return Pointer to render data, or nullptr on failure
     */
    MaterialRenderData *
    GetOrCreateRenderDataWithReflection(std::shared_ptr<InxMaterial> material, const std::vector<char> &vertShaderCode,
                                        const std::vector<char> &fragShaderCode, const ShaderProgramKey &programKey,
                                        VkBuffer sceneUBO, VkDeviceSize sceneUBOSize,
                                        VkBuffer lightingUBO = VK_NULL_HANDLE, VkDeviceSize lightingUBOSize = 0);

    /**
     * @brief Get existing render data for a material (doesn't create new)
     */
    MaterialRenderData *GetRenderData(const std::string &materialName);

    /**
     * @brief Check if render data exists for a material
     */
    bool HasRenderData(const std::string &materialName) const;

    /**
     * @brief Get the default material render data
     */
    MaterialRenderData *GetDefaultRenderData();

    /// Get or create a semantic pass pipeline without replacing the material's
    /// Forward pipeline or descriptor set.
    MaterialPassRenderData *GetOrCreatePassRenderData(std::shared_ptr<InxMaterial> material, ShaderProgram &program,
                                                      const MaterialPassPipelineDescriptor &pipeline);

    [[nodiscard]] MaterialPassPipelineDescriptor
    GetDefaultPassPipelineDescriptor(ShaderCompileTarget target = ShaderCompileTarget::Forward) const;

    /**
     * @brief Get pipeline by hash (for caching)
     */
    VkPipeline GetCachedPipeline(size_t pipelineHash) const;

    /**
     * @brief Update material UBO with current property values
     */
    void UpdateMaterialProperties(const std::string &materialName, const InxMaterial &material);

    /**
     * @brief Bind a texture to a material
     */
    void BindMaterialTexture(const std::string &materialName, uint32_t binding, VkImageView imageView,
                             VkSampler sampler);

    /**
     * @brief Set default texture for fallback
     */
    void SetDefaultTexture(VkImageView imageView, VkSampler sampler);

    /**
     * @brief Set default flat normal map texture for fallback
     */
    void SetDefaultNormalTexture(VkImageView imageView, VkSampler sampler);

    /**
     * @brief Set texture resolver for material Texture2D properties
     *
     * When a material has Texture2D properties, this resolver is called to
     * load the texture file and return VkImageView + VkSampler pairs.
     */
    void SetTextureResolver(TextureResolver resolver)
    {
        m_descriptorManager.SetTextureResolver(std::move(resolver));
    }

    /**
     * @brief Get the material descriptor manager
     */
    MaterialDescriptorManager &GetDescriptorManager()
    {
        return m_descriptorManager;
    }

    /**
     * @brief Returns true if the VkDescriptorSet belongs to this manager and is still live.
     */
    [[nodiscard]] bool IsDescriptorSetLive(VkDescriptorSet ds) const
    {
        return m_descriptorManager.IsDescriptorSetLive(ds);
    }

    /**
     * @brief Invalidate render data for materials using a specific shader
     *
     * This should be called when a shader is hot-reloaded to force pipeline recreation.
     * @param shaderId The shader identifier that was modified
     */
    void InvalidateMaterialsUsingShader(const std::string &shaderId);

    void InvalidateMaterialsUsingProgramPair(const ShaderStagePair &stages);

    /**
     * @brief Remove render data for materials that reference a specific texture.
     *
     * This is used when a texture is reimported and its cached VkImageView /
     * VkSampler handles are about to be destroyed. It also covers runtime-only
     * material instances that are not tracked by the asset dependency graph.
     *
     * GUID-only contract: material Texture2D values are normalized to GUIDs
     * at the setter boundary, so matching is plain GUID equality.
     *
     * @param textureGuid The texture asset GUID
     * @return Number of materials invalidated
     */
    uint32_t InvalidateMaterialsUsingTexture(const std::string &textureGuid);

    /**
     * @brief Mark ALL cached material pipelines as dirty.
     *
     * Called when the render graph topology changes (e.g. forward→deferred switch,
     * MSAA change) so that every material's pipeline is re-evaluated on the next
     * draw against the new render pass configuration.
     */
    void InvalidateAllMaterialPipelines();

    /**
     * @brief Remove render data for a specific material (force recreation)
     */
    void RemoveRenderData(const std::string &materialName);
    [[nodiscard]] size_t CollectUnusedRenderData();
    [[nodiscard]] MaterialGpuResidencySnapshot GetResidencySnapshot() const;

    /// @brief Get the color attachment format used for pipeline creation.
    [[nodiscard]] VkFormat GetColorFormat() const
    {
        return m_colorFormat;
    }

    /// @brief Get the depth attachment format used for pipeline creation.
    [[nodiscard]] VkFormat GetDepthFormat() const
    {
        return m_depthFormat;
    }

    /// @brief Get the MSAA sample count used for pipeline creation.
    [[nodiscard]] VkSampleCountFlagBits GetSampleCount() const
    {
        return m_sampleCount;
    }

  private:
    VkDevice m_device = VK_NULL_HANDLE;
    VmaAllocator m_allocator = VK_NULL_HANDLE;
    VkPhysicalDevice m_physicalDevice = VK_NULL_HANDLE;
    VkRenderPass m_internalRenderPass = VK_NULL_HANDLE; // Internally created compatible render pass
    VkFormat m_colorFormat = VK_FORMAT_UNDEFINED;
    VkFormat m_depthFormat = VK_FORMAT_UNDEFINED;
    VkSampleCountFlagBits m_sampleCount = VK_SAMPLE_COUNT_1_BIT;

    std::unordered_map<MaterialPassPipelineDescriptor, VkRenderPass, MaterialPassPipelineDescriptorHash>
        m_passRenderPassCache;

    // Injected dependency — owned externally by InxVkCoreModular
    ShaderProgramCache *m_shaderProgramCache = nullptr;

    // Material name -> render data
    std::unordered_map<std::string, std::unique_ptr<MaterialRenderData>> m_renderDataMap;

    std::unordered_map<MaterialPassRenderDataKey, std::unique_ptr<MaterialPassRenderData>,
                       MaterialPassRenderDataKeyHash>
        m_passRenderDataMap;

    // Pipeline hash -> pipeline (for sharing pipelines across materials with same config)
    std::unordered_map<size_t, VkPipeline> m_pipelineCache;

    // Vulkan Pipeline Cache for faster recreation
    VkPipelineCache m_vkPipelineCache = VK_NULL_HANDLE;

  public:
    VkPipelineCache GetVkPipelineCache() const
    {
        return m_vkPipelineCache;
    }

  private:
    // Default material render data
    MaterialRenderData *m_defaultRenderData = nullptr;

    // Material descriptor manager for per-material descriptor sets
    MaterialDescriptorManager m_descriptorManager;
    FrameDeletionQueue *m_deletionQueue = nullptr;

    void RetireMaterialUBO(InxMaterial::DetachedUBO resource);

    /**
     * @brief Create a shader module from SPIR-V code
     */
    VkShaderModule CreateShaderModule(const std::vector<char> &code);

    /**
     * @brief Create pipeline using shader program (new method)
     */
    VkPipeline CreatePipelineWithProgram(ShaderProgram *program, const RenderState &renderState);
    VkPipeline CreatePipelineWithProgram(ShaderProgram *program, const RenderState &renderState,
                                         const MaterialPassPipelineDescriptor &pipeline);

    [[nodiscard]] VkRenderPass GetCompatibleRenderPass(const MaterialPassPipelineDescriptor &pipeline);
    [[nodiscard]] static bool IsMaterialDescriptorSetCompatible(const ShaderProgram &forward,
                                                                const ShaderProgram &pass);
    [[nodiscard]] static size_t FoldPassPipelineHash(size_t baseHash, const MaterialPassPipelineDescriptor &pipeline,
                                                     const ShaderProgramVariantKey &programKey);
    void RemovePassRenderData(const std::string &materialKey);
    void RemoveAllPassRenderData();
    void RetirePipelineIfUnreferenced(VkPipeline pipeline);

    /**
     * @brief Create internal compatible render pass from stored formats
     */
    void CreateInternalRenderPass();

    /**
     * @brief Build a Vulkan render pass with N color + optional depth attachment.
     * Shared by the default and semantic pass pipeline caches.
     */
    VkRenderPass BuildCompatibleRenderPass(uint32_t colorAttachmentCount, const VkFormat *colorFormats);
    VkRenderPass BuildCompatibleRenderPass(const MaterialPassPipelineDescriptor &pipeline);

    /**
     * @brief Write forward-pass Vulkan handles to a material and clear its dirty flag.
     */
    static void SyncMaterialForwardPass(InxMaterial *material, VkPipeline pipeline, VkPipelineLayout layout,
                                        VkDescriptorSet descSet, ShaderProgram *program);

    /**
     * @brief Check whether another Forward or semantic pass entry references the same VkPipeline.
     */
    bool IsPipelineSharedByOthers(const std::string &excludeName, VkPipeline pipeline) const;

    /**
     * @brief Destroy non-forward pass pipelines stored on a material and clear handles.
     */
    void DestroyNonForwardPipelines(InxMaterial *material, bool deferred = false);
};

} // namespace infernux
