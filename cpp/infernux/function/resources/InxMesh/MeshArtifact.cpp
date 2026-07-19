#include "MeshArtifact.h"

#include "InxMesh.h"

#include <cstring>
#include <limits>
#include <stdexcept>
#include <vector>

namespace infernux
{
namespace
{
constexpr std::string_view Magic = "INXMESHART";
constexpr uint32_t EndianMarker = 0x01020304U;
constexpr uint32_t MaximumElementCount = 100'000'000U;
constexpr uint32_t MaximumStringBytes = 16U * 1024U * 1024U;

uint64_t Fnv1a64(std::string_view bytes)
{
    uint64_t hash = 14695981039346656037ULL;
    for (const unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

void AppendU32(std::string &out, uint32_t value)
{
    for (unsigned shift = 0; shift < 32; shift += 8)
        out.push_back(static_cast<char>((value >> shift) & 0xffU));
}

void AppendU64(std::string &out, uint64_t value)
{
    for (unsigned shift = 0; shift < 64; shift += 8)
        out.push_back(static_cast<char>((value >> shift) & 0xffU));
}

void AppendFloat(std::string &out, float value)
{
    uint32_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    AppendU32(out, bits);
}

void AppendString(std::string &out, std::string_view value)
{
    if (value.size() > MaximumStringBytes)
        throw std::overflow_error("mesh artifact string exceeds the format limit");
    AppendU32(out, static_cast<uint32_t>(value.size()));
    out.append(value);
}

class Reader final
{
  public:
    explicit Reader(std::string_view bytes) : m_bytes(bytes)
    {
    }

    [[nodiscard]] uint32_t ReadU32()
    {
        Require(sizeof(uint32_t));
        uint32_t value = 0;
        for (unsigned shift = 0; shift < 32; shift += 8)
            value |= static_cast<uint32_t>(static_cast<unsigned char>(m_bytes[m_cursor++])) << shift;
        return value;
    }

    [[nodiscard]] uint64_t ReadU64()
    {
        Require(sizeof(uint64_t));
        uint64_t value = 0;
        for (unsigned shift = 0; shift < 64; shift += 8)
            value |= static_cast<uint64_t>(static_cast<unsigned char>(m_bytes[m_cursor++])) << shift;
        return value;
    }

    [[nodiscard]] float ReadFloat()
    {
        const uint32_t bits = ReadU32();
        float value = 0.0f;
        static_assert(sizeof(bits) == sizeof(value));
        std::memcpy(&value, &bits, sizeof(value));
        return value;
    }

    [[nodiscard]] std::string ReadString()
    {
        const uint32_t size = ReadU32();
        if (size > MaximumStringBytes)
            throw std::invalid_argument("mesh artifact string exceeds the format limit");
        Require(size);
        std::string value(m_bytes.substr(m_cursor, size));
        m_cursor += size;
        return value;
    }

    [[nodiscard]] uint32_t ReadCount()
    {
        const uint32_t count = ReadU32();
        if (count > MaximumElementCount)
            throw std::invalid_argument("mesh artifact element count exceeds the format limit");
        return count;
    }

    [[nodiscard]] size_t Cursor() const noexcept
    {
        return m_cursor;
    }

    [[nodiscard]] bool AtEnd() const noexcept
    {
        return m_cursor == m_bytes.size();
    }

  private:
    void Require(size_t size) const
    {
        if (size > m_bytes.size() - m_cursor)
            throw std::invalid_argument("mesh artifact is truncated");
    }

    std::string_view m_bytes;
    size_t m_cursor = 0;
};

void AppendVec2(std::string &out, const glm::vec2 &value)
{
    AppendFloat(out, value.x);
    AppendFloat(out, value.y);
}

void AppendVec3(std::string &out, const glm::vec3 &value)
{
    AppendFloat(out, value.x);
    AppendFloat(out, value.y);
    AppendFloat(out, value.z);
}

void AppendVec4(std::string &out, const glm::vec4 &value)
{
    AppendFloat(out, value.x);
    AppendFloat(out, value.y);
    AppendFloat(out, value.z);
    AppendFloat(out, value.w);
}

glm::vec2 ReadVec2(Reader &reader)
{
    return {reader.ReadFloat(), reader.ReadFloat()};
}

glm::vec3 ReadVec3(Reader &reader)
{
    return {reader.ReadFloat(), reader.ReadFloat(), reader.ReadFloat()};
}

glm::vec4 ReadVec4(Reader &reader)
{
    return {reader.ReadFloat(), reader.ReadFloat(), reader.ReadFloat(), reader.ReadFloat()};
}

void AppendCount(std::string &out, size_t count)
{
    if (count > MaximumElementCount)
        throw std::overflow_error("mesh artifact element count exceeds the format limit");
    AppendU32(out, static_cast<uint32_t>(count));
}
} // namespace

std::string MeshArtifact::Serialize(const InxMesh &mesh, std::string_view sourceContentHash)
{
    if (sourceContentHash.empty())
        throw std::invalid_argument("mesh artifact requires a source content hash");

    std::string bytes(Magic);
    AppendU32(bytes, EndianMarker);
    AppendString(bytes, sourceContentHash);
    AppendString(bytes, mesh.GetName());

    const auto &vertices = mesh.GetVertices();
    AppendCount(bytes, vertices.size());
    for (const Vertex &vertex : vertices) {
        AppendVec3(bytes, vertex.pos);
        AppendVec3(bytes, vertex.normal);
        AppendVec4(bytes, vertex.tangent);
        AppendVec3(bytes, vertex.color);
        AppendVec2(bytes, vertex.texCoord);
        for (glm::length_t component = 0; component < vertex.boneIndices.length(); ++component)
            AppendU32(bytes, vertex.boneIndices[component]);
        AppendVec4(bytes, vertex.boneWeights);
    }

    const auto &indices = mesh.GetIndices();
    AppendCount(bytes, indices.size());
    for (uint32_t index : indices)
        AppendU32(bytes, index);

    const auto &subMeshes = mesh.GetSubMeshes();
    AppendCount(bytes, subMeshes.size());
    for (const SubMesh &subMesh : subMeshes) {
        AppendU32(bytes, subMesh.indexStart);
        AppendU32(bytes, subMesh.indexCount);
        AppendU32(bytes, subMesh.vertexStart);
        AppendU32(bytes, subMesh.vertexCount);
        AppendU32(bytes, subMesh.materialSlot);
        AppendU32(bytes, subMesh.nodeGroup);
        AppendVec3(bytes, subMesh.boundsMin);
        AppendVec3(bytes, subMesh.boundsMax);
        AppendString(bytes, subMesh.name);
    }

    const auto &slotNames = mesh.GetMaterialSlotNames();
    AppendCount(bytes, slotNames.size());
    for (const auto &name : slotNames)
        AppendString(bytes, name);

    const auto &slotData = mesh.GetMaterialSlotData();
    AppendCount(bytes, slotData.size());
    for (const MaterialSlotData &material : slotData) {
        AppendVec4(bytes, material.baseColor);
        AppendVec4(bytes, material.emissionColor);
        AppendFloat(bytes, material.metallic);
        AppendFloat(bytes, material.smoothness);
        AppendFloat(bytes, material.opacity);
    }

    const auto &nodeNames = mesh.GetNodeNames();
    AppendCount(bytes, nodeNames.size());
    for (const auto &name : nodeNames)
        AppendString(bytes, name);

    AppendU64(bytes, Fnv1a64(bytes));
    return bytes;
}

std::shared_ptr<InxMesh> MeshArtifact::Deserialize(std::string_view bytes, std::string_view expectedSourceContentHash)
{
    if (bytes.size() < Magic.size() + sizeof(uint32_t) + sizeof(uint64_t) || bytes.substr(0, Magic.size()) != Magic)
        throw std::invalid_argument("mesh artifact has an invalid header");

    const size_t payloadSize = bytes.size() - sizeof(uint64_t);
    Reader checksumReader(bytes.substr(payloadSize));
    const uint64_t storedChecksum = checksumReader.ReadU64();
    if (storedChecksum != Fnv1a64(bytes.substr(0, payloadSize)))
        throw std::invalid_argument("mesh artifact checksum mismatch");

    Reader reader(bytes.substr(Magic.size(), payloadSize - Magic.size()));
    if (reader.ReadU32() != EndianMarker)
        throw std::invalid_argument("mesh artifact has an invalid endian marker");
    const std::string sourceContentHash = reader.ReadString();
    if (sourceContentHash.empty() || sourceContentHash != expectedSourceContentHash)
        throw std::invalid_argument("mesh artifact does not match the imported source content");

    auto mesh = std::make_shared<InxMesh>(reader.ReadString());

    std::vector<Vertex> vertices(reader.ReadCount());
    for (Vertex &vertex : vertices) {
        vertex.pos = ReadVec3(reader);
        vertex.normal = ReadVec3(reader);
        vertex.tangent = ReadVec4(reader);
        vertex.color = ReadVec3(reader);
        vertex.texCoord = ReadVec2(reader);
        for (glm::length_t component = 0; component < vertex.boneIndices.length(); ++component)
            vertex.boneIndices[component] = reader.ReadU32();
        vertex.boneWeights = ReadVec4(reader);
    }

    std::vector<uint32_t> indices(reader.ReadCount());
    for (uint32_t &index : indices) {
        index = reader.ReadU32();
        if (index >= vertices.size())
            throw std::invalid_argument("mesh artifact contains an out-of-range vertex index");
    }

    std::vector<SubMesh> subMeshes(reader.ReadCount());
    for (SubMesh &subMesh : subMeshes) {
        subMesh.indexStart = reader.ReadU32();
        subMesh.indexCount = reader.ReadU32();
        subMesh.vertexStart = reader.ReadU32();
        subMesh.vertexCount = reader.ReadU32();
        subMesh.materialSlot = reader.ReadU32();
        subMesh.nodeGroup = reader.ReadU32();
        subMesh.boundsMin = ReadVec3(reader);
        subMesh.boundsMax = ReadVec3(reader);
        subMesh.name = reader.ReadString();
        if (subMesh.indexStart > indices.size() || subMesh.indexCount > indices.size() - subMesh.indexStart ||
            subMesh.vertexStart > vertices.size() || subMesh.vertexCount > vertices.size() - subMesh.vertexStart)
            throw std::invalid_argument("mesh artifact contains an invalid submesh range");
    }

    std::vector<std::string> slotNames(reader.ReadCount());
    for (auto &name : slotNames)
        name = reader.ReadString();

    std::vector<MaterialSlotData> slotData(reader.ReadCount());
    for (MaterialSlotData &material : slotData) {
        material.baseColor = ReadVec4(reader);
        material.emissionColor = ReadVec4(reader);
        material.metallic = reader.ReadFloat();
        material.smoothness = reader.ReadFloat();
        material.opacity = reader.ReadFloat();
    }

    std::vector<std::string> nodeNames(reader.ReadCount());
    for (auto &name : nodeNames)
        name = reader.ReadString();
    if (!reader.AtEnd())
        throw std::invalid_argument("mesh artifact contains trailing data");

    mesh->SetData(std::move(vertices), std::move(indices), std::move(subMeshes));
    mesh->SetMaterialSlotNames(std::move(slotNames));
    mesh->SetMaterialSlotData(std::move(slotData));
    mesh->SetNodeNames(std::move(nodeNames));
    return mesh;
}

bool MeshArtifact::HasCurrentHeader(std::string_view bytes) noexcept
{
    return bytes.size() >= Magic.size() + sizeof(uint32_t) && bytes.substr(0, Magic.size()) == Magic &&
           static_cast<unsigned char>(bytes[Magic.size()]) == 0x04 &&
           static_cast<unsigned char>(bytes[Magic.size() + 1]) == 0x03 &&
           static_cast<unsigned char>(bytes[Magic.size() + 2]) == 0x02 &&
           static_cast<unsigned char>(bytes[Magic.size() + 3]) == 0x01;
}

} // namespace infernux
