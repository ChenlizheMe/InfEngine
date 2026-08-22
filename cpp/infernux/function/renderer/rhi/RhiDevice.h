#pragma once

#include "RhiCapabilities.h"
#include "RhiDescriptors.h"
#include "RhiResourceIndex.h"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <shared_mutex>
#include <string_view>

namespace infernux::rhi
{

class TextureGpuView;

struct BindlessTextureTableBinding final
{
    BindingLayoutHandle layout;
    BindGroupHandle group;

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return layout.IsValid() && group.IsValid();
    }
};

enum class DeviceCapability : uint8_t
{
    DescriptorIndexing = 0,
    DynamicRendering,
    TimelineSemaphore,
    Synchronization2,
    Submit2,
};

enum class DeviceCapabilityDiagnosticCode : uint8_t
{
    None = 0,
    Unsupported,
    NotEnabled,
    IncompleteDescriptorIndexing,
};

struct DeviceCapabilityStatus final
{
    bool supported = false;
    bool enabled = false;

    [[nodiscard]] constexpr bool IsSupported() const noexcept
    {
        return supported;
    }

    [[nodiscard]] constexpr bool IsEnabled() const noexcept
    {
        return enabled;
    }
};

/// Descriptor-indexing is deliberately represented by the individual Vulkan
/// subfeatures. A single descriptorIndexing boolean is not sufficient to
/// prove that a bindless layout can be created.
struct BindlessCapabilityStatus final
{
    DeviceCapabilityStatus descriptorIndexing;
    DeviceCapabilityStatus runtimeDescriptorArray;
    DeviceCapabilityStatus shaderSampledImageArrayNonUniformIndexing;
    DeviceCapabilityStatus descriptorBindingPartiallyBound;
    DeviceCapabilityStatus descriptorBindingVariableDescriptorCount;
    DeviceCapabilityStatus descriptorBindingSampledImageUpdateAfterBind;
    DeviceCapabilityStatus descriptorBindingUniformBufferUpdateAfterBind;
    DeviceCapabilityStatus descriptorBindingStorageBufferUpdateAfterBind;
    DeviceCapabilityStatus descriptorBindingUpdateUnusedWhilePending;

    [[nodiscard]] constexpr bool IsSupported() const noexcept
    {
        return descriptorIndexing.supported && runtimeDescriptorArray.supported &&
               shaderSampledImageArrayNonUniformIndexing.supported && descriptorBindingPartiallyBound.supported &&
               descriptorBindingVariableDescriptorCount.supported &&
               descriptorBindingSampledImageUpdateAfterBind.supported;
    }

    [[nodiscard]] constexpr bool IsEnabled() const noexcept
    {
        return descriptorIndexing.enabled && runtimeDescriptorArray.enabled &&
               shaderSampledImageArrayNonUniformIndexing.enabled && descriptorBindingPartiallyBound.enabled &&
               descriptorBindingVariableDescriptorCount.enabled && descriptorBindingSampledImageUpdateAfterBind.enabled;
    }
};

struct DeviceCapabilityState final
{
    BindlessCapabilityStatus bindless;
    DeviceCapabilityStatus dynamicRendering;
    DeviceCapabilityStatus timelineSemaphore;
    DeviceCapabilityStatus synchronization2;
    DeviceCapabilityStatus submit2;

    [[nodiscard]] constexpr DeviceCapabilityStatus Get(DeviceCapability capability) const noexcept
    {
        switch (capability) {
        case DeviceCapability::DescriptorIndexing:
            return {bindless.IsSupported(), bindless.IsEnabled()};
        case DeviceCapability::DynamicRendering:
            return dynamicRendering;
        case DeviceCapability::TimelineSemaphore:
            return timelineSemaphore;
        case DeviceCapability::Synchronization2:
            return synchronization2;
        case DeviceCapability::Submit2:
            return submit2;
        }
        return {};
    }
};

/// Stable shader-ABI fingerprint for the capabilities that affect descriptor
/// set construction. It is intentionally small and backend-neutral: a shader
/// cache must not reuse a program built for a different enabled contract.
[[nodiscard]] constexpr uint64_t ComputeDeviceShaderContractKey(const DeviceCapabilityState &state) noexcept
{
    uint64_t key = 0x494e585348414445ull; // "INXSHADER"
    const bool bindless = state.bindless.IsEnabled();
    const bool dynamicRendering = state.dynamicRendering.IsEnabled();
    const bool synchronization2 = state.synchronization2.IsEnabled();
    const bool submit2 = state.submit2.IsEnabled();
    key |= static_cast<uint64_t>(bindless) << 0u;
    key |= static_cast<uint64_t>(dynamicRendering) << 1u;
    key |= static_cast<uint64_t>(synchronization2) << 2u;
    key |= static_cast<uint64_t>(submit2) << 3u;
    return key;
}

struct DeviceCapabilityRequest final
{
    bool descriptorIndexing = false;
    bool dynamicRendering = false;
    bool timelineSemaphore = false;
    bool synchronization2 = false;
    bool submit2 = false;
};

struct DeviceCapabilityCheck final
{
    DeviceCapabilityDiagnosticCode code = DeviceCapabilityDiagnosticCode::None;
    DeviceCapability capability = DeviceCapability::DescriptorIndexing;

    [[nodiscard]] constexpr bool IsSupported() const noexcept
    {
        return code == DeviceCapabilityDiagnosticCode::None;
    }

    [[nodiscard]] constexpr std::string_view Message() const noexcept
    {
        switch (code) {
        case DeviceCapabilityDiagnosticCode::None:
            return {};
        case DeviceCapabilityDiagnosticCode::Unsupported:
            return "requested Vulkan capability is not supported by the physical device";
        case DeviceCapabilityDiagnosticCode::NotEnabled:
            return "requested Vulkan capability is supported but was not enabled at device creation";
        case DeviceCapabilityDiagnosticCode::IncompleteDescriptorIndexing:
            return "descriptor indexing is incomplete for the requested bindless contract";
        }
        return "unknown Vulkan capability diagnostic";
    }
};

[[nodiscard]] constexpr DeviceCapabilityCheck CheckDeviceCapability(const DeviceCapabilityState &state,
                                                                    DeviceCapability capability) noexcept
{
    const auto status = state.Get(capability);
    if (!status.supported)
        return {DeviceCapabilityDiagnosticCode::Unsupported, capability};
    if (!status.enabled)
        return {DeviceCapabilityDiagnosticCode::NotEnabled, capability};
    return {};
}

[[nodiscard]] constexpr DeviceCapabilityCheck CheckDeviceCapabilities(const DeviceCapabilityState &state,
                                                                      const DeviceCapabilityRequest &request) noexcept
{
    if (request.descriptorIndexing) {
        if (!state.bindless.IsSupported())
            return {DeviceCapabilityDiagnosticCode::IncompleteDescriptorIndexing, DeviceCapability::DescriptorIndexing};
        if (!state.bindless.IsEnabled())
            return {DeviceCapabilityDiagnosticCode::NotEnabled, DeviceCapability::DescriptorIndexing};
    }
    const DeviceCapability requested[] = {DeviceCapability::DynamicRendering, DeviceCapability::TimelineSemaphore,
                                          DeviceCapability::Synchronization2, DeviceCapability::Submit2};
    const bool enabled[] = {request.dynamicRendering, request.timelineSemaphore, request.synchronization2,
                            request.submit2};
    for (size_t i = 0; i < 4; ++i) {
        if (enabled[i]) {
            const auto result = CheckDeviceCapability(state, requested[i]);
            if (!result.IsSupported())
                return result;
        }
    }
    return {};
}

/// Shared lifetime state for resources that can outlive their owning device
/// wrapper. Resources must not call a backend after this flag becomes false.
struct DeviceLifetime final
{
    // Resource releases take a shared lock while device Reset/destruction
    // takes the exclusive lock. The atomic remains useful for cheap validity
    // probes, but is not used as a release-vs-teardown synchronization point.
    mutable std::shared_mutex gate;
    std::atomic<bool> alive{true};
};

/// Backend-neutral resource creation surface. Runtime systems retain only
/// RHI handles; Vulkan/WebGPU ownership stays in the concrete adapter.
class Device
{
  public:
    virtual ~Device() = default;

    [[nodiscard]] virtual DeviceId GetDeviceId() const noexcept
    {
        return InvalidDeviceId;
    }
    [[nodiscard]] virtual const DeviceCaps &GetCapabilities() const noexcept
    {
        static const DeviceCaps empty{};
        return empty;
    }

    [[nodiscard]] virtual const DeviceCapabilityState &GetCapabilityState() const noexcept
    {
        static const DeviceCapabilityState empty{};
        return empty;
    }

    [[nodiscard]] virtual std::shared_ptr<DeviceLifetime> GetLifetime() const noexcept
    {
        return {};
    }

    /// Optional device-global sampled-texture table. Backends publish this
    /// capability once the table and its fallback descriptor are complete.
    /// Renderers consume only RHI handles and stable ResourceIndex values.
    [[nodiscard]] virtual BindlessTextureTableBinding GetBindlessTextureTableBinding() const noexcept
    {
        return {};
    }
    [[nodiscard]] virtual ResourceIndex
    PublishBindlessTexture(const std::shared_ptr<const TextureGpuView> &texture) noexcept
    {
        (void)texture;
        return {};
    }
    virtual void MarkBindlessTexturesUsed(const ResourceIndex *resources, size_t count) noexcept
    {
        (void)resources;
        (void)count;
    }

    [[nodiscard]] virtual BufferHandle CreateBuffer(const BufferDesc &desc) = 0;
    [[nodiscard]] virtual TextureHandle CreateTexture(const TextureDesc &desc) = 0;
    [[nodiscard]] virtual TextureViewHandle CreateTextureView(const TextureViewDesc &desc) = 0;
    [[nodiscard]] virtual SamplerHandle CreateSampler(const SamplerDesc &desc) = 0;
    [[nodiscard]] virtual ShaderModuleHandle CreateShaderModule(const ShaderModuleDesc &desc) = 0;
    [[nodiscard]] virtual BindingLayoutHandle CreateBindingLayout(const BindingLayoutDesc &desc) = 0;
    [[nodiscard]] virtual BindGroupHandle CreateBindGroup(const BindGroupDesc &desc) = 0;
    [[nodiscard]] virtual GraphicsPipelineHandle CreateGraphicsPipeline(const GraphicsPipelineDesc &desc) = 0;
    [[nodiscard]] virtual ComputePipelineHandle CreateComputePipeline(const ComputePipelineDesc &desc) = 0;

    virtual bool WriteBuffer(BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize) = 0;
    /// Copy bytes from a host-visible readback buffer after the submission
    /// that populated it has completed. Device-local and upload buffers are
    /// intentionally rejected by concrete backends.
    [[nodiscard]] virtual bool ReadBuffer(BufferHandle handle, uint64_t offset, void *data, uint64_t byteSize)
    {
        (void)handle;
        (void)offset;
        (void)data;
        (void)byteSize;
        return false;
    }

    virtual void Release(BufferHandle handle) noexcept = 0;
    virtual void Release(TextureHandle handle) noexcept = 0;
    virtual void Release(TextureViewHandle handle) noexcept = 0;
    virtual void Release(SamplerHandle handle) noexcept = 0;
    virtual void Release(ShaderModuleHandle handle) noexcept = 0;
    virtual void Release(BindingLayoutHandle handle) noexcept = 0;
    virtual void Release(BindGroupHandle handle) noexcept = 0;
    virtual void Release(GraphicsPipelineHandle handle) noexcept = 0;
    virtual void Release(ComputePipelineHandle handle) noexcept = 0;
};

} // namespace infernux::rhi
