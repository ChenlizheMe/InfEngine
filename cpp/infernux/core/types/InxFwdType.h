#pragma once

namespace infernux
{
// ----------------------------------
// Resource Type Enumeration
// ----------------------------------
enum class ResourceType
{
    Meta = -1,
    Shader,
    Texture,
    Mesh,
    Material, // Material (.mat) - JSON file with vert + frag shader paths
    Script,   // Python script (.py) - for component scripts and editor tools
    Audio,    // Decoded audio clip (.wav/.ogg/.mp3/.flac) for AudioSource
    DefaultText,
    DefaultBinary,
    PhysicMaterial, // Physics surface material (.physicMaterial)
    RenderEffect,   // Reusable render effect or effect group source document
    ParticleGraph   // GPU particle authoring graph (.particlegraph)
};

} // namespace infernux
