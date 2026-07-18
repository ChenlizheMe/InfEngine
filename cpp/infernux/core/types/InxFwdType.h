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
    Audio,    // Audio (.wav) - audio clip for playback via AudioSource
    DefaultText,
    DefaultBinary,
    PhysicMaterial, // Physics surface material (.physicMaterial)
    RenderEffect    // Reusable render effect or effect group source document
};

} // namespace infernux
