#pragma once

#include "RhiDescriptors.h"

#include <cstddef>
#include <cstdint>

namespace infernux::rhi
{

enum class BufferUsage : uint8_t
{
    Vertex,
    Index,
    Storage,
};

struct BufferUploadRequest
{
    const void *data = nullptr;
    size_t byteSize = 0;
    BufferUsage usage = BufferUsage::Storage;
};

struct TextureSubresourceUpload
{
    const void *data = nullptr;
    size_t byteSize = 0;
    uint32_t mipLevel = 0;
    uint32_t baseLayer = 0;
    uint32_t layerCount = 1;
    uint32_t width = 1;
    uint32_t height = 1;
    uint32_t depth = 1;
    size_t rowPitch = 0;
    size_t slicePitch = 0;
};

struct TextureUploadRequest
{
    TextureDesc texture;
    TextureViewDesc view;
    SamplerDesc sampler;
    const TextureSubresourceUpload *subresources = nullptr;
    uint32_t subresourceCount = 0;
};

} // namespace infernux::rhi
