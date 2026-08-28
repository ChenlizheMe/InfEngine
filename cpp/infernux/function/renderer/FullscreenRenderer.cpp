#include "FullscreenRenderer.h"

#include "rhi/RhiDevice.h"

#include <algorithm>
#include <unordered_map>
#include <utility>
#include <vector>

namespace infernux
{

bool FullscreenPipelineKey::operator==(const FullscreenPipelineKey &other) const noexcept
{
    return shaderName == other.shaderName && renderTargetLayout == other.renderTargetLayout &&
           samples == other.samples && colorFormat == other.colorFormat &&
           inputTextureCount == other.inputTextureCount && depthInputMask == other.depthInputMask &&
           useDynamicRendering == other.useDynamicRendering;
}

size_t FullscreenPipelineKeyHash::operator()(const FullscreenPipelineKey &key) const noexcept
{
    size_t hash = std::hash<std::string>{}(key.shaderName);
    const auto combine = [&hash](size_t value) { hash ^= value + 0x9e3779b9U + (hash << 6) + (hash >> 2); };
    combine(rhi::HandleHash<rhi::RenderTargetLayoutTag>{}(key.renderTargetLayout));
    combine(static_cast<size_t>(key.samples));
    combine(static_cast<size_t>(key.colorFormat));
    combine(key.inputTextureCount);
    combine(key.depthInputMask);
    combine(key.useDynamicRendering ? 1U : 0U);
    return hash;
}

struct FullscreenRenderer::Impl
{
    struct PipelineEntry
    {
        FullscreenPipelineEntry rhi;
        rhi::BindingLayoutHandle emptyGapLayout;
    };

    std::shared_ptr<FullscreenRendererHost> host;
    rhi::Device *device = nullptr;
    rhi::SamplerHandle linearSampler;
    rhi::SamplerHandle nearestSampler;
    std::unordered_map<FullscreenPipelineKey, PipelineEntry, FullscreenPipelineKeyHash> pipelines;
    std::vector<std::vector<rhi::BindGroupHandle>> frameBindGroups;

    [[nodiscard]] uint32_t CurrentFrame() const noexcept
    {
        if (!host || frameBindGroups.empty())
            return 0;
        return host->GetCurrentFrame() % static_cast<uint32_t>(frameBindGroups.size());
    }

    void ReleaseBindGroups(uint32_t frame)
    {
        if (!device || frame >= frameBindGroups.size())
            return;
        for (const auto handle : frameBindGroups[frame])
            device->Release(handle);
        frameBindGroups[frame].clear();
    }

    void DestroyPipeline(PipelineEntry &entry)
    {
        if (device) {
            device->Release(entry.rhi.pipeline);
            device->Release(entry.rhi.inputLayout);
            device->Release(entry.emptyGapLayout);
        }
        entry = {};
    }

    PipelineEntry CreatePipeline(const FullscreenPipelineKey &key)
    {
        PipelineEntry entry;
        if (!host || !device || key.inputTextureCount > rhi::BindingLayoutDesc::MaxEntries ||
            (!key.useDynamicRendering && !key.renderTargetLayout.IsValid()))
            return entry;

        rhi::BindingLayoutDesc inputLayoutDesc;
        inputLayoutDesc.entryCount = key.inputTextureCount;
        for (uint32_t index = 0; index < key.inputTextureCount; ++index) {
            auto &binding = inputLayoutDesc.entries[index];
            binding.binding = index;
            binding.type = rhi::BindingType::CombinedTextureSampler;
            binding.visibility = rhi::ShaderStage::Fragment;
            binding.depthRead = (key.depthInputMask & (1u << index)) != 0;
        }
        entry.rhi.inputLayout = device->CreateBindingLayout(inputLayoutDesc);
        if (!entry.rhi.inputLayout.IsValid())
            return entry;

        rhi::GraphicsPipelineDesc desc;
        desc.useDynamicRendering = key.useDynamicRendering;
        desc.renderTargetLayout = key.renderTargetLayout;
        desc.topology = rhi::PrimitiveTopology::TriangleList;
        desc.raster.cullMode = rhi::CullMode::None;
        desc.raster.frontFace = rhi::FrontFace::Clockwise;
        desc.samples = key.samples;
        desc.colorTargetCount = 1;
        desc.colorTargets[0].format = key.colorFormat;
        desc.bindingLayouts[desc.bindingLayoutCount++] = entry.rhi.inputLayout;
        desc.pushConstantStages = rhi::ShaderStage::Fragment;
        desc.pushConstantBytes = sizeof(FullscreenPushConstants);
        if (key.useDynamicRendering) {
            desc.renderingSignature.colorFormatCount = 1;
            desc.renderingSignature.colorFormats[0] = key.colorFormat;
            desc.renderingSignature.samples = key.samples;
        }

        const auto perViewLayout = host->GetPerViewLayout();
        const auto globalsLayout = host->GetGlobalsLayout();
        if (perViewLayout.IsValid() || globalsLayout.IsValid()) {
            if (perViewLayout.IsValid()) {
                desc.bindingLayouts[desc.bindingLayoutCount++] = perViewLayout;
            } else {
                entry.emptyGapLayout = device->CreateBindingLayout({});
                if (!entry.emptyGapLayout.IsValid()) {
                    DestroyPipeline(entry);
                    return {};
                }
                desc.bindingLayouts[desc.bindingLayoutCount++] = entry.emptyGapLayout;
            }
        }
        if (globalsLayout.IsValid())
            desc.bindingLayouts[desc.bindingLayoutCount++] = globalsLayout;

        desc.vertexShader = host->AcquireShaderModule("Fullscreen Triangle", rhi::ShaderStage::Vertex);
        desc.fragmentShader = host->AcquireShaderModule(key.shaderName, rhi::ShaderStage::Fragment);
        if (!desc.vertexShader.IsValid() || !desc.fragmentShader.IsValid()) {
            host->ReportError("FullscreenRenderer: missing shader modules for '" + key.shaderName + "'");
            device->Release(desc.vertexShader);
            device->Release(desc.fragmentShader);
            DestroyPipeline(entry);
            return {};
        }

        entry.rhi.pipeline = device->CreateGraphicsPipeline(desc);
        device->Release(desc.vertexShader);
        device->Release(desc.fragmentShader);
        if (!entry.rhi.pipeline.IsValid()) {
            host->ReportError("FullscreenRenderer: failed to create pipeline for '" + key.shaderName + "'");
            DestroyPipeline(entry);
            return {};
        }
        entry.rhi.hasPerView = perViewLayout.IsValid();
        entry.rhi.hasGlobals = globalsLayout.IsValid();
        return entry;
    }
};

FullscreenRenderer::FullscreenRenderer() = default;
FullscreenRenderer::~FullscreenRenderer()
{
    Destroy();
}

void FullscreenRenderer::Initialize(std::shared_ptr<FullscreenRendererHost> host)
{
    Destroy();
    if (!host)
        return;

    m_impl = std::make_unique<Impl>();
    m_impl->host = std::move(host);
    m_impl->device = &m_impl->host->GetRhiDevice();

    rhi::SamplerDesc linear;
    linear.addressU = rhi::AddressMode::ClampToEdge;
    linear.addressV = rhi::AddressMode::ClampToEdge;
    linear.addressW = rhi::AddressMode::ClampToEdge;
    rhi::SamplerDesc nearest = linear;
    nearest.minFilter = rhi::FilterMode::Nearest;
    nearest.magFilter = rhi::FilterMode::Nearest;
    nearest.mipFilter = rhi::FilterMode::Nearest;
    m_impl->linearSampler = m_impl->device->CreateSampler(linear);
    m_impl->nearestSampler = m_impl->device->CreateSampler(nearest);
    if (!m_impl->linearSampler.IsValid() || !m_impl->nearestSampler.IsValid()) {
        m_impl->host->ReportError("FullscreenRenderer: failed to create RHI samplers");
        Destroy();
        return;
    }

    const uint32_t frames = std::max(1u, m_impl->host->GetFrameCount());
    m_impl->frameBindGroups.resize(frames);
}

void FullscreenRenderer::Destroy()
{
    if (!m_impl)
        return;
    for (uint32_t frame = 0; frame < m_impl->frameBindGroups.size(); ++frame)
        m_impl->ReleaseBindGroups(frame);
    for (auto &[key, entry] : m_impl->pipelines)
        m_impl->DestroyPipeline(entry);
    if (m_impl->device) {
        m_impl->device->Release(m_impl->linearSampler);
        m_impl->device->Release(m_impl->nearestSampler);
    }
    m_impl.reset();
}

const FullscreenPipelineEntry &FullscreenRenderer::EnsurePipeline(const FullscreenPipelineKey &key)
{
    static const FullscreenPipelineEntry invalid;
    if (!m_impl)
        return invalid;
    const auto found = m_impl->pipelines.find(key);
    if (found != m_impl->pipelines.end())
        return found->second.rhi;
    auto [it, inserted] = m_impl->pipelines.emplace(key, m_impl->CreatePipeline(key));
    return it->second.rhi;
}

void FullscreenRenderer::InvalidateShader(const std::string &shaderName)
{
    if (!m_impl || shaderName.empty())
        return;

    size_t retired = 0;
    for (auto entry = m_impl->pipelines.begin(); entry != m_impl->pipelines.end();) {
        if (entry->first.shaderName != shaderName) {
            ++entry;
            continue;
        }
        auto pipeline = std::move(entry->second);
        entry = m_impl->pipelines.erase(entry);
        m_impl->DestroyPipeline(pipeline);
        ++retired;
    }
    if (retired > 0) {
        m_impl->host->ReportInfo("FullscreenRenderer: retired " + std::to_string(retired) +
                                 " pipeline revision(s) for shader '" + shaderName + "'");
    }
}

rhi::BindGroupHandle FullscreenRenderer::AllocateBindGroup(rhi::BindingLayoutHandle layout,
                                                           const FullscreenTextureInput *inputs, uint32_t inputCount,
                                                           rhi::SamplerHandle colorSampler)
{
    if (!m_impl || !m_impl->device || m_impl->frameBindGroups.empty())
        return {};
    const uint32_t frame = m_impl->CurrentFrame();

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = layout;
    groupDesc.lifetime = rhi::BindGroupLifetime::FrameTransient;
    groupDesc.textureCount = inputCount;
    if (!layout.IsValid() || !inputs || inputCount > groupDesc.textures.size())
        return {};
    for (uint32_t i = 0; i < inputCount; ++i) {
        const auto &input = inputs[i];
        const bool nearestSampling = input.depthRead || rhi::IsIntegerFormat(input.format);
        auto &texture = groupDesc.textures[i];
        texture.binding = i;
        texture.type = rhi::BindingType::CombinedTextureSampler;
        texture.texture = input.view;
        texture.sampler = nearestSampling ? m_impl->nearestSampler : colorSampler;
        texture.depthRead = input.depthRead;
        if (!texture.texture.IsValid() || !texture.sampler.IsValid())
            return {};
    }

    const auto handle = m_impl->device->CreateBindGroup(groupDesc);
    if (handle.IsValid())
        m_impl->frameBindGroups[frame].push_back(handle);
    return handle;
}

void FullscreenRenderer::Draw(rhi::GraphicsCommandEncoder &encoder, const FullscreenPipelineEntry &entry,
                              rhi::BindGroupHandle inputGroup, rhi::BindGroupHandle perViewGroup,
                              const FullscreenPushConstants &pushConstants, uint32_t pushConstantSize)
{
    if (!m_impl || !entry.pipeline.IsValid())
        return;
    encoder.BindPipeline(entry.pipeline);
    if (inputGroup.IsValid())
        encoder.BindGroup(entry.pipeline, 0, inputGroup);
    if (entry.hasPerView && perViewGroup.IsValid())
        encoder.BindGroup(entry.pipeline, 1, perViewGroup);
    if (entry.hasGlobals) {
        const auto globalsGroup = m_impl->host->GetCurrentGlobalsGroup();
        if (globalsGroup.IsValid())
            encoder.BindGroup(entry.pipeline, 2, globalsGroup);
    }
    encoder.PushConstants(entry.pipeline, rhi::ShaderStage::Fragment, pushConstantSize, pushConstants.values);
    encoder.Draw(3);
}

void FullscreenRenderer::ResetPool()
{
    if (!m_impl || m_impl->frameBindGroups.empty())
        return;
    m_impl->ReleaseBindGroups(m_impl->CurrentFrame());
}

rhi::SamplerHandle FullscreenRenderer::GetLinearSampler() const noexcept
{
    return m_impl ? m_impl->linearSampler : rhi::SamplerHandle{};
}

} // namespace infernux
