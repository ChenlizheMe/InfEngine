#include "ShaderProgram.h"
#include <algorithm>
#include <core/log/InxLog.h>
#include <platform/filesystem/InxPath.h>
#include <stdexcept>

namespace infernux
{

VkDescriptorSetLayout ShaderProgram::s_globalsDescSetLayout = VK_NULL_HANDLE;
VkDescriptorSetLayout ShaderProgram::s_perViewDescSetLayout = VK_NULL_HANDLE;
bool ShaderProgram::s_updateAfterBindEnabled = false;
VkDescriptorSetLayout ShaderProgram::s_bindlessTextureDescSetLayout = VK_NULL_HANDLE;
bool ShaderProgram::s_bindlessTextureEnabled = false;

void ShaderProgram::SetGlobalsDescSetLayout(VkDescriptorSetLayout layout)
{
    s_globalsDescSetLayout = layout;
}

VkDescriptorSetLayout ShaderProgram::GetGlobalsDescSetLayout()
{
    return s_globalsDescSetLayout;
}

void ShaderProgram::SetPerViewDescSetLayout(VkDescriptorSetLayout layout)
{
    s_perViewDescSetLayout = layout;
}

VkDescriptorSetLayout ShaderProgram::GetPerViewDescSetLayout()
{
    return s_perViewDescSetLayout;
}

void ShaderProgram::SetUpdateAfterBindEnabled(bool enabled)
{
    s_updateAfterBindEnabled = enabled;
}

bool ShaderProgram::IsUpdateAfterBindEnabled()
{
    return s_updateAfterBindEnabled;
}

void ShaderProgram::SetBindlessTextureDescSetLayout(VkDescriptorSetLayout layout)
{
    s_bindlessTextureDescSetLayout = layout;
}

VkDescriptorSetLayout ShaderProgram::GetBindlessTextureDescSetLayout()
{
    return s_bindlessTextureDescSetLayout;
}

void ShaderProgram::SetBindlessTextureEnabled(bool enabled)
{
    s_bindlessTextureEnabled = enabled;
}

bool ShaderProgram::IsBindlessTextureEnabled()
{
    return s_bindlessTextureEnabled && s_bindlessTextureDescSetLayout != VK_NULL_HANDLE;
}

// ============================================================================
// ShaderProgram Implementation
// ============================================================================

ShaderProgram::~ShaderProgram()
{
    Destroy();
}

ShaderProgram::ShaderProgram(ShaderProgram &&other) noexcept
    : m_device(other.m_device), m_shaderId(std::move(other.m_shaderId)), m_variantKey(std::move(other.m_variantKey)),
      m_vertModule(other.m_vertModule), m_fragModule(other.m_fragModule),
      m_vertReflection(std::move(other.m_vertReflection)), m_fragReflection(std::move(other.m_fragReflection)),
      m_descriptorBindings(std::move(other.m_descriptorBindings)),
      m_descriptorSetLayouts(std::move(other.m_descriptorSetLayouts)), m_pipelineLayout(other.m_pipelineLayout),
      m_materialUBOLayout(std::move(other.m_materialUBOLayout)), m_hasMaterialUBO(other.m_hasMaterialUBO),
      m_vertexMaterialUBOLayout(std::move(other.m_vertexMaterialUBOLayout)),
      m_hasVertexMaterialUBO(other.m_hasVertexMaterialUBO),
      m_bindlessTextureIndexLayout(std::move(other.m_bindlessTextureIndexLayout)),
      m_hasBindlessTextureIndexLayout(other.m_hasBindlessTextureIndexLayout),
      m_usesBindlessTextureABI(other.m_usesBindlessTextureABI)
{
    other.m_device = VK_NULL_HANDLE;
    other.m_vertModule = VK_NULL_HANDLE;
    other.m_fragModule = VK_NULL_HANDLE;
    other.m_pipelineLayout = VK_NULL_HANDLE;
    other.m_hasMaterialUBO = false;
    other.m_hasVertexMaterialUBO = false;
    other.m_hasBindlessTextureIndexLayout = false;
    other.m_usesBindlessTextureABI = false;
    other.m_descriptorSetLayouts.clear();
}

ShaderProgram &ShaderProgram::operator=(ShaderProgram &&other) noexcept
{
    if (this != &other) {
        Destroy();

        m_device = other.m_device;
        m_shaderId = std::move(other.m_shaderId);
        m_variantKey = std::move(other.m_variantKey);
        m_vertModule = other.m_vertModule;
        m_fragModule = other.m_fragModule;
        m_vertReflection = std::move(other.m_vertReflection);
        m_fragReflection = std::move(other.m_fragReflection);
        m_descriptorBindings = std::move(other.m_descriptorBindings);
        m_descriptorSetLayouts = std::move(other.m_descriptorSetLayouts);
        m_pipelineLayout = other.m_pipelineLayout;
        m_materialUBOLayout = std::move(other.m_materialUBOLayout);
        m_hasMaterialUBO = other.m_hasMaterialUBO;
        m_vertexMaterialUBOLayout = std::move(other.m_vertexMaterialUBOLayout);
        m_hasVertexMaterialUBO = other.m_hasVertexMaterialUBO;
        m_bindlessTextureIndexLayout = std::move(other.m_bindlessTextureIndexLayout);
        m_hasBindlessTextureIndexLayout = other.m_hasBindlessTextureIndexLayout;
        m_usesBindlessTextureABI = other.m_usesBindlessTextureABI;

        other.m_device = VK_NULL_HANDLE;
        other.m_vertModule = VK_NULL_HANDLE;
        other.m_fragModule = VK_NULL_HANDLE;
        other.m_pipelineLayout = VK_NULL_HANDLE;
        other.m_hasMaterialUBO = false;
        other.m_hasVertexMaterialUBO = false;
        other.m_hasBindlessTextureIndexLayout = false;
        other.m_usesBindlessTextureABI = false;
        other.m_descriptorSetLayouts.clear();
    }
    return *this;
}

bool ShaderProgram::Create(VkDevice device, const std::vector<char> &vertSpirv, const std::vector<char> &fragSpirv,
                           const ShaderProgramVariantKey &variantKey)
{
    m_device = device;
    m_variantKey = variantKey;
    m_shaderId = variantKey.ToString();

    // Create shader modules
    m_vertModule = CreateShaderModule(vertSpirv);
    if (m_vertModule == VK_NULL_HANDLE) {
        INXLOG_ERROR("Failed to create vertex shader module for program: ", m_shaderId);
        return false;
    }

    m_fragModule = CreateShaderModule(fragSpirv);
    if (m_fragModule == VK_NULL_HANDLE) {
        INXLOG_ERROR("Failed to create fragment shader module for program: ", m_shaderId);
        vkDestroyShaderModule(m_device, m_vertModule, nullptr);
        m_vertModule = VK_NULL_HANDLE;
        return false;
    }

    // Reflect shader resources
    if (!m_vertReflection.Reflect(vertSpirv, VK_SHADER_STAGE_VERTEX_BIT)) {
        INXLOG_ERROR("Failed to reflect vertex shader: ", m_shaderId);
        Destroy();
        return false;
    }

    if (!m_fragReflection.Reflect(fragSpirv, VK_SHADER_STAGE_FRAGMENT_BIT)) {
        INXLOG_ERROR("Failed to reflect fragment shader: ", m_shaderId);
        Destroy();
        return false;
    }

    // Merge reflection data
    MergeReflectionData();
    const bool hasReflectedBindlessTextureABI =
        std::any_of(m_descriptorBindings.begin(), m_descriptorBindings.end(), [](const auto &binding) {
            return binding.set == BindlessTextureSet && binding.binding == BindlessTextureBinding &&
                   binding.type == VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
        });
    if (hasReflectedBindlessTextureABI &&
        (!IsBindlessTextureEnabled() || GetBindlessTextureDescSetLayout() == VK_NULL_HANDLE)) {
        INXLOG_ERROR("Shader program '", m_shaderId,
                     "' contains the bindless material texture ABI, but the active device does not provide the "
                     "required descriptor-indexing table; use the bounded sampler compilation");
        Destroy();
        return false;
    }
    m_usesBindlessTextureABI = hasReflectedBindlessTextureABI;

    // Validate vertex→fragment stage interface
    if (!ValidateStageInterface()) {
        INXLOG_ERROR("Shader interface validation failed for program: ", m_shaderId,
                     ". Vertex outputs and fragment inputs are incompatible.");
        Destroy();
        return false;
    }

    // Extract material UBO layout
    ExtractMaterialUBOLayout();

    // Create descriptor set layouts
    if (!CreateDescriptorSetLayouts()) {
        INXLOG_ERROR("Failed to create descriptor set layouts for program: ", m_shaderId);
        Destroy();
        return false;
    }

    // Create pipeline layout
    if (!CreatePipelineLayout()) {
        INXLOG_ERROR("Failed to create pipeline layout for program: ", m_shaderId);
        Destroy();
        return false;
    }

    // INXLOG_INFO("Created shader program: ", shaderId, " with ", m_descriptorBindings.size(), " bindings");
    return true;
}

void ShaderProgram::Destroy()
{
    if (m_device == VK_NULL_HANDLE) {
        return;
    }

    if (m_pipelineLayout != VK_NULL_HANDLE) {
        vkDestroyPipelineLayout(m_device, m_pipelineLayout, nullptr);
        m_pipelineLayout = VK_NULL_HANDLE;
    }

    for (auto &[set, layout] : m_descriptorSetLayouts) {
        // Skip the shared globals layout — it is owned by VkCore and
        // destroyed in DestroyGlobalsDescriptorResources().
        if (layout != VK_NULL_HANDLE && layout != s_globalsDescSetLayout && layout != s_perViewDescSetLayout &&
            layout != s_bindlessTextureDescSetLayout) {
            vkDestroyDescriptorSetLayout(m_device, layout, nullptr);
        }
    }
    m_descriptorSetLayouts.clear();

    if (m_fragModule != VK_NULL_HANDLE) {
        vkDestroyShaderModule(m_device, m_fragModule, nullptr);
        m_fragModule = VK_NULL_HANDLE;
    }

    if (m_vertModule != VK_NULL_HANDLE) {
        vkDestroyShaderModule(m_device, m_vertModule, nullptr);
        m_vertModule = VK_NULL_HANDLE;
    }

    m_descriptorBindings.clear();
    m_hasMaterialUBO = false;
    m_hasVertexMaterialUBO = false;
    m_bindlessTextureIndexLayout = {};
    m_hasBindlessTextureIndexLayout = false;
    m_usesBindlessTextureABI = false;
    m_device = VK_NULL_HANDLE;
}

VkShaderModule ShaderProgram::CreateShaderModule(const std::vector<char> &code)
{
    if (code.empty()) {
        INXLOG_ERROR("Cannot create shader module from empty code");
        return VK_NULL_HANDLE;
    }

    VkShaderModuleCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    createInfo.codeSize = code.size();
    createInfo.pCode = reinterpret_cast<const uint32_t *>(code.data());

    VkShaderModule module;
    if (vkCreateShaderModule(m_device, &createInfo, nullptr, &module) != VK_SUCCESS) {
        INXLOG_ERROR("vkCreateShaderModule failed");
        return VK_NULL_HANDLE;
    }

    return module;
}

void ShaderProgram::MergeReflectionData()
{
    m_descriptorBindings.clear();

    // Helper to add or merge a binding
    auto addBinding = [this](uint32_t binding, uint32_t set, VkDescriptorType type, uint32_t count,
                             VkShaderStageFlags stage, const std::string &name) {
        // Check if binding already exists
        for (auto &existing : m_descriptorBindings) {
            if (existing.binding == binding && existing.set == set) {
                // Merge stage flags
                existing.stageFlags |= stage;
                return;
            }
        }

        // Add new binding
        MergedDescriptorBinding merged;
        merged.binding = binding;
        merged.set = set;
        merged.type = type;
        merged.descriptorCount = count;
        merged.stageFlags = stage;
        merged.name = name;
        m_descriptorBindings.push_back(merged);
    };

    // Process vertex shader UBOs
    for (const auto &ubo : m_vertReflection.GetUniformBuffers()) {
        addBinding(ubo.binding, ubo.set, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1, VK_SHADER_STAGE_VERTEX_BIT, ubo.name);
    }

    // Process vertex shader samplers
    for (const auto &sampler : m_vertReflection.GetSampledImages()) {
        addBinding(sampler.binding, sampler.set, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, sampler.arraySize,
                   VK_SHADER_STAGE_VERTEX_BIT, sampler.name);
    }

    // Process fragment shader UBOs
    for (const auto &ubo : m_fragReflection.GetUniformBuffers()) {
        addBinding(ubo.binding, ubo.set, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1, VK_SHADER_STAGE_FRAGMENT_BIT, ubo.name);
    }

    // Process fragment shader samplers
    for (const auto &sampler : m_fragReflection.GetSampledImages()) {
        addBinding(sampler.binding, sampler.set, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, sampler.arraySize,
                   VK_SHADER_STAGE_FRAGMENT_BIT, sampler.name);
    }

    // Sort by set, then by binding
    std::sort(m_descriptorBindings.begin(), m_descriptorBindings.end(),
              [](const MergedDescriptorBinding &a, const MergedDescriptorBinding &b) {
                  if (a.set != b.set)
                      return a.set < b.set;
                  return a.binding < b.binding;
              });
}

// Helper: human-readable name for VkFormat
static const char *VkFormatName(VkFormat fmt)
{
    switch (fmt) {
    case VK_FORMAT_R32_SFLOAT:
        return "float";
    case VK_FORMAT_R32G32_SFLOAT:
        return "vec2";
    case VK_FORMAT_R32G32B32_SFLOAT:
        return "vec3";
    case VK_FORMAT_R32G32B32A32_SFLOAT:
        return "vec4";
    case VK_FORMAT_R32_SINT:
        return "int";
    case VK_FORMAT_R32G32_SINT:
        return "ivec2";
    case VK_FORMAT_R32G32B32_SINT:
        return "ivec3";
    case VK_FORMAT_R32G32B32A32_SINT:
        return "ivec4";
    default:
        return "unknown";
    }
}

bool ShaderProgram::ValidateStageInterface() const
{
    const auto &vertOutputs = m_vertReflection.GetOutputs();
    const auto &fragInputs = m_fragReflection.GetInputs();
    bool valid = true;

    for (const auto &fragIn : fragInputs) {
        // Find the matching vertex output at the same location
        const ShaderIOVariable *matchedOutput = nullptr;
        for (const auto &vertOut : vertOutputs) {
            if (vertOut.location == fragIn.location) {
                matchedOutput = &vertOut;
                break;
            }
        }

        if (!matchedOutput) {
            INXLOG_ERROR("Shader interface mismatch in '", m_shaderId, "': fragment input '", fragIn.name,
                         "' (location ", fragIn.location, ", type ", VkFormatName(fragIn.format),
                         ") has no matching vertex output. "
                         "The fragment shader will receive undefined values.");
            valid = false;
            continue;
        }

        if (matchedOutput->format != fragIn.format) {
            INXLOG_ERROR("Shader interface mismatch in '", m_shaderId, "': vertex output '", matchedOutput->name,
                         "' is ", VkFormatName(matchedOutput->format), " but fragment input '", fragIn.name,
                         "' expects ", VkFormatName(fragIn.format), " at location ", fragIn.location,
                         ". This will cause a Vulkan validation error and potential GPU crash.");
            valid = false;
        }
    }

    return valid;
}

void ShaderProgram::ExtractMaterialUBOLayout()
{
    m_hasMaterialUBO = false;
    m_hasVertexMaterialUBO = false;
    m_hasBindlessTextureIndexLayout = false;

    // Look for the bindless texture-index block and the canonical material
    // properties block in the fragment shader. The index block belongs to the
    // material set of the active domain, so its set/binding cannot be hard-coded here.
    for (const auto &ubo : m_fragReflection.GetUniformBuffers()) {
        if (ubo.name == "InxMaterialTextureIndices") {
            m_bindlessTextureIndexLayout.binding = ubo.binding;
            m_bindlessTextureIndexLayout.size = ubo.size;
            m_bindlessTextureIndexLayout.members = ubo.members;
            m_hasBindlessTextureIndexLayout = true;
            continue;
        }
        if (ubo.name == "MaterialProperties") {
            m_materialUBOLayout.binding = ubo.binding;
            m_materialUBOLayout.size = ubo.size;
            m_materialUBOLayout.members = ubo.members;
            m_hasMaterialUBO = true;

            break;
        }
    }

    // Also look for a vertex-stage MaterialProperties UBO at binding 14
    // (used when the vertex ShaderInfo declares Properties)
    for (const auto &ubo : m_vertReflection.GetUniformBuffers()) {
        if (ubo.name == "MaterialProperties" && ubo.binding == 14 && ubo.set == 0) {
            m_vertexMaterialUBOLayout.binding = ubo.binding;
            m_vertexMaterialUBOLayout.size = ubo.size;
            m_vertexMaterialUBOLayout.members = ubo.members;
            m_hasVertexMaterialUBO = true;

            break;
        }
    }

    // A linked program exposes one canonical MaterialProperties block to both
    // stages. When reflection reports the same layout twice, bind one buffer
    // with merged stage visibility instead of allocating a duplicate vertex UBO.
    if (m_hasMaterialUBO && m_hasVertexMaterialUBO &&
        m_materialUBOLayout.binding == m_vertexMaterialUBOLayout.binding &&
        m_materialUBOLayout.size == m_vertexMaterialUBOLayout.size &&
        m_materialUBOLayout.members.size() == m_vertexMaterialUBOLayout.members.size()) {
        const bool sameMembers = std::equal(
            m_materialUBOLayout.members.begin(), m_materialUBOLayout.members.end(),
            m_vertexMaterialUBOLayout.members.begin(), [](const UniformMember &lhs, const UniformMember &rhs) {
                return lhs.name == rhs.name && lhs.offset == rhs.offset && lhs.size == rhs.size;
            });
        if (sameMembers) {
            m_vertexMaterialUBOLayout = {};
            m_hasVertexMaterialUBO = false;
        }
    }
}

bool ShaderProgram::CreateDescriptorSetLayouts()
{
    // Group bindings by descriptor set
    std::unordered_map<uint32_t, std::vector<VkDescriptorSetLayoutBinding>> setBindings;

    for (const auto &merged : m_descriptorBindings) {
        VkDescriptorSetLayoutBinding binding{};
        binding.binding = merged.binding;
        binding.descriptorType = merged.type;
        binding.descriptorCount = merged.descriptorCount;
        binding.stageFlags = merged.stageFlags;
        binding.pImmutableSamplers = nullptr;

        setBindings[merged.set].push_back(binding);
    }

    if (m_usesBindlessTextureABI) {
        uint32_t textureIndexSet = 0;
        uint32_t textureIndexBinding = MaterialTextureIndexBinding;
        const auto reflectedIndexBuffer =
            std::find_if(m_fragReflection.GetUniformBuffers().begin(), m_fragReflection.GetUniformBuffers().end(),
                         [](const auto &buffer) { return buffer.name == "InxMaterialTextureIndices"; });
        if (reflectedIndexBuffer != m_fragReflection.GetUniformBuffers().end()) {
            textureIndexSet = reflectedIndexBuffer->set;
            textureIndexBinding = reflectedIndexBuffer->binding;
        }
        auto &materialBindings = setBindings[textureIndexSet];
        const auto existing =
            std::find_if(materialBindings.begin(), materialBindings.end(),
                         [textureIndexBinding](const auto &binding) { return binding.binding == textureIndexBinding; });
        if (existing == materialBindings.end()) {
            VkDescriptorSetLayoutBinding indexBinding{};
            indexBinding.binding = textureIndexBinding;
            indexBinding.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            indexBinding.descriptorCount = 1;
            indexBinding.stageFlags = VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT;
            materialBindings.push_back(indexBinding);
        } else if (existing->descriptorType != VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER || existing->descriptorCount != 1) {
            INXLOG_ERROR("Bindless material ABI reserves set ", textureIndexSet, " binding ", textureIndexBinding,
                         " for a uniform buffer, but shader ", m_shaderId, " declares another descriptor type");
            return false;
        }
    }

    // Create a layout for each set.
    //
    // For the material set (set 0) we retain UPDATE_AFTER_BIND compatibility when
    // supported. MaterialDescriptorManager now publishes immutable replacement sets,
    // so live-update correctness no longer depends on mutating an in-flight set.
    //
    // Sets 1, 2 (per-view, globals) keep the legacy layout — they are written once
    // per frame on the CPU before any submission and have no live-update concern.
    const bool enableUpdateAfterBind = IsUpdateAfterBindEnabled();
    for (auto &[setIndex, bindings] : setBindings) {
        VkDescriptorSetLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        layoutInfo.bindingCount = static_cast<uint32_t>(bindings.size());
        layoutInfo.pBindings = bindings.data();

        std::vector<VkDescriptorBindingFlags> bindingFlags;
        VkDescriptorSetLayoutBindingFlagsCreateInfo bindingFlagsInfo{};
        if (enableUpdateAfterBind && setIndex == 0) {
            bindingFlags.assign(bindings.size(), VK_DESCRIPTOR_BINDING_UPDATE_AFTER_BIND_BIT |
                                                     VK_DESCRIPTOR_BINDING_PARTIALLY_BOUND_BIT);
            bindingFlagsInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_BINDING_FLAGS_CREATE_INFO;
            bindingFlagsInfo.bindingCount = static_cast<uint32_t>(bindingFlags.size());
            bindingFlagsInfo.pBindingFlags = bindingFlags.data();
            layoutInfo.pNext = &bindingFlagsInfo;
            layoutInfo.flags = VK_DESCRIPTOR_SET_LAYOUT_CREATE_UPDATE_AFTER_BIND_POOL_BIT;
        }

        VkDescriptorSetLayout layout;
        if (vkCreateDescriptorSetLayout(m_device, &layoutInfo, nullptr, &layout) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create descriptor set layout for set ", setIndex);
            return false;
        }

        m_descriptorSetLayouts[setIndex] = layout;
    }

    // If no bindings, create an empty layout for set 0
    if (m_descriptorSetLayouts.empty()) {
        VkDescriptorSetLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        layoutInfo.bindingCount = 0;
        layoutInfo.pBindings = nullptr;

        VkDescriptorSetLayout layout;
        if (vkCreateDescriptorSetLayout(m_device, &layoutInfo, nullptr, &layout) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create empty descriptor set layout");
            return false;
        }
        m_descriptorSetLayouts[0] = layout;
    }

    return true;
}

bool ShaderProgram::CreatePipelineLayout()
{
    if (m_variantKey.target != ShaderCompileTarget::Shadow && s_perViewDescSetLayout != VK_NULL_HANDLE) {
        auto it = m_descriptorSetLayouts.find(1);
        if (it != m_descriptorSetLayouts.end() && it->second != s_perViewDescSetLayout)
            vkDestroyDescriptorSetLayout(m_device, it->second, nullptr);
        m_descriptorSetLayouts[1] = s_perViewDescSetLayout;
    }

    // If a globals descriptor set layout was registered, ensure set 2 exists
    // in the layout map. Replace any reflection-created set 2 with the
    // canonical engine layout so descriptor set compatibility is guaranteed.
    if (m_variantKey.target != ShaderCompileTarget::Shadow && s_globalsDescSetLayout != VK_NULL_HANDLE) {
        // If reflection already created a set 2, destroy it — we use the shared one
        auto it = m_descriptorSetLayouts.find(2);
        if (it != m_descriptorSetLayouts.end() && it->second != s_globalsDescSetLayout) {
            vkDestroyDescriptorSetLayout(m_device, it->second, nullptr);
        }
        m_descriptorSetLayouts[2] = s_globalsDescSetLayout;
    }

    if (m_usesBindlessTextureABI) {
        auto it = m_descriptorSetLayouts.find(BindlessTextureSet);
        if (it != m_descriptorSetLayouts.end() && it->second != s_bindlessTextureDescSetLayout)
            vkDestroyDescriptorSetLayout(m_device, it->second, nullptr);
        m_descriptorSetLayouts[BindlessTextureSet] = s_bindlessTextureDescSetLayout;
    }

    // Get layouts in order (set 0, 1, 2, ...)
    std::vector<VkDescriptorSetLayout> layouts;
    uint32_t maxSet = 0;

    for (const auto &[setIndex, layout] : m_descriptorSetLayouts) {
        maxSet = std::max(maxSet, setIndex);
    }

    for (uint32_t i = 0; i <= maxSet; ++i) {
        auto it = m_descriptorSetLayouts.find(i);
        if (it != m_descriptorSetLayouts.end()) {
            layouts.push_back(it->second);
        } else {
            // Create empty layout for gaps
            VkDescriptorSetLayoutCreateInfo layoutInfo{};
            layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
            layoutInfo.bindingCount = 0;
            layoutInfo.pBindings = nullptr;

            VkDescriptorSetLayout emptyLayout;
            if (vkCreateDescriptorSetLayout(m_device, &layoutInfo, nullptr, &emptyLayout) != VK_SUCCESS) {
                INXLOG_ERROR("Failed to create empty descriptor set layout for gap at set ", i);
                return false;
            }
            m_descriptorSetLayouts[i] = emptyLayout;
            layouts.push_back(emptyLayout);
        }
    }

    // Collect push constant ranges
    std::vector<VkPushConstantRange> pushConstantRanges;
    for (const auto &pc : m_vertReflection.GetPushConstants()) {
        VkPushConstantRange range{};
        range.stageFlags = pc.stageFlags;
        range.offset = pc.offset;
        range.size = pc.size;
        pushConstantRanges.push_back(range);
    }
    for (const auto &pc : m_fragReflection.GetPushConstants()) {
        // Check if we need to merge with existing range
        bool merged = false;
        for (auto &existing : pushConstantRanges) {
            if (existing.offset == pc.offset && existing.size == pc.size) {
                existing.stageFlags |= pc.stageFlags;
                merged = true;
                break;
            }
        }
        if (!merged) {
            VkPushConstantRange range{};
            range.stageFlags = pc.stageFlags;
            range.offset = pc.offset;
            range.size = pc.size;
            pushConstantRanges.push_back(range);
        }
    }

    VkPipelineLayoutCreateInfo pipelineLayoutInfo{};
    pipelineLayoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    pipelineLayoutInfo.setLayoutCount = static_cast<uint32_t>(layouts.size());
    pipelineLayoutInfo.pSetLayouts = layouts.empty() ? nullptr : layouts.data();
    pipelineLayoutInfo.pushConstantRangeCount = static_cast<uint32_t>(pushConstantRanges.size());
    pipelineLayoutInfo.pPushConstantRanges = pushConstantRanges.empty() ? nullptr : pushConstantRanges.data();

    if (vkCreatePipelineLayout(m_device, &pipelineLayoutInfo, nullptr, &m_pipelineLayout) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create pipeline layout");
        return false;
    }

    return true;
}

bool ShaderProgram::HasDeclaredDescriptorSet(uint32_t set) const
{
    return std::any_of(m_descriptorBindings.begin(), m_descriptorBindings.end(),
                       [set](const MergedDescriptorBinding &binding) { return binding.set == set; });
}

uint32_t ShaderProgram::GetTextureBindingCount() const
{
    uint32_t count = 0;
    for (const auto &binding : m_descriptorBindings) {
        if (binding.type == VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER) {
            count += binding.descriptorCount;
        }
    }
    return count;
}

// ============================================================================
// ShaderProgramCache Implementation
// ============================================================================

void ShaderProgramCache::Initialize(VkDevice device)
{
    m_device = device;
}

void ShaderProgramCache::Shutdown()
{
    Clear();
    m_device = VK_NULL_HANDLE;
}

ShaderProgramPublication ShaderProgramCache::GetOrCreateProgram(const ShaderProgramKey &programKey,
                                                                const std::vector<char> &vertSpirv,
                                                                const std::vector<char> &fragSpirv)
{
    return GetOrCreateProgram({programKey, ShaderCompileTarget::Forward}, vertSpirv, fragSpirv);
}

ShaderProgramPublication ShaderProgramCache::GetOrCreateProgram(const ShaderProgramVariantKey &variantKey,
                                                                const std::vector<char> &vertSpirv,
                                                                const std::vector<char> &fragSpirv)
{
    const ShaderProgramVariantKey canonicalKey = CanonicalKey(variantKey);
    // Check cache first
    auto it = m_programs.find(canonicalKey);
    if (it != m_programs.end()) {
        return it->second;
    }

    // Check if this program previously failed creation (don't retry every frame)
    if (m_failedPrograms.count(canonicalKey)) {
        return nullptr;
    }

    // Create new program
    auto program = std::make_shared<ShaderProgram>();
    if (!program->Create(m_device, vertSpirv, fragSpirv, canonicalKey)) {
        INXLOG_ERROR("Failed to create shader program: ", canonicalKey.ToString());
        m_failedPrograms.insert(canonicalKey);
        return nullptr;
    }

    ShaderProgramPublication publication = std::move(program);
    m_programs[canonicalKey] = publication;
    return publication;
}

ShaderProgramPublication ShaderProgramCache::GetProgram(const ShaderProgramKey &programKey) const
{
    return GetProgram({programKey, ShaderCompileTarget::Forward});
}

ShaderProgramPublication ShaderProgramCache::GetProgram(const ShaderProgramVariantKey &variantKey) const
{
    auto it = m_programs.find(CanonicalKey(variantKey));
    return it != m_programs.end() ? it->second : nullptr;
}

bool ShaderProgramCache::HasProgram(const ShaderProgramKey &programKey) const
{
    return HasProgram({programKey, ShaderCompileTarget::Forward});
}

bool ShaderProgramCache::HasProgram(const ShaderProgramVariantKey &variantKey) const
{
    return m_programs.find(CanonicalKey(variantKey)) != m_programs.end();
}

ShaderProgramPublication ShaderProgramCache::TakeProgram(const ShaderProgramKey &programKey)
{
    return TakeProgram({programKey, ShaderCompileTarget::Forward});
}

ShaderProgramPublication ShaderProgramCache::TakeProgram(const ShaderProgramVariantKey &variantKey)
{
    const ShaderProgramVariantKey canonicalKey = CanonicalKey(variantKey);
    auto found = m_programs.find(canonicalKey);
    if (found == m_programs.end()) {
        m_failedPrograms.erase(canonicalKey);
        return nullptr;
    }
    auto program = std::move(found->second);
    m_programs.erase(found);
    m_failedPrograms.erase(canonicalKey);
    return program;
}

std::vector<ShaderProgramPublication> ShaderProgramCache::TakePrograms(const ShaderProgramKey &programKey)
{
    std::vector<ShaderProgramVariantKey> keys;
    for (const auto &[key, program] : m_programs) {
        (void)program;
        if (key.program == programKey)
            keys.push_back(key);
    }
    for (auto it = m_failedPrograms.begin(); it != m_failedPrograms.end();) {
        if (it->program == programKey)
            it = m_failedPrograms.erase(it);
        else
            ++it;
    }

    std::vector<ShaderProgramPublication> programs;
    programs.reserve(keys.size());
    for (const auto &key : keys)
        programs.push_back(TakeProgram(key));
    return programs;
}

std::vector<ShaderProgramPublication> ShaderProgramCache::TakeProgramsContainingShader(const std::string &shaderName)
{
    if (shaderName.empty())
        throw std::invalid_argument("Shader program invalidation requires a non-empty shader identifier");

    auto normalizeIdentifier = [](const std::string &path) { return PortablePathStem(path); };

    auto matchesShader = [&](const std::string &stageId) {
        if (stageId == shaderName || stageId.rfind(shaderName + "/", 0) == 0)
            return true;
        return normalizeIdentifier(stageId) == normalizeIdentifier(shaderName);
    };

    std::vector<ShaderProgramVariantKey> toRemove;
    for (const auto &[key, program] : m_programs) {
        (void)program;
        if (matchesShader(key.program.stages.vertexShaderId) || matchesShader(key.program.stages.fragmentShaderId))
            toRemove.push_back(key);
    }
    for (auto it = m_failedPrograms.begin(); it != m_failedPrograms.end();) {
        if (matchesShader(it->program.stages.vertexShaderId) || matchesShader(it->program.stages.fragmentShaderId))
            it = m_failedPrograms.erase(it);
        else
            ++it;
    }

    std::vector<ShaderProgramPublication> retired;
    retired.reserve(toRemove.size());
    for (const auto &key : toRemove) {
        retired.push_back(TakeProgram(key));
    }

    return retired;
}

void ShaderProgramCache::Clear()
{
    m_programs.clear();
    m_failedPrograms.clear();
}

} // namespace infernux
