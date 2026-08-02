#include <function/renderer/SceneRenderGraph.h>

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cmath>
#include <utility>

using namespace infernux;

namespace
{

RenderGraphDescription MakeShadowGraph(ShaderCompileTarget target)
{
    RenderGraphDescription graph;
    graph.textures.push_back({"ShadowDepth", rhi::PixelFormat::D32SFloat, false, true, 16, 16, 0, 1});

    GraphPassDesc pass;
    pass.name = "ShadowCasters";
    pass.commands.push_back({GraphCommandType::DrawShadowCasters});
    pass.commands.front().shaderTarget = target;
    pass.writeDepth = "ShadowDepth";
    graph.passes.push_back(std::move(pass));
    return graph;
}

RenderGraphDescription MakeMotionGraph(rhi::PixelFormat colorFormat, bool readableDepth)
{
    RenderGraphDescription graph;
    graph.textures.push_back({"Motion", colorFormat, false, false, 0, 0, 0, 1});
    graph.textures.push_back({"Depth", rhi::PixelFormat::D32SFloat, false, true, 0, 0, 0, 1});

    GraphPassDesc pass;
    pass.name = "Motion";
    pass.commands.push_back({GraphCommandType::DrawRenderers});
    pass.commands.front().shaderTarget = ShaderCompileTarget::Motion;
    pass.writeColors.push_back({0, "Motion"});
    if (readableDepth)
        pass.readTextures.push_back("Depth");
    graph.passes.push_back(std::move(pass));
    graph.outputTexture = "Motion";
    return graph;
}

RenderGraphDescription MakeTemporalGraph(bool completePair, bool singleSample = true)
{
    RenderGraphDescription graph;
    GraphTextureDesc read{"HistoryRead", rhi::PixelFormat::RGBA16SFloat, false, false, 0, 0, 0, singleSample ? 1u : 4u};
    read.role = GraphTextureRole::TemporalRead;
    read.temporalKey = "taa";
    graph.textures.push_back(read);
    if (completePair) {
        GraphTextureDesc write{"HistoryWrite", rhi::PixelFormat::RGBA16SFloat, false, false, 0, 0, 0, 1};
        write.role = GraphTextureRole::TemporalWrite;
        write.temporalKey = "taa";
        graph.textures.push_back(write);
    }
    return graph;
}

} // namespace

int main()
{
    const auto invalidTarget = MakeShadowGraph(ShaderCompileTarget::Forward);
    assert(!SceneRenderGraph::ValidateGraphDescription(invalidTarget, 1));

    const auto validTarget = MakeShadowGraph(ShaderCompileTarget::Shadow);
    assert(SceneRenderGraph::ValidateGraphDescription(validTarget, 1));

    assert(SceneRenderGraph::ValidateGraphDescription(MakeMotionGraph(rhi::PixelFormat::RG16SFloat, true), 1));
    assert(!SceneRenderGraph::ValidateGraphDescription(MakeMotionGraph(rhi::PixelFormat::RGBA8UNorm, true), 1));
    assert(!SceneRenderGraph::ValidateGraphDescription(MakeMotionGraph(rhi::PixelFormat::RG16SFloat, false), 1));
    assert(SceneRenderGraph::ValidateGraphDescription(MakeTemporalGraph(true), 1));
    assert(!SceneRenderGraph::ValidateGraphDescription(MakeTemporalGraph(false), 1));
    assert(!SceneRenderGraph::ValidateGraphDescription(MakeTemporalGraph(true, false), 1));

    const glm::vec2 firstJitter = SceneRenderGraph::ComputeTemporalJitterNdc(0, 200, 100);
    assert(std::abs(firstJitter.x) < 1e-7f);
    assert(std::abs(firstJitter.y + (1.0f / 300.0f)) < 1e-6f);
    assert(SceneRenderGraph::ComputeTemporalJitterNdc(0, 200, 100) ==
           SceneRenderGraph::ComputeTemporalJitterNdc(8, 200, 100));
    assert(SceneRenderGraph::ComputeTemporalJitterNdc(0, 0, 100) == glm::vec2(0.0f));

    glm::mat4 perspective(1.0f);
    perspective[2][3] = -1.0f;
    const glm::vec2 offset{0.01f, -0.02f};
    const glm::mat4 jitteredPerspective = SceneRenderGraph::ApplyTemporalJitter(perspective, offset);
    assert(std::abs(jitteredPerspective[2][0] + 0.01f) < 1e-7f);
    assert(std::abs(jitteredPerspective[2][1] - 0.02f) < 1e-7f);

    glm::mat4 orthographic(1.0f);
    const glm::mat4 jitteredOrthographic = SceneRenderGraph::ApplyTemporalJitter(orthographic, offset);
    assert(std::abs(jitteredOrthographic[3][0] - 0.01f) < 1e-7f);
    assert(std::abs(jitteredOrthographic[3][1] + 0.02f) < 1e-7f);
    return 0;
}
