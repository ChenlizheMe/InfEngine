#pragma once

#include <glm/glm.hpp>
#include <nlohmann/json.hpp>
#include <string>

namespace infernux
{

/**
 * @brief Per-scene environment (lighting) settings — Unity-style.
 *
 * Owned by @ref Scene and serialized into the scene document under the
 * "environment" key. The renderer reads these every frame:
 *  - The skybox pass draws @ref skyboxMaterialGuid (empty = the builtin
 *    procedural sky material "SkyboxProcedural").
 *  - Ambient lighting is derived according to @ref ambientSource.
 *
 * All colors are authored in sRGB (what the color picker shows); the
 * conversion to linear happens at the CPU->GPU boundary
 * (SceneLightCollector::SetAmbient*).
 */
struct SceneEnvironmentSettings
{
    /// Ambient light source, mirrors Unity's Environment Lighting > Source.
    enum class AmbientSource : int
    {
        Skybox = 0,   ///< Derive ambient gradient from the skybox material.
        Gradient = 1, ///< Explicit sky/equator/ground tri-color gradient.
        Color = 2,    ///< Single flat ambient color.
    };

    /// GUID of the skybox material asset. Empty = builtin procedural sky.
    std::string skyboxMaterialGuid;

    // Parameters for the builtin procedural sky (used only when
    // skyboxMaterialGuid is empty). The renderer pushes these onto the
    // builtin "SkyboxProcedural" material every frame, so they persist with
    // the scene instead of living on the shared builtin material instance.
    // Colors are authored sRGB. Defaults: #6E7E9C / #A6B9D0 / #585858.
    glm::vec3 skyTopColor{0.431f, 0.494f, 0.612f};
    glm::vec3 skyHorizonColor{0.651f, 0.725f, 0.816f};
    glm::vec3 skyGroundColor{0.345f, 0.345f, 0.345f};
    float skyExposure = 1.0f;

    int ambientSource = static_cast<int>(AmbientSource::Skybox);
    float ambientIntensity = 1.0f;

    // Used when ambientSource == Color
    glm::vec3 ambientColor{0.212f, 0.227f, 0.259f};

    // Used when ambientSource == Gradient
    glm::vec3 ambientSkyColor{0.212f, 0.227f, 0.259f};
    glm::vec3 ambientEquatorColor{0.114f, 0.125f, 0.133f};
    glm::vec3 ambientGroundColor{0.047f, 0.043f, 0.035f};

    [[nodiscard]] nlohmann::json ToJson() const
    {
        nlohmann::json j;
        j["skyboxMaterialGuid"] = skyboxMaterialGuid;
        j["skyTopColor"] = {skyTopColor.r, skyTopColor.g, skyTopColor.b};
        j["skyHorizonColor"] = {skyHorizonColor.r, skyHorizonColor.g, skyHorizonColor.b};
        j["skyGroundColor"] = {skyGroundColor.r, skyGroundColor.g, skyGroundColor.b};
        j["skyExposure"] = skyExposure;
        j["ambientSource"] = ambientSource;
        j["ambientIntensity"] = ambientIntensity;
        j["ambientColor"] = {ambientColor.r, ambientColor.g, ambientColor.b};
        j["ambientSkyColor"] = {ambientSkyColor.r, ambientSkyColor.g, ambientSkyColor.b};
        j["ambientEquatorColor"] = {ambientEquatorColor.r, ambientEquatorColor.g, ambientEquatorColor.b};
        j["ambientGroundColor"] = {ambientGroundColor.r, ambientGroundColor.g, ambientGroundColor.b};
        return j;
    }

    static SceneEnvironmentSettings FromJson(const nlohmann::json &j)
    {
        SceneEnvironmentSettings env;
        if (!j.is_object())
            return env;
        const auto readColor = [&](const char *key, glm::vec3 &out) {
            if (j.contains(key) && j[key].is_array() && j[key].size() >= 3 && j[key][0].is_number() &&
                j[key][1].is_number() && j[key][2].is_number()) {
                out = glm::vec3(j[key][0].get<float>(), j[key][1].get<float>(), j[key][2].get<float>());
            }
        };
        if (j.contains("skyboxMaterialGuid") && j["skyboxMaterialGuid"].is_string())
            env.skyboxMaterialGuid = j["skyboxMaterialGuid"].get<std::string>();
        readColor("skyTopColor", env.skyTopColor);
        readColor("skyHorizonColor", env.skyHorizonColor);
        readColor("skyGroundColor", env.skyGroundColor);
        if (j.contains("skyExposure") && j["skyExposure"].is_number())
            env.skyExposure = glm::clamp(j["skyExposure"].get<float>(), 0.0f, 8.0f);
        if (j.contains("ambientSource") && j["ambientSource"].is_number_integer())
            env.ambientSource = glm::clamp(j["ambientSource"].get<int>(), 0, 2);
        if (j.contains("ambientIntensity") && j["ambientIntensity"].is_number())
            env.ambientIntensity = glm::clamp(j["ambientIntensity"].get<float>(), 0.0f, 8.0f);
        readColor("ambientColor", env.ambientColor);
        readColor("ambientSkyColor", env.ambientSkyColor);
        readColor("ambientEquatorColor", env.ambientEquatorColor);
        readColor("ambientGroundColor", env.ambientGroundColor);
        return env;
    }
};

} // namespace infernux
