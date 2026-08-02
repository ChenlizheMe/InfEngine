#include <function/renderer/rhi/RhiQuery.h>

#include <cassert>
#include <string>

using infernux::rhi::GpuTimestampFrame;
using infernux::rhi::GpuTimestampSample;
using infernux::rhi::TimestampRegionHandle;
using infernux::rhi::TimestampTickDelta;

int main()
{
    TimestampRegionHandle invalid;
    TimestampRegionHandle valid{3};
    assert(!invalid.IsValid());
    assert(valid.IsValid());

    assert(TimestampTickDelta(10, 25, 64) == 15);
    assert(TimestampTickDelta(250, 5, 8) == 11);
    assert(TimestampTickDelta(10, 25, 0) == 0);

    GpuTimestampSample sample;
    sample.SetName("SceneRenderGraph");
    sample.milliseconds = 1.25;
    assert(sample.Name() == "SceneRenderGraph");

    const std::string longName(100, 'x');
    sample.SetName(longName);
    assert(sample.Name().size() == sample.name.size() - 1);

    GpuTimestampFrame frame;
    frame.Reset(9);
    frame.samples[0].SetName("Frame");
    frame.samples[0].milliseconds = 2.5;
    frame.sampleCount = 1;
    frame.available = true;
    assert(frame.Find("Frame") != nullptr);
    assert(frame.Find("Frame")->milliseconds == 2.5);
    assert(frame.Find("Missing") == nullptr);

    frame.Reset(10);
    assert(frame.serial == 10);
    assert(frame.sampleCount == 0);
    assert(!frame.available);
    return 0;
}
