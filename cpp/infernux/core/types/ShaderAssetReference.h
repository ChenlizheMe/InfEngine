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

} // namespace infernux
