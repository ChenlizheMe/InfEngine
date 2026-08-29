#include "EditorGizmos.h"
#include <core/config/MathConstants.h>
#include <core/log/InxLog.h>
#include <function/scene/Scene.h>

namespace infernux
{

EditorGizmos::EditorGizmos()
{
    CreateGridMesh();
}

const std::vector<Vertex> &EditorGizmos::GetGridVertices()
{
    if (m_gridDirty) {
        CreateGridMesh();
        m_gridDirty = false;
    }
    return m_gridVertices;
}

const std::vector<uint32_t> &EditorGizmos::GetGridIndices()
{
    if (m_gridDirty) {
        CreateGridMesh();
        m_gridDirty = false;
    }
    return m_gridIndices;
}

DrawCallResult EditorGizmos::GetDrawCalls(std::shared_ptr<InxMaterial> gizmoMaterial,
                                          std::shared_ptr<InxMaterial> gridMaterial, uint64_t selectedObjectId,
                                          Scene *activeScene, const glm::vec3 &cameraPos)
{
    DrawCallResult result;

    // Update selected object's world matrix for outline (handles Transform changes)
    if (selectedObjectId != 0 && activeScene) {
        GameObject *selectedObj = activeScene->FindByID(selectedObjectId);
        if (selectedObj && selectedObj->IsActiveInHierarchy()) {
            Transform *transform = selectedObj->GetTransform();
            if (transform) {
                UpdateSelectionWorldMatrix(transform->GetWorldMatrix());
            }
        }
    }

    // Grid draw call — use persistent cached grid data
    {
        if (m_showGrid) {
            const auto &gridVerts = GetGridVertices();
            const auto &gridInds = GetGridIndices();

            if (!gridVerts.empty()) {
                DrawCall dc;
                dc.indexStart = 0;
                dc.indexCount = static_cast<uint32_t>(gridInds.size());
                dc.worldMatrix = glm::mat4(1.0f); // Grid shader consumes clip-space quad positions directly
                dc.material = gridMaterial ? gridMaterial : gizmoMaterial;
                dc.objectId = 0; // Gizmo objectId = 0
                dc.identity = RenderProxyHandle::Synthetic(RenderDomain::EditorGizmo, 1).MakeDrawIdentity();
                dc.meshVertices = &gridVerts;
                dc.meshIndices = &gridInds;
                result.drawCalls.push_back(dc);
            }
        }
    }

    // NOTE: Outline is now rendered via post-process passes in InxVkCoreModular
    // (RenderOutlineMask + RenderOutlineComposite). The old inverted-hull draw
    // call has been removed.

    return result;
}

void EditorGizmos::SetSelectionOutline(const std::vector<glm::vec3> &positions, const std::vector<glm::vec3> &normals,
                                       const std::vector<uint32_t> &indices, const glm::mat4 &worldMatrix)
{
    m_selectionPositions = positions;
    m_selectionNormals = normals;
    m_selectionIndices = indices;
    m_selectionWorldMatrix = worldMatrix;
    m_hasSelectionOutline = true;
    m_selectionDirty = true;
}

void EditorGizmos::ClearSelectionOutline()
{
    m_hasSelectionOutline = false;
    m_selectionPositions.clear();
    m_selectionNormals.clear();
    m_selectionIndices.clear();
    m_outlineVertices.clear();
    m_outlineIndices.clear();
    m_selectionDirty = true;
}

void EditorGizmos::GetOutlineMeshData(std::vector<Vertex> &outVertices, std::vector<uint32_t> &outIndices)
{
    if (m_selectionDirty) {
        CreateOutlineMesh();
        m_selectionDirty = false;
    }

    // Transform vertices to world space with scale applied
    // This is necessary because the UBO uses identity model matrix
    glm::mat4 scaledWorldMatrix = GetOutlineScaledWorldMatrix();

    outVertices.clear();
    outVertices.reserve(m_outlineVertices.size());

    for (const Vertex &v : m_outlineVertices) {
        Vertex transformed = v;
        glm::vec4 worldPos = scaledWorldMatrix * glm::vec4(v.pos[0], v.pos[1], v.pos[2], 1.0f);
        transformed.pos[0] = worldPos.x;
        transformed.pos[1] = worldPos.y;
        transformed.pos[2] = worldPos.z;
        outVertices.push_back(transformed);
    }

    outIndices = m_outlineIndices;
}

void EditorGizmos::CreateOutlineMesh()
{
    m_outlineVertices.clear();
    m_outlineIndices.clear();

    if (!m_hasSelectionOutline || m_selectionPositions.empty() || m_selectionIndices.empty()) {
        INXLOG_DEBUG("CreateOutlineMesh: no selection data, hasOutline=", m_hasSelectionOutline,
                     ", positions=", m_selectionPositions.size(), ", indices=", m_selectionIndices.size());
        return;
    }

    INXLOG_DEBUG("CreateOutlineMesh: creating outline with ", m_selectionPositions.size(), " positions");

    // Build smooth normals by averaging face normals at each vertex
    std::vector<glm::vec3> smoothNormals(m_selectionPositions.size(), glm::vec3(0.0f));

    // Accumulate face normals for each vertex
    for (size_t i = 0; i + 2 < m_selectionIndices.size(); i += 3) {
        uint32_t i0 = m_selectionIndices[i];
        uint32_t i1 = m_selectionIndices[i + 1];
        uint32_t i2 = m_selectionIndices[i + 2];

        if (i0 >= m_selectionPositions.size() || i1 >= m_selectionPositions.size() ||
            i2 >= m_selectionPositions.size()) {
            continue;
        }

        const glm::vec3 &v0 = m_selectionPositions[i0];
        const glm::vec3 &v1 = m_selectionPositions[i1];
        const glm::vec3 &v2 = m_selectionPositions[i2];

        glm::vec3 edge1 = v1 - v0;
        glm::vec3 edge2 = v2 - v0;
        glm::vec3 faceNormal = glm::cross(edge1, edge2);

        // Weight by triangle area (length of cross product)
        smoothNormals[i0] += faceNormal;
        smoothNormals[i1] += faceNormal;
        smoothNormals[i2] += faceNormal;
    }

    // Normalize all smooth normals
    for (auto &n : smoothNormals) {
        float len = glm::length(n);
        if (len > kEpsilon) {
            n /= len;
        } else {
            n = glm::vec3(0.0f, 1.0f, 0.0f); // Default up
        }
    }

    // Use smooth normals if provided normals are empty, otherwise blend
    std::vector<glm::vec3> finalNormals;
    if (m_selectionNormals.size() == m_selectionPositions.size()) {
        finalNormals = m_selectionNormals;
        // Normalize provided normals
        for (auto &n : finalNormals) {
            float len = glm::length(n);
            if (len > kEpsilon) {
                n /= len;
            }
        }
    } else {
        finalNormals = smoothNormals;
    }

    // Create outline vertices
    // Store outline width in vertex color.x (shader will read this for expansion)
    const glm::vec3 outlineColor(m_outlineWidth, 0.5f, 0.0f); // x = width, y,z for color info
    glm::vec4 tangent(1.0f, 0.0f, 0.0f, 1.0f);

    for (size_t i = 0; i < m_selectionPositions.size(); ++i) {
        const glm::vec3 &pos = m_selectionPositions[i];
        const glm::vec3 &normal = finalNormals[i];

        Vertex v = Vertex::CreateFull(pos, normal, tangent, outlineColor, {0.0f, 0.0f});
        m_outlineVertices.push_back(v);
    }

    // Copy indices (keep original winding order)
    // With front face culling enabled, only back faces will be rendered
    // These back faces are the "inner shell" when the mesh is scaled up
    for (size_t i = 0; i + 2 < m_selectionIndices.size(); i += 3) {
        uint32_t i0 = m_selectionIndices[i];
        uint32_t i1 = m_selectionIndices[i + 1];
        uint32_t i2 = m_selectionIndices[i + 2];

        // Keep original winding order
        m_outlineIndices.push_back(i0);
        m_outlineIndices.push_back(i1);
        m_outlineIndices.push_back(i2);
    }
}

glm::mat4 EditorGizmos::GetOutlineScaledWorldMatrix() const
{
    // Decompose the world matrix to apply uniform scale
    // Extract translation (object center in world space)
    glm::vec3 translation = glm::vec3(m_selectionWorldMatrix[3]);

    // Extract scale (length of each basis vector)
    glm::vec3 scale;
    scale.x = glm::length(glm::vec3(m_selectionWorldMatrix[0]));
    scale.y = glm::length(glm::vec3(m_selectionWorldMatrix[1]));
    scale.z = glm::length(glm::vec3(m_selectionWorldMatrix[2]));

    // Extract rotation (normalize basis vectors)
    glm::mat4 rotation = glm::mat4(1.0f);
    if (scale.x > kEpsilon)
        rotation[0] = m_selectionWorldMatrix[0] / scale.x;
    if (scale.y > kEpsilon)
        rotation[1] = m_selectionWorldMatrix[1] / scale.y;
    if (scale.z > kEpsilon)
        rotation[2] = m_selectionWorldMatrix[2] / scale.z;
    rotation[3] = glm::vec4(0.0f, 0.0f, 0.0f, 1.0f);

    // Calculate distance from camera to object center
    float distance = glm::length(m_cameraPosition - translation);

    // Calculate outline offset based on distance
    // The further the object, the larger the absolute offset needs to be
    // to maintain constant screen-space width
    // outlineOffset = pixelWidth * distance * factor
    // factor depends on FOV and screen resolution, we use an empirical value
    float distanceFactor = distance * 0.003f * m_outlinePixelWidth;

    // Calculate average object size for relative scaling
    float avgScale = (scale.x + scale.y + scale.z) / 3.0f;
    if (avgScale < kEpsilon)
        avgScale = 1.0f;

    // Add distance-based offset to scale
    glm::vec3 scaledScale = scale + glm::vec3(distanceFactor);

    // Reconstruct matrix: T * R * S
    glm::mat4 result = glm::mat4(1.0f);
    result = glm::translate(result, translation);
    result = result * rotation;
    result = glm::scale(result, scaledScale);

    return result;
}

void EditorGizmos::CreateGridMesh()
{
    m_gridVertices.clear();
    m_gridIndices.clear();

    // Infinite grid: a fullscreen clip-space quad. The shader unprojects each
    // fragment ray and intersects it with the XZ plane at Y=0.
    glm::vec3 color(0.5f, 0.5f, 0.5f); // Not used by procedural shader, kept for vertex format
    glm::vec3 normal(0.0f, 0.0f, 1.0f);
    glm::vec4 tangent(1.0f, 0.0f, 0.0f, 1.0f);

    m_gridVertices.push_back(Vertex::CreateFull({-1.0f, -1.0f, 0.0f}, normal, tangent, color, {0.0f, 0.0f}));
    m_gridVertices.push_back(Vertex::CreateFull({1.0f, -1.0f, 0.0f}, normal, tangent, color, {1.0f, 0.0f}));
    m_gridVertices.push_back(Vertex::CreateFull({1.0f, 1.0f, 0.0f}, normal, tangent, color, {1.0f, 1.0f}));
    m_gridVertices.push_back(Vertex::CreateFull({-1.0f, 1.0f, 0.0f}, normal, tangent, color, {0.0f, 1.0f}));

    // Two triangles covering the whole viewport.
    m_gridIndices.push_back(0);
    m_gridIndices.push_back(1);
    m_gridIndices.push_back(2);
    m_gridIndices.push_back(2);
    m_gridIndices.push_back(3);
    m_gridIndices.push_back(0);
}

} // namespace infernux
