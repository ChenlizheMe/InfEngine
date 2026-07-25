#pragma once

#include "InxRenderStruct.h"
#include <cmath>
#include <cstdint>
#include <glm/glm.hpp>
#include <memory>
#include <vector>

namespace infernux
{

class Scene;
class InxMaterial;

/**
 * @brief Editor 3D manipulation tools (translate / rotate / scale handles).
 *
 * Generates draw calls for the currently active 3D gizmo at the selected
 * object's position. The geometry is constructed once and cached; only the
 * world matrix changes each frame.
 *
 * Rendering uses a dedicated queue range (32501-32700) drawn in a separate
 * _EditorTools pass that sits above the _EditorGizmos pass, with depth test
 * disabled so the handles are always visible.
 *
 * Supported modes:
 *  - **Translate** (W): three arrows plus XY/XZ/YZ plane squares.
 *  - **Rotate**    (E): three torus rings, one per axis.
 *  - **Scale**     (R): three lines with cube endpoints, XY/XZ/YZ plane squares,
 *                       and a grey center cube for uniform XYZ scale.
 *
 * Hover / drag interaction is handled on the Python side via the existing
 * pick_scene_object_id() system.  Python calls SetHighlightedAxis() to
 * change the handle colour on hover.
 */
class EditorTools
{
  public:
    /// Active tool mode
    enum class ToolMode
    {
        None,      ///< No tool active (Q)
        Translate, ///< Move tool (W)
        Rotate,    ///< Rotate tool (E)
        Scale      ///< Scale tool (R)
    };

    /// Which handle is being hovered/dragged
    enum class HandleAxis
    {
        None,
        X,
        Y,
        Z,
        XY,
        XZ,
        YZ,
        Center ///< Uniform scale cube at the gizmo origin (Scale tool only)
    };

    EditorTools();
    ~EditorTools() = default;

    // ====================================================================
    // Configuration
    // ====================================================================

    void SetToolMode(ToolMode mode);
    [[nodiscard]] ToolMode GetToolMode() const
    {
        return m_mode;
    }

    void SetHandleSize(float size)
    {
        m_handleSize = size;
    }
    [[nodiscard]] float GetHandleSize() const
    {
        return m_handleSize;
    }

    /// Set the highlighted (hovered) handle and rebuild mesh vertex colours.
    /// @param axis None / X / Y / Z / XY / XZ / YZ / Center
    void SetHighlightedAxis(HandleAxis axis);

    [[nodiscard]] HandleAxis GetHighlightedAxis() const
    {
        return m_highlightedAxis;
    }

    /// Enable/disable local coordinate mode (gizmo aligns to object rotation)
    void SetLocalMode(bool local)
    {
        m_localMode = local;
    }
    [[nodiscard]] bool GetLocalMode() const
    {
        return m_localMode;
    }

    // ====================================================================
    // Draw call generation
    // ====================================================================

    /**
     * @brief Build draw calls for the active 3D tool at the selected object.
     *
     * @param material      Unlit material for handle rendering (queue 32501+)
     * @param selectedObjId ID of the selected object (0 = none → empty result)
     * @param activeScene   Scene to look up the object's Transform
     * @param cameraPos     Camera position for constant-size scaling
     * @return DrawCallResult containing one DrawCall per handle element
     */
    [[nodiscard]] DrawCallResult GetDrawCalls(std::shared_ptr<InxMaterial> material, uint64_t selectedObjId,
                                              Scene *activeScene, const glm::vec3 &cameraPos);

    // ====================================================================
    // Gizmo handle object IDs — used by both C++ and Python for identification
    // ====================================================================

    static constexpr uint64_t EDITOR_TOOL_BASE_ID = 0xEDED000000000000ULL;
    static constexpr uint64_t X_AXIS_ID = EDITOR_TOOL_BASE_ID | 1;
    static constexpr uint64_t Y_AXIS_ID = EDITOR_TOOL_BASE_ID | 2;
    static constexpr uint64_t Z_AXIS_ID = EDITOR_TOOL_BASE_ID | 3;
    static constexpr uint64_t XY_PLANE_ID = EDITOR_TOOL_BASE_ID | 4;
    static constexpr uint64_t XZ_PLANE_ID = EDITOR_TOOL_BASE_ID | 5;
    static constexpr uint64_t YZ_PLANE_ID = EDITOR_TOOL_BASE_ID | 6;
    static constexpr uint64_t CENTER_ID = EDITOR_TOOL_BASE_ID | 7;

    static constexpr float AXIS_LENGTH = 1.0f;
    /// Plane handles sit in the positive quadrant with one corner at the origin.
    static constexpr float PLANE_OFFSET = 0.0f;
    static constexpr float PLANE_SIZE = 0.28f;
    /// Half-extent of the uniform-scale cube at the gizmo origin.
    static constexpr float CENTER_CUBE_HALF = 0.07f;

    // ====================================================================
    // Reserved queue range
    // ====================================================================

    static constexpr int QUEUE_MIN = 32501;
    static constexpr int QUEUE_MAX = 32700;

  private:
    // ---- Geometry builders ----
    void BuildTranslateHandleMeshes();
    void BuildRotateHandleMeshes();
    void BuildScaleHandleMeshes();
    void RebuildActiveMeshes(); ///< Rebuild meshes for the current mode

    // Build a cylinder along +Y from y=0 to y=length
    static void BuildCylinder(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, float radius, float length,
                              int segments, const glm::vec3 &color);

    // Build a cone along +Y with base at y=baseY, tip at y=baseY+height
    static void BuildCone(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, float radius, float height,
                          float baseY, int segments, const glm::vec3 &color);

    // Build a torus (ring) in the XZ plane centred at origin
    static void BuildTorus(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, float majorRadius, float tubeRadius,
                           int majorSegs, int tubeSegs, const glm::vec3 &color);

    // Build a small cube centred at (0, centreY, 0)
    static void BuildCube(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, float halfSize, float centreY,
                          const glm::vec3 &color);

    // Build a square plane handle inside the positive quadrant of a two-axis plane.
    static void BuildPlaneQuad(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, const glm::vec3 &origin,
                               const glm::vec3 &axisU, const glm::vec3 &axisV, float offset, float size,
                               const glm::vec3 &color);

    ToolMode m_mode = ToolMode::Translate;
    HandleAxis m_highlightedAxis = HandleAxis::None;
    float m_handleSize = 1.75f; // Base size multiplier (visual scale vs legacy 1.0)
    bool m_localMode = false;  // true = align gizmo to object's local rotation

    // ---- Cached per-axis geometry (in local space) ----
    // Shared across all modes: X/Y/Z per-axis vertex & index arrays.
    // The builders fill these; GetDrawCalls applies mode-appropriate rotations.
    bool m_meshesBuilt = false;
    bool m_meshDirty = false; // Set after highlight change to force GPU re-upload
    // X-axis handle
    std::vector<Vertex> m_arrowXVerts;
    std::vector<uint32_t> m_arrowXInds;
    // Y-axis handle
    std::vector<Vertex> m_arrowYVerts;
    std::vector<uint32_t> m_arrowYInds;
    // Z-axis handle
    std::vector<Vertex> m_arrowZVerts;
    std::vector<uint32_t> m_arrowZInds;
    // XY plane handle
    std::vector<Vertex> m_planeXYVerts;
    std::vector<uint32_t> m_planeXYInds;
    // XZ plane handle
    std::vector<Vertex> m_planeXZVerts;
    std::vector<uint32_t> m_planeXZInds;
    // YZ plane handle
    std::vector<Vertex> m_planeYZVerts;
    std::vector<uint32_t> m_planeYZInds;
    // Uniform scale cube at the origin (Scale tool)
    std::vector<Vertex> m_centerCubeVerts;
    std::vector<uint32_t> m_centerCubeInds;
};

} // namespace infernux
