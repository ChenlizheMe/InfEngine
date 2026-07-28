#include "MaterialDescriptor.h"
#include <algorithm>
#include <core/log/InxLog.h>
#include <core/types/ColorSpace.h>
#include <cstring>
#include <limits>

namespace infernux
{

namespace
{

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
        case MaterialPropertyType::Float4: {
            glm::vec4 value = std::get<glm::vec4>(prop.value);
            WriteData(offset, &value, sizeof(glm::vec4));
            break;
        }
        case MaterialPropertyType::Color: {
            // Authored colors are sRGB; shading runs in linear space.
            glm::vec4 value = inx::color::SrgbToLinear(std::get<glm::vec4>(prop.value));
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
                                           vk::VkDescriptorManager *descriptorManager)
{
    m_vmaAllocator = allocator;
    m_device = device;
    m_physicalDevice = physicalDevice;
    m_descriptorManager = descriptorManager;
    if (!m_descriptorManager)
        INXLOG_ERROR("MaterialDescriptorManager requires the Vulkan descriptor manager");
}

void MaterialDescriptorManager::Shutdown()
{
    Clear();

    // Default bindings participate in the same revisioned texture publication
    // ownership as material bindings. Release them explicitly while the RHI
    // device is still alive; relying on member destruction would keep the
    // TextureResource alive until after InxVkCoreModular destroys its device.
    m_defaultGpuView.reset();
    m_defaultNormalGpuView.reset();
    m_defaultImageView = VK_NULL_HANDLE;
    m_defaultSampler = VK_NULL_HANDLE;
    m_defaultNormalImageView = VK_NULL_HANDLE;
    m_defaultNormalSampler = VK_NULL_HANDLE;

    if (m_descriptorManager)
        (void)m_descriptorManager->Collect((std::numeric_limits<rhi::SubmissionSerial>::max)());

    m_device = VK_NULL_HANDLE;
    m_physicalDevice = VK_NULL_HANDLE;
    m_descriptorManager = nullptr;
    m_liveDescriptorHandles.clear();
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
        outBinding = {m_defaultNormalImageView, m_defaultNormalSampler, {}, m_defaultNormalGpuView};
        return true;
    }

    if (m_defaultImageView != VK_NULL_HANDLE && m_defaultSampler != VK_NULL_HANDLE) {
        outBinding = {m_defaultImageView, m_defaultSampler, {}, m_defaultGpuView};
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
    if (outBinding.imageView == VK_NULL_HANDLE || outBinding.sampler == VK_NULL_HANDLE || !outBinding.gpuView ||
        !outBinding.gpuView->IsValid()) {
        outBinding = {};
        INXLOG_ERROR("Texture resolver returned Ready without a complete GPU binding for texture '", texturePath,
                     "' (binding='", bindingName, "')");
        return TextureResolveStatus::Failed;
    }
    return TextureResolveStatus::Ready;
}

MaterialDescriptorSet *MaterialDescriptorManager::GetOrCreateDescriptorSet(const InxMaterial &material,
                                                                           const ShaderProgram &program)
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
    matDescSet->bindings = program.GetDescriptorBindings();

    if (!m_descriptorManager) {
        INXLOG_ERROR("Material descriptor manager is unavailable for material: ", materialName);
        return nullptr;
    }
    const auto arena =
        m_updateAfterBindEnabled ? vk::DescriptorArena::UpdateAfterBind : vk::DescriptorArena::Persistent;
    matDescSet->descriptorLease = m_descriptorManager->Allocate(requiredLayout, arena);
    if (!matDescSet->descriptorLease.IsValid()) {
        INXLOG_ERROR("Failed to allocate descriptor set for material: ", materialName);
        return nullptr;
    }
    matDescSet->descriptorSet = matDescSet->descriptorLease.set;

    // Track this handle so callers can verify it's still live before binding.
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

    // Create the vertex-stage material UBO when ShaderInfo declares Properties (binding 14).
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
    if (!UpdateDescriptorBindings(*matDescSet, program)) {
        m_liveDescriptorHandles.erase(reinterpret_cast<uint64_t>(matDescSet->descriptorSet));
        RetireDescriptorSet(std::shared_ptr<MaterialDescriptorSet>(std::move(matDescSet)));
        return nullptr;
    }

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

bool MaterialDescriptorManager::UpdateDescriptorBindings(MaterialDescriptorSet &matDescSet,
                                                         const ShaderProgram &program)
{
    matDescSet.bufferBindings.clear();
    std::vector<VkWriteDescriptorSet> writes;
    std::vector<VkDescriptorBufferInfo> bufferInfos;
    std::vector<VkDescriptorImageInfo> imageInfos;

    // Reserve space to avoid reallocation invalidating pointers
    const auto &bindings = program.GetDescriptorBindings();
    bufferInfos.reserve(bindings.size());
    imageInfos.reserve(bindings.size());

    INXLOG_DEBUG("UpdateDescriptorBindings: ", bindings.size(), " reflected bindings");

    for (const auto &binding : bindings) {
        // Only write set 0 bindings into the material descriptor set.
        // Set 1 (per-view shadow map) is handled separately per render graph.
        if (binding.set != 0) {
            continue;
        }

        if (binding.type == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER) {
            VkDescriptorBufferInfo bufferInfo{};

            INXLOG_DEBUG("  Binding ", binding.binding, ": UBO");

            // Set 0 is exclusively material-owned. Camera and lighting data
            // belong to the active RenderView descriptor set (set 1).
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
            } else {
                INXLOG_ERROR("Material shader ABI violation: set 0 uniform buffer '", binding.name,
                             "' at binding ", binding.binding,
                             " is not a reflected material Properties block. Camera and lighting uniforms must use "
                             "the engine-owned RenderView set 1 contract.");
                return false;
            }

            AppendBufferWrite(writes, bufferInfos, matDescSet.descriptorSet, binding.binding, binding.type, bufferInfo,
                              binding.descriptorCount);
            matDescSet.bufferBindings[binding.binding] = bufferInfo;
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
    return true;
}

bool MaterialDescriptorManager::PublishDescriptorReplacement(
    MaterialDescriptorSet &descriptorSet,
    const std::unordered_map<uint32_t, MaterialDescriptorSet::TextureBinding> &textureBindings)
{
    if (!m_descriptorManager || descriptorSet.layout == VK_NULL_HANDLE)
        return false;

    const auto arena =
        m_updateAfterBindEnabled ? vk::DescriptorArena::UpdateAfterBind : vk::DescriptorArena::Persistent;
    const vk::DescriptorLease replacement = m_descriptorManager->Allocate(descriptorSet.layout, arena);
    if (!replacement.IsValid()) {
        INXLOG_ERROR("Failed to allocate copy-on-write material descriptor set");
        return false;
    }

    std::vector<VkWriteDescriptorSet> writes;
    std::vector<VkDescriptorBufferInfo> bufferInfos;
    std::vector<VkDescriptorImageInfo> imageInfos;
    writes.reserve(descriptorSet.bindings.size());
    bufferInfos.reserve(descriptorSet.bindings.size());
    imageInfos.reserve(descriptorSet.bindings.size());

    for (const auto &binding : descriptorSet.bindings) {
        if (binding.set != 0)
            continue;
        if (binding.type == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER) {
            const auto buffer = descriptorSet.bufferBindings.find(binding.binding);
            if (buffer == descriptorSet.bufferBindings.end()) {
                INXLOG_ERROR("Cannot publish material descriptor replacement: uniform binding ", binding.binding,
                             " has no buffer snapshot");
                m_descriptorManager->Retire(replacement);
                return false;
            }
            AppendBufferWrite(writes, bufferInfos, replacement.set, binding.binding, binding.type, buffer->second,
                              binding.descriptorCount);
            continue;
        }
        if (binding.type != VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER) {
            INXLOG_ERROR("Cannot publish material descriptor replacement: unsupported set-0 descriptor type ",
                         static_cast<int>(binding.type), " at binding ", binding.binding);
            m_descriptorManager->Retire(replacement);
            return false;
        }

        MaterialDescriptorSet::TextureBinding textureBinding{};
        const auto texture = textureBindings.find(binding.binding);
        if (texture != textureBindings.end()) {
            textureBinding = texture->second;
        } else if (!TryGetDefaultTextureBinding(binding.name, textureBinding)) {
            INXLOG_ERROR("Cannot publish material descriptor replacement: image binding ", binding.binding, " ('",
                         binding.name, "') has neither an explicit texture nor a default");
            m_descriptorManager->Retire(replacement);
            return false;
        }
        if (textureBinding.imageView == VK_NULL_HANDLE || textureBinding.sampler == VK_NULL_HANDLE) {
            INXLOG_ERROR("Cannot publish material descriptor replacement: image binding ", binding.binding, " ('",
                         binding.name, "') resolves to a null image or sampler");
            m_descriptorManager->Retire(replacement);
            return false;
        }
        AppendImageWrite(writes, imageInfos, replacement.set, binding.binding, textureBinding.imageView,
                         textureBinding.sampler);
    }

    if (!writes.empty())
        vkUpdateDescriptorSets(m_device, static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);

    if (!m_deletionQueue && !descriptorSet.textureBindings.empty()) {
        INXLOG_ERROR("Cannot publish material descriptor replacement without a GPU retirement queue");
        m_descriptorManager->Retire(replacement);
        return false;
    }

    const vk::DescriptorLease retiredLease = descriptorSet.descriptorLease;
    const VkDescriptorSet retiredSet = descriptorSet.descriptorSet;
    auto retiredTextureBindings = std::move(descriptorSet.textureBindings);
    descriptorSet.descriptorLease = replacement;
    descriptorSet.descriptorSet = replacement.set;
    descriptorSet.textureBindings = textureBindings;
    m_liveDescriptorHandles.erase(reinterpret_cast<uint64_t>(retiredSet));
    m_liveDescriptorHandles.insert(reinterpret_cast<uint64_t>(replacement.set));
    m_descriptorManager->Retire(retiredLease);
    if (m_deletionQueue && !retiredTextureBindings.empty()) {
        m_deletionQueue->Retire([bindings = std::move(retiredTextureBindings)]() mutable { bindings.clear(); });
    }
    return true;
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
    auto candidateBindings = matDescSet.textureBindings;
    bool candidateHasPendingTextures = false;
    bool bindingsChanged = false;
    const auto &properties = material.GetAllProperties();

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
                        const auto previous = candidateBindings.find(binding.binding);
                        bindingsChanged = bindingsChanged || previous == candidateBindings.end() ||
                                          !HasSameGpuBinding(previous->second, resolvedBinding);
                        candidateBindings[binding.binding] = resolvedBinding;
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

                if (!isPlaceholder && resolveStatus == TextureResolveStatus::Pending) {
                    candidateHasPendingTextures = true;
                    const auto previous = candidateBindings.find(binding.binding);
                    if (previous != candidateBindings.end() && previous->second.gpuView &&
                        previous->second.gpuView->IsValid()) {
                        break;
                    }
                }

                const bool hasBinding = resolvedExplicit || TryGetDefaultTextureBinding(binding.name, resolvedBinding);

                if (hasBinding) {
                    const auto previous = candidateBindings.find(binding.binding);
                    bindingsChanged = bindingsChanged || previous == candidateBindings.end() ||
                                      !HasSameGpuBinding(previous->second, resolvedBinding);
                    candidateBindings[binding.binding] = resolvedBinding;
                    if (resolvedExplicit) {
                        INXLOG_DEBUG("Re-bound texture '", *texturePath, "' to binding ", binding.binding,
                                     " for material '", materialName, "'");
                    }
                } else {
                    if (resolveStatus == TextureResolveStatus::Failed) {
                        INXLOG_WARN("Failed to resolve texture '", *texturePath, "' for material '", materialName,
                                    "' property '", propName, "' — binding default texture");
                    }
                }
                break;
            }
        }
    }

    const bool previousHasPendingTextures = matDescSet.hasPendingTextures;
    matDescSet.hasPendingTextures = candidateHasPendingTextures;
    if (!bindingsChanged)
        return;

    if (!PublishDescriptorReplacement(matDescSet, candidateBindings)) {
        matDescSet.hasPendingTextures = previousHasPendingTextures;
        INXLOG_ERROR("Material texture publication failed for '", materialName,
                     "'; the previous complete descriptor set remains active");
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

    if (imageView == VK_NULL_HANDLE || sampler == VK_NULL_HANDLE) {
        INXLOG_ERROR("Cannot bind a null texture descriptor to material '", materialName, "' at binding ", binding);
        return;
    }

    auto &descriptorSet = *it->second;
    const auto previousBinding = descriptorSet.textureBindings.find(binding);
    if (previousBinding != descriptorSet.textureBindings.end() &&
        HasSameGpuBinding(previousBinding->second, {imageView, sampler})) {
        return;
    }

    auto candidateBindings = descriptorSet.textureBindings;
    candidateBindings[binding] = {imageView, sampler};
    if (!PublishDescriptorReplacement(descriptorSet, candidateBindings)) {
        INXLOG_ERROR("Texture binding publication failed for material '", materialName,
                     "'; the previous complete descriptor set remains active");
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
        }
        m_descriptorSets.erase(it);
        RetireDescriptorSet(std::move(retiredEntry));
    }
}

void MaterialDescriptorManager::RetireDescriptorSet(std::shared_ptr<MaterialDescriptorSet> descriptorSet)
{
    if (!descriptorSet)
        throw std::invalid_argument("Cannot retire an empty material descriptor set");

    const vk::DescriptorLease lease = descriptorSet->descriptorLease;
    auto pending = m_pendingDescriptorSetReleases;
    if (m_descriptorManager)
        m_descriptorManager->Retire(lease);

    if (m_deletionQueue) {
        pending->fetch_add(1, std::memory_order_relaxed);
        m_deletionQueue->Retire([descriptorSet = std::move(descriptorSet), pending = std::move(pending)]() mutable {
            descriptorSet->descriptorSet = VK_NULL_HANDLE;
            descriptorSet->descriptorLease = {};
            descriptorSet.reset();
            pending->fetch_sub(1, std::memory_order_relaxed);
        });
        return;
    }

    vkDeviceWaitIdle(m_device);
    if (m_descriptorManager)
        (void)m_descriptorManager->Collect((std::numeric_limits<rhi::SubmissionSerial>::max)());
    descriptorSet->descriptorSet = VK_NULL_HANDLE;
    descriptorSet->descriptorLease = {};
}

void MaterialDescriptorManager::Clear()
{
    for (auto &[name, descriptorSet] : m_descriptorSets) {
        (void)name;
        if (descriptorSet && m_descriptorManager)
            m_descriptorManager->Retire(descriptorSet->descriptorLease);
    }
    m_descriptorSets.clear();
    // All handles are now invalid — clear the live-handle tracking set.
    m_liveDescriptorHandles.clear();
}

void MaterialDescriptorManager::SetDefaultTexture(VkImageView imageView, VkSampler sampler,
                                                   std::shared_ptr<const rhi::TextureGpuView> gpuView)
{
    m_defaultImageView = imageView;
    m_defaultSampler = sampler;
    m_defaultGpuView = std::move(gpuView);
}

void MaterialDescriptorManager::SetDefaultNormalTexture(VkImageView imageView, VkSampler sampler,
                                                         std::shared_ptr<const rhi::TextureGpuView> gpuView)
{
    m_defaultNormalImageView = imageView;
    m_defaultNormalSampler = sampler;
    m_defaultNormalGpuView = std::move(gpuView);
}

} // namespace infernux
