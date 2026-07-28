#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/resources/ShaderAsset/ShaderInfoSchema.h>

#include <algorithm>
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>
#include <string>
#include <string_view>

namespace
{
infernux::InxShaderLoader MakeCompiler()
{
    return infernux::InxShaderLoader(true, false, false, false, false, true, false, false, false, false);
}

std::string ReadText(const std::string &path)
{
    std::ifstream stream(path, std::ios::binary);
    assert(stream && "shader test resource must exist");
    return {std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
}

void RequireCompiles(infernux::InxShaderLoader &compiler, const std::string &source, const std::string &path)
{
    infernux::InxResourceMeta metadata;
    compiler.CreateMeta(source.data(), source.size(), path, metadata);
    const auto compiled = compiler.Compile(source.c_str(), source.size(), metadata);
    if (!compiled || compiled->empty()) {
        std::cerr << "Structured shader compilation failed for " << path << '\n';
        assert(false && "structured shader failed to compile");
    }
}

void RequireLinkedProgramCompiles(infernux::InxShaderLoader &compiler, const std::string &vertexPath,
                                  const std::string &fragmentPath)
{
    const auto compiled =
        compiler.CompileLinkedForward(ReadText(vertexPath), vertexPath, ReadText(fragmentPath), fragmentPath);
    if (!compiled.IsValid()) {
        std::cerr << "Structured shader program compilation failed for " << vertexPath << " + " << fragmentPath << '\n';
        for (const auto &error : compiled.errors)
            std::cerr << "  " << error << '\n';
        assert(false && "structured shader program failed to compile");
    }
}
} // namespace

int main()
{
    const std::string richSource = R"(
// ShaderInfo { Name "Ignored/InComment" }
ShaderInfo
{
    Name "Tests/WaveSurface"
    ShadingModel "PBR"
    Surface Opaque
    Queue 2050
    Cull Back
    DepthWrite On
    CastShadows On
    ReceiveShadows Off
    Imports ["Lib Common", "Lib Color"]
    Capabilities [ForwardPlus, Deferred, Shadow]

    Properties
    {
        Float amplitude = 0.35 Range(0.0, 4.0)
        Color crestColor = [0.2, 0.75, 0.9, 1.0] HDR
        Texture2D normalMap = Normal
    }

    Inputs
    {
        Smooth Float2 waveUV Semantic(TexCoord0)
        Flat Float waveBand Space(World)
    }
}

// void main() must not count.
void surface (out SurfaceData surface) { }
)";

    const infernux::ShaderInfoDocument info = infernux::ParseShaderInfo(richSource);
    assert(info.IsValid());
    assert(info.kind == infernux::ShaderInfoKind::Shader);
    assert(info.name == "Tests/WaveSurface");
    assert(info.shadingModel == "PBR");
    assert(info.renderQueue == 2050);
    assert(info.castShadows == true);
    assert(info.receiveShadows == false);
    assert(info.imports.size() == 2);
    assert(info.capabilities.size() == 3);
    assert(info.properties.size() == 3);
    assert(info.properties[0].range.has_value());
    assert(info.properties[0].range->minimum == 0.0);
    assert(info.properties[1].hdr);
    assert(info.properties[1].defaultValue == "[0.2, 0.75, 0.9, 1.0]");
    assert(info.inputs.size() == 2);
    assert(info.inputs[0].semantic == "TexCoord0");
    assert(info.inputs[1].interpolation == "Flat");
    assert(info.inputs[1].space == "World");

    const std::string stripped = infernux::StripShaderInfoDeclaration(richSource, info);
    assert(stripped.size() == richSource.size());
    assert(std::count(stripped.begin(), stripped.end(), '\n') ==
           std::count(richSource.begin(), richSource.end(), '\n'));
    assert(stripped.find("Tests/WaveSurface") == std::string::npos);
    assert(stripped.find("void surface") != std::string::npos);

    const infernux::ShaderEntryPointSet entries = infernux::DetectShaderEntryPoints(richSource);
    assert(entries.surface);
    assert(!entries.main);
    assert(!entries.vertex);

    auto compiler = MakeCompiler();
    infernux::InxShaderLoader::AddShaderSearchPath(INFERNUX_TEST_SHADER_ROOT);

    const std::string standalonePassSource = R"(
#version 450
ShaderInfo {
    Name "Tests/StandalonePass"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D sourceTexture
        Texture2DUInt objectMetadata
    }
    PushConstants settings {
        Float intensity
        Float4 tint
    }
    Inputs {
        Float2 inputUv
    }
    Outputs {
        Float4 outputColor
    }
}
void main() {
    outputColor = texture(sourceTexture, inputUv) * settings.tint * settings.intensity;
}
)";
    const auto standaloneInfo = infernux::ParseShaderInfo(standalonePassSource);
    assert(standaloneInfo.IsValid());
    assert(standaloneInfo.resources.size() == 2);
    assert(standaloneInfo.resources[1].type == "Texture2DUInt");
    assert(standaloneInfo.pushConstants.has_value());
    assert(standaloneInfo.pushConstants->fields.size() == 2);
    assert(!infernux::FindShaderLayoutDeclaration(standalonePassSource).has_value());
    RequireCompiles(compiler, standalonePassSource, "StandalonePass.frag");

    for (const auto &entry : std::filesystem::recursive_directory_iterator(INFERNUX_TEST_SHADER_ROOT)) {
        if (!entry.is_regular_file() || entry.path().string().find("_templates") != std::string::npos)
            continue;
        const std::string extension = entry.path().extension().string();
        if (extension != ".vert" && extension != ".frag" && extension != ".glsl" && extension != ".shadingmodel")
            continue;
        const std::string path = entry.path().string();
        const std::string source = ReadText(path);
        const auto builtinInfo = infernux::ParseShaderInfo(source);
        if (!builtinInfo.IsValid()) {
            std::cerr << "Invalid built-in ShaderInfo: " << path << '\n';
            for (const auto &diagnostic : builtinInfo.diagnostics)
                std::cerr << "  " << diagnostic.location.line << ':' << diagnostic.location.column << ' '
                          << diagnostic.message << '\n';
        }
        assert(builtinInfo.IsValid());
        assert(!infernux::FindShaderLayoutDeclaration(source).has_value());
        const auto builtinEntries =
            infernux::DetectShaderEntryPoints(infernux::StripShaderInfoDeclaration(source, builtinInfo));
        if ((extension == ".vert" || extension == ".frag") && builtinEntries.main) {
            const auto hasCapability = [&](std::string_view capability) {
                return std::find(builtinInfo.capabilities.begin(), builtinInfo.capabilities.end(), capability) !=
                       builtinInfo.capabilities.end();
            };
            assert((hasCapability("Fullscreen") || hasCapability("Standalone")) &&
                   "explicit main() stages must declare their direct compilation domain");
        }
        const std::string filename = entry.path().filename().string();
        const bool linkedParticleStage = filename == "particle_unlit.frag" ||
                                         filename == "particle_six_way_smoke.frag" ||
                                         filename == "particle_sprite.vert";
        if ((extension == ".vert" || extension == ".frag") && !linkedParticleStage)
            RequireCompiles(compiler, source, path);
    }

    const std::string shaderRoot = INFERNUX_TEST_SHADER_ROOT;
    RequireLinkedProgramCompiles(compiler, shaderRoot + "/particle_sprite.vert", shaderRoot + "/particle_unlit.frag");
    RequireLinkedProgramCompiles(compiler, shaderRoot + "/particle_sprite.vert",
                                 shaderRoot + "/particle_six_way_smoke.frag");

    const std::string removedAnnotationSource =
        "#version 450\n" + std::string(1, '@') + "shader_id: Removed\nvoid main() { }\n";
    const infernux::ShaderDescriptor removedAnnotationDescriptor =
        compiler.ParseShaderSource(removedAnnotationSource, "Removed.frag");
    assert(!removedAnnotationDescriptor.errors.empty());

    const infernux::ShaderDescriptor gridDescriptor =
        compiler.ParseShaderSource(ReadText(std::string(INFERNUX_TEST_SHADER_ROOT) + "/grid.frag"), "grid.frag");
    assert(gridDescriptor.surfaceOptions.surfaceType == "transparent");
    assert(gridDescriptor.surfaceOptions.blendMode == "alpha");
    assert(gridDescriptor.depthWrite == "off");

    const infernux::ShaderDescriptor gizmoIconDescriptor = compiler.ParseShaderSource(
        ReadText(std::string(INFERNUX_TEST_SHADER_ROOT) + "/gizmo_icon.frag"), "gizmo_icon.frag");
    assert(gizmoIconDescriptor.surfaceOptions.surfaceType == "transparent");
    assert(gizmoIconDescriptor.surfaceOptions.blendMode == "alpha");
    assert(gizmoIconDescriptor.surfaceOptions.alphaClip == "0.01");
    assert(gizmoIconDescriptor.depthWrite == "off");

    const std::string vertexEntrySource = R"(
// VertexOutput vertex(inout VertexInput ignored)
VertexOutput vertex(inout VertexInput value) { return VertexOutput(); }
)";
    const std::string rewrittenVertex =
        infernux::RewriteShaderEntryPoint(vertexEntrySource, "VertexOutput", "vertex", "inxVertexEntry");
    assert(rewrittenVertex.find("// VertexOutput vertex(inout VertexInput ignored)") != std::string::npos);
    assert(rewrittenVertex.find("VertexOutput inxVertexEntry(inout VertexInput value)") != std::string::npos);

    const infernux::ShaderDescriptor descriptor = compiler.ParseShaderSource(richSource, "WaveSurface.frag");
    assert(descriptor.shaderId == "Tests/WaveSurface");
    assert(descriptor.shadingModel == "PBR");
    assert(descriptor.hasSurfaceFunc);
    assert(!descriptor.hasMainFunc);
    assert(descriptor.properties.size() == 2);
    assert(descriptor.textureProperties.size() == 1);
    assert(descriptor.inputs.size() == 2);

    infernux::InxResourceMeta richMetadata;
    compiler.CreateMeta(richSource.data(), richSource.size(), "WaveSurface.frag", richMetadata);
    assert(richMetadata.GetDataAs<std::string>("shader_schema_format") == "ShaderInfo");
    const nlohmann::json propertySchema = nlohmann::json::parse(richMetadata.GetDataAs<std::string>("properties"));
    assert(propertySchema.size() == 3);
    assert(propertySchema[0]["default"] == 0.35);
    assert(propertySchema[0]["range"] == nlohmann::json::array({0.0, 4.0}));
    assert(propertySchema[1]["hdr"] == true);

    const std::string vertexSource = R"(
#version 450
ShaderInfo {
    Name "Tests/StructuredWave"
    Properties {
        Float amplitude = 0.05 Range(0.0, 1.0)
    }
}
void vertex(inout VertexInput v) {
    v.position.y += sin(v.position.x) * material.amplitude;
}
)";
    RequireCompiles(compiler, vertexSource, "StructuredWave.vert");

    const std::string fragmentSource = R"(
#version 450
ShaderInfo {
    Name "Tests/StructuredUnlit"
    ShadingModel "Unlit"
    Surface Opaque
    Queue 2000
    Properties {
        Color baseColor = [1.0, 0.5, 0.25, 1.0] HDR
        Texture2D texSampler = white
    }
}
void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 sampled = texture(texSampler, v_TexCoord);
    s.albedo = sampled.rgb * material.baseColor.rgb;
    s.alpha = sampled.a * material.baseColor.a;
}
)";
    RequireCompiles(compiler, fragmentSource, "StructuredUnlit.frag");

    const auto invalid =
        infernux::ParseShaderInfo("ShaderInfo { Version 2 Properties { Float x = 1.0 Float x = 2.0 } }");
    assert(!invalid.IsValid());
    assert(invalid.diagnostics.size() >= 2);

    const auto duplicate = infernux::ParseShaderInfo("ShaderInfo { Name \"First\" }\nShaderInfo { Name \"Second\" }\n");
    assert(!duplicate.IsValid());

    const auto reversedRange =
        infernux::ParseShaderInfo("ShaderInfo { Properties { Float amount = 1.0 Range(2.0, 1.0) } }");
    assert(!reversedRange.IsValid());
    const auto vectorRange =
        infernux::ParseShaderInfo("ShaderInfo { Properties { Float3 direction = [0, 1, 0] Range(0, 1) } }");
    assert(!vectorRange.IsValid());

    const std::string forbiddenLayout = R"(
ShaderInfo { Name "Tests/NoLayout" }
// layout(location = 3) in vec2 ignoredComment;
layout(location = 0) out vec4 outColor;
void main() { outColor = vec4(1.0); }
)";
    const auto layoutLocation = infernux::FindShaderLayoutDeclaration(forbiddenLayout);
    assert(layoutLocation.has_value());
    assert(layoutLocation->line == 4);
    const auto layoutDescriptor = compiler.ParseShaderSource(forbiddenLayout, "NoLayout.frag");
    assert(!layoutDescriptor.errors.empty());

    const auto mixedDescriptor =
        compiler.ParseShaderSource("ShaderInfo { Name \"Tests/Mixed\" }\n" + std::string(1, '@') +
                                       "queue: 2100\nvoid surface(out SurfaceData s) {}\n",
                                   "Mixed.frag");
    assert(!mixedDescriptor.errors.empty());

    std::cout << "ShaderInfo schema tests passed\n";
    return 0;
}
