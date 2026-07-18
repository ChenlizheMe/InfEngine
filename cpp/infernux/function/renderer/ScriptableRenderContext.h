#pragma once

#include "InxRenderStruct.h"
#include "ProfileConfig.h"
#include "RenderGraphDescription.h"
#include "RendererList.h"
#include <function/scene/Camera.h>
#include <function/scene/PrimitiveMeshes.h>
#include <function/scene/SceneSystem.h>

#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{

// Forward declarations
class InxVkCoreModular;
class SceneRenderGraph;
class EditorGizmos;
class EditorTools;
class GizmosDrawCallBuffer;
class ParticleDrawCallBuffer;
class InxMaterial;
class CommandBuffer;
class TransientResourcePool;
struct RenderTargetHandle;
enum class RenderCommandType : uint8_t;

// ============================================================================
// CullingResults
// ============================================================================

/**
 * @brief Result of scene culling — visible draw calls ready for filtering.
 *
 * Returned by ScriptableRenderContext::Cull(). Contains all visible
 * draw calls that can then be filtered/sorted by DrawRenderers().
 */
struct CullingResults
{
    RendererList visibleRenderers;
    RendererList shadowCasters;
    uint32_t lightCount = 0; ///< Number of visible lights (populated by Cull)

    [[nodiscard]] size_t visibleObjectCount() const
    {
        return visibleRenderers.Size();
    }
    [[nodiscard]] size_t visibleLightCount() const
    {
        return lightCount;
    }
};

// ============================================================================
// Editor Gizmos Context (internal — not exposed to Python)
// ============================================================================

/**
 * @brief Internal context for auto-appending editor gizmos after Submit().
 *
 * The SRC auto-appends gizmo draw calls so Python RenderPipeline
 * doesn't need to worry about editor-only rendering.
 */
struct EditorGizmosContext
{
    EditorGizmos *gizmos = nullptr;
    EditorTools *editorTools = nullptr;
    GizmosDrawCallBuffer *componentGizmos = nullptr; ///< Component gizmos supplied by the scripting layer
    ParticleDrawCallBuffer *particles = nullptr;     ///< Runtime particle billboard batches
    std::shared_ptr<InxMaterial> gizmoMaterial;
    std::shared_ptr<InxMaterial> gridMaterial;
    std::shared_ptr<InxMaterial> editorToolsMaterial;
    std::shared_ptr<InxMaterial> componentGizmosMaterial;    ///< Material for component gizmos (queue 30000)
    std::shared_ptr<InxMaterial> componentGizmoIconMaterial; ///< Fallback material for icon billboards
    std::shared_ptr<InxMaterial> cameraGizmoIconMaterial;    ///< Textured camera icon material
    std::shared_ptr<InxMaterial> lightGizmoIconMaterial;     ///< Textured light icon material
    std::shared_ptr<InxMaterial> particleMaterial;           ///< Default particle billboard material
    uint64_t selectedObjectId = 0;
    Scene *activeScene = nullptr;
    glm::vec3 cameraPos{0.0f};
};

// ============================================================================
// ScriptableRenderContext
// ============================================================================

/**
 * @brief Unity SRP-style render context.
 *
 * Provides the interface Python RenderPipeline uses to define rendering:
 *   1. SetupCameraProperties() — bind camera VP matrices
 *   2. Cull() — get visible objects
 *   3. ApplyGraph() — apply a Python-defined RenderGraph topology
 *   4. SubmitCulling() — upload all draw calls and execute graph passes
 *
 * Python calls happen once per frame; all heavy work (sorting, GPU uploads)
 * is in C++.
 */
class ScriptableRenderContext
{
  public:
#if INFERNUX_FRAME_PROFILE
    struct ProfileSnapshot
    {
        double cullMs = 0.0;
        double cullEditorMs = 0.0;
        double cullGameMs = 0.0;
        double applyGraphMs = 0.0;
        double submitMs = 0.0;
        double submitBaseMs = 0.0;
        double submitEditorAppendMs = 0.0;
        double ensureBuffersMs = 0.0;
        double cacheGraphMs = 0.0;
        double cullCalls = 0.0;
        double cullEditorCalls = 0.0;
        double cullGameCalls = 0.0;
        double submitCalls = 0.0;
        double baseDrawCalls = 0.0;
        double finalDrawCalls = 0.0;
        double borrowedRendererListSubmits = 0.0;
        double ownedRendererListSubmits = 0.0;
        double materializedDrawCalls = 0.0;
    };

    [[nodiscard]] static ProfileSnapshot GetProfileSnapshot();
    static void ResetProfileSnapshot();
#endif

    ScriptableRenderContext(InxVkCoreModular *vkCore, SceneRenderGraph *graph,
                            const EditorGizmosContext &gizmoCtx = {});

    /// @brief Set camera VP matrices for rendering
    void SetupCameraProperties(Camera *camera);

    /// @brief Cull scene objects visible to the given camera. Returns culling results.
    CullingResults &Cull(Camera *camera);

    // ====================================================================
    // RenderGraph-driven API (replaces DrawRenderers + Submit combo)
    // ====================================================================

    /// @brief Apply a Python-defined RenderGraph topology.
    /// This sets the pass topology on the underlying SceneRenderGraph.
    /// Must be called before Submit() or SubmitCulling().
    void ApplyGraph(const RenderGraphDescription &desc);

    /// Upload only changed graph parameter blocks; does not rebuild topology.
    void UpdateParameterBlocks(const std::vector<GraphParameterBlockUpdate> &updates);

    /// @brief Submit all culling results as full draw calls + execute graph.
    /// Replaces the DrawRenderers() + DrawSkybox() + Submit() combo.
    /// DrawCall filtering is done by RenderGraph pass callbacks.
    /// Consumes the result's owned draw-call arrays. A CullingResults object is
    /// single-submit, matching the context's single-submit lifetime.
    void SubmitCulling(CullingResults &culling);

    /// @brief Single-call render path: setup + cull + apply_graph + submit.
    /// Avoids 3 extra Python→C++ round-trips compared to calling each step separately.
    void RenderWithGraph(Camera *camera, const RenderGraphDescription &desc);

    /// Check whether the active SceneRenderGraph already owns an artifact.
    [[nodiscard]] bool IsGraphRevisionCurrent(uint64_t sourceRevision) const;

    /// Single-call steady-state render path that does not upload graph topology.
    /// Returns false when the caller must fall back to RenderWithGraph().
    bool RenderCompiled(Camera *camera, uint64_t sourceRevision);

    // ====================================================================
    // CommandBuffer integration
    // ====================================================================

    /// @brief Execute a deferred CommandBuffer.
    /// Commands are buffered and actually executed during Submit().
    void ExecuteCommandBuffer(CommandBuffer &cmd);

    /// @brief Static predicate: does the Vulkan backend currently honour @p type?
    ///
    /// The Python-facing CommandBuffer API exposes commands that have not yet
    /// been wired to the Vulkan backend (multi-RT bind, async readback, etc.).
    /// Bindings and tools can call this before recording to surface the
    /// limitation up front instead of relying on the post-hoc warning emitted
    /// inside ProcessPendingCommandBuffers.
    [[nodiscard]] static bool IsCommandImplemented(RenderCommandType type) noexcept;

    // ====================================================================
    // Render target operations
    // ====================================================================

    /// @brief Get a handle representing the final camera render target.
    RenderTargetHandle GetCameraTarget(Camera *camera) const;

    // ====================================================================
    // Global shader parameters (immediate mode)
    // ====================================================================

    void SetGlobalTexture(const std::string &name, RenderTargetHandle handle);
    void SetGlobalFloat(const std::string &name, float value);
    void SetGlobalVector(const std::string &name, float x, float y, float z, float w);

    // ====================================================================
    // Scene access (RenderStack integration)
    // ====================================================================

    /// @brief Get the scene associated with this render context.
    /// Returns the gizmo context's active scene, or falls back to
    /// SceneManager::GetActiveScene().
    [[nodiscard]] Scene *GetScene() const
    {
        return m_scene;
    }

    // ====================================================================
    // TransientResourcePool access
    // ====================================================================

    /// @brief Set the transient resource pool (called by InxRenderer during setup).
    void SetTransientResourcePool(TransientResourcePool *pool)
    {
        m_transientPool = pool;
    }

    /// @brief Get the transient resource pool.
    [[nodiscard]] TransientResourcePool *GetTransientResourcePool() const
    {
        return m_transientPool;
    }

    // ====================================================================
    // Global parameter accessors for CommandBuffer execution
    // ====================================================================

    [[nodiscard]] const std::unordered_map<std::string, float> &GetGlobalFloats() const
    {
        return m_globalFloats;
    }
    [[nodiscard]] const std::unordered_map<std::string, std::array<float, 4>> &GetGlobalVectors() const
    {
        return m_globalVectors;
    }
    [[nodiscard]] const std::unordered_map<std::string, uint32_t> &GetGlobalTextures() const
    {
        return m_globalTextures;
    }

  private:
    InxVkCoreModular *m_vkCore;
    SceneRenderGraph *m_graph;
    EditorGizmosContext m_gizmoCtx;

    // Draw calls accumulated by DrawRenderers() (in submission order)
    std::vector<DrawCall> m_orderedDrawCalls;

    Scene *m_scene = nullptr;
    Camera *m_activeCamera = nullptr;
    glm::mat4 m_cachedView{1.0f};
    glm::mat4 m_cachedProj{1.0f};
    bool m_hasCullData = false;
    CullingResults m_cachedCullingResults; ///< Cached for repeated Cull() calls
    bool m_submitted = false;

    // Deferred CommandBuffer execution state
    TransientResourcePool *m_transientPool = nullptr;
    std::vector<CommandBuffer *> m_pendingCommandBuffers;

    // Global shader parameter state
    std::unordered_map<std::string, float> m_globalFloats;
    std::unordered_map<std::string, std::array<float, 4>> m_globalVectors;
    std::unordered_map<std::string, uint32_t> m_globalTextures; // name → RT handle

    // Handle → pool slot mapping for transient RT resolution
    std::unordered_map<uint32_t, uint32_t> m_handleToSlotMap; // RenderTargetHandle.id → pool slot

    /// @brief Process all pending CommandBuffers' commands (RT management, globals).
    void ProcessPendingCommandBuffers();
};

// ============================================================================
// RenderPipelineCallback — C++ interface for Python render pipelines
// ============================================================================

/**
 * @brief Abstract base for render pipelines.
 *
 * Python classes override Render() via pybind11 trampoline.
 * The engine calls Render() once per frame with a ScriptableRenderContext
 * and the list of active cameras.
 */
class RenderPipelineCallback
{
  public:
    virtual ~RenderPipelineCallback() = default;

    /// @brief Called once per frame to define the rendering pass sequence.
    virtual void Render(ScriptableRenderContext &context, const std::vector<Camera *> &cameras) = 0;

    /// @brief Called when the pipeline is being replaced or engine is shutting down.
    virtual void Dispose()
    {
    }
};

} // namespace infernux
