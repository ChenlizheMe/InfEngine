#include "InxMesh.h"

#include <function/resources/InxSkinnedMesh/InxSkinnedMesh.h>

#include <core/log/InxLog.h>

#include <limits>
#include <stdexcept>

namespace infernux
{

size_t InxMesh::GetRuntimeMemoryBytes() const noexcept
{
    size_t bytes = sizeof(*this) + m_name.capacity() + m_guid.capacity() + m_filePath.capacity();
    bytes += m_vertices.capacity() * sizeof(Vertex);
    bytes += m_indices.capacity() * sizeof(uint32_t);
    bytes += m_subMeshes.capacity() * sizeof(SubMesh);
    for (const auto &subMesh : m_subMeshes)
        bytes += subMesh.name.capacity();
    bytes += m_materialSlotNames.capacity() * sizeof(std::string);
    for (const auto &name : m_materialSlotNames)
        bytes += name.capacity();
    bytes += m_materialSlotData.capacity() * sizeof(MaterialSlotData);
    bytes += m_nodeNames.capacity() * sizeof(std::string);
    for (const auto &name : m_nodeNames)
        bytes += name.capacity();
    if (m_skinnedData)
        bytes += m_skinnedData->GetRuntimeMemoryBytes();
    return bytes;
}

void InxMesh::SetSkinnedData(std::shared_ptr<const InxSkinnedMesh> skinnedData)
{
    if (skinnedData && !skinnedData->IsValid())
        throw std::invalid_argument("InxMesh cannot attach invalid skinned data");
    m_skinnedData = std::move(skinnedData);
    ++m_generation;
}

void InxMesh::SetData(std::vector<Vertex> vertices, std::vector<uint32_t> indices, std::vector<SubMesh> subMeshes)
{
    m_vertices = std::move(vertices);
    m_indices = std::move(indices);
    m_subMeshes = std::move(subMeshes);

    RecalculateBounds();
    ++m_generation;

    INXLOG_DEBUG("InxMesh::SetData: '", m_name, "' — ", m_vertices.size(), " verts, ", m_indices.size(), " indices, ",
                 m_subMeshes.size(), " submesh(es)");
}

void InxMesh::RecalculateBounds()
{
    if (m_vertices.empty()) {
        m_boundsMin = glm::vec3(0.0f);
        m_boundsMax = glm::vec3(0.0f);
        return;
    }

    constexpr float INF = std::numeric_limits<float>::max();
    m_boundsMin = glm::vec3(INF);
    m_boundsMax = glm::vec3(-INF);

    for (const auto &v : m_vertices) {
        m_boundsMin = glm::min(m_boundsMin, v.pos);
        m_boundsMax = glm::max(m_boundsMax, v.pos);
    }
}

} // namespace infernux
