#include "EditorTools.h"
#include "InxLog.h"
#include <algorithm>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/Scene.h>
#include <glm/gtc/matrix_transform.hpp>
#include <glm/gtc/quaternion.hpp>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace infernux
{

// ============================================================================
// Axis colors
// ============================================================================

static constexpr glm::vec3 COLOR_X_DEFAULT{1.0f, 0.2f, 0.2f};
static constexpr glm::vec3 COLOR_Y_DEFAULT{0.2f, 1.0f, 0.2f};
static constexpr glm::vec3 COLOR_Z_DEFAULT{0.2f, 0.4f, 1.0f};
static constexpr glm::vec3 COLOR_HIGHLIGHT{1.0f, 1.0f, 0.0f}; // Yellow highlight

// ============================================================================
// Construction
// ============================================================================

EditorTools::EditorTools()
{
    RebuildActiveMeshes();
}

// ============================================================================
// SetToolMode — switch modes and rebuild geometry
// ============================================================================

void EditorTools::SetToolMode(ToolMode mode)
{
    if (mode == m_mode)
        return;
    m_mode = mode;
    m_meshesBuilt = false;
    m_highlightedAxis = HandleAxis::None;
    RebuildActiveMeshes();
}

// ============================================================================
// Geometry helpers — cylinder & cone (shared by translate + scale)
// ============================================================================

void EditorTools::BuildCylinder(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, float radius, float length,
                                int segments, const glm::vec3 &color)
{
    uint32_t baseIdx = static_cast<uint32_t>(verts.size());

    for (int i = 0; i <= segments; ++i) {
        float angle = static_cast<float>(i) / static_cast<float>(segments) * 2.0f * static_cast<float>(M_PI);
        float cx = std::cos(angle) * radius;
        float cz = std::sin(angle) * radius;
        glm::vec3 normal = glm::normalize(glm::vec3(cx, 0.0f, cz));
        verts.push_back(Vertex::Create(glm::vec3(cx, 0.0f, cz), normal, glm::vec2(0.0f), color));
        verts.push_back(Vertex::Create(glm::vec3(cx, length, cz), normal, glm::vec2(0.0f), color));
    }

    for (int i = 0; i < segments; ++i) {
        uint32_t bl = baseIdx + static_cast<uint32_t>(i) * 2;
        uint32_t tl = bl + 1;
        uint32_t br = bl + 2;
        uint32_t tr = bl + 3;
        inds.push_back(bl);
        inds.push_back(br);
        inds.push_back(tl);
        inds.push_back(tl);
        inds.push_back(br);
        inds.push_back(tr);
    }

    // Bottom cap
    uint32_t bottomCenter = static_cast<uint32_t>(verts.size());
    verts.push_back(Vertex::Create(glm::vec3(0.0f, 0.0f, 0.0f), glm::vec3(0.0f, -1.0f, 0.0f), glm::vec2(0.0f), color));
    for (int i = 0; i < segments; ++i) {
        uint32_t cur = baseIdx + static_cast<uint32_t>(i) * 2;
        uint32_t nxt = baseIdx + static_cast<uint32_t>(i + 1) * 2;
        inds.push_back(bottomCenter);
        inds.push_back(nxt);
        inds.push_back(cur);
    }

    // Top cap
    uint32_t topCenter = static_cast<uint32_t>(verts.size());
    verts.push_back(Vertex::Create(glm::vec3(0.0f, length, 0.0f), glm::vec3(0.0f, 1.0f, 0.0f), glm::vec2(0.0f), color));
    for (int i = 0; i < segments; ++i) {
        uint32_t cur = baseIdx + static_cast<uint32_t>(i) * 2 + 1;
        uint32_t nxt = baseIdx + static_cast<uint32_t>(i + 1) * 2 + 1;
        inds.push_back(topCenter);
        inds.push_back(cur);
        inds.push_back(nxt);
    }
}

void EditorTools::BuildCone(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, float radius, float height,
                            float baseY, int segments, const glm::vec3 &color)
{
    uint32_t baseIdx = static_cast<uint32_t>(verts.size());

    for (int i = 0; i <= segments; ++i) {
        float angle = static_cast<float>(i) / static_cast<float>(segments) * 2.0f * static_cast<float>(M_PI);
        float cx = std::cos(angle) * radius;
        float cz = std::sin(angle) * radius;
        float slopeLen = std::sqrt(radius * radius + height * height);
        glm::vec3 normal = glm::normalize(
            glm::vec3(cx / radius * height / slopeLen, radius / slopeLen, cz / radius * height / slopeLen));
        verts.push_back(Vertex::Create(glm::vec3(cx, baseY, cz), normal, glm::vec2(0.0f), color));
    }

    uint32_t tipIdx = static_cast<uint32_t>(verts.size());
    verts.push_back(
        Vertex::Create(glm::vec3(0.0f, baseY + height, 0.0f), glm::vec3(0.0f, 1.0f, 0.0f), glm::vec2(0.0f), color));

    for (int i = 0; i < segments; ++i) {
        uint32_t cur = baseIdx + static_cast<uint32_t>(i);
        uint32_t nxt = baseIdx + static_cast<uint32_t>(i + 1);
        inds.push_back(cur);
        inds.push_back(nxt);
        inds.push_back(tipIdx);
    }

    uint32_t capCenter = static_cast<uint32_t>(verts.size());
    verts.push_back(Vertex::Create(glm::vec3(0.0f, baseY, 0.0f), glm::vec3(0.0f, -1.0f, 0.0f), glm::vec2(0.0f), color));
    for (int i = 0; i < segments; ++i) {
        uint32_t cur = baseIdx + static_cast<uint32_t>(i);
        uint32_t nxt = baseIdx + static_cast<uint32_t>(i + 1);
        inds.push_back(capCenter);
        inds.push_back(nxt);
        inds.push_back(cur);
    }
}

// ============================================================================
// Geometry helper — torus (ring) for Rotate tool
// ============================================================================

void EditorTools::BuildTorus(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, float majorRadius,
                             float tubeRadius, int majorSegs, int tubeSegs, const glm::vec3 &color)
{
    // Torus in the XZ plane (Y = up).
    // majorRadius = distance from centre to tube centre.
    // tubeRadius  = radius of the tube cross-section.
    uint32_t baseIdx = static_cast<uint32_t>(verts.size());

    for (int i = 0; i <= majorSegs; ++i) {
        float theta = static_cast<float>(i) / static_cast<float>(majorSegs) * 2.0f * static_cast<float>(M_PI);
        float cosTheta = std::cos(theta);
        float sinTheta = std::sin(theta);

        for (int j = 0; j <= tubeSegs; ++j) {
            float phi = static_cast<float>(j) / static_cast<float>(tubeSegs) * 2.0f * static_cast<float>(M_PI);
            float cosPhi = std::cos(phi);
            float sinPhi = std::sin(phi);

            float x = (majorRadius + tubeRadius * cosPhi) * cosTheta;
            float y = tubeRadius * sinPhi;
            float z = (majorRadius + tubeRadius * cosPhi) * sinTheta;

            glm::vec3 normal = glm::normalize(glm::vec3(cosPhi * cosTheta, sinPhi, cosPhi * sinTheta));

            verts.push_back(Vertex::Create(glm::vec3(x, y, z), normal, glm::vec2(0.0f), color));
        }
    }

    // Indices
    for (int i = 0; i < majorSegs; ++i) {
        for (int j = 0; j < tubeSegs; ++j) {
            uint32_t a = baseIdx + static_cast<uint32_t>(i * (tubeSegs + 1) + j);
            uint32_t b = baseIdx + static_cast<uint32_t>((i + 1) * (tubeSegs + 1) + j);
            uint32_t c = baseIdx + static_cast<uint32_t>((i + 1) * (tubeSegs + 1) + (j + 1));
            uint32_t d = baseIdx + static_cast<uint32_t>(i * (tubeSegs + 1) + (j + 1));
            inds.push_back(a);
            inds.push_back(b);
            inds.push_back(c);
            inds.push_back(a);
            inds.push_back(c);
            inds.push_back(d);
        }
    }
}

// ============================================================================
// Geometry helper — small cube for Scale tool endpoints
// ============================================================================

void EditorTools::BuildCube(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, float halfSize, float centreY,
                            const glm::vec3 &color)
{
    // Axis-aligned cube centred at (0, centreY, 0)
    float lo = centreY - halfSize;
    float hi = centreY + halfSize;

    uint32_t base = static_cast<uint32_t>(verts.size());

    // 8 vertices
    glm::vec3 positions[8] = {
        {-halfSize, lo, -halfSize}, {+halfSize, lo, -halfSize}, {+halfSize, lo, +halfSize}, {-halfSize, lo, +halfSize},
        {-halfSize, hi, -halfSize}, {+halfSize, hi, -halfSize}, {+halfSize, hi, +halfSize}, {-halfSize, hi, +halfSize},
    };

    // 6 faces, 2 triangles each
    // Face normals
    static const glm::vec3 faceNormals[6] = {
        {0, -1, 0}, {0, 1, 0}, {0, 0, -1}, {0, 0, 1}, {-1, 0, 0}, {1, 0, 0},
    };
    static const int faceIndices[6][4] = {
        {0, 3, 2, 1}, // bottom (-Y)
        {4, 5, 6, 7}, // top (+Y)
        {0, 1, 5, 4}, // front (-Z)
        {2, 3, 7, 6}, // back (+Z)
        {0, 4, 7, 3}, // left (-X)
        {1, 2, 6, 5}, // right (+X)
    };

    for (int f = 0; f < 6; ++f) {
        uint32_t fbase = static_cast<uint32_t>(verts.size());
        for (int v = 0; v < 4; ++v) {
            verts.push_back(Vertex::Create(positions[faceIndices[f][v]], faceNormals[f], glm::vec2(0.0f), color));
        }
        inds.push_back(fbase);
        inds.push_back(fbase + 1);
        inds.push_back(fbase + 2);
        inds.push_back(fbase);
        inds.push_back(fbase + 2);
        inds.push_back(fbase + 3);
    }
}

void EditorTools::BuildPlaneQuad(std::vector<Vertex> &verts, std::vector<uint32_t> &inds, const glm::vec3 &origin,
                                 const glm::vec3 &axisU, const glm::vec3 &axisV, float offset, float size,
                                 const glm::vec3 &color)
{
    const glm::vec3 p0 = origin + axisU * offset + axisV * offset;
    const glm::vec3 p1 = origin + axisU * (offset + size) + axisV * offset;
    const glm::vec3 p2 = origin + axisU * (offset + size) + axisV * (offset + size);
    const glm::vec3 p3 = origin + axisU * offset + axisV * (offset + size);
    const glm::vec3 normal = glm::normalize(glm::cross(axisU, axisV));
    const uint32_t base = static_cast<uint32_t>(verts.size());

    verts.push_back(Vertex::Create(p0, normal, glm::vec2(0.0f, 0.0f), color));
    verts.push_back(Vertex::Create(p1, normal, glm::vec2(1.0f, 0.0f), color));
    verts.push_back(Vertex::Create(p2, normal, glm::vec2(1.0f, 1.0f), color));
    verts.push_back(Vertex::Create(p3, normal, glm::vec2(0.0f, 1.0f), color));

    inds.push_back(base + 0);
    inds.push_back(base + 1);
    inds.push_back(base + 2);
    inds.push_back(base + 0);
    inds.push_back(base + 2);
    inds.push_back(base + 3);

    verts.push_back(Vertex::Create(p0, -normal, glm::vec2(0.0f, 0.0f), color));
    verts.push_back(Vertex::Create(p3, -normal, glm::vec2(0.0f, 1.0f), color));
    verts.push_back(Vertex::Create(p2, -normal, glm::vec2(1.0f, 1.0f), color));
    verts.push_back(Vertex::Create(p1, -normal, glm::vec2(1.0f, 0.0f), color));

    inds.push_back(base + 4);
    inds.push_back(base + 5);
    inds.push_back(base + 6);
    inds.push_back(base + 4);
    inds.push_back(base + 6);
    inds.push_back(base + 7);
}

// ============================================================================
// Build translate handles: three arrows (cylinder + cone), built along +Y
// ============================================================================

void EditorTools::BuildTranslateHandleMeshes()
{
    constexpr float shaftRadius = 0.02f;
    constexpr float shaftLength = 0.8f;
    constexpr float coneRadius = 0.06f;
    constexpr float coneHeight = 0.2f;
    constexpr int segments = 12;

    glm::vec3 xColor = (m_highlightedAxis == HandleAxis::X) ? COLOR_HIGHLIGHT : COLOR_X_DEFAULT;
    glm::vec3 yColor = (m_highlightedAxis == HandleAxis::Y) ? COLOR_HIGHLIGHT : COLOR_Y_DEFAULT;
    glm::vec3 zColor = (m_highlightedAxis == HandleAxis::Z) ? COLOR_HIGHLIGHT : COLOR_Z_DEFAULT;
    glm::vec3 xyColor = (m_highlightedAxis == HandleAxis::XY) ? COLOR_HIGHLIGHT : glm::vec3(1.0f, 0.8f, 0.2f);
    glm::vec3 xzColor = (m_highlightedAxis == HandleAxis::XZ) ? COLOR_HIGHLIGHT : glm::vec3(1.0f, 0.35f, 0.35f);
    glm::vec3 yzColor = (m_highlightedAxis == HandleAxis::YZ) ? COLOR_HIGHLIGHT : glm::vec3(0.35f, 1.0f, 0.8f);

    m_arrowXVerts.clear();
    m_arrowXInds.clear();
    BuildCylinder(m_arrowXVerts, m_arrowXInds, shaftRadius, shaftLength, segments, xColor);
    BuildCone(m_arrowXVerts, m_arrowXInds, coneRadius, coneHeight, shaftLength, segments, xColor);

    m_arrowYVerts.clear();
    m_arrowYInds.clear();
    BuildCylinder(m_arrowYVerts, m_arrowYInds, shaftRadius, shaftLength, segments, yColor);
    BuildCone(m_arrowYVerts, m_arrowYInds, coneRadius, coneHeight, shaftLength, segments, yColor);

    m_arrowZVerts.clear();
    m_arrowZInds.clear();
    BuildCylinder(m_arrowZVerts, m_arrowZInds, shaftRadius, shaftLength, segments, zColor);
    BuildCone(m_arrowZVerts, m_arrowZInds, coneRadius, coneHeight, shaftLength, segments, zColor);

    m_planeXYVerts.clear();
    m_planeXYInds.clear();
    BuildPlaneQuad(m_planeXYVerts, m_planeXYInds, glm::vec3(0.0f), glm::vec3(1.0f, 0.0f, 0.0f),
                   glm::vec3(0.0f, 1.0f, 0.0f), PLANE_OFFSET, PLANE_SIZE, xyColor);

    m_planeXZVerts.clear();
    m_planeXZInds.clear();
    BuildPlaneQuad(m_planeXZVerts, m_planeXZInds, glm::vec3(0.0f), glm::vec3(1.0f, 0.0f, 0.0f),
                   glm::vec3(0.0f, 0.0f, 1.0f), PLANE_OFFSET, PLANE_SIZE, xzColor);

    m_planeYZVerts.clear();
    m_planeYZInds.clear();
    BuildPlaneQuad(m_planeYZVerts, m_planeYZInds, glm::vec3(0.0f), glm::vec3(0.0f, 1.0f, 0.0f),
                   glm::vec3(0.0f, 0.0f, 1.0f), PLANE_OFFSET, PLANE_SIZE, yzColor);

    m_meshesBuilt = true;
}

// ============================================================================
// Build rotate handles: three torus rings in their respective planes
// ============================================================================
// Each ring is built in the XZ plane, then GetDrawCalls rotates it into
// the correct axis plane.  X-ring → ring around X axis (rotate 90° Y→Z),
// Y-ring → ring around Y axis (stays in XZ), Z-ring → ring around Z axis.
// ============================================================================

void EditorTools::BuildRotateHandleMeshes()
{
    constexpr float majorRadius = 0.85f; // matches translate arrow length roughly
    constexpr float tubeRadius = 0.015f;
    constexpr int majorSegs = 48;
    constexpr int tubeSegs = 8;

    glm::vec3 xColor = (m_highlightedAxis == HandleAxis::X) ? COLOR_HIGHLIGHT : COLOR_X_DEFAULT;
    glm::vec3 yColor = (m_highlightedAxis == HandleAxis::Y) ? COLOR_HIGHLIGHT : COLOR_Y_DEFAULT;
    glm::vec3 zColor = (m_highlightedAxis == HandleAxis::Z) ? COLOR_HIGHLIGHT : COLOR_Z_DEFAULT;

    m_arrowXVerts.clear();
    m_arrowXInds.clear();
    BuildTorus(m_arrowXVerts, m_arrowXInds, majorRadius, tubeRadius, majorSegs, tubeSegs, xColor);

    m_arrowYVerts.clear();
    m_arrowYInds.clear();
    BuildTorus(m_arrowYVerts, m_arrowYInds, majorRadius, tubeRadius, majorSegs, tubeSegs, yColor);

    m_arrowZVerts.clear();
    m_arrowZInds.clear();
    BuildTorus(m_arrowZVerts, m_arrowZInds, majorRadius, tubeRadius, majorSegs, tubeSegs, zColor);

    m_meshesBuilt = true;
}

// ============================================================================
// Build scale handles: thin shaft + small cube at the endpoint
// ============================================================================

void EditorTools::BuildScaleHandleMeshes()
{
    constexpr float shaftRadius = 0.02f;
    constexpr float shaftLength = 0.75f;
    constexpr float cubeHalf = 0.04f;
    constexpr int segments = 12;
    static constexpr glm::vec3 COLOR_CENTER_DEFAULT{0.72f, 0.72f, 0.74f};

    glm::vec3 xColor = (m_highlightedAxis == HandleAxis::X) ? COLOR_HIGHLIGHT : COLOR_X_DEFAULT;
    glm::vec3 yColor = (m_highlightedAxis == HandleAxis::Y) ? COLOR_HIGHLIGHT : COLOR_Y_DEFAULT;
    glm::vec3 zColor = (m_highlightedAxis == HandleAxis::Z) ? COLOR_HIGHLIGHT : COLOR_Z_DEFAULT;
    glm::vec3 centerColor = (m_highlightedAxis == HandleAxis::Center) ? COLOR_HIGHLIGHT : COLOR_CENTER_DEFAULT;

    m_arrowXVerts.clear();
    m_arrowXInds.clear();
    BuildCylinder(m_arrowXVerts, m_arrowXInds, shaftRadius, shaftLength, segments, xColor);
    BuildCube(m_arrowXVerts, m_arrowXInds, cubeHalf, shaftLength + cubeHalf, xColor);

    m_arrowYVerts.clear();
    m_arrowYInds.clear();
    BuildCylinder(m_arrowYVerts, m_arrowYInds, shaftRadius, shaftLength, segments, yColor);
    BuildCube(m_arrowYVerts, m_arrowYInds, cubeHalf, shaftLength + cubeHalf, yColor);

    m_arrowZVerts.clear();
    m_arrowZInds.clear();
    BuildCylinder(m_arrowZVerts, m_arrowZInds, shaftRadius, shaftLength, segments, zColor);
    BuildCube(m_arrowZVerts, m_arrowZInds, cubeHalf, shaftLength + cubeHalf, zColor);

    // Scale tool keeps only the three axis handles + center cube (no planes).
    m_planeXYVerts.clear();
    m_planeXYInds.clear();
    m_planeXZVerts.clear();
    m_planeXZInds.clear();
    m_planeYZVerts.clear();
    m_planeYZInds.clear();

    m_centerCubeVerts.clear();
    m_centerCubeInds.clear();
    BuildCube(m_centerCubeVerts, m_centerCubeInds, CENTER_CUBE_HALF, 0.0f, centerColor);

    m_meshesBuilt = true;
}

// ============================================================================
// RebuildActiveMeshes — dispatcher
// ============================================================================

void EditorTools::RebuildActiveMeshes()
{
    switch (m_mode) {
    case ToolMode::Translate:
        BuildTranslateHandleMeshes();
        break;
    case ToolMode::Rotate:
        BuildRotateHandleMeshes();
        break;
    case ToolMode::Scale:
        BuildScaleHandleMeshes();
        break;
    default:
        m_meshesBuilt = true;
        break;
    }
    m_meshDirty = true;
}

// ============================================================================

void EditorTools::SetHighlightedAxis(HandleAxis axis)
{
    if (axis == m_highlightedAxis)
        return;
    m_highlightedAxis = axis;
    RebuildActiveMeshes();
}

// ============================================================================
// GetDrawCalls — produce draw calls for active axis/plane handles
// ============================================================================

DrawCallResult EditorTools::GetDrawCalls(std::shared_ptr<InxMaterial> material, uint64_t selectedObjId,
                                         Scene *activeScene, const glm::vec3 &cameraPos)
{
    DrawCallResult result;

    if (m_mode == ToolMode::None || selectedObjId == 0 || !activeScene) {
        return result;
    }

    if (!m_meshesBuilt) {
        RebuildActiveMeshes();
    }

    GameObject *selectedObj = activeScene->FindByID(selectedObjId);
    if (!selectedObj || !selectedObj->IsActiveInHierarchy()) {
        return result;
    }

    Transform *transform = selectedObj->GetTransform();
    if (!transform) {
        return result;
    }

    glm::vec3 objPos = transform->GetPosition();

    float dist = glm::length(cameraPos - objPos);
    float scale = dist * 0.15f * m_handleSize;
    if (scale < 0.01f) {
        scale = 0.01f;
    }

    glm::mat4 baseTransform;
    if (m_localMode) {
        // Local mode: include object's world rotation so gizmo axes align
        // with the object's local coordinate system
        glm::quat worldRot = transform->GetWorldRotation();
        baseTransform = glm::translate(glm::mat4(1.0f), objPos) * glm::mat4_cast(worldRot) *
                        glm::scale(glm::mat4(1.0f), glm::vec3(scale));
    } else {
        baseTransform = glm::translate(glm::mat4(1.0f), objPos) * glm::scale(glm::mat4(1.0f), glm::vec3(scale));
    }

    // ---- Per-mode axis rotations ----
    // Translate & Scale: arrow/shaft built along +Y → rotate to point along each axis.
    // Rotate: torus built in XZ plane (ring around Y) → rotate so each ring
    //         circles its respective axis.
    //
    // Translate/Scale rotations:
    //   X: rotate -90° around Z  (Y → X)
    //   Y: identity
    //   Z: rotate +90° around X  (Y → Z)
    //
    // Rotate rotations:
    //   X-ring: rotate +90° around Z  (XZ ring → YZ ring, circles X axis)
    //   Y-ring: identity              (XZ ring circles Y axis)
    //   Z-ring: rotate +90° around X  (XZ ring → XY ring, circles Z axis)

    glm::mat4 xRotation, yRotation, zRotation;

    if (m_mode == ToolMode::Rotate) {
        xRotation = glm::rotate(glm::mat4(1.0f), glm::radians(90.0f), glm::vec3(0.0f, 0.0f, 1.0f));
        yRotation = glm::mat4(1.0f);
        zRotation = glm::rotate(glm::mat4(1.0f), glm::radians(90.0f), glm::vec3(1.0f, 0.0f, 0.0f));
    } else {
        // Translate & Scale — same axis mapping
        xRotation = glm::rotate(glm::mat4(1.0f), glm::radians(-90.0f), glm::vec3(0.0f, 0.0f, 1.0f));
        yRotation = glm::mat4(1.0f);
        zRotation = glm::rotate(glm::mat4(1.0f), glm::radians(90.0f), glm::vec3(1.0f, 0.0f, 0.0f));
    }

    const bool dirty = m_meshDirty;
    m_meshDirty = false;

    // X-axis draw call
    {
        DrawCall dc;
        dc.indexStart = 0;
        dc.indexCount = static_cast<uint32_t>(m_arrowXInds.size());
        dc.worldMatrix = baseTransform * xRotation;
        dc.material = material;
        dc.objectId = X_AXIS_ID;
        dc.identity = RenderProxyHandle::Synthetic(RenderDomain::EditorTool, dc.objectId).MakeDrawIdentity();
        dc.meshVertices = &m_arrowXVerts;
        dc.meshIndices = &m_arrowXInds;
        dc.forceBufferUpdate = dirty;
        result.drawCalls.push_back(dc);
    }

    // Y-axis draw call
    {
        DrawCall dc;
        dc.indexStart = 0;
        dc.indexCount = static_cast<uint32_t>(m_arrowYInds.size());
        dc.worldMatrix = baseTransform * yRotation;
        dc.material = material;
        dc.objectId = Y_AXIS_ID;
        dc.identity = RenderProxyHandle::Synthetic(RenderDomain::EditorTool, dc.objectId).MakeDrawIdentity();
        dc.meshVertices = &m_arrowYVerts;
        dc.meshIndices = &m_arrowYInds;
        dc.forceBufferUpdate = dirty;
        result.drawCalls.push_back(dc);
    }

    // Z-axis draw call
    {
        DrawCall dc;
        dc.indexStart = 0;
        dc.indexCount = static_cast<uint32_t>(m_arrowZInds.size());
        dc.worldMatrix = baseTransform * zRotation;
        dc.material = material;
        dc.objectId = Z_AXIS_ID;
        dc.identity = RenderProxyHandle::Synthetic(RenderDomain::EditorTool, dc.objectId).MakeDrawIdentity();
        dc.meshVertices = &m_arrowZVerts;
        dc.meshIndices = &m_arrowZInds;
        dc.forceBufferUpdate = dirty;
        result.drawCalls.push_back(dc);
    }

    if (m_mode == ToolMode::Translate) {
        DrawCall xyDc;
        xyDc.indexStart = 0;
        xyDc.indexCount = static_cast<uint32_t>(m_planeXYInds.size());
        xyDc.worldMatrix = baseTransform;
        xyDc.material = material;
        xyDc.objectId = XY_PLANE_ID;
        xyDc.identity = RenderProxyHandle::Synthetic(RenderDomain::EditorTool, xyDc.objectId).MakeDrawIdentity();
        xyDc.meshVertices = &m_planeXYVerts;
        xyDc.meshIndices = &m_planeXYInds;
        xyDc.forceBufferUpdate = dirty;
        result.drawCalls.push_back(xyDc);

        DrawCall xzDc;
        xzDc.indexStart = 0;
        xzDc.indexCount = static_cast<uint32_t>(m_planeXZInds.size());
        xzDc.worldMatrix = baseTransform;
        xzDc.material = material;
        xzDc.objectId = XZ_PLANE_ID;
        xzDc.identity = RenderProxyHandle::Synthetic(RenderDomain::EditorTool, xzDc.objectId).MakeDrawIdentity();
        xzDc.meshVertices = &m_planeXZVerts;
        xzDc.meshIndices = &m_planeXZInds;
        xzDc.forceBufferUpdate = dirty;
        result.drawCalls.push_back(xzDc);

        DrawCall yzDc;
        yzDc.indexStart = 0;
        yzDc.indexCount = static_cast<uint32_t>(m_planeYZInds.size());
        yzDc.worldMatrix = baseTransform;
        yzDc.material = material;
        yzDc.objectId = YZ_PLANE_ID;
        yzDc.identity = RenderProxyHandle::Synthetic(RenderDomain::EditorTool, yzDc.objectId).MakeDrawIdentity();
        yzDc.meshVertices = &m_planeYZVerts;
        yzDc.meshIndices = &m_planeYZInds;
        yzDc.forceBufferUpdate = dirty;
        result.drawCalls.push_back(yzDc);
    }

    if (m_mode == ToolMode::Scale && !m_centerCubeInds.empty()) {
        DrawCall centerDc;
        centerDc.indexStart = 0;
        centerDc.indexCount = static_cast<uint32_t>(m_centerCubeInds.size());
        centerDc.worldMatrix = baseTransform;
        centerDc.material = material;
        centerDc.objectId = CENTER_ID;
        centerDc.identity =
            RenderProxyHandle::Synthetic(RenderDomain::EditorTool, centerDc.objectId).MakeDrawIdentity();
        centerDc.meshVertices = &m_centerCubeVerts;
        centerDc.meshIndices = &m_centerCubeInds;
        centerDc.forceBufferUpdate = dirty;
        result.drawCalls.push_back(centerDc);
    }

    auto depthKey = [&cameraPos](const DrawCall &dc) {
        if (!dc.meshVertices || dc.meshVertices->empty())
            return 0.0f;
        glm::vec3 center(0.0f);
        for (const auto &v : *dc.meshVertices)
            center += glm::vec3(dc.worldMatrix * glm::vec4(v.pos, 1.0f));
        center /= static_cast<float>(dc.meshVertices->size());
        const glm::vec3 delta = cameraPos - center;
        return glm::dot(delta, delta);
    };

    std::stable_sort(result.drawCalls.begin(), result.drawCalls.end(),
                     [&](const DrawCall &a, const DrawCall &b) { return depthKey(a) > depthKey(b); });

    return result;
}

} // namespace infernux
