#include "GlslStageInterfaceEmitter.h"

#include <sstream>
#include <string_view>

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
    source << "};\n";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitVertexCall(const ShaderProgramInterfaceArtifact &artifact,
                                                      const ShaderDescriptor &vertex)
{
    if (vertex.outputs.empty())
        return "    vertex(v);\n";

    std::ostringstream source;
    source << "    VertexOutput output;\n";
    source << "    vertex(v, output);\n";
    for (const auto &varying : artifact.varyings)
        source << "    " << VariableName(varying.name) << " = output." << varying.name << ";\n";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitSurfaceCall(const ShaderProgramInterfaceArtifact &artifact)
{
    if (artifact.varyings.empty())
        return "    surface(s);";

    std::ostringstream source;
    source << "    FragmentInput input;\n";
    for (const auto &varying : artifact.varyings)
        source << "    input." << varying.name << " = " << VariableName(varying.name) << ";\n";
    source << "    surface(input, s);";
    return source.str();
}

std::string GlslStageInterfaceEmitter::EmitTextureDeclarations(const ShaderProgramInterfaceArtifact &artifact,
                                                               ShaderStageVisibility stage)
{
    std::ostringstream source;
    for (const auto &property : artifact.properties) {
        if (!property.textureSlot || !HasVisibility(property.visibility, stage))
            continue;
        source << "layout(set = " << MaterialDescriptorSet
               << ", binding = " << (FirstTextureBinding + *property.textureSlot) << ") uniform sampler2D "
               << property.schema.name << ";\n";
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
