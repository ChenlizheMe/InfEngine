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

} // namespace

int main()
{
    const auto invalidTarget = MakeShadowGraph(ShaderCompileTarget::Forward);
    assert(!SceneRenderGraph::ValidateGraphDescription(invalidTarget, 1));

    const auto validTarget = MakeShadowGraph(ShaderCompileTarget::Shadow);
    assert(SceneRenderGraph::ValidateGraphDescription(validTarget, 1));
    return 0;
}
