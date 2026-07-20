#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/resources/ShaderAsset/ShaderInfoSchema.h>

#include <algorithm>
#include <cassert>
#include <filesystem>
#include <iostream>
#include <nlohmann/json.hpp>
#include <string>

namespace
{
infernux::InxShaderLoader MakeCompiler()
{
    return infernux::InxShaderLoader(true, false, false, false, false, true, false, false, false, false);
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
    Imports ["lib/common", "lib/color"]
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

    const std::string legacySurfaceSource = R"(
#version 450
// @shader_id: legacy_surface
// @shading_model: unlit
// @property: baseColor, Color, [1.0, 1.0, 1.0, 1.0]
void surface(out SurfaceData s)
{
    s = InitSurfaceData();
}
)";
    const infernux::ShaderEntryPointSet legacyEntries = infernux::DetectShaderEntryPoints(legacySurfaceSource);
    assert(legacyEntries.surface);
    assert(!legacyEntries.main);
    assert(!legacyEntries.vertex);

    const std::string vertexEntrySource = R"(
// VertexOutput vertex(inout VertexInput ignored)
VertexOutput vertex(inout VertexInput value) { return VertexOutput(); }
)";
    const std::string rewrittenVertex =
        infernux::RewriteShaderEntryPoint(vertexEntrySource, "VertexOutput", "vertex", "inxVertexEntry");
    assert(rewrittenVertex.find("// VertexOutput vertex(inout VertexInput ignored)") != std::string::npos);
    assert(rewrittenVertex.find("VertexOutput inxVertexEntry(inout VertexInput value)") != std::string::npos);

    auto compiler = MakeCompiler();
    infernux::InxShaderLoader::AddShaderSearchPath(INFERNUX_TEST_SHADER_ROOT);
    const infernux::ShaderDescriptor descriptor = compiler.ParseShaderSource(richSource, "WaveSurface.frag");
    assert(descriptor.usesStructuredInfo);
    assert(descriptor.shaderId == "Tests/WaveSurface");
    assert(descriptor.shadingModel == "pbr");
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
    ShadingModel "unlit"
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

    const std::string legacySource = R"(
@shader_id: Legacy/Test
@property: amount, Float, 0.5
@property: glow, Color, [0.1, 0.2, 0.3, 1.0], HDR
// void main() { }
void vertex(inout VertexInput v) { v.position.x += amount; }
)";
    const auto legacy = compiler.ParseShaderSource(legacySource, "Legacy.vert");
    assert(!legacy.usesStructuredInfo);
    assert(legacy.shaderId == "Legacy/Test");
    assert(legacy.properties.size() == 2);
    assert(legacy.properties[1].defaultValue == "[0.1, 0.2, 0.3, 1.0]");
    assert(legacy.properties[1].hdr);
    assert(legacy.hasVertexFunc);
    assert(!legacy.hasMainFunc);

    infernux::InxResourceMeta legacyMetadata;
    compiler.CreateMeta(legacySource.data(), legacySource.size(), "Legacy.vert", legacyMetadata);
    const nlohmann::json legacyProperties = nlohmann::json::parse(legacyMetadata.GetDataAs<std::string>("properties"));
    assert(legacyProperties[1]["default"] == nlohmann::json::array({0.1, 0.2, 0.3, 1.0}));
    assert(legacyProperties[1]["hdr"] == true);

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
ShaderInfo { Version 1 Name "Tests/NoLayout" }
// layout(location = 3) in vec2 ignoredComment;
layout(location = 0) out vec4 outColor;
void main() { outColor = vec4(1.0); }
)";
    const auto layoutLocation = infernux::FindShaderLayoutDeclaration(forbiddenLayout);
    assert(layoutLocation.has_value());
    assert(layoutLocation->line == 4);
    const auto layoutDescriptor = compiler.ParseShaderSource(forbiddenLayout, "NoLayout.frag");
    assert(!layoutDescriptor.errors.empty());

    const auto mixedDescriptor = compiler.ParseShaderSource(
        "ShaderInfo { Version 1 Name \"Tests/Mixed\" }\n@queue: 2100\nvoid surface(out SurfaceData s) {}\n",
        "Mixed.frag");
    assert(!mixedDescriptor.errors.empty());

    std::cout << "ShaderInfo schema tests passed\n";
    return 0;
}
