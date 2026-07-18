#pragma once

#include "RhiDescriptors.h"

#include <cstddef>
#include <cstdint>

namespace infernux::rhi
{

class GraphicsCommandEncoder
{
  public:
    struct Dispatch
    {
        void (*bindPipeline)(void *, GraphicsPipelineHandle) = nullptr;
        void (*bindGroup)(void *, GraphicsPipelineHandle, uint32_t, BindGroupHandle) = nullptr;
        void (*pushConstants)(void *, GraphicsPipelineHandle, ShaderStage, uint32_t, const void *) = nullptr;
        void (*draw)(void *, uint32_t, uint32_t, uint32_t, uint32_t) = nullptr;
        void (*drawIndirect)(void *, BufferHandle, uint64_t, uint32_t, uint32_t) = nullptr;
    };

    constexpr GraphicsCommandEncoder() noexcept = default;
    constexpr GraphicsCommandEncoder(void *context, const Dispatch *dispatch) noexcept
        : m_context(context), m_dispatch(dispatch)
    {
    }

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return m_context != nullptr && m_dispatch != nullptr;
    }

    void BindPipeline(GraphicsPipelineHandle pipeline) const
    {
        if (IsValid() && m_dispatch->bindPipeline)
            m_dispatch->bindPipeline(m_context, pipeline);
    }

    void BindGroup(GraphicsPipelineHandle pipeline, uint32_t setIndex, BindGroupHandle group) const
    {
        if (IsValid() && m_dispatch->bindGroup)
            m_dispatch->bindGroup(m_context, pipeline, setIndex, group);
    }

    void PushConstants(GraphicsPipelineHandle pipeline, ShaderStage stages, uint32_t byteSize, const void *data) const
    {
        if (IsValid() && m_dispatch->pushConstants && byteSize > 0 && data)
            m_dispatch->pushConstants(m_context, pipeline, stages, byteSize, data);
    }

    void Draw(uint32_t vertexCount, uint32_t instanceCount = 1, uint32_t firstVertex = 0,
              uint32_t firstInstance = 0) const
    {
        if (IsValid() && m_dispatch->draw)
            m_dispatch->draw(m_context, vertexCount, instanceCount, firstVertex, firstInstance);
    }

    void DrawIndirect(BufferHandle arguments, uint64_t offset = 0, uint32_t drawCount = 1, uint32_t stride = 16) const
    {
        if (IsValid() && m_dispatch->drawIndirect && arguments.IsValid() && drawCount > 0)
            m_dispatch->drawIndirect(m_context, arguments, offset, drawCount, stride);
    }

  private:
    void *m_context = nullptr;
    const Dispatch *m_dispatch = nullptr;
};

class ComputeCommandEncoder
{
  public:
    struct DispatchTable
    {
        void (*bindPipeline)(void *, ComputePipelineHandle) = nullptr;
        void (*bindGroup)(void *, ComputePipelineHandle, uint32_t, BindGroupHandle) = nullptr;
        void (*pushConstants)(void *, ComputePipelineHandle, uint32_t, const void *) = nullptr;
        void (*dispatch)(void *, uint32_t, uint32_t, uint32_t) = nullptr;
        void (*dispatchIndirect)(void *, BufferHandle, uint64_t) = nullptr;
    };

    constexpr ComputeCommandEncoder() noexcept = default;
    constexpr ComputeCommandEncoder(void *context, const DispatchTable *dispatch) noexcept
        : m_context(context), m_dispatch(dispatch)
    {
    }

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return m_context != nullptr && m_dispatch != nullptr;
    }

    void BindPipeline(ComputePipelineHandle pipeline) const
    {
        if (IsValid() && m_dispatch->bindPipeline)
            m_dispatch->bindPipeline(m_context, pipeline);
    }

    void BindGroup(ComputePipelineHandle pipeline, uint32_t setIndex, BindGroupHandle group) const
    {
        if (IsValid() && m_dispatch->bindGroup)
            m_dispatch->bindGroup(m_context, pipeline, setIndex, group);
    }

    void PushConstants(ComputePipelineHandle pipeline, uint32_t byteSize, const void *data) const
    {
        if (IsValid() && m_dispatch->pushConstants && byteSize > 0 && data)
            m_dispatch->pushConstants(m_context, pipeline, byteSize, data);
    }

    void Dispatch(uint32_t groupCountX, uint32_t groupCountY = 1, uint32_t groupCountZ = 1) const
    {
        if (IsValid() && m_dispatch->dispatch && groupCountX > 0 && groupCountY > 0 && groupCountZ > 0)
            m_dispatch->dispatch(m_context, groupCountX, groupCountY, groupCountZ);
    }

    void DispatchIndirect(BufferHandle arguments, uint64_t offset = 0) const
    {
        if (IsValid() && m_dispatch->dispatchIndirect && arguments.IsValid())
            m_dispatch->dispatchIndirect(m_context, arguments, offset);
    }

  private:
    void *m_context = nullptr;
    const DispatchTable *m_dispatch = nullptr;
};

static_assert(sizeof(GraphicsCommandEncoder) == sizeof(void *) * 2);
static_assert(sizeof(ComputeCommandEncoder) == sizeof(void *) * 2);

} // namespace infernux::rhi
