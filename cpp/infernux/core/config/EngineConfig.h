#pragma once

/**
 * @file EngineConfig.h
 * @brief Centralized runtime engine configuration.
 *
 * All engine constants that were previously hardcoded across multiple files
 * are consolidated here. Values can be modified at runtime from C++ or Python
 * before the corresponding subsystem initializes.
 *
 * Python binding: `_Infernux.EngineConfig` (see BindingInfernux.cpp)
 */

#include <cstddef>
#include <cstdint>
#include <glm/glm.hpp>

namespace infernux
{

struct EngineConfig
{
    // ========================================================================
    // Rendering — Descriptor Pools
    // ========================================================================

    /// Initial maximum number of materials before descriptor pool expansion.
    uint32_t maxMaterialsPerPool = 256;

    /// Number of UBO descriptors allocated per material slot in the pool.
    uint32_t uboDescriptorsPerMaterial = 4;

    /// Number of combined-image-sampler descriptors allocated per material slot.
    uint32_t samplerDescriptorsPerMaterial = 8;

    /// Number of fullscreen descriptor sets allocated per frame-in-flight.
    uint32_t fullscreenDescriptorSetsPerFrame = 128;

    /// Number of fullscreen sampled-image descriptors allocated per frame-in-flight.
    uint32_t fullscreenSamplerDescriptorsPerFrame = 256;

    // ========================================================================
    // Rendering — Textures
    // ========================================================================

    /// Whether to auto-generate mipmaps when loading textures (unless size is 1×1).
    bool enableMipmap = true;

    /// Max anisotropy multiplier (1.0 = use device max). Values < 1.0 scale down.
    float anisotropyScale = 1.0f;

    // ========================================================================
    // Rendering — Render Queue Ranges
    // ========================================================================

    /// User-space render queue range (materials created by user scripts).
    int32_t userQueueMin = 0;
    int32_t userQueueMax = 9999;

    /// Opaque queue range used by the built-in forward/deferred pipelines.
    int32_t opaqueQueueMin = 0;
    int32_t opaqueQueueMax = 2500;

    /// Transparent queue range used by the built-in forward/deferred pipelines.
    int32_t transparentQueueMin = 2501;
    int32_t transparentQueueMax = 5000;

    /// Shadow-caster queue range used by the built-in forward/deferred pipelines.
    int32_t shadowCasterQueueMin = 0;
    int32_t shadowCasterQueueMax = 2999;

    /// Component gizmos queue range (script-side, depth-tested).
    int32_t componentGizmoQueueMin = 10000;
    int32_t componentGizmoQueueMax = 20000;

    /// Editor gizmos queue range (grid, etc.).
    int32_t editorGizmoQueueMin = 20001;
    int32_t editorGizmoQueueMax = 25000;

    /// Editor tools queue range (translate/rotate/scale handles).
    int32_t editorToolsQueueMin = 32501;
    int32_t editorToolsQueueMax = 32700;

    /// Skybox render queue.
    int32_t skyboxQueue = 32767;

    // ========================================================================
    // Rendering — Swapchain
    // ========================================================================

    /// Preferred number of swapchain images (double/triple buffering).
    uint32_t preferredSwapchainImageCount = 3;

    /// Max frames in flight for the renderer.
    uint32_t maxFramesInFlight = 2;

    // ========================================================================
    // Physics — Jolt Configuration
    // ========================================================================

    /// Jolt temp allocator size in bytes.
    size_t physicsTempAllocatorSize = 256 * 1024 * 1024; // 256 MB

    /// Maximum number of physics jobs in the Jolt job system.
    uint32_t physicsMaxJobs = 4096;

    /// Maximum number of physics barriers.
    uint32_t physicsMaxBarriers = 16;

    /// Maximum physics bodies in the simulation.
    uint32_t physicsMaxBodies = 65536;

    /// Maximum body pairs for contact tracking.
    uint32_t physicsMaxBodyPairs = 65536;

    /// Maximum contact constraints.
    uint32_t physicsMaxContactConstraints = 65536;

    /// Number of collision substeps per fixed physics step.
    /// Unity-compatible default: one simulation step per 0.02-second tick.
    /// Fast bodies should opt into continuous collision detection instead of
    /// charging every body for a second global substep.
    int physicsCollisionSteps = 1;

    /// Backend-native Jolt solver defaults. These are not numerically
    /// interchangeable with PhysX/Unity iteration counts: one Jolt velocity
    /// step does not provide a usable friction solve for stacked contacts.
    int physicsVelocitySteps = 10;
    int physicsPositionSteps = 3;
    float physicsPenetrationSlop = 0.002f;
    float physicsSpeculativeContactDistance = 0.01f;
    float physicsLinearCastMaxPenetration = 0.1f;
    float physicsBaumgarte = 0.15f;
    float physicsMaxPenetrationDistance = 0.05f;
    float physicsLinearCastThreshold = 0.5f;

    /// Relative normal speed below which restitution is suppressed (m/s).
    float physicsMinVelocityForRestitution = 2.0f;

    /// Minimum quiet time before a dynamic body may enter sleep (seconds).
    float physicsTimeBeforeSleep = 0.5f;

    /// Maximum tracked point velocity for sleep eligibility (m/s).
    // Unity exposes a mass-normalized energy threshold of 0.005. Jolt uses a
    // point-velocity threshold, whose translational equivalent is sqrt(2E)=0.1 m/s.
    float physicsPointVelocitySleepThreshold = 0.1f;

    /// Default gravity vector.
    glm::vec3 physicsGravity{0.0f, -9.81f, 0.0f};

    /// Maximum concurrent physics jobs in the shared JobSystem (0 = auto).
    uint32_t physicsMaxConcurrency = 0;

    // ========================================================================
    // Physics — Default Collider Properties
    // ========================================================================

    /// Default dynamic friction for new colliders [0..1].
    float defaultColliderFriction = 0.6f;

    /// Default bounciness (restitution) for new colliders [0..1].
    float defaultColliderBounciness = 0.0f;

    // ========================================================================
    // Physics — Default Rigidbody Properties
    // ========================================================================

    float defaultRigidbodyMass = 1.0f;
    float defaultRigidbodyDrag = 0.0f;
    float defaultRigidbodyAngularDrag = 0.05f;
    float defaultMaxAngularVelocity = 7.0f;  // rad/s
    float defaultMaxLinearVelocity = 500.0f; // m/s

    // ========================================================================
    // Physics — Layers
    // ========================================================================

    /// Number of game-layer slots.
    uint32_t physicsLayerCount = 32;

    /// Default layer mask for queries (all layers except layer 2 by convention).
    uint32_t defaultQueryLayerMask = 0xFFFFFFFFu & ~(1u << 2);

    // ========================================================================
    // Singleton access
    // ========================================================================

    static EngineConfig &Get();

  private:
    EngineConfig() = default;
};

} // namespace infernux
