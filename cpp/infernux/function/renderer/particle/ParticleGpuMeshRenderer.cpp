#include "ParticleGpuMeshRenderer.h"

#include <core/types/ColorSpace.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <algorithm>
#include <cstring>

namespace infernux::particle
{

namespace
{
uint8_t PipelineStateSignature(const GpuBillboardMaterialState &state) noexcept
{
    return static_cast<uint8_t>((state.blendEnabled ? 1u : 0u) | (state.depthTestEnabled ? 2u : 0u) |
                                (state.depthWriteEnabled ? 4u : 0u) | (state.premultipliedAlpha ? 8u : 0u));
}

} // namespace

ParticleGpuMeshRenderer::~ParticleGpuMeshRenderer()
{
    Destroy();
}

bool ParticleGpuMeshRenderer::Create(rhi::Device &device, const GpuMeshRendererDesc &desc)
{
    Destroy();
    if (!desc.instances.IsValid() || !desc.renderIndices.IsValid() || !desc.mesh || !desc.vertexShader.words ||
        desc.vertexShader.wordCount == 0 || !desc.fragmentShader.words || desc.fragmentShader.wordCount == 0 ||
        (desc.semantics.receiveSceneLighting &&
         (!desc.forwardPlusFragmentShader.words || desc.forwardPlusFragmentShader.wordCount == 0)) ||
        !desc.pickingFragmentShader.words || desc.pickingFragmentShader.wordCount == 0 ||
        !desc.motionVertexShader.words || desc.motionVertexShader.wordCount == 0 || !desc.motionFragmentShader.words ||
        desc.motionFragmentShader.wordCount == 0 || !desc.meshVertices.IsValid() || !desc.meshIndices.IsValid() ||
        desc.indexCount == 0 || !desc.meshBufferKeepAlive) {
        return false;
    }
    const auto &sourceVertices = desc.mesh->GetVertices();
    const auto &sourceIndices = desc.mesh->GetIndices();
    if (sourceVertices.empty() || sourceIndices.empty() || sourceIndices.size() != desc.indexCount ||
        std::any_of(sourceIndices.begin(), sourceIndices.end(),
                    [&](uint32_t index) { return index >= sourceVertices.size(); })) {
        return false;
    }

    m_device = &device;
    m_mesh = desc.mesh;
    m_meshBufferKeepAlive = desc.meshBufferKeepAlive;
    m_material = desc.material;
    m_fallbackMaterial = desc.fallbackMaterial;
    m_semantics = desc.semantics;
    if (!m_semantics.IsValid()) {
        Destroy();
        return false;
    }
    m_indexCount = desc.indexCount;
    m_instances = desc.instances;
    m_renderIndices = desc.renderIndices;
    m_meshVertices = desc.meshVertices;
    m_meshIndices = desc.meshIndices;
    m_staticVertexStorageBuffers = {
        GpuParticleStaticBuffer{m_meshVertices, sourceVertices.size() * 5u * sizeof(std::array<float, 4>)},
        GpuParticleStaticBuffer{m_meshIndices, sourceIndices.size() * sizeof(uint32_t)},
    };

    m_vertexShader = device.CreateShaderModule({desc.vertexShader.words, desc.vertexShader.wordCount});
    m_fragmentShader = device.CreateShaderModule({desc.fragmentShader.words, desc.fragmentShader.wordCount});
    if (m_semantics.receiveSceneLighting) {
        m_forwardPlusFragmentShader =
            device.CreateShaderModule({desc.forwardPlusFragmentShader.words, desc.forwardPlusFragmentShader.wordCount});
    }
    m_pickingFragmentShader =
        device.CreateShaderModule({desc.pickingFragmentShader.words, desc.pickingFragmentShader.wordCount});
    m_motionVertexShader =
        device.CreateShaderModule({desc.motionVertexShader.words, desc.motionVertexShader.wordCount});
    m_motionFragmentShader =
        device.CreateShaderModule({desc.motionFragmentShader.words, desc.motionFragmentShader.wordCount});
    if (!m_meshVertices.IsValid() || !m_meshIndices.IsValid() || !m_vertexShader.IsValid() ||
        !m_fragmentShader.IsValid() || !m_pickingFragmentShader.IsValid() || !m_motionVertexShader.IsValid() ||
        !m_motionFragmentShader.IsValid() ||
        (m_semantics.receiveSceneLighting && !m_forwardPlusFragmentShader.IsValid())) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layout;
    for (uint32_t binding = 0; binding < 4; ++binding)
        layout.entries[layout.entryCount++] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    m_layout = device.CreateBindingLayout(layout);
    m_group = CreateBindGroup(m_renderIndices);
    if (!m_layout.IsValid() || !m_group.IsValid()) {
        Destroy();
        return false;
    }
    return true;
}

void ParticleGpuMeshRenderer::Destroy() noexcept
{
    if (m_device) {
        for (const auto &entry : m_pipelines)
            m_device->Release(entry.pipeline);
        for (const auto &entry : m_viewGroups)
            m_device->Release(entry.group);
        m_device->Release(m_group);
        m_device->Release(m_layout);
        m_device->Release(m_motionFragmentShader);
        m_device->Release(m_motionVertexShader);
        m_device->Release(m_pickingFragmentShader);
        m_device->Release(m_forwardPlusFragmentShader);
        m_device->Release(m_fragmentShader);
        m_device->Release(m_vertexShader);
    }
    m_device = nullptr;
    m_mesh.reset();
    m_meshBufferKeepAlive.reset();
    m_material.reset();
    m_fallbackMaterial = {};
    m_semantics = {};
    m_indexCount = 0;
    m_instances = {};
    m_renderIndices = {};
    m_meshVertices = {};
    m_meshIndices = {};
    m_staticVertexStorageBuffers.clear();
    m_vertexShader = {};
    m_fragmentShader = {};
    m_forwardPlusFragmentShader = {};
    m_pickingFragmentShader = {};
    m_motionVertexShader = {};
    m_motionFragmentShader = {};
    m_layout = {};
    m_group = {};
    m_viewGroups.clear();
    m_pipelines.clear();
}

bool ParticleGpuMeshRenderer::IsValid() const noexcept
{
    return m_device && m_mesh && m_indexCount > 0 && m_instances.IsValid() && m_renderIndices.IsValid() &&
           m_meshVertices.IsValid() && m_meshIndices.IsValid() && m_vertexShader.IsValid() &&
           m_fragmentShader.IsValid() && m_pickingFragmentShader.IsValid() && m_motionVertexShader.IsValid() &&
           m_motionFragmentShader.IsValid() && m_layout.IsValid() && m_group.IsValid();
}

int32_t ParticleGpuMeshRenderer::RenderQueue() const noexcept
{
    return ResolveMaterialState().renderQueue;
}

GpuBillboardMaterialState ParticleGpuMeshRenderer::ResolveMaterialState() const noexcept
{
    if (!m_material || m_material->IsDeleted())
        return m_fallbackMaterial;
    const auto &state = m_material->GetRenderState();
    return {state.renderQueue, state.blendEnable, state.depthTestEnable, state.depthWriteEnable,
            state.srcColorBlendFactor == VK_BLEND_FACTOR_ONE &&
                state.dstColorBlendFactor == VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA};
}

std::array<float, 4> ParticleGpuMeshRenderer::ResolveMaterialTint() const noexcept
{
    if (!m_material || m_material->IsDeleted())
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const auto *property = m_material->GetProperty("baseColor");
    if (!property || (property->type != MaterialPropertyType::Color && property->type != MaterialPropertyType::Float4))
        return {1.0f, 1.0f, 1.0f, 1.0f};
    const auto *value = std::get_if<glm::vec4>(&property->value);
    if (!value)
        return {1.0f, 1.0f, 1.0f, 1.0f};
    // Authored Color properties are sRGB; shading runs in linear space.
    const glm::vec4 tint = property->type == MaterialPropertyType::Color ? inx::color::SrgbToLinear(*value) : *value;
    return {tint.x, tint.y, tint.z, tint.w};
}

rhi::BindGroupHandle ParticleGpuMeshRenderer::CreateBindGroup(rhi::BufferHandle renderIndices) const
{
    if (!m_device || !m_layout.IsValid() || !renderIndices.IsValid())
        return {};
    rhi::BindGroupDesc group;
    group.layout = m_layout;
    const std::array<rhi::BufferHandle, 4> buffers = {m_instances, renderIndices, m_meshVertices, m_meshIndices};
    for (uint32_t binding = 0; binding < buffers.size(); ++binding)
        group.buffers[group.bufferCount++] = {binding, rhi::BindingType::StorageBuffer, buffers[binding], 0, 0};
    return m_device->CreateBindGroup(group);
}

rhi::BindGroupHandle ParticleGpuMeshRenderer::ResolveBindGroup(rhi::BufferHandle renderIndices)
{
    if (!renderIndices.IsValid() || renderIndices == m_renderIndices)
        return m_group;
    const auto found = std::find_if(m_viewGroups.begin(), m_viewGroups.end(),
                                    [&](const auto &entry) { return entry.renderIndices == renderIndices; });
    if (found != m_viewGroups.end())
        return found->group;
    const auto group = CreateBindGroup(renderIndices);
    if (group.IsValid())
        m_viewGroups.push_back({renderIndices, group});
    return group;
}

bool ParticleGpuMeshRenderer::RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                         rhi::RenderTargetLayoutHandle renderTargetLayout,
                                         const MaterialPassPipelineDescriptor &pass,
                                         rhi::BufferHandle indirectArguments, const GpuParticleViewConstants &view,
                                         rhi::BufferHandle renderIndices, rhi::TextureViewHandle sceneDepth,
                                         bool sceneDepthIsDepth, const GpuParticleForwardPlusBindings &forwardPlus)
{
    (void)sceneDepth;
    (void)sceneDepthIsDepth;
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid())
        return false;
    const bool usesForwardPlusLighting =
        pass.target == ShaderCompileTarget::ForwardPlus && m_semantics.receiveSceneLighting;
    if (usesForwardPlusLighting && !forwardPlus.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(
        renderTargetLayout, pass, usesForwardPlusLighting ? forwardPlus.layout : rhi::BindingLayoutHandle{});
    const auto group = ResolveBindGroup(renderIndices);
    if (!pipeline.IsValid() || !group.IsValid())
        return false;
    auto constants = view;
    constants.materialTint = ResolveMaterialTint();
    constants.lightingControl[0] = usesForwardPlusLighting ? 1.0f : 0.0f;
    constants.renderingControl[0] = m_semantics.receiveShadows ? 1.0f : 0.0f;
    constants.renderingControl[1] = pass.target == ShaderCompileTarget::Shadow ? 1.0f : 0.0f;
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    if (usesForwardPlusLighting)
        encoder.BindGroup(pipeline, 1, forwardPlus.group);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

bool ParticleGpuMeshRenderer::RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                                rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                const MaterialPassPipelineDescriptor &pass,
                                                rhi::BufferHandle indirectArguments,
                                                const GpuParticleViewConstants &view, uint64_t ownerObjectId,
                                                rhi::BufferHandle renderIndices)
{
    if (!IsValid() || ownerObjectId == 0 || !encoder.IsValid() || !indirectArguments.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass);
    const auto group = ResolveBindGroup(renderIndices);
    if (!pipeline.IsValid() || !group.IsValid())
        return false;
    auto constants = view;
    const std::array<uint32_t, 4> objectId = {static_cast<uint32_t>(ownerObjectId),
                                              static_cast<uint32_t>(ownerObjectId >> 32u), 0u, 0u};
    std::memcpy(constants.materialTint.data(), objectId.data(), sizeof(objectId));
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

rhi::GraphicsPipelineHandle
ParticleGpuMeshRenderer::GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                             const MaterialPassPipelineDescriptor &pass,
                                             rhi::BindingLayoutHandle forwardPlusLayout)
{
    if (!renderTargetLayout.IsValid() || !pass.IsValid() ||
        (pass.target != ShaderCompileTarget::Forward && pass.target != ShaderCompileTarget::ForwardPlus &&
         pass.target != ShaderCompileTarget::Shadow && pass.target != ShaderCompileTarget::Picking &&
         pass.target != ShaderCompileTarget::Motion)) {
        return {};
    }
    const bool picking = pass.target == ShaderCompileTarget::Picking;
    const bool shadow = pass.target == ShaderCompileTarget::Shadow;
    const bool motion = pass.target == ShaderCompileTarget::Motion;
    const bool usesForwardPlusLighting =
        pass.target == ShaderCompileTarget::ForwardPlus && m_semantics.receiveSceneLighting;
    if (usesForwardPlusLighting && !forwardPlusLayout.IsValid())
        return {};
    const auto state = (picking || shadow) ? GpuBillboardMaterialState{3000, false, true, true}
                       : motion            ? GpuBillboardMaterialState{3000, false, true, false}
                                           : ResolveMaterialState();
    const uint8_t signature = PipelineStateSignature(state);
    const auto found = std::find_if(m_pipelines.begin(), m_pipelines.end(), [&](const auto &entry) {
        return entry.renderTargetLayout == renderTargetLayout && entry.pass == pass &&
               entry.forwardPlusLayout == forwardPlusLayout && entry.materialStateSignature == signature;
    });
    if (found != m_pipelines.end())
        return found->pipeline;

    rhi::GraphicsPipelineDesc desc;
    desc.vertexShader = motion ? m_motionVertexShader : m_vertexShader;
    desc.fragmentShader = motion    ? m_motionFragmentShader
                          : picking ? m_pickingFragmentShader
                                    : (usesForwardPlusLighting ? m_forwardPlusFragmentShader : m_fragmentShader);
    desc.renderTargetLayout = renderTargetLayout;
    desc.raster.cullMode = rhi::CullMode::Back;
    desc.raster.frontFace = rhi::FrontFace::Clockwise;
    desc.depth.testEnabled = state.depthTestEnabled && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.depth.writeEnabled = state.depthWriteEnabled && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.samples = pass.samples;
    for (size_t index = 0; index < pass.colorFormats.size(); ++index) {
        desc.colorTargets[index].format = pass.colorFormats[index];
        desc.colorTargets[index].blendEnabled = state.blendEnabled;
        desc.colorTargets[index].premultipliedAlpha = state.premultipliedAlpha;
    }
    desc.colorTargetCount = static_cast<uint32_t>(pass.colorFormats.size());
    desc.bindingLayouts[0] = m_layout;
    desc.bindingLayoutCount = 1;
    if (usesForwardPlusLighting) {
        desc.bindingLayouts[1] = forwardPlusLayout;
        desc.bindingLayoutCount = 2;
    }
    desc.pushConstantStages = rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment;
    desc.pushConstantBytes = sizeof(GpuParticleViewConstants);
    const auto pipeline = m_device->CreateGraphicsPipeline(desc);
    if (pipeline.IsValid())
        m_pipelines.push_back({renderTargetLayout, pass, forwardPlusLayout, signature, pipeline});
    return pipeline;
}

} // namespace infernux::particle
