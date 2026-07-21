#include "ParticleGpuBillboardRenderer.h"

#include <function/renderer/FrameDeletionQueue.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstring>

namespace infernux::particle
{

namespace
{

uint8_t PipelineStateSignature(const GpuBillboardMaterialState &state) noexcept
{
    return static_cast<uint8_t>((state.blendEnabled ? 1u : 0u) | (state.depthTestEnabled ? 2u : 0u) |
                                (state.depthWriteEnabled ? 4u : 0u));
}

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
        return WriteValue(bytes, offset, std::get<glm::vec4>(property.value));
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
        return readFloatArray(&result[0], 4) && WriteValue(bytes, offset, result);
    }
    if (binding.type == "Mat4") {
        glm::mat4 result{0.0f};
        return readFloatArray(&result[0][0], 16) && WriteValue(bytes, offset, result);
    }
    return false;
}

std::vector<uint32_t> CopySpirvWords(const std::vector<char> &bytes)
{
    if (bytes.empty() || bytes.size() % sizeof(uint32_t) != 0)
        return {};
    std::vector<uint32_t> words(bytes.size() / sizeof(uint32_t));
    std::memcpy(words.data(), bytes.data(), bytes.size());
    return words;
}

} // namespace

ParticleGpuBillboardRenderer::~ParticleGpuBillboardRenderer()
{
    Destroy();
}

bool ParticleGpuBillboardRenderer::Create(rhi::Device &device, const GpuBillboardRendererDesc &desc)
{
    Destroy();
    if (!desc.instances.IsValid())
        return false;

    const ShaderProgramArtifact::PassVariant *linkedVariant = nullptr;
    std::vector<uint32_t> linkedVertexWords;
    std::vector<uint32_t> linkedFragmentWords;
    if (desc.shaderProgram) {
        if (!desc.renderIndices.IsValid() || !desc.shaderProgram->IsValid() ||
            desc.shaderProgram->domain != ShaderProgramDomain::ParticleSprite)
            return false;
        linkedVariant = desc.shaderProgram->FindVariant(ShaderCompileTarget::Forward);
        if (!linkedVariant)
            return false;
        linkedVertexWords = CopySpirvWords(linkedVariant->vertexSpirv);
        linkedFragmentWords = CopySpirvWords(linkedVariant->fragmentSpirv);
        if (linkedVertexWords.empty() || linkedFragmentWords.empty())
            return false;
    } else if (!desc.vertexShader.words || desc.vertexShader.wordCount == 0 || !desc.fragmentShader.words ||
               desc.fragmentShader.wordCount == 0) {
        return false;
    }

    m_device = &device;
    m_material = desc.material;
    m_shaderProgram = desc.shaderProgram;
    m_fallbackMaterial = desc.fallbackMaterial;
    m_textureResolver = desc.textureResolver;
    m_textureVersionResolver = desc.textureVersionResolver;
    m_deletionQueue = desc.deletionQueue;
    m_instances = desc.instances;
    m_renderIndices = desc.renderIndices;
    m_vertexShader = linkedVariant ? device.CreateShaderModule({linkedVertexWords.data(), linkedVertexWords.size()})
                                   : device.CreateShaderModule({desc.vertexShader.words, desc.vertexShader.wordCount});
    m_fragmentShader = linkedVariant
                           ? device.CreateShaderModule({linkedFragmentWords.data(), linkedFragmentWords.size()})
                           : device.CreateShaderModule({desc.fragmentShader.words, desc.fragmentShader.wordCount});
    if (desc.vertexShader.words && desc.vertexShader.wordCount &&
        desc.pickingFragmentShader.words && desc.pickingFragmentShader.wordCount) {
        m_pickingVertexShader =
            device.CreateShaderModule({desc.vertexShader.words, desc.vertexShader.wordCount});
        m_pickingFragmentShader = device.CreateShaderModule(
            {desc.pickingFragmentShader.words, desc.pickingFragmentShader.wordCount});
    }
    if (!m_vertexShader.IsValid() || !m_fragmentShader.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    layoutDesc.entries[layoutDesc.entryCount++] = {0, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    if (UsesLinkedProgram()) {
        layoutDesc.entries[layoutDesc.entryCount++] = {1, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
        for (const auto &property : m_shaderProgram->properties) {
            if (!property.textureSlot)
                continue;
            const uint32_t binding = 2u + *property.textureSlot;
            const auto visibility = ToRhiStages(property.stages);
            if (binding >= 14u || visibility == rhi::ShaderStage::None ||
                layoutDesc.entryCount >= rhi::BindingLayoutDesc::MaxEntries) {
                Destroy();
                return false;
            }
            layoutDesc.entries[layoutDesc.entryCount++] = {binding, rhi::BindingType::CombinedTextureSampler,
                                                           visibility, 1};
            m_textures.push_back({binding, visibility, property.name, property.textureDefault});
        }
        if (m_shaderProgram->materialBufferSize > 0) {
            if (layoutDesc.entryCount >= rhi::BindingLayoutDesc::MaxEntries) {
                Destroy();
                return false;
            }
            layoutDesc.entries[layoutDesc.entryCount++] = {14, rhi::BindingType::UniformBuffer,
                                                           rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, 1};
            rhi::BufferDesc bufferDesc;
            bufferDesc.byteSize = m_shaderProgram->materialBufferSize;
            bufferDesc.usage = rhi::BufferUsageFlags::Uniform;
            bufferDesc.memory = rhi::BufferMemory::Upload;
            m_materialBuffer = device.CreateBuffer(bufferDesc);
            if (!m_materialBuffer.IsValid()) {
                Destroy();
                return false;
            }
        }
    } else if (m_textureResolver) {
        layoutDesc.entries[layoutDesc.entryCount++] = {1, rhi::BindingType::CombinedTextureSampler,
                                                       rhi::ShaderStage::Fragment, 1};
        m_textures.push_back({1, rhi::ShaderStage::Fragment, "texSampler"});
    }
    m_usesTexture = !m_textures.empty();
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    const bool materialReady = RefreshMaterialBuffer(true);
    const bool bindingReady = materialReady && (m_usesTexture ? RefreshTextureBindings(true) : RebuildBindGroup());
    if (!bindingReady) {
        Destroy();
        return false;
    }
    return true;
}

void ParticleGpuBillboardRenderer::Destroy() noexcept
{
    if (m_device) {
        for (const auto &entry : m_pipelines)
            m_device->Release(entry.pipeline);
        m_device->Release(m_group);
        for (const auto &entry : m_viewGroups)
            m_device->Release(entry.group);
        for (const auto &binding : m_textures) {
            if (binding.releaseHandles) {
                m_device->Release(binding.texture);
                m_device->Release(binding.sampler);
            }
        }
        m_device->Release(m_materialBuffer);
        m_device->Release(m_layout);
        m_device->Release(m_fragmentShader);
        m_device->Release(m_vertexShader);
        m_device->Release(m_pickingFragmentShader);
        m_device->Release(m_pickingVertexShader);
    }
    m_device = nullptr;
    m_material.reset();
    m_shaderProgram.reset();
    m_fallbackMaterial = {};
    m_textureResolver = {};
    m_textureVersionResolver = {};
    m_deletionQueue = nullptr;
    m_instances = {};
    m_renderIndices = {};
    m_vertexShader = {};
    m_fragmentShader = {};
    m_pickingVertexShader = {};
    m_pickingFragmentShader = {};
    m_layout = {};
    m_group = {};
    m_viewGroups.clear();
    m_materialBuffer = {};
    m_textures.clear();
    m_materialVersion = 0;
    m_materialVersionInitialized = false;
    m_usesTexture = false;
    m_pipelines.clear();
}

bool ParticleGpuBillboardRenderer::IsValid() const noexcept
{
    return m_device && m_instances.IsValid() && (!UsesLinkedProgram() || m_renderIndices.IsValid()) &&
           m_vertexShader.IsValid() && m_fragmentShader.IsValid() && m_layout.IsValid() && m_group.IsValid();
}

int32_t ParticleGpuBillboardRenderer::RenderQueue() const noexcept
{
    return ResolveMaterialState().renderQueue;
}

bool ParticleGpuBillboardRenderer::UsesLinkedProgram() const noexcept
{
    return static_cast<bool>(m_shaderProgram);
}

GpuBillboardMaterialState ParticleGpuBillboardRenderer::ResolveMaterialState() const noexcept
{
    if (!m_material || m_material->IsDeleted())
        return m_fallbackMaterial;
    const auto &renderState = m_material->GetRenderState();
    return {renderState.renderQueue, renderState.blendEnable, renderState.depthTestEnable,
            renderState.depthWriteEnable};
}

std::array<float, 4> ParticleGpuBillboardRenderer::ResolveMaterialTint() const noexcept
{
    if (!m_material || m_material->IsDeleted())
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const auto *property = m_material->GetProperty("baseColor");
    if (!property || (property->type != MaterialPropertyType::Color && property->type != MaterialPropertyType::Float4))
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const auto *value = std::get_if<glm::vec4>(&property->value);
    return value ? std::array<float, 4>{value->x, value->y, value->z, value->w}
                 : std::array<float, 4>{1.0f, 1.0f, 1.0f, 1.0f};
}

std::string ParticleGpuBillboardRenderer::ResolveMaterialTextureGuid(const TextureBindingState &binding) const
{
    if (!m_material || m_material->IsDeleted())
        return binding.defaultGuid;
    const auto *property = m_material->GetProperty(binding.name);
    if (!property || property->type != MaterialPropertyType::Texture2D)
        return binding.defaultGuid;
    const auto *value = std::get_if<std::string>(&property->value);
    return value && !value->empty() ? *value : binding.defaultGuid;
}

void ParticleGpuBillboardRenderer::RetireBindGroup(rhi::BindGroupHandle group)
{
    if (!m_device || !group.IsValid())
        return;
    auto release = [device = m_device, group] { device->Release(group); };
    if (m_deletionQueue)
        m_deletionQueue->Push(std::move(release));
    else
        release();
}

void ParticleGpuBillboardRenderer::RetireTexture(rhi::TextureViewHandle texture, rhi::SamplerHandle sampler,
                                                 std::shared_ptr<void> keepAlive, bool releaseHandles)
{
    if (!m_device || (!texture.IsValid() && !sampler.IsValid() && !keepAlive))
        return;
    auto release = [device = m_device, texture, sampler, keepAlive = std::move(keepAlive), releaseHandles]() mutable {
        if (releaseHandles) {
            device->Release(texture);
            device->Release(sampler);
        }
        keepAlive.reset();
    };
    if (m_deletionQueue)
        m_deletionQueue->Push(std::move(release));
    else
        release();
}

bool ParticleGpuBillboardRenderer::RefreshMaterialBuffer(bool force)
{
    if (!UsesLinkedProgram() || m_shaderProgram->materialBufferSize == 0)
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
        const float threshold =
            m_material && !m_material->IsDeleted() ? m_material->GetRenderState().alphaClipThreshold : 0.5f;
        (void)WriteValue(bytes, *m_shaderProgram->alphaClipThresholdOffset, threshold);
    }
    if (!m_device->WriteBuffer(m_materialBuffer, 0, bytes.data(), bytes.size()))
        return false;
    m_materialVersion = version;
    m_materialVersionInitialized = true;
    return true;
}

rhi::BindGroupHandle ParticleGpuBillboardRenderer::CreateBindGroup(const std::vector<TextureBindingState> &textures,
                                                                   rhi::BufferHandle renderIndices) const
{
    if (!m_device || !m_layout.IsValid())
        return {};
    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    groupDesc.buffers[groupDesc.bufferCount++] = {0, rhi::BindingType::StorageBuffer, m_instances, 0, 0};
    if (UsesLinkedProgram()) {
        if (!renderIndices.IsValid())
            return {};
        groupDesc.buffers[groupDesc.bufferCount++] = {1, rhi::BindingType::StorageBuffer, renderIndices, 0, 0};
    }
    if (m_materialBuffer.IsValid()) {
        groupDesc.buffers[groupDesc.bufferCount++] = {14, rhi::BindingType::UniformBuffer, m_materialBuffer, 0,
                                                      m_shaderProgram ? m_shaderProgram->materialBufferSize : 0};
    }
    for (const auto &binding : textures) {
        if (!binding.texture.IsValid() || !binding.sampler.IsValid() ||
            groupDesc.textureCount >= rhi::BindGroupDesc::MaxTextureBindings) {
            return {};
        }
        groupDesc.textures[groupDesc.textureCount++] = {binding.binding, rhi::BindingType::CombinedTextureSampler,
                                                        binding.texture, binding.sampler, false};
    }
    return m_device->CreateBindGroup(groupDesc);
}

bool ParticleGpuBillboardRenderer::RebuildBindGroup()
{
    const auto group = CreateBindGroup(m_textures, m_renderIndices);
    if (!group.IsValid())
        return false;
    RetireBindGroup(m_group);
    RetireViewBindGroups();
    m_group = group;
    return true;
}

void ParticleGpuBillboardRenderer::RetireViewBindGroups()
{
    for (const auto &entry : m_viewGroups)
        RetireBindGroup(entry.group);
    m_viewGroups.clear();
}

rhi::BindGroupHandle ParticleGpuBillboardRenderer::ResolveBindGroup(rhi::BufferHandle renderIndices)
{
    if (!UsesLinkedProgram() || !renderIndices.IsValid() || renderIndices == m_renderIndices)
        return m_group;
    const auto existing = std::find_if(m_viewGroups.begin(), m_viewGroups.end(),
                                       [&](const auto &entry) { return entry.renderIndices == renderIndices; });
    if (existing != m_viewGroups.end())
        return existing->group;
    const auto group = CreateBindGroup(m_textures, renderIndices);
    if (group.IsValid())
        m_viewGroups.push_back({renderIndices, group});
    return group;
}

bool ParticleGpuBillboardRenderer::RefreshTextureBindings(bool force)
{
    if (!m_usesTexture)
        return true;
    if (!m_textureResolver)
        return false;

    auto candidate = m_textures;
    std::vector<size_t> changed;
    changed.reserve(candidate.size());
    for (size_t index = 0; index < candidate.size(); ++index) {
        auto &binding = candidate[index];
        const std::string textureGuid = ResolveMaterialTextureGuid(binding);
        const uint64_t textureVersion = m_textureVersionResolver ? m_textureVersionResolver(textureGuid) : 0;
        if (!force && !binding.pending && textureGuid == binding.requestedGuid &&
            textureVersion == binding.requestedVersion) {
            continue;
        }

        auto lease = m_textureResolver(textureGuid, binding.name);
        const bool pending = lease.status == GpuBillboardTextureStatus::Pending;
        bool usingFallback = false;
        if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() || !lease.sampler.IsValid() ||
            !lease.keepAlive) {
            if (lease.texture.IsValid())
                m_device->Release(lease.texture);
            if (lease.sampler.IsValid())
                m_device->Release(lease.sampler);
            if (pending && binding.fallback && textureGuid == binding.requestedGuid &&
                textureVersion == binding.requestedVersion && binding.texture.IsValid() && binding.sampler.IsValid()) {
                binding.pending = true;
                continue;
            }
            const std::string fallbackGuid = !binding.defaultGuid.empty() && binding.defaultGuid != textureGuid
                                                 ? binding.defaultGuid
                                                 : std::string{};
            lease = m_textureResolver(fallbackGuid, binding.name);
            usingFallback = true;
        }
        if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() || !lease.sampler.IsValid() ||
            !lease.keepAlive) {
            if (lease.texture.IsValid())
                m_device->Release(lease.texture);
            if (lease.sampler.IsValid())
                m_device->Release(lease.sampler);
            if (binding.texture.IsValid() && binding.sampler.IsValid()) {
                binding.pending = pending;
                continue;
            }
            for (const size_t changedIndex : changed) {
                m_device->Release(candidate[changedIndex].texture);
                m_device->Release(candidate[changedIndex].sampler);
            }
            return false;
        }

        binding.texture = lease.texture;
        binding.sampler = lease.sampler;
        binding.keepAlive = std::move(lease.keepAlive);
        binding.releaseHandles = lease.releaseHandles;
        binding.requestedGuid = textureGuid;
        binding.requestedVersion = textureVersion;
        binding.pending = pending;
        binding.fallback = usingFallback;
        changed.push_back(index);
    }

    if (changed.empty())
        return m_group.IsValid();
    const auto group = CreateBindGroup(candidate, m_renderIndices);
    if (!group.IsValid()) {
        for (const size_t index : changed) {
            m_device->Release(candidate[index].texture);
            m_device->Release(candidate[index].sampler);
        }
        return m_group.IsValid();
    }

    RetireBindGroup(m_group);
    RetireViewBindGroups();
    for (const size_t index : changed) {
        auto &previous = m_textures[index];
        RetireTexture(previous.texture, previous.sampler, std::move(previous.keepAlive), previous.releaseHandles);
    }
    m_group = group;
    m_textures = std::move(candidate);
    return true;
}

bool ParticleGpuBillboardRenderer::RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                              rhi::RenderTargetLayoutHandle renderTargetLayout,
                                              const MaterialPassPipelineDescriptor &pass,
                                              rhi::BufferHandle indirectArguments,
                                              const GpuBillboardViewConstants &view, rhi::BufferHandle renderIndices)
{
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid() || !RefreshMaterialBuffer(false) ||
        !RefreshTextureBindings(false))
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass);
    if (!pipeline.IsValid())
        return false;
    const auto group = ResolveBindGroup(renderIndices);
    if (!group.IsValid())
        return false;
    auto constants = view;
    constants.materialTint = UsesLinkedProgram() ? std::array<float, 4>{1.0f, 1.0f, 1.0f, 1.0f} : ResolveMaterialTint();
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

bool ParticleGpuBillboardRenderer::RecordPickingDraw(
    const rhi::GraphicsCommandEncoder &encoder,
    rhi::RenderTargetLayoutHandle renderTargetLayout,
    const MaterialPassPipelineDescriptor &pass,
    rhi::BufferHandle indirectArguments,
    const GpuBillboardViewConstants &view,
    uint64_t ownerObjectId,
    rhi::BufferHandle renderIndices)
{
    if (!IsValid() || !m_pickingVertexShader.IsValid() || !m_pickingFragmentShader.IsValid() ||
        ownerObjectId == 0 || !encoder.IsValid() || !indirectArguments.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass);
    const auto group = ResolveBindGroup(renderIndices);
    if (!pipeline.IsValid() || !group.IsValid())
        return false;
    auto constants = view;
    const std::array<uint32_t, 4> objectId = {
        static_cast<uint32_t>(ownerObjectId),
        static_cast<uint32_t>(ownerObjectId >> 32u),
        0u,
        0u,
    };
    std::memcpy(constants.materialTint.data(), objectId.data(), sizeof(objectId));
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment,
                          sizeof(constants), &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

rhi::GraphicsPipelineHandle
ParticleGpuBillboardRenderer::GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                  const MaterialPassPipelineDescriptor &pass)
{
    if (!renderTargetLayout.IsValid() || !pass.IsValid() ||
        (pass.target != ShaderCompileTarget::Forward && pass.target != ShaderCompileTarget::Picking))
        return {};
    const bool picking = pass.target == ShaderCompileTarget::Picking;
    const auto materialState = picking
                                   ? GpuBillboardMaterialState{3000, false, true, true}
                                   : ResolveMaterialState();
    const uint8_t pipelineStateSignature = PipelineStateSignature(materialState);
    for (const auto &entry : m_pipelines) {
        if (entry.renderTargetLayout == renderTargetLayout && entry.pass == pass &&
            entry.materialStateSignature == pipelineStateSignature)
            return entry.pipeline;
    }

    rhi::GraphicsPipelineDesc desc;
    desc.vertexShader = picking ? m_pickingVertexShader : m_vertexShader;
    desc.fragmentShader = picking ? m_pickingFragmentShader : m_fragmentShader;
    desc.renderTargetLayout = renderTargetLayout;
    desc.raster.cullMode = rhi::CullMode::None;
    desc.depth.testEnabled = materialState.depthTestEnabled && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.depth.writeEnabled = materialState.depthWriteEnabled && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.samples = pass.samples;
    for (size_t index = 0; index < pass.colorFormats.size(); ++index) {
        desc.colorTargets[index].format = pass.colorFormats[index];
        desc.colorTargets[index].blendEnabled = materialState.blendEnabled;
    }
    desc.colorTargetCount = static_cast<uint32_t>(pass.colorFormats.size());
    desc.bindingLayouts[0] = m_layout;
    desc.bindingLayoutCount = 1;
    desc.pushConstantStages = rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment;
    desc.pushConstantBytes = sizeof(GpuBillboardViewConstants);
    const auto pipeline = m_device->CreateGraphicsPipeline(desc);
    if (pipeline.IsValid())
        m_pipelines.push_back({renderTargetLayout, pass, pipelineStateSignature, pipeline});
    return pipeline;
}

} // namespace infernux::particle
