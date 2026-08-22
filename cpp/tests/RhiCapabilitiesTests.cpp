#include <function/renderer/rhi/RhiDevice.h>

#include <cassert>
#include <string>

using namespace infernux::rhi;

int main()
{
    DeviceCaps capabilities;
    capabilities.backend = BackendType::Vulkan;
    capabilities.adapterType = AdapterType::Discrete;
    capabilities.SetAdapterName("Test Adapter");
    assert(capabilities.AdapterName() == "Test Adapter");

    auto &rgba = capabilities.formats[static_cast<size_t>(PixelFormat::RGBA8UNorm)];
    rgba.format = PixelFormat::RGBA8UNorm;
    rgba.optimalTiling = FormatFeature::Sampled | FormatFeature::FilterLinear | FormatFeature::ColorAttachment;
    rgba.sampleCounts = SampleCountBit(SampleCount::One) | SampleCountBit(SampleCount::Four);

    assert(capabilities.CheckFormat(PixelFormat::RGBA8UNorm, FormatFeature::Sampled).IsSupported());
    const auto storageCheck = capabilities.CheckFormat(PixelFormat::RGBA8UNorm, FormatFeature::Storage);
    assert(!storageCheck.IsSupported());
    assert(storageCheck.code == CapabilityDiagnosticCode::UnsupportedFormatFeatures);

    assert(capabilities.CheckSampleCount(PixelFormat::RGBA8UNorm, SampleCount::Four).IsSupported());
    const auto sampleCheck = capabilities.CheckSampleCount(PixelFormat::RGBA8UNorm, SampleCount::Eight);
    assert(!sampleCheck.IsSupported());
    assert(sampleCheck.code == CapabilityDiagnosticCode::UnsupportedSampleCount);

    const auto invalidCheck = capabilities.CheckFormat(PixelFormat::Undefined, FormatFeature::Sampled);
    assert(invalidCheck.code == CapabilityDiagnosticCode::InvalidFormat);

    const std::string longName(DeviceCaps::AdapterNameCapacity * 2, 'x');
    capabilities.SetAdapterName(longName);
    assert(capabilities.AdapterName().size() == DeviceCaps::AdapterNameCapacity - 1);

    DeviceCapabilityState state;
    DeviceCapabilityRequest emptyRequest;
    assert(CheckDeviceCapabilities(state, emptyRequest).IsSupported());

    state.dynamicRendering.supported = true;
    const auto notEnabled = CheckDeviceCapability(state, DeviceCapability::DynamicRendering);
    assert(!notEnabled.IsSupported());
    assert(notEnabled.code == DeviceCapabilityDiagnosticCode::NotEnabled);

    state.dynamicRendering.enabled = true;
    assert(CheckDeviceCapability(state, DeviceCapability::DynamicRendering).IsSupported());

    DeviceCapabilityRequest bindlessRequest;
    bindlessRequest.descriptorIndexing = true;
    const auto incompleteBindless = CheckDeviceCapabilities(state, bindlessRequest);
    assert(!incompleteBindless.IsSupported());
    assert(incompleteBindless.code == DeviceCapabilityDiagnosticCode::IncompleteDescriptorIndexing);

    state.bindless.descriptorIndexing.supported = true;
    state.bindless.runtimeDescriptorArray.supported = true;
    state.bindless.shaderSampledImageArrayNonUniformIndexing.supported = true;
    state.bindless.descriptorBindingPartiallyBound.supported = true;
    state.bindless.descriptorBindingVariableDescriptorCount.supported = true;
    state.bindless.descriptorBindingSampledImageUpdateAfterBind.supported = true;
    assert(CheckDeviceCapabilities(state, bindlessRequest).code == DeviceCapabilityDiagnosticCode::NotEnabled);

    state.bindless.descriptorIndexing.enabled = true;
    state.bindless.runtimeDescriptorArray.enabled = true;
    state.bindless.shaderSampledImageArrayNonUniformIndexing.enabled = true;
    state.bindless.descriptorBindingPartiallyBound.enabled = true;
    state.bindless.descriptorBindingVariableDescriptorCount.enabled = true;
    state.bindless.descriptorBindingSampledImageUpdateAfterBind.enabled = true;
    assert(CheckDeviceCapabilities(state, bindlessRequest).IsSupported());

    DeviceCapabilityRequest syncRequest;
    syncRequest.synchronization2 = true;
    state.synchronization2 = {true, false};
    assert(CheckDeviceCapabilities(state, syncRequest).code == DeviceCapabilityDiagnosticCode::NotEnabled);
    state.synchronization2.enabled = true;
    assert(CheckDeviceCapabilities(state, syncRequest).IsSupported());

    return 0;
}
