#pragma once

#include "rhi/RhiCommand.h"
#include "rhi/RhiDevice.h"

#include <cstddef>
#include <cstdint>
#include <string_view>
#include <vector>

namespace infernux
{

/// RHI compute implementation of the scene-depth sampling contract. It
/// converts a multisampled depth attachment into a single-sample R32F texture
/// without exposing backend-specific resolve commands to particle rendering.
class SceneDepthResolver
{
  public:
    SceneDepthResolver() = default;
    ~SceneDepthResolver();

    SceneDepthResolver(const SceneDepthResolver &) = delete;
    SceneDepthResolver &operator=(const SceneDepthResolver &) = delete;

    [[nodiscard]] bool Initialize(rhi::Device &device, const uint32_t *spirv, size_t wordCount);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] bool Record(const rhi::ComputeCommandEncoder &encoder, rhi::TextureViewHandle sourceDepth,
                              rhi::TextureViewHandle resolvedDepth, uint32_t width, uint32_t height,
                              uint32_t sampleCount);

    /// Detach graph-specific descriptor groups so the caller can retire them
    /// after in-flight command buffers finish using the previous graph.
    [[nodiscard]] std::vector<rhi::BindGroupHandle> TakeBindGroups();

    [[nodiscard]] static std::string_view ShaderSource() noexcept;

  private:
    struct BindingEntry
    {
        rhi::TextureViewHandle sourceDepth;
        rhi::TextureViewHandle resolvedDepth;
        rhi::BindGroupHandle group;
    };

    [[nodiscard]] rhi::BindGroupHandle ResolveBindGroup(rhi::TextureViewHandle sourceDepth,
                                                        rhi::TextureViewHandle resolvedDepth);

    rhi::Device *m_device = nullptr;
    rhi::SamplerHandle m_nearestSampler;
    rhi::BindingLayoutHandle m_layout;
    rhi::ComputePipelineHandle m_pipeline;
    std::vector<BindingEntry> m_bindings;
};

} // namespace infernux
