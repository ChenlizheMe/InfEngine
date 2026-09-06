#include "ParticleGpuBillboardRenderer.h"

#include <function/renderer/rhi/GpuRetirementQueue.h>

#include <algorithm>
#include <array>
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
    if (!desc.instances.IsValid() || !desc.renderIndices.IsValid() || !desc.vertexShader.words ||
        desc.vertexShader.wordCount == 0 || !desc.motionVertexShader.words || desc.motionVertexShader.wordCount == 0 ||
        desc.flipbookColumns == 0 || desc.flipbookRows == 0 ||
        static_cast<uint64_t>(desc.flipbookColumns) * desc.flipbookRows > 65536u || !desc.shaderProgram ||
        !desc.shaderProgram->IsValid() || desc.shaderProgram->domain != ShaderProgramDomain::ParticleSprite ||
        !desc.semantics.IsValid() ||
        (desc.semantics.softParticles && !desc.shaderProgram->usesParticleSceneDepthBinding))
        return false;

    const auto *linkedVariant = desc.shaderProgram->FindVariant(ShaderCompileTarget::Forward);
    const auto *linkedForwardPlusVariant = desc.shaderProgram->FindVariant(ShaderCompileTarget::ForwardPlus);
    const auto *linkedMotionVariant = desc.shaderProgram->FindVariant(ShaderCompileTarget::Motion);
    if (!linkedVariant || !linkedMotionVariant || (desc.semantics.receiveSceneLighting && !linkedForwardPlusVariant))
        return false;

    const auto linkedFragmentWords = CopySpirvWords(linkedVariant->fragmentSpirv);
    const auto linkedMotionFragmentWords = CopySpirvWords(linkedMotionVariant->fragmentSpirv);
    if (linkedFragmentWords.empty() || linkedMotionFragmentWords.empty())
        return false;

    std::vector<uint32_t> linkedForwardPlusFragmentWords;
    if (linkedForwardPlusVariant) {
        linkedForwardPlusFragmentWords = CopySpirvWords(linkedForwardPlusVariant->fragmentSpirv);
        if (linkedForwardPlusFragmentWords.empty())
            return false;
    }

    m_device = &device;
    m_shaderProgram = desc.shaderProgram;
    m_deletionQueue = desc.deletionQueue;
    m_semantics = desc.semantics;
    m_flipbookColumns = desc.flipbookColumns;
    m_flipbookRows = desc.flipbookRows;
    m_instances = desc.instances;
    m_renderIndices = desc.renderIndices;
    m_vertexShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(desc.vertexShader.words, desc.vertexShader.wordCount));
    m_fragmentShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(linkedFragmentWords.data(), linkedFragmentWords.size()));
    m_motionVertexShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(desc.motionVertexShader.words, desc.motionVertexShader.wordCount));
    m_motionFragmentShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(linkedMotionFragmentWords.data(), linkedMotionFragmentWords.size()));
    if (linkedForwardPlusVariant) {
        m_forwardPlusFragmentShader = device.CreateShaderModule(rhi::ShaderModuleDesc::FromSpirV(
            linkedForwardPlusFragmentWords.data(), linkedForwardPlusFragmentWords.size()));
    }

    // Picking stays output-specific. It does not consume the linked surface ABI.
    if (desc.vertexShader.words && desc.vertexShader.wordCount && desc.pickingFragmentShader.words &&
        desc.pickingFragmentShader.wordCount) {
        m_pickingVertexShader = device.CreateShaderModule(
            rhi::ShaderModuleDesc::FromSpirV(desc.vertexShader.words, desc.vertexShader.wordCount));
        m_pickingFragmentShader = device.CreateShaderModule(
            rhi::ShaderModuleDesc::FromSpirV(desc.pickingFragmentShader.words, desc.pickingFragmentShader.wordCount));
    }
    if (!m_vertexShader.IsValid() || !m_fragmentShader.IsValid() || !m_motionVertexShader.IsValid() ||
        !m_motionFragmentShader.IsValid() ||
        (desc.semantics.receiveSceneLighting && !m_forwardPlusFragmentShader.IsValid())) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc geometryLayoutDesc;
    geometryLayoutDesc.entries[geometryLayoutDesc.entryCount++] = {0, rhi::BindingType::StorageBuffer,
                                                                   rhi::ShaderStage::Vertex, 1};
    geometryLayoutDesc.entries[geometryLayoutDesc.entryCount++] = {1, rhi::BindingType::StorageBuffer,
                                                                   rhi::ShaderStage::Vertex, 1};
    m_geometryLayout = device.CreateBindingLayout(geometryLayoutDesc);
    m_emptyLayout = device.CreateBindingLayout({});
    if (!m_geometryLayout.IsValid() || !m_emptyLayout.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindGroupDesc emptyGroupDesc;
    emptyGroupDesc.layout = m_emptyLayout;
    m_emptyGroup = device.CreateBindGroup(emptyGroupDesc);
    if (!m_emptyGroup.IsValid() || !m_surface.Create(device, desc.shaderProgram, desc.material, desc.fallbackMaterial,
                                                     desc.semantics, desc.textureResolver, desc.deletionQueue)) {
        Destroy();
        return false;
    }
    m_geometryGroup = CreateGeometryGroup(m_renderIndices);
    if (!m_geometryGroup.IsValid()) {
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
        RetireViewBindGroups();
        RetireBindGroup(m_geometryGroup);
        m_device->Release(m_emptyGroup);
        m_device->Release(m_geometryLayout);
        m_device->Release(m_emptyLayout);
        m_device->Release(m_forwardPlusFragmentShader);
        m_device->Release(m_fragmentShader);
        m_device->Release(m_vertexShader);
        m_device->Release(m_pickingFragmentShader);
        m_device->Release(m_pickingVertexShader);
        m_device->Release(m_motionFragmentShader);
        m_device->Release(m_motionVertexShader);
    }
    m_surface.Destroy();
    m_device = nullptr;
    m_shaderProgram.reset();
    m_deletionQueue = nullptr;
    m_semantics = {};
    m_flipbookColumns = 1;
    m_flipbookRows = 1;
    m_instances = {};
    m_renderIndices = {};
    m_vertexShader = {};
    m_fragmentShader = {};
    m_forwardPlusFragmentShader = {};
    m_pickingVertexShader = {};
    m_pickingFragmentShader = {};
    m_motionVertexShader = {};
    m_motionFragmentShader = {};
    m_geometryLayout = {};
    m_geometryGroup = {};
    m_emptyLayout = {};
    m_emptyGroup = {};
    m_viewGroups.clear();
    m_pipelines.clear();
}

bool ParticleGpuBillboardRenderer::IsValid() const noexcept
{
    return m_device && m_instances.IsValid() && m_renderIndices.IsValid() && m_vertexShader.IsValid() &&
           m_fragmentShader.IsValid() && m_motionVertexShader.IsValid() && m_motionFragmentShader.IsValid() &&
           m_geometryLayout.IsValid() && m_geometryGroup.IsValid() && m_emptyLayout.IsValid() &&
           m_emptyGroup.IsValid() && m_surface.IsValid();
}

int32_t ParticleGpuBillboardRenderer::RenderQueue() const noexcept
{
    return ResolveMaterialState().renderQueue;
}

GpuBillboardMaterialState ParticleGpuBillboardRenderer::ResolveMaterialState() const noexcept
{
    return m_surface.ResolveMaterialState();
}

void ParticleGpuBillboardRenderer::RetireBindGroup(rhi::BindGroupHandle group)
{
    if (!m_device || !group.IsValid())
        return;
    auto release = [device = m_device, group] { device->Release(group); };
    if (m_deletionQueue)
        m_deletionQueue->Retire(std::move(release));
    else
        release();
}

void ParticleGpuBillboardRenderer::RetireViewBindGroups()
{
    for (const auto &entry : m_viewGroups)
        RetireBindGroup(entry.group);
    m_viewGroups.clear();
}

rhi::BindGroupHandle ParticleGpuBillboardRenderer::CreateGeometryGroup(rhi::BufferHandle renderIndices) const
{
    if (!m_device || !m_geometryLayout.IsValid() || !m_instances.IsValid() || !renderIndices.IsValid())
        return {};
    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_geometryLayout;
    groupDesc.buffers[groupDesc.bufferCount++] = {0, rhi::BindingType::StorageBuffer, m_instances, 0, 0};
    groupDesc.buffers[groupDesc.bufferCount++] = {1, rhi::BindingType::StorageBuffer, renderIndices, 0, 0};
    return m_device->CreateBindGroup(groupDesc);
}

rhi::BindGroupHandle ParticleGpuBillboardRenderer::ResolveGeometryGroup(rhi::BufferHandle renderIndices)
{
    if (!renderIndices.IsValid() || renderIndices == m_renderIndices)
        return m_geometryGroup;
    const auto existing = std::find_if(m_viewGroups.begin(), m_viewGroups.end(),
                                       [&](const auto &entry) { return entry.renderIndices == renderIndices; });
    if (existing != m_viewGroups.end())
        return existing->group;
    const auto group = CreateGeometryGroup(renderIndices);
    if (group.IsValid())
        m_viewGroups.push_back({renderIndices, group});
    return group;
}

bool ParticleGpuBillboardRenderer::RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                              const MaterialPassPipelineDescriptor &pass,
                                              rhi::BufferHandle indirectArguments,
                                              const GpuBillboardViewConstants &view, rhi::BufferHandle renderIndices,
                                              rhi::TextureViewHandle sceneDepth, bool sceneDepthIsDepth,
                                              const GpuParticlePerViewBindings &perView)
{
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid() || !m_surface.RefreshMaterialBuffer(false) ||
        !m_surface.RefreshTextureBindings(false))
        return false;
    if (m_semantics.softParticles && !sceneDepth.IsValid())
        return false;
    const bool usesForwardPlusLighting =
        pass.target == ShaderCompileTarget::ForwardPlus && m_semantics.receiveSceneLighting;
    const bool usesPerViewBindings =
        pass.target == ShaderCompileTarget::Forward || pass.target == ShaderCompileTarget::ForwardPlus;
    if (usesPerViewBindings && !perView.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(pass, usesPerViewBindings ? perView.layout : rhi::BindingLayoutHandle{});
    const auto geometryGroup = ResolveGeometryGroup(renderIndices);
    const auto surfaceGroup = m_surface.ResolveBindGroup(sceneDepth, sceneDepthIsDepth);
    const bool usesBindlessTextures = m_surface.UsesBindlessTextures();
    const auto bindlessTable = m_surface.BindlessTableBinding();
    if (!pipeline.IsValid() || !geometryGroup.IsValid() || !surfaceGroup.IsValid() ||
        (usesBindlessTextures && !bindlessTable.IsValid()))
        return false;

    auto constants = view;
    constants.materialTint = {1.0f, 1.0f, 1.0f, 1.0f};
    constants.cameraRight[3] = m_semantics.softDistance;
    constants.cameraUp[3] = m_semantics.softParticles ? 1.0f : 0.0f;
    constants.lightingControl[0] = usesForwardPlusLighting ? 1.0f : 0.0f;
    constants.lightingControl[1] = m_semantics.sortMode != ParticleSortMode::None ? 1.0f : 0.0f;
    constants.lightingControl[2] = m_surface.ResolveMaterialFloat("softness", 0.18f);
    constants.renderingControl[0] = m_semantics.receiveShadows ? 1.0f : 0.0f;
    constants.renderingControl[1] = ResolveMaterialState().premultipliedAlpha ? 1.0f : 0.0f;
    constants.renderingControl[2] = static_cast<float>(m_flipbookColumns);
    constants.renderingControl[3] = static_cast<float>(m_flipbookRows);
    constants.alignmentReference[3] = static_cast<float>(m_semantics.spriteAlignment);
    if (m_semantics.spriteAlignment == ParticleSpriteAlignment::Axis)
        std::copy(m_semantics.alignmentAxis.begin(), m_semantics.alignmentAxis.end(),
                  constants.alignmentReference.begin());

    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, geometryGroup);
    encoder.BindGroup(pipeline, 1, usesPerViewBindings ? perView.group : m_emptyGroup);
    encoder.BindGroup(pipeline, 2, surfaceGroup);
    if (usesBindlessTextures) {
        encoder.BindGroup(pipeline, 3, bindlessTable.group);
        m_surface.MarkBindlessTexturesUsed();
    }
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

bool ParticleGpuBillboardRenderer::RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                                     const MaterialPassPipelineDescriptor &pass,
                                                     rhi::BufferHandle indirectArguments,
                                                     const GpuBillboardViewConstants &view, uint64_t ownerObjectId,
                                                     rhi::BufferHandle renderIndices)
{
    if (!IsValid() || !m_pickingVertexShader.IsValid() || !m_pickingFragmentShader.IsValid() || ownerObjectId == 0 ||
        !encoder.IsValid() || !indirectArguments.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(pass);
    const auto geometryGroup = ResolveGeometryGroup(renderIndices);
    const auto surfaceGroup = m_surface.ResolveBindGroup();
    if (!pipeline.IsValid() || !geometryGroup.IsValid() || !surfaceGroup.IsValid())
        return false;
    auto constants = view;
    const std::array<uint32_t, 4> objectId = {static_cast<uint32_t>(ownerObjectId),
                                              static_cast<uint32_t>(ownerObjectId >> 32u), 0u, 0u};
    std::memcpy(constants.materialTint.data(), objectId.data(), sizeof(objectId));
    constants.lightingControl[1] = m_semantics.sortMode != ParticleSortMode::None ? 1.0f : 0.0f;
    constants.renderingControl[0] = 0.0f;
    constants.renderingControl[2] = static_cast<float>(m_flipbookColumns);
    constants.renderingControl[3] = static_cast<float>(m_flipbookRows);
    constants.alignmentReference[3] = static_cast<float>(m_semantics.spriteAlignment);
    if (m_semantics.spriteAlignment == ParticleSpriteAlignment::Axis)
        std::copy(m_semantics.alignmentAxis.begin(), m_semantics.alignmentAxis.end(),
                  constants.alignmentReference.begin());

    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, geometryGroup);
    encoder.BindGroup(pipeline, 1, m_emptyGroup);
    encoder.BindGroup(pipeline, 2, surfaceGroup);
    encoder.PushConstants(pipeline, rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment, sizeof(constants),
                          &constants);
    encoder.DrawIndirect(indirectArguments);
    return true;
}

rhi::GraphicsPipelineHandle
ParticleGpuBillboardRenderer::GetOrCreatePipeline(const MaterialPassPipelineDescriptor &pass,
                                                  rhi::BindingLayoutHandle perViewLayout)
{
    if (!pass.IsValid() ||
        (pass.target != ShaderCompileTarget::Forward && pass.target != ShaderCompileTarget::ForwardPlus &&
         pass.target != ShaderCompileTarget::Picking && pass.target != ShaderCompileTarget::Motion))
        return {};
    const bool picking = pass.target == ShaderCompileTarget::Picking;
    const bool motion = pass.target == ShaderCompileTarget::Motion;
    const bool usesForwardPlusLighting =
        pass.target == ShaderCompileTarget::ForwardPlus && m_semantics.receiveSceneLighting;
    const bool usesPerViewBindings =
        pass.target == ShaderCompileTarget::Forward || pass.target == ShaderCompileTarget::ForwardPlus;
    if (usesPerViewBindings && !perViewLayout.IsValid())
        return {};
    const auto materialState =
        (picking || motion) ? GpuBillboardMaterialState{3000, false, true, false} : ResolveMaterialState();
    const uint8_t pipelineStateSignature = PipelineStateSignature(materialState);
    for (const auto &entry : m_pipelines) {
        if (entry.pass == pass && entry.perViewLayout == perViewLayout &&
            entry.materialStateSignature == pipelineStateSignature)
            return entry.pipeline;
    }

    rhi::GraphicsPipelineDesc desc;
    desc.vertexShader = motion ? m_motionVertexShader : picking ? m_pickingVertexShader : m_vertexShader;
    desc.fragmentShader = motion                    ? m_motionFragmentShader
                          : picking                 ? m_pickingFragmentShader
                          : usesForwardPlusLighting ? m_forwardPlusFragmentShader
                                                    : m_fragmentShader;
    pass.ApplyRenderingContract(desc);
    desc.raster.cullMode = rhi::CullMode::None;
    desc.depth.testEnabled = materialState.depthTestEnabled && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.depth.writeEnabled =
        materialState.depthWriteEnabled && !pass.depthReadOnly && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.samples = pass.samples;
    for (size_t index = 0; index < pass.colorFormats.size(); ++index) {
        desc.colorTargets[index].format = pass.colorFormats[index];
        desc.colorTargets[index].blendEnabled = materialState.blendEnabled;
        desc.colorTargets[index].premultipliedAlpha = materialState.premultipliedAlpha;
    }
    desc.colorTargetCount = static_cast<uint32_t>(pass.colorFormats.size());
    desc.bindingLayouts[0] = m_geometryLayout;
    desc.bindingLayouts[1] = usesPerViewBindings ? perViewLayout : m_emptyLayout;
    desc.bindingLayouts[2] = m_surface.Layout();
    if (!picking && !motion && m_surface.UsesBindlessTextures())
        desc.bindingLayouts[3] = m_surface.BindlessTableBinding().layout;
    desc.bindingLayoutCount = !picking && !motion && m_surface.UsesBindlessTextures() ? 4 : 3;
    desc.pushConstantStages = rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment;
    desc.pushConstantBytes = sizeof(GpuBillboardViewConstants);
    const auto pipeline = m_device->CreateGraphicsPipeline(desc);
    if (pipeline.IsValid())
        m_pipelines.push_back({pass, perViewLayout, pipelineStateSignature, pipeline});
    return pipeline;
}

} // namespace infernux::particle
