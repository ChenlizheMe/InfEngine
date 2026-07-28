#include <function/renderer/rhi/RhiCapabilities.h>

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
    return 0;
}
