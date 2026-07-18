#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux
{

/**
 * @brief Type of shader resource binding
 */
enum class ShaderResourceType
{
    UniformBuffer,
    StorageBuffer,
    SampledImage, // Combined image sampler
    StorageImage,
    Sampler,
    InputAttachment,
    PushConstant
};

/**
 * @brief A single uniform member within a UBO
 */
struct UniformMember
{
    std::string name;
    uint32_t offset;
    uint32_t size;
    uint32_t arraySize; // 1 for non-array
    VkFormat format;    // Inferred from type
};

/**
 * @brief A uniform buffer descriptor
 */
struct UniformBufferInfo
{
    std::string name;
    uint32_t binding;
    uint32_t set;
    uint32_t size;
    VkShaderStageFlags stageFlags;
    std::vector<UniformMember> members;
};

/**
 * @brief A sampled image (texture) descriptor
 */
struct SampledImageInfo
{
    std::string name;
    uint32_t binding;
    uint32_t set;
    uint32_t arraySize; // For texture arrays
    VkShaderStageFlags stageFlags;
};

/**
 * @brief Push constant range info
 */
struct PushConstantInfo
{
    std::string name;
    uint32_t offset;
    uint32_t size;
    VkShaderStageFlags stageFlags;
    std::vector<UniformMember> members;
};

/**
 * @brief Shader stage input/output
 */
struct ShaderIOVariable
{
    std::string name;
    uint32_t location;
    VkFormat format = VK_FORMAT_UNDEFINED;
};

/**
 * @brief ShaderReflection - Extract resource info from compiled SPIR-V
 *
 * Uses SPIRV-Cross to reflect shader resources including:
 * - Uniform buffers and their members
 * - Sampled images (textures)
 * - Push constants
 * - Input/Output variables
 */
class ShaderReflection
{
  public:
    ShaderReflection() = default;
    ~ShaderReflection() = default;

    /**
     * @brief Reflect a SPIR-V shader
     * @param spirvCode The compiled SPIR-V bytecode
     * @param stage The shader stage (for stage flags)
     * @return true if reflection succeeded
     */
    bool Reflect(const std::vector<uint32_t> &spirvCode, VkShaderStageFlagBits stage);

    /**
     * @brief Reflect from a vector<char> (common format from file loading)
     */
    bool Reflect(const std::vector<char> &spirvCode, VkShaderStageFlagBits stage);

    // Getters for reflected data
    [[nodiscard]] const std::vector<UniformBufferInfo> &GetUniformBuffers() const
    {
        return m_uniformBuffers;
    }
    [[nodiscard]] const std::vector<SampledImageInfo> &GetSampledImages() const
    {
        return m_sampledImages;
    }
    [[nodiscard]] const std::vector<PushConstantInfo> &GetPushConstants() const
    {
        return m_pushConstants;
    }
    [[nodiscard]] const std::vector<ShaderIOVariable> &GetInputs() const
    {
        return m_inputs;
    }
    [[nodiscard]] const std::vector<ShaderIOVariable> &GetOutputs() const
    {
        return m_outputs;
    }

    [[nodiscard]] VkShaderStageFlagBits GetStage() const
    {
        return m_stage;
    }

    /**
     * @brief Get all descriptor set layout bindings
     * @param set The descriptor set index
     */
    [[nodiscard]] std::vector<VkDescriptorSetLayoutBinding> GetDescriptorSetLayoutBindings(uint32_t set = 0) const;

    /**
     * @brief Get all unique descriptor sets used
     */
    [[nodiscard]] std::vector<uint32_t> GetUsedDescriptorSets() const;

    /**
     * @brief Clear all reflected data
     */
    void Clear();

  private:
    VkShaderStageFlagBits m_stage = VK_SHADER_STAGE_VERTEX_BIT;

    std::vector<UniformBufferInfo> m_uniformBuffers;
    std::vector<SampledImageInfo> m_sampledImages;
    std::vector<PushConstantInfo> m_pushConstants;
    std::vector<ShaderIOVariable> m_inputs;
    std::vector<ShaderIOVariable> m_outputs;
};

} // namespace infernux
