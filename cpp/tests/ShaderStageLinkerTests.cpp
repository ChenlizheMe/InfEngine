#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
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
void vertex(inout VertexInput vertex, out VertexOutput output)
{
    vertex.position.y += material.amplitude + texture(displacement, vertex.texCoord).r;
    output.waveUV = vertex.texCoord;
    output.waveHeight = vertex.position.y;
    output.waveBand = 1;
    output.unusedOutput = 0.0;
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
void surface(in FragmentInput input, out SurfaceData surface)
{
    surface = InitSurfaceData();
    surface.albedo = material.deepColor.rgb * texture(normalMap, input.waveUV).rgb;
    surface.emission = vec3(input.waveUV, input.waveHeight);
    surface.metallic = float(input.waveBand) * 0.1;
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
    assert(windDirection.byteSize == 16);
    const auto &windStrength = RequireProperty(artifact, "windStrength");
    assert(windStrength.bufferOffset == 32);
    const auto &displacement = RequireProperty(artifact, "displacement");
    assert(displacement.textureSlot == 0);
    const auto &deepColor = RequireProperty(artifact, "deepColor");
    assert(deepColor.bufferOffset == 48);
    const auto &normalMap = RequireProperty(artifact, "normalMap");
    assert(normalMap.textureSlot == 1);
    assert(artifact.alphaClipThresholdOffset == 64);
    assert(artifact.materialBufferSize == 80);
    assert(artifact.varyingInterfaceSignature != 0);
    assert(artifact.materialLayoutSignature != 0);
    assert(artifact.compatibilitySignature != 0);

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
    assert(compiledProgram.generatedVertexSource.find("layout(location = 6) smooth out vec2 _inx_v_waveUV;") !=
           std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("layout(location = 7) flat in int _inx_v_waveBand;") !=
           std::string::npos);
    assert(compiledProgram.generatedVertexSource.find("vertex(v, output);") != std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("surface(input, s);") != std::string::npos);
    assert(compiledProgram.generatedVertexSource.find("binding = 2) uniform sampler2D displacement;") !=
           std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("binding = 3) uniform sampler2D normalMap;") !=
           std::string::npos);
    assert(compiledProgram.generatedFragmentSource.find("binding = 14) uniform MaterialProperties") !=
           std::string::npos);

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
    assert(unconsumedOutputsProgram.generatedVertexSource.find("vertex(v, output);") != std::string::npos);

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

    std::cout << "Shader stage linker tests passed\n";
    return 0;
}
