#include <function/renderer/SceneRenderGraph.h>

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
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
    return 0;
}
