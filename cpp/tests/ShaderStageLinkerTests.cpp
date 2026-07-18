#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/ShaderAsset/ShaderPassVariantPlanner.h>
#include <function/resources/ShaderAsset/ShaderStageLinker.h>

#include <algorithm>
#include <cassert>
#include <iostream>
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

bool HasDiagnostic(const infernux::ShaderProgramInterfaceArtifact &artifact, infernux::ShaderLinkDiagnosticCode code)
{
    return std::any_of(artifact.diagnostics.begin(), artifact.diagnostics.end(),
                       [&](const auto &diagnostic) { return diagnostic.code == code; });
}
} // namespace

int main()
{
    auto compiler = MakeCompiler();
    const std::string waveVertex = R"(
ShaderInfo
{
    Version 1
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
    Version 1
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
    assert(artifact.schemaVersion == infernux::ShaderProgramInterfaceArtifact::CurrentSchemaVersion);
    assert(artifact.vertex.shaderId == "Tests/WaveDeform");
    assert(artifact.fragment.shaderId == "Tests/OceanSurface");
    assert(artifact.shadingModel == "pbr");
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
    for (const auto target : {infernux::ShaderCompileTarget::Forward, infernux::ShaderCompileTarget::GBuffer,
                              infernux::ShaderCompileTarget::Shadow, infernux::ShaderCompileTarget::Depth,
                              infernux::ShaderCompileTarget::Picking, infernux::ShaderCompileTarget::Motion}) {
        const auto *requirement = passPlan.Find(target);
        assert(requirement != nullptr);
        assert(requirement->enabled);
        assert(!requirement->reason.empty());
    }

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
    assert(gbufferProgram.generatedFragmentSource.find("fragmentInput.waveUV = _inx_v_waveUV;") != std::string::npos);

    const auto completeCompilation =
        compiler.CompileLinkedProgramArtifact(waveVertex, "WaveDeform.vert", oceanFragment, "OceanSurface.frag");
    if (!completeCompilation.IsValid()) {
        for (const auto &error : completeCompilation.errors)
            std::cerr << error << '\n';
    }
    assert(completeCompilation.IsValid());
    assert(completeCompilation.compiledVariants.size() == 5);
    assert(completeCompilation.pendingTargets.size() == 1);
    assert(completeCompilation.pendingTargets[0] == infernux::ShaderCompileTarget::Motion);
    const auto completeArtifact = completeCompilation.CreateRuntimeArtifact();
    assert(completeArtifact.IsValid());
    assert(completeArtifact.variants.size() == 5);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::GBuffer) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Shadow) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Depth) != nullptr);
    assert(completeArtifact.FindVariant(infernux::ShaderCompileTarget::Picking) != nullptr);
    assert(completeArtifact.key.revision != runtimeArtifact.key.revision);
    const auto shadowCompilation =
        std::find_if(completeCompilation.compiledVariants.begin(), completeCompilation.compiledVariants.end(),
                     [](const auto &variant) { return variant.target == infernux::ShaderCompileTarget::Shadow; });
    assert(shadowCompilation != completeCompilation.compiledVariants.end());
    assert(shadowCompilation->generatedVertexSource.find("layout(location = 6) smooth out vec2 _inx_v_waveUV;") !=
           std::string::npos);
    assert(shadowCompilation->generatedVertexSource.find(
               "layout(set = 2, binding = 0) uniform sampler2D displacement;") != std::string::npos);
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

    auto reorderedArtifact = completeArtifact;
    std::reverse(reorderedArtifact.variants.begin(), reorderedArtifact.variants.end());
    assert(infernux::ComputeShaderProgramArtifactRevision(reorderedArtifact) == completeArtifact.key.revision);
    auto changedVariantArtifact = completeArtifact;
    changedVariantArtifact.variants[1].fragmentSpirv.back() ^= 1;
    assert(infernux::ComputeShaderProgramArtifactRevision(changedVariantArtifact) != completeArtifact.key.revision);
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

    const std::string transparentFragment = R"(
ShaderInfo
{
    Version 1
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
    assert(transparentCompilation.compiledVariants.size() == 1);
    assert(transparentCompilation.pendingTargets.empty());
    const auto transparentArtifact = transparentCompilation.CreateRuntimeArtifact();
    assert(transparentArtifact.IsValid());
    assert(transparentArtifact.variants.size() == 1);
    assert(transparentArtifact.FindVariant(infernux::ShaderCompileTarget::Forward) != nullptr);

    const std::string fragmentWithoutCustomInputs = R"(
ShaderInfo
{
    Version 1
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
    assert(unconsumedOutputsProgram.generatedVertexSource.find("VertexOutput _inx_output = inxVertexEntry(v);") !=
           std::string::npos);

    const std::string legacyStandardVertex = R"(
#version 450
@shader_id: standard
)";
    const auto migrationProgram = compiler.CompileLinkedForward(legacyStandardVertex, "standard.vert",
                                                                fragmentWithoutCustomInputs, "PlainSurface.frag");
    assert(migrationProgram.IsValid());
    assert(migrationProgram.CreateRuntimeArtifact().IsValid());
    assert(migrationProgram.CreateRuntimeArtifact().key.stages.vertexShaderId == "standard");
    assert(migrationProgram.generatedVertexSource.find("uniform MaterialProperties") == std::string::npos);
    assert(migrationProgram.generatedFragmentSource.find("uniform MaterialProperties") != std::string::npos);

    const std::string migrationMaterialFragment = R"(
ShaderInfo
{
    Version 1
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
        legacyStandardVertex, "standard.vert", migrationMaterialFragment, "MigrationMaterial.frag");
    if (!migrationMaterialProgram.IsValid()) {
        for (const auto &error : migrationMaterialProgram.errors)
            std::cerr << error << '\n';
    }
    assert(migrationMaterialProgram.IsValid());

    const std::string lavaFragment = R"(
ShaderInfo
{
    Version 1
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
    Version 1
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
    Version 1
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
    Version 1
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

    std::cout << "Shader stage linker tests passed\n";
    return 0;
}
