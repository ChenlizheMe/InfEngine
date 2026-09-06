#pragma once

#include <string>

namespace infernux
{

struct ShaderAssetReference
{
    std::string guid;
    std::string shaderId;
    std::string pathHint;

    [[nodiscard]] bool IsAssigned() const noexcept
    {
        return !guid.empty() || !shaderId.empty();
    }

    [[nodiscard]] std::string StableKey() const
    {
        return guid.empty() ? shaderId : guid;
    }

    friend bool operator==(const ShaderAssetReference &lhs, const ShaderAssetReference &rhs) noexcept
    {
        return lhs.guid == rhs.guid && lhs.shaderId == rhs.shaderId && lhs.pathHint == rhs.pathHint;
    }
};

/// @brief True when the two references identify the same shader *asset*.
/// GUID is the only durable identity: moving the file refreshes pathHint and
/// shader metadata may refresh shaderId, neither of which changes the asset.
/// Symbolic built-in shaders identify themselves by shaderId because they do
/// not carry an asset GUID.
[[nodiscard]] inline bool ReferencesSameShader(const ShaderAssetReference &lhs,
                                               const ShaderAssetReference &rhs) noexcept
{
    if (!lhs.guid.empty() && !rhs.guid.empty())
        return lhs.guid == rhs.guid;
    return lhs.shaderId == rhs.shaderId;
}

} // namespace infernux
