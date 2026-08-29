#include "ShaderReflection.h"
#include <core/log/InxLog.h>

// SPIRV-Cross headers
#if defined(INFERNUX_SPIRV_CROSS_FLAT_INCLUDE)
#include <spirv_cross.hpp>
#include <spirv_glsl.hpp>
#else
#include <spirv_cross/spirv_cross.hpp>
#include <spirv_cross/spirv_glsl.hpp>
#endif

#include <cstring>

namespace infernux
{
namespace
{
VkFormat StageIoFormat(const spirv_cross::SPIRType &type)
{
    if (type.width != 32)
        return VK_FORMAT_UNDEFINED;

    if (type.basetype == spirv_cross::SPIRType::Float) {
        switch (type.vecsize) {
        case 1:
            return VK_FORMAT_R32_SFLOAT;
        case 2:
            return VK_FORMAT_R32G32_SFLOAT;
        case 3:
            return VK_FORMAT_R32G32B32_SFLOAT;
        case 4:
            return VK_FORMAT_R32G32B32A32_SFLOAT;
        default:
            return VK_FORMAT_UNDEFINED;
        }
    }
    if (type.basetype == spirv_cross::SPIRType::Int) {
        switch (type.vecsize) {
        case 1:
            return VK_FORMAT_R32_SINT;
        case 2:
            return VK_FORMAT_R32G32_SINT;
        case 3:
            return VK_FORMAT_R32G32B32_SINT;
        case 4:
            return VK_FORMAT_R32G32B32A32_SINT;
        default:
            return VK_FORMAT_UNDEFINED;
        }
    }
    if (type.basetype == spirv_cross::SPIRType::UInt) {
        switch (type.vecsize) {
        case 1:
            return VK_FORMAT_R32_UINT;
        case 2:
            return VK_FORMAT_R32G32_UINT;
        case 3:
            return VK_FORMAT_R32G32B32_UINT;
        case 4:
            return VK_FORMAT_R32G32B32A32_UINT;
        default:
            return VK_FORMAT_UNDEFINED;
        }
    }
    return VK_FORMAT_UNDEFINED;
}
} // namespace

bool ShaderReflection::Reflect(const std::vector<char> &spirvCode, VkShaderStageFlagBits stage)
{
    if (spirvCode.empty() || spirvCode.size() % 4 != 0) {
        INXLOG_ERROR("Invalid SPIR-V code size: ", spirvCode.size());
        return false;
    }

    // Convert char vector to uint32_t vector
    std::vector<uint32_t> spirv(spirvCode.size() / 4);
    std::memcpy(spirv.data(), spirvCode.data(), spirvCode.size());

    return Reflect(spirv, stage);
}

bool ShaderReflection::Reflect(const std::vector<uint32_t> &spirvCode, VkShaderStageFlagBits stage)
{
    Clear();
    m_stage = stage;

    if (spirvCode.empty()) {
        INXLOG_ERROR("Empty SPIR-V code");
        return false;
    }

    try {
        spirv_cross::Compiler compiler(spirvCode);

        // Get all shader resources
        spirv_cross::ShaderResources resources = compiler.get_shader_resources();

        // Process uniform buffers
        for (const auto &ubo : resources.uniform_buffers) {
            UniformBufferInfo info;
            info.name = ubo.name;
            info.binding = compiler.get_decoration(ubo.id, spv::DecorationBinding);
            info.set = compiler.get_decoration(ubo.id, spv::DecorationDescriptorSet);
            info.stageFlags = stage;

            // Get buffer size
            const auto &type = compiler.get_type(ubo.base_type_id);
            info.size = static_cast<uint32_t>(compiler.get_declared_struct_size(type));

            // Get member info
            uint32_t memberCount = static_cast<uint32_t>(type.member_types.size());
            for (uint32_t i = 0; i < memberCount; ++i) {
                UniformMember member;
                member.name = compiler.get_member_name(ubo.base_type_id, i);
                member.offset = compiler.type_struct_member_offset(type, i);
                member.size = static_cast<uint32_t>(compiler.get_declared_struct_member_size(type, i));

                // Get array size if applicable
                const auto &memberType = compiler.get_type(type.member_types[i]);
                member.arraySize = memberType.array.empty() ? 1 : memberType.array[0];

                // Infer VkFormat from type
                member.format = VK_FORMAT_UNDEFINED;
                if (memberType.basetype == spirv_cross::SPIRType::Float) {
                    if (memberType.columns == 1) {
                        switch (memberType.vecsize) {
                        case 1:
                            member.format = VK_FORMAT_R32_SFLOAT;
                            break;
                        case 2:
                            member.format = VK_FORMAT_R32G32_SFLOAT;
                            break;
                        case 3:
                            member.format = VK_FORMAT_R32G32B32_SFLOAT;
                            break;
                        case 4:
                            member.format = VK_FORMAT_R32G32B32A32_SFLOAT;
                            break;
                        }
                    }
                } else if (memberType.basetype == spirv_cross::SPIRType::Int) {
                    switch (memberType.vecsize) {
                    case 1:
                        member.format = VK_FORMAT_R32_SINT;
                        break;
                    case 2:
                        member.format = VK_FORMAT_R32G32_SINT;
                        break;
                    case 3:
                        member.format = VK_FORMAT_R32G32B32_SINT;
                        break;
                    case 4:
                        member.format = VK_FORMAT_R32G32B32A32_SINT;
                        break;
                    }
                }

                info.members.push_back(member);
            }

            m_uniformBuffers.push_back(info);
            INXLOG_DEBUG("Reflected UBO: ", info.name, " binding=", info.binding, " set=", info.set,
                         " size=", info.size);
        }

        // Process sampled images (combined image samplers and separate textures)
        for (const auto &image : resources.sampled_images) {
            SampledImageInfo info;
            info.name = image.name;
            info.binding = compiler.get_decoration(image.id, spv::DecorationBinding);
            info.set = compiler.get_decoration(image.id, spv::DecorationDescriptorSet);
            info.stageFlags = stage;
            info.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;

            const auto &type = compiler.get_type(image.type_id);
            info.arraySize = type.array.empty() ? 1 : type.array[0];

            m_sampledImages.push_back(info);
            INXLOG_DEBUG("Reflected sampled image: ", info.name, " binding=", info.binding, " set=", info.set);
        }

        // Process separate samplers
        for (const auto &sampler : resources.separate_samplers) {
            SampledImageInfo info;
            info.name = sampler.name;
            info.binding = compiler.get_decoration(sampler.id, spv::DecorationBinding);
            info.set = compiler.get_decoration(sampler.id, spv::DecorationDescriptorSet);
            info.stageFlags = stage;
            info.arraySize = 1;
            info.descriptorType = VK_DESCRIPTOR_TYPE_SAMPLER;

            m_sampledImages.push_back(info);
            INXLOG_DEBUG("Reflected separate sampler: ", info.name, " binding=", info.binding);
        }

        // Process separate images
        for (const auto &image : resources.separate_images) {
            SampledImageInfo info;
            info.name = image.name;
            info.binding = compiler.get_decoration(image.id, spv::DecorationBinding);
            info.set = compiler.get_decoration(image.id, spv::DecorationDescriptorSet);
            info.stageFlags = stage;
            info.descriptorType = VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;

            const auto &type = compiler.get_type(image.type_id);
            info.arraySize = type.array.empty() ? 1 : type.array[0];

            m_sampledImages.push_back(info);
            INXLOG_DEBUG("Reflected separate image: ", info.name, " binding=", info.binding);
        }

        for (const auto &buffer : resources.storage_buffers) {
            StorageBufferInfo info;
            info.name = buffer.name;
            info.binding = compiler.get_decoration(buffer.id, spv::DecorationBinding);
            info.set = compiler.get_decoration(buffer.id, spv::DecorationDescriptorSet);
            info.stageFlags = stage;
            const auto &type = compiler.get_type(buffer.type_id);
            info.arraySize = type.array.empty() ? 1 : type.array[0];
            const auto accessFlags = compiler.get_buffer_block_flags(buffer.id);
            info.readOnly = accessFlags.get(spv::DecorationNonWritable);
            info.writeOnly = accessFlags.get(spv::DecorationNonReadable);
            m_storageBuffers.push_back(info);
            INXLOG_DEBUG("Reflected storage buffer: ", info.name, " binding=", info.binding, " set=", info.set);
        }

        for (const auto &image : resources.storage_images) {
            StorageImageInfo info;
            info.name = image.name;
            info.binding = compiler.get_decoration(image.id, spv::DecorationBinding);
            info.set = compiler.get_decoration(image.id, spv::DecorationDescriptorSet);
            info.stageFlags = stage;
            const auto &type = compiler.get_type(image.type_id);
            info.arraySize = type.array.empty() ? 1 : type.array[0];
            info.readOnly = type.image.access == spv::AccessQualifierReadOnly ||
                            compiler.has_decoration(image.id, spv::DecorationNonWritable);
            info.writeOnly = type.image.access == spv::AccessQualifierWriteOnly ||
                             compiler.has_decoration(image.id, spv::DecorationNonReadable);
            m_storageImages.push_back(info);
            INXLOG_DEBUG("Reflected storage image: ", info.name, " binding=", info.binding, " set=", info.set);
        }

        // Process push constants
        for (const auto &pc : resources.push_constant_buffers) {
            PushConstantInfo info;
            info.name = pc.name;
            info.stageFlags = stage;

            const auto &type = compiler.get_type(pc.base_type_id);
            info.size = static_cast<uint32_t>(compiler.get_declared_struct_size(type));
            info.offset = 0; // Push constants start at offset 0

            // Get member info
            uint32_t memberCount = static_cast<uint32_t>(type.member_types.size());
            for (uint32_t i = 0; i < memberCount; ++i) {
                UniformMember member;
                member.name = compiler.get_member_name(pc.base_type_id, i);
                member.offset = compiler.type_struct_member_offset(type, i);
                member.size = static_cast<uint32_t>(compiler.get_declared_struct_member_size(type, i));

                const auto &memberType = compiler.get_type(type.member_types[i]);
                member.arraySize = memberType.array.empty() ? 1 : memberType.array[0];
                member.format = VK_FORMAT_UNDEFINED;

                info.members.push_back(member);
            }

            m_pushConstants.push_back(info);
            INXLOG_DEBUG("Reflected push constant: ", info.name, " size=", info.size);
        }

        // Process stage inputs
        for (const auto &input : resources.stage_inputs) {
            ShaderIOVariable var;
            var.name = input.name;
            var.location = compiler.get_decoration(input.id, spv::DecorationLocation);

            const auto &type = compiler.get_type(input.base_type_id);
            var.format = StageIoFormat(type);

            m_inputs.push_back(var);
        }

        // Process stage outputs
        for (const auto &output : resources.stage_outputs) {
            ShaderIOVariable var;
            var.name = output.name;
            var.location = compiler.get_decoration(output.id, spv::DecorationLocation);

            const auto &type = compiler.get_type(output.base_type_id);
            var.format = StageIoFormat(type);

            m_outputs.push_back(var);
        }

        INXLOG_DEBUG("Shader reflection complete: ", m_uniformBuffers.size(), " UBOs, ", m_sampledImages.size(),
                     " sampled resources, ", m_storageBuffers.size(), " storage buffers, ", m_storageImages.size(),
                     " storage images");

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("SPIRV-Cross reflection failed: ", e.what());
        return false;
    }
}

std::vector<VkDescriptorSetLayoutBinding> ShaderReflection::GetDescriptorSetLayoutBindings(uint32_t set) const
{
    std::vector<VkDescriptorSetLayoutBinding> bindings;

    // Add uniform buffer bindings
    for (const auto &ubo : m_uniformBuffers) {
        if (ubo.set == set) {
            VkDescriptorSetLayoutBinding binding{};
            binding.binding = ubo.binding;
            binding.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            binding.descriptorCount = 1;
            binding.stageFlags = ubo.stageFlags;
            binding.pImmutableSamplers = nullptr;
            bindings.push_back(binding);
        }
    }

    // Add sampled image bindings
    for (const auto &image : m_sampledImages) {
        if (image.set == set) {
            VkDescriptorSetLayoutBinding binding{};
            binding.binding = image.binding;
            binding.descriptorType = image.descriptorType;
            binding.descriptorCount = image.arraySize;
            binding.stageFlags = image.stageFlags;
            binding.pImmutableSamplers = nullptr;
            bindings.push_back(binding);
        }
    }

    for (const auto &buffer : m_storageBuffers) {
        if (buffer.set == set) {
            VkDescriptorSetLayoutBinding binding{};
            binding.binding = buffer.binding;
            binding.descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            binding.descriptorCount = buffer.arraySize;
            binding.stageFlags = buffer.stageFlags;
            bindings.push_back(binding);
        }
    }

    for (const auto &image : m_storageImages) {
        if (image.set == set) {
            VkDescriptorSetLayoutBinding binding{};
            binding.binding = image.binding;
            binding.descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
            binding.descriptorCount = image.arraySize;
            binding.stageFlags = image.stageFlags;
            bindings.push_back(binding);
        }
    }

    std::sort(bindings.begin(), bindings.end(),
              [](const auto &left, const auto &right) { return left.binding < right.binding; });

    return bindings;
}

std::vector<uint32_t> ShaderReflection::GetUsedDescriptorSets() const
{
    std::vector<uint32_t> sets;

    for (const auto &ubo : m_uniformBuffers) {
        if (std::find(sets.begin(), sets.end(), ubo.set) == sets.end()) {
            sets.push_back(ubo.set);
        }
    }

    for (const auto &image : m_sampledImages) {
        if (std::find(sets.begin(), sets.end(), image.set) == sets.end()) {
            sets.push_back(image.set);
        }
    }

    for (const auto &buffer : m_storageBuffers) {
        if (std::find(sets.begin(), sets.end(), buffer.set) == sets.end())
            sets.push_back(buffer.set);
    }

    for (const auto &image : m_storageImages) {
        if (std::find(sets.begin(), sets.end(), image.set) == sets.end())
            sets.push_back(image.set);
    }

    std::sort(sets.begin(), sets.end());
    return sets;
}

void ShaderReflection::Clear()
{
    m_uniformBuffers.clear();
    m_sampledImages.clear();
    m_storageBuffers.clear();
    m_storageImages.clear();
    m_pushConstants.clear();
    m_inputs.clear();
    m_outputs.clear();
}

} // namespace infernux
