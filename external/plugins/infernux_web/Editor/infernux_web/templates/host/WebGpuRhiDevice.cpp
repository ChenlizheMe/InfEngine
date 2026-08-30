#include "WebGpuRhiDevice.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <mutex>
#include <string_view>
#include <utility>

namespace infernux::web
{
namespace
{

bool IsIdentifierCharacter(char value)
{
    return (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') || (value >= '0' && value <= '9') ||
           value == '_';
}

bool HasIdentifierUseAfter(std::string_view source, std::string_view identifier, size_t offset)
{
    while ((offset = source.find(identifier, offset)) != std::string_view::npos) {
        const bool startsCleanly = offset == 0 || !IsIdentifierCharacter(source[offset - 1]);
        const size_t end = offset + identifier.size();
        const bool endsCleanly = end == source.size() || !IsIdentifierCharacter(source[end]);
        if (startsCleanly && endsCleanly)
            return true;
        offset = end;
    }
    return false;
}

std::array<std::vector<uint32_t>, rhi::ComputePipelineDesc::MaxBindingLayouts>
ReflectUsedComputeBindings(std::string_view source)
{
    std::array<std::vector<uint32_t>, rhi::ComputePipelineDesc::MaxBindingLayouts> result;
    size_t cursor = 0;
    while ((cursor = source.find("@group(", cursor)) != std::string_view::npos) {
        const size_t groupStart = cursor + 7;
        size_t groupEnd = groupStart;
        while (groupEnd < source.size() && source[groupEnd] >= '0' && source[groupEnd] <= '9')
            ++groupEnd;
        if (groupEnd == groupStart)
            break;
        const uint32_t group =
            static_cast<uint32_t>(std::stoul(std::string(source.substr(groupStart, groupEnd - groupStart))));
        const size_t bindingMarker = source.find("@binding(", groupEnd);
        const size_t statementEnd = source.find(';', groupEnd);
        if (bindingMarker == std::string_view::npos || statementEnd == std::string_view::npos ||
            bindingMarker > statementEnd) {
            cursor = groupEnd;
            continue;
        }
        const size_t bindingStart = bindingMarker + 9;
        size_t bindingEnd = bindingStart;
        while (bindingEnd < source.size() && source[bindingEnd] >= '0' && source[bindingEnd] <= '9')
            ++bindingEnd;
        const size_t colon = source.rfind(':', statementEnd);
        if (bindingEnd == bindingStart || colon == std::string_view::npos || colon < bindingEnd ||
            group >= result.size()) {
            cursor = statementEnd + 1;
            continue;
        }
        size_t identifierEnd = colon;
        while (identifierEnd > bindingEnd && (source[identifierEnd - 1] == ' ' || source[identifierEnd - 1] == '\t'))
            --identifierEnd;
        size_t identifierStart = identifierEnd;
        while (identifierStart > bindingEnd && IsIdentifierCharacter(source[identifierStart - 1]))
            --identifierStart;
        const std::string_view identifier = source.substr(identifierStart, identifierEnd - identifierStart);
        if (!identifier.empty() && HasIdentifierUseAfter(source, identifier, statementEnd + 1)) {
            const uint32_t binding =
                static_cast<uint32_t>(std::stoul(std::string(source.substr(bindingStart, bindingEnd - bindingStart))));
            auto &bindings = result[group];
            if (std::find(bindings.begin(), bindings.end(), binding) == bindings.end())
                bindings.push_back(binding);
        }
        cursor = statementEnd + 1;
    }
    return result;
}

bool UsesBinding(const std::vector<uint32_t> *usedBindings, uint32_t binding)
{
    return usedBindings == nullptr ||
           std::find(usedBindings->begin(), usedBindings->end(), binding) != usedBindings->end();
}

wgpu::ShaderStage ToWebShaderStage(rhi::ShaderStage stages)
{
    wgpu::ShaderStage result = wgpu::ShaderStage::None;
    if (rhi::HasShaderStage(stages, rhi::ShaderStage::Vertex))
        result |= wgpu::ShaderStage::Vertex;
    if (rhi::HasShaderStage(stages, rhi::ShaderStage::Fragment))
        result |= wgpu::ShaderStage::Fragment;
    if (rhi::HasShaderStage(stages, rhi::ShaderStage::Compute))
        result |= wgpu::ShaderStage::Compute;
    return result;
}

wgpu::TextureFormat ToWebFormat(rhi::PixelFormat format)
{
    switch (format) {
    case rhi::PixelFormat::R8UNorm:
        return wgpu::TextureFormat::R8Unorm;
    case rhi::PixelFormat::RG8UNorm:
        return wgpu::TextureFormat::RG8Unorm;
    case rhi::PixelFormat::RGBA8UNorm:
        return wgpu::TextureFormat::RGBA8Unorm;
    case rhi::PixelFormat::RGBA8Srgb:
        return wgpu::TextureFormat::RGBA8UnormSrgb;
    case rhi::PixelFormat::BGRA8UNorm:
        return wgpu::TextureFormat::BGRA8Unorm;
    case rhi::PixelFormat::BGRA8Srgb:
        return wgpu::TextureFormat::BGRA8UnormSrgb;
    case rhi::PixelFormat::R16SFloat:
        return wgpu::TextureFormat::R16Float;
    case rhi::PixelFormat::RG16SFloat:
        return wgpu::TextureFormat::RG16Float;
    case rhi::PixelFormat::RGBA16SFloat:
        return wgpu::TextureFormat::RGBA16Float;
    case rhi::PixelFormat::RGBA16UNorm:
        return wgpu::TextureFormat::RGBA16Unorm;
    case rhi::PixelFormat::R32SFloat:
        return wgpu::TextureFormat::R32Float;
    case rhi::PixelFormat::RG32UInt:
        return wgpu::TextureFormat::RG32Uint;
    case rhi::PixelFormat::RGBA32SFloat:
        return wgpu::TextureFormat::RGBA32Float;
    case rhi::PixelFormat::RGB10A2UNorm:
        return wgpu::TextureFormat::RGB10A2Unorm;
    case rhi::PixelFormat::BC1RgbaUNorm:
        return wgpu::TextureFormat::BC1RGBAUnorm;
    case rhi::PixelFormat::BC1RgbaSrgb:
        return wgpu::TextureFormat::BC1RGBAUnormSrgb;
    case rhi::PixelFormat::BC3UNorm:
        return wgpu::TextureFormat::BC3RGBAUnorm;
    case rhi::PixelFormat::BC3Srgb:
        return wgpu::TextureFormat::BC3RGBAUnormSrgb;
    case rhi::PixelFormat::BC4UNorm:
        return wgpu::TextureFormat::BC4RUnorm;
    case rhi::PixelFormat::BC5UNorm:
        return wgpu::TextureFormat::BC5RGUnorm;
    case rhi::PixelFormat::BC6HUFloat:
        return wgpu::TextureFormat::BC6HRGBUfloat;
    case rhi::PixelFormat::BC7UNorm:
        return wgpu::TextureFormat::BC7RGBAUnorm;
    case rhi::PixelFormat::BC7Srgb:
        return wgpu::TextureFormat::BC7RGBAUnormSrgb;
    case rhi::PixelFormat::D32SFloat:
        return wgpu::TextureFormat::Depth32Float;
    case rhi::PixelFormat::D24UNormS8UInt:
        return wgpu::TextureFormat::Depth24PlusStencil8;
    default:
        return wgpu::TextureFormat::Undefined;
    }
}

wgpu::BufferUsage ToWebBufferUsage(const rhi::BufferDesc &desc)
{
    wgpu::BufferUsage usage = wgpu::BufferUsage::None;
    if (rhi::HasBufferUsage(desc.usage, rhi::BufferUsageFlags::Storage))
        usage |= wgpu::BufferUsage::Storage;
    if (rhi::HasBufferUsage(desc.usage, rhi::BufferUsageFlags::Uniform))
        usage |= wgpu::BufferUsage::Uniform;
    if (rhi::HasBufferUsage(desc.usage, rhi::BufferUsageFlags::Vertex))
        usage |= wgpu::BufferUsage::Vertex;
    if (rhi::HasBufferUsage(desc.usage, rhi::BufferUsageFlags::Index))
        usage |= wgpu::BufferUsage::Index;
    if (rhi::HasBufferUsage(desc.usage, rhi::BufferUsageFlags::Indirect))
        usage |= wgpu::BufferUsage::Indirect;
    if (rhi::HasBufferUsage(desc.usage, rhi::BufferUsageFlags::TransferSource))
        usage |= wgpu::BufferUsage::CopySrc;
    // Device::WriteBuffer is part of the backend-neutral contract, so every
    // WebGPU buffer must accept queue writes even when callers did not model
    // them as an explicit transfer operation.
    usage |= wgpu::BufferUsage::CopyDst;
    if (desc.memory == rhi::BufferMemory::Readback)
        usage |= wgpu::BufferUsage::MapRead | wgpu::BufferUsage::CopyDst;
    return usage;
}

wgpu::TextureUsage ToWebTextureUsage(rhi::TextureUsageFlags usage)
{
    wgpu::TextureUsage result = wgpu::TextureUsage::None;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::Sampled))
        result |= wgpu::TextureUsage::TextureBinding;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::Storage))
        result |= wgpu::TextureUsage::StorageBinding;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::ColorAttachment) ||
        rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::DepthStencilAttachment))
        result |= wgpu::TextureUsage::RenderAttachment;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::TransferSource))
        result |= wgpu::TextureUsage::CopySrc;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::TransferDestination))
        result |= wgpu::TextureUsage::CopyDst;
    return result;
}

wgpu::TextureDimension ToWebTextureDimension(rhi::TextureDimension dimension)
{
    switch (dimension) {
    case rhi::TextureDimension::Texture1D:
        return wgpu::TextureDimension::e1D;
    case rhi::TextureDimension::Texture2D:
        return wgpu::TextureDimension::e2D;
    case rhi::TextureDimension::Texture3D:
        return wgpu::TextureDimension::e3D;
    }
    return wgpu::TextureDimension::Undefined;
}

wgpu::TextureViewDimension ToWebViewDimension(rhi::TextureViewDimension dimension)
{
    switch (dimension) {
    case rhi::TextureViewDimension::Texture1D:
        return wgpu::TextureViewDimension::e1D;
    case rhi::TextureViewDimension::Texture1DArray:
        return wgpu::TextureViewDimension::Undefined;
    case rhi::TextureViewDimension::Texture2D:
        return wgpu::TextureViewDimension::e2D;
    case rhi::TextureViewDimension::Texture2DArray:
        return wgpu::TextureViewDimension::e2DArray;
    case rhi::TextureViewDimension::Texture3D:
        return wgpu::TextureViewDimension::e3D;
    case rhi::TextureViewDimension::Cube:
        return wgpu::TextureViewDimension::Cube;
    case rhi::TextureViewDimension::CubeArray:
        return wgpu::TextureViewDimension::CubeArray;
    }
    return wgpu::TextureViewDimension::Undefined;
}

wgpu::TextureAspect ToWebAspect(rhi::TextureAspect aspect)
{
    switch (aspect) {
    case rhi::TextureAspect::Color:
        return wgpu::TextureAspect::All;
    case rhi::TextureAspect::Depth:
        return wgpu::TextureAspect::DepthOnly;
    case rhi::TextureAspect::Stencil:
        return wgpu::TextureAspect::StencilOnly;
    case rhi::TextureAspect::DepthStencil:
        return wgpu::TextureAspect::All;
    }
    return wgpu::TextureAspect::Undefined;
}

wgpu::FilterMode ToWebFilter(rhi::FilterMode mode)
{
    return mode == rhi::FilterMode::Nearest ? wgpu::FilterMode::Nearest : wgpu::FilterMode::Linear;
}

wgpu::MipmapFilterMode ToWebMipmapFilter(rhi::FilterMode mode)
{
    return mode == rhi::FilterMode::Nearest ? wgpu::MipmapFilterMode::Nearest : wgpu::MipmapFilterMode::Linear;
}

wgpu::AddressMode ToWebAddress(rhi::AddressMode mode)
{
    switch (mode) {
    case rhi::AddressMode::Repeat:
        return wgpu::AddressMode::Repeat;
    case rhi::AddressMode::MirroredRepeat:
        return wgpu::AddressMode::MirrorRepeat;
    case rhi::AddressMode::ClampToEdge:
        return wgpu::AddressMode::ClampToEdge;
    }
    return wgpu::AddressMode::Undefined;
}

wgpu::PrimitiveTopology ToWebTopology(rhi::PrimitiveTopology topology)
{
    switch (topology) {
    case rhi::PrimitiveTopology::TriangleList:
        return wgpu::PrimitiveTopology::TriangleList;
    case rhi::PrimitiveTopology::TriangleStrip:
        return wgpu::PrimitiveTopology::TriangleStrip;
    case rhi::PrimitiveTopology::LineList:
        return wgpu::PrimitiveTopology::LineList;
    }
    return wgpu::PrimitiveTopology::Undefined;
}

wgpu::CullMode ToWebCull(rhi::CullMode mode)
{
    switch (mode) {
    case rhi::CullMode::None:
        return wgpu::CullMode::None;
    case rhi::CullMode::Front:
        return wgpu::CullMode::Front;
    case rhi::CullMode::Back:
        return wgpu::CullMode::Back;
    }
    return wgpu::CullMode::Undefined;
}

wgpu::FrontFace ToWebFrontFace(rhi::FrontFace face)
{
    return face == rhi::FrontFace::Clockwise ? wgpu::FrontFace::CW : wgpu::FrontFace::CCW;
}

wgpu::CompareFunction ToWebCompare(rhi::CompareFunction compare)
{
    switch (compare) {
    case rhi::CompareFunction::Never:
        return wgpu::CompareFunction::Never;
    case rhi::CompareFunction::Less:
        return wgpu::CompareFunction::Less;
    case rhi::CompareFunction::Equal:
        return wgpu::CompareFunction::Equal;
    case rhi::CompareFunction::LessEqual:
        return wgpu::CompareFunction::LessEqual;
    case rhi::CompareFunction::Greater:
        return wgpu::CompareFunction::Greater;
    case rhi::CompareFunction::NotEqual:
        return wgpu::CompareFunction::NotEqual;
    case rhi::CompareFunction::GreaterEqual:
        return wgpu::CompareFunction::GreaterEqual;
    case rhi::CompareFunction::Always:
        return wgpu::CompareFunction::Always;
    }
    return wgpu::CompareFunction::Undefined;
}

uint64_t AlignedUniformSize(uint32_t byteSize)
{
    return std::max<uint64_t>(16, (static_cast<uint64_t>(byteSize) + 15u) & ~15ull);
}

} // namespace

const rhi::GraphicsCommandEncoder::Dispatch WebGpuRhiDevice::s_graphicsDispatch = {
    &WebGpuRhiDevice::BindPipeline, &WebGpuRhiDevice::BindGroup,    &WebGpuRhiDevice::PushConstants,
    &WebGpuRhiDevice::Draw,         &WebGpuRhiDevice::DrawIndirect,
};

const rhi::ComputeCommandEncoder::DispatchTable WebGpuRhiDevice::s_computeDispatch = {
    &WebGpuRhiDevice::BindComputePipeline,  &WebGpuRhiDevice::BindComputeGroup,
    &WebGpuRhiDevice::PushComputeConstants, &WebGpuRhiDevice::Dispatch,
    &WebGpuRhiDevice::DispatchIndirect,
};

const rhi::TransferCommandEncoder::DispatchTable WebGpuRhiDevice::s_transferDispatch = {
    &WebGpuRhiDevice::CopyBuffer,
    &WebGpuRhiDevice::CopyTexture,
    &WebGpuRhiDevice::ResolveTexture,
};

WebGpuRhiDevice::WebGpuRhiDevice(wgpu::Device device, wgpu::Queue queue)
    : m_deviceId(rhi::AllocateDeviceId()), m_device(std::move(device)), m_queue(std::move(queue))
{
    m_capabilities.backend = rhi::BackendType::WebGPU;
    m_capabilities.SetAdapterName("WebGPU browser adapter");
    m_capabilities.apiVersionMajor = 1;
    m_capabilities.limits.maxColorAttachments = 8;
    m_capabilities.limits.maxPushConstantBytes = 256;
    m_capabilities.limits.maxSampledTexturesPerStage = 16;
    m_capabilities.limits.maxStorageBuffersPerStage = 8;
    m_capabilities.limits.maxSamplerAnisotropy = 16.0f;
    m_capabilityState.dynamicRendering = {true, true};

    for (size_t index = 1; index < rhi::kPixelFormatCount; ++index) {
        const auto format = static_cast<rhi::PixelFormat>(index);
        // BC formats require the optional texture-compression-bc device
        // feature. The baseline Web Player does not request optional adapter
        // features yet, so cooked textures must remain portable/uncompressed.
        if (rhi::IsBlockCompressedFormat(format) || ToWebFormat(format) == wgpu::TextureFormat::Undefined)
            continue;
        auto &caps = m_capabilities.formats[index];
        caps.format = format;
        caps.optimalTiling =
            rhi::FormatFeature::Sampled | rhi::FormatFeature::TransferSource | rhi::FormatFeature::TransferDestination;
        caps.optimalTiling |= rhi::IsDepthFormat(format) ? rhi::FormatFeature::DepthStencilAttachment
                                                         : rhi::FormatFeature::ColorAttachment;
        // Advertise only the sample count the backend has validated for every
        // published format. Format-specific 4x MSAA can be enabled later from
        // an explicit WebGPU capability table.
        caps.sampleCounts = rhi::SampleCountBit(rhi::SampleCount::One);
    }
}

WebGpuRhiDevice::~WebGpuRhiDevice()
{
    if (m_lifetime) {
        std::unique_lock lock(m_lifetime->gate);
        m_lifetime->alive.store(false, std::memory_order_release);
    }
}

rhi::DeviceId WebGpuRhiDevice::GetDeviceId() const noexcept
{
    return m_deviceId;
}

const rhi::DeviceCaps &WebGpuRhiDevice::GetCapabilities() const noexcept
{
    return m_capabilities;
}

const rhi::DeviceCapabilityState &WebGpuRhiDevice::GetCapabilityState() const noexcept
{
    return m_capabilityState;
}

std::shared_ptr<rhi::DeviceLifetime> WebGpuRhiDevice::GetLifetime() const noexcept
{
    return m_lifetime;
}

template <typename HandleType, typename Payload>
HandleType WebGpuRhiDevice::Register(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, Payload payload)
{
    uint32_t index = 0;
    if (freeHead != UINT32_MAX) {
        index = freeHead;
        auto &slot = slots[index];
        freeHead = slot.nextFree;
        slot.nextFree = UINT32_MAX;
        slot.payload = std::move(payload);
        slot.occupied = true;
    } else {
        if (slots.size() >= std::numeric_limits<uint32_t>::max())
            return {};
        index = static_cast<uint32_t>(slots.size());
        slots.push_back({std::move(payload), 1, UINT32_MAX, true});
    }
    return {index, rhi::ComposeHandleGeneration(m_deviceId, slots[index].generation)};
}

template <typename HandleType, typename Payload>
void WebGpuRhiDevice::ReleaseSlot(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, HandleType handle) noexcept
{
    if (handle.Device() != m_deviceId || handle.index >= slots.size())
        return;
    auto &slot = slots[handle.index];
    if (!slot.occupied || slot.generation != handle.Version())
        return;
    slot.payload = {};
    slot.occupied = false;
    ++slot.generation;
    if (slot.generation == 0)
        slot.generation = 1;
    slot.nextFree = freeHead;
    freeHead = handle.index;
}

template <typename HandleType, typename Payload>
Payload *WebGpuRhiDevice::Resolve(std::vector<Slot<Payload>> &slots, HandleType handle) noexcept
{
    if (handle.Device() != m_deviceId || handle.index >= slots.size())
        return nullptr;
    auto &slot = slots[handle.index];
    return slot.occupied && slot.generation == handle.Version() ? &slot.payload : nullptr;
}

template <typename HandleType, typename Payload>
const Payload *WebGpuRhiDevice::Resolve(const std::vector<Slot<Payload>> &slots, HandleType handle) const noexcept
{
    if (handle.Device() != m_deviceId || handle.index >= slots.size())
        return nullptr;
    const auto &slot = slots[handle.index];
    return slot.occupied && slot.generation == handle.Version() ? &slot.payload : nullptr;
}

rhi::BufferHandle WebGpuRhiDevice::CreateBuffer(const rhi::BufferDesc &desc)
{
    if (desc.byteSize == 0 || desc.initialDataBytes > desc.byteSize ||
        (desc.initialDataBytes > 0 && desc.initialData == nullptr)) {
        SetError("invalid WebGPU buffer descriptor");
        return {};
    }
    wgpu::BufferDescriptor native;
    native.size = desc.byteSize;
    native.usage = ToWebBufferUsage(desc);
    if (native.usage == wgpu::BufferUsage::None) {
        SetError("WebGPU buffer has no usable flags");
        return {};
    }
    auto buffer = m_device.CreateBuffer(&native);
    if (!buffer) {
        SetError("WebGPU failed to create buffer");
        return {};
    }
    if (desc.initialDataBytes > 0)
        m_queue.WriteBuffer(buffer, 0, desc.initialData, static_cast<size_t>(desc.initialDataBytes));
    return Register<rhi::BufferHandle>(m_buffers, m_freeBuffer, {std::move(buffer), desc.byteSize});
}

rhi::TextureHandle WebGpuRhiDevice::CreateTexture(const rhi::TextureDesc &desc)
{
    const auto format = ToWebFormat(desc.format);
    if (format == wgpu::TextureFormat::Undefined || desc.width == 0 || desc.height == 0 || desc.depthOrLayers == 0 ||
        desc.mipLevels == 0 || !m_capabilities.CheckSampleCount(desc.format, desc.samples).IsSupported()) {
        SetError("invalid or unsupported WebGPU texture descriptor");
        return {};
    }
    wgpu::TextureDescriptor native;
    native.dimension = ToWebTextureDimension(desc.dimension);
    native.size = {desc.width, desc.height, desc.depthOrLayers};
    native.format = format;
    native.mipLevelCount = desc.mipLevels;
    native.sampleCount = static_cast<uint32_t>(desc.samples);
    native.usage = ToWebTextureUsage(desc.usage);
    std::array<wgpu::TextureFormat, 1> viewFormats{};
    if (desc.mutableFormat) {
        rhi::PixelFormat alternate = rhi::PixelFormat::Undefined;
        if (rhi::IsSrgbFormat(desc.format))
            alternate = rhi::LinearColorFormat(desc.format);
        else if (desc.format == rhi::PixelFormat::RGBA8UNorm)
            alternate = rhi::PixelFormat::RGBA8Srgb;
        else if (desc.format == rhi::PixelFormat::BGRA8UNorm)
            alternate = rhi::PixelFormat::BGRA8Srgb;
        viewFormats[0] = ToWebFormat(alternate);
        if (viewFormats[0] != wgpu::TextureFormat::Undefined && viewFormats[0] != format) {
            native.viewFormatCount = 1;
            native.viewFormats = viewFormats.data();
        }
    }
    auto texture = m_device.CreateTexture(&native);
    if (!texture) {
        SetError("WebGPU failed to create texture");
        return {};
    }
    return Register<rhi::TextureHandle>(m_textures, m_freeTexture, {std::move(texture), desc});
}

rhi::TextureViewHandle WebGpuRhiDevice::CreateTextureView(const rhi::TextureViewDesc &desc)
{
    const auto *texture = Resolve(m_textures, desc.texture);
    if (!texture) {
        SetError("invalid WebGPU texture handle for view");
        return {};
    }
    wgpu::TextureViewDescriptor native;
    native.dimension = ToWebViewDimension(desc.dimension);
    if (native.dimension == wgpu::TextureViewDimension::Undefined) {
        SetError("WebGPU does not support this texture view dimension");
        return {};
    }
    native.format = ToWebFormat(desc.format == rhi::PixelFormat::Undefined ? texture->desc.format : desc.format);
    native.aspect = ToWebAspect(desc.aspect);
    native.baseMipLevel = desc.baseMip;
    native.mipLevelCount = desc.mipCount;
    native.baseArrayLayer = desc.baseLayer;
    native.arrayLayerCount = desc.layerCount;
    auto view = texture->texture.CreateView(&native);
    if (!view) {
        SetError("WebGPU failed to create texture view");
        return {};
    }
    return Register<rhi::TextureViewHandle>(m_textureViews, m_freeTextureView, {std::move(view)});
}

rhi::SamplerHandle WebGpuRhiDevice::CreateSampler(const rhi::SamplerDesc &desc)
{
    wgpu::SamplerDescriptor native;
    native.minFilter = ToWebFilter(desc.minFilter);
    native.magFilter = ToWebFilter(desc.magFilter);
    native.mipmapFilter = ToWebMipmapFilter(desc.mipFilter);
    native.addressModeU = ToWebAddress(desc.addressU);
    native.addressModeV = ToWebAddress(desc.addressV);
    native.addressModeW = ToWebAddress(desc.addressW);
    native.lodMinClamp = desc.minLod;
    native.lodMaxClamp = std::max(desc.minLod, desc.maxLod);
    native.maxAnisotropy = static_cast<uint16_t>(std::clamp(std::lround(desc.maxAnisotropy), 1l, 16l));
    auto sampler = m_device.CreateSampler(&native);
    if (!sampler) {
        SetError("WebGPU failed to create sampler");
        return {};
    }
    return Register<rhi::SamplerHandle>(m_samplers, m_freeSampler, {std::move(sampler)});
}

rhi::ShaderModuleHandle WebGpuRhiDevice::CreateShaderModule(const rhi::ShaderModuleDesc &desc)
{
    if (desc.language != rhi::ShaderSourceLanguage::Wgsl || desc.code == nullptr || desc.byteSize == 0) {
        SetError("WebGPU shader modules require cooked WGSL");
        return {};
    }
    std::string normalizedSource(static_cast<const char *>(desc.code), desc.byteSize);
    // The RHI StorageBuffer contract permits read and write access. Vulkan can
    // bind that wider contract to a shader that only reads, while WebGPU
    // requires the shader access mode and BindGroupLayout type to match
    // exactly. Widen read-only WGSL declarations without changing their use.
    constexpr std::string_view readOnlyStorage = "var<storage, read>";
    constexpr std::string_view readWriteStorage = "var<storage, read_write>";
    size_t storageCursor = 0;
    while ((storageCursor = normalizedSource.find(readOnlyStorage, storageCursor)) != std::string::npos) {
        normalizedSource.replace(storageCursor, readOnlyStorage.size(), readWriteStorage);
        storageCursor += readWriteStorage.size();
    }
    wgpu::ShaderSourceWGSL source;
    source.code = wgpu::StringView(normalizedSource.data(), normalizedSource.size());
    wgpu::ShaderModuleDescriptor native;
    native.nextInChain = &source;
    auto module = m_device.CreateShaderModule(&native);
    if (!module) {
        SetError("WebGPU failed to create WGSL shader module");
        return {};
    }
    return Register<rhi::ShaderModuleHandle>(m_shaderModules, m_freeShaderModule,
                                             {std::move(module), std::move(normalizedSource)});
}

wgpu::BindGroupLayout WebGpuRhiDevice::CreateNativeBindingLayout(const rhi::BindingLayoutDesc &desc,
                                                                 bool includePushConstant,
                                                                 const std::vector<uint32_t> *usedBindings)
{
    std::vector<wgpu::BindGroupLayoutEntry> entries;
    entries.reserve(desc.entryCount * 2 + (includePushConstant ? 1 : 0));
    for (uint32_t index = 0; index < desc.entryCount; ++index) {
        const auto &source = desc.entries[index];
        if (!UsesBinding(usedBindings, source.binding) &&
            !(source.type == rhi::BindingType::CombinedTextureSampler &&
              UsesBinding(usedBindings, WebSamplerBinding(source.binding))))
            continue;
        if (source.count != 1 || source.binding >= WebSamplerBindingBase) {
            SetError("WebGPU binding arrays and reserved bindings are not supported by this ABI");
            return {};
        }
        wgpu::BindGroupLayoutEntry entry;
        entry.binding = source.binding;
        entry.visibility = ToWebShaderStage(source.visibility);
        switch (source.type) {
        case rhi::BindingType::UniformBuffer:
            entry.buffer.type = wgpu::BufferBindingType::Uniform;
            break;
        case rhi::BindingType::StorageBuffer:
            entry.buffer.type = wgpu::BufferBindingType::Storage;
            break;
        case rhi::BindingType::SampledTexture:
            entry.texture.sampleType =
                source.depthRead ? wgpu::TextureSampleType::Depth : wgpu::TextureSampleType::Float;
            entry.texture.viewDimension = wgpu::TextureViewDimension::e2D;
            break;
        case rhi::BindingType::Sampler:
            entry.sampler.type = wgpu::SamplerBindingType::Filtering;
            break;
        case rhi::BindingType::CombinedTextureSampler: {
            entry.texture.sampleType =
                source.depthRead ? wgpu::TextureSampleType::Depth : wgpu::TextureSampleType::Float;
            entry.texture.viewDimension = wgpu::TextureViewDimension::e2D;
            entries.push_back(entry);
            wgpu::BindGroupLayoutEntry sampler;
            sampler.binding = WebSamplerBinding(source.binding);
            sampler.visibility = entry.visibility;
            sampler.sampler.type =
                source.depthRead ? wgpu::SamplerBindingType::NonFiltering : wgpu::SamplerBindingType::Filtering;
            entries.push_back(sampler);
            continue;
        }
        case rhi::BindingType::StorageTexture:
            SetError("RHI storage texture layouts do not yet carry the WebGPU format and access contract");
            return {};
        }
        entries.push_back(entry);
    }
    if (includePushConstant) {
        wgpu::BindGroupLayoutEntry push;
        push.binding = WebPushConstantBinding;
        push.visibility = wgpu::ShaderStage::Vertex | wgpu::ShaderStage::Fragment | wgpu::ShaderStage::Compute;
        push.buffer.type = wgpu::BufferBindingType::Uniform;
        entries.push_back(push);
    }
    wgpu::BindGroupLayoutDescriptor native;
    native.entryCount = entries.size();
    native.entries = entries.data();
    return m_device.CreateBindGroupLayout(&native);
}

rhi::BindingLayoutHandle WebGpuRhiDevice::CreateBindingLayout(const rhi::BindingLayoutDesc &desc)
{
    if (desc.entryCount > desc.entries.size()) {
        SetError("WebGPU binding layout has too many entries");
        return {};
    }
    // Compute shaders use stage-specific layouts. Creating the Vulkan-sized
    // superset here would exceed WebGPU limits before unused resources can be
    // removed, so native layouts are materialized with each pipeline instead.
    return Register<rhi::BindingLayoutHandle>(m_bindingLayouts, m_freeBindingLayout, {{}, desc});
}

wgpu::BindGroup WebGpuRhiDevice::CreateNativeBindGroup(const rhi::BindGroupDesc *desc, wgpu::BindGroupLayout layout,
                                                       wgpu::Buffer pushConstantBuffer,
                                                       const std::vector<uint32_t> *usedBindings)
{
    std::vector<wgpu::BindGroupEntry> entries;
    if (desc) {
        entries.reserve(desc->bufferCount + desc->textureCount * 2 + (pushConstantBuffer ? 1 : 0));
        for (uint32_t index = 0; index < desc->bufferCount; ++index) {
            const auto &source = desc->buffers[index];
            if (!UsesBinding(usedBindings, source.binding))
                continue;
            const auto *buffer = Resolve(m_buffers, source.buffer);
            if (!buffer) {
                SetError("invalid WebGPU buffer binding");
                return {};
            }
            wgpu::BindGroupEntry entry;
            entry.binding = source.binding;
            entry.buffer = buffer->buffer;
            entry.offset = source.offset;
            entry.size = source.byteSize == 0 ? wgpu::kWholeSize : source.byteSize;
            entries.push_back(entry);
        }
        for (uint32_t index = 0; index < desc->textureCount; ++index) {
            const auto &source = desc->textures[index];
            if (!UsesBinding(usedBindings, source.binding) &&
                !(source.type == rhi::BindingType::CombinedTextureSampler &&
                  UsesBinding(usedBindings, WebSamplerBinding(source.binding))))
                continue;
            if (source.type != rhi::BindingType::Sampler) {
                const auto *view = Resolve(m_textureViews, source.texture);
                if (!view) {
                    SetError("invalid WebGPU texture binding");
                    return {};
                }
                wgpu::BindGroupEntry texture;
                texture.binding = source.binding;
                texture.textureView = view->view;
                entries.push_back(texture);
            }
            if (source.type == rhi::BindingType::CombinedTextureSampler || source.type == rhi::BindingType::Sampler) {
                const auto *sampler = Resolve(m_samplers, source.sampler);
                if (!sampler) {
                    SetError("invalid WebGPU sampler binding");
                    return {};
                }
                wgpu::BindGroupEntry samplerEntry;
                samplerEntry.binding = source.type == rhi::BindingType::CombinedTextureSampler
                                           ? WebSamplerBinding(source.binding)
                                           : source.binding;
                samplerEntry.sampler = sampler->sampler;
                entries.push_back(samplerEntry);
            }
        }
    }
    if (pushConstantBuffer) {
        wgpu::BindGroupEntry push;
        push.binding = WebPushConstantBinding;
        push.buffer = pushConstantBuffer;
        push.offset = 0;
        push.size = wgpu::kWholeSize;
        entries.push_back(push);
    }
    wgpu::BindGroupDescriptor native;
    native.layout = layout;
    native.entryCount = entries.size();
    native.entries = entries.data();
    return m_device.CreateBindGroup(&native);
}

rhi::BindGroupHandle WebGpuRhiDevice::CreateBindGroup(const rhi::BindGroupDesc &desc)
{
    if (desc.bufferCount > desc.buffers.size() || desc.textureCount > desc.textures.size()) {
        SetError("WebGPU bind group has too many entries");
        return {};
    }
    const auto *layout = Resolve(m_bindingLayouts, desc.layout);
    if (!layout) {
        SetError("invalid WebGPU binding layout handle");
        return {};
    }
    return Register<rhi::BindGroupHandle>(m_bindGroups, m_freeBindGroup, {{}, desc});
}

rhi::GraphicsPipelineHandle WebGpuRhiDevice::CreateGraphicsPipeline(const rhi::GraphicsPipelineDesc &desc)
{
    const auto *vertex = Resolve(m_shaderModules, desc.vertexShader);
    const auto *fragment = Resolve(m_shaderModules, desc.fragmentShader);
    if (!vertex || !fragment || !desc.useDynamicRendering || !desc.HasValidRenderingContract() ||
        desc.bindingLayoutCount > desc.bindingLayouts.size() || desc.pushConstantBytes > 256) {
        SetError("invalid WebGPU graphics pipeline contract");
        return {};
    }

    GraphicsPipelinePayload payload;
    payload.layoutCount = desc.bindingLayoutCount;
    payload.pushConstantBytes = desc.pushConstantBytes;
    for (uint32_t index = 0; index < desc.bindingLayoutCount; ++index) {
        const auto *layout = Resolve(m_bindingLayouts, desc.bindingLayouts[index]);
        if (!layout) {
            SetError("invalid WebGPU graphics binding layout");
            return {};
        }
        payload.layouts[index] =
            CreateNativeBindingLayout(layout->desc, index == WebPushConstantGroup && desc.pushConstantBytes > 0);
        if (!payload.layouts[index])
            return {};
    }
    if (desc.pushConstantBytes > 0 && payload.layoutCount == 0) {
        rhi::BindingLayoutDesc empty;
        payload.layouts[0] = CreateNativeBindingLayout(empty, true);
        payload.layoutCount = 1;
    }

    wgpu::PipelineLayoutDescriptor layoutDesc;
    layoutDesc.bindGroupLayoutCount = payload.layoutCount;
    layoutDesc.bindGroupLayouts = payload.layouts.data();
    auto pipelineLayout = m_device.CreatePipelineLayout(&layoutDesc);

    std::array<wgpu::BlendState, rhi::GraphicsPipelineDesc::MaxColorTargets> blends{};
    std::array<wgpu::ColorTargetState, rhi::GraphicsPipelineDesc::MaxColorTargets> targets{};
    for (uint32_t index = 0; index < desc.colorTargetCount; ++index) {
        const auto &source = desc.colorTargets[index];
        targets[index].format = ToWebFormat(source.format);
        targets[index].writeMask = static_cast<wgpu::ColorWriteMask>(source.writeMask);
        if (source.blendEnabled) {
            auto &blend = blends[index];
            blend.color.operation = wgpu::BlendOperation::Add;
            blend.color.srcFactor = source.premultipliedAlpha ? wgpu::BlendFactor::One : wgpu::BlendFactor::SrcAlpha;
            blend.color.dstFactor = wgpu::BlendFactor::OneMinusSrcAlpha;
            blend.alpha.operation = wgpu::BlendOperation::Add;
            blend.alpha.srcFactor = wgpu::BlendFactor::One;
            blend.alpha.dstFactor = wgpu::BlendFactor::OneMinusSrcAlpha;
            targets[index].blend = &blend;
        }
    }

    wgpu::FragmentState fragmentState;
    fragmentState.module = fragment->module;
    fragmentState.entryPoint = "main";
    fragmentState.targetCount = desc.colorTargetCount;
    fragmentState.targets = targets.data();

    wgpu::DepthStencilState depthState;
    depthState.format = ToWebFormat(desc.renderingSignature.depthFormat);
    depthState.depthWriteEnabled = desc.depth.writeEnabled ? wgpu::OptionalBool::True : wgpu::OptionalBool::False;
    depthState.depthCompare = desc.depth.testEnabled ? ToWebCompare(desc.depth.compare) : wgpu::CompareFunction::Always;

    wgpu::RenderPipelineDescriptor native;
    native.layout = pipelineLayout;
    native.vertex.module = vertex->module;
    native.vertex.entryPoint = "main";
    native.fragment = &fragmentState;
    native.primitive.topology = ToWebTopology(desc.topology);
    native.primitive.frontFace = ToWebFrontFace(desc.raster.frontFace);
    native.primitive.cullMode = ToWebCull(desc.raster.cullMode);
    native.multisample.count = static_cast<uint32_t>(desc.samples);
    if (desc.renderingSignature.depthFormat != rhi::PixelFormat::Undefined)
        native.depthStencil = &depthState;
    payload.pipeline = m_device.CreateRenderPipeline(&native);
    if (!payload.pipeline) {
        SetError("WebGPU failed to create graphics pipeline");
        return {};
    }
    return Register<rhi::GraphicsPipelineHandle>(m_graphicsPipelines, m_freeGraphicsPipeline, std::move(payload));
}

rhi::ComputePipelineHandle WebGpuRhiDevice::CreateComputePipeline(const rhi::ComputePipelineDesc &desc)
{
    const auto *shader = Resolve(m_shaderModules, desc.computeShader);
    if (!shader || desc.bindingLayoutCount > desc.bindingLayouts.size() || desc.pushConstantBytes > 256) {
        SetError("invalid WebGPU compute pipeline contract");
        return {};
    }
    ComputePipelinePayload payload;
    payload.layoutCount = desc.bindingLayoutCount;
    payload.pushConstantBytes = desc.pushConstantBytes;
    payload.usedBindings = ReflectUsedComputeBindings(shader->source);
    for (uint32_t index = 0; index < desc.bindingLayoutCount; ++index) {
        const auto *layout = Resolve(m_bindingLayouts, desc.bindingLayouts[index]);
        if (!layout) {
            SetError("invalid WebGPU compute binding layout");
            return {};
        }
        payload.layouts[index] = CreateNativeBindingLayout(
            layout->desc, index == WebPushConstantGroup && desc.pushConstantBytes > 0, &payload.usedBindings[index]);
    }
    if (desc.pushConstantBytes > 0 && payload.layoutCount == 0) {
        rhi::BindingLayoutDesc empty;
        payload.layouts[0] = CreateNativeBindingLayout(empty, true);
        payload.layoutCount = 1;
    }
    wgpu::PipelineLayoutDescriptor layoutDesc;
    layoutDesc.bindGroupLayoutCount = payload.layoutCount;
    layoutDesc.bindGroupLayouts = payload.layouts.data();
    wgpu::ComputePipelineDescriptor native;
    native.layout = m_device.CreatePipelineLayout(&layoutDesc);
    native.compute.module = shader->module;
    native.compute.entryPoint = "main";
    payload.pipeline = m_device.CreateComputePipeline(&native);
    if (!payload.pipeline) {
        SetError("WebGPU failed to create compute pipeline");
        return {};
    }
    return Register<rhi::ComputePipelineHandle>(m_computePipelines, m_freeComputePipeline, std::move(payload));
}

bool WebGpuRhiDevice::WriteBuffer(rhi::BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize)
{
    const auto *buffer = Resolve(m_buffers, handle);
    if (!buffer || !data || byteSize == 0 || offset > buffer->byteSize || byteSize > buffer->byteSize - offset) {
        SetError("invalid WebGPU buffer write");
        return false;
    }
    m_queue.WriteBuffer(buffer->buffer, offset, data, static_cast<size_t>(byteSize));
    return true;
}

bool WebGpuRhiDevice::ReadBuffer(rhi::BufferHandle, uint64_t, void *, uint64_t)
{
    SetError("WebGPU buffer readback is asynchronous and is not exposed by the synchronous RHI contract");
    return false;
}

#define INFERNUX_WEB_RELEASE(Type, member, freeMember)                                                                 \
    void WebGpuRhiDevice::Release(rhi::Type##Handle handle) noexcept                                                   \
    {                                                                                                                  \
        ReleaseSlot(member, freeMember, handle);                                                                       \
    }

INFERNUX_WEB_RELEASE(Buffer, m_buffers, m_freeBuffer)
INFERNUX_WEB_RELEASE(Texture, m_textures, m_freeTexture)
INFERNUX_WEB_RELEASE(TextureView, m_textureViews, m_freeTextureView)
INFERNUX_WEB_RELEASE(Sampler, m_samplers, m_freeSampler)
INFERNUX_WEB_RELEASE(ShaderModule, m_shaderModules, m_freeShaderModule)
INFERNUX_WEB_RELEASE(BindingLayout, m_bindingLayouts, m_freeBindingLayout)
INFERNUX_WEB_RELEASE(BindGroup, m_bindGroups, m_freeBindGroup)
INFERNUX_WEB_RELEASE(GraphicsPipeline, m_graphicsPipelines, m_freeGraphicsPipeline)
INFERNUX_WEB_RELEASE(ComputePipeline, m_computePipelines, m_freeComputePipeline)

#undef INFERNUX_WEB_RELEASE

rhi::GraphicsCommandEncoder WebGpuRhiDevice::MakeGraphicsCommandEncoder(WebGpuGraphicsCommandContext &context,
                                                                        wgpu::RenderPassEncoder pass) noexcept
{
    context = {};
    context.device = this;
    context.pass = std::move(pass);
    return {&context, &s_graphicsDispatch};
}

rhi::ComputeCommandEncoder WebGpuRhiDevice::MakeComputeCommandEncoder(WebGpuComputeCommandContext &context,
                                                                      wgpu::ComputePassEncoder pass) noexcept
{
    context = {};
    context.device = this;
    context.pass = std::move(pass);
    return {&context, &s_computeDispatch};
}

rhi::TransferCommandEncoder WebGpuRhiDevice::MakeTransferCommandEncoder(WebGpuTransferCommandContext &context,
                                                                        wgpu::CommandEncoder encoder) noexcept
{
    context = {};
    context.device = this;
    context.encoder = std::move(encoder);
    return {&context, &s_transferDispatch};
}

wgpu::Buffer WebGpuRhiDevice::GetNativeBuffer(rhi::BufferHandle handle) const noexcept
{
    const auto *payload = Resolve(m_buffers, handle);
    return payload ? payload->buffer : wgpu::Buffer{};
}

wgpu::Buffer WebGpuRhiDevice::CreatePushConstantBuffer(uint32_t byteSize, const void *data)
{
    wgpu::BufferDescriptor desc;
    desc.size = AlignedUniformSize(byteSize);
    desc.usage = wgpu::BufferUsage::Uniform | wgpu::BufferUsage::CopyDst;
    auto buffer = m_device.CreateBuffer(&desc);
    if (buffer && data && byteSize > 0)
        m_queue.WriteBuffer(buffer, 0, data, byteSize);
    return buffer;
}

void WebGpuRhiDevice::BindGraphicsGroup(WebGpuGraphicsCommandContext &context, uint32_t setIndex)
{
    const auto *pipeline = Resolve(m_graphicsPipelines, context.pipeline);
    if (!pipeline || setIndex >= pipeline->layoutCount)
        return;
    const auto handle = context.groups[setIndex];
    const auto *group = Resolve(m_bindGroups, handle);
    if (setIndex == WebPushConstantGroup && pipeline->pushConstantBytes > 0) {
        if (!context.pushConstantBuffer)
            return;
        auto native = CreateNativeBindGroup(group ? &group->desc : nullptr, pipeline->layouts[setIndex],
                                            context.pushConstantBuffer);
        if (native)
            context.pass.SetBindGroup(setIndex, native);
    } else if (group) {
        auto native = CreateNativeBindGroup(&group->desc, pipeline->layouts[setIndex], {});
        if (native)
            context.pass.SetBindGroup(setIndex, native);
    }
}

void WebGpuRhiDevice::BindComputeGroup(WebGpuComputeCommandContext &context, uint32_t setIndex)
{
    const auto *pipeline = Resolve(m_computePipelines, context.pipeline);
    if (!pipeline || setIndex >= pipeline->layoutCount)
        return;
    const auto *group = Resolve(m_bindGroups, context.groups[setIndex]);
    if (setIndex == WebPushConstantGroup && pipeline->pushConstantBytes > 0) {
        if (!context.pushConstantBuffer)
            return;
        auto native = CreateNativeBindGroup(group ? &group->desc : nullptr, pipeline->layouts[setIndex],
                                            context.pushConstantBuffer, &pipeline->usedBindings[setIndex]);
        if (native)
            context.pass.SetBindGroup(setIndex, native);
    } else if (group) {
        auto native =
            CreateNativeBindGroup(&group->desc, pipeline->layouts[setIndex], {}, &pipeline->usedBindings[setIndex]);
        if (native)
            context.pass.SetBindGroup(setIndex, native);
    }
}

void WebGpuRhiDevice::BindPipeline(void *raw, rhi::GraphicsPipelineHandle pipeline)
{
    auto &context = *static_cast<WebGpuGraphicsCommandContext *>(raw);
    const auto *payload = context.device->Resolve(context.device->m_graphicsPipelines, pipeline);
    if (!payload)
        return;
    context.pipeline = pipeline;
    context.pass.SetPipeline(payload->pipeline);
    for (uint32_t index = 0; index < payload->layoutCount; ++index)
        context.device->BindGraphicsGroup(context, index);
}

void WebGpuRhiDevice::BindGroup(void *raw, rhi::GraphicsPipelineHandle pipeline, uint32_t setIndex,
                                rhi::BindGroupHandle group)
{
    auto &context = *static_cast<WebGpuGraphicsCommandContext *>(raw);
    if (pipeline != context.pipeline || setIndex >= context.groups.size())
        return;
    context.groups[setIndex] = group;
    context.device->BindGraphicsGroup(context, setIndex);
}

void WebGpuRhiDevice::PushConstants(void *raw, rhi::GraphicsPipelineHandle pipeline, rhi::ShaderStage,
                                    uint32_t byteSize, const void *data)
{
    auto &context = *static_cast<WebGpuGraphicsCommandContext *>(raw);
    const auto *payload = context.device->Resolve(context.device->m_graphicsPipelines, pipeline);
    if (!payload || pipeline != context.pipeline || byteSize > payload->pushConstantBytes)
        return;
    context.pushConstantBuffer = context.device->CreatePushConstantBuffer(byteSize, data);
    context.pushConstantBytes = byteSize;
    context.device->BindGraphicsGroup(context, WebPushConstantGroup);
}

void WebGpuRhiDevice::Draw(void *raw, uint32_t vertexCount, uint32_t instanceCount, uint32_t firstVertex,
                           uint32_t firstInstance)
{
    static_cast<WebGpuGraphicsCommandContext *>(raw)->pass.Draw(vertexCount, instanceCount, firstVertex, firstInstance);
}

void WebGpuRhiDevice::DrawIndirect(void *raw, rhi::BufferHandle arguments, uint64_t offset, uint32_t drawCount,
                                   uint32_t stride)
{
    auto &context = *static_cast<WebGpuGraphicsCommandContext *>(raw);
    const auto *buffer = context.device->Resolve(context.device->m_buffers, arguments);
    if (!buffer)
        return;
    for (uint32_t index = 0; index < drawCount; ++index)
        context.pass.DrawIndirect(buffer->buffer, offset + static_cast<uint64_t>(index) * stride);
}

void WebGpuRhiDevice::BindComputePipeline(void *raw, rhi::ComputePipelineHandle pipeline)
{
    auto &context = *static_cast<WebGpuComputeCommandContext *>(raw);
    const auto *payload = context.device->Resolve(context.device->m_computePipelines, pipeline);
    if (!payload)
        return;
    context.pipeline = pipeline;
    context.pass.SetPipeline(payload->pipeline);
    for (uint32_t index = 0; index < payload->layoutCount; ++index)
        context.device->BindComputeGroup(context, index);
}

void WebGpuRhiDevice::BindComputeGroup(void *raw, rhi::ComputePipelineHandle pipeline, uint32_t setIndex,
                                       rhi::BindGroupHandle group)
{
    auto &context = *static_cast<WebGpuComputeCommandContext *>(raw);
    if (pipeline != context.pipeline || setIndex >= context.groups.size())
        return;
    context.groups[setIndex] = group;
    context.device->BindComputeGroup(context, setIndex);
}

void WebGpuRhiDevice::PushComputeConstants(void *raw, rhi::ComputePipelineHandle pipeline, uint32_t byteSize,
                                           const void *data)
{
    auto &context = *static_cast<WebGpuComputeCommandContext *>(raw);
    const auto *payload = context.device->Resolve(context.device->m_computePipelines, pipeline);
    if (!payload || pipeline != context.pipeline || byteSize > payload->pushConstantBytes)
        return;
    context.pushConstantBuffer = context.device->CreatePushConstantBuffer(byteSize, data);
    context.pushConstantBytes = byteSize;
    context.device->BindComputeGroup(context, WebPushConstantGroup);
}

void WebGpuRhiDevice::Dispatch(void *raw, uint32_t x, uint32_t y, uint32_t z)
{
    static_cast<WebGpuComputeCommandContext *>(raw)->pass.DispatchWorkgroups(x, y, z);
}

void WebGpuRhiDevice::DispatchIndirect(void *raw, rhi::BufferHandle arguments, uint64_t offset)
{
    auto &context = *static_cast<WebGpuComputeCommandContext *>(raw);
    const auto *buffer = context.device->Resolve(context.device->m_buffers, arguments);
    if (buffer)
        context.pass.DispatchWorkgroupsIndirect(buffer->buffer, offset);
}

void WebGpuRhiDevice::CopyBuffer(void *raw, rhi::BufferHandle source, rhi::BufferHandle destination,
                                 const rhi::BufferCopyRegion &region)
{
    auto &context = *static_cast<WebGpuTransferCommandContext *>(raw);
    const auto *src = context.device->Resolve(context.device->m_buffers, source);
    const auto *dst = context.device->Resolve(context.device->m_buffers, destination);
    if (src && dst)
        context.encoder.CopyBufferToBuffer(src->buffer, region.sourceOffset, dst->buffer, region.destinationOffset,
                                           region.byteSize);
}

void WebGpuRhiDevice::CopyTexture(void *raw, rhi::TextureHandle source, rhi::TextureHandle destination,
                                  const rhi::TextureCopyRegion &region)
{
    auto &context = *static_cast<WebGpuTransferCommandContext *>(raw);
    const auto *src = context.device->Resolve(context.device->m_textures, source);
    const auto *dst = context.device->Resolve(context.device->m_textures, destination);
    if (!src || !dst)
        return;
    wgpu::TexelCopyTextureInfo srcInfo;
    srcInfo.texture = src->texture;
    srcInfo.mipLevel = region.sourceMip;
    srcInfo.origin = {0, 0, region.sourceLayer};
    srcInfo.aspect = ToWebAspect(region.aspect);
    wgpu::TexelCopyTextureInfo dstInfo;
    dstInfo.texture = dst->texture;
    dstInfo.mipLevel = region.destinationMip;
    dstInfo.origin = {0, 0, region.destinationLayer};
    dstInfo.aspect = ToWebAspect(region.aspect);
    wgpu::Extent3D size{region.width, region.height, region.depth};
    context.encoder.CopyTextureToTexture(&srcInfo, &dstInfo, &size);
}

void WebGpuRhiDevice::ResolveTexture(void *raw, rhi::TextureHandle, rhi::TextureHandle,
                                     const rhi::TextureResolveRegion &)
{
    static_cast<WebGpuTransferCommandContext *>(raw)->device->SetError(
        "WebGPU multisample resolves must be declared on a render-pass attachment");
}

const std::string &WebGpuRhiDevice::LastError() const noexcept
{
    return m_lastError;
}

void WebGpuRhiDevice::ClearError() noexcept
{
    m_lastError.clear();
}

void WebGpuRhiDevice::SetError(std::string message)
{
    m_lastError = std::move(message);
}

} // namespace infernux::web
