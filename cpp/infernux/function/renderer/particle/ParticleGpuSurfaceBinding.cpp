#include "ParticleGpuSurfaceBinding.h"

#include <core/config/EngineConfig.h>
#include <core/types/ColorSpace.h>
#include <function/renderer/rhi/GpuRetirementQueue.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstring>

namespace infernux::particle
{

namespace
{

rhi::ShaderStage ToRhiStages(ShaderProgramStageMask stages) noexcept
{
    rhi::ShaderStage result = rhi::ShaderStage::None;
    if (HasStage(stages, ShaderProgramStageMask::Vertex))
        result = result | rhi::ShaderStage::Vertex;
    if (HasStage(stages, ShaderProgramStageMask::Fragment))
        result = result | rhi::ShaderStage::Fragment;
    return result;
}

template <typename T> bool WriteValue(std::vector<uint8_t> &bytes, uint32_t offset, const T &value)
{
    if (offset > bytes.size() || sizeof(T) > bytes.size() - offset)
        return false;
    std::memcpy(bytes.data() + offset, &value, sizeof(T));
    return true;
}

bool WriteMaterialProperty(std::vector<uint8_t> &bytes, const ShaderProgramPropertyBinding &binding,
                           const MaterialProperty &property)
{
    if (!binding.bufferOffset)
        return false;
    const uint32_t offset = *binding.bufferOffset;
    if (binding.type == "Float" && property.type == MaterialPropertyType::Float)
        return WriteValue(bytes, offset, std::get<float>(property.value));
    if (binding.type == "Float2" && property.type == MaterialPropertyType::Float2)
        return WriteValue(bytes, offset, std::get<glm::vec2>(property.value));
    if (binding.type == "Float3" && property.type == MaterialPropertyType::Float3)
        return WriteValue(bytes, offset, std::get<glm::vec3>(property.value));
    if ((binding.type == "Float4" || binding.type == "Color") &&
        (property.type == MaterialPropertyType::Float4 || property.type == MaterialPropertyType::Color)) {
        glm::vec4 value = std::get<glm::vec4>(property.value);
        if (property.type == MaterialPropertyType::Color)
            value = inx::color::SrgbToLinear(value);
        return WriteValue(bytes, offset, value);
    }
    if (binding.type == "Int" && property.type == MaterialPropertyType::Int)
        return WriteValue(bytes, offset, std::get<int>(property.value));
    if (binding.type == "Mat4" && property.type == MaterialPropertyType::Mat4)
        return WriteValue(bytes, offset, std::get<glm::mat4>(property.value));
    return false;
}

bool WriteDefaultProperty(std::vector<uint8_t> &bytes, const ShaderProgramPropertyBinding &binding)
{
    if (!binding.bufferOffset || binding.defaultValue.empty())
        return false;
    const auto value = nlohmann::json::parse(binding.defaultValue, nullptr, false);
    if (value.is_discarded())
        return false;
    const uint32_t offset = *binding.bufferOffset;
    if (binding.type == "Float" && value.is_number())
        return WriteValue(bytes, offset, value.get<float>());
    if (binding.type == "Int" && value.is_number_integer())
        return WriteValue(bytes, offset, value.get<int>());
    if (!value.is_array())
        return false;

    auto readFloatArray = [&](float *destination, size_t count) {
        if (value.size() != count)
            return false;
        for (size_t index = 0; index < count; ++index) {
            if (!value[index].is_number())
                return false;
            destination[index] = value[index].get<float>();
        }
        return true;
    };
    if (binding.type == "Float2") {
        glm::vec2 result{};
        return readFloatArray(&result[0], 2) && WriteValue(bytes, offset, result);
    }
    if (binding.type == "Float3") {
        glm::vec3 result{};
        return readFloatArray(&result[0], 3) && WriteValue(bytes, offset, result);
    }
    if (binding.type == "Float4" || binding.type == "Color") {
        glm::vec4 result{};
        if (!readFloatArray(&result[0], 4))
            return false;
        if (binding.type == "Color")
            result = inx::color::SrgbToLinear(result);
        return WriteValue(bytes, offset, result);
    }
    if (binding.type == "Mat4") {
        glm::mat4 result{0.0f};
        return readFloatArray(&result[0][0], 16) && WriteValue(bytes, offset, result);
    }
    return false;
}

} // namespace

ParticleGpuSurfaceBinding::~ParticleGpuSurfaceBinding()
{
    Destroy();
}

bool ParticleGpuSurfaceBinding::Create(rhi::Device &device, std::shared_ptr<const ShaderProgramArtifact> shaderProgram,
                                       std::shared_ptr<InxMaterial> material,
                                       GpuBillboardMaterialState fallbackMaterial, ParticleOutputSemantics semantics,
                                       GpuBillboardTextureResolver textureResolver, GpuRetirementQueue *deletionQueue)
{
    Destroy();
    if (!shaderProgram || !shaderProgram->IsValid() || shaderProgram->domain != ShaderProgramDomain::ParticleSprite)
        return false;

    const auto fail = [this]() {
        Destroy();
        return false;
    };

    m_device = &device;
    m_shaderProgram = std::move(shaderProgram);
    m_material = std::move(material);
    m_fallbackMaterial = fallbackMaterial;
    m_semantics = semantics;
    m_textureResolver = std::move(textureResolver);
    m_deletionQueue = deletionQueue;
    m_supportsSceneDepth = m_shaderProgram->usesParticleSceneDepthBinding;
    m_usesBindlessTextures = m_shaderProgram->usesBindlessTextureABI;
    if (m_usesBindlessTextures && !device.GetBindlessTextureTableBinding().IsValid())
        return fail();

    rhi::BindingLayoutDesc layoutDesc;
    for (const auto &property : m_shaderProgram->properties) {
        if (!property.textureSlot)
            continue;
        const uint32_t binding = 2u + *property.textureSlot;
        const auto visibility = ToRhiStages(property.stages);
        if (binding < 2u || binding >= 14u || visibility == rhi::ShaderStage::None ||
            layoutDesc.entryCount >= rhi::BindingLayoutDesc::MaxEntries)
            return fail();
        if (!m_usesBindlessTextures) {
            layoutDesc.entries[layoutDesc.entryCount++] = {binding, rhi::BindingType::CombinedTextureSampler,
                                                           visibility, 1};
        }
        m_textures.push_back({binding, *property.textureSlot, visibility, property.name, property.textureDefault});
    }
    if (m_usesBindlessTextures) {
        if (layoutDesc.entryCount >= rhi::BindingLayoutDesc::MaxEntries)
            return fail();
        layoutDesc.entries[layoutDesc.entryCount++] = {2, rhi::BindingType::UniformBuffer, rhi::ShaderStage::Fragment,
                                                       1};
        uint32_t textureCount = 1;
        for (const auto &binding : m_textures)
            textureCount = (std::max)(textureCount, binding.textureSlot + 1u);
        rhi::BufferDesc indexBufferDesc;
        indexBufferDesc.byteSize = (textureCount * sizeof(uint32_t) + 15u) & ~15ull;
        indexBufferDesc.usage = rhi::BufferUsageFlags::Uniform;
        indexBufferDesc.memory = rhi::BufferMemory::Upload;
        m_textureIndexBuffer = device.CreateBuffer(indexBufferDesc);
        if (!m_textureIndexBuffer.IsValid())
            return fail();
    }
    if (m_shaderProgram->materialBufferSize > 0) {
        if (layoutDesc.entryCount >= rhi::BindingLayoutDesc::MaxEntries)
            return fail();
        layoutDesc.entries[layoutDesc.entryCount++] = {14, rhi::BindingType::UniformBuffer,
                                                       rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, 1};
        rhi::BufferDesc bufferDesc;
        bufferDesc.byteSize = m_shaderProgram->materialBufferSize;
        bufferDesc.usage = rhi::BufferUsageFlags::Uniform;
        bufferDesc.memory = rhi::BufferMemory::Upload;
        m_materialBuffer = device.CreateBuffer(bufferDesc);
        if (!m_materialBuffer.IsValid())
            return fail();
    }
    if (m_supportsSceneDepth) {
        if (layoutDesc.entryCount >= rhi::BindingLayoutDesc::MaxEntries)
            return fail();
        layoutDesc.entries[layoutDesc.entryCount++] = {15, rhi::BindingType::CombinedTextureSampler,
                                                       rhi::ShaderStage::Fragment, 1};
        if (m_textures.empty()) {
            if (!m_textureResolver)
                return fail();
            m_sceneDepthFallback = m_textureResolver("white", "_InxParticleSceneDepth");
            if (m_sceneDepthFallback.status != GpuBillboardTextureStatus::Ready ||
                !m_sceneDepthFallback.texture.IsValid() || !m_sceneDepthFallback.sampler.IsValid() ||
                !m_sceneDepthFallback.gpuView || !m_sceneDepthFallback.gpuView->IsValid())
                return fail();
        }
    }

    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid())
        return fail();
    m_usesTexture = !m_textures.empty();
    if (!RefreshMaterialBuffer(true) || !RefreshTextureBindings(true)) {
        Destroy();
        return false;
    }
    return true;
}

void ParticleGpuSurfaceBinding::Destroy() noexcept
{
    if (m_device) {
        RetireBindGroup(m_group);
        RetireViewBindGroups();
        m_device->Release(m_materialBuffer);
        m_device->Release(m_textureIndexBuffer);
        m_device->Release(m_layout);
    }
    m_device = nullptr;
    m_shaderProgram.reset();
    m_material.reset();
    m_fallbackMaterial = {};
    m_semantics = {};
    m_textureResolver = {};
    m_deletionQueue = nullptr;
    m_layout = {};
    m_group = {};
    m_viewGroups.clear();
    m_materialBuffer = {};
    m_textureIndexBuffer = {};
    m_textures.clear();
    m_sceneDepthFallback = {};
    m_materialVersion = 0;
    m_materialVersionInitialized = false;
    m_usesTexture = false;
    m_usesBindlessTextures = false;
    m_supportsSceneDepth = false;
}

bool ParticleGpuSurfaceBinding::IsValid() const noexcept
{
    return m_device && m_shaderProgram && m_layout.IsValid() && m_group.IsValid();
}

GpuBillboardMaterialState ParticleGpuSurfaceBinding::ResolveMaterialState() const noexcept
{
    GpuBillboardMaterialState state = m_fallbackMaterial;
    if (m_material && !m_material->IsDeleted()) {
        const auto &renderState = m_material->GetRenderState();
        state = {renderState.renderQueue, renderState.blendEnable, renderState.depthTestEnable,
                 renderState.depthWriteEnable,
                 renderState.srcColorBlendFactor == VK_BLEND_FACTOR_ONE &&
                     renderState.dstColorBlendFactor == VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA};
    }
    if (m_semantics.softParticles) {
        state.renderQueue = std::max(state.renderQueue, EngineConfig::Get().transparentQueueMin);
        state.blendEnabled = true;
        state.depthWriteEnabled = false;
    }
    return state;
}

std::array<float, 4> ParticleGpuSurfaceBinding::ResolveMaterialTint() const noexcept
{
    if (!m_material || m_material->IsDeleted())
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const auto *property = m_material->GetProperty("baseColor");
    if (!property || (property->type != MaterialPropertyType::Color && property->type != MaterialPropertyType::Float4))
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const auto *value = std::get_if<glm::vec4>(&property->value);
    if (!value)
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const glm::vec4 tint = property->type == MaterialPropertyType::Color ? inx::color::SrgbToLinear(*value) : *value;
    return {tint.x, tint.y, tint.z, tint.w};
}

float ParticleGpuSurfaceBinding::ResolveMaterialFloat(const char *name, float fallback) const noexcept
{
    if (!name || !m_material || m_material->IsDeleted())
        return fallback;
    const auto *property = m_material->GetProperty(name);
    if (!property || property->type != MaterialPropertyType::Float)
        return fallback;
    const auto *value = std::get_if<float>(&property->value);
    return value ? *value : fallback;
}

std::string ParticleGpuSurfaceBinding::ResolveMaterialTextureGuid(const TextureBindingState &binding) const
{
    if (!m_material || m_material->IsDeleted())
        return binding.defaultGuid;
    const auto *property = m_material->GetProperty(binding.name);
    if (!property || property->type != MaterialPropertyType::Texture2D)
        return binding.defaultGuid;
    const auto *value = std::get_if<std::string>(&property->value);
    return value && !value->empty() ? *value : binding.defaultGuid;
}

void ParticleGpuSurfaceBinding::RetireBindGroup(rhi::BindGroupHandle group)
{
    if (!m_device || !group.IsValid())
        return;
    auto release = [device = m_device, group] { device->Release(group); };
    if (m_deletionQueue)
        m_deletionQueue->Retire(std::move(release));
    else
        release();
}

void ParticleGpuSurfaceBinding::RetireTexture(std::shared_ptr<const rhi::TextureGpuView> gpuView)
{
    if (!gpuView)
        return;
    auto release = [gpuView = std::move(gpuView)]() mutable { gpuView.reset(); };
    if (m_deletionQueue)
        m_deletionQueue->Retire(std::move(release));
    else
        release();
}

bool ParticleGpuSurfaceBinding::RefreshMaterialBuffer(bool force)
{
    if (m_shaderProgram->materialBufferSize == 0)
        return true;
    if (!m_materialBuffer.IsValid())
        return false;
    const uint64_t version = m_material && !m_material->IsDeleted() ? m_material->GetVersion() : 0;
    if (!force && m_materialVersionInitialized && version == m_materialVersion)
        return true;

    std::vector<uint8_t> bytes(m_shaderProgram->materialBufferSize, 0);
    for (const auto &binding : m_shaderProgram->properties) {
        if (!binding.bufferOffset)
            continue;
        (void)WriteDefaultProperty(bytes, binding);
        if (m_material && !m_material->IsDeleted()) {
            if (const auto *property = m_material->GetProperty(binding.name))
                (void)WriteMaterialProperty(bytes, binding, *property);
        }
    }
    if (m_shaderProgram->alphaClipThresholdOffset) {
        float threshold = 0.0f;
        if (m_material && !m_material->IsDeleted()) {
            const RenderState &renderState = m_material->GetRenderState();
            if (renderState.alphaClipEnabled)
                threshold = renderState.alphaClipThreshold;
        }
        (void)WriteValue(bytes, *m_shaderProgram->alphaClipThresholdOffset, threshold);
    }
    if (!m_device->WriteBuffer(m_materialBuffer, 0, bytes.data(), bytes.size()))
        return false;
    m_materialVersion = version;
    m_materialVersionInitialized = true;
    return true;
}

rhi::BindGroupHandle ParticleGpuSurfaceBinding::CreateBindGroup(const std::vector<TextureBindingState> &textures,
                                                                rhi::TextureViewHandle sceneDepth,
                                                                bool sceneDepthIsDepth) const
{
    if (!m_device || !m_layout.IsValid())
        return {};
    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    if (m_materialBuffer.IsValid())
        groupDesc.buffers[groupDesc.bufferCount++] = {14, rhi::BindingType::UniformBuffer, m_materialBuffer, 0,
                                                      m_shaderProgram->materialBufferSize};
    if (m_usesBindlessTextures) {
        if (!m_textureIndexBuffer.IsValid())
            return {};
        groupDesc.buffers[groupDesc.bufferCount++] = {2, rhi::BindingType::UniformBuffer, m_textureIndexBuffer, 0, 0};
    } else {
        for (const auto &binding : textures) {
            if (!binding.texture.IsValid() || !binding.sampler.IsValid() ||
                groupDesc.textureCount >= rhi::BindGroupDesc::MaxTextureBindings)
                return {};
            groupDesc.textures[groupDesc.textureCount++] = {binding.binding, rhi::BindingType::CombinedTextureSampler,
                                                            binding.texture, binding.sampler, false};
        }
    }
    if (!m_supportsSceneDepth)
        return m_device->CreateBindGroup(groupDesc);

    const rhi::TextureViewHandle fallbackTexture =
        !textures.empty() ? textures.front().texture : m_sceneDepthFallback.texture;
    const rhi::SamplerHandle fallbackSampler =
        !textures.empty() ? textures.front().sampler : m_sceneDepthFallback.sampler;
    const bool readsSceneDepth = sceneDepth.IsValid();
    const rhi::TextureViewHandle depthTexture = readsSceneDepth ? sceneDepth : fallbackTexture;
    if (!depthTexture.IsValid() || !fallbackSampler.IsValid() ||
        groupDesc.textureCount >= rhi::BindGroupDesc::MaxTextureBindings)
        return {};
    groupDesc.textures[groupDesc.textureCount++] = {15, rhi::BindingType::CombinedTextureSampler, depthTexture,
                                                    fallbackSampler, readsSceneDepth && sceneDepthIsDepth};
    return m_device->CreateBindGroup(groupDesc);
}

bool ParticleGpuSurfaceBinding::RefreshTextureIndexBuffer(const std::vector<TextureBindingState> &textures)
{
    if (!m_usesBindlessTextures)
        return true;
    if (!m_device || !m_textureIndexBuffer.IsValid())
        return false;

    uint32_t textureCount = 1;
    for (const auto &binding : textures)
        textureCount = (std::max)(textureCount, binding.textureSlot + 1u);
    std::vector<uint32_t> indices(((textureCount * sizeof(uint32_t) + 15u) & ~15ull) / sizeof(uint32_t),
                                  rhi::ResourceIndex::FallbackIndex);
    for (const auto &binding : textures) {
        if (binding.textureSlot < indices.size() && binding.resourceIndex.IsValid())
            indices[binding.textureSlot] = binding.resourceIndex.index;
    }
    return m_device->WriteBuffer(m_textureIndexBuffer, 0, indices.data(), indices.size() * sizeof(uint32_t));
}

void ParticleGpuSurfaceBinding::MarkBindlessTexturesUsed() noexcept
{
    if (!m_device || !m_usesBindlessTextures)
        return;
    std::array<rhi::ResourceIndex, 12> resources{};
    size_t count = 0;
    for (const auto &binding : m_textures) {
        if (binding.resourceIndex.IsValid() && count < resources.size())
            resources[count++] = binding.resourceIndex;
    }
    m_device->MarkBindlessTexturesUsed(resources.data(), count);
}

bool ParticleGpuSurfaceBinding::RebuildBindGroup()
{
    const auto group = CreateBindGroup(m_textures);
    if (!group.IsValid())
        return false;
    RetireBindGroup(m_group);
    RetireViewBindGroups();
    m_group = group;
    return true;
}

void ParticleGpuSurfaceBinding::RetireViewBindGroups()
{
    for (const auto &entry : m_viewGroups)
        RetireBindGroup(entry.group);
    m_viewGroups.clear();
}

rhi::BindGroupHandle ParticleGpuSurfaceBinding::ResolveBindGroup(rhi::TextureViewHandle sceneDepth,
                                                                 bool sceneDepthIsDepth)
{
    if (!sceneDepth.IsValid())
        return m_group;
    const auto existing = std::find_if(m_viewGroups.begin(), m_viewGroups.end(), [&](const auto &entry) {
        return entry.sceneDepth == sceneDepth && entry.sceneDepthIsDepth == sceneDepthIsDepth;
    });
    if (existing != m_viewGroups.end())
        return existing->group;
    const auto group = CreateBindGroup(m_textures, sceneDepth, sceneDepthIsDepth);
    if (group.IsValid())
        m_viewGroups.push_back({sceneDepth, sceneDepthIsDepth, group});
    return group;
}

bool ParticleGpuSurfaceBinding::RefreshTextureBindings(bool force)
{
    if (!m_usesTexture) {
        return (!m_usesBindlessTextures || RefreshTextureIndexBuffer(m_textures)) &&
               (m_group.IsValid() || RebuildBindGroup());
    }
    if (!m_textureResolver)
        return false;

    auto candidate = m_textures;
    std::vector<size_t> changed;
    changed.reserve(candidate.size());
    for (size_t index = 0; index < candidate.size(); ++index) {
        auto &binding = candidate[index];
        const std::string textureGuid = ResolveMaterialTextureGuid(binding);
        GpuBillboardTextureLease lease;
        if (!force && !binding.pending && textureGuid == binding.requestedGuid && binding.gpuSlot &&
            !binding.gpuSlot->NeedsRefresh()) {
            auto published = binding.gpuSlot->Acquire();
            if (published && published->IsValid()) {
                if (binding.gpuView && binding.gpuView->GetRevision() == published->GetRevision() &&
                    binding.gpuView->GetSourceId() == published->GetSourceId())
                    continue;
                lease.status = GpuBillboardTextureStatus::Ready;
                lease.texture = published->GetView();
                lease.sampler = published->GetSampler();
                lease.gpuSlot = binding.gpuSlot;
                lease.gpuView = std::move(published);
            }
        }
        if (lease.status != GpuBillboardTextureStatus::Ready)
            lease = m_textureResolver(textureGuid, binding.name);
        const bool pending = lease.status == GpuBillboardTextureStatus::Pending;
        if (pending && textureGuid == binding.requestedGuid && binding.gpuView && binding.gpuView->IsValid() &&
            binding.texture.IsValid() && binding.sampler.IsValid()) {
            binding.pending = true;
            continue;
        }
        bool usingFallback = false;
        if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() || !lease.sampler.IsValid() ||
            !lease.gpuView || !lease.gpuView->IsValid()) {
            const std::string fallbackGuid = !binding.defaultGuid.empty() && binding.defaultGuid != textureGuid
                                                 ? binding.defaultGuid
                                                 : std::string{};
            lease = m_textureResolver(fallbackGuid, binding.name);
            usingFallback = true;
        }
        if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() || !lease.sampler.IsValid() ||
            !lease.gpuView || !lease.gpuView->IsValid()) {
            if (binding.texture.IsValid() && binding.sampler.IsValid()) {
                binding.pending = pending;
                continue;
            }
            return m_group.IsValid();
        }
        if (!force && !binding.pending && textureGuid == binding.requestedGuid && binding.gpuView &&
            binding.gpuView->GetSourceId() == lease.gpuView->GetSourceId() &&
            binding.gpuView->GetRevision() == lease.gpuView->GetRevision())
            continue;

        binding.texture = lease.texture;
        binding.sampler = lease.sampler;
        binding.gpuSlot = std::move(lease.gpuSlot);
        binding.gpuView = std::move(lease.gpuView);
        binding.resourceIndex =
            m_usesBindlessTextures ? m_device->PublishBindlessTexture(binding.gpuView) : rhi::ResourceIndex{};
        binding.requestedGuid = textureGuid;
        binding.requestedVersion = binding.gpuView->GetRevision();
        binding.pending = pending;
        binding.fallback = usingFallback;
        changed.push_back(index);
    }
    if (changed.empty())
        return m_group.IsValid();

    if (!RefreshTextureIndexBuffer(candidate))
        return m_group.IsValid();

    if (!m_usesBindlessTextures || !m_group.IsValid()) {
        const auto group = CreateBindGroup(candidate);
        if (!group.IsValid())
            return m_group.IsValid();
        RetireBindGroup(m_group);
        RetireViewBindGroups();
        m_group = group;
    }
    for (const size_t index : changed)
        RetireTexture(std::move(m_textures[index].gpuView));
    m_textures = std::move(candidate);
    return true;
}

} // namespace infernux::particle
