#include "GizmosDrawCallBuffer.h"

#include <algorithm>
#include <core/config/MathConstants.h>
#include <core/log/InxLog.h>
#include <cstring>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <glm/glm.hpp>
#include <unordered_set>

namespace infernux
{

// ============================================================================
// SetData — replace buffer contents with a fresh frame of gizmo geometry
// ============================================================================

void GizmosDrawCallBuffer::SetData(std::vector<Vertex> vertices, std::vector<uint32_t> indices,
                                   std::vector<DrawDescriptor> descriptors)
{
    m_vertices = std::move(vertices);
    m_indices = std::move(indices);
    m_descriptors = std::move(descriptors);
    m_slicesDirty = true;
}

// ============================================================================
// Clear
// ============================================================================

void GizmosDrawCallBuffer::Clear()
{
    m_vertices.clear();
    m_indices.clear();
    m_descriptors.clear();
    m_slicedVertices.clear();
    m_slicedIndices.clear();
    m_slicesDirty = true;

    m_iconEntries.clear();
    m_iconSlicedVertices.clear();
    m_iconSlicedIndices.clear();
    m_iconSlicesDirty = true;
}

// ============================================================================
// HasData
// ============================================================================

bool GizmosDrawCallBuffer::HasData() const
{
    return !m_descriptors.empty();
}

// ============================================================================
// RebuildSlices — split packed arrays into per-descriptor slices
// ============================================================================

void GizmosDrawCallBuffer::RebuildSlices() const
{
    if (!m_slicesDirty)
        return;

    m_slicedVertices.clear();
    m_slicedIndices.clear();
    m_slicedVertices.resize(m_descriptors.size());
    m_slicedIndices.resize(m_descriptors.size());

    for (size_t i = 0; i < m_descriptors.size(); ++i) {
        const auto &desc = m_descriptors[i];

        // Collect all unique vertex indices referenced by this descriptor's
        // index range, then build a compact vertex slice.
        uint32_t idxEnd = desc.indexStart + desc.indexCount;
        if (idxEnd > static_cast<uint32_t>(m_indices.size()))
            idxEnd = static_cast<uint32_t>(m_indices.size());

        // Find min/max vertex index to know the referenced vertex range
        uint32_t minVert = UINT32_MAX;
        uint32_t maxVert = 0;
        for (uint32_t j = desc.indexStart; j < idxEnd; ++j) {
            uint32_t vi = m_indices[j];
            if (vi < minVert)
                minVert = vi;
            if (vi > maxVert)
                maxVert = vi;
        }

        if (minVert > maxVert) {
            // No valid indices
            continue;
        }

        // Copy the referenced vertex range into the slice
        uint32_t vertCount = maxVert - minVert + 1;
        if (maxVert < static_cast<uint32_t>(m_vertices.size())) {
            m_slicedVertices[i].assign(m_vertices.begin() + minVert, m_vertices.begin() + minVert + vertCount);
        }

        // Rebase indices to the slice's local vertex range
        auto &sliceIndices = m_slicedIndices[i];
        sliceIndices.reserve(desc.indexCount);
        for (uint32_t j = desc.indexStart; j < idxEnd; ++j) {
            sliceIndices.push_back(m_indices[j] - minVert);
        }
    }

    m_slicesDirty = false;
}

// ============================================================================
// GetDrawCalls — produce DrawCallResult for SubmitCulling()
// ============================================================================

DrawCallResult GizmosDrawCallBuffer::GetDrawCalls(std::shared_ptr<InxMaterial> gizmoMaterial) const
{
    DrawCallResult result;
    if (m_descriptors.empty())
        return result;

    RebuildSlices();

    result.drawCalls.reserve(m_descriptors.size());

    for (size_t i = 0; i < m_descriptors.size(); ++i) {
        const auto &desc = m_descriptors[i];
        if (m_slicedIndices[i].empty())
            continue;

        // Build world matrix from the flat float[16]
        glm::mat4 world;
        std::memcpy(&world, desc.worldMatrix, sizeof(float) * 16);

        DrawCall dc;
        dc.indexStart = 0; // Each slice starts at 0
        dc.indexCount = static_cast<uint32_t>(m_slicedIndices[i].size());
        dc.worldMatrix = world;
        dc.material = gizmoMaterial;
        dc.objectId = OBJECT_ID_PREFIX | static_cast<uint64_t>(i);
        dc.identity = RenderProxyHandle::Synthetic(RenderDomain::ComponentGizmo, dc.objectId).MakeDrawIdentity();
        dc.meshVertices = &m_slicedVertices[i];
        dc.meshIndices = &m_slicedIndices[i];
        dc.forceBufferUpdate = true; // Immediate-mode: data changes every frame

        result.drawCalls.push_back(dc);
    }

    return result;
}

// ============================================================================
// Icon billboard methods
// ============================================================================

void GizmosDrawCallBuffer::SetIconData(std::vector<IconEntry> entries)
{
    m_iconEntries = std::move(entries);
    m_iconSlicesDirty = true;
}

void GizmosDrawCallBuffer::ClearIcons()
{
    m_iconEntries.clear();
    m_iconSlicedVertices.clear();
    m_iconSlicedIndices.clear();
    m_iconSlicesDirty = true;
}

bool GizmosDrawCallBuffer::HasIconData() const
{
    return !m_iconEntries.empty();
}

DrawCallResult GizmosDrawCallBuffer::GetIconDrawCalls(std::shared_ptr<InxMaterial> defaultIconMaterial,
                                                      std::shared_ptr<InxMaterial> cameraIconMaterial,
                                                      std::shared_ptr<InxMaterial> lightIconMaterial,
                                                      const glm::vec3 &cameraPos, const glm::vec3 &cameraRight,
                                                      const glm::vec3 &cameraUp) const
{
    DrawCallResult result;
    if (m_iconEntries.empty())
        return result;

    glm::vec3 billboardRight = cameraRight;
    glm::vec3 billboardUp = cameraUp;
    if (glm::dot(billboardRight, billboardRight) < kEpsilon) {
        billboardRight = glm::vec3(1.0f, 0.0f, 0.0f);
    } else {
        billboardRight = glm::normalize(billboardRight);
    }
    if (glm::dot(billboardUp, billboardUp) < kEpsilon) {
        billboardUp = glm::vec3(0.0f, 1.0f, 0.0f);
    } else {
        billboardUp = glm::normalize(billboardUp);
    }

    // Rebuild billboard geometry if entries changed
    if (m_iconSlicesDirty) {
        m_iconSlicedVertices.clear();
        m_iconSlicedIndices.clear();
        m_iconSlicedVertices.resize(m_iconEntries.size());
        m_iconSlicedIndices.resize(m_iconEntries.size());
        m_iconSlicesDirty = false;
    }

    result.drawCalls.reserve(m_iconEntries.size());
    std::vector<float> iconDistances;
    iconDistances.reserve(m_iconEntries.size());
    std::vector<std::string> iconMaterialNames;
    iconMaterialNames.reserve(m_iconEntries.size());

    for (size_t i = 0; i < m_iconEntries.size(); ++i) {
        const auto &icon = m_iconEntries[i];

        // Compute billboard orientation
        glm::vec3 toCamera = cameraPos - icon.position;
        float distance = glm::length(toCamera);
        if (distance < 0.001f) {
            // When the editor camera sits on top of an icon (common for scene
            // cameras), keep the icon drawable instead of silently dropping it.
            toCamera = glm::vec3(0.0f, 0.0f, 1.0f);
            distance = 0.0f;
        } else {
            toCamera /= distance; // normalize
        }
        iconDistances.push_back(distance);

        // Constant angular size
        float worldSize = std::max(distance * ICON_SIZE_FACTOR, ICON_MIN_WORLD_SIZE);

        glm::vec3 topLeft = icon.position + billboardUp * worldSize - billboardRight * worldSize;
        glm::vec3 topRight = icon.position + billboardUp * worldSize + billboardRight * worldSize;
        glm::vec3 bottomRight = icon.position - billboardUp * worldSize + billboardRight * worldSize;
        glm::vec3 bottomLeft = icon.position - billboardUp * worldSize - billboardRight * worldSize;

        auto makeVertex = [&](const glm::vec3 &pos, const glm::vec2 &uv) -> Vertex {
            Vertex v;
            v.pos = pos;
            v.normal = toCamera;
            v.tangent = glm::vec4(billboardRight, 1.0f);
            v.color = icon.color;
            v.texCoord = uv;
            return v;
        };

        auto &verts = m_iconSlicedVertices[i];
        verts.clear();
        verts.push_back(makeVertex(topLeft, glm::vec2(0.0f, 0.0f)));
        verts.push_back(makeVertex(topRight, glm::vec2(1.0f, 0.0f)));
        verts.push_back(makeVertex(bottomRight, glm::vec2(1.0f, 1.0f)));
        verts.push_back(makeVertex(bottomLeft, glm::vec2(0.0f, 1.0f)));

        auto &indices = m_iconSlicedIndices[i];
        indices = {0, 1, 2, 0, 2, 3};

        std::shared_ptr<InxMaterial> iconMaterial = defaultIconMaterial;
        if (icon.iconKind == ICON_KIND_CAMERA && cameraIconMaterial) {
            iconMaterial = cameraIconMaterial;
        } else if (icon.iconKind == ICON_KIND_LIGHT && lightIconMaterial) {
            iconMaterial = lightIconMaterial;
        }
        iconMaterialNames.push_back(iconMaterial ? iconMaterial->GetName() : std::string("<null>"));
        if (!iconMaterial) {
            continue;
        }

        DrawCall dc;
        dc.indexStart = 0;
        dc.indexCount = 6;
        dc.worldMatrix = glm::mat4(1.0f); // identity — vertices are in world space
        dc.material = iconMaterial;
        dc.objectId = ICON_ID_PREFIX | icon.objectId; // prefixed to avoid buffer collision
        dc.identity = RenderProxyHandle::Synthetic(RenderDomain::ComponentGizmo, dc.objectId).MakeDrawIdentity();
        dc.meshVertices = &m_iconSlicedVertices[i];
        dc.meshIndices = &m_iconSlicedIndices[i];
        dc.forceBufferUpdate = true;

        result.drawCalls.push_back(dc);
    }

    static size_t s_lastIconEntryCount = static_cast<size_t>(-1);
    static size_t s_lastBuiltIconDrawCallCount = static_cast<size_t>(-1);
    if (s_lastIconEntryCount != m_iconEntries.size() || s_lastBuiltIconDrawCallCount != result.drawCalls.size()) {
        // INXLOG_INFO("GizmoIcons: built ", result.drawCalls.size(), " draw call(s) from ", m_iconEntries.size(),
        //             " icon entr", (m_iconEntries.size() == 1 ? "y" : "ies"), " cameraPos=", cameraPos.x, ",",
        //             cameraPos.y, ",", cameraPos.z);
        for (size_t i = 0; i < m_iconEntries.size(); ++i) {
            const auto &icon = m_iconEntries[i];
            const char *kindName = "default";
            if (icon.iconKind == ICON_KIND_CAMERA) {
                kindName = "camera";
            } else if (icon.iconKind == ICON_KIND_LIGHT) {
                kindName = "light";
            }
            // INXLOG_INFO("GizmoIcons: entry[", i, "] kind=", kindName, " objectId=", icon.objectId,
            //             " pos=", icon.position.x, ",", icon.position.y, ",", icon.position.z,
            //             " distance=", (i < iconDistances.size() ? iconDistances[i] : -1.0f),
            //             " material=", (i < iconMaterialNames.size() ? iconMaterialNames[i] :
            //             std::string("<missing>")));
        }
        s_lastIconEntryCount = m_iconEntries.size();
        s_lastBuiltIconDrawCallCount = result.drawCalls.size();
    }

    return result;
}

} // namespace infernux
