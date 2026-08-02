#pragma once

#include <glm/glm.hpp>

/**
 * @file ColorSpace.h
 * @brief sRGB <-> linear conversions for the linear rendering pipeline.
 *
 * Convention (matches Unity's linear color space):
 *  - Authored colors (color pickers, serialized Color properties, light
 *    colors) are stored in sRGB on the CPU — what the user picks is what
 *    the value holds.
 *  - The GPU shading pipeline works in linear space. Every color that
 *    crosses the CPU->GPU boundary (material UBOs, lighting UBOs, per
 *    instance colors) must be converted with SrgbToLinear at pack time.
 *  - sRGB textures are decoded in hardware (VK_FORMAT_*_SRGB), and the
 *    final image is encoded back once by the display_encode pass.
 */

namespace inx::color
{

inline float SrgbToLinear(float c)
{
    return (c <= 0.04045f) ? c / 12.92f : glm::pow((c + 0.055f) / 1.055f, 2.4f);
}

inline float LinearToSrgb(float c)
{
    return (c <= 0.0031308f) ? c * 12.92f : 1.055f * glm::pow(c, 1.0f / 2.4f) - 0.055f;
}

inline glm::vec3 SrgbToLinear(const glm::vec3 &c)
{
    return {SrgbToLinear(c.x), SrgbToLinear(c.y), SrgbToLinear(c.z)};
}

inline glm::vec3 LinearToSrgb(const glm::vec3 &c)
{
    return {LinearToSrgb(c.x), LinearToSrgb(c.y), LinearToSrgb(c.z)};
}

/// Converts rgb, preserves alpha (alpha is coverage, never gamma-encoded).
inline glm::vec4 SrgbToLinear(const glm::vec4 &c)
{
    return {SrgbToLinear(c.x), SrgbToLinear(c.y), SrgbToLinear(c.z), c.w};
}

inline glm::vec4 LinearToSrgb(const glm::vec4 &c)
{
    return {LinearToSrgb(c.x), LinearToSrgb(c.y), LinearToSrgb(c.z), c.w};
}

} // namespace inx::color
