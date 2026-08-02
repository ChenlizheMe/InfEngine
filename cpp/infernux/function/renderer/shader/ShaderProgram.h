#pragma once

#include "ShaderReflection.h"
#include <core/types/ShaderProgramArtifact.h>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux
{

/**
 * @brief Merged descriptor binding info from multiple shader stages
 */
struct MergedDescriptorBinding
{
    uint32_t binding;
    uint32_t set;
    VkDescriptorType type;
    uint32_t descriptorCount;
    VkShaderStageFlags stageFlags; // Combined from all stages using this binding
    std::string name;
};

/**
 * @brief Material uniform buffer layout
 *
 * Describes the layout of a material's UBO that can be
 * updated from material properties.
 */
struct MaterialUBOLayout
{
    uint32_t binding;
    uint32_t size;
    std::vector<UniformMember> members;

    // Get offset and size for a named property
    bool GetMemberInfo(const std::string &name, uint32_t &offset, uint32_t &size) const
    {
        for (const auto &member : members) {
            if (member.name == name) {
                offset = member.offset;
                size = member.size;
                return true;
            }
        }
        return false;
    }
};

/**
 * @brief ShaderProgram - Combined vert + frag shader with merged reflection
 *
 * This class represents a complete shader program (vert + frag) and provides:
 * - Merged descriptor set layouts from both stages
 * - Auto-generated VkDescriptorSetLayout
 * - Material UBO layout extraction
 * - VkPipelineLayout generation
 */
class ShaderProgram
{
  public:
    ShaderProgram() = default;
    ~ShaderProgram();

    // Non-copyable
    ShaderProgram(const ShaderProgram &) = delete;
    ShaderProgram &operator=(const ShaderProgram &) = delete;

    // Movable
    ShaderProgram(ShaderProgram &&other) noexcept;
    ShaderProgram &operator=(ShaderProgram &&other) noexcept;

    /**
     * @brief Create shader program from SPIR-V code
     * @param device Vulkan device
     * @param vertSpirv Vertex shader SPIR-V
     * @param fragSpirv Fragment shader SPIR-V
     * @param shaderId Unique identifier for this program
     * @return true if creation succeeded
     */
    bool Create(VkDevice device, const std::vector<char> &vertSpirv, const std::vector<char> &fragSpirv,
                const ShaderProgramVariantKey &variantKey);

    /**
     * @brief Destroy all Vulkan resources
     */
    void Destroy();

    // Getters
    [[nodiscard]] const std::string &GetShaderId() const
    {
        return m_shaderId;
    }

    [[nodiscard]] const ShaderProgramKey &GetProgramKey() const noexcept
    {
        return m_variantKey.program;
    }

    [[nodiscard]] ShaderCompileTarget GetCompileTarget() const noexcept
    {
        return m_variantKey.target;
    }

    [[nodiscard]] VkShaderModule GetVertexModule() const
    {
        return m_vertModule;
    }
    [[nodiscard]] VkShaderModule GetFragmentModule() const
    {
        return m_fragModule;
    }

    [[nodiscard]] VkDescriptorSetLayout GetDescriptorSetLayout(uint32_t set = 0) const
    {
        auto it = m_descriptorSetLayouts.find(set);
        return it != m_descriptorSetLayouts.end() ? it->second : VK_NULL_HANDLE;
    }

    [[nodiscard]] bool HasDeclaredDescriptorSet(uint32_t set) const;

    [[nodiscard]] VkPipelineLayout GetPipelineLayout() const
    {
        return m_pipelineLayout;
    }

    [[nodiscard]] const std::vector<MergedDescriptorBinding> &GetDescriptorBindings() const
    {
        return m_descriptorBindings;
    }

    [[nodiscard]] const MaterialUBOLayout *GetMaterialUBOLayout() const
    {
        return m_hasMaterialUBO ? &m_materialUBOLayout : nullptr;
    }

    [[nodiscard]] const MaterialUBOLayout *GetVertexMaterialUBOLayout() const
    {
        return m_hasVertexMaterialUBO ? &m_vertexMaterialUBOLayout : nullptr;
    }

    [[nodiscard]] const ShaderReflection &GetVertexReflection() const
    {
        return m_vertReflection;
    }
    [[nodiscard]] const ShaderReflection &GetFragmentReflection() const
    {
        return m_fragReflection;
    }

    /**
     * @brief Get the number of texture bindings
     */
    [[nodiscard]] uint32_t GetTextureBindingCount() const;

    /**
     * @brief Check if shader has a material properties UBO
     */
    [[nodiscard]] bool HasMaterialUBO() const
    {
        return m_hasMaterialUBO;
    }

    [[nodiscard]] bool HasVertexMaterialUBO() const
    {
        return m_hasVertexMaterialUBO;
    }

    /**
     * @brief Check if valid
     */
    [[nodiscard]] bool IsValid() const
    {
        return m_vertModule != VK_NULL_HANDLE && m_fragModule != VK_NULL_HANDLE;
    }

    /**
     * @brief Set the engine-globals descriptor set layout (set 2).
     * Called once at startup; all ShaderProgram instances will include this
     * layout in their pipeline layouts so that the globals UBO can be bound.
     */
    static void SetGlobalsDescSetLayout(VkDescriptorSetLayout layout);
    [[nodiscard]] static VkDescriptorSetLayout GetGlobalsDescSetLayout();

    static void SetPerViewDescSetLayout(VkDescriptorSetLayout layout);
    [[nodiscard]] static VkDescriptorSetLayout GetPerViewDescSetLayout();

    /**
     * @brief Globally enable VK_DESCRIPTOR_BINDING_UPDATE_AFTER_BIND on
     * material-set sampler bindings. Set once at startup based on the
     * device's Vulkan 1.2 descriptor-indexing support; when true, the
     * material descriptor pool/layout pair is created with the matching
     * UPDATE_AFTER_BIND_POOL bit so MaterialDescriptorManager can rewrite
     * bindings without a full GPU drain.
     */
    static void SetUpdateAfterBindEnabled(bool enabled);
    [[nodiscard]] static bool IsUpdateAfterBindEnabled();

  private:
    VkDevice m_device = VK_NULL_HANDLE;
    std::string m_shaderId;
    ShaderProgramVariantKey m_variantKey;

    static VkDescriptorSetLayout s_globalsDescSetLayout;
    static VkDescriptorSetLayout s_perViewDescSetLayout;
    static bool s_updateAfterBindEnabled;

    // Shader modules
    VkShaderModule m_vertModule = VK_NULL_HANDLE;
    VkShaderModule m_fragModule = VK_NULL_HANDLE;

    // Reflection data
    ShaderReflection m_vertReflection;
    ShaderReflection m_fragReflection;

    // Merged descriptor bindings
    std::vector<MergedDescriptorBinding> m_descriptorBindings;

    // Vulkan resources (auto-generated from reflection)
    std::unordered_map<uint32_t, VkDescriptorSetLayout> m_descriptorSetLayouts;
    VkPipelineLayout m_pipelineLayout = VK_NULL_HANDLE;

    // Material UBO layout (if present)
    MaterialUBOLayout m_materialUBOLayout;
    bool m_hasMaterialUBO = false;

    // Vertex-stage material UBO layout (if present, binding 14)
    MaterialUBOLayout m_vertexMaterialUBOLayout;
    bool m_hasVertexMaterialUBO = false;

    /**
     * @brief Create shader module from SPIR-V
     */
    VkShaderModule CreateShaderModule(const std::vector<char> &code);

    /**
     * @brief Merge reflection data from both stages
     */
    void MergeReflectionData();

    /**
     * @brief Create descriptor set layouts from merged bindings
     */
    bool CreateDescriptorSetLayouts();

    /**
     * @brief Create pipeline layout
     */
    bool CreatePipelineLayout();

    /**
     * @brief Extract material UBO layout (named "MaterialProperties" or binding 2)
     */
    void ExtractMaterialUBOLayout();

    /**
     * @brief Validate vertex output / fragment input interface compatibility
     * @return true if interfaces are compatible, false if there is a type mismatch
     */
    bool ValidateStageInterface() const;
};

/// Immutable, revision-carrying GPU shader publication. ShaderProgram is
/// mutable only while Create() builds a complete candidate; every cache and
/// renderer consumer receives this const shared owner after publication.
using ShaderProgramPublication = std::shared_ptr<const ShaderProgram>;

/**
 * @brief ShaderProgramCache - Cache for shader programs
 *
 * Manages shader program creation and caching by shader ID.
 */
class ShaderProgramCache
{
  public:
    ShaderProgramCache() = default;
    ~ShaderProgramCache() = default;

    // Non-copyable
    ShaderProgramCache(const ShaderProgramCache &) = delete;
    ShaderProgramCache &operator=(const ShaderProgramCache &) = delete;

    /**
     * @brief Initialize the cache
     */
    void Initialize(VkDevice device);

    /**
     * @brief Shutdown and cleanup all programs
     */
    void Shutdown();

    /**
     * @brief Get or create a shader program
     */
    ShaderProgramPublication GetOrCreateProgram(const ShaderProgramKey &programKey, const std::vector<char> &vertSpirv,
                                                const std::vector<char> &fragSpirv);
    ShaderProgramPublication GetOrCreateProgram(const ShaderProgramVariantKey &variantKey,
                                                const std::vector<char> &vertSpirv, const std::vector<char> &fragSpirv);

    /**
     * @brief Get existing program
     */
    ShaderProgramPublication GetProgram(const ShaderProgramKey &programKey) const;
    ShaderProgramPublication GetProgram(const ShaderProgramVariantKey &variantKey) const;

    /**
     * @brief Check if program exists
     */
    bool HasProgram(const ShaderProgramKey &programKey) const;
    bool HasProgram(const ShaderProgramVariantKey &variantKey) const;

    [[nodiscard]] ShaderProgramPublication TakeProgram(const ShaderProgramKey &programKey);
    [[nodiscard]] ShaderProgramPublication TakeProgram(const ShaderProgramVariantKey &variantKey);

    /// Transfer every semantic pass program belonging to one artifact revision.
    [[nodiscard]] std::vector<ShaderProgramPublication> TakePrograms(const ShaderProgramKey &programKey);

    /**
     * @brief Transfer ownership of all programs using the specified shader.
     * @param shaderName Simple
     * shader name (e.g., "123", not full path)
     */
    [[nodiscard]] std::vector<ShaderProgramPublication> TakeProgramsContainingShader(const std::string &shaderName);

    /**
     * @brief Clear all cached programs
     */
    void Clear();

  private:
    VkDevice m_device = VK_NULL_HANDLE;
    std::unordered_map<ShaderProgramVariantKey, ShaderProgramPublication, ShaderProgramVariantKeyHash> m_programs;
    std::unordered_set<ShaderProgramVariantKey, ShaderProgramVariantKeyHash> m_failedPrograms;
};

} // namespace infernux
