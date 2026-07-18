#include "ParticleGpuBillboardRenderer.h"

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
    m_instances = desc.instances;
    m_vertexShader = device.CreateShaderModule({desc.vertexShader.words, desc.vertexShader.wordCount});
    m_fragmentShader = device.CreateShaderModule({desc.fragmentShader.words, desc.fragmentShader.wordCount});
    if (!m_vertexShader.IsValid() || !m_fragmentShader.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    layoutDesc.entries[0] = {0, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    layoutDesc.entryCount = 1;
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    groupDesc.buffers[0] = {0, rhi::BindingType::StorageBuffer, m_instances, 0, 0};
    groupDesc.bufferCount = 1;
    m_group = device.CreateBindGroup(groupDesc);
    if (!m_group.IsValid()) {
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
        m_device->Release(m_layout);
        m_device->Release(m_fragmentShader);
        m_device->Release(m_vertexShader);
    }
    m_device = nullptr;
    m_material.reset();
    m_fallbackMaterial = {};
    m_instances = {};
    m_vertexShader = {};
    m_fragmentShader = {};
    m_layout = {};
    m_group = {};
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

bool ParticleGpuBillboardRenderer::RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                              rhi::RenderTargetLayoutHandle renderTargetLayout,
                                              const MaterialPassPipelineDescriptor &pass,
                                              rhi::BufferHandle indirectArguments,
                                              const GpuBillboardViewConstants &view)
{
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass);
    if (!pipeline.IsValid())
        return false;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, m_group);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex, sizeof(view), &view);
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
    desc.pushConstantStages = rhi::ShaderStage::Vertex;
    desc.pushConstantBytes = sizeof(GpuBillboardViewConstants);
    const auto pipeline = m_device->CreateGraphicsPipeline(desc);
    if (pipeline.IsValid())
        m_pipelines.push_back({renderTargetLayout, pass, pipelineStateSignature, pipeline});
    return pipeline;
}

} // namespace infernux::particle
