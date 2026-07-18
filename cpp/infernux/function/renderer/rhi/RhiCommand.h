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

  private:
    void *m_context = nullptr;
    const Dispatch *m_dispatch = nullptr;
};

static_assert(sizeof(GraphicsCommandEncoder) == sizeof(void *) * 2);

} // namespace infernux::rhi
