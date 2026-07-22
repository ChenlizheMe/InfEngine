#pragma once

#include <function/renderer/MaterialPassPipeline.h>
#include <function/renderer/rhi/RhiCommand.h>

#include <array>
#include <cstdint>

namespace infernux::particle
{

struct alignas(16) GpuParticleViewConstants
{
    std::array<float, 16> viewProjection{};
    std::array<float, 4> cameraRight{};
    std::array<float, 4> cameraUp{};
    std::array<float, 4> materialTint{1.0f, 1.0f, 1.0f, 1.0f};
    std::array<float, 4> depthReconstruct{};
};

class ParticleGpuOutputRenderer
{
  public:
    virtual ~ParticleGpuOutputRenderer() = default;

    [[nodiscard]] virtual bool IsValid() const noexcept = 0;
    [[nodiscard]] virtual int32_t RenderQueue() const noexcept = 0;
    [[nodiscard]] virtual bool RequiresSceneDepth() const noexcept = 0;
    [[nodiscard]] virtual uint32_t VertexCount() const noexcept = 0;
    [[nodiscard]] virtual rhi::BufferHandle InstanceBuffer() const noexcept = 0;
    [[nodiscard]] virtual rhi::BufferHandle RenderIndexBuffer() const noexcept = 0;

    [[nodiscard]] virtual bool RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                          rhi::RenderTargetLayoutHandle renderTargetLayout,
                                          const MaterialPassPipelineDescriptor &pass,
                                          rhi::BufferHandle indirectArguments, const GpuParticleViewConstants &view,
                                          rhi::BufferHandle renderIndices = {}, rhi::TextureViewHandle sceneDepth = {},
                                          bool sceneDepthIsDepth = true) = 0;
    [[nodiscard]] virtual bool RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                                 rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                 const MaterialPassPipelineDescriptor &pass,
                                                 rhi::BufferHandle indirectArguments,
                                                 const GpuParticleViewConstants &view, uint64_t ownerObjectId,
                                                 rhi::BufferHandle renderIndices = {}) = 0;
};

static_assert(sizeof(GpuParticleViewConstants) == 128);

} // namespace infernux::particle
