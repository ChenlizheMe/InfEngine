#pragma once

#include <function/renderer/rhi/RhiCommand.h>

#include <cstdint>
#include <memory>
#include <string>

namespace infernux
{

class InxVkCoreModular;

struct FullscreenPushConstants
{
    float values[32] = {};
};

struct FullscreenPipelineKey
{
    std::string shaderName;
    rhi::RenderTargetLayoutHandle renderTargetLayout;
    rhi::SampleCount samples = rhi::SampleCount::One;
    rhi::PixelFormat colorFormat = rhi::PixelFormat::RGBA8UNorm;
    uint32_t inputTextureCount = 0;

    bool operator==(const FullscreenPipelineKey &other) const noexcept;
};

struct FullscreenPipelineKeyHash
{
    size_t operator()(const FullscreenPipelineKey &key) const noexcept;
};

struct FullscreenPipelineEntry
{
    rhi::GraphicsPipelineHandle pipeline;
    rhi::BindingLayoutHandle inputLayout;
    bool hasPerView = false;
    bool hasGlobals = false;
};

struct FullscreenTextureInput
{
    rhi::TextureViewHandle view;
    rhi::PixelFormat format = rhi::PixelFormat::Undefined;
    bool depthRead = false;
};

class FullscreenRenderer
{
  public:
    FullscreenRenderer();
    ~FullscreenRenderer();

    FullscreenRenderer(const FullscreenRenderer &) = delete;
    FullscreenRenderer &operator=(const FullscreenRenderer &) = delete;

    void Initialize(InxVkCoreModular *vkCore);
    void Destroy();

    const FullscreenPipelineEntry &EnsurePipeline(const FullscreenPipelineKey &key);

    /// Retire every cached pipeline compiled from @p shaderName. The next
    /// record resolves the newly published module and builds a fresh pipeline.
    void InvalidateShader(const std::string &shaderName);

    rhi::BindGroupHandle AllocateBindGroup(rhi::BindingLayoutHandle layout, const FullscreenTextureInput *inputs,
                                           uint32_t inputCount,
                                           rhi::SamplerHandle colorSampler);

    void Draw(rhi::GraphicsCommandEncoder &encoder, const FullscreenPipelineEntry &entry,
              rhi::BindGroupHandle inputGroup, rhi::BindGroupHandle perViewGroup,
              const FullscreenPushConstants &pushConstants, uint32_t pushConstantSize);

    void ResetPool();

    [[nodiscard]] rhi::SamplerHandle GetLinearSampler() const noexcept;

  private:
    struct Impl;
    std::unique_ptr<Impl> m_impl;
};

} // namespace infernux
