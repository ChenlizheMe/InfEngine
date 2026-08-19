#include "GlslStageInterfaceEmitter.h"

#include <sstream>
#include <string_view>
#include <vector>

namespace infernux
{
namespace
{
std::string_view GlslType(std::string_view type)
{
    if (type == "Float")
        return "float";
    if (type == "Float2")
        return "vec2";
    if (type == "Float3")
        return "vec3";
    if (type == "Float4" || type == "Color")
        return "vec4";
    if (type == "Int")
        return "int";
    if (type == "Mat4")
        return "mat4";
    return {};
}

std::string_view Qualifier(std::string_view interpolation)
{
    if (interpolation == "Flat")
        return "flat ";
    if (interpolation == "NoPerspective")
        return "noperspective ";
    if (interpolation == "Centroid")
        return "centroid ";
    return "smooth ";
}

std::string VariableName(std::string_view varying)
{
    return "_inx_v_" + std::string(varying);
}

std::string_view DefaultGlslLiteral(std::string_view glslType)
{
    if (glslType == "float")
        return "0.0";
    if (glslType == "vec2")
        return "vec2(0.0)";
    if (glslType == "vec3")
        return "vec3(0.0)";
    if (glslType == "vec4")
        return "vec4(0.0)";
    if (glslType == "int")
        return "0";
    if (glslType == "mat4")
        return "mat4(1.0)";
    return "0.0";
}
} // namespace

std::string GlslStageInterfaceEmitter::EmitVertexDeclarations(const ShaderProgramInterfaceArtifact &artifact,
                                                              const ShaderDescriptor &vertex)
{
    std::ostringstream source;
    source << "\n// Linked user vertex outputs\n";
    for (const auto &varying : artifact.varyings) {
        source << "layout(location = " << varying.location << ") " << Qualifier(varying.interpolation) << "out "
               << varying.glslType << " " << VariableName(varying.name) << ";\n";
    }
    source << "struct VertexOutput {\n";
    for (const auto &varying : vertex.outputs) {
        const auto type = GlslType(varying.type);
        if (!type.empty())
            source << "    " << type << " " << varying.name << ";\n";
    }
    source << "};\n";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitFragmentDeclarations(const ShaderProgramInterfaceArtifact &artifact)
{
    std::ostringstream source;
    source << "\n// Linked user fragment inputs\n";
    for (const auto &varying : artifact.varyings) {
        source << "layout(location = " << varying.location << ") " << Qualifier(varying.interpolation) << "in "
               << varying.glslType << " " << VariableName(varying.name) << ";\n";
    }
    source << "struct FragmentInput {\n";
    for (const auto &varying : artifact.varyings)
        source << "    " << varying.glslType << " " << varying.name << ";\n";
    source << "};\n"
              "FragmentInput fragmentInput;\n";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitStandaloneVertexOutputPreview(const ShaderDescriptor &vertex)
{
    std::ostringstream source;
    source << "\n// Standalone preview of linked vertex outputs\n";
    source << "struct VertexOutput {\n";
    for (const auto &varying : vertex.outputs) {
        const auto type = GlslType(varying.type);
        if (!type.empty())
            source << "    " << type << " " << varying.name << ";\n";
    }
    source << "};\n";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitStandaloneFragmentInputPreview(const ShaderDescriptor &fragment)
{
    std::ostringstream source;
    source << "\n// Standalone preview of linked fragment inputs\n";
    source << "struct FragmentInput {\n";
    std::vector<std::string_view> members;
    for (const auto &varying : fragment.inputs) {
        const auto type = GlslType(varying.type);
        if (type.empty())
            continue;
        source << "    " << type << " " << varying.name << ";\n";
        members.push_back(type);
    }
    source << "};\nFragmentInput fragmentInput";
    if (!members.empty()) {
        source << " = FragmentInput(";
        for (size_t index = 0; index < members.size(); ++index) {
            if (index > 0)
                source << ", ";
            source << DefaultGlslLiteral(members[index]);
        }
        source << ")";
    }
    source << ";\n";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitVertexCall(const ShaderProgramInterfaceArtifact &artifact,
                                                      const ShaderDescriptor &vertex)
{
    if (vertex.outputs.empty())
        return "    inxVertexEntry(v);\n";

    std::ostringstream source;
    source << "    VertexOutput _inx_output = inxVertexEntry(v);\n";
    for (const auto &varying : artifact.varyings)
        source << "    " << VariableName(varying.name) << " = _inx_output." << varying.name << ";\n";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitSurfaceCall(const ShaderProgramInterfaceArtifact &artifact)
{
    if (artifact.varyings.empty())
        return "    surface(s);";

    std::ostringstream source;
    for (const auto &varying : artifact.varyings)
        source << "    fragmentInput." << varying.name << " = " << VariableName(varying.name) << ";\n";
    source << "    surface(s);";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitTextureDeclarations(const ShaderProgramInterfaceArtifact &artifact,
                                                               ShaderStageVisibility stage, uint32_t descriptorSet,
                                                               uint32_t firstTextureBinding)
{
    std::ostringstream source;
    for (const auto &property : artifact.properties) {
        if (!property.textureSlot || !HasVisibility(property.visibility, stage))
            continue;
        source << "layout(set = " << descriptorSet << ", binding = " << (firstTextureBinding + *property.textureSlot)
               << ") uniform sampler2D " << property.schema.name << ";\n";
    }
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitMaterialBlockMembers(const ShaderProgramInterfaceArtifact &artifact)
{
    std::ostringstream source;
    for (const auto &property : artifact.properties) {
        if (!property.bufferOffset)
            continue;
        const auto type = GlslType(property.schema.type);
        if (!type.empty())
            source << "    " << type << " " << property.schema.name << ";\n";
    }
    if (artifact.alphaClipThresholdOffset)
        source << "    float _AlphaClipThreshold;\n";
    return source.str();
}

} // namespace infernux
