#include "ParticleGpuMeshRenderer.h"

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

std::vector<uint32_t> CopySpirvWords(const std::vector<char> &bytes)
{
    if (bytes.empty() || bytes.size() % sizeof(uint32_t) != 0)
        return {};
    std::vector<uint32_t> words(bytes.size() / sizeof(uint32_t));
    std::memcpy(words.data(), bytes.data(), bytes.size());
    return words;
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
        desc.vertexShader.wordCount == 0 || !desc.shadowFragmentShader.words ||
        desc.shadowFragmentShader.wordCount == 0 || !desc.shaderProgram || !desc.shaderProgram->IsValid() ||
        desc.shaderProgram->domain != ShaderProgramDomain::ParticleSprite || !desc.textureResolver ||
        !desc.pickingFragmentShader.words || desc.pickingFragmentShader.wordCount == 0 ||
        !desc.motionVertexShader.words || desc.motionVertexShader.wordCount == 0 || !desc.motionFragmentShader.words ||
        desc.motionFragmentShader.wordCount == 0 || !desc.meshVertices.IsValid() || !desc.meshIndices.IsValid() ||
        desc.indexCount == 0 || !desc.meshBufferKeepAlive || !desc.semantics.IsValid()) {
        return false;
    }
    const auto *linkedVariant = desc.shaderProgram->FindVariant(ShaderCompileTarget::Forward);
    const auto *linkedForwardPlusVariant = desc.shaderProgram->FindVariant(ShaderCompileTarget::ForwardPlus);
    if (!linkedVariant || (desc.semantics.receiveSceneLighting && !linkedForwardPlusVariant))
        return false;
    const auto linkedFragmentWords = CopySpirvWords(linkedVariant->fragmentSpirv);
    const auto linkedForwardPlusFragmentWords =
        linkedForwardPlusVariant ? CopySpirvWords(linkedForwardPlusVariant->fragmentSpirv) : std::vector<uint32_t>{};
    if (linkedFragmentWords.empty() || (desc.semantics.receiveSceneLighting && linkedForwardPlusFragmentWords.empty()))
        return false;
    const auto &sourceVertices = desc.mesh->GetVertices();
    const auto &sourceIndices = desc.mesh->GetIndices();
    if (sourceVertices.empty() || sourceIndices.empty() || sourceIndices.size() != desc.indexCount ||
        std::any_of(sourceIndices.begin(), sourceIndices.end(),
                    [&](uint32_t index) { return index >= sourceVertices.size(); })) {
        return false;
    }

    m_device = &device;
    m_shaderProgram = desc.shaderProgram;
    m_deletionQueue = desc.deletionQueue;
    m_mesh = desc.mesh;
    m_meshBufferKeepAlive = desc.meshBufferKeepAlive;
    m_semantics = desc.semantics;
    m_indexCount = desc.indexCount;
    m_instances = desc.instances;
    m_renderIndices = desc.renderIndices;
    m_meshVertices = desc.meshVertices;
    m_meshIndices = desc.meshIndices;
    m_staticVertexStorageBuffers = {
        GpuParticleStaticBuffer{m_meshVertices, sourceVertices.size() * 5u * sizeof(std::array<float, 4>)},
        GpuParticleStaticBuffer{m_meshIndices, sourceIndices.size() * sizeof(uint32_t)},
    };

    m_vertexShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(desc.vertexShader.words, desc.vertexShader.wordCount));
    m_fragmentShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(linkedFragmentWords.data(), linkedFragmentWords.size()));
    m_shadowFragmentShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(desc.shadowFragmentShader.words, desc.shadowFragmentShader.wordCount));
    if (m_semantics.receiveSceneLighting) {
        m_forwardPlusFragmentShader = device.CreateShaderModule(rhi::ShaderModuleDesc::FromSpirV(
            linkedForwardPlusFragmentWords.data(), linkedForwardPlusFragmentWords.size()));
    }
    m_pickingFragmentShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(desc.pickingFragmentShader.words, desc.pickingFragmentShader.wordCount));
    m_motionVertexShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(desc.motionVertexShader.words, desc.motionVertexShader.wordCount));
    m_motionFragmentShader = device.CreateShaderModule(
        rhi::ShaderModuleDesc::FromSpirV(desc.motionFragmentShader.words, desc.motionFragmentShader.wordCount));
    if (!m_meshVertices.IsValid() || !m_meshIndices.IsValid() || !m_vertexShader.IsValid() ||
        !m_fragmentShader.IsValid() || !m_shadowFragmentShader.IsValid() || !m_pickingFragmentShader.IsValid() ||
        !m_motionVertexShader.IsValid() || !m_motionFragmentShader.IsValid() ||
        (m_semantics.receiveSceneLighting && !m_forwardPlusFragmentShader.IsValid())) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layout;
    for (uint32_t binding = 0; binding < 4; ++binding)
        layout.entries[layout.entryCount++] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Vertex, 1};
    m_geometryLayout = device.CreateBindingLayout(layout);
    m_emptyLayout = device.CreateBindingLayout({});
    rhi::BindGroupDesc emptyGroupDesc;
    emptyGroupDesc.layout = m_emptyLayout;
    m_emptyGroup = device.CreateBindGroup(emptyGroupDesc);
    if (!m_geometryLayout.IsValid() || !m_emptyLayout.IsValid() || !m_emptyGroup.IsValid() ||
        !m_surface.Create(device, desc.shaderProgram, desc.material, desc.fallbackMaterial, desc.semantics,
                          desc.textureResolver, desc.deletionQueue)) {
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

void ParticleGpuMeshRenderer::Destroy() noexcept
{
    if (m_device) {
        for (const auto &entry : m_pipelines)
            m_device->Release(entry.pipeline);
        for (const auto &entry : m_viewGroups)
            m_device->Release(entry.group);
        m_device->Release(m_geometryGroup);
        m_device->Release(m_emptyGroup);
        m_device->Release(m_geometryLayout);
        m_device->Release(m_emptyLayout);
        m_device->Release(m_motionFragmentShader);
        m_device->Release(m_motionVertexShader);
        m_device->Release(m_pickingFragmentShader);
        m_device->Release(m_forwardPlusFragmentShader);
        m_device->Release(m_shadowFragmentShader);
        m_device->Release(m_fragmentShader);
        m_device->Release(m_vertexShader);
    }
    m_surface.Destroy();
    m_device = nullptr;
    m_shaderProgram.reset();
    m_deletionQueue = nullptr;
    m_mesh.reset();
    m_meshBufferKeepAlive.reset();
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
    m_shadowFragmentShader = {};
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

bool ParticleGpuMeshRenderer::IsValid() const noexcept
{
    return m_device && m_mesh && m_indexCount > 0 && m_instances.IsValid() && m_renderIndices.IsValid() &&
           m_meshVertices.IsValid() && m_meshIndices.IsValid() && m_vertexShader.IsValid() &&
           m_fragmentShader.IsValid() && m_shadowFragmentShader.IsValid() && m_pickingFragmentShader.IsValid() &&
           m_motionVertexShader.IsValid() && m_motionFragmentShader.IsValid() && m_geometryLayout.IsValid() &&
           m_geometryGroup.IsValid() && m_emptyLayout.IsValid() && m_emptyGroup.IsValid() && m_surface.IsValid();
}

int32_t ParticleGpuMeshRenderer::RenderQueue() const noexcept
{
    return m_surface.ResolveMaterialState().renderQueue;
}

rhi::BindGroupHandle ParticleGpuMeshRenderer::CreateGeometryGroup(rhi::BufferHandle renderIndices) const
{
    if (!m_device || !m_geometryLayout.IsValid() || !renderIndices.IsValid())
        return {};
    rhi::BindGroupDesc group;
    group.layout = m_geometryLayout;
    const std::array<rhi::BufferHandle, 4> buffers = {m_instances, renderIndices, m_meshVertices, m_meshIndices};
    for (uint32_t binding = 0; binding < buffers.size(); ++binding)
        group.buffers[group.bufferCount++] = {binding, rhi::BindingType::StorageBuffer, buffers[binding], 0, 0};
    return m_device->CreateBindGroup(group);
}

rhi::BindGroupHandle ParticleGpuMeshRenderer::ResolveGeometryGroup(rhi::BufferHandle renderIndices)
{
    if (!renderIndices.IsValid() || renderIndices == m_renderIndices)
        return m_geometryGroup;
    const auto found = std::find_if(m_viewGroups.begin(), m_viewGroups.end(),
                                    [&](const auto &entry) { return entry.renderIndices == renderIndices; });
    if (found != m_viewGroups.end())
        return found->group;
    const auto group = CreateGeometryGroup(renderIndices);
    if (group.IsValid())
        m_viewGroups.push_back({renderIndices, group});
    return group;
}

bool ParticleGpuMeshRenderer::RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                         rhi::RenderTargetLayoutHandle renderTargetLayout,
                                         const MaterialPassPipelineDescriptor &pass,
                                         rhi::BufferHandle indirectArguments, const GpuParticleViewConstants &view,
                                         rhi::BufferHandle renderIndices, rhi::TextureViewHandle sceneDepth,
                                         bool sceneDepthIsDepth, const GpuParticlePerViewBindings &perView)
{
    if (!IsValid() || !encoder.IsValid() || !indirectArguments.IsValid() || !m_surface.RefreshMaterialBuffer(false) ||
        !m_surface.RefreshTextureBindings(false))
        return false;
    const bool usesForwardPlusLighting =
        pass.target == ShaderCompileTarget::ForwardPlus && m_semantics.receiveSceneLighting;
    const bool usesPerViewBindings =
        pass.target == ShaderCompileTarget::Forward || pass.target == ShaderCompileTarget::ForwardPlus;
    if (usesPerViewBindings && !perView.IsValid())
        return false;
    const auto pipeline = GetOrCreatePipeline(renderTargetLayout, pass,
                                              usesPerViewBindings ? perView.layout : rhi::BindingLayoutHandle{});
    const auto geometryGroup = ResolveGeometryGroup(renderIndices);
    const auto surfaceGroup = m_surface.ResolveBindGroup(sceneDepth, sceneDepthIsDepth);
    const bool usesBindlessTextures =
        m_surface.UsesBindlessTextures() &&
        (pass.target == ShaderCompileTarget::Forward || pass.target == ShaderCompileTarget::ForwardPlus);
    const auto bindlessTable = m_surface.BindlessTableBinding();
    if (!pipeline.IsValid() || !geometryGroup.IsValid() || !surfaceGroup.IsValid() ||
        (usesBindlessTextures && !bindlessTable.IsValid()))
        return false;
    auto constants = view;
    constants.materialTint = {1.0f, 1.0f, 1.0f, 1.0f};
    constants.lightingControl[0] = usesForwardPlusLighting ? 1.0f : 0.0f;
    constants.renderingControl[0] = m_semantics.receiveShadows ? 1.0f : 0.0f;
    constants.renderingControl[1] = pass.target == ShaderCompileTarget::Shadow ? 1.0f : 0.0f;
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
    const auto geometryGroup = ResolveGeometryGroup(renderIndices);
    const auto surfaceGroup = m_surface.ResolveBindGroup();
    if (!pipeline.IsValid() || !geometryGroup.IsValid() || !surfaceGroup.IsValid())
        return false;
    auto constants = view;
    const std::array<uint32_t, 4> objectId = {static_cast<uint32_t>(ownerObjectId),
                                              static_cast<uint32_t>(ownerObjectId >> 32u), 0u, 0u};
    std::memcpy(constants.materialTint.data(), objectId.data(), sizeof(objectId));
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
ParticleGpuMeshRenderer::GetOrCreatePipeline(rhi::RenderTargetLayoutHandle renderTargetLayout,
                                             const MaterialPassPipelineDescriptor &pass,
                                             rhi::BindingLayoutHandle perViewLayout)
{
    if ((!pass.UsesDynamicRendering() && !renderTargetLayout.IsValid()) || !pass.IsValid() ||
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
    const bool usesPerViewBindings =
        pass.target == ShaderCompileTarget::Forward || pass.target == ShaderCompileTarget::ForwardPlus;
    if (usesPerViewBindings && !perViewLayout.IsValid())
        return {};
    const auto state = (picking || shadow) ? GpuBillboardMaterialState{3000, false, true, true}
                       : motion            ? GpuBillboardMaterialState{3000, false, true, false}
                                           : m_surface.ResolveMaterialState();
    const uint8_t signature = PipelineStateSignature(state);
    const auto found = std::find_if(m_pipelines.begin(), m_pipelines.end(), [&](const auto &entry) {
        return entry.renderTargetLayout == renderTargetLayout && entry.pass == pass &&
               entry.perViewLayout == perViewLayout && entry.materialStateSignature == signature;
    });
    if (found != m_pipelines.end())
        return found->pipeline;

    rhi::GraphicsPipelineDesc desc;
    desc.vertexShader = motion ? m_motionVertexShader : m_vertexShader;
    desc.fragmentShader = motion    ? m_motionFragmentShader
                          : picking ? m_pickingFragmentShader
                          : shadow  ? m_shadowFragmentShader
                                    : (usesForwardPlusLighting ? m_forwardPlusFragmentShader : m_fragmentShader);
    pass.ApplyRenderingContract(desc, renderTargetLayout);
    desc.raster.cullMode = rhi::CullMode::Back;
    desc.raster.frontFace = rhi::FrontFace::Clockwise;
    desc.depth.testEnabled = state.depthTestEnabled && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.depth.writeEnabled =
        state.depthWriteEnabled && !pass.depthReadOnly && pass.depthFormat != rhi::PixelFormat::Undefined;
    desc.samples = pass.samples;
    for (size_t index = 0; index < pass.colorFormats.size(); ++index) {
        desc.colorTargets[index].format = pass.colorFormats[index];
        desc.colorTargets[index].blendEnabled = state.blendEnabled;
        desc.colorTargets[index].premultipliedAlpha = state.premultipliedAlpha;
    }
    desc.colorTargetCount = static_cast<uint32_t>(pass.colorFormats.size());
    desc.bindingLayouts[0] = m_geometryLayout;
    desc.bindingLayouts[1] = usesPerViewBindings ? perViewLayout : m_emptyLayout;
    desc.bindingLayouts[2] = m_surface.Layout();
    const bool usesBindlessTextures = !picking && !shadow && !motion && m_surface.UsesBindlessTextures();
    if (usesBindlessTextures)
        desc.bindingLayouts[3] = m_surface.BindlessTableBinding().layout;
    desc.bindingLayoutCount = usesBindlessTextures ? 4 : 3;
    desc.pushConstantStages = rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment;
    desc.pushConstantBytes = sizeof(GpuParticleViewConstants);
    const auto pipeline = m_device->CreateGraphicsPipeline(desc);
    if (pipeline.IsValid())
        m_pipelines.push_back({renderTargetLayout, pass, perViewLayout, signature, pipeline});
    return pipeline;
}

} // namespace infernux::particle
