#include "SceneDepthResolver.h"

#include <algorithm>

namespace infernux
{

namespace
{

struct alignas(16) ResolveConstants
{
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t sampleCount = 1;
    uint32_t reserved = 0;
};

static_assert(sizeof(ResolveConstants) == 16);

} // namespace

SceneDepthResolver::~SceneDepthResolver()
{
    Destroy();
}

bool SceneDepthResolver::Initialize(rhi::Device &device, const uint32_t *spirv, size_t wordCount)
{
    Destroy();
    if (!spirv || wordCount < 5 || spirv[0] != 0x07230203u)
        return false;

    m_device = &device;

    rhi::SamplerDesc samplerDesc;
    samplerDesc.minFilter = rhi::FilterMode::Nearest;
    samplerDesc.magFilter = rhi::FilterMode::Nearest;
    samplerDesc.mipFilter = rhi::FilterMode::Nearest;
    samplerDesc.addressU = rhi::AddressMode::ClampToEdge;
    samplerDesc.addressV = rhi::AddressMode::ClampToEdge;
    samplerDesc.addressW = rhi::AddressMode::ClampToEdge;
    m_nearestSampler = device.CreateSampler(samplerDesc);

    rhi::BindingLayoutDesc layoutDesc;
    layoutDesc.entries[0] = {0, rhi::BindingType::CombinedTextureSampler, rhi::ShaderStage::Compute, 1};
    layoutDesc.entries[1] = {1, rhi::BindingType::StorageTexture, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 2;
    m_layout = device.CreateBindingLayout(layoutDesc);

    const auto shader = device.CreateShaderModule(rhi::ShaderModuleDesc::FromSpirV(spirv, wordCount));
    if (shader.IsValid() && m_layout.IsValid()) {
        rhi::ComputePipelineDesc pipelineDesc;
        pipelineDesc.computeShader = shader;
        pipelineDesc.bindingLayouts[0] = m_layout;
        pipelineDesc.bindingLayoutCount = 1;
        pipelineDesc.pushConstantBytes = sizeof(ResolveConstants);
        m_pipeline = device.CreateComputePipeline(pipelineDesc);
    }
    device.Release(shader);

    if (!IsValid()) {
        Destroy();
        return false;
    }
    return true;
}

void SceneDepthResolver::Destroy() noexcept
{
    if (m_device) {
        for (const auto &binding : m_bindings)
            m_device->Release(binding.group);
        m_device->Release(m_pipeline);
        m_device->Release(m_layout);
        m_device->Release(m_nearestSampler);
    }
    m_device = nullptr;
    m_nearestSampler = {};
    m_layout = {};
    m_pipeline = {};
    m_bindings.clear();
}

bool SceneDepthResolver::IsValid() const noexcept
{
    return m_device && m_nearestSampler.IsValid() && m_layout.IsValid() && m_pipeline.IsValid();
}

bool SceneDepthResolver::Record(const rhi::ComputeCommandEncoder &encoder, rhi::TextureViewHandle sourceDepth,
                                rhi::TextureViewHandle resolvedDepth, uint32_t width, uint32_t height,
                                uint32_t sampleCount)
{
    if (!IsValid() || !encoder.IsValid() || !sourceDepth.IsValid() || !resolvedDepth.IsValid() || width == 0 ||
        height == 0 || (sampleCount != 2 && sampleCount != 4 && sampleCount != 8)) {
        return false;
    }

    const auto group = ResolveBindGroup(sourceDepth, resolvedDepth);
    if (!group.IsValid())
        return false;

    ResolveConstants constants;
    constants.width = width;
    constants.height = height;
    constants.sampleCount = sampleCount;
    encoder.BindPipeline(m_pipeline);
    encoder.BindGroup(m_pipeline, 0, group);
    encoder.PushConstants(m_pipeline, sizeof(constants), &constants);
    encoder.Dispatch((width + 7u) / 8u, (height + 7u) / 8u, 1);
    return true;
}

std::vector<rhi::BindGroupHandle> SceneDepthResolver::TakeBindGroups()
{
    std::vector<rhi::BindGroupHandle> result;
    result.reserve(m_bindings.size());
    for (const auto &binding : m_bindings)
        result.push_back(binding.group);
    m_bindings.clear();
    return result;
}

std::string_view SceneDepthResolver::ShaderSource() noexcept
{
    return R"glsl(#version 450
layout(local_size_x = 8, local_size_y = 8, local_size_z = 1) in;
layout(set = 0, binding = 0) uniform sampler2DMS source_depth;
layout(r32f, set = 0, binding = 1) uniform writeonly image2D resolved_depth;
layout(push_constant) uniform ResolveConstants {
    uint width;
    uint height;
    uint sample_count;
    uint reserved;
} pc;

void main() {
    uvec2 coordinate = gl_GlobalInvocationID.xy;
    if (coordinate.x >= pc.width || coordinate.y >= pc.height) return;

    ivec2 pixel = ivec2(coordinate);
    float depth = texelFetch(source_depth, pixel, 0).r;
    for (uint sample_index = 1u; sample_index < pc.sample_count; ++sample_index) {
        // Infernux currently uses conventional 0-near/1-far depth with LESS.
        // The nearest covered sample gives conservative soft-particle edges.
        depth = min(depth, texelFetch(source_depth, pixel, int(sample_index)).r);
    }
    imageStore(resolved_depth, pixel, vec4(depth, 0.0, 0.0, 1.0));
}
)glsl";
}

rhi::BindGroupHandle SceneDepthResolver::ResolveBindGroup(rhi::TextureViewHandle sourceDepth,
                                                          rhi::TextureViewHandle resolvedDepth)
{
    const auto existing = std::find_if(m_bindings.begin(), m_bindings.end(), [&](const auto &entry) {
        return entry.sourceDepth == sourceDepth && entry.resolvedDepth == resolvedDepth;
    });
    if (existing != m_bindings.end())
        return existing->group;

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    groupDesc.textures[0] = {0, rhi::BindingType::CombinedTextureSampler, sourceDepth, m_nearestSampler, true};
    groupDesc.textures[1] = {1, rhi::BindingType::StorageTexture, resolvedDepth, {}, false};
    groupDesc.textureCount = 2;
    const auto group = m_device->CreateBindGroup(groupDesc);
    if (group.IsValid())
        m_bindings.push_back({sourceDepth, resolvedDepth, group});
    return group;
}

} // namespace infernux
