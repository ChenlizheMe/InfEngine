#pragma once

#include "InxRenderStruct.h"
#include <cstdint>
#include <glm/glm.hpp>
#include <memory>
#include <mutex>
#include <vector>

namespace infernux
{

class InxMaterial;

/**
 * @brief Buffer that receives packed gizmo geometry from Python and produces DrawCalls.
 *
 * Python-side Gizmos/GizmosCollector packs all per-frame gizmo primitives
 * into flat vertex/index arrays plus a descriptor list, then uploads them
 * in a single call via SetData().  The C++ side stores the data and
 * produces DrawCall entries consumed by ScriptableRenderContext::SubmitCulling().
 *
 * Queue range: 10000-20000 (_ComponentGizmos pass, depth-tested).
 *
 * Object IDs use prefix 0xEDED_GIZM_xxxx_xxxx to avoid collision with
 * scene objects and editor tools.
 */
class GizmosDrawCallBuffer
{
  public:
    /// Queue range for component gizmos (depth-tested, rendered before editor gizmos)
    static constexpr int QUEUE_MIN = 10000;
    static constexpr int QUEUE_MAX = 20000;

    /// Object ID prefix for component gizmos
    static constexpr uint64_t OBJECT_ID_PREFIX = 0xEDED612D00000000ULL;

    /// Object ID prefix for component gizmo icons (billboards).
    /// Uses a distinct prefix so per-object GPU buffers don't collide
    /// with the game object's own mesh buffers.
    static constexpr uint64_t ICON_ID_PREFIX = 0xEDED713D00000000ULL;

    static constexpr uint32_t ICON_KIND_DEFAULT = 0;
    static constexpr uint32_t ICON_KIND_CAMERA = 1;
    static constexpr uint32_t ICON_KIND_LIGHT = 2;
    static constexpr uint32_t ICON_KIND_PARTICLE = 3;

    /// Billboard materials for the built-in icon kinds. Any missing entry falls
    /// back to @c fallback.
    struct IconMaterials
    {
        std::shared_ptr<InxMaterial> fallback;
        std::shared_ptr<InxMaterial> camera;
        std::shared_ptr<InxMaterial> light;
        std::shared_ptr<InxMaterial> particle;

        [[nodiscard]] const std::shared_ptr<InxMaterial> &Resolve(uint32_t iconKind) const
        {
            switch (iconKind) {
            case ICON_KIND_CAMERA:
                if (camera)
                    return camera;
                break;
            case ICON_KIND_LIGHT:
                if (light)
                    return light;
                break;
            case ICON_KIND_PARTICLE:
                if (particle)
                    return particle;
                break;
            default:
                break;
            }
            return fallback;
        }
    };

    GizmosDrawCallBuffer() = default;
    ~GizmosDrawCallBuffer() = default;

    // Non-copyable
    GizmosDrawCallBuffer(const GizmosDrawCallBuffer &) = delete;
    GizmosDrawCallBuffer &operator=(const GizmosDrawCallBuffer &) = delete;

    /**
     * @brief Descriptor for a single gizmo draw call within the packed buffer.
     *
     * Each descriptor identifies a contiguous range of indices within the
     * shared vertex/index arrays, along with a world-space transform.
     */
    struct DrawDescriptor
    {
        uint32_t indexStart = 0; ///< Offset into the shared index array
        uint32_t indexCount = 0; ///< Number of indices for this draw
        float worldMatrix[16];   ///< Column-major 4x4 world transform
    };

    /**
     * @brief An icon entry for billboard rendering at a world position.
     *
     * Icons are rendered as camera-facing diamond quads (TRIANGLE_LIST).
     * Each icon carries the actual GameObject ID so clicking it selects
     * the owning object (Unity-style component icons).
     */
    struct IconEntry
    {
        glm::vec3 position{0.0f};              ///< World-space position
        uint64_t objectId = 0;                 ///< Owning GameObject ID (for picking)
        glm::vec3 color{1.0f};                 ///< Icon tint color (RGB)
        uint32_t iconKind = ICON_KIND_DEFAULT; ///< Built-in icon kind for material selection
    };

    /**
     * @brief Upload a complete frame's worth of gizmo geometry.
     *
     * Replaces any previous data.  Called once per frame by the Python
     * GizmosCollector before SubmitCulling().
     *
     * @param vertices  Flat array of Vertex structs
     * @param indices   Flat array of uint32 indices
     * @param descriptors  Per-draw-call descriptors
     */
    void SetData(std::vector<Vertex> vertices, std::vector<uint32_t> indices, std::vector<DrawDescriptor> descriptors);

    /**
     * @brief Clear all buffered data (e.g. when no gizmos to draw).
     */
    void Clear();

    /**
     * @brief Check if buffer has any data to draw.
     */
    [[nodiscard]] bool HasData() const;

    /**
     * @brief Build DrawCalls from the buffered data.
     *
     * Each DrawDescriptor becomes one DrawCall with:
     *   - material = gizmoMaterial (unlit vertex-color)
     *   - objectId = OBJECT_ID_PREFIX | descriptorIndex
     *   - forceBufferUpdate = true (immediate-mode: data changes every frame)
     *
     * @param gizmoMaterial  Material for gizmo rendering (vertex-color, unlit)
     * @return DrawCallResult containing all gizmo draw calls
     */
    [[nodiscard]] DrawCallResult GetDrawCalls(std::shared_ptr<InxMaterial> gizmoMaterial) const;

    // ====================================================================
    // Icon billboard API — Unity-style clickable component icons
    // ====================================================================

    /**
     * @brief Upload a frame's worth of icon entries.
     *
     * Replaces previous icon data.  Called once per frame by Python
     * GizmosCollector alongside SetData().
     */
    void SetIconData(std::vector<IconEntry> entries);

    /**
     * @brief Clear all icon data.
     */
    void ClearIcons();

    /**
     * @brief Check if any icon data exists.
     */
    [[nodiscard]] bool HasIconData() const;

    /**
     * @brief Build DrawCalls for icon billboard quads.
     *
     * Each IconEntry becomes one DrawCall with:
     *   - 4 vertices forming a camera-facing diamond quad
     *   - material = iconMaterial (TRIANGLE_LIST, unlit vertex-color)
     *   - objectId = IconEntry::objectId (the actual GameObject ID)
     *   - Constant angular size relative to distance from camera
     *
     * @param materials     Per-kind icon billboard materials
     * @param cameraPos     Editor camera world position (for constant-size scaling)
     * @param cameraRight   Editor camera world-space right axis
     * @param cameraUp      Editor camera world-space up axis
     * @return DrawCallResult containing all icon draw calls
     */
    [[nodiscard]] DrawCallResult GetIconDrawCalls(const IconMaterials &materials, const glm::vec3 &cameraPos,
                                                  const glm::vec3 &cameraRight, const glm::vec3 &cameraUp) const;

    /**
     * @brief Get icon entries for picking tests.
     */
    [[nodiscard]] const std::vector<IconEntry> &GetIconEntries() const
    {
        return m_iconEntries;
    }

    /// Angular size factor: icon world-size = distance * ICON_SIZE_FACTOR
    static constexpr float ICON_SIZE_FACTOR = 0.036f;

    /// Minimum half-size used when an icon is extremely close to the camera.
    static constexpr float ICON_MIN_WORLD_SIZE = 0.10f;

  private:
    std::vector<Vertex> m_vertices;
    std::vector<uint32_t> m_indices;
    std::vector<DrawDescriptor> m_descriptors;

    // Per-descriptor vertex/index slices cached for stable pointers
    // (DrawCall requires const pointers that remain valid until next SetData)
    mutable std::vector<std::vector<Vertex>> m_slicedVertices;
    mutable std::vector<std::vector<uint32_t>> m_slicedIndices;
    mutable bool m_slicesDirty = true;

    /// @brief Rebuild per-descriptor vertex/index slices from the packed arrays.
    void RebuildSlices() const;

    // ---- Icon billboard data ----
    std::vector<IconEntry> m_iconEntries;
    mutable std::vector<std::vector<Vertex>> m_iconSlicedVertices;
    mutable std::vector<std::vector<uint32_t>> m_iconSlicedIndices;
    mutable bool m_iconSlicesDirty = true;
};

} // namespace infernux
