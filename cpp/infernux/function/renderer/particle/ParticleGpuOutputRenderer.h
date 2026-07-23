#pragma once

#include <function/renderer/MaterialPassPipeline.h>
#include <function/renderer/rhi/RhiCommand.h>

#include <array>
#include <cstdint>
#include <vector>

namespace infernux::particle
{

struct alignas(16) GpuParticleViewConstants
{
    std::array<float, 16> viewProjection{};
    // During a shadow pass these hold the current view's light vector and
    // bias data. Surface and picking passes keep their camera-space meaning.
    std::array<float, 4> cameraRight{};
    std::array<float, 4> cameraUp{};
    std::array<float, 4> materialTint{1.0f, 1.0f, 1.0f, 1.0f};
    std::array<float, 4> depthReconstruct{};
    std::array<float, 4> lightingControl{};
    // y is set when the vertex shader is recording a shadow caster pass.
    std::array<float, 4> renderingControl{};
};

struct GpuParticleForwardPlusBindings
{
    rhi::BindingLayoutHandle layout;
    rhi::BindGroupHandle group;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return layout.IsValid() && group.IsValid();
    }
};

struct GpuParticleStaticBuffer
{
    rhi::BufferHandle buffer;
    uint64_t byteSize = 0;
};

class ParticleGpuOutputRenderer
{
  public:
    virtual ~ParticleGpuOutputRenderer() = default;

    [[nodiscard]] virtual bool IsValid() const noexcept = 0;
    [[nodiscard]] virtual int32_t RenderQueue() const noexcept = 0;
    [[nodiscard]] virtual bool RequiresSceneDepth() const noexcept = 0;
    [[nodiscard]] virtual bool CanCastShadows() const noexcept
    {
        return false;
    }
    [[nodiscard]] virtual uint32_t VertexCount() const noexcept = 0;
    [[nodiscard]] virtual rhi::BufferHandle InstanceBuffer() const noexcept = 0;
    [[nodiscard]] virtual rhi::BufferHandle RenderIndexBuffer() const noexcept = 0;
    [[nodiscard]] virtual const std::vector<GpuParticleStaticBuffer> &StaticVertexStorageBuffers() const noexcept
    {
        static const std::vector<GpuParticleStaticBuffer> empty;
        return empty;
    }

    [[nodiscard]] virtual bool RecordDraw(const rhi::GraphicsCommandEncoder &encoder,
                                          rhi::RenderTargetLayoutHandle renderTargetLayout,
                                          const MaterialPassPipelineDescriptor &pass,
                                          rhi::BufferHandle indirectArguments, const GpuParticleViewConstants &view,
                                          rhi::BufferHandle renderIndices = {}, rhi::TextureViewHandle sceneDepth = {},
                                          bool sceneDepthIsDepth = true,
                                          const GpuParticleForwardPlusBindings &forwardPlus = {}) = 0;
    [[nodiscard]] virtual bool RecordPickingDraw(const rhi::GraphicsCommandEncoder &encoder,
                                                 rhi::RenderTargetLayoutHandle renderTargetLayout,
                                                 const MaterialPassPipelineDescriptor &pass,
                                                 rhi::BufferHandle indirectArguments,
                                                 const GpuParticleViewConstants &view, uint64_t ownerObjectId,
                                                 rhi::BufferHandle renderIndices = {}) = 0;
};

static_assert(sizeof(GpuParticleViewConstants) == 160);

} // namespace infernux::particle
