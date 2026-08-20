#include <function/renderer/particle/ParticleGpuBounds.h>
#include <function/renderer/particle/ParticleGpuCuller.h>
#include <function/renderer/particle/ParticleGpuMigrator.h>
#include <function/renderer/particle/ParticleGpuRibbonRenderer.h>
#include <function/renderer/particle/ParticleGpuRibbonTopology.h>
#include <function/renderer/particle/ParticleGpuSorter.h>
#include <function/renderer/shader/ShaderReflection.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/ShaderAsset/ShaderPassVariantPlanner.h>
#include <function/resources/ShaderAsset/ShaderStageLinker.h>

#include <algorithm>
#include <cassert>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

namespace
{
infernux::InxShaderLoader MakeCompiler()
{
    return infernux::InxShaderLoader(true, false, false, false, false, true, false, false, false, false);
}

const infernux::LinkedShaderProperty &RequireProperty(const infernux::ShaderProgramInterfaceArtifact &artifact,
                                                      const std::string &name)
{
    const auto property = std::find_if(artifact.properties.begin(), artifact.properties.end(),
                                       [&](const auto &candidate) { return candidate.schema.name == name; });
    assert(property != artifact.properties.end());
    return *property;
}

const infernux::ShaderProgramPropertyBinding &RequireRuntimeProperty(const infernux::ShaderProgramArtifact &artifact,
                                                                     const std::string &name)
{
    const auto property = std::find_if(artifact.properties.begin(), artifact.properties.end(),
                                       [&](const auto &candidate) { return candidate.name == name; });
    assert(property != artifact.properties.end());
    return *property;
}

bool HasDiagnostic(const infernux::ShaderProgramInterfaceArtifact &artifact, infernux::ShaderLinkDiagnosticCode code)
{
    return std::any_of(artifact.diagnostics.begin(), artifact.diagnostics.end(),
                       [&](const auto &diagnostic) { return diagnostic.code == code; });
}

std::string ReadText(const std::string &path)
{
    std::ifstream input(path, std::ios::binary);
    assert(input.good());
    std::ostringstream text;
    text << input.rdbuf();
    return text.str();
}

} // namespace

int main()
{
    assert(infernux::ShaderCompileTargetUsesInstanceAuxiliary(infernux::ShaderCompileTarget::Forward));
    assert(infernux::ShaderCompileTargetUsesInstanceAuxiliary(infernux::ShaderCompileTarget::ForwardPlus));
    assert(infernux::ShaderCompileTargetUsesInstanceAuxiliary(infernux::ShaderCompileTarget::GBuffer));
    assert(!infernux::ShaderCompileTargetUsesInstanceAuxiliary(infernux::ShaderCompileTarget::Shadow));

    auto compiler = MakeCompiler();
    const std::string waveVertex = R"(
ShaderInfo
{
    Name "Tests/WaveDeform"
    Properties
    {
        Float amplitude = 0.35 Range(0.0, 4.0)
        Float speed = 0.8
        Float3 windDirection = [1.0, 0.0, 0.0]
        Float windStrength = 0.2
        Texture2D displacement = Black
    }
    Outputs
    {
        Smooth Float2 waveUV Semantic(TexCoord7)
        Smooth Float waveHeight Space(World)
        Flat Int waveBand
        Smooth Float unusedOutput
    }
}
VertexOutput vertex(inout VertexInput v)
{
    VertexOutput result;
    v.position.y += material.amplitude + texture(displacement, v.texCoord).r;
    result.waveUV = v.texCoord;
    result.waveHeight = v.position.y;
    result.waveBand = 1;
    result.unusedOutput = 0.0;
    return result;
}
)";
    const std::string oceanFragment = R"(
ShaderInfo
{
    Name "Tests/OceanSurface"
    ShadingModel "PBR"
    Properties
    {
        Float amplitude = 0.35 Range(0.0, 4.0)
        Color deepColor = [0.01, 0.08, 0.16, 1.0]
        Texture2D normalMap = Normal
    }
    Inputs
    {
        Flat Int waveBand
        Smooth Float waveHeight Space(World)
        Smooth Float2 waveUV Semantic(TexCoord7)
    }
}
void surface(out SurfaceData s)
{
    s = InitSurfaceData();
    s.albedo = material.deepColor.rgb * texture(normalMap, fragmentInput.waveUV).rgb;
    s.emission = vec3(fragmentInput.waveUV, fragmentInput.waveHeight);
    s.metallic = float(fragmentInput.waveBand) * 0.1;
}
)";

    const auto vertex = compiler.ParseShaderSource(waveVertex, "WaveDeform.vert");
    const auto fragment = compiler.ParseShaderSource(oceanFragment, "OceanSurface.frag");
    const auto artifact = infernux::ShaderStageLinker::Link(vertex, fragment);
    assert(artifact.IsValid());
    assert(artifact.vertex.shaderId == "Tests/WaveDeform");
    assert(artifact.fragment.shaderId == "Tests/OceanSurface");
    assert(artifact.shadingModel == "PBR");
    assert(artifact.varyings.size() == 3);
    assert(artifact.varyings[0].name == "waveUV");
    assert(artifact.varyings[0].location == 6);
    assert(artifact.varyings[1].name == "waveBand");
    assert(artifact.varyings[1].location == 7);
    assert(artifact.varyings[2].name == "waveHeight");
    assert(artifact.varyings[2].location == 8);

    const auto &amplitude = RequireProperty(artifact, "amplitude");
    assert(amplitude.visibility ==
           (infernux::ShaderStageVisibility::Vertex | infernux::ShaderStageVisibility::Fragment));
    assert(amplitude.bufferOffset == 0);
    assert(amplitude.byteSize == 4);
    const auto &speed = RequireProperty(artifact, "speed");
    assert(speed.bufferOffset == 4);
    const auto &windDirection = RequireProperty(artifact, "windDirection");
    assert(windDirection.bufferOffset == 16);
    assert(windDirection.byteSize == 12);
    const auto &windStrength = RequireProperty(artifact, "windStrength");
    assert(windStrength.bufferOffset == 28);
    const auto &displacement = RequireProperty(artifact, "displacement");
    assert(displacement.textureSlot == 0);
    const auto &deepColor = RequireProperty(artifact, "deepColor");
    assert(deepColor.bufferOffset == 32);
    const auto &normalMap = RequireProperty(artifact, "normalMap");
    assert(normalMap.textureSlot == 1);
    assert(artifact.alphaClipThresholdOffset == 48);
    assert(artifact.materialBufferSize == 64);
    assert(artifact.varyingInterfaceSignature != 0);
    assert(artifact.materialLayoutSignature != 0);
    assert(artifact.compatibilitySignature != 0);

    const auto passPlan = infernux::ShaderPassVariantPlanner::Plan(vertex, fragment, artifact);
    assert(passPlan.IsValid());
    assert(passPlan.requirements.size() == static_cast<size_t>(infernux::ShaderCompileTarget::Count));
    for (const auto target : {infernux::ShaderCompileTarget::Forward, infernux::ShaderCompileTarget::ForwardPlus,
                              infernux::ShaderCompileTarget::GBuffer, infernux::ShaderCompileTarget::Shadow,
                              infernux::ShaderCompileTarget::Depth, infernux::ShaderCompileTarget::Picking,
                              infernux::ShaderCompileTarget::Motion, infernux::ShaderCompileTarget::Normal,
                              infernux::ShaderCompileTarget::BaseColor}) {
        const auto *requirement = passPlan.Find(target);
        assert(requirement != nullptr);
        assert(requirement->enabled);
        assert(!requirement->reason.empty());
    }
    const auto forwardOnlyPlan = infernux::ShaderPassVariantPlanner::Plan(vertex, fragment, artifact, false);
    const auto *forwardOnlyGBuffer = forwardOnlyPlan.Find(infernux::ShaderCompileTarget::GBuffer);
    assert(forwardOnlyGBuffer != nullptr);
    assert(!forwardOnlyGBuffer->enabled);
    assert(forwardOnlyGBuffer->fallback == infernux::ShaderCompileTarget::ForwardPlus);
    assert(forwardOnlyGBuffer->reason.find("Unsupported [Deferred]") != std::string::npos);

    infernux::InxShaderLoader::AddShaderSearchPath(INFERNUX_TEST_SHADER_ROOT);
    const auto compiledProgram =
        compiler.CompileLinkedForward(waveVertex, "WaveDeform.vert", oceanFragment, "OceanSurface.frag");
    if (!compiledProgram.IsValid()) {
        for (const auto &error : compiledProgram.errors)
            std::cerr << error << '\n';
        std::cerr << "--- generated vertex ---\n" << compiledProgram.generatedVertexSource;
        std::cerr << "--- generated fragment ---\n" << compiledProgram.generatedFragmentSource;
    }
    assert(compiledProgram.IsValid());
    const auto runtimeArtifact = compiledProgram.CreateRuntimeArtifact();
    assert(runtimeArtifact.IsValid());
    assert(runtimeArtifact.key.stages.vertexShaderId == "Tests/WaveDeform");
    assert(runtimeArtifact.key.stages.fragmentShaderId == "Tests/OceanSurface");
    assert(runtimeArtifact.key.revision != 0);
    assert(runtimeArtifact.domain == infernux::ShaderProgramDomain::Mesh);
    assert(runtimeArtifact.shadingModel == "PBR");
    assert(runtimeArtifact.materialBufferSize == artifact.materialBufferSize);
    assert(runtimeArtifact.alphaClipThresholdOffset == artifact.alphaClipThresholdOffset);
    assert(runtimeArtifact.properties.size() == artifact.properties.size());
    const auto &runtimeAmplitude = RequireRuntimeProperty(runtimeArtifact, "amplitude");
    assert(runtimeAmplitude.bufferOffset == amplitude.bufferOffset);
    assert(runtimeAmplitude.byteSize == amplitude.byteSize);
    assert(infernux::HasStage(runtimeAmplitude.stages, infernux::ShaderProgramStageMask::Vertex));
    assert(infernux::HasStage(runtimeAmplitude.stages, infernux::ShaderProgramStageMask::Fragment));
    assert(runtimeAmplitude.range == amplitude.schema.range);
    const auto &runtimeNormalMap = RequireRuntimeProperty(runtimeArtifact, "normalMap");
    assert(runtimeNormalMap.IsTexture());
    assert(runtimeNormalMap.textureSlot == normalMap.textureSlot);
    assert(runtimeNormalMap.textureDefault == "Normal");
    assert(!infernux::HasStage(runtimeNormalMap.stages, infernux::ShaderProgramStageMask::Vertex));
    assert(infernux::HasStage(runtimeNormalMap.stages, infernux::ShaderProgramStageMask::Fragment));
    assert(runtimeArtifact.compatibilitySignature == compiledProgram.interfaceArtifact.compatibilitySignature);
    const auto *forwardVariant = runtimeArtifact.FindVariant(infernux::ShaderCompileTarget::Forward);
    assert(forwardVariant != nullptr);
    assert(forwardVariant->vertexSpirv == compiledProgram.vertexSpirv);
    assert(forwardVariant->fragmentSpirv == compiledProgram.fragmentSpirv);
    auto duplicateVariantArtifact = runtimeArtifact;
    duplicateVariantArtifact.variants.push_back(*forwardVariant);
    assert(!duplicateVariantArtifact.IsValid());
    assert(runtimeArtifact.key.ToString().find("Tests/WaveDeform|Tests/OceanSurface@") == 0);
    assert(compiledProgram.generatedVertexSource.find("layout(location = 6) smooth out vec2 _inx_v_waveUV;") !=
           std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("layout(location = 7) flat in int _inx_v_waveBand;") !=
           std::string::npos);
    assert(compiledProgram.generatedVertexSource.find("VertexOutput _inx_output = inxVertexEntry(v);") !=
           std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("fragmentInput.waveUV = _inx_v_waveUV;") != std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("surface(s);") != std::string::npos);
    assert(compiledProgram.generatedVertexSource.find("binding = 2) uniform sampler2D displacement;") !=
           std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("binding = 3) uniform sampler2D normalMap;") !=
           std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("binding = 14) uniform MaterialProperties") !=
           std::string::npos);

    const auto gbufferProgram = compiler.CompileLinkedProgram(
        waveVertex, "WaveDeform.vert", oceanFragment, "OceanSurface.frag", infernux::ShaderCompileTarget::GBuffer);
    if (!gbufferProgram.IsValid()) {
        for (const auto &error : gbufferProgram.errors)
            std::cerr << error << '\n';
    }
    assert(gbufferProgram.IsValid());
    const auto gbufferArtifact = gbufferProgram.CreateRuntimeArtifact();
    assert(gbufferArtifact.IsValid());
    assert(gbufferArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) == nullptr);
    const auto *gbufferVariant = gbufferArtifact.FindVariant(infernux::ShaderCompileTarget::GBuffer);
    assert(gbufferVariant != nullptr);
    assert(gbufferVariant->vertexSpirv == gbufferProgram.vertexSpirv);
    assert(gbufferVariant->fragmentSpirv == gbufferProgram.fragmentSpirv);
    assert(gbufferArtifact.key.revision != runtimeArtifact.key.revision);
    assert(gbufferProgram.generatedFragmentSource.find("layout(location = 0) out vec4 outGBuf0;") != std::string::npos);
    assert(gbufferProgram.generatedFragmentSource.find("layout(location = 4) out uvec2 outGBuf4;") !=
           std::string::npos);
    uint32_t pbrModelId = 2166136261u;
    for (const unsigned char character : std::string_view("PBR")) {
        pbrModelId ^= character;
        pbrModelId *= 16777619u;
    }
    assert(gbufferProgram.generatedFragmentSource.find("gbuf4 = uvec2(_inx_ObjectLayerMask, " +
                                                       std::to_string(pbrModelId) + "u);") != std::string::npos);
    assert(gbufferProgram.generatedFragmentSource.find("vec3 litColor =") == std::string::npos);
    assert(gbufferProgram.generatedFragmentSource.find("fragmentInput.waveUV = _inx_v_waveUV;") != std::string::npos);

    const auto completeCompilation =
        compiler.CompileLinkedProgramArtifact(waveVertex, "WaveDeform.vert", oceanFragment, "OceanSurface.frag");
    if (!completeCompilation.IsValid()) {
        for (const auto &error : completeCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(completeCompilation.IsValid());
    assert(completeCompilation.compiledVariants.size() == 9);
    assert(completeCompilation.pendingTargets.empty());
    const auto completeArtifact = completeCompilation.CreateRuntimeArtifact();
    assert(completeArtifact.IsValid());
    assert(completeArtifact.variants.size() == 9);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::GBuffer) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Shadow) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Depth) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Picking) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Motion) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Normal) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::BaseColor) != nullptr);
    assert(completeArtifact.key.revision != runtimeArtifact.key.revision);

    const auto shadowCompilation =
        std::find_if(completeCompilation.compiledVariants.begin(), completeCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Shadow; });
    assert(shadowCompilation != completeCompilation.compiledVariants.end());
    assert(shadowCompilation->generatedVertexSource.find("layout(location = 6) smooth out vec2 _inx_v_waveUV;") !=
           std::string::npos);
    assert(shadowCompilation->generatedVertexSource.find(
               "layout(set = 2, binding = 0) uniform sampler2D displacement;") != std::string::npos);
    assert(shadowCompilation->generatedVertexSource.find("set = 1, binding = 1") != std::string::npos);
    assert(shadowCompilation->generatedVertexSource.find("set = 1, binding = 2") != std::string::npos);
    assert(shadowCompilation->generatedVertexSource.find("set = 1, binding = 3") != std::string::npos);
    assert(shadowCompilation->generatedVertexSource.find("InstanceAuxBuffer") == std::string::npos);
    assert(shadowCompilation->generatedVertexSource.find("set = 1, binding = 4") == std::string::npos);
    assert(shadowCompilation->generatedFragmentSource.find("layout(location = 6) smooth in vec2 _inx_v_waveUV;") !=
           std::string::npos);

    const auto depthCompilation =
        std::find_if(completeCompilation.compiledVariants.begin(), completeCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Depth; });
    assert(depthCompilation != completeCompilation.compiledVariants.end());
    assert(depthCompilation->generatedFragmentSource.find("#define INX_DEPTH_PASS 1") != std::string::npos);
    assert(depthCompilation->generatedFragmentSource.find("surface(s);") != std::string::npos);
    assert(depthCompilation->generatedFragmentSource.find("s.alpha < material._AlphaClipThreshold") !=
           std::string::npos);
    assert(depthCompilation->generatedFragmentSource.find("outObjectId") == std::string::npos);

    const auto pickingCompilation =
        std::find_if(completeCompilation.compiledVariants.begin(), completeCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Picking; });
    assert(pickingCompilation != completeCompilation.compiledVariants.end());
    assert(pickingCompilation->generatedVertexSource.find("set = 2, binding = 4") != std::string::npos);
    assert(pickingCompilation->generatedVertexSource.find("layout(location = 15) flat out uvec2 _inx_ObjectId;") !=
           std::string::npos);
    assert(pickingCompilation->generatedVertexSource.find(
               "_inx_ObjectId = instanceAuxData[gl_InstanceIndex].objectId;") != std::string::npos);
    assert(pickingCompilation->generatedFragmentSource.find("layout(location = 15) flat in uvec2 _inx_ObjectId;") !=
           std::string::npos);
    assert(pickingCompilation->generatedFragmentSource.find("outObjectId = _inx_ObjectId;") != std::string::npos);
    assert(pickingCompilation->generatedFragmentSource.find("surface(s);") != std::string::npos);

    const auto motionCompilation =
        std::find_if(completeCompilation.compiledVariants.begin(), completeCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Motion; });
    assert(motionCompilation != completeCompilation.compiledVariants.end());
    assert(motionCompilation->generatedVertexSource.find("set = 2, binding = 4") != std::string::npos);
    assert(motionCompilation->generatedVertexSource.find("previousViewProj * aux.previousModel") != std::string::npos);
    assert(motionCompilation->generatedVertexSource.find("previousBoneOffset") != std::string::npos);
    assert(motionCompilation->generatedVertexSource.find("previousLocalPosition = inxUnskinnedPosition") !=
           std::string::npos);
    assert(motionCompilation->generatedFragmentSource.find("outMotion = _inx_MotionVector;") != std::string::npos);

    const auto baseColorCompilation =
        std::find_if(completeCompilation.compiledVariants.begin(), completeCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::BaseColor; });
    assert(baseColorCompilation != completeCompilation.compiledVariants.end());
    assert(baseColorCompilation->generatedFragmentSource.find("#define INX_BASE_COLOR_PASS 1") != std::string::npos);
    assert(baseColorCompilation->generatedFragmentSource.find("layout(location = 0) out vec4 outBaseColor;") !=
           std::string::npos);
    assert(baseColorCompilation->generatedFragmentSource.find("outBaseColor = vec4(s.albedo, s.alpha);") !=
           std::string::npos);
    assert(baseColorCompilation->generatedFragmentSource.find("s.alpha < material._AlphaClipThreshold") !=
           std::string::npos);
    assert(baseColorCompilation->generatedFragmentSource.find("void shading(") == std::string::npos);

    auto reorderedArtifact = completeArtifact;
    std::reverse(reorderedArtifact.variants.begin(), reorderedArtifact.variants.end());
    assert(infernux::ComputeShaderProgramArtifactRevision(reorderedArtifact) == completeArtifact.key.revision);
    auto changedVariantArtifact = completeArtifact;
    changedVariantArtifact.variants[1].fragmentSpirv.back() ^= 1;
    assert(infernux::ComputeShaderProgramArtifactRevision(changedVariantArtifact) != completeArtifact.key.revision);
    auto changedMaterialArtifact = completeArtifact;
    auto changedProperty =
        std::find_if(changedMaterialArtifact.properties.begin(), changedMaterialArtifact.properties.end(),
                     [](const auto &property) { return property.name == "amplitude"; });
    assert(changedProperty != changedMaterialArtifact.properties.end());
    changedProperty->defaultValue = "0.75";
    assert(infernux::ComputeShaderProgramArtifactRevision(changedMaterialArtifact) != completeArtifact.key.revision);
    const infernux::ShaderProgramVariantKey forwardProgramKey{completeArtifact.key,
                                                              infernux::ShaderCompileTarget::Forward};
    const infernux::ShaderProgramVariantKey shadowProgramKey{completeArtifact.key,
                                                             infernux::ShaderCompileTarget::Shadow};
    assert(forwardProgramKey.IsValid());
    assert(forwardProgramKey != shadowProgramKey);
    assert(infernux::ShaderProgramVariantKeyHash{}(forwardProgramKey) !=
           infernux::ShaderProgramVariantKeyHash{}(shadowProgramKey));

    std::string brokenGBufferFragment = oceanFragment;
    const std::string validMetallicLine = "    s.metallic = float(fragmentInput.waveBand) * 0.1;";
    const std::string brokenMetallicLine = R"(
#ifdef INX_GBUFFER_PASS
this_is_not_valid_glsl
#else
    s.metallic = float(fragmentInput.waveBand) * 0.1;
#endif
)";
    brokenGBufferFragment.replace(brokenGBufferFragment.find(validMetallicLine), validMetallicLine.size(),
                                  brokenMetallicLine);
    const auto atomicFailure = compiler.CompileLinkedProgramArtifact(waveVertex, "WaveDeform.vert",
                                                                     brokenGBufferFragment, "OceanSurface.frag");
    assert(!atomicFailure.IsValid());
    assert(std::any_of(atomicFailure.errors.begin(), atomicFailure.errors.end(),
                       [](const std::string &error) { return error.find("GBuffer:") != std::string::npos; }));
    assert(!atomicFailure.CreateRuntimeArtifact().IsValid());

    const auto supportedDepthProgram = compiler.CompileLinkedProgram(
        waveVertex, "WaveDeform.vert", oceanFragment, "OceanSurface.frag", infernux::ShaderCompileTarget::Depth);
    assert(supportedDepthProgram.IsValid());

    const std::string passBuffersFragment = R"(
ShaderInfo
{
    Name "Tests/PassBuffers"
    ShadingModel "Unlit"
    Capabilities [PassBuffers]
}
void surface(out SurfaceData surface)
{
    surface = InitSurfaceData();
    surface.albedo = texture(_InxPassColor, vec2(0.5)).rgb;
    surface.alpha = texture(_InxPassDepth, vec2(0.5)).r;
}
)";
    const auto passBuffersProgram = compiler.CompileLinkedProgram(
        waveVertex, "WaveDeform.vert", passBuffersFragment, "PassBuffers.frag", infernux::ShaderCompileTarget::Forward);
    assert(passBuffersProgram.IsValid());
    assert(passBuffersProgram.generatedFragmentSource.find("uniform sampler2D _InxPassColor;") != std::string::npos);
    assert(passBuffersProgram.generatedFragmentSource.find("uniform sampler2D _InxPassDepth;") != std::string::npos);
    assert(passBuffersProgram.generatedFragmentSource.find("uniform sampler2D _InxPassNormal;") != std::string::npos);
    assert(passBuffersProgram.generatedFragmentSource.find("uniform sampler2D _InxPassMotion;") != std::string::npos);
    assert(passBuffersProgram.generatedFragmentSource.find("_InxSceneColor") == std::string::npos);
    assert(passBuffersProgram.generatedFragmentSource.find("_InxSceneDepth") == std::string::npos);
    assert(passBuffersProgram.generatedFragmentSource.find("_InxSceneNormal") == std::string::npos);
    assert(passBuffersProgram.generatedFragmentSource.find("_InxSceneMotion") == std::string::npos);

    const std::string transparentFragment = R"(
ShaderInfo
{
    Name "Tests/TransparentForwardOnly"
    ShadingModel "Unlit"
    Surface Transparent
    CastShadows Off
    Capabilities [ForwardOnly, NoPicking, NoMotionVectors]
}
void surface(out SurfaceData surface)
{
    surface = InitSurfaceData();
    surface.alpha = 0.5;
}
)";
    const auto transparentDescriptor = compiler.ParseShaderSource(transparentFragment, "TransparentForwardOnly.frag");
    const auto transparentInterface = infernux::ShaderStageLinker::Link(vertex, transparentDescriptor);
    assert(transparentInterface.IsValid());
    const auto transparentPlan =
        infernux::ShaderPassVariantPlanner::Plan(vertex, transparentDescriptor, transparentInterface);
    assert(transparentPlan.IsValid());
    assert(transparentPlan.Find(infernux::ShaderCompileTarget::Forward)->enabled);
    assert(transparentPlan.Find(infernux::ShaderCompileTarget::ForwardPlus)->enabled);
    assert(!transparentPlan.Find(infernux::ShaderCompileTarget::GBuffer)->enabled);
    assert(transparentPlan.Find(infernux::ShaderCompileTarget::GBuffer)->fallback ==
           infernux::ShaderCompileTarget::Forward);
    assert(!transparentPlan.Find(infernux::ShaderCompileTarget::Shadow)->enabled);
    assert(!transparentPlan.Find(infernux::ShaderCompileTarget::Depth)->enabled);
    assert(!transparentPlan.Find(infernux::ShaderCompileTarget::Picking)->enabled);
    assert(!transparentPlan.Find(infernux::ShaderCompileTarget::Motion)->enabled);

    const auto transparentCompilation = compiler.CompileLinkedProgramArtifact(
        waveVertex, "WaveDeform.vert", transparentFragment, "TransparentForwardOnly.frag");
    assert(transparentCompilation.IsValid());
    assert(transparentCompilation.compiledVariants.size() == 2);
    assert(transparentCompilation.pendingTargets.empty());
    const auto transparentArtifact = transparentCompilation.CreateRuntimeArtifact();
    assert(transparentArtifact.IsValid());
    assert(transparentArtifact.variants.size() == 2);
    assert(transparentArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);
    assert(transparentArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus) != nullptr);

    const std::string particleVertex = R"(
ShaderInfo
{
    Name "Tests/ParticleSprite"
    Capabilities [ParticleSprite]
}
)";
    const std::string particleFragment = R"(
ShaderInfo
{
    Name "Tests/ParticleSurface"
    ShadingModel "Unlit"
    Surface Transparent
    Properties
    {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D texSampler = white
    }
}
void surface(out SurfaceData surface)
{
    surface = InitSurfaceData();
    vec4 sampled = texture(texSampler, v_TexCoord);
    surface.albedo = sampled.rgb * v_Color * material.baseColor.rgb;
    surface.alpha = sampled.a * material.baseColor.a;
}
)";
    const auto particleVertexDescriptor = compiler.ParseShaderSource(particleVertex, "ParticleSprite.vert");
    const auto particleFragmentDescriptor = compiler.ParseShaderSource(particleFragment, "ParticleSurface.frag");
    const auto particleInterface =
        infernux::ShaderStageLinker::Link(particleVertexDescriptor, particleFragmentDescriptor);
    assert(particleInterface.IsValid());
    assert(particleInterface.domain == infernux::ShaderProgramDomain::ParticleSprite);
    const auto particlePlan = infernux::ShaderPassVariantPlanner::Plan(particleVertexDescriptor,
                                                                       particleFragmentDescriptor, particleInterface);
    assert(particlePlan.IsValid());
    assert(particlePlan.Find(infernux::ShaderCompileTarget::Forward)->enabled);
    assert(particlePlan.Find(infernux::ShaderCompileTarget::ForwardPlus)->enabled);
    assert(particlePlan.Find(infernux::ShaderCompileTarget::Motion)->enabled);
    for (const auto target : {infernux::ShaderCompileTarget::GBuffer, infernux::ShaderCompileTarget::Shadow,
                              infernux::ShaderCompileTarget::Depth, infernux::ShaderCompileTarget::Picking}) {
        assert(!particlePlan.Find(target)->enabled);
    }
    const auto particleCompilation = compiler.CompileLinkedProgramArtifact(particleVertex, "ParticleSprite.vert",
                                                                           particleFragment, "ParticleSurface.frag");
    if (!particleCompilation.IsValid()) {
        for (const auto &error : particleCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(particleCompilation.IsValid());
    assert(particleCompilation.compiledVariants.size() == 3);
    assert(particleCompilation.pendingTargets.empty());
    const auto &particleForward = particleCompilation.compiledVariants.front();
    assert(particleForward.generatedVertexSource.find("readonly buffer ParticleInstances") != std::string::npos);
    assert(particleForward.generatedVertexSource.find("layout(location = 14) out float v_ParticleAlpha;") !=
           std::string::npos);
    assert(particleForward.generatedVertexSource.find("layout(location = 12) out float v_ParticleNormalizedAge;") !=
           std::string::npos);
    assert(particleForward.generatedVertexSource.find("v_ParticleNormalizedAge = instance.scale_custom.w;") !=
           std::string::npos);
    assert(particleForward.generatedVertexSource.find("v_ParticleFlipbookNextTexCoord") != std::string::npos);
    assert(particleForward.generatedVertexSource.find("v_ParticleFlipbookBlend = fract(authoredFrame);") !=
           std::string::npos);
    assert(particleForward.generatedVertexSource.find("v_ParticleId = instance.ribbon_data.w;") != std::string::npos);
    assert(particleForward.generatedVertexSource.find("UniformBufferObject") == std::string::npos);
    assert(particleForward.generatedFragmentSource.find("layout(location = 14) in float v_ParticleAlpha;") !=
           std::string::npos);
    assert(particleForward.generatedFragmentSource.find("layout(location = 12) in float v_ParticleNormalizedAge;") !=
           std::string::npos);
    assert(particleForward.generatedFragmentSource.find(
               "layout(location = 10) in vec2 v_ParticleFlipbookNextTexCoord;") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("sampleParticleFlipbook") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("float postSurfaceCoverage = v_ParticleAlpha;") !=
           std::string::npos);
    assert(particleForward.generatedFragmentSource.find("s.alpha *= postSurfaceCoverage;") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("set = 2, binding = 2") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("set = 2, binding = 14") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("set = 2, binding = 15") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("_inxParticleEyeDepth") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("return abs(numerator / ") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("sceneDepth - particleDepth") != std::string::npos);
    assert(particleForward.generatedVertexSource.find("set = 0, binding = 1") != std::string::npos);
    assert(particleForward.generatedVertexSource.find("draw_indices[gl_InstanceIndex]") != std::string::npos);
    assert(particleForward.generatedFragmentSource.find("_Globals") == std::string::npos);
    const auto particleArtifact = particleCompilation.CreateRuntimeArtifact();
    assert(particleArtifact.IsValid());
    assert(particleArtifact.domain == infernux::ShaderProgramDomain::ParticleSprite);
    assert(particleArtifact.usesParticleSceneDepthBinding);
    assert(particleArtifact.variants.size() == 3);
    assert(particleArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus) != nullptr);
    assert(particleArtifact.FindVariant(infernux::ShaderCompileTarget::Motion) != nullptr);

    const std::string litParticleFragment = R"(
ShaderInfo
{
    Name "Tests/LitParticleSurface"
    ShadingModel "PBR"
    Surface Transparent
    Properties
    {
        Color baseColor = [1.0, 0.5, 0.25, 1.0]
    }
}
void surface(out SurfaceData surface)
{
    surface = InitSurfaceData();
    surface.albedo = material.baseColor.rgb * v_Color;
    surface.alpha = material.baseColor.a;
    surface.smoothness = 0.4;
}
)";
    const auto litParticleCompilation = compiler.CompileLinkedProgramArtifact(
        particleVertex, "ParticleSprite.vert", litParticleFragment, "LitParticleSurface.frag");
    if (!litParticleCompilation.IsValid()) {
        for (const auto &error : litParticleCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(litParticleCompilation.IsValid());
    assert(litParticleCompilation.compiledVariants.size() == 3);
    const auto litParticleArtifact = litParticleCompilation.CreateRuntimeArtifact();
    assert(litParticleArtifact.IsValid());
    const auto *litParticleForwardPlus = litParticleArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus);
    assert(litParticleForwardPlus != nullptr);
    const auto &litParticleForwardPlusCompilation = litParticleCompilation.compiledVariants[1];
    assert(litParticleForwardPlusCompilation.target == infernux::ShaderCompileTarget::ForwardPlus);
    assert(litParticleForwardPlusCompilation.generatedFragmentSource.find("set = 1, binding = 0") != std::string::npos);
    assert(litParticleForwardPlusCompilation.generatedFragmentSource.find("set = 1, binding = 3") != std::string::npos);
    assert(litParticleForwardPlusCompilation.generatedFragmentSource.find("set = 1, binding = 4") != std::string::npos);
    assert(litParticleForwardPlusCompilation.generatedFragmentSource.find("ParticleLightingUBO") != std::string::npos);
    assert(litParticleForwardPlusCompilation.generatedFragmentSource.find("inxParticleReceivesShadows") !=
           std::string::npos);
    assert(litParticleForwardPlusCompilation.generatedFragmentSource.find("postSurfaceCoverage") != std::string::npos);
    assert(litParticleForwardPlusCompilation.generatedFragmentSource.find(
               "_forwardResult.rgb *= postSurfaceCoverage") != std::string::npos);
    assert(litParticleForwardPlusCompilation.generatedFragmentSource.find("ForwardPlusTileMaskBuffer") !=
           std::string::npos);

    const std::string shaderRoot = INFERNUX_TEST_SHADER_ROOT;
    const auto builtinParticleCompilation = compiler.CompileLinkedProgramArtifact(
        ReadText(shaderRoot + "/particle_sprite.vert"), shaderRoot + "/particle_sprite.vert",
        ReadText(shaderRoot + "/unlit.frag"), shaderRoot + "/unlit.frag");
    if (!builtinParticleCompilation.IsValid()) {
        for (const auto &error : builtinParticleCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(builtinParticleCompilation.IsValid());
    const auto builtinParticleArtifact = builtinParticleCompilation.CreateRuntimeArtifact();
    assert(builtinParticleArtifact.IsValid());
    assert(builtinParticleArtifact.domain == infernux::ShaderProgramDomain::ParticleSprite);
    assert(builtinParticleArtifact.key.stages.vertexShaderId == "Particle Sprite");
    assert(builtinParticleArtifact.key.stages.fragmentShaderId == "Unlit");
    assert(builtinParticleArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);

    const auto defaultParticleCompilation = compiler.CompileLinkedProgramArtifact(
        ReadText(shaderRoot + "/particle_sprite.vert"), shaderRoot + "/particle_sprite.vert",
        ReadText(shaderRoot + "/particle_unlit.frag"), shaderRoot + "/particle_unlit.frag");
    if (!defaultParticleCompilation.IsValid()) {
        for (const auto &error : defaultParticleCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(defaultParticleCompilation.IsValid());
    const auto defaultParticleArtifact = defaultParticleCompilation.CreateRuntimeArtifact();
    assert(defaultParticleArtifact.IsValid());
    assert(defaultParticleArtifact.key.stages.fragmentShaderId == "Particle Unlit");
    assert(defaultParticleArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);
    assert(defaultParticleCompilation.compiledVariants.size() == 3);
    assert(defaultParticleCompilation.compiledVariants.front().generatedFragmentSource.find("radialAlpha") ==
           std::string::npos);
    assert(defaultParticleCompilation.compiledVariants.front().generatedFragmentSource.find(
               "vec4 texColor = sampleAlbedoAlpha(texSampler);") != std::string::npos);
    assert(defaultParticleCompilation.compiledVariants.front().generatedFragmentSource.find(
               "s.alpha = texColor.a * material.baseColor.a;") != std::string::npos);

    const auto sixWaySmokeCompilation = compiler.CompileLinkedProgramArtifact(
        ReadText(shaderRoot + "/particle_sprite.vert"), shaderRoot + "/particle_sprite.vert",
        ReadText(shaderRoot + "/particle_six_way_smoke.frag"), shaderRoot + "/particle_six_way_smoke.frag");
    if (!sixWaySmokeCompilation.IsValid()) {
        for (const auto &error : sixWaySmokeCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(sixWaySmokeCompilation.IsValid());
    assert(sixWaySmokeCompilation.interfaceArtifact.domain == infernux::ShaderProgramDomain::ParticleSprite);
    assert(RequireProperty(sixWaySmokeCompilation.interfaceArtifact, "positiveAxesMap").schema.isTexture);
    assert(RequireProperty(sixWaySmokeCompilation.interfaceArtifact, "negativeAxesMap").schema.isTexture);
    const auto sixWaySmokeArtifact = sixWaySmokeCompilation.CreateRuntimeArtifact();
    assert(sixWaySmokeArtifact.IsValid());
    assert(sixWaySmokeArtifact.key.stages.fragmentShaderId == "Particle Six-Way Smoke");
    assert(sixWaySmokeArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);
    assert(sixWaySmokeArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus) != nullptr);

    // Lit declares the bindless capability in its source, but the loader must
    // still honor the active device contract. A device without descriptor
    // indexing receives the original bounded sampler ABI and must not emit
    // the bindless set or the texture-index UBO.
    infernux::InxShaderLoader::SetBindlessTextureABIEnabled(false);
    const auto boundedLitCompilation =
        compiler.CompileLinkedProgramArtifact(ReadText(shaderRoot + "/standard.vert"), shaderRoot + "/standard.vert",
                                              ReadText(shaderRoot + "/lit.frag"), shaderRoot + "/lit.frag");
    if (!boundedLitCompilation.IsValid()) {
        for (const auto &error : boundedLitCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(boundedLitCompilation.IsValid());
    assert(!boundedLitCompilation.CreateRuntimeArtifact().usesBindlessTextureABI);
    const auto boundedForward =
        std::find_if(boundedLitCompilation.compiledVariants.begin(), boundedLitCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Forward; });
    assert(boundedForward != boundedLitCompilation.compiledVariants.end());
    assert(boundedForward->generatedFragmentSource.find("layout(set = 3, binding = 0)") == std::string::npos);
    assert(boundedForward->generatedFragmentSource.find("layout(std140, set = 0, binding = 15)") == std::string::npos);
    assert(boundedForward->generatedFragmentSource.find("binding = 2) uniform sampler2D texSampler;") !=
           std::string::npos);
    assert(boundedForward->generatedFragmentSource.find("sampleAlbedoAlpha(uint textureIndex)") == std::string::npos);
    infernux::ShaderReflection boundedReflection;
    assert(boundedReflection.Reflect(boundedForward->fragmentSpirv, VK_SHADER_STAGE_FRAGMENT_BIT));
    assert(std::none_of(boundedReflection.GetSampledImages().begin(), boundedReflection.GetSampledImages().end(),
                        [](const auto &image) { return image.set == 3 && image.binding == 0; }));
    assert(std::any_of(
        boundedReflection.GetSampledImages().begin(), boundedReflection.GetSampledImages().end(),
        [](const auto &image) { return image.name == "texSampler" && image.set == 0 && image.binding == 2; }));
    assert(std::none_of(boundedReflection.GetUniformBuffers().begin(), boundedReflection.GetUniformBuffers().end(),
                        [](const auto &buffer) { return buffer.set == 0 && buffer.binding == 15; }));

    const std::string bindlessCutoutFragment = R"(
ShaderInfo {
    Name "Tests/BindlessCutout"
    Capabilities [BindlessTextures]
    ShadingModel Unlit
    AlphaClip on
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D texSampler = white
    }
}
void surface(out SurfaceData surface) {
    surface = InitSurfaceData();
    vec4 sampled = sampleAlbedoAlpha(texSampler);
    surface.albedo = sampled.rgb * material.baseColor.rgb;
    surface.alpha = sampled.a * material.baseColor.a;
}
)";
    const auto boundedCutoutCompilation =
        compiler.CompileLinkedProgramArtifact(ReadText(shaderRoot + "/standard.vert"), shaderRoot + "/standard.vert",
                                              bindlessCutoutFragment, "BindlessCutout.frag");
    assert(boundedCutoutCompilation.IsValid());
    const auto boundedCutoutShadow =
        std::find_if(boundedCutoutCompilation.compiledVariants.begin(), boundedCutoutCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Shadow; });
    assert(boundedCutoutShadow != boundedCutoutCompilation.compiledVariants.end());
    assert(!boundedCutoutShadow->usesBindlessTextureABI);
    assert(boundedCutoutShadow->generatedFragmentSource.find("set = 2, binding = 0") != std::string::npos);
    assert(boundedCutoutShadow->generatedFragmentSource.find("set = 3, binding = 0") == std::string::npos);

    // The same source must compile to the bindless ABI when the device
    // advertises the complete descriptor-indexing contract.
    infernux::InxShaderLoader::SetBindlessTextureABIEnabled(true);
    const auto builtinLitParticleCompilation = compiler.CompileLinkedProgramArtifact(
        ReadText(shaderRoot + "/particle_sprite.vert"), shaderRoot + "/particle_sprite.vert",
        ReadText(shaderRoot + "/lit.frag"), shaderRoot + "/lit.frag");
    if (!builtinLitParticleCompilation.IsValid()) {
        for (const auto &error : builtinLitParticleCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(builtinLitParticleCompilation.IsValid());
    const auto builtinLitParticleArtifact = builtinLitParticleCompilation.CreateRuntimeArtifact();
    assert(builtinLitParticleArtifact.IsValid());
    assert(builtinLitParticleArtifact.domain == infernux::ShaderProgramDomain::ParticleSprite);
    assert(builtinLitParticleArtifact.usesBindlessTextureABI);
    assert(builtinLitParticleArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus) != nullptr);
    const auto &builtinLitParticleForward = builtinLitParticleCompilation.compiledVariants.front();
    assert(builtinLitParticleForward.generatedFragmentSource.find("set = 2, binding = 2") != std::string::npos);
    assert(builtinLitParticleForward.generatedFragmentSource.find("set = 3, binding = 0") != std::string::npos);
    assert(builtinLitParticleForward.generatedFragmentSource.find(
               "layout(std140, set = 2, binding = 2) uniform InxMaterialTextureIndices") != std::string::npos);
    assert(builtinLitParticleForward.generatedFragmentSource.find("uniform sampler2D texSampler") == std::string::npos);

    const auto builtinLitCompilation =
        compiler.CompileLinkedProgramArtifact(ReadText(shaderRoot + "/standard.vert"), shaderRoot + "/standard.vert",
                                              ReadText(shaderRoot + "/lit.frag"), shaderRoot + "/lit.frag");
    if (!builtinLitCompilation.IsValid()) {
        for (const auto &error : builtinLitCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(builtinLitCompilation.IsValid());
    const auto builtinLitArtifact = builtinLitCompilation.CreateRuntimeArtifact();
    assert(builtinLitArtifact.IsValid());
    assert(builtinLitArtifact.usesBindlessTextureABI);
    assert(builtinLitArtifact.key.stages.vertexShaderId == "Standard");
    assert(builtinLitArtifact.key.stages.fragmentShaderId == "Lit");
    assert(builtinLitArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);
    assert(builtinLitArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus) != nullptr);
    assert(builtinLitArtifact.FindVariant(infernux::ShaderCompileTarget::GBuffer) != nullptr);
    const auto builtinForward =
        std::find_if(builtinLitCompilation.compiledVariants.begin(), builtinLitCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Forward; });
    const auto builtinForwardPlus =
        std::find_if(builtinLitCompilation.compiledVariants.begin(), builtinLitCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::ForwardPlus; });
    assert(builtinForward != builtinLitCompilation.compiledVariants.end());
    assert(builtinForwardPlus != builtinLitCompilation.compiledVariants.end());
    const auto builtinGBuffer =
        std::find_if(builtinLitCompilation.compiledVariants.begin(), builtinLitCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::GBuffer; });
    assert(builtinGBuffer != builtinLitCompilation.compiledVariants.end());
    assert(builtinForward->generatedFragmentSource.find("void shading(") != std::string::npos);
    assert(builtinForward->generatedFragmentSource.find("layout(set = 3, binding = 0) uniform sampler2D") !=
           std::string::npos);
    assert(builtinForward->generatedFragmentSource.find("nonuniformEXT") != std::string::npos);
    assert(builtinForward->generatedFragmentSource.find("layout(std140, set = 0, binding = 15)") != std::string::npos);
    assert(builtinForward->generatedFragmentSource.find("sampleAlbedoAlpha(uint textureIndex)") != std::string::npos);
    infernux::ShaderReflection bindlessReflection;
    assert(bindlessReflection.Reflect(builtinForward->fragmentSpirv, VK_SHADER_STAGE_FRAGMENT_BIT));
    assert(std::any_of(bindlessReflection.GetSampledImages().begin(), bindlessReflection.GetSampledImages().end(),
                       [](const auto &image) { return image.set == 3 && image.binding == 0; }));
    assert(std::any_of(bindlessReflection.GetUniformBuffers().begin(), bindlessReflection.GetUniformBuffers().end(),
                       [](const auto &buffer) { return buffer.set == 0 && buffer.binding == 15; }));
    assert(builtinForwardPlus->generatedFragmentSource.find("void shading(") != std::string::npos);
    assert(builtinForward->generatedFragmentSource.find("evaluateForward") == std::string::npos);
    assert(builtinForwardPlus->generatedFragmentSource.find("evaluateForward") == std::string::npos);
    assert(builtinGBuffer->generatedFragmentSource.find("INX_GBUFFER_PASS") != std::string::npos);
    assert(builtinGBuffer->generatedFragmentSource.find("void shading(") == std::string::npos);
    assert(builtinGBuffer->generatedFragmentSource.find("CanonicalLightBuffer") == std::string::npos);
    assert(builtinForward->generatedFragmentSource.find("CanonicalLightBuffer") == std::string::npos);
    assert(builtinForwardPlus->generatedFragmentSource.find("#define INX_FORWARD_PLUS_PASS 1") != std::string::npos);
    assert(builtinForwardPlus->generatedFragmentSource.find("set = 1, binding = 1") != std::string::npos);
    assert(builtinForwardPlus->generatedFragmentSource.find("set = 1, binding = 2") != std::string::npos);
    assert(builtinForwardPlus->generatedFragmentSource.find("set = 1, binding = 3") != std::string::npos);
    assert(builtinForwardPlus->generatedFragmentSource.find("inxForwardPlusTileHeader()") != std::string::npos);
    assert(builtinForwardPlus->generatedVertexSource.find("set = 2, binding = 4") != std::string::npos);
    assert(builtinForwardPlus->generatedVertexSource.find("_inx_ObjectLayerMask =") != std::string::npos);
    assert(builtinForwardPlus->generatedFragmentSource.find("light.metadata.y & _inx_ObjectLayerMask") !=
           std::string::npos);

    const auto bindlessCutoutCompilation =
        compiler.CompileLinkedProgramArtifact(ReadText(shaderRoot + "/standard.vert"), shaderRoot + "/standard.vert",
                                              bindlessCutoutFragment, "BindlessCutout.frag");
    if (!bindlessCutoutCompilation.IsValid()) {
        for (const auto &error : bindlessCutoutCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(bindlessCutoutCompilation.IsValid());
    const auto bindlessCutoutArtifact = bindlessCutoutCompilation.CreateRuntimeArtifact();
    assert(bindlessCutoutArtifact.usesBindlessTextureABI);
    const auto bindlessCutoutShadow = std::find_if(
        bindlessCutoutCompilation.compiledVariants.begin(), bindlessCutoutCompilation.compiledVariants.end(),
        [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Shadow; });
    assert(bindlessCutoutShadow != bindlessCutoutCompilation.compiledVariants.end());
    assert(bindlessCutoutShadow->usesBindlessTextureABI);
    assert(bindlessCutoutShadow->generatedFragmentSource.find("set = 3, binding = 0") != std::string::npos);
    assert(bindlessCutoutShadow->generatedFragmentSource.find("set = 2, binding = 15") != std::string::npos);
    assert(bindlessCutoutShadow->generatedFragmentSource.find(
               "inxSampleBindlessTexture(_InxMaterialTextureIndices.texSampler, uv).a") != std::string::npos);
    infernux::ShaderReflection bindlessShadowReflection;
    assert(bindlessShadowReflection.Reflect(bindlessCutoutShadow->fragmentSpirv, VK_SHADER_STAGE_FRAGMENT_BIT));
    assert(std::any_of(bindlessShadowReflection.GetSampledImages().begin(),
                       bindlessShadowReflection.GetSampledImages().end(),
                       [](const auto &image) { return image.set == 3 && image.binding == 0; }));
    assert(std::any_of(bindlessShadowReflection.GetUniformBuffers().begin(),
                       bindlessShadowReflection.GetUniformBuffers().end(),
                       [](const auto &buffer) { return buffer.set == 2 && buffer.binding == 15; }));
    infernux::InxShaderLoader::SetBindlessTextureABIEnabled(false);

    const auto builtinToonCompilation =
        compiler.CompileLinkedProgramArtifact(ReadText(shaderRoot + "/standard.vert"), shaderRoot + "/standard.vert",
                                              ReadText(shaderRoot + "/toon.frag"), shaderRoot + "/toon.frag");
    if (!builtinToonCompilation.IsValid()) {
        for (const auto &error : builtinToonCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(builtinToonCompilation.IsValid());
    const auto builtinToonArtifact = builtinToonCompilation.CreateRuntimeArtifact();
    assert(builtinToonArtifact.IsValid());
    assert(builtinToonArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);
    assert(builtinToonArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus) != nullptr);
    assert(builtinToonArtifact.FindVariant(infernux::ShaderCompileTarget::GBuffer) != nullptr);
    const auto toonGBuffer =
        std::find_if(builtinToonCompilation.compiledVariants.begin(), builtinToonCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::GBuffer; });
    assert(toonGBuffer != builtinToonCompilation.compiledVariants.end());
    assert(toonGBuffer->generatedFragmentSource.find("s.shadingParam0") != std::string::npos);
    assert(toonGBuffer->generatedFragmentSource.find("s.shadingParam1") != std::string::npos);

    const auto spriteLitCompilation = compiler.CompileLinkedProgramArtifact(
        ReadText(shaderRoot + "/standard.vert"), shaderRoot + "/standard.vert",
        ReadText(shaderRoot + "/sprite_lit.frag"), shaderRoot + "/sprite_lit.frag");
    if (!spriteLitCompilation.IsValid()) {
        for (const auto &error : spriteLitCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(spriteLitCompilation.IsValid());
    assert(spriteLitCompilation.CreateRuntimeArtifact().FindVariant(infernux::ShaderCompileTarget::Shadow) != nullptr);
    const auto spriteShadow =
        std::find_if(spriteLitCompilation.compiledVariants.begin(), spriteLitCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Shadow; });
    assert(spriteShadow != spriteLitCompilation.compiledVariants.end());
    assert(spriteShadow->generatedFragmentSource.find("uniform ShaderLighting") == std::string::npos);
    assert(spriteShadow->generatedFragmentSource.find("vec3 sampleAmbientProbe") == std::string::npos);

    const auto errorCompilation =
        compiler.CompileLinkedProgramArtifact(ReadText(shaderRoot + "/error.vert"), shaderRoot + "/error.vert",
                                              ReadText(shaderRoot + "/error.frag"), shaderRoot + "/error.frag");
    if (!errorCompilation.IsValid()) {
        for (const auto &error : errorCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(errorCompilation.IsValid());
    const auto errorArtifact = errorCompilation.CreateRuntimeArtifact();
    assert(errorArtifact.FindVariant(infernux::ShaderCompileTarget::ForwardPlus) != nullptr);
    assert(errorArtifact.FindVariant(infernux::ShaderCompileTarget::Shadow) != nullptr);
    for (const auto &variant : errorCompilation.compiledVariants) {
        if (variant.target != infernux::ShaderCompileTarget::Forward &&
            variant.target != infernux::ShaderCompileTarget::ForwardPlus &&
            variant.target != infernux::ShaderCompileTarget::GBuffer)
            continue;
        assert(variant.generatedVertexSource.find("layout(location = 15) flat out uint _inx_ObjectLayerMask;") !=
               std::string::npos);
        assert(variant.generatedFragmentSource.find("layout(location = 15) flat in uint _inx_ObjectLayerMask;") !=
               std::string::npos);
    }

    const std::string fragmentWithoutCustomInputs = R"(
ShaderInfo
{
    Name "Tests/PlainSurface"
    ShadingModel "Unlit"
}
void surface(out SurfaceData surface)
{
    surface = InitSurfaceData();
    surface.albedo = vec3(0.4, 0.5, 0.6);
}
)";
    const auto unconsumedOutputsProgram =
        compiler.CompileLinkedForward(waveVertex, "WaveDeform.vert", fragmentWithoutCustomInputs, "PlainSurface.frag");
    assert(unconsumedOutputsProgram.IsValid());
    assert(unconsumedOutputsProgram.interfaceArtifact.varyings.empty());
    assert(unconsumedOutputsProgram.generatedFragmentSource.find(
               "#define INX_SHADING_CAMERA_POSITION lighting.cameraPos.xyz") != std::string::npos);
    assert(unconsumedOutputsProgram.generatedFragmentSource.find("uniform LightingUBO") != std::string::npos);
    assert(unconsumedOutputsProgram.generatedVertexSource.find("VertexOutput _inx_output = inxVertexEntry(v);") !=
           std::string::npos);

    const std::string standardVertex = R"(
#version 450
ShaderInfo { Name "Standard" }
)";
    const auto migrationProgram = compiler.CompileLinkedForward(standardVertex, "standard.vert",
                                                                fragmentWithoutCustomInputs, "PlainSurface.frag");
    assert(migrationProgram.IsValid());
    assert(migrationProgram.CreateRuntimeArtifact().IsValid());
    assert(migrationProgram.CreateRuntimeArtifact().key.stages.vertexShaderId == "Standard");
    assert(migrationProgram.generatedVertexSource.find("uniform MaterialProperties") == std::string::npos);
    assert(migrationProgram.generatedFragmentSource.find("uniform MaterialProperties") != std::string::npos);

    const std::string migrationMaterialFragment = R"(
ShaderInfo
{
    Name "Tests/MigrationMaterial"
    ShadingModel "Unlit"
    Properties
    {
        Color baseColor = [0.15, 0.65, 1.0, 1.0] HDR
        Float intensity = 1.0 Range(0.0, 2.0)
        Texture2D texSampler = white
    }
}
void surface(out SurfaceData surface)
{
    surface = InitSurfaceData();
    vec4 sampled = texture(texSampler, v_TexCoord);
    surface.albedo = sampled.rgb * material.baseColor.rgb * material.intensity;
    surface.alpha = sampled.a * material.baseColor.a;
}
)";
    const auto migrationMaterialProgram = compiler.CompileLinkedForward(
        standardVertex, "standard.vert", migrationMaterialFragment, "MigrationMaterial.frag");
    if (!migrationMaterialProgram.IsValid()) {
        for (const auto &error : migrationMaterialProgram.errors)
            std::cerr << error << '\n';
    }
    assert(migrationMaterialProgram.IsValid());

    const std::string lavaFragment = R"(
ShaderInfo
{
    Name "Tests/LavaSurface"
    ShadingModel "Unlit"
    Inputs
    {
        Smooth Float waveHeight Space(World)
        Smooth Float2 waveUV Semantic(TexCoord7)
    }
}
void surface(out SurfaceData surface) { }
)";
    const auto lava = compiler.ParseShaderSource(lavaFragment, "LavaSurface.frag");
    const auto lavaArtifact = infernux::ShaderStageLinker::Link(vertex, lava);
    assert(lavaArtifact.IsValid());
    assert(lavaArtifact.varyings.size() == 2);
    assert(lavaArtifact.varyings[0].name == "waveUV");
    assert(lavaArtifact.varyings[1].name == "waveHeight");

    const std::string missingInput = R"(
ShaderInfo
{
    Name "Tests/Missing"
    Inputs { Smooth Float missing }
}
void main() { }
)";
    auto brokenFragment = compiler.ParseShaderSource(missingInput, "Missing.frag");
    auto brokenArtifact = infernux::ShaderStageLinker::Link(vertex, brokenFragment);
    assert(!brokenArtifact.IsValid());
    assert(HasDiagnostic(brokenArtifact, infernux::ShaderLinkDiagnosticCode::MissingVertexOutput));

    const std::string mismatchedInput = R"(
ShaderInfo
{
    Name "Tests/Mismatch"
    Properties { Float amplitude = 0.5 Range(0.0, 1.0) }
    Inputs
    {
        Flat Float2 waveUV Semantic(TexCoord7)
        Smooth Float waveHeight Space(Object)
    }
}
void main() { }
)";
    brokenFragment = compiler.ParseShaderSource(mismatchedInput, "Mismatch.frag");
    brokenArtifact = infernux::ShaderStageLinker::Link(vertex, brokenFragment);
    assert(!brokenArtifact.IsValid());
    assert(HasDiagnostic(brokenArtifact, infernux::ShaderLinkDiagnosticCode::InterpolationMismatch));
    assert(HasDiagnostic(brokenArtifact, infernux::ShaderLinkDiagnosticCode::SpaceMismatch));
    assert(HasDiagnostic(brokenArtifact, infernux::ShaderLinkDiagnosticCode::PropertyContractMismatch));

    const std::string textureVarying = R"(
ShaderInfo
{
    Name "Tests/TextureVarying"
    Inputs { Texture2D displacement }
}
void main() { }
)";
    brokenFragment = compiler.ParseShaderSource(textureVarying, "TextureVarying.frag");
    brokenArtifact = infernux::ShaderStageLinker::Link(vertex, brokenFragment);
    assert(!brokenArtifact.IsValid());
    assert(HasDiagnostic(brokenArtifact, infernux::ShaderLinkDiagnosticCode::UnsupportedVaryingType));

    infernux::ShaderStageLinkOptions constrained;
    constrained.firstUserVaryingLocation = 8;
    constrained.maximumVaryingLocations = 9;
    brokenArtifact = infernux::ShaderStageLinker::Link(vertex, fragment, constrained);
    assert(!brokenArtifact.IsValid());
    assert(HasDiagnostic(brokenArtifact, infernux::ShaderLinkDiagnosticCode::LocationLimitExceeded));

    auto tooManyTextures = fragment;
    tooManyTextures.textureProperties.clear();
    for (uint32_t index = 0; index < 13; ++index) {
        infernux::ShaderProperty texture;
        texture.name = "texture" + std::to_string(index);
        texture.type = "Texture2D";
        texture.isTexture = true;
        texture.defaultValue = "White";
        texture.textureDefault = "White";
        tooManyTextures.textureProperties.push_back(std::move(texture));
    }
    brokenArtifact = infernux::ShaderStageLinker::Link(vertex, tooManyTextures);
    assert(!brokenArtifact.IsValid());
    assert(HasDiagnostic(brokenArtifact, infernux::ShaderLinkDiagnosticCode::TextureBindingLimitExceeded));

    const auto repeated = infernux::ShaderStageLinker::Link(vertex, fragment);
    assert(repeated.varyingInterfaceSignature == artifact.varyingInterfaceSignature);
    assert(repeated.materialLayoutSignature == artifact.materialLayoutSignature);
    assert(repeated.compatibilitySignature == artifact.compatibilitySignature);

    const auto repeatedCompilation =
        compiler.CompileLinkedForward(waveVertex, "WaveDeform.vert", oceanFragment, "OceanSurface.frag");
    assert(repeatedCompilation.IsValid());
    assert(repeatedCompilation.CreateRuntimeArtifact().key == runtimeArtifact.key);

    std::string changedFragment = oceanFragment;
    changedFragment.replace(changedFragment.find("0.1"), 3, "0.2");
    const auto changedCompilation =
        compiler.CompileLinkedForward(waveVertex, "WaveDeform.vert", changedFragment, "OceanSurface.frag");
    assert(changedCompilation.IsValid());
    assert(changedCompilation.CreateRuntimeArtifact().key.stages == runtimeArtifact.key.stages);
    assert(changedCompilation.CreateRuntimeArtifact().key.revision != runtimeArtifact.key.revision);

    const std::array<std::pair<std::string_view, const char *>, 5> particleSortShaders = {{
        {infernux::particle::GpuParticleSortShaderSources::Small(), "ParticleSortSmall.comp"},
        {infernux::particle::GpuParticleSortShaderSources::Generate(), "ParticleSortGenerate.comp"},
        {infernux::particle::GpuParticleSortShaderSources::Histogram(), "ParticleSortHistogram.comp"},
        {infernux::particle::GpuParticleSortShaderSources::Scan(), "ParticleSortScan.comp"},
        {infernux::particle::GpuParticleSortShaderSources::Scatter(), "ParticleSortScatter.comp"},
    }};
    const auto particleSortGenerate = infernux::particle::GpuParticleSortShaderSources::Generate();
    assert(particleSortGenerate.find("inx_particle_sort_key(view_position.z") != std::string_view::npos);
    assert(particleSortGenerate.find("visibility[particle_index].position_radius.xyz") != std::string_view::npos &&
           particleSortGenerate.find("ribbon_data.w") == std::string_view::npos);
    assert(particleSortGenerate.find("(depth_key & 0xffffff00u) | (particle_id & 0xffu)") != std::string_view::npos);
    assert(particleSortGenerate.find("-view_position.z") == std::string_view::npos);
    for (const auto &[source, name] : particleSortShaders) {
        const auto spirv = compiler.CompileComputeGlsl(std::string(source), name);
        assert(spirv.size() >= 5 * sizeof(uint32_t));
        uint32_t magic = 0;
        std::memcpy(&magic, spirv.data(), sizeof(magic));
        assert(magic == 0x07230203u);
    }

    const std::array<std::pair<std::string_view, const char *>, 3> particleCullShaders = {{
        {infernux::particle::GpuParticleCullShaderSources::Reset(), "ParticleCullReset.comp"},
        {infernux::particle::GpuParticleCullShaderSources::Cull(), "ParticleCull.comp"},
        {infernux::particle::GpuParticleCullShaderSources::Finalize(), "ParticleCullFinalize.comp"},
    }};
    const auto particleCullSource = infernux::particle::GpuParticleCullShaderSources::Cull();
    assert(particleCullSource.find("visible_segments") == std::string_view::npos);
    assert(particleCullSource.find("ribbon_segments") != std::string_view::npos);
    assert(particleCullSource.find("ParticleVisibilityInstance") != std::string_view::npos);
    assert(particleCullSource.find("ParticleVisibilityInstance instance = visibility[particle_index]") !=
           std::string_view::npos);
    assert(particleCullSource.find("instance.position_radius.xyz") != std::string_view::npos);
    assert(particleCullSource.find("ribbon_instances[first_index]") != std::string_view::npos);
    for (const auto &[source, name] : particleCullShaders) {
        const auto spirv = compiler.CompileComputeGlsl(std::string(source), name);
        assert(spirv.size() >= 5 * sizeof(uint32_t));
        uint32_t magic = 0;
        std::memcpy(&magic, spirv.data(), sizeof(magic));
        assert(magic == 0x07230203u);
    }

    const std::array<std::pair<std::string_view, const char *>, 2> particleBoundsShaders = {{
        {infernux::particle::GpuParticleBoundsShaderSources::Reset(), "ParticleBoundsReset.comp"},
        {infernux::particle::GpuParticleBoundsShaderSources::Reduce(), "ParticleBoundsReduce.comp"},
    }};
    for (const auto &[source, name] : particleBoundsShaders) {
        const auto spirv = compiler.CompileComputeGlsl(std::string(source), name);
        assert(spirv.size() >= 5 * sizeof(uint32_t));
        uint32_t magic = 0;
        std::memcpy(&magic, spirv.data(), sizeof(magic));
        assert(magic == 0x07230203u);
    }

    const std::array<std::pair<std::string_view, const char *>, 2> particleMigrationShaders = {{
        {infernux::particle::GpuParticleMigrationShaderSources::Reset(), "ParticleMigrationReset.comp"},
        {infernux::particle::GpuParticleMigrationShaderSources::Migrate(), "ParticleMigration.comp"},
    }};
    for (const auto &[source, name] : particleMigrationShaders) {
        const auto spirv = compiler.CompileComputeGlsl(std::string(source), name);
        assert(spirv.size() >= 5 * sizeof(uint32_t));
        uint32_t magic = 0;
        std::memcpy(&magic, spirv.data(), sizeof(magic));
        assert(magic == 0x07230203u);
    }

    const std::array<std::pair<std::string_view, const char *>, 5> particleRibbonTopologyShaders = {{
        {infernux::particle::GpuParticleRibbonShaderSources::Reset(), "ParticleRibbonReset.comp"},
        {infernux::particle::GpuParticleRibbonShaderSources::Initialize(), "ParticleRibbonInitialize.comp"},
        {infernux::particle::GpuParticleRibbonShaderSources::Histogram(), "ParticleRibbonHistogram.comp"},
        {infernux::particle::GpuParticleRibbonShaderSources::Scan(), "ParticleRibbonScan.comp"},
        {infernux::particle::GpuParticleRibbonShaderSources::Scatter(), "ParticleRibbonScatter.comp"},
    }};
    const auto particleRibbonReset = infernux::particle::GpuParticleRibbonShaderSources::Reset();
    assert(particleRibbonReset.find("if (simulation_allowed == 0u) return") == std::string_view::npos);
    assert(particleRibbonReset.find("simulation_allowed != 0u ? inx_live_count() : 0u") !=
           std::string_view::npos);
    assert(particleRibbonReset.find("ribbon_instance_count = live_count > 1u ? 1u : 0u") !=
           std::string_view::npos);
    assert(infernux::particle::GpuParticleRibbonShaderSources::Scatter().find("ribbon_data") != std::string_view::npos);
    assert(infernux::particle::GpuParticleRibbonShaderSources::Scatter().find("& 0xffff") == std::string_view::npos);
    for (const auto &[source, name] : particleRibbonTopologyShaders) {
        const auto spirv = compiler.CompileComputeGlsl(std::string(source), name);
        assert(spirv.size() >= 5 * sizeof(uint32_t));
        uint32_t magic = 0;
        std::memcpy(&magic, spirv.data(), sizeof(magic));
        assert(magic == 0x07230203u);
    }

    const auto ribbonVertex = compiler.CompileVertexGlsl(
        std::string(infernux::particle::GpuParticleRibbonRenderShaderSources::Vertex()), "ParticleRibbon.vert");
    const auto ribbonPicking = compiler.CompileFragmentGlsl(
        std::string(infernux::particle::GpuParticleRibbonRenderShaderSources::PickingFragment()),
        "ParticleRibbonPicking.frag");
    const auto ribbonMotionVertex = compiler.CompileVertexGlsl(
        std::string(infernux::particle::GpuParticleRibbonRenderShaderSources::MotionVertex()),
        "ParticleRibbonMotion.vert");
    const auto ribbonMotionFragment = compiler.CompileFragmentGlsl(
        std::string(infernux::particle::GpuParticleRibbonRenderShaderSources::MotionFragment()),
        "ParticleRibbonMotion.frag");
    assert(ribbonVertex.size() >= 5 * sizeof(uint32_t));
    assert(infernux::particle::GpuParticleRibbonRenderShaderSources::Vertex().find("visible_segments") !=
           std::string_view::npos);
    assert(infernux::particle::GpuParticleRibbonRenderShaderSources::Vertex().find(
               "out_particle_local_uv = segment_local_uv") != std::string_view::npos);
    assert(infernux::particle::GpuParticleRibbonRenderShaderSources::Vertex().find("view.alignment_reference.z") !=
           std::string_view::npos);
    assert(infernux::particle::GpuParticleRibbonRenderShaderSources::Vertex().find("source_instance_count") !=
           std::string_view::npos);
    assert(infernux::particle::GpuParticleRibbonRenderShaderSources::Vertex().find("ribbon_join_offset") !=
           std::string_view::npos);
    assert(ribbonPicking.size() >= 5 * sizeof(uint32_t));
    assert(ribbonMotionVertex.size() >= 5 * sizeof(uint32_t));
    assert(ribbonMotionFragment.size() >= 5 * sizeof(uint32_t));

    std::cout << "Shader stage linker tests passed\n";
    return 0;
}
