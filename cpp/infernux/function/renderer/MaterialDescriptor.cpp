#include "MaterialDescriptor.h"
#include <algorithm>
#include <core/config/EngineConfig.h>
#include <core/log/InxLog.h>
#include <cstring>

namespace infernux
{

namespace
{

constexpr std::string_view kSceneUBOName = "UniformBufferObject";
constexpr std::string_view kLightingUBOName = "LightingUBO";

bool IsSceneUBOBinding(const MergedDescriptorBinding &binding)
{
    return binding.set == 0 && (binding.name == kSceneUBOName || binding.binding == 0);
}

bool IsLightingUBOBinding(const MergedDescriptorBinding &binding)
{
    return binding.set == 0 && (binding.name == kLightingUBOName || binding.binding == 1);
}

void AppendBufferWrite(std::vector<VkWriteDescriptorSet> &writes, std::vector<VkDescriptorBufferInfo> &bufferInfos,
                       VkDescriptorSet dstSet, uint32_t binding, VkDescriptorType descriptorType,
                       const VkDescriptorBufferInfo &bufferInfo, uint32_t descriptorCount = 1)
{
    bufferInfos.push_back(bufferInfo);

    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = dstSet;
    write.dstBinding = binding;
    write.dstArrayElement = 0;
    write.descriptorCount = descriptorCount;
    write.descriptorType = descriptorType;
    write.pBufferInfo = &bufferInfos.back();
    writes.push_back(write);
}

void AppendImageWrite(std::vector<VkWriteDescriptorSet> &writes, std::vector<VkDescriptorImageInfo> &imageInfos,
                      VkDescriptorSet dstSet, uint32_t binding, VkImageView imageView, VkSampler sampler,
                      VkImageLayout imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL)
{
    VkDescriptorImageInfo imageInfo{};
    imageInfo.imageLayout = imageLayout;
    imageInfo.imageView = imageView;
    imageInfo.sampler = sampler;
    imageInfos.push_back(imageInfo);

    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = dstSet;
    write.dstBinding = binding;
    write.dstArrayElement = 0;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    write.pImageInfo = &imageInfos.back();
    writes.push_back(write);
}

bool HasSameGpuBinding(const MaterialDescriptorSet::TextureBinding &left,
                       const MaterialDescriptorSet::TextureBinding &right)
{
    return left.imageView == right.imageView && left.sampler == right.sampler;
}

} // namespace

// ============================================================================
// MaterialUBO Implementation
// ============================================================================

MaterialUBO::~MaterialUBO()
{
    Destroy();
}

bool MaterialUBO::Create(VmaAllocator allocator, VkDevice device, const MaterialUBOLayout &layout)
{
    m_allocator = allocator;
    m_device = device;
    m_layout = layout;
    m_size = layout.size;

    if (m_size == 0) {
        INXLOG_WARN("Creating MaterialUBO with size 0");
        return true;
    }

    // Create buffer via VMA
    VkBufferCreateInfo bufferInfo{};
    bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bufferInfo.size = m_size;
    bufferInfo.usage = VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT;
    bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

    VmaAllocationCreateInfo allocCreateInfo{};
    allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
    allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
    allocCreateInfo.requiredFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;

    VmaAllocationInfo allocInfo{};
    VkResult result = vmaCreateBuffer(allocator, &bufferInfo, &allocCreateInfo, &m_buffer, &m_allocation, &allocInfo);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create material UBO buffer via VMA");
        return false;
    }

    m_mappedData = allocInfo.pMappedData;

    // Zero-initialize
    std::memset(m_mappedData, 0, m_size);

    INXLOG_DEBUG("Created MaterialUBO with size ", m_size, " bytes");
    return true;
}

void MaterialUBO::Destroy()
{
    if (m_buffer != VK_NULL_HANDLE && m_allocator != VK_NULL_HANDLE) {
        m_mappedData = nullptr;
        vmaDestroyBuffer(m_allocator, m_buffer, m_allocation);
        m_buffer = VK_NULL_HANDLE;
        m_allocation = VK_NULL_HANDLE;
    }

    m_allocator = VK_NULL_HANDLE;
    m_device = VK_NULL_HANDLE;
}

void MaterialUBO::Update(const InxMaterial &material)
{
    if (!m_mappedData || m_size == 0) {
        return;
    }

    const auto &properties = material.GetAllProperties();

    for (const auto &[name, prop] : properties) {
        uint32_t offset, size;
        if (!m_layout.GetMemberInfo(name, offset, size)) {
            continue; // Property not in UBO layout
        }

        switch (prop.type) {
        case MaterialPropertyType::Float: {
            float value = std::get<float>(prop.value);
            WriteData(offset, &value, sizeof(float));
            break;
        }
        case MaterialPropertyType::Float2: {
            glm::vec2 value = std::get<glm::vec2>(prop.value);
            WriteData(offset, &value, sizeof(glm::vec2));
            break;
        }
        case MaterialPropertyType::Float3: {
            glm::vec3 value = std::get<glm::vec3>(prop.value);
            WriteData(offset, &value, sizeof(glm::vec3));
            break;
        }
        case MaterialPropertyType::Float4:
        case MaterialPropertyType::Color: {
            glm::vec4 value = std::get<glm::vec4>(prop.value);
            WriteData(offset, &value, sizeof(glm::vec4));
            break;
        }
        case MaterialPropertyType::Int: {
            int value = std::get<int>(prop.value);
            WriteData(offset, &value, sizeof(int));
            break;
        }
        case MaterialPropertyType::Mat4: {
            glm::mat4 value = std::get<glm::mat4>(prop.value);
            WriteData(offset, &value, sizeof(glm::mat4));
            break;
        }
        case MaterialPropertyType::Texture2D:
            // Textures are bound separately, not in UBO
            break;
        }
    }
}

void MaterialUBO::WriteData(uint32_t offset, const void *data, uint32_t size)
{
    if (!m_mappedData || offset + size > m_size) {
        INXLOG_WARN("MaterialUBO write out of bounds: offset=", offset, " size=", size, " bufferSize=", m_size);
        return;
    }

    std::memcpy(static_cast<char *>(m_mappedData) + offset, data, size);
}

void MaterialUBO::SetFloat(const std::string &name, float value)
{
    uint32_t offset, size;
    if (m_layout.GetMemberInfo(name, offset, size)) {
        WriteData(offset, &value, sizeof(float));
    }
}

void MaterialUBO::SetVec2(const std::string &name, const glm::vec2 &value)
{
    uint32_t offset, size;
    if (m_layout.GetMemberInfo(name, offset, size)) {
        WriteData(offset, &value, sizeof(glm::vec2));
    }
}

void MaterialUBO::SetVec3(const std::string &name, const glm::vec3 &value)
{
    uint32_t offset, size;
    if (m_layout.GetMemberInfo(name, offset, size)) {
        WriteData(offset, &value, sizeof(glm::vec3));
    }
}

void MaterialUBO::SetVec4(const std::string &name, const glm::vec4 &value)
{
    uint32_t offset, size;
    if (m_layout.GetMemberInfo(name, offset, size)) {
        WriteData(offset, &value, sizeof(glm::vec4));
    }
}

void MaterialUBO::SetInt(const std::string &name, int value)
{
    uint32_t offset, size;
    if (m_layout.GetMemberInfo(name, offset, size)) {
        WriteData(offset, &value, sizeof(int));
    }
}

void MaterialUBO::SetMat4(const std::string &name, const glm::mat4 &value)
{
    uint32_t offset, size;
    if (m_layout.GetMemberInfo(name, offset, size)) {
        WriteData(offset, &value, sizeof(glm::mat4));
    }
}

// ============================================================================
// MaterialDescriptorManager Implementation
// ============================================================================

MaterialDescriptorManager::~MaterialDescriptorManager()
{
    Shutdown();
}

void MaterialDescriptorManager::Initialize(VmaAllocator allocator, VkDevice device, VkPhysicalDevice physicalDevice,
                                           uint32_t maxMaterials)
{
    m_vmaAllocator = allocator;
    m_device = device;
    m_physicalDevice = physicalDevice;
    m_poolPageSize = maxMaterials;

    if (CreateDescriptorPool(maxMaterials) == VK_NULL_HANDLE) {
        INXLOG_ERROR("Failed to create material descriptor pool");
    }
}

void MaterialDescriptorManager::Shutdown()
{
    Clear();

    if (m_device != VK_NULL_HANDLE) {
        for (auto pool : m_descriptorPools) {
            if (pool != VK_NULL_HANDLE) {
                vkDestroyDescriptorPool(m_device, pool, nullptr);
            }
        }
    }
    m_descriptorPools.clear();

    m_device = VK_NULL_HANDLE;
    m_physicalDevice = VK_NULL_HANDLE;
    m_liveDescriptorHandles.clear();
}

VkDescriptorPool MaterialDescriptorManager::CreateDescriptorPool(uint32_t maxMaterials)
{
    const EngineConfig &config = EngineConfig::Get();
    const uint32_t uboDescriptorsPerMaterial = std::max(1u, config.uboDescriptorsPerMaterial);
    const uint32_t samplerDescriptorsPerMaterial = std::max(1u, config.samplerDescriptorsPerMaterial);

    std::vector<VkDescriptorPoolSize> poolSizes = {
        {VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, maxMaterials * uboDescriptorsPerMaterial},
        {VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, maxMaterials * samplerDescriptorsPerMaterial},
    };

    VkDescriptorPoolCreateFlags poolFlags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    if (m_updateAfterBindEnabled) {
        // Required when any layout allocated from this pool carries
        // VK_DESCRIPTOR_SET_LAYOUT_CREATE_UPDATE_AFTER_BIND_POOL_BIT
        // (see ShaderProgram::CreateDescriptorSetLayouts).
        poolFlags |= VK_DESCRIPTOR_POOL_CREATE_UPDATE_AFTER_BIND_BIT;
    }

    VkDescriptorPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    poolInfo.flags = poolFlags;
    poolInfo.maxSets = maxMaterials;
    poolInfo.poolSizeCount = static_cast<uint32_t>(poolSizes.size());
    poolInfo.pPoolSizes = poolSizes.data();

    VkDescriptorPool pool = VK_NULL_HANDLE;
    if (vkCreateDescriptorPool(m_device, &poolInfo, nullptr, &pool) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create material descriptor pool");
        return VK_NULL_HANDLE;
    }

    m_descriptorPools.push_back(pool);
    return pool;
}

bool MaterialDescriptorManager::IsPlaceholderTexturePath(std::string_view texturePath) const
{
    return texturePath == "white" || texturePath == "black" || texturePath == "normal";
}

bool MaterialDescriptorManager::IsNormalBindingName(std::string_view bindingName) const
{
    return bindingName.find("normal") != std::string_view::npos || bindingName.find("Normal") != std::string_view::npos;
}

bool MaterialDescriptorManager::TryGetDefaultTextureBinding(std::string_view bindingName,
                                                            MaterialDescriptorSet::TextureBinding &outBinding) const
{
    if (IsNormalBindingName(bindingName) && m_defaultNormalImageView != VK_NULL_HANDLE &&
        m_defaultNormalSampler != VK_NULL_HANDLE) {
        outBinding = {m_defaultNormalImageView, m_defaultNormalSampler};
        return true;
    }

    if (m_defaultImageView != VK_NULL_HANDLE && m_defaultSampler != VK_NULL_HANDLE) {
        outBinding = {m_defaultImageView, m_defaultSampler};
        return true;
    }

    outBinding = {};
    return false;
}

TextureResolveStatus
MaterialDescriptorManager::ResolveExplicitTextureBinding(const std::string &texturePath, const std::string &bindingName,
                                                         MaterialDescriptorSet::TextureBinding &outBinding) const
{
    if (!m_textureResolver || texturePath.empty()) {
        return TextureResolveStatus::Failed;
    }

    TextureResolveResult result = m_textureResolver(texturePath, bindingName);
    if (result.status != TextureResolveStatus::Ready) {
        outBinding = {};
        return result.status;
    }

    outBinding = std::move(result.binding);
    if (outBinding.imageView == VK_NULL_HANDLE || outBinding.sampler == VK_NULL_HANDLE || !outBinding.keepAlive) {
        outBinding = {};
        INXLOG_ERROR("Texture resolver returned Ready without a complete GPU binding for texture '", texturePath,
                     "' (binding='", bindingName, "')");
        return TextureResolveStatus::Failed;
    }
    return TextureResolveStatus::Ready;
}

MaterialDescriptorSet *MaterialDescriptorManager::GetOrCreateDescriptorSet(const InxMaterial &material,
                                                                           const ShaderProgram &program,
                                                                           VkBuffer sceneUBO, VkDeviceSize sceneUBOSize,
                                                                           VkBuffer lightingUBO,
                                                                           VkDeviceSize lightingUBOSize)
{
    const std::string materialName = material.GetMaterialKey();

    VkDescriptorSetLayout requiredLayout = program.GetDescriptorSetLayout(0);
    if (requiredLayout == VK_NULL_HANDLE) {
        INXLOG_ERROR("Shader program has no descriptor set layout");
        return nullptr;
    }

    // Check if already exists AND uses the same layout
    auto it = m_descriptorSets.find(materialName);
    if (it != m_descriptorSets.end() && it->second->isValid) {
        const MaterialUBOLayout *requiredMaterialLayout = program.GetMaterialUBOLayout();
        const MaterialUBOLayout *requiredVertexMaterialLayout = program.GetVertexMaterialUBOLayout();
        bool needsMaterialUBO = requiredMaterialLayout != nullptr && requiredMaterialLayout->size > 0;
        bool needsVertexMaterialUBO = requiredVertexMaterialLayout != nullptr && requiredVertexMaterialLayout->size > 0;
        bool hasMaterialUBO = it->second->materialUBO && it->second->materialUBO->IsValid();
        bool hasVertexMaterialUBO = it->second->vertexMaterialUBO && it->second->vertexMaterialUBO->IsValid();

        // CRITICAL: Must verify layout matches - shader may have changed
        if (it->second->layout == requiredLayout && needsMaterialUBO == hasMaterialUBO &&
            needsVertexMaterialUBO == hasVertexMaterialUBO) {
            INXLOG_DEBUG("GetOrCreateDescriptorSet: REUSING cached descriptor for '", materialName, "'");
            return it->second.get();
        } else {
            INXLOG_INFO("Material '", materialName, "' descriptor requirements changed, recreating descriptor set");
            auto staleEntry = std::shared_ptr<MaterialDescriptorSet>(std::move(it->second));
            if (staleEntry && staleEntry->descriptorSet != VK_NULL_HANDLE) {
                m_liveDescriptorHandles.erase(reinterpret_cast<uint64_t>(staleEntry->descriptorSet));
            }
            m_descriptorSets.erase(it);
            RetireDescriptorSet(std::move(staleEntry));
        }
    }

    // Create new descriptor set
    auto matDescSet = std::make_unique<MaterialDescriptorSet>();
    matDescSet->layout = requiredLayout; // Track which layout we're using

    // Allocate descriptor set from the most recent pool; grow if exhausted.
    VkDescriptorSetAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    allocInfo.descriptorSetCount = 1;
    allocInfo.pSetLayouts = &requiredLayout;

    VkDescriptorPool activePool = m_descriptorPools.empty() ? VK_NULL_HANDLE : m_descriptorPools.back();
    allocInfo.descriptorPool = activePool;

    VkResult allocResult = (activePool != VK_NULL_HANDLE)
                               ? vkAllocateDescriptorSets(m_device, &allocInfo, &matDescSet->descriptorSet)
                               : VK_ERROR_OUT_OF_POOL_MEMORY;

    if (allocResult != VK_SUCCESS) {
        // Pool exhausted — allocate a new pool page and retry
        INXLOG_WARN("Descriptor pool exhausted, growing pool chain (page ", m_descriptorPools.size(), ")");
        activePool = CreateDescriptorPool(m_poolPageSize);
        if (activePool == VK_NULL_HANDLE) {
            INXLOG_ERROR("Failed to grow descriptor pool for material: ", materialName);
            return nullptr;
        }
        allocInfo.descriptorPool = activePool;
        allocResult = vkAllocateDescriptorSets(m_device, &allocInfo, &matDescSet->descriptorSet);
        if (allocResult != VK_SUCCESS) {
            INXLOG_ERROR("Failed to allocate descriptor set even after pool growth for material: ", materialName);
            return nullptr;
        }
    }
    matDescSet->ownerPool = activePool;

    // Track this handle so callers can verify it's still live before binding.
    m_allocatedHandles.insert(reinterpret_cast<uint64_t>(matDescSet->descriptorSet));
    m_liveDescriptorHandles.insert(reinterpret_cast<uint64_t>(matDescSet->descriptorSet));

    // Create material UBO if shader has one
    const MaterialUBOLayout *uboLayout = program.GetMaterialUBOLayout();
    if (uboLayout != nullptr && uboLayout->size > 0) {
        matDescSet->materialUBO = std::make_unique<MaterialUBO>();
        if (!matDescSet->materialUBO->Create(m_vmaAllocator, m_device, *uboLayout)) {
            INXLOG_ERROR("Failed to create material UBO for: ", materialName);
        } else {
            // Update UBO with current material values
            matDescSet->materialUBO->Update(material);
        }
    }

    // Create vertex-stage material UBO if the vertex shader declares @property fields (binding 14)
    const MaterialUBOLayout *vertUboLayout = program.GetVertexMaterialUBOLayout();
    if (vertUboLayout != nullptr && vertUboLayout->size > 0) {
        matDescSet->vertexMaterialUBO = std::make_unique<MaterialUBO>();
        if (!matDescSet->vertexMaterialUBO->Create(m_vmaAllocator, m_device, *vertUboLayout)) {
            INXLOG_ERROR("Failed to create vertex material UBO for: ", materialName);
        } else {
            matDescSet->vertexMaterialUBO->Update(material);
        }
    }

    // Update descriptor bindings
    UpdateDescriptorBindings(*matDescSet, program, sceneUBO, sceneUBOSize, lightingUBO, lightingUBOSize);

    // Resolve material Texture2D properties → actual GPU textures
    if (m_textureResolver) {
        const auto &properties = material.GetAllProperties();
        const auto &bindings = program.GetDescriptorBindings();

        // Collect all descriptor writes and flush as a single batch
        std::vector<VkWriteDescriptorSet> texWrites;
        std::vector<VkDescriptorImageInfo> texImageInfos;
        texWrites.reserve(properties.size());
        texImageInfos.reserve(properties.size());

        for (const auto &[propName, prop] : properties) {
            if (prop.type != MaterialPropertyType::Texture2D) {
                continue;
            }

            // Get the texture path from the property value
            const std::string *texturePath = std::get_if<std::string>(&prop.value);
            if (!texturePath || texturePath->empty()) {
                continue;
            }
            const bool isPlaceholderTexture = IsPlaceholderTexturePath(*texturePath);

            // Find the matching sampler binding by name (set 0 only)
            for (const auto &binding : bindings) {
                if (binding.set != 0) {
                    continue;
                }
                if (binding.type != VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER) {
                    continue;
                }

                // Match property name to sampler name from shader reflection
                if (binding.name == propName) {
                    MaterialDescriptorSet::TextureBinding resolvedBinding{};
                    const TextureResolveStatus resolveStatus =
                        isPlaceholderTexture
                            ? TextureResolveStatus::Pending
                            : ResolveExplicitTextureBinding(*texturePath, binding.name, resolvedBinding);
                    const bool resolvedExplicit = resolveStatus == TextureResolveStatus::Ready;

                    if (!isPlaceholderTexture && resolveStatus == TextureResolveStatus::Pending) {
                        matDescSet->hasPendingTextures = true;
                    }

                    if (!resolvedExplicit && !TryGetDefaultTextureBinding(binding.name, resolvedBinding)) {
                        matDescSet->textureBindings.erase(binding.binding);
                        break;
                    }

                    matDescSet->textureBindings[binding.binding] = resolvedBinding;

                    if (resolvedExplicit) {
                        INXLOG_DEBUG("Bound texture '", *texturePath, "' to binding ", binding.binding,
                                     " for material '", materialName, "'");
                    } else if (resolveStatus == TextureResolveStatus::Failed) {
                        INXLOG_WARN("Failed to resolve texture '", *texturePath, "' for material '", materialName,
                                    "' property '", propName, "' — binding default texture");
                    }

                    AppendImageWrite(texWrites, texImageInfos, matDescSet->descriptorSet, binding.binding,
                                     resolvedBinding.imageView, resolvedBinding.sampler);
                    break;
                }
            }
        }

        if (!texWrites.empty()) {
            vkUpdateDescriptorSets(m_device, static_cast<uint32_t>(texWrites.size()), texWrites.data(), 0, nullptr);
        }
    }

    matDescSet->isValid = true;

    MaterialDescriptorSet *result = matDescSet.get();
    m_descriptorSets[materialName] = std::move(matDescSet);

    INXLOG_DEBUG("Created descriptor set for material: ", materialName);
    return result;
}

void MaterialDescriptorManager::UpdateDescriptorBindings(MaterialDescriptorSet &matDescSet,
                                                         const ShaderProgram &program, VkBuffer sceneUBO,
                                                         VkDeviceSize sceneUBOSize, VkBuffer lightingUBO,
                                                         VkDeviceSize lightingUBOSize)
{
    std::vector<VkWriteDescriptorSet> writes;
    std::vector<VkDescriptorBufferInfo> bufferInfos;
    std::vector<VkDescriptorImageInfo> imageInfos;

    // Reserve space to avoid reallocation invalidating pointers
    const auto &bindings = program.GetDescriptorBindings();
    bufferInfos.reserve(bindings.size());
    imageInfos.reserve(bindings.size());

    INXLOG_DEBUG("UpdateDescriptorBindings: ", bindings.size(),
                 " bindings, lightingUBO=", (lightingUBO != VK_NULL_HANDLE ? "valid" : "null"),
                 ", lightingUBOSize=", lightingUBOSize);

    for (const auto &binding : bindings) {
        // Only write set 0 bindings into the material descriptor set.
        // Set 1 (per-view shadow map) is handled separately per render graph.
        if (binding.set != 0) {
            continue;
        }

        if (binding.type == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER) {
            VkDescriptorBufferInfo bufferInfo{};

            INXLOG_DEBUG("  Binding ", binding.binding, ": UBO");

            // Use shader reflection to distinguish material UBO from scene/lighting UBOs.
            // The material UBO binding varies per shader type (e.g. binding 1 for unlit,
            // binding 3 for lit), so we must check reflection rather than hardcoding.
            const MaterialUBOLayout *matLayout = program.GetMaterialUBOLayout();
            bool isMaterialUBOBinding = matLayout && matLayout->size > 0 && binding.binding == matLayout->binding;

            const MaterialUBOLayout *vertMatLayout = program.GetVertexMaterialUBOLayout();
            bool isVertexMaterialUBOBinding =
                vertMatLayout && vertMatLayout->size > 0 && binding.binding == vertMatLayout->binding;

            if (isVertexMaterialUBOBinding && matDescSet.vertexMaterialUBO && matDescSet.vertexMaterialUBO->IsValid()) {
                // Vertex-stage material UBO at binding 14
                bufferInfo.buffer = matDescSet.vertexMaterialUBO->GetBuffer();
                bufferInfo.offset = 0;
                bufferInfo.range = matDescSet.vertexMaterialUBO->GetSize();
            } else if (isMaterialUBOBinding && matDescSet.materialUBO && matDescSet.materialUBO->IsValid()) {
                // Material UBO — identified by shader reflection binding number
                bufferInfo.buffer = matDescSet.materialUBO->GetBuffer();
                bufferInfo.offset = 0;
                bufferInfo.range = matDescSet.materialUBO->GetSize();
            } else if (IsSceneUBOBinding(binding)) {
                bufferInfo.buffer = sceneUBO;
                bufferInfo.offset = 0;
                bufferInfo.range = sceneUBOSize;
            } else if (IsLightingUBOBinding(binding) && lightingUBO != VK_NULL_HANDLE) {
                // Lighting UBO is identified by reflected name first and only
                // falls back to binding=1 for legacy/generated shader layouts.
                INXLOG_DEBUG("    -> Binding LightingUBO, size=", lightingUBOSize);
                bufferInfo.buffer = lightingUBO;
                bufferInfo.offset = 0;
                bufferInfo.range = lightingUBOSize;
            } else if (matDescSet.materialUBO && matDescSet.materialUBO->IsValid()) {
                // Fallback: any remaining UBO binding -> material UBO
                bufferInfo.buffer = matDescSet.materialUBO->GetBuffer();
                bufferInfo.offset = 0;
                bufferInfo.range = matDescSet.materialUBO->GetSize();
            } else {
                continue; // Skip if no valid buffer
            }

            AppendBufferWrite(writes, bufferInfos, matDescSet.descriptorSet, binding.binding, binding.type, bufferInfo,
                              binding.descriptorCount);
        } else if (binding.type == VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER) {
            // Check if we have a texture bound for this slot
            auto texIt = matDescSet.textureBindings.find(binding.binding);
            MaterialDescriptorSet::TextureBinding textureBinding{};
            if (texIt != matDescSet.textureBindings.end()) {
                textureBinding = texIt->second;
            } else if (!TryGetDefaultTextureBinding(binding.name, textureBinding)) {
                continue; // Skip if no valid image
            }

            AppendImageWrite(writes, imageInfos, matDescSet.descriptorSet, binding.binding, textureBinding.imageView,
                             textureBinding.sampler);
        }
    }

    if (!writes.empty()) {
        vkUpdateDescriptorSets(m_device, static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);
    }
}

void MaterialDescriptorManager::UpdateMaterialUBO(const std::string &materialName, const InxMaterial &material)
{
    auto it = m_descriptorSets.find(materialName);
    if (it != m_descriptorSets.end()) {
        if (it->second->materialUBO) {
            it->second->materialUBO->Update(material);
        }
        if (it->second->vertexMaterialUBO) {
            it->second->vertexMaterialUBO->Update(material);
        }
    }
}

void MaterialDescriptorManager::ResolveTextureProperties(const std::string &materialName, const InxMaterial &material,
                                                          const std::vector<MergedDescriptorBinding> &bindings)
{
    if (!m_textureResolver) {
        return;
    }

    auto it = m_descriptorSets.find(materialName);
    if (it == m_descriptorSets.end() || !it->second->isValid) {
        return;
    }

    auto &matDescSet = *it->second;
    matDescSet.hasPendingTextures = false;
    const auto &properties = material.GetAllProperties();

    // Collect all descriptor writes into a single batch
    std::vector<VkWriteDescriptorSet> writes;
    std::vector<VkDescriptorImageInfo> imageInfos;
    writes.reserve(properties.size());
    imageInfos.reserve(properties.size());

    for (const auto &[propName, prop] : properties) {
        if (prop.type != MaterialPropertyType::Texture2D) {
            continue;
        }

        const std::string *texturePath = std::get_if<std::string>(&prop.value);

        for (const auto &binding : bindings) {
            if (binding.set != 0 || binding.type != VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER) {
                continue;
            }
            if (binding.name == propName) {
                MaterialDescriptorSet::TextureBinding resolvedBinding{};

                if (!texturePath || texturePath->empty()) {
                    if (TryGetDefaultTextureBinding(binding.name, resolvedBinding)) {
                        matDescSet.textureBindings[binding.binding] = resolvedBinding;
                        AppendImageWrite(writes, imageInfos, matDescSet.descriptorSet, binding.binding,
                                         resolvedBinding.imageView, resolvedBinding.sampler);
                    } else {
                        matDescSet.textureBindings.erase(binding.binding);
                    }
                    INXLOG_DEBUG("Cleared texture binding ", binding.binding, " for material '", materialName,
                                 "' property '", propName, "' -> rebound default texture");
                    break;
                }

                const bool isPlaceholder = IsPlaceholderTexturePath(*texturePath);
                const TextureResolveStatus resolveStatus =
                    isPlaceholder ? TextureResolveStatus::Pending
                                  : ResolveExplicitTextureBinding(*texturePath, binding.name, resolvedBinding);
                const bool resolvedExplicit = resolveStatus == TextureResolveStatus::Ready;
                const bool hasBinding = resolvedExplicit || TryGetDefaultTextureBinding(binding.name, resolvedBinding);

                if (!isPlaceholder && resolveStatus == TextureResolveStatus::Pending) {
                    matDescSet.hasPendingTextures = true;
                }

                if (hasBinding) {
                    const auto previous = matDescSet.textureBindings.find(binding.binding);
                    const bool bindingChanged = previous == matDescSet.textureBindings.end() ||
                                                !HasSameGpuBinding(previous->second, resolvedBinding);
                    matDescSet.textureBindings[binding.binding] = resolvedBinding;
                    if (bindingChanged) {
                        AppendImageWrite(writes, imageInfos, matDescSet.descriptorSet, binding.binding,
                                         resolvedBinding.imageView, resolvedBinding.sampler);
                    }
                    if (resolvedExplicit) {
                        INXLOG_DEBUG("Re-bound texture '", *texturePath, "' to binding ", binding.binding,
                                     " for material '", materialName, "'");
                    }
                } else {
                    if (resolveStatus == TextureResolveStatus::Failed) {
                        INXLOG_WARN("Failed to resolve texture '", *texturePath, "' for material '", materialName,
                                    "' property '", propName, "' — binding default texture");
                    }
                    matDescSet.textureBindings.erase(binding.binding);
                }
                break;
            }
        }
    }

    if (!writes.empty()) {
        WaitForGpuIdleBeforeSharedDescriptorWrite();
        vkUpdateDescriptorSets(m_device, static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);
    }
}

bool MaterialDescriptorManager::HasPendingTextureProperties(const std::string &materialName) const
{
    const auto it = m_descriptorSets.find(materialName);
    return it != m_descriptorSets.end() && it->second && it->second->isValid && it->second->hasPendingTextures;
}

void MaterialDescriptorManager::BindTexture(const std::string &materialName, uint32_t binding, VkImageView imageView,
                                            VkSampler sampler)
{
    auto it = m_descriptorSets.find(materialName);
    if (it == m_descriptorSets.end())
        return;

    it->second->textureBindings[binding] = {imageView, sampler};

    VkDescriptorImageInfo imageInfo{};
    imageInfo.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    imageInfo.imageView = imageView;
    imageInfo.sampler = sampler;

    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = it->second->descriptorSet;
    write.dstBinding = binding;
    write.dstArrayElement = 0;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    write.pImageInfo = &imageInfo;

    WaitForGpuIdleBeforeSharedDescriptorWrite();
    vkUpdateDescriptorSets(m_device, 1, &write, 0, nullptr);
}

void MaterialDescriptorManager::WaitForGpuIdleBeforeSharedDescriptorWrite()
{
    // Fast path: when descriptor indexing UPDATE_AFTER_BIND is enabled,
    // the driver tracks descriptor liveness internally and lets us rewrite
    // bindings that are currently in command buffers in flight. No drain
    // is required and the editor texture-edit hot path becomes truly free.
    if (m_updateAfterBindEnabled) {
        return;
    }

    // Legacy fallback: layouts/pools were created without the descriptor-
    // indexing flags (older GPU or feature unsupported), so we still have
    // to drain the device before mutating a shared descriptor set.
    if (m_device != VK_NULL_HANDLE) {
        vkDeviceWaitIdle(m_device);
    }
}

void MaterialDescriptorManager::RemoveDescriptorSet(const std::string &materialName)
{
    auto it = m_descriptorSets.find(materialName);
    if (it != m_descriptorSets.end()) {
        auto retiredEntry = std::shared_ptr<MaterialDescriptorSet>(std::move(it->second));
        if (retiredEntry && retiredEntry->descriptorSet != VK_NULL_HANDLE) {
            const uint64_t handle = reinterpret_cast<uint64_t>(retiredEntry->descriptorSet);
            m_liveDescriptorHandles.erase(handle);
            m_allocatedHandles.erase(handle);
        }
        m_descriptorSets.erase(it);
        RetireDescriptorSet(std::move(retiredEntry));
    }
}

void MaterialDescriptorManager::RetireDescriptorSet(std::shared_ptr<MaterialDescriptorSet> descriptorSet)
{
    if (!descriptorSet)
        throw std::invalid_argument("Cannot retire an empty material descriptor set");

    const VkDevice device = m_device;
    const VkDescriptorPool ownerPool = descriptorSet->ownerPool;
    const VkDescriptorSet handle = descriptorSet->descriptorSet;
    auto pending = m_pendingDescriptorSetReleases;

    if (m_deletionQueue) {
        pending->fetch_add(1, std::memory_order_relaxed);
        m_deletionQueue->Push([device, ownerPool, handle, descriptorSet = std::move(descriptorSet),
                               pending = std::move(pending)]() mutable {
            vkFreeDescriptorSets(device, ownerPool, 1, &handle);
            descriptorSet->descriptorSet = VK_NULL_HANDLE;
            descriptorSet.reset();
            pending->fetch_sub(1, std::memory_order_relaxed);
        });
        return;
    }

    vkDeviceWaitIdle(device);
    vkFreeDescriptorSets(device, ownerPool, 1, &handle);
    descriptorSet->descriptorSet = VK_NULL_HANDLE;
}

void MaterialDescriptorManager::Clear()
{
    // Reset all pool pages (more efficient than freeing one by one)
    if (m_device != VK_NULL_HANDLE) {
        for (auto pool : m_descriptorPools) {
            if (pool != VK_NULL_HANDLE) {
                vkResetDescriptorPool(m_device, pool, 0);
            }
        }
    }
    m_descriptorSets.clear();
    // All handles are now invalid — clear the live-handle tracking set.
    m_allocatedHandles.clear();
    m_liveDescriptorHandles.clear();
}

void MaterialDescriptorManager::SetDefaultTexture(VkImageView imageView, VkSampler sampler)
{
    m_defaultImageView = imageView;
    m_defaultSampler = sampler;
}

void MaterialDescriptorManager::SetDefaultNormalTexture(VkImageView imageView, VkSampler sampler)
{
    m_defaultNormalImageView = imageView;
    m_defaultNormalSampler = sampler;
}

} // namespace infernux
