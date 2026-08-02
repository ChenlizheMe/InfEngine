#pragma once

#include "rhi/RhiUpload.h"

#include <function/resources/InxTexture/InxTexture.h>

#include <vector>

namespace infernux
{

class TextureUploadBatch final
{
  public:
    TextureUploadBatch(const TextureCpuData &cpuData, const rhi::SamplerDesc &sampler);

    [[nodiscard]] const rhi::TextureUploadRequest &GetRequest() noexcept
    {
        m_request.subresources = m_subresources.data();
        m_request.subresourceCount = static_cast<uint32_t>(m_subresources.size());
        return m_request;
    }

  private:
    rhi::TextureUploadRequest m_request;
    std::vector<rhi::TextureSubresourceUpload> m_subresources;
};

} // namespace infernux
