#include "ParticleGpuBillboardRenderer.h"

#include <function/renderer/FrameDeletionQueue.h>
#include <function/resources/InxMaterial/InxMaterial.h>

namespace infernux::particle
{

namespace
{

uint8_t PipelineStateSignature(const GpuBillboardMaterialState &state) noexcept
{
    return static_cast<uint8_t>((state.blendEnabled ? 1u : 0u) | (state.depthTestEnabled ? 2u : 0u) |
                                (state.depthWriteEnabled ? 4u : 0u));
}

} // namespace

ParticleGpuBillboardRenderer::~ParticleGpuBillboardRenderer()
{
    Destroy();
}

bool ParticleGpuBillboardRenderer::Create(rhi::Device &device, const GpuBillboardRendererDesc &desc)
{
    Destroy();
    if (!desc.vertexShader.words || desc.vertexShader.wordCount == 0 || !desc.fragmentShader.words ||
        desc.fragmentShader.wordCount == 0 || !desc.instances.IsValid())
        return false;

    m_device = &device;
    m_material = desc.material;
    m_fallbackMaterial = desc.fallbackMaterial;
    m_textureResolver = desc.textureResolver;
    m_textureVersionResolver = desc.textureVersionResolver;
    m_deletionQueue = desc.deletionQueue;
    m_usesTexture = static_cast<bool>(m_textureResolver);
    m_instances = desc.instances;
    m_vertexShader = device.CreateShaderModule({desc.vertexShader.words, desc.vertexShader.wordCount});
    m_fragmentShader = device.CreateShaderModule({desc.fragmentShader.words, desc.fragmentShader.wordCount});
    if (!m_vertexShader.IsValid() || !m_fragmentShader.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    layoutDesc.entries[0] = {0, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    if (m_usesTexture)
        layoutDesc.entries[1] = {1, rhi::BindingType::CombinedTextureSampler, rhi::ShaderStage::Fragment, 1};
    layoutDesc.entryCount = m_usesTexture ? 2 : 1;
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    bool bindingReady = false;
    if (m_usesTexture) {
        bindingReady = RefreshTextureBinding(true);
    } else {
        rhi::BindGroupDesc groupDesc;
        groupDesc.layout = m_layout;
        groupDesc.buffers[0] = {0, rhi::BindingType::StorageBuffer, m_instances, 0, 0};
        groupDesc.bufferCount = 1;
        m_group = device.CreateBindGroup(groupDesc);
        bindingReady = m_group.IsValid();
    }
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
        m_device->Release(m_texture);
        m_device->Release(m_sampler);
        m_device->Release(m_layout);
        m_device->Release(m_fragmentShader);
        m_device->Release(m_vertexShader);
    }
    m_device = nullptr;
    m_material.reset();
    m_fallbackMaterial = {};
    m_textureResolver = {};
    m_textureVersionResolver = {};
    m_deletionQueue = nullptr;
    m_instances = {};
    m_vertexShader = {};
    m_fragmentShader = {};
    m_layout = {};
    m_group = {};
    m_texture = {};
    m_sampler = {};
    m_textureKeepAlive.reset();
    m_textureGuid.clear();
    m_textureVersion = 0;
    m_texturePending = false;
    m_textureFallback = false;
    m_usesTexture = false;
    m_pipelines.clear();
}

bool ParticleGpuBillboardRenderer::IsValid() const noexcept
{
    return m_device && m_instances.IsValid() && m_vertexShader.IsValid() && m_fragmentShader.IsValid() &&
           m_layout.IsValid() && m_group.IsValid();
}

int32_t ParticleGpuBillboardRenderer::RenderQueue() const noexcept
{
    return ResolveMaterialState().renderQueue;
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

std::string ParticleGpuBillboardRenderer::ResolveMaterialTextureGuid() const
{
    if (!m_material || m_material->IsDeleted())
        return {};
    const auto *property = m_material->GetProperty("texSampler");
    if (!property || property->type != MaterialPropertyType::Texture2D)
        return {};
    const auto *value = std::get_if<std::string>(&property->value);
    return value ? *value : std::string{};
}

void ParticleGpuBillboardRenderer::RetireTextureBinding(rhi::BindGroupHandle group, rhi::TextureViewHandle texture,
                                                        rhi::SamplerHandle sampler, std::shared_ptr<void> keepAlive)
{
    if (!m_device)
        return;
    if (!group.IsValid() && !texture.IsValid() && !sampler.IsValid() && !keepAlive)
        return;
    auto release = [device = m_device, group, texture, sampler, keepAlive = std::move(keepAlive)]() mutable {
        device->Release(group);
        device->Release(texture);
        device->Release(sampler);
        keepAlive.reset();
    };
    if (m_deletionQueue)
        m_deletionQueue->Push(std::move(release));
    else
        release();
}

bool ParticleGpuBillboardRenderer::RefreshTextureBinding(bool force)
{
    if (!m_usesTexture)
        return true;
    const std::string textureGuid = ResolveMaterialTextureGuid();
    const uint64_t textureVersion = m_textureVersionResolver ? m_textureVersionResolver(textureGuid) : 0;
    if (!force && !m_texturePending && textureGuid == m_textureGuid && textureVersion == m_textureVersion)
        return true;

    auto lease = m_textureResolver(textureGuid, "texSampler");
    const bool pending = lease.status == GpuBillboardTextureStatus::Pending;
    bool usingFallback = false;
    if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() || !lease.sampler.IsValid() ||
        !lease.keepAlive) {
        if (lease.texture.IsValid())
            m_device->Release(lease.texture);
        if (lease.sampler.IsValid())
            m_device->Release(lease.sampler);
        if (pending && m_textureFallback && textureGuid == m_textureGuid && textureVersion == m_textureVersion) {
            m_texturePending = true;
            return m_group.IsValid();
        }
        lease = m_textureResolver({}, "texSampler");
        usingFallback = true;
    }
    if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() || !lease.sampler.IsValid() ||
        !lease.keepAlive) {
        if (lease.texture.IsValid())
            m_device->Release(lease.texture);
        if (lease.sampler.IsValid())
            m_device->Release(lease.sampler);
        return m_group.IsValid();
    }

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    groupDesc.buffers[0] = {0, rhi::BindingType::StorageBuffer, m_instances, 0, 0};
    groupDesc.bufferCount = 1;
    groupDesc.textures[0] = {1, rhi::BindingType::CombinedTextureSampler, lease.texture, lease.sampler, false};
    groupDesc.textureCount = 1;
    const auto group = m_device->CreateBindGroup(groupDesc);
    if (!group.IsValid()) {
        m_device->Release(lease.texture);
        m_device->Release(lease.sampler);
        return m_group.IsValid();
    }

    RetireTextureBinding(m_group, m_texture, m_sampler, std::move(m_textureKeepAlive));
    m_group = group;
    m_texture = lease.texture;
    m_sampler = lease.sampler;
    m_textureKeepAlive = std::move(lease.keepAlive);
    m_textureGuid = textureGuid;
    m_textureVersion = textureVersion;
    m_texturePending = pending;
    m_textureFallback = usingFallback;
    return true;
}

bool ParticleGpuBillboardRenderer::RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                              rhi::RenderTargetLayoutHandle renderTargetLayout,
                                              const MaterialPassPipelineDescriptor &pass,
                                              rhi::BufferHandle indirectArguments,
                                              const GpuBillboardViewConstants &view)
{
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid() || !RefreshTextureBinding(false))
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass);
    if (!pipeline.IsValid())
        return false;
    auto constants = view;
    constants.materialTint = ResolveMaterialTint();
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, m_group);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

rhi::GraphicsPipelineHandle
ParticleGpuBillboardRenderer::GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                  const MaterialPassPipelineDescriptor &pass)
{
    if (!renderTargetLayout.IsValid() || !pass.IsValid() || pass.target != ShaderCompileTarget::Forward)
        return {};
    const auto materialState = ResolveMaterialState();
    const uint8_t pipelineStateSignature = PipelineStateSignature(materialState);
    for (const auto &entry : m_pipelines) {
        if (entry.renderTargetLayout == renderTargetLayout && entry.pass == pass &&
            entry.materialStateSignature == pipelineStateSignature)
            return entry.pipeline;
    }

    rhi::GraphicsPipelineDesc desc;
    desc.vertexShader = m_vertexShader;
    desc.fragmentShader = m_fragmentShader;
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
