#include "WebSceneRenderer.h"

#include <core/types/ColorSpace.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxTexture/InxTexture.h>
#include <function/scene/Camera.h>
#include <function/scene/GameObject.h>
#include <function/scene/Light.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>
#include <function/scene/Transform.h>

#include <glm/gtc/matrix_inverse.hpp>
#include <glm/gtc/matrix_transform.hpp>

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <optional>

namespace infernux::web
{
namespace
{

constexpr uint32_t kLineVertexMarker = 0x4C494E45u;
constexpr uint32_t kMaxPunctualLights = 8u;

constexpr char kSceneShader[] = R"wgsl(
struct PunctualLightData {
    position_range: vec4<f32>,
    color_intensity: vec4<f32>,
    direction_outer_cos: vec4<f32>,
    parameters: vec4<f32>,
};

struct CameraData {
    view_projection: mat4x4<f32>,
    inverse_view_projection: mat4x4<f32>,
    light_view_projection: mat4x4<f32>,
    camera_position: vec4<f32>,
    light_direction_strength: vec4<f32>,
    light_color_intensity: vec4<f32>,
    sky_top_exposure: vec4<f32>,
    sky_horizon: vec4<f32>,
    sky_ground: vec4<f32>,
    ambient: vec4<f32>,
    ambient_sky: vec4<f32>,
    ambient_equator: vec4<f32>,
    ambient_ground: vec4<f32>,
    light_counts: vec4<u32>,
    punctual_lights: array<PunctualLightData, 8>,
};

@group(0) @binding(0) var<uniform> camera: CameraData;
@group(0) @binding(1) var shadow_map: texture_depth_2d;
@group(0) @binding(2) var shadow_sampler: sampler_comparison;

struct VertexInput {
    @location(0) position: vec3<f32>,
    @location(1) normal: vec3<f32>,
    @location(2) uv: vec2<f32>,
    @location(3) color: vec4<f32>,
    @location(4) emission: vec4<f32>,
    @location(5) material: vec4<f32>,
    @location(6) surface: vec4<f32>,
    @location(7) tangent: vec4<f32>,
};

struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) normal: vec3<f32>,
    @location(1) color: vec4<f32>,
    @location(2) world_position: vec3<f32>,
    @location(3) shadow_position: vec4<f32>,
    @location(4) emission: vec4<f32>,
    @location(5) material: vec4<f32>,
    @location(6) surface: vec4<f32>,
    @location(7) uv: vec2<f32>,
    @location(8) tangent: vec4<f32>,
};

@group(1) @binding(0) var material_base_color_sampler: sampler;
@group(1) @binding(1) var material_base_color: texture_2d<f32>;
@group(1) @binding(2) var material_metallic_sampler: sampler;
@group(1) @binding(3) var material_metallic_map: texture_2d<f32>;
@group(1) @binding(4) var material_smoothness_sampler: sampler;
@group(1) @binding(5) var material_smoothness_map: texture_2d<f32>;
@group(1) @binding(6) var material_ao_sampler: sampler;
@group(1) @binding(7) var material_ao_map: texture_2d<f32>;
@group(1) @binding(8) var material_normal_sampler: sampler;
@group(1) @binding(9) var material_normal_map: texture_2d<f32>;

@vertex
fn vertex_main(input: VertexInput) -> VertexOutput {
    var output: VertexOutput;
    output.position = camera.view_projection * vec4<f32>(input.position, 1.0);
    output.normal = input.normal;
    output.color = input.color;
    output.world_position = input.position;
    output.shadow_position = camera.light_view_projection * vec4<f32>(input.position, 1.0);
    output.emission = input.emission;
    output.material = input.material;
    output.surface = input.surface;
    output.uv = input.uv;
    output.tangent = input.tangent;
    return output;
}

const PI: f32 = 3.14159265358979323846;

fn fresnel_schlick(f0: vec3<f32>, f90: f32, u: f32) -> vec3<f32> {
    let x = 1.0 - u;
    let x2 = x * x;
    let x5 = x * x2 * x2;
    return f0 * (1.0 - x5) + vec3<f32>(f90 * x5);
}

fn fresnel_schlick_roughness(f0: vec3<f32>, f90: f32, ndotv: f32,
                             perceptual_roughness: f32) -> vec3<f32> {
    let grazing = max(vec3<f32>(f90 * (1.0 - perceptual_roughness)), f0);
    let x = 1.0 - ndotv;
    let x2 = x * x;
    let x5 = x * x2 * x2;
    return f0 + (grazing - f0) * x5;
}

fn env_brdf_approx(perceptual_roughness: f32, ndotv: f32) -> vec2<f32> {
    let c0 = vec4<f32>(-1.0, -0.0275, -0.572, 0.022);
    let c1 = vec4<f32>(1.0, 0.0425, 1.04, -0.04);
    let r = perceptual_roughness * c0 + c1;
    let a004 = min(r.x * r.x, exp2(-9.28 * ndotv)) * r.x + r.y;
    return vec2<f32>(-1.04, 1.04) * a004 + r.zw;
}

fn sky_gradient(y: f32, sky: vec3<f32>, equator: vec3<f32>, ground: vec3<f32>) -> vec3<f32> {
    let base = mix(ground, sky, smoothstep(-0.10, 0.45, y));
    var horizon_mask = 0.0;
    if (y < 0.0) {
        let side = 1.0 - smoothstep(0.0, 0.10, -y);
        horizon_mask = side * side;
    } else {
        horizon_mask = 1.0 - smoothstep(0.0, 0.45, y);
    }
    return mix(base, equator, horizon_mask * 0.35);
}

fn sample_ambient_probe(direction: vec3<f32>) -> vec3<f32> {
    if (camera.ambient_equator.w < 0.5) {
        return max(camera.ambient.rgb * camera.ambient.w, vec3<f32>(0.0));
    }
    return max(sky_gradient(direction.y, camera.ambient_sky.rgb,
                            camera.ambient_equator.rgb, camera.ambient_ground.rgb),
               vec3<f32>(0.0));
}

fn sample_ambient_irradiance(direction: vec3<f32>) -> vec3<f32> {
    if (camera.ambient_equator.w < 0.5) {
        return max(camera.ambient.rgb * camera.ambient.w, vec3<f32>(0.0));
    }
    let y = clamp(direction.y, -1.0, 1.0);
    if (y >= 0.0) {
        return max(mix(camera.ambient_equator.rgb, camera.ambient_sky.rgb, y), vec3<f32>(0.0));
    }
    return max(mix(camera.ambient_equator.rgb, camera.ambient_ground.rgb, -y), vec3<f32>(0.0));
}

fn specular_ambient_direction(normal: vec3<f32>, view_direction: vec3<f32>,
                              perceptual_roughness: f32) -> vec3<f32> {
    let reflection = reflect(-view_direction, normal);
    let roughness = perceptual_roughness * perceptual_roughness;
    let factor = (1.0 - roughness) * (sqrt(max(1.0 - roughness, 0.0)) + roughness);
    return normalize(mix(normal, reflection, clamp(factor, 0.0, 1.0)));
}

fn specular_occlusion(ndotv: f32, occlusion: f32, perceptual_roughness: f32) -> f32 {
    return clamp(pow(ndotv + occlusion, exp2(-16.0 * perceptual_roughness - 1.0))
                 - 1.0 + occlusion, 0.0, 1.0);
}

fn geometric_specular_aa(world_normal: vec3<f32>, roughness: f32) -> f32 {
    let du = dpdx(world_normal);
    let dv = dpdy(world_normal);
    let variance = 0.25 * (dot(du, du) + dot(dv, dv));
    let kernel_roughness = min(variance, 0.18);
    return max(roughness, sqrt(roughness * roughness + kernel_roughness));
}

fn horizon_occlusion(reflection_direction: vec3<f32>, geometric_normal: vec3<f32>) -> f32 {
    let horizon = clamp(1.0 + dot(reflection_direction, geometric_normal), 0.0, 1.0);
    return horizon * horizon;
}

fn disney_diffuse(ndotv: f32, ndotl: f32, ldotv: f32, perceptual_roughness: f32) -> f32 {
    let fd90 = 0.5 + perceptual_roughness * (1.0 + ldotv);
    let light_x = 1.0 - ndotl;
    let view_x = 1.0 - ndotv;
    let light_scatter = 1.0 + (fd90 - 1.0) * light_x * light_x * light_x * light_x * light_x;
    let view_scatter = 1.0 + (fd90 - 1.0) * view_x * view_x * view_x * view_x * view_x;
    return (1.0 / PI) * (1.0 / 1.03571) * light_scatter * view_scatter;
}

fn dv_smith_joint_ggx(ndoth: f32, ndotl: f32, ndotv: f32, roughness: f32) -> f32 {
    let a2 = roughness * roughness;
    let s = (ndoth * a2 - ndoth) * ndoth + 1.0;
    let lambda_v = ndotl * sqrt(max((-ndotv * a2 + ndotv) * ndotv + a2, 1.0e-7));
    let lambda_l = ndotv * sqrt(max((-ndotl * a2 + ndotl) * ndotl + a2, 1.0e-7));
    return (1.0 / PI) * 0.5 * a2 / max(s * s * (lambda_v + lambda_l), 1.0e-7);
}

fn evaluate_pbr_light(normal: vec3<f32>, view_direction: vec3<f32>, light_direction: vec3<f32>,
                      radiance: vec3<f32>, albedo: vec3<f32>, metallic: f32,
                      perceptual_roughness: f32, roughness: f32, f0: vec3<f32>, f90: f32,
                      energy_compensation: vec3<f32>, specular_highlights: f32) -> vec3<f32> {
    let half_vector_sum = view_direction + light_direction;
    var half_vector = normal;
    if (dot(half_vector_sum, half_vector_sum) > 1.0e-8) {
        half_vector = normalize(half_vector_sum);
    }
    let ndotl = max(dot(normal, light_direction), 0.0);
    if (ndotl <= 0.0) {
        return vec3<f32>(0.0);
    }
    let ndotv = max(dot(normal, view_direction), 0.0);
    let ndoth = max(dot(normal, half_vector), 0.0);
    let ldoth = max(dot(light_direction, half_vector), 0.0);
    let ldotv = max(dot(light_direction, view_direction), 0.0);
    let fresnel = fresnel_schlick(f0, f90, ldoth);
    let specular = fresnel * dv_smith_joint_ggx(ndoth, ndotl, ndotv, roughness)
                   * energy_compensation * clamp(specular_highlights, 0.0, 1.0);
    let diffuse = albedo * (1.0 - metallic) *
                  disney_diffuse(ndotv, ndotl, ldotv, perceptual_roughness);
    return (diffuse + specular) * radiance * (ndotl * PI);
}

fn punctual_attenuation(distance_to_light: f32, range: f32) -> f32 {
    let safe_range = max(range, 1.0e-4);
    let distance_squared = distance_to_light * distance_to_light;
    let ratio_squared = distance_squared / (safe_range * safe_range);
    let factor = clamp(1.0 - ratio_squared * ratio_squared, 0.0, 1.0);
    return factor * factor / (distance_squared + 1.0);
}

fn toon_step(value: f32, threshold: f32, softness: f32) -> f32 {
    let width = max(softness, 0.0001);
    return smoothstep(threshold - width, threshold + width, value);
}

fn evaluate_toon_light(normal: vec3<f32>, view_direction: vec3<f32>,
                       light_direction: vec3<f32>, radiance: vec3<f32>,
                       albedo: vec3<f32>, threshold: f32, softness: f32,
                       smoothness: f32, specular_strength: f32) -> vec3<f32> {
    let ndotl = max(dot(normal, light_direction), 0.0);
    let diffuse_band = toon_step(ndotl, threshold, softness);
    let half_sum = view_direction + light_direction;
    var half_vector = normal;
    if (dot(half_sum, half_sum) > 1.0e-8) {
        half_vector = normalize(half_sum);
    }
    let exponent = mix(12.0, 256.0, clamp(smoothness, 0.0, 1.0));
    let raw_specular = pow(max(dot(normal, half_vector), 0.0), exponent);
    let specular_band = toon_step(raw_specular, 0.5, softness * 0.5);
    let diffuse = albedo * mix(0.18, 1.0, diffuse_band);
    let specular = vec3<f32>(specular_band * clamp(specular_strength, 0.0, 1.0));
    return (diffuse + specular) * radiance * select(0.0, 1.0, ndotl >= 0.0001);
}

fn sample_shadow(position: vec4<f32>, normal: vec3<f32>) -> f32 {
    if (camera.light_direction_strength.w <= 0.0 || position.w <= 0.0) {
        return 1.0;
    }
    let ndc = position.xyz / position.w;
    let uv = vec2<f32>(ndc.x * 0.5 + 0.5, 0.5 - ndc.y * 0.5);
    if (ndc.z <= 0.0 || ndc.z >= 1.0 || any(uv < vec2<f32>(0.0)) || any(uv > vec2<f32>(1.0))) {
        return 1.0;
    }
    let toward_light = normalize(camera.light_direction_strength.xyz);
    let normal_bias = 0.0015 * (1.0 - max(dot(normal, toward_light), 0.0));
    let texel = 1.0 / vec2<f32>(textureDimensions(shadow_map));
    var visibility = 0.0;
    for (var y = -1; y <= 1; y = y + 1) {
        for (var x = -1; x <= 1; x = x + 1) {
            visibility += textureSampleCompareLevel(shadow_map, shadow_sampler,
                                                     uv + vec2<f32>(f32(x), f32(y)) * texel,
                                                     ndc.z - 0.0008 - normal_bias);
        }
    }
    let filtered = visibility / 9.0;
    return mix(1.0 - camera.light_direction_strength.w, 1.0, filtered);
}

fn sample_material_normal(input: VertexOutput, geometric_normal: vec3<f32>) -> vec3<f32> {
    var tangent = input.tangent.xyz - geometric_normal * dot(input.tangent.xyz, geometric_normal);
    if (dot(tangent, tangent) <= 1.0e-8) {
        let fallback = select(vec3<f32>(1.0, 0.0, 0.0), vec3<f32>(0.0, 0.0, 1.0),
                              abs(geometric_normal.y) > 0.999);
        tangent = normalize(cross(fallback, geometric_normal));
    } else {
        tangent = normalize(tangent);
    }
    let handedness = select(-1.0, 1.0, input.tangent.w >= 0.0);
    let bitangent = normalize(cross(geometric_normal, tangent)) * handedness;
    var encoded = textureSample(material_normal_map, material_normal_sampler, input.uv).rg * 2.0 - 1.0;
    encoded *= abs(input.tangent.w);
    let normal_z = sqrt(max(1.0 - dot(encoded, encoded), 0.0));
    return normalize(tangent * encoded.x + bitangent * encoded.y + geometric_normal * normal_z);
}

@fragment
fn fragment_main(input: VertexOutput) -> @location(0) vec4<f32> {
    let geometric_normal = normalize(input.normal);
    let normal = sample_material_normal(input, geometric_normal);
    let sampled_color = textureSample(material_base_color, material_base_color_sampler, input.uv);
    let surface_color = input.color * sampled_color;
    if (input.surface.w >= 0.0 && surface_color.a < input.surface.w) {
        discard;
    }
    let sampled_metallic = textureSample(material_metallic_map, material_metallic_sampler, input.uv).r;
    let sampled_smoothness = textureSample(material_smoothness_map, material_smoothness_sampler, input.uv).r;
    let sampled_occlusion = textureSample(material_ao_map, material_ao_sampler, input.uv).r;
    var perceptual_roughness = clamp(1.0 - input.material.y * sampled_smoothness, 0.045, 1.0);
    var roughness = perceptual_roughness * perceptual_roughness;
    // Derivatives must execute in uniform fragment control flow on WebGPU.
    // Evaluate geometric AA before the per-material unlit early return.
    roughness = geometric_specular_aa(normal, roughness);
    perceptual_roughness = max(perceptual_roughness, sqrt(roughness));
    if (input.surface.x > 0.5 && input.surface.x < 1.5) {
        return vec4<f32>(surface_color.rgb + input.emission.rgb * input.emission.a, surface_color.a);
    }
    let view_direction = normalize(camera.camera_position.xyz - input.world_position);
    if (input.surface.x >= 1.5) {
        let toward_light = normalize(camera.light_direction_strength.xyz);
        let shadow = sample_shadow(input.shadow_position, normal);
        let radiance = camera.light_color_intensity.rgb * camera.light_color_intensity.w;
        var direct = evaluate_toon_light(
            normal, view_direction, toward_light, radiance, surface_color.rgb,
            clamp(input.surface.y, 0.0, 1.0), clamp(input.surface.z, 0.0, 0.25),
            input.material.y, input.material.w) * shadow;
        for (var light_index = 0u; light_index < camera.light_counts.x; light_index = light_index + 1u) {
            let light = camera.punctual_lights[light_index];
            let to_light = light.position_range.xyz - input.world_position;
            let distance_to_light = length(to_light);
            if (distance_to_light <= 1.0e-5 || distance_to_light >= light.position_range.w) {
                continue;
            }
            let local_direction = to_light / distance_to_light;
            var cone = 1.0;
            if (light.parameters.y > 0.5) {
                let theta = dot(local_direction, -normalize(light.direction_outer_cos.xyz));
                cone = clamp((theta - light.direction_outer_cos.w) /
                             max(light.parameters.x - light.direction_outer_cos.w, 1.0e-4), 0.0, 1.0);
            }
            let attenuation = punctual_attenuation(distance_to_light, light.position_range.w) * cone;
            direct += evaluate_toon_light(
                normal, view_direction, local_direction,
                light.color_intensity.rgb * light.color_intensity.w * attenuation,
                surface_color.rgb, clamp(input.surface.y, 0.0, 1.0),
                clamp(input.surface.z, 0.0, 0.25), input.material.y, input.material.w);
        }
        let ambient = sample_ambient_irradiance(normal) * surface_color.rgb
                      * clamp(input.material.z, 0.0, 1.0) * 0.65;
        return vec4<f32>(ambient + direct + input.emission.rgb * input.emission.a,
                         surface_color.a);
    }
    let ndotv = max(dot(normal, view_direction), 0.0);
    let metallic = clamp(input.material.x * sampled_metallic, 0.0, 1.0);
    let occlusion = clamp(input.material.z * sampled_occlusion, 0.0, 1.0);
    let specular_highlights = clamp(input.material.w, 0.0, 1.0);
    let f0 = mix(vec3<f32>(0.04), surface_color.rgb, vec3<f32>(metallic));
    let f90 = clamp(50.0 * dot(f0, vec3<f32>(0.33333)), 0.0, 1.0);
    let env_brdf = env_brdf_approx(perceptual_roughness, ndotv);
    let reflectivity = env_brdf.x + env_brdf.y;
    let energy_compensation = vec3<f32>(1.0) + f0 * (1.0 / max(reflectivity, 0.001) - 1.0);
    let toward_light = normalize(camera.light_direction_strength.xyz);
    let shadow = sample_shadow(input.shadow_position, normal);
    let radiance = camera.light_color_intensity.rgb * camera.light_color_intensity.w;
    var direct = evaluate_pbr_light(normal, view_direction, toward_light, radiance, surface_color.rgb,
                                    metallic, perceptual_roughness, roughness, f0, f90,
                                    energy_compensation, specular_highlights) * shadow;

    for (var light_index = 0u; light_index < camera.light_counts.x; light_index = light_index + 1u) {
        let light = camera.punctual_lights[light_index];
        let to_light = light.position_range.xyz - input.world_position;
        let distance_to_light = length(to_light);
        if (distance_to_light <= 1.0e-5 || distance_to_light >= light.position_range.w) {
            continue;
        }
        let local_direction = to_light / distance_to_light;
        var cone = 1.0;
        if (light.parameters.y > 0.5) {
            let theta = dot(local_direction, -normalize(light.direction_outer_cos.xyz));
            cone = clamp((theta - light.direction_outer_cos.w) /
                         max(light.parameters.x - light.direction_outer_cos.w, 1.0e-4), 0.0, 1.0);
        }
        let attenuation = punctual_attenuation(distance_to_light, light.position_range.w) * cone;
        let local_radiance = light.color_intensity.rgb * light.color_intensity.w * attenuation;
        direct += evaluate_pbr_light(normal, view_direction, local_direction, local_radiance,
                                     surface_color.rgb, metallic, perceptual_roughness, roughness, f0, f90,
                                     energy_compensation, specular_highlights);
    }
    let diffuse_irradiance = sample_ambient_irradiance(normal);
    let reflection_direction = specular_ambient_direction(normal, view_direction, perceptual_roughness);
    let prefilter = clamp(perceptual_roughness * (1.7 - 0.7 * perceptual_roughness), 0.0, 1.0);
    let specular_irradiance = mix(sample_ambient_probe(reflection_direction),
                                  sample_ambient_irradiance(reflection_direction), prefilter);
    let environment_fresnel = fresnel_schlick_roughness(f0, f90, ndotv, perceptual_roughness);
    let diffuse_weight = (vec3<f32>(1.0) - environment_fresnel) * (1.0 - metallic);
    let ambient_diffuse = diffuse_weight * surface_color.rgb * diffuse_irradiance * occlusion;
    let ambient_specular = specular_irradiance * (f0 * env_brdf.x + vec3<f32>(env_brdf.y))
                           * energy_compensation
                           * specular_occlusion(ndotv, occlusion, perceptual_roughness)
                           * horizon_occlusion(reflection_direction, normal)
                           * specular_highlights;
    let emission = input.emission.rgb * input.emission.a;
    return vec4<f32>(ambient_diffuse + ambient_specular + direct + emission,
                     surface_color.a);
}
)wgsl";

constexpr char kSkyShader[] = R"wgsl(
struct CameraData {
    view_projection: mat4x4<f32>,
    inverse_view_projection: mat4x4<f32>,
    light_view_projection: mat4x4<f32>,
    camera_position: vec4<f32>,
    light_direction_strength: vec4<f32>,
    light_color_intensity: vec4<f32>,
    sky_top_exposure: vec4<f32>,
    sky_horizon: vec4<f32>,
    sky_ground: vec4<f32>,
    ambient: vec4<f32>,
};
@group(0) @binding(0) var<uniform> camera: CameraData;

struct SkyOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) clip_position: vec2<f32>,
};

@vertex
fn vertex_main(@builtin(vertex_index) index: u32) -> SkyOutput {
    let positions = array<vec2<f32>, 3>(
        vec2<f32>(-1.0, -1.0), vec2<f32>(3.0, -1.0), vec2<f32>(-1.0, 3.0));
    var output: SkyOutput;
    output.position = vec4<f32>(positions[index], 1.0, 1.0);
    output.clip_position = positions[index];
    return output;
}

@fragment
fn fragment_main(input: SkyOutput) -> @location(0) vec4<f32> {
    let far_world = camera.inverse_view_projection * vec4<f32>(input.clip_position, 1.0, 1.0);
    let direction = normalize(far_world.xyz / far_world.w - camera.camera_position.xyz);
    let y = direction.y;
    let base = mix(camera.sky_ground.rgb, camera.sky_top_exposure.rgb,
                   smoothstep(-0.10, 0.45, y));
    var horizon_mask = 0.0;
    if (y < 0.0) {
        let side = 1.0 - smoothstep(0.0, 0.10, -y);
        horizon_mask = side * side;
    } else {
        horizon_mask = 1.0 - smoothstep(0.0, 0.45, y);
    }
    var color = mix(base, camera.sky_horizon.rgb, horizon_mask * 0.35);
    let horizon_glow = pow(1.0 - abs(y), 8.0) * 0.15;
    color += vec3<f32>(horizon_glow);
    return vec4<f32>(color * camera.sky_top_exposure.w, 1.0);
}
)wgsl";

constexpr char kShadowShader[] = R"wgsl(
struct CameraData {
    view_projection: mat4x4<f32>,
    inverse_view_projection: mat4x4<f32>,
    light_view_projection: mat4x4<f32>,
    camera_position: vec4<f32>,
    light_direction_strength: vec4<f32>,
    light_color_intensity: vec4<f32>,
    sky_top_exposure: vec4<f32>,
    sky_horizon: vec4<f32>,
    sky_ground: vec4<f32>,
    ambient: vec4<f32>,
};
@group(0) @binding(0) var<uniform> camera: CameraData;

@vertex
fn vertex_main(@location(0) position: vec3<f32>) -> @builtin(position) vec4<f32> {
    return camera.light_view_projection * vec4<f32>(position, 1.0);
}
)wgsl";

glm::vec4 MaterialColor(const std::shared_ptr<InxMaterial> &material)
{
    if (!material)
        return glm::vec4(1.0f);
    const MaterialProperty *property = material->GetProperty("baseColor");
    if (!property)
        return glm::vec4(1.0f);
    if (property->type == MaterialPropertyType::Color || property->type == MaterialPropertyType::Float4) {
        if (const auto *value = std::get_if<glm::vec4>(&property->value))
            return *value;
    }
    if (property->type == MaterialPropertyType::Float3) {
        if (const auto *value = std::get_if<glm::vec3>(&property->value))
            return glm::vec4(*value, 1.0f);
    }
    return glm::vec4(1.0f);
}

float MaterialFloat(const std::shared_ptr<InxMaterial> &material, const char *name, float fallback)
{
    if (!material)
        return fallback;
    const MaterialProperty *property = material->GetProperty(name);
    if (!property || property->type != MaterialPropertyType::Float)
        return fallback;
    if (const auto *value = std::get_if<float>(&property->value))
        return *value;
    return fallback;
}

glm::vec4 MaterialVector(const std::shared_ptr<InxMaterial> &material, const char *name, const glm::vec4 &fallback)
{
    if (!material)
        return fallback;
    const MaterialProperty *property = material->GetProperty(name);
    if (!property)
        return fallback;
    if (const auto *value = std::get_if<glm::vec4>(&property->value))
        return *value;
    if (const auto *value = std::get_if<glm::vec3>(&property->value))
        return glm::vec4(*value, fallback.w);
    return fallback;
}

template <size_t Size>
std::string MaterialTextureGuid(const std::shared_ptr<InxMaterial> &material,
                                const std::array<const char *, Size> &names, const char *fallback)
{
    if (!material)
        return fallback;
    for (const char *name : names) {
        const MaterialProperty *property = material->GetProperty(name);
        if (!property || property->type != MaterialPropertyType::Texture2D)
            continue;
        if (const auto *guid = std::get_if<std::string>(&property->value); guid && !guid->empty())
            return *guid;
    }
    return fallback;
}

std::array<std::string, 5> MaterialTextureGuids(const std::shared_ptr<InxMaterial> &material)
{
    constexpr std::array<const char *, 5> baseNames = {"baseColorTexture", "albedoMap", "albedoTexture", "mainTexture",
                                                       "texSampler"};
    constexpr std::array<const char *, 3> metallicNames = {"metallicMap", "metalnessMap", "metallicTexture"};
    constexpr std::array<const char *, 2> smoothnessNames = {"smoothnessMap", "smoothnessTexture"};
    constexpr std::array<const char *, 3> aoNames = {"aoMap", "occlusionMap", "ambientOcclusionMap"};
    constexpr std::array<const char *, 3> normalNames = {"normalMap", "normalTexture", "bumpMap"};
    return {MaterialTextureGuid(material, baseNames, "white"), MaterialTextureGuid(material, metallicNames, "white"),
            MaterialTextureGuid(material, smoothnessNames, "white"), MaterialTextureGuid(material, aoNames, "white"),
            MaterialTextureGuid(material, normalNames, "normal")};
}

wgpu::TextureFormat ToWebTextureFormat(TextureFormat format)
{
    switch (format) {
    case TextureFormat::Rgba8UNorm:
        return wgpu::TextureFormat::RGBA8Unorm;
    case TextureFormat::Rgba8Srgb:
        return wgpu::TextureFormat::RGBA8UnormSrgb;
    case TextureFormat::Rgba16UNorm:
        return wgpu::TextureFormat::RGBA16Unorm;
    case TextureFormat::Rgba16Float:
        return wgpu::TextureFormat::RGBA16Float;
    case TextureFormat::Rgba32Float:
        return wgpu::TextureFormat::RGBA32Float;
    default:
        return wgpu::TextureFormat::Undefined;
    }
}

std::array<uint8_t, 4> DecodeRgb565(uint16_t packed)
{
    const uint8_t red = static_cast<uint8_t>((packed >> 11u) & 0x1fu);
    const uint8_t green = static_cast<uint8_t>((packed >> 5u) & 0x3fu);
    const uint8_t blue = static_cast<uint8_t>(packed & 0x1fu);
    return {static_cast<uint8_t>((red << 3u) | (red >> 2u)), static_cast<uint8_t>((green << 2u) | (green >> 4u)),
            static_cast<uint8_t>((blue << 3u) | (blue >> 2u)), 255u};
}

std::array<std::array<uint8_t, 4>, 4> DecodeBcColorTable(const uint8_t *block, bool allowTransparent)
{
    const uint16_t endpoint0 = static_cast<uint16_t>(block[0]) | static_cast<uint16_t>(block[1]) << 8u;
    const uint16_t endpoint1 = static_cast<uint16_t>(block[2]) | static_cast<uint16_t>(block[3]) << 8u;
    std::array<std::array<uint8_t, 4>, 4> table{};
    table[0] = DecodeRgb565(endpoint0);
    table[1] = DecodeRgb565(endpoint1);
    if (!allowTransparent || endpoint0 > endpoint1) {
        for (uint32_t channel = 0; channel < 3; ++channel) {
            table[2][channel] = static_cast<uint8_t>((2u * table[0][channel] + table[1][channel]) / 3u);
            table[3][channel] = static_cast<uint8_t>((table[0][channel] + 2u * table[1][channel]) / 3u);
        }
        table[2][3] = 255u;
        table[3][3] = 255u;
    } else {
        for (uint32_t channel = 0; channel < 3; ++channel)
            table[2][channel] = static_cast<uint8_t>((table[0][channel] + table[1][channel]) / 2u);
        table[2][3] = 255u;
        table[3] = {0u, 0u, 0u, 0u};
    }
    return table;
}

std::array<uint8_t, 8> DecodeBc3AlphaTable(uint8_t endpoint0, uint8_t endpoint1)
{
    std::array<uint8_t, 8> table{endpoint0, endpoint1};
    if (endpoint0 > endpoint1) {
        for (uint32_t index = 1; index <= 6; ++index)
            table[index + 1] = static_cast<uint8_t>(((7u - index) * endpoint0 + index * endpoint1) / 7u);
    } else {
        for (uint32_t index = 1; index <= 4; ++index)
            table[index + 1] = static_cast<uint8_t>(((5u - index) * endpoint0 + index * endpoint1) / 5u);
        table[6] = 0u;
        table[7] = 255u;
    }
    return table;
}

uint64_t DecodeBcScalarIndices(const uint8_t *block)
{
    uint64_t indices = 0;
    for (uint32_t byte = 0; byte < 6; ++byte)
        indices |= static_cast<uint64_t>(block[2 + byte]) << (8u * byte);
    return indices;
}

std::optional<TextureCpuData> DecodeBcTextureToRgba8(const TextureCpuData &source)
{
    const bool bc1 = source.format == TextureFormat::BC1RgbaUNorm || source.format == TextureFormat::BC1RgbaSrgb;
    const bool bc3 = source.format == TextureFormat::BC3UNorm || source.format == TextureFormat::BC3Srgb;
    const bool bc4 = source.format == TextureFormat::BC4UNorm;
    const bool bc5 = source.format == TextureFormat::BC5UNorm;
    if ((!bc1 && !bc3 && !bc4 && !bc5) || source.dimension != TextureDimension::Texture2D || source.mipLevels.empty())
        return std::nullopt;

    TextureCpuData decoded;
    decoded.dimension = TextureDimension::Texture2D;
    decoded.semantic = source.semantic;
    decoded.format = TextureFormatIsSrgb(source.format) ? TextureFormat::Rgba8Srgb : TextureFormat::Rgba8UNorm;

    const uint32_t blockBytes = (bc1 || bc4) ? 8u : 16u;
    for (const TextureMipLevel &mip : source.mipLevels) {
        const uint32_t blockColumns = std::max(1u, (mip.width + 3u) / 4u);
        const uint32_t blockRows = std::max(1u, (mip.height + 3u) / 4u);
        const uint64_t requiredRowPitch = static_cast<uint64_t>(blockColumns) * blockBytes;
        if (mip.width == 0 || mip.height == 0 || mip.depth != 1 || mip.rowPitch < requiredRowPitch ||
            mip.byteOffset > source.bytes.size() || mip.byteSize > source.bytes.size() - mip.byteOffset)
            return std::nullopt;
        const uint64_t requiredBytes = static_cast<uint64_t>(blockRows - 1u) * mip.rowPitch + requiredRowPitch;
        if (mip.byteSize < requiredBytes)
            return std::nullopt;

        const uint64_t rowPitch = static_cast<uint64_t>(mip.width) * 4u;
        const uint64_t byteSize = rowPitch * mip.height;
        if (byteSize > std::numeric_limits<size_t>::max() - decoded.bytes.size())
            return std::nullopt;
        TextureMipLevel decodedMip;
        decodedMip.width = mip.width;
        decodedMip.height = mip.height;
        decodedMip.depth = 1;
        decodedMip.byteOffset = decoded.bytes.size();
        decodedMip.byteSize = byteSize;
        decodedMip.rowPitch = rowPitch;
        decodedMip.slicePitch = byteSize;
        decoded.mipLevels.push_back(decodedMip);
        decoded.bytes.resize(decoded.bytes.size() + static_cast<size_t>(byteSize));

        for (uint32_t blockY = 0; blockY < blockRows; ++blockY) {
            for (uint32_t blockX = 0; blockX < blockColumns; ++blockX) {
                const uint8_t *block = source.bytes.data() + mip.byteOffset +
                                       static_cast<uint64_t>(blockY) * mip.rowPitch +
                                       static_cast<uint64_t>(blockX) * blockBytes;
                std::array<std::array<uint8_t, 4>, 4> colors{};
                uint32_t colorIndices = 0;
                std::array<uint8_t, 8> alphaOrRed{};
                std::array<uint8_t, 8> green{};
                uint64_t alphaOrRedIndices = 0;
                uint64_t greenIndices = 0;
                if (bc1 || bc3) {
                    const uint8_t *colorBlock = block + (bc3 ? 8u : 0u);
                    colors = DecodeBcColorTable(colorBlock, bc1);
                    colorIndices = static_cast<uint32_t>(colorBlock[4]) | static_cast<uint32_t>(colorBlock[5]) << 8u |
                                   static_cast<uint32_t>(colorBlock[6]) << 16u |
                                   static_cast<uint32_t>(colorBlock[7]) << 24u;
                    if (bc3) {
                        alphaOrRed = DecodeBc3AlphaTable(block[0], block[1]);
                        alphaOrRedIndices = DecodeBcScalarIndices(block);
                    }
                } else {
                    alphaOrRed = DecodeBc3AlphaTable(block[0], block[1]);
                    alphaOrRedIndices = DecodeBcScalarIndices(block);
                    if (bc5) {
                        green = DecodeBc3AlphaTable(block[8], block[9]);
                        greenIndices = DecodeBcScalarIndices(block + 8u);
                    }
                }

                for (uint32_t pixel = 0; pixel < 16; ++pixel) {
                    const uint32_t x = blockX * 4u + pixel % 4u;
                    const uint32_t y = blockY * 4u + pixel / 4u;
                    if (x >= mip.width || y >= mip.height)
                        continue;
                    std::array<uint8_t, 4> rgba{};
                    if (bc1 || bc3) {
                        rgba = colors[(colorIndices >> (pixel * 2u)) & 0x3u];
                        if (bc3)
                            rgba[3] = alphaOrRed[(alphaOrRedIndices >> (pixel * 3u)) & 0x7u];
                    } else {
                        const uint8_t red = alphaOrRed[(alphaOrRedIndices >> (pixel * 3u)) & 0x7u];
                        const uint8_t greenValue = bc5 ? green[(greenIndices >> (pixel * 3u)) & 0x7u] : red;
                        rgba = {red, greenValue, static_cast<uint8_t>(bc5 ? 255u : red), 255u};
                    }
                    std::memcpy(decoded.bytes.data() + decodedMip.byteOffset + static_cast<uint64_t>(y) * rowPitch +
                                    x * 4u,
                                rgba.data(), rgba.size());
                }
            }
        }
    }
    return decoded;
}

bool MaterialIsUnlit(const std::shared_ptr<InxMaterial> &material)
{
    if (!material)
        return false;
    std::string shader = material->GetFragShaderName();
    std::transform(shader.begin(), shader.end(), shader.begin(),
                   [](unsigned char value) { return static_cast<char>(std::tolower(value)); });
    return shader.find("unlit") != std::string::npos;
}

bool MaterialIsToon(const std::shared_ptr<InxMaterial> &material)
{
    if (!material)
        return false;
    std::string shader = material->GetFragShaderName();
    std::transform(shader.begin(), shader.end(), shader.begin(),
                   [](unsigned char value) { return static_cast<char>(std::tolower(value)); });
    return shader.find("toon") != std::string::npos;
}

bool Finite(const glm::vec3 &value)
{
    return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

glm::mat4 SkinMatrix(const Vertex &vertex, const std::vector<glm::mat4> *palette)
{
    if (!palette)
        return glm::mat4(1.0f);
    glm::mat4 result(0.0f);
    float totalWeight = 0.0f;
    for (uint32_t slot = 0; slot < 4; ++slot) {
        const float weight = vertex.boneWeights[slot];
        const uint32_t bone = vertex.boneIndices[slot];
        if (!(weight > 0.0f) || bone >= palette->size())
            continue;
        result += (*palette)[bone] * weight;
        totalWeight += weight;
    }
    return totalWeight > 0.0f ? result : glm::mat4(1.0f);
}

uint64_t GrowCapacity(uint64_t required)
{
    uint64_t capacity = 4096;
    while (capacity < required && capacity <= std::numeric_limits<uint64_t>::max() / 2)
        capacity *= 2;
    return std::max(capacity, required);
}

glm::mat4 ToWebClipSpace(const glm::mat4 &vulkanViewProjection)
{
    // Camera publishes the Vulkan projection used by the native renderer,
    // including its Y inversion. WebGPU performs its own framebuffer-space Y
    // mapping, so carrying that inversion across would present the whole game
    // upside down. Keep the correction at this backend boundary.
    glm::mat4 correction(1.0f);
    correction[1][1] = -1.0f;
    return correction * vulkanViewProjection;
}

} // namespace

bool WebSceneRenderer::Initialize(wgpu::Device device, wgpu::Queue queue, wgpu::TextureFormat colorFormat)
{
    m_device = std::move(device);
    m_queue = std::move(queue);
    m_colorFormat = colorFormat;
    if (!m_device || !m_queue || colorFormat == wgpu::TextureFormat::Undefined)
        return false;

    wgpu::BufferDescriptor cameraBufferDescriptor;
    cameraBufferDescriptor.size = sizeof(CameraData);
    cameraBufferDescriptor.usage = wgpu::BufferUsage::Uniform | wgpu::BufferUsage::CopyDst;
    m_cameraBuffer = m_device.CreateBuffer(&cameraBufferDescriptor);
    if (!m_cameraBuffer)
        return false;

    std::array<wgpu::BindGroupLayoutEntry, 3> cameraEntries{};
    cameraEntries[0].binding = 0;
    cameraEntries[0].visibility = wgpu::ShaderStage::Vertex | wgpu::ShaderStage::Fragment;
    cameraEntries[0].buffer.type = wgpu::BufferBindingType::Uniform;
    cameraEntries[0].buffer.minBindingSize = sizeof(CameraData);
    cameraEntries[1].binding = 1;
    cameraEntries[1].visibility = wgpu::ShaderStage::Fragment;
    cameraEntries[1].texture.sampleType = wgpu::TextureSampleType::Depth;
    cameraEntries[1].texture.viewDimension = wgpu::TextureViewDimension::e2D;
    cameraEntries[2].binding = 2;
    cameraEntries[2].visibility = wgpu::ShaderStage::Fragment;
    cameraEntries[2].sampler.type = wgpu::SamplerBindingType::Comparison;
    wgpu::BindGroupLayoutDescriptor cameraLayoutDescriptor;
    cameraLayoutDescriptor.entryCount = cameraEntries.size();
    cameraLayoutDescriptor.entries = cameraEntries.data();
    m_cameraLayout = m_device.CreateBindGroupLayout(&cameraLayoutDescriptor);

    if (!m_cameraLayout || !CreateShadowResources() || !CreateMaterialTextureResources())
        return false;

    std::array<wgpu::BindGroupEntry, 3> cameraBindings{};
    cameraBindings[0].binding = 0;
    cameraBindings[0].buffer = m_cameraBuffer;
    cameraBindings[0].size = sizeof(CameraData);
    cameraBindings[1].binding = 1;
    cameraBindings[1].textureView = m_shadowView;
    cameraBindings[2].binding = 2;
    cameraBindings[2].sampler = m_shadowSampler;
    wgpu::BindGroupDescriptor cameraGroupDescriptor;
    cameraGroupDescriptor.layout = m_cameraLayout;
    cameraGroupDescriptor.entryCount = cameraBindings.size();
    cameraGroupDescriptor.entries = cameraBindings.data();
    m_cameraGroup = m_device.CreateBindGroup(&cameraGroupDescriptor);

    wgpu::BindGroupLayoutEntry shadowCameraEntry;
    shadowCameraEntry.binding = 0;
    shadowCameraEntry.visibility = wgpu::ShaderStage::Vertex;
    shadowCameraEntry.buffer.type = wgpu::BufferBindingType::Uniform;
    shadowCameraEntry.buffer.minBindingSize = sizeof(CameraData);
    wgpu::BindGroupLayoutDescriptor shadowCameraLayoutDescriptor;
    shadowCameraLayoutDescriptor.entryCount = 1;
    shadowCameraLayoutDescriptor.entries = &shadowCameraEntry;
    m_shadowCameraLayout = m_device.CreateBindGroupLayout(&shadowCameraLayoutDescriptor);

    wgpu::BindGroupEntry shadowCameraBinding;
    shadowCameraBinding.binding = 0;
    shadowCameraBinding.buffer = m_cameraBuffer;
    shadowCameraBinding.size = sizeof(CameraData);
    wgpu::BindGroupDescriptor shadowCameraGroupDescriptor;
    shadowCameraGroupDescriptor.layout = m_shadowCameraLayout;
    shadowCameraGroupDescriptor.entryCount = 1;
    shadowCameraGroupDescriptor.entries = &shadowCameraBinding;
    m_shadowCameraGroup = m_device.CreateBindGroup(&shadowCameraGroupDescriptor);
    return m_cameraGroup && m_shadowCameraLayout && m_shadowCameraGroup && CreatePipelines();
}

bool WebSceneRenderer::CreateShadowResources()
{
    wgpu::TextureDescriptor textureDescriptor;
    textureDescriptor.dimension = wgpu::TextureDimension::e2D;
    textureDescriptor.size = {m_shadowResolution, m_shadowResolution, 1};
    textureDescriptor.format = wgpu::TextureFormat::Depth32Float;
    textureDescriptor.mipLevelCount = 1;
    textureDescriptor.sampleCount = 1;
    textureDescriptor.usage = wgpu::TextureUsage::RenderAttachment | wgpu::TextureUsage::TextureBinding;
    m_shadowTexture = m_device.CreateTexture(&textureDescriptor);
    m_shadowView = m_shadowTexture ? m_shadowTexture.CreateView() : wgpu::TextureView{};

    wgpu::SamplerDescriptor samplerDescriptor;
    samplerDescriptor.addressModeU = wgpu::AddressMode::ClampToEdge;
    samplerDescriptor.addressModeV = wgpu::AddressMode::ClampToEdge;
    samplerDescriptor.minFilter = wgpu::FilterMode::Linear;
    samplerDescriptor.magFilter = wgpu::FilterMode::Linear;
    samplerDescriptor.compare = wgpu::CompareFunction::LessEqual;
    m_shadowSampler = m_device.CreateSampler(&samplerDescriptor);
    return m_shadowView && m_shadowSampler;
}

bool WebSceneRenderer::CreateMaterialTextureResources()
{
    std::array<wgpu::BindGroupLayoutEntry, 10> entries{};
    for (uint32_t textureIndex = 0; textureIndex < 5; ++textureIndex) {
        const uint32_t samplerBinding = textureIndex * 2u;
        const uint32_t textureBinding = samplerBinding + 1u;
        entries[samplerBinding].binding = samplerBinding;
        entries[samplerBinding].visibility = wgpu::ShaderStage::Fragment;
        entries[samplerBinding].sampler.type = wgpu::SamplerBindingType::Filtering;
        entries[textureBinding].binding = textureBinding;
        entries[textureBinding].visibility = wgpu::ShaderStage::Fragment;
        entries[textureBinding].texture.sampleType = wgpu::TextureSampleType::Float;
        entries[textureBinding].texture.viewDimension = wgpu::TextureViewDimension::e2D;
    }
    wgpu::BindGroupLayoutDescriptor layoutDescriptor;
    layoutDescriptor.entryCount = entries.size();
    layoutDescriptor.entries = entries.data();
    m_materialTextureLayout = m_device.CreateBindGroupLayout(&layoutDescriptor);
    if (!m_materialTextureLayout)
        return false;

    const auto createSolid = [this](const std::array<uint8_t, 4> &pixel, bool srgb) {
        TextureCpuData data;
        data.dimension = TextureDimension::Texture2D;
        data.semantic = TextureSemantic::Color;
        data.format = srgb ? TextureFormat::Rgba8Srgb : TextureFormat::Rgba8UNorm;
        data.mipLevels.push_back(TextureMipLevel{1, 1, 1, 0, 4, 4, 4});
        data.bytes.assign(pixel.begin(), pixel.end());
        return UploadMaterialTexture(data, "linear", "repeat", 1);
    };
    m_whiteTexture = createSolid({255, 255, 255, 255}, true);
    m_blackTexture = createSolid({0, 0, 0, 255}, true);
    m_normalTexture = createSolid({128, 128, 255, 255}, false);
    const std::array<GPUTexture, 5> defaults = {m_whiteTexture, m_whiteTexture, m_whiteTexture, m_whiteTexture,
                                                m_normalTexture};
    m_defaultMaterialTextureGroup = CreateMaterialTextureGroup(defaults);
    return m_whiteTexture.view && m_blackTexture.view && m_normalTexture.view && m_defaultMaterialTextureGroup;
}

wgpu::BindGroup WebSceneRenderer::CreateMaterialTextureGroup(const std::array<GPUTexture, 5> &textures)
{
    std::array<wgpu::BindGroupEntry, 10> entries{};
    for (uint32_t textureIndex = 0; textureIndex < textures.size(); ++textureIndex) {
        if (!textures[textureIndex].view || !textures[textureIndex].sampler)
            return {};
        const uint32_t samplerBinding = textureIndex * 2u;
        const uint32_t textureBinding = samplerBinding + 1u;
        entries[samplerBinding].binding = samplerBinding;
        entries[samplerBinding].sampler = textures[textureIndex].sampler;
        entries[textureBinding].binding = textureBinding;
        entries[textureBinding].textureView = textures[textureIndex].view;
    }
    wgpu::BindGroupDescriptor descriptor;
    descriptor.layout = m_materialTextureLayout;
    descriptor.entryCount = entries.size();
    descriptor.entries = entries.data();
    return m_device.CreateBindGroup(&descriptor);
}

WebSceneRenderer::GPUTexture WebSceneRenderer::UploadMaterialTexture(const TextureCpuData &texture,
                                                                     const std::string &filterMode,
                                                                     const std::string &wrapMode, int anisoLevel)
{
    GPUTexture gpu;
    if (!m_device || !m_queue || texture.dimension != TextureDimension::Texture2D || !texture.IsValid())
        return gpu;
    std::optional<TextureCpuData> decoded;
    const TextureCpuData *upload = &texture;
    if (TextureFormatIsBlockCompressed(texture.format)) {
        decoded = DecodeBcTextureToRgba8(texture);
        if (!decoded)
            return gpu;
        upload = &*decoded;
    }
    const wgpu::TextureFormat format = ToWebTextureFormat(upload->format);
    const TextureMipLevel &baseMip = upload->mipLevels.front();
    if (format == wgpu::TextureFormat::Undefined || baseMip.width == 0 || baseMip.height == 0 ||
        upload->mipLevels.size() > std::numeric_limits<uint32_t>::max())
        return gpu;
    for (const TextureMipLevel &mip : upload->mipLevels) {
        if (mip.width == 0 || mip.height == 0 || mip.depth != 1 || mip.byteOffset > upload->bytes.size() ||
            mip.byteSize > upload->bytes.size() - mip.byteOffset || mip.rowPitch > std::numeric_limits<uint32_t>::max())
            return {};
    }

    wgpu::TextureDescriptor descriptor;
    descriptor.dimension = wgpu::TextureDimension::e2D;
    descriptor.size = {baseMip.width, baseMip.height, 1};
    descriptor.format = format;
    descriptor.mipLevelCount = static_cast<uint32_t>(upload->mipLevels.size());
    descriptor.sampleCount = 1;
    descriptor.usage = wgpu::TextureUsage::CopyDst | wgpu::TextureUsage::TextureBinding;
    gpu.texture = m_device.CreateTexture(&descriptor);
    gpu.view = gpu.texture ? gpu.texture.CreateView() : wgpu::TextureView{};
    wgpu::SamplerDescriptor samplerDescriptor;
    const wgpu::AddressMode addressMode = wrapMode == "clamp"    ? wgpu::AddressMode::ClampToEdge
                                          : wrapMode == "mirror" ? wgpu::AddressMode::MirrorRepeat
                                                                 : wgpu::AddressMode::Repeat;
    samplerDescriptor.addressModeU = addressMode;
    samplerDescriptor.addressModeV = addressMode;
    samplerDescriptor.addressModeW = addressMode;
    const bool point = filterMode == "point";
    const uint16_t anisotropy = point ? 1u : static_cast<uint16_t>(std::clamp(anisoLevel < 0 ? 16 : anisoLevel, 1, 16));
    samplerDescriptor.minFilter = point ? wgpu::FilterMode::Nearest : wgpu::FilterMode::Linear;
    samplerDescriptor.magFilter = point ? wgpu::FilterMode::Nearest : wgpu::FilterMode::Linear;
    samplerDescriptor.mipmapFilter =
        filterMode == "trilinear" || anisotropy > 1u ? wgpu::MipmapFilterMode::Linear : wgpu::MipmapFilterMode::Nearest;
    samplerDescriptor.lodMaxClamp = static_cast<float>(upload->mipLevels.size() - 1u);
    samplerDescriptor.maxAnisotropy = anisotropy;
    gpu.sampler = m_device.CreateSampler(&samplerDescriptor);
    if (!gpu.texture || !gpu.view || !gpu.sampler)
        return {};

    for (uint32_t mipLevel = 0; mipLevel < upload->mipLevels.size(); ++mipLevel) {
        const TextureMipLevel &mip = upload->mipLevels[mipLevel];
        wgpu::TexelCopyTextureInfo destination;
        destination.texture = gpu.texture;
        destination.mipLevel = mipLevel;
        wgpu::TexelCopyBufferLayout layout;
        layout.bytesPerRow = static_cast<uint32_t>(mip.rowPitch);
        layout.rowsPerImage = mip.height;
        const wgpu::Extent3D extent{mip.width, mip.height, 1};
        m_queue.WriteTexture(&destination, upload->bytes.data() + mip.byteOffset, static_cast<size_t>(mip.byteSize),
                             &layout, &extent);
    }
    gpu.mipLevels = static_cast<uint32_t>(upload->mipLevels.size());
    return gpu;
}

WebSceneRenderer::GPUTexture WebSceneRenderer::ResolveMaterialTexture(const std::string &guid,
                                                                      const GPUTexture &fallback)
{
    if (guid.empty() || guid == "white")
        return m_whiteTexture;
    if (guid == "normal")
        return m_normalTexture;
    if (guid == "black")
        return m_blackTexture;
    MaterialTextureState &state = m_materialTextures[guid];
    if (state.gpu.view)
        return state.gpu;
    if (state.failed)
        return fallback;

    AssetRegistry &registry = AssetRegistry::Instance();
    if (!state.ticket) {
        const auto texture = registry.LoadAsset<InxTexture>(guid, ResourceType::Texture);
        if (!texture) {
            state.failed = true;
            std::fprintf(stderr, "INFERNUX_WEB_MATERIAL_TEXTURE_FAILED guid=%s reason=asset-load\n", guid.c_str());
            return fallback;
        }
        state.filterMode = texture->GetFilterMode();
        state.wrapMode = texture->GetWrapMode();
        state.anisoLevel = texture->GetAnisoLevel();
        state.ticket = registry.BeginTextureUploadStaging(guid);
    }
    const auto staging = registry.TryConsumeTextureUploadStaging(state.ticket);
    if (!staging)
        return fallback;
    state.gpu = UploadMaterialTexture(*staging, state.filterMode, state.wrapMode, state.anisoLevel);
    state.ticket.reset();
    if (!state.gpu.view) {
        state.failed = true;
        std::fprintf(stderr, "INFERNUX_WEB_MATERIAL_TEXTURE_FAILED guid=%s format=%s\n", guid.c_str(),
                     TextureFormatName(staging->format));
        return fallback;
    }
    ++m_materialTextureGeneration;
    std::printf("INFERNUX_WEB_MATERIAL_TEXTURE_READY guid=%s width=%u height=%u mips=%u filter=%s wrap=%s\n",
                guid.c_str(), staging->mipLevels.front().width, staging->mipLevels.front().height, state.gpu.mipLevels,
                state.filterMode.c_str(), state.wrapMode.c_str());
    return state.gpu;
}

wgpu::BindGroup WebSceneRenderer::ResolveMaterialTextureSet(const std::shared_ptr<InxMaterial> &material)
{
    const std::array<std::string, 5> guids = MaterialTextureGuids(material);
    std::array<GPUTexture, 5> textures{};
    std::string key;
    for (size_t index = 0; index < guids.size(); ++index) {
        if (index > 0)
            key.push_back('\x1f');
        key.append(guids[index]);
        const GPUTexture &fallback = index == 4 ? m_normalTexture : m_whiteTexture;
        textures[index] = ResolveMaterialTexture(guids[index], fallback);
    }
    MaterialTextureSetState &state = m_materialTextureSets[key];
    if (!state.group || state.textureGeneration != m_materialTextureGeneration) {
        state.group = CreateMaterialTextureGroup(textures);
        state.textureGeneration = m_materialTextureGeneration;
    }
    return state.group ? state.group : m_defaultMaterialTextureGroup;
}

bool WebSceneRenderer::CreatePipelines()
{
    wgpu::ShaderSourceWGSL shaderSource;
    shaderSource.code = kSceneShader;
    wgpu::ShaderModuleDescriptor shaderDescriptor;
    shaderDescriptor.nextInChain = &shaderSource;
    const wgpu::ShaderModule shader = m_device.CreateShaderModule(&shaderDescriptor);
    if (!shader)
        return false;

    std::array<wgpu::VertexAttribute, 8> attributes{};
    attributes[0].format = wgpu::VertexFormat::Float32x3;
    attributes[0].offset = offsetof(WebVertex, position);
    attributes[0].shaderLocation = 0;
    attributes[1].format = wgpu::VertexFormat::Float32x3;
    attributes[1].offset = offsetof(WebVertex, normal);
    attributes[1].shaderLocation = 1;
    attributes[2].format = wgpu::VertexFormat::Float32x2;
    attributes[2].offset = offsetof(WebVertex, uv);
    attributes[2].shaderLocation = 2;
    attributes[3].format = wgpu::VertexFormat::Float32x4;
    attributes[3].offset = offsetof(WebVertex, color);
    attributes[3].shaderLocation = 3;
    attributes[4].format = wgpu::VertexFormat::Float32x4;
    attributes[4].offset = offsetof(WebVertex, emission);
    attributes[4].shaderLocation = 4;
    attributes[5].format = wgpu::VertexFormat::Float32x4;
    attributes[5].offset = offsetof(WebVertex, material);
    attributes[5].shaderLocation = 5;
    attributes[6].format = wgpu::VertexFormat::Float32x4;
    attributes[6].offset = offsetof(WebVertex, surface);
    attributes[6].shaderLocation = 6;
    attributes[7].format = wgpu::VertexFormat::Float32x4;
    attributes[7].offset = offsetof(WebVertex, tangent);
    attributes[7].shaderLocation = 7;
    wgpu::VertexBufferLayout vertexLayout;
    vertexLayout.arrayStride = sizeof(WebVertex);
    vertexLayout.stepMode = wgpu::VertexStepMode::Vertex;
    vertexLayout.attributeCount = attributes.size();
    vertexLayout.attributes = attributes.data();

    wgpu::ColorTargetState colorTarget;
    colorTarget.format = m_colorFormat;
    colorTarget.writeMask = wgpu::ColorWriteMask::All;
    wgpu::FragmentState fragment;
    fragment.module = shader;
    fragment.entryPoint = "fragment_main";
    fragment.targetCount = 1;
    fragment.targets = &colorTarget;

    wgpu::DepthStencilState depth;
    depth.format = wgpu::TextureFormat::Depth24Plus;
    depth.depthWriteEnabled = wgpu::OptionalBool::True;
    depth.depthCompare = wgpu::CompareFunction::LessEqual;

    wgpu::PipelineLayoutDescriptor pipelineLayoutDescriptor;
    const std::array<wgpu::BindGroupLayout, 2> sceneLayouts = {m_cameraLayout, m_materialTextureLayout};
    pipelineLayoutDescriptor.bindGroupLayoutCount = sceneLayouts.size();
    pipelineLayoutDescriptor.bindGroupLayouts = sceneLayouts.data();

    wgpu::RenderPipelineDescriptor pipelineDescriptor;
    pipelineDescriptor.layout = m_device.CreatePipelineLayout(&pipelineLayoutDescriptor);
    pipelineDescriptor.vertex.module = shader;
    pipelineDescriptor.vertex.entryPoint = "vertex_main";
    pipelineDescriptor.vertex.bufferCount = 1;
    pipelineDescriptor.vertex.buffers = &vertexLayout;
    pipelineDescriptor.fragment = &fragment;
    pipelineDescriptor.primitive.topology = wgpu::PrimitiveTopology::TriangleList;
    pipelineDescriptor.primitive.frontFace = wgpu::FrontFace::CW;
    pipelineDescriptor.primitive.cullMode = wgpu::CullMode::None;
    pipelineDescriptor.depthStencil = &depth;
    pipelineDescriptor.multisample.count = 1;
    m_opaquePipeline = m_device.CreateRenderPipeline(&pipelineDescriptor);
    if (!m_opaquePipeline)
        return false;

    wgpu::BlendState transparentBlend;
    transparentBlend.color.srcFactor = wgpu::BlendFactor::SrcAlpha;
    transparentBlend.color.dstFactor = wgpu::BlendFactor::OneMinusSrcAlpha;
    transparentBlend.alpha.srcFactor = wgpu::BlendFactor::One;
    transparentBlend.alpha.dstFactor = wgpu::BlendFactor::OneMinusSrcAlpha;
    colorTarget.blend = &transparentBlend;
    depth.depthWriteEnabled = wgpu::OptionalBool::False;
    m_transparentPipeline = m_device.CreateRenderPipeline(&pipelineDescriptor);
    colorTarget.blend = nullptr;
    depth.depthWriteEnabled = wgpu::OptionalBool::True;
    if (!m_transparentPipeline)
        return false;

    wgpu::ShaderSourceWGSL skyShaderSource;
    skyShaderSource.code = kSkyShader;
    wgpu::ShaderModuleDescriptor skyShaderDescriptor;
    skyShaderDescriptor.nextInChain = &skyShaderSource;
    const wgpu::ShaderModule skyShader = m_device.CreateShaderModule(&skyShaderDescriptor);
    wgpu::FragmentState skyFragment;
    skyFragment.module = skyShader;
    skyFragment.entryPoint = "fragment_main";
    skyFragment.targetCount = 1;
    skyFragment.targets = &colorTarget;
    wgpu::RenderPipelineDescriptor skyPipelineDescriptor;
    skyPipelineDescriptor.layout = pipelineDescriptor.layout;
    skyPipelineDescriptor.vertex.module = skyShader;
    skyPipelineDescriptor.vertex.entryPoint = "vertex_main";
    skyPipelineDescriptor.fragment = &skyFragment;
    skyPipelineDescriptor.primitive.topology = wgpu::PrimitiveTopology::TriangleList;
    skyPipelineDescriptor.primitive.cullMode = wgpu::CullMode::None;
    wgpu::DepthStencilState skyDepth;
    skyDepth.format = wgpu::TextureFormat::Depth24Plus;
    skyDepth.depthWriteEnabled = wgpu::OptionalBool::False;
    skyDepth.depthCompare = wgpu::CompareFunction::Always;
    skyPipelineDescriptor.depthStencil = &skyDepth;
    m_skyPipeline = m_device.CreateRenderPipeline(&skyPipelineDescriptor);

    wgpu::ShaderSourceWGSL shadowShaderSource;
    shadowShaderSource.code = kShadowShader;
    wgpu::ShaderModuleDescriptor shadowShaderDescriptor;
    shadowShaderDescriptor.nextInChain = &shadowShaderSource;
    const wgpu::ShaderModule shadowShader = m_device.CreateShaderModule(&shadowShaderDescriptor);
    wgpu::DepthStencilState shadowDepth;
    shadowDepth.format = wgpu::TextureFormat::Depth32Float;
    shadowDepth.depthWriteEnabled = wgpu::OptionalBool::True;
    shadowDepth.depthCompare = wgpu::CompareFunction::LessEqual;
    shadowDepth.depthBias = 2;
    shadowDepth.depthBiasSlopeScale = 2.0f;
    wgpu::PipelineLayoutDescriptor shadowPipelineLayoutDescriptor;
    shadowPipelineLayoutDescriptor.bindGroupLayoutCount = 1;
    shadowPipelineLayoutDescriptor.bindGroupLayouts = &m_shadowCameraLayout;
    wgpu::RenderPipelineDescriptor shadowPipelineDescriptor;
    shadowPipelineDescriptor.layout = m_device.CreatePipelineLayout(&shadowPipelineLayoutDescriptor);
    shadowPipelineDescriptor.vertex.module = shadowShader;
    shadowPipelineDescriptor.vertex.entryPoint = "vertex_main";
    shadowPipelineDescriptor.vertex.bufferCount = 1;
    shadowPipelineDescriptor.vertex.buffers = &vertexLayout;
    shadowPipelineDescriptor.primitive.topology = wgpu::PrimitiveTopology::TriangleList;
    shadowPipelineDescriptor.primitive.frontFace = wgpu::FrontFace::CW;
    shadowPipelineDescriptor.primitive.cullMode = wgpu::CullMode::None;
    shadowPipelineDescriptor.depthStencil = &shadowDepth;
    m_shadowPipeline = m_device.CreateRenderPipeline(&shadowPipelineDescriptor);
    return m_skyPipeline && m_shadowPipeline;
}

void WebSceneRenderer::Resize(uint32_t width, uint32_t height)
{
    width = std::max(1u, width);
    height = std::max(1u, height);
    if (m_depthTexture && width == m_depthWidth && height == m_depthHeight)
        return;

    wgpu::TextureDescriptor descriptor;
    descriptor.dimension = wgpu::TextureDimension::e2D;
    descriptor.size = {width, height, 1};
    descriptor.format = wgpu::TextureFormat::Depth24Plus;
    descriptor.mipLevelCount = 1;
    descriptor.sampleCount = 1;
    descriptor.usage = wgpu::TextureUsage::RenderAttachment;
    m_depthTexture = m_device.CreateTexture(&descriptor);
    m_depthView = m_depthTexture ? m_depthTexture.CreateView() : wgpu::TextureView{};
    m_depthWidth = width;
    m_depthHeight = height;
}

bool WebSceneRenderer::HasDepthTarget() const noexcept
{
    return static_cast<bool>(m_depthView);
}

wgpu::TextureView WebSceneRenderer::GetDepthView() const noexcept
{
    return m_depthView;
}

void WebSceneRenderer::SetSkyEnabledForDiagnostics(bool enabled) noexcept
{
    m_diagnosticSkyEnabled = enabled;
}

void WebSceneRenderer::SetShadowsEnabledForDiagnostics(bool enabled) noexcept
{
    m_diagnosticShadowsEnabled = enabled;
}

bool WebSceneRenderer::EnsureBuffer(wgpu::Buffer &buffer, uint64_t &capacity, uint64_t required,
                                    wgpu::BufferUsage usage)
{
    if (required == 0)
        return false;
    if (buffer && required <= capacity)
        return true;
    capacity = GrowCapacity(required);
    wgpu::BufferDescriptor descriptor;
    descriptor.size = capacity;
    descriptor.usage = usage | wgpu::BufferUsage::CopyDst;
    buffer = m_device.CreateBuffer(&descriptor);
    return static_cast<bool>(buffer);
}

bool WebSceneRenderer::BuildFrame(uint32_t width, uint32_t height)
{
    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene) {
        ReportFrameIssue("no-active-scene");
        return false;
    }
    Camera *camera = scene->FindGameCamera(nullptr);
    if (!camera) {
        ReportFrameIssue("no-game-camera");
        return false;
    }

    camera->SetAspectRatio(static_cast<float>(width) / static_cast<float>(std::max(1u, height)));
    const size_t visibleCount = m_extractor.ExtractCameraFrame(m_world, camera);
    if (visibleCount == 0) {
        ReportFrameIssue("no-visible-renderers");
        return false;
    }
    const auto frame = m_world.Acquire();
    if (!frame || !frame->PrimaryView().valid) {
        ReportFrameIssue("invalid-render-snapshot");
        return false;
    }

    m_vertices.clear();
    m_indices.clear();
    m_drawRanges.clear();
    const glm::mat4 cameraToWorld = glm::inverse(camera->GetViewMatrix());
    const glm::vec3 cameraRight = glm::normalize(glm::vec3(cameraToWorld[0]));
    const glm::vec3 cameraUp = glm::normalize(glm::vec3(cameraToWorld[1]));
    const glm::vec3 viewFacing = glm::normalize(-glm::vec3(cameraToWorld[2]));
    const auto &drawCalls = frame->DrawCalls().drawCalls;
    for (const DrawCall &draw : drawCalls) {
        if (!draw.frustumVisible || !draw.meshVertices || !draw.meshIndices || draw.indexCount == 0)
            continue;
        const auto &sourceVertices = *draw.meshVertices;
        const auto &sourceIndices = *draw.meshIndices;
        if (sourceVertices.empty() || sourceIndices.empty() || draw.indexStart >= sourceIndices.size())
            continue;
        const uint32_t indexCount =
            static_cast<uint32_t>(std::min<size_t>(draw.indexCount, sourceIndices.size() - draw.indexStart));
        const uint32_t vertexBase = static_cast<uint32_t>(m_vertices.size());
        const glm::vec4 materialColor = inx::color::SrgbToLinear(MaterialColor(draw.material));
        const glm::vec4 emission =
            inx::color::SrgbToLinear(MaterialVector(draw.material, "emissionColor", glm::vec4(0.0f)));
        const glm::vec4 materialParameters(
            std::clamp(MaterialFloat(draw.material, "metallic", 0.0f), 0.0f, 1.0f),
            std::clamp(MaterialFloat(draw.material, "smoothness", 0.5f), 0.0f, 1.0f),
            std::clamp(MaterialFloat(draw.material, "ambientOcclusion", 1.0f), 0.0f, 1.0f),
            std::clamp(MaterialFloat(draw.material, "specularHighlights", 1.0f), 0.0f, 1.0f));
        const float normalScale = std::max(0.0f, MaterialFloat(draw.material, "normalScale", 1.0f));
        float shadingModel = 0.0f;
        if (MaterialIsUnlit(draw.material))
            shadingModel = 1.0f;
        else if (MaterialIsToon(draw.material))
            shadingModel = 2.0f;
        float alphaClipThreshold = -1.0f;
        if (draw.material && draw.material->GetRenderState().alphaClipEnabled)
            alphaClipThreshold = std::clamp(draw.material->GetRenderState().alphaClipThreshold, 0.0f, 1.0f);
        const glm::vec4 surfaceParameters(
            shadingModel, std::clamp(MaterialFloat(draw.material, "diffuseThreshold", 0.45f), 0.0f, 1.0f),
            std::clamp(MaterialFloat(draw.material, "bandSoftness", 0.04f), 0.0f, 0.25f), alphaClipThreshold);
        WebDrawRange range;
        range.firstIndex = static_cast<uint32_t>(m_indices.size());
        range.castsShadows = draw.castsShadows;
        range.materialTextureGroup = ResolveMaterialTextureSet(draw.material);
        if (draw.material) {
            const RenderState &state = draw.material->GetRenderState();
            range.transparent = state.blendEnable || state.renderQueue >= 3000 || materialColor.a < 0.999f;
        } else {
            range.transparent = materialColor.a < 0.999f;
        }
        const glm::mat3 worldNormal = glm::inverseTranspose(glm::mat3(draw.worldMatrix));

        m_vertices.reserve(m_vertices.size() + sourceVertices.size());
        for (const Vertex &source : sourceVertices) {
            const bool lineVertex = source.boneIndices.w == kLineVertexMarker;
            range.line = range.line || lineVertex;
            glm::vec3 worldPosition;
            glm::vec3 worldNormalValue;
            glm::vec4 worldTangentValue(1.0f, 0.0f, 0.0f, normalScale);
            float vertexAlpha = 1.0f;
            if (lineVertex) {
                const glm::vec3 center = glm::vec3(draw.worldMatrix * glm::vec4(source.pos, 1.0f));
                glm::vec3 tangent = glm::mat3(draw.worldMatrix) * glm::vec3(source.tangent);
                tangent = Finite(tangent) && glm::dot(tangent, tangent) > 1.0e-10f ? glm::normalize(tangent)
                                                                                   : glm::vec3(1.0f, 0.0f, 0.0f);
                glm::vec3 facing = source.boneIndices.z == 0u ? viewFacing : worldNormal * source.normal;
                facing = Finite(facing) && glm::dot(facing, facing) > 1.0e-10f ? glm::normalize(facing)
                                                                               : glm::vec3(0.0f, 0.0f, 1.0f);
                glm::vec3 fallbackSide = cameraRight - tangent * glm::dot(cameraRight, tangent);
                if (glm::dot(fallbackSide, fallbackSide) < 1.0e-8f)
                    fallbackSide = cameraUp - tangent * glm::dot(cameraUp, tangent);
                if (glm::dot(fallbackSide, fallbackSide) < 1.0e-10f)
                    fallbackSide = std::abs(tangent.x) < 0.9f ? glm::cross(tangent, glm::vec3(1.0f, 0.0f, 0.0f))
                                                              : glm::cross(tangent, glm::vec3(0.0f, 1.0f, 0.0f));
                fallbackSide = glm::normalize(fallbackSide);
                glm::vec3 geometricSide = glm::cross(facing, tangent);
                const float geometricLength = glm::length(geometricSide);
                glm::vec3 side;
                if (geometricLength > 0.20f) {
                    side = geometricSide / geometricLength;
                } else if (geometricLength > 1.0e-6f) {
                    geometricSide /= geometricLength;
                    if (glm::dot(fallbackSide, geometricSide) < 0.0f)
                        fallbackSide = -fallbackSide;
                    const float normalizedWeight = std::clamp((geometricLength - 0.025f) / 0.175f, 0.0f, 1.0f);
                    const float geometricWeight =
                        normalizedWeight * normalizedWeight * (3.0f - 2.0f * normalizedWeight);
                    side = glm::normalize(glm::mix(fallbackSide, geometricSide, geometricWeight));
                } else {
                    side = fallbackSide;
                }
                worldPosition = center + side * source.boneWeights.x;
                worldNormalValue = source.boneIndices.y != 0u ? facing : worldNormal * source.normal;
                worldTangentValue = glm::vec4(tangent, normalScale);
                vertexAlpha = std::clamp(source.boneWeights.y, 0.0f, 1.0f);
            } else {
                const glm::mat4 skin = SkinMatrix(source, draw.skinBoneMatrices);
                const glm::vec3 localPosition = glm::vec3(skin * glm::vec4(source.pos, 1.0f));
                worldPosition = glm::vec3(draw.worldMatrix * glm::vec4(localPosition, 1.0f));
                const glm::vec3 localNormal = glm::mat3(skin) * source.normal;
                worldNormalValue = worldNormal * localNormal;
                const glm::mat3 tangentTransform = glm::mat3(draw.worldMatrix) * glm::mat3(skin);
                glm::vec3 tangent = tangentTransform * glm::vec3(source.tangent);
                const float transformSign = glm::determinant(tangentTransform) < 0.0f ? -1.0f : 1.0f;
                const float handedness = source.tangent.w < 0.0f ? -1.0f : 1.0f;
                worldTangentValue = glm::vec4(tangent, handedness * transformSign * normalScale);
            }
            if (!Finite(worldNormalValue) || glm::dot(worldNormalValue, worldNormalValue) < 1.0e-8f)
                worldNormalValue = glm::vec3(0.0f, 1.0f, 0.0f);
            else
                worldNormalValue = glm::normalize(worldNormalValue);
            glm::vec3 tangent = glm::vec3(worldTangentValue);
            tangent -= worldNormalValue * glm::dot(worldNormalValue, tangent);
            if (!Finite(tangent) || glm::dot(tangent, tangent) < 1.0e-8f) {
                const glm::vec3 axis =
                    std::abs(worldNormalValue.y) > 0.999f ? glm::vec3(0.0f, 0.0f, 1.0f) : glm::vec3(0.0f, 1.0f, 0.0f);
                tangent = glm::normalize(glm::cross(axis, worldNormalValue));
            } else {
                tangent = glm::normalize(tangent);
            }
            worldTangentValue = glm::vec4(tangent, worldTangentValue.w);
            const glm::vec4 color = glm::vec4(source.color, vertexAlpha) * materialColor;
            range.transparent = range.transparent || color.a < 0.999f;
            WebVertex vertex{};
            std::memcpy(vertex.position, &worldPosition, sizeof(vertex.position));
            std::memcpy(vertex.normal, &worldNormalValue, sizeof(vertex.normal));
            std::memcpy(vertex.tangent, &worldTangentValue, sizeof(vertex.tangent));
            std::memcpy(vertex.uv, &source.texCoord, sizeof(vertex.uv));
            std::memcpy(vertex.color, &color, sizeof(vertex.color));
            std::memcpy(vertex.emission, &emission, sizeof(vertex.emission));
            std::memcpy(vertex.material, &materialParameters, sizeof(vertex.material));
            std::memcpy(vertex.surface, &surfaceParameters, sizeof(vertex.surface));
            m_vertices.push_back(vertex);
        }

        m_indices.reserve(m_indices.size() + indexCount);
        for (uint32_t index = 0; index < indexCount; ++index) {
            const uint32_t sourceIndex = sourceIndices[draw.indexStart + index];
            if (sourceIndex >= sourceVertices.size())
                continue;
            m_indices.push_back(vertexBase + sourceIndex);
        }
        range.indexCount = static_cast<uint32_t>(m_indices.size()) - range.firstIndex;
        if (range.indexCount > 0)
            m_drawRanges.push_back(range);
    }

    if (m_vertices.empty() || m_indices.empty()) {
        ReportFrameIssue("empty-draw-stream");
        return false;
    }

    const SceneEnvironmentSettings &environment = scene->GetEnvironment();
    m_cameraData = {};
    m_cameraData.viewProjection = ToWebClipSpace(frame->PrimaryView().viewProjection);
    m_cameraData.inverseViewProjection = glm::inverse(m_cameraData.viewProjection);
    m_cameraData.cameraPosition = glm::vec4(frame->PrimaryView().position, 1.0f);
    m_cameraData.skyTopExposure = glm::vec4(inx::color::SrgbToLinear(environment.skyTopColor), environment.skyExposure);
    m_cameraData.skyHorizon = glm::vec4(inx::color::SrgbToLinear(environment.skyHorizonColor), 1.0f);
    m_cameraData.skyGround = glm::vec4(inx::color::SrgbToLinear(environment.skyGroundColor), 1.0f);
    m_drawSky = m_diagnosticSkyEnabled && camera->GetClearFlags() == CameraClearFlags::Skybox;

    using AmbientSource = SceneEnvironmentSettings::AmbientSource;
    switch (static_cast<AmbientSource>(environment.ambientSource)) {
    case AmbientSource::Color:
        m_cameraData.ambient =
            glm::vec4(inx::color::SrgbToLinear(environment.ambientColor), environment.ambientIntensity);
        m_cameraData.ambientEquator.w = 0.0f;
        break;
    case AmbientSource::Gradient:
        m_cameraData.ambientSky =
            glm::vec4(inx::color::SrgbToLinear(environment.ambientSkyColor) * environment.ambientIntensity, 1.0f);
        m_cameraData.ambientEquator =
            glm::vec4(inx::color::SrgbToLinear(environment.ambientEquatorColor) * environment.ambientIntensity, 1.0f);
        m_cameraData.ambientGround =
            glm::vec4(inx::color::SrgbToLinear(environment.ambientGroundColor) * environment.ambientIntensity, 1.0f);
        break;
    case AmbientSource::Skybox:
    default:
        m_cameraData.ambientSky = glm::vec4(inx::color::SrgbToLinear(environment.skyTopColor) *
                                                (environment.skyExposure * environment.ambientIntensity),
                                            1.0f);
        m_cameraData.ambientEquator = glm::vec4(inx::color::SrgbToLinear(environment.skyHorizonColor) *
                                                    (environment.skyExposure * environment.ambientIntensity),
                                                1.0f);
        m_cameraData.ambientGround = glm::vec4(inx::color::SrgbToLinear(environment.skyGroundColor) *
                                                   (environment.skyExposure * environment.ambientIntensity),
                                               1.0f);
        break;
    }

    Light *directionalLight = nullptr;
    for (Light *light : SceneManager::Instance().GetActiveLights()) {
        if (light && light->IsEnabled() && light->GetLightType() == LightType::Directional &&
            light->GetAffectGeometry()) {
            directionalLight = light;
            break;
        }
    }
    glm::vec3 rayDirection(-0.35f, -0.82f, -0.45f);
    glm::vec3 lightColor(1.0f);
    float lightIntensity = 1.0f;
    float shadowStrength = 0.0f;
    if (directionalLight) {
        if (Transform *transform = directionalLight->GetTransform()) {
            const glm::vec3 forward = transform->GetWorldForward();
            if (Finite(forward) && glm::dot(forward, forward) > 1.0e-8f)
                rayDirection = glm::normalize(forward);
        }
        lightColor = inx::color::SrgbToLinear(directionalLight->GetColor());
        lightIntensity = directionalLight->GetIntensity();
        if (directionalLight->GetShadows() != LightShadows::None)
            shadowStrength = directionalLight->GetShadowStrength();
    }
    const glm::vec3 towardLight = -glm::normalize(rayDirection);
    m_cameraData.lightColorIntensity = glm::vec4(lightColor, lightIntensity);
    m_shadowEnabled = m_diagnosticShadowsEnabled && shadowStrength > 0.0f;
    m_cameraData.lightDirectionStrength = glm::vec4(towardLight, m_shadowEnabled ? shadowStrength : 0.0f);

    uint32_t punctualLightCount = 0;
    for (Light *light : SceneManager::Instance().GetActiveLights()) {
        if (!light || !light->IsEnabled() || !light->GetAffectGeometry() || punctualLightCount >= kMaxPunctualLights)
            continue;
        const LightType type = light->GetLightType();
        if (type != LightType::Point && type != LightType::Spot)
            continue;
        Transform *transform = light->GetTransform();
        if (!transform)
            continue;
        CameraData::PunctualLightData &target = m_cameraData.punctualLights[punctualLightCount++];
        target.positionRange = glm::vec4(transform->GetWorldPosition(), light->GetRange());
        target.colorIntensity = glm::vec4(inx::color::SrgbToLinear(light->GetColor()), light->GetIntensity());
        glm::vec3 forward = transform->GetWorldForward();
        if (!Finite(forward) || glm::dot(forward, forward) < 1.0e-8f)
            forward = glm::vec3(0.0f, -1.0f, 0.0f);
        target.directionOuterCos =
            glm::vec4(glm::normalize(forward), std::cos(glm::radians(light->GetOuterSpotAngle() * 0.5f)));
        target.parameters = glm::vec4(std::cos(glm::radians(light->GetSpotAngle() * 0.5f)),
                                      type == LightType::Spot ? 1.0f : 0.0f, 0.0f, 0.0f);
    }
    m_cameraData.lightCounts.x = punctualLightCount;

    glm::vec3 boundsMin(std::numeric_limits<float>::max());
    glm::vec3 boundsMax(std::numeric_limits<float>::lowest());
    for (const WebVertex &vertex : m_vertices) {
        const glm::vec3 position(vertex.position[0], vertex.position[1], vertex.position[2]);
        boundsMin = glm::min(boundsMin, position);
        boundsMax = glm::max(boundsMax, position);
    }
    const glm::vec3 center = (boundsMin + boundsMax) * 0.5f;
    const float radius = std::max(2.0f, glm::length(boundsMax - boundsMin) * 0.6f);
    const glm::vec3 lightUp = std::abs(glm::dot(rayDirection, glm::vec3(0.0f, 1.0f, 0.0f))) > 0.95f
                                  ? glm::vec3(1.0f, 0.0f, 0.0f)
                                  : glm::vec3(0.0f, 1.0f, 0.0f);
    const glm::vec3 lightPosition = center - rayDirection * (radius * 2.0f);
    const glm::mat4 lightView = glm::lookAtRH(lightPosition, center, lightUp);
    const glm::mat4 lightProjection = glm::orthoRH_ZO(-radius, radius, -radius, radius, 0.1f, radius * 4.5f);
    m_cameraData.lightViewProjection = lightProjection * lightView;

    if (!m_lastFrameIssue.empty()) {
        std::printf("INFERNUX_WEB_SCENE_RENDER_RECOVERED previous=%s vertices=%zu indices=%zu\n",
                    m_lastFrameIssue.c_str(), m_vertices.size(), m_indices.size());
        m_lastFrameIssue.clear();
    }
    return true;
}

void WebSceneRenderer::ReportFrameIssue(const char *issue)
{
    if (m_lastFrameIssue == issue)
        return;
    m_lastFrameIssue = issue;
    std::fprintf(stderr, "INFERNUX_WEB_SCENE_RENDER_EMPTY reason=%s\n", issue);
}

bool WebSceneRenderer::Prepare(wgpu::CommandEncoder encoder, uint32_t width, uint32_t height)
{
    m_framePrepared = false;
    if (!m_opaquePipeline || !m_transparentPipeline || !m_shadowPipeline || !encoder || !BuildFrame(width, height))
        return false;
    const uint64_t vertexBytes = m_vertices.size() * sizeof(WebVertex);
    const uint64_t indexBytes = m_indices.size() * sizeof(uint32_t);
    if (!EnsureBuffer(m_vertexBuffer, m_vertexCapacity, vertexBytes, wgpu::BufferUsage::Vertex) ||
        !EnsureBuffer(m_indexBuffer, m_indexCapacity, indexBytes, wgpu::BufferUsage::Index))
        return false;
    m_queue.WriteBuffer(m_vertexBuffer, 0, m_vertices.data(), vertexBytes);
    m_queue.WriteBuffer(m_indexBuffer, 0, m_indices.data(), indexBytes);
    m_queue.WriteBuffer(m_cameraBuffer, 0, &m_cameraData, sizeof(m_cameraData));

    if (m_shadowEnabled) {
        wgpu::RenderPassDepthStencilAttachment depthAttachment;
        depthAttachment.view = m_shadowView;
        depthAttachment.depthLoadOp = wgpu::LoadOp::Clear;
        depthAttachment.depthStoreOp = wgpu::StoreOp::Store;
        depthAttachment.depthClearValue = 1.0f;
        wgpu::RenderPassDescriptor descriptor;
        descriptor.colorAttachmentCount = 0;
        descriptor.colorAttachments = nullptr;
        descriptor.depthStencilAttachment = &depthAttachment;
        wgpu::RenderPassEncoder shadowPass = encoder.BeginRenderPass(&descriptor);
        shadowPass.SetPipeline(m_shadowPipeline);
        shadowPass.SetBindGroup(0, m_shadowCameraGroup);
        shadowPass.SetVertexBuffer(0, m_vertexBuffer, 0, vertexBytes);
        shadowPass.SetIndexBuffer(m_indexBuffer, wgpu::IndexFormat::Uint32, 0, indexBytes);
        for (const WebDrawRange &range : m_drawRanges) {
            if (range.castsShadows)
                shadowPass.DrawIndexed(range.indexCount, 1, range.firstIndex, 0, 0);
        }
        shadowPass.End();
    }
    m_framePrepared = true;
    return true;
}

bool WebSceneRenderer::RenderPrepared(wgpu::RenderPassEncoder pass)
{
    if (!m_framePrepared || !pass)
        return false;
    const uint64_t vertexBytes = m_vertices.size() * sizeof(WebVertex);
    const uint64_t indexBytes = m_indices.size() * sizeof(uint32_t);

    if (m_drawSky && m_skyPipeline) {
        pass.SetPipeline(m_skyPipeline);
        pass.SetBindGroup(0, m_cameraGroup);
        pass.SetBindGroup(1, m_defaultMaterialTextureGroup);
        pass.Draw(3, 1, 0, 0);
    }

    pass.SetBindGroup(0, m_cameraGroup);
    pass.SetVertexBuffer(0, m_vertexBuffer, 0, vertexBytes);
    pass.SetIndexBuffer(m_indexBuffer, wgpu::IndexFormat::Uint32, 0, indexBytes);
    bool transparentPipeline = false;
    pass.SetPipeline(m_opaquePipeline);
    size_t transparentCount = 0;
    size_t lineCount = 0;
    for (const WebDrawRange &range : m_drawRanges) {
        if (range.transparent != transparentPipeline) {
            transparentPipeline = range.transparent;
            pass.SetPipeline(transparentPipeline ? m_transparentPipeline : m_opaquePipeline);
        }
        transparentCount += range.transparent ? 1u : 0u;
        lineCount += range.line ? 1u : 0u;
        pass.SetBindGroup(1, range.materialTextureGroup ? range.materialTextureGroup : m_defaultMaterialTextureGroup);
        pass.DrawIndexed(range.indexCount, 1, range.firstIndex, 0, 0);
    }
    if (!m_reportedFirstFrame) {
        std::printf("INFERNUX_WEB_SCENE_RENDER_READY vertices=%zu indices=%zu draws=%zu transparent=%zu material=pbr\n",
                    m_vertices.size(), m_indices.size(), m_drawRanges.size(), transparentCount);
        if (m_drawSky)
            std::printf("INFERNUX_WEB_SKY_READY mode=procedural\n");
        if (m_shadowEnabled)
            std::printf("INFERNUX_WEB_SHADOW_READY resolution=%u\n", m_shadowResolution);
        m_reportedFirstFrame = true;
    }
    if (lineCount > 0 && !m_reportedLineDraw) {
        std::printf("INFERNUX_WEB_LINE_DRAW_READY draws=%zu expansion=camera-facing alpha=vertex\n", lineCount);
        m_reportedLineDraw = true;
    }
    return true;
}

} // namespace infernux::web
