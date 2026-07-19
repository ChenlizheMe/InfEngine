#include "SkinnedMeshArtifact.h"

#include "InxSkinnedMesh.h"

#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <unordered_set>

namespace infernux
{
namespace
{
constexpr std::string_view Magic = "INXSKINART";
constexpr uint32_t EndianMarker = 0x01020304U;
constexpr uint32_t MaximumVertices = 10'000'000U;
constexpr uint32_t MaximumIndices = 30'000'000U;
constexpr uint32_t MaximumObjects = 1'000'000U;
constexpr uint32_t MaximumKeys = 100'000'000U;
constexpr uint32_t MaximumStringBytes = 1024U * 1024U;
constexpr uint32_t MaximumHashBytes = 1024U;
constexpr uint64_t MaximumArtifactBytes = 2ULL * 1024ULL * 1024ULL * 1024ULL;

uint64_t Fnv1a64(std::string_view bytes)
{
    uint64_t hash = 14695981039346656037ULL;
    for (const unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

void AppendU32(std::string &output, uint32_t value)
{
    for (unsigned int shift = 0; shift < 32; shift += 8)
        output.push_back(static_cast<char>((value >> shift) & 0xffU));
}

void AppendU64(std::string &output, uint64_t value)
{
    for (unsigned int shift = 0; shift < 64; shift += 8)
        output.push_back(static_cast<char>((value >> shift) & 0xffU));
}

void AppendI32(std::string &output, int32_t value)
{
    uint32_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    AppendU32(output, bits);
}

void AppendFloat(std::string &output, float value)
{
    if (!std::isfinite(value))
        throw std::invalid_argument("skinned Mesh artifact contains a non-finite float");
    uint32_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    AppendU32(output, bits);
}

void AppendDouble(std::string &output, double value)
{
    if (!std::isfinite(value))
        throw std::invalid_argument("skinned Mesh artifact contains a non-finite double");
    uint64_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    AppendU64(output, bits);
}

void AppendString(std::string &output, std::string_view value, bool allowEmpty = true)
{
    if ((!allowEmpty && value.empty()) || value.size() > MaximumStringBytes)
        throw std::invalid_argument("skinned Mesh artifact contains an invalid string");
    AppendU32(output, static_cast<uint32_t>(value.size()));
    output.append(value);
}

void AppendCount(std::string &output, size_t count, uint32_t maximum)
{
    if (count > maximum)
        throw std::overflow_error("skinned Mesh artifact count exceeds its format limit");
    AppendU32(output, static_cast<uint32_t>(count));
}

void AppendVec2(std::string &output, const glm::vec2 &value)
{
    AppendFloat(output, value.x);
    AppendFloat(output, value.y);
}

void AppendVec3(std::string &output, const glm::vec3 &value)
{
    AppendFloat(output, value.x);
    AppendFloat(output, value.y);
    AppendFloat(output, value.z);
}

void AppendVec4(std::string &output, const glm::vec4 &value)
{
    AppendFloat(output, value.x);
    AppendFloat(output, value.y);
    AppendFloat(output, value.z);
    AppendFloat(output, value.w);
}

void AppendQuat(std::string &output, const glm::quat &value)
{
    AppendFloat(output, value.w);
    AppendFloat(output, value.x);
    AppendFloat(output, value.y);
    AppendFloat(output, value.z);
}

void AppendMat4(std::string &output, const glm::mat4 &value)
{
    for (glm::length_t column = 0; column < value.length(); ++column)
        AppendVec4(output, value[column]);
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
        for (unsigned int shift = 0; shift < 32; shift += 8)
            value |= static_cast<uint32_t>(static_cast<unsigned char>(m_bytes[m_cursor++])) << shift;
        return value;
    }

    [[nodiscard]] uint64_t ReadU64()
    {
        Require(sizeof(uint64_t));
        uint64_t value = 0;
        for (unsigned int shift = 0; shift < 64; shift += 8)
            value |= static_cast<uint64_t>(static_cast<unsigned char>(m_bytes[m_cursor++])) << shift;
        return value;
    }

    [[nodiscard]] int32_t ReadI32()
    {
        const uint32_t bits = ReadU32();
        int32_t value = 0;
        static_assert(sizeof(bits) == sizeof(value));
        std::memcpy(&value, &bits, sizeof(value));
        return value;
    }

    [[nodiscard]] float ReadFloat()
    {
        const uint32_t bits = ReadU32();
        float value = 0.0f;
        static_assert(sizeof(bits) == sizeof(value));
        std::memcpy(&value, &bits, sizeof(value));
        if (!std::isfinite(value))
            throw std::invalid_argument("skinned Mesh artifact contains a non-finite float");
        return value;
    }

    [[nodiscard]] double ReadDouble()
    {
        const uint64_t bits = ReadU64();
        double value = 0.0;
        static_assert(sizeof(bits) == sizeof(value));
        std::memcpy(&value, &bits, sizeof(value));
        if (!std::isfinite(value))
            throw std::invalid_argument("skinned Mesh artifact contains a non-finite double");
        return value;
    }

    [[nodiscard]] std::string ReadString(bool allowEmpty = true)
    {
        const uint32_t size = ReadU32();
        if ((!allowEmpty && size == 0) || size > MaximumStringBytes)
            throw std::invalid_argument("skinned Mesh artifact contains an invalid string");
        Require(size);
        std::string value(m_bytes.substr(m_cursor, size));
        m_cursor += size;
        return value;
    }

    [[nodiscard]] uint32_t ReadCount(uint32_t maximum)
    {
        const uint32_t count = ReadU32();
        if (count > maximum)
            throw std::invalid_argument("skinned Mesh artifact count exceeds its format limit");
        return count;
    }

    [[nodiscard]] bool AtEnd() const noexcept
    {
        return m_cursor == m_bytes.size();
    }

  private:
    void Require(size_t size) const
    {
        if (m_cursor > m_bytes.size() || size > m_bytes.size() - m_cursor)
            throw std::invalid_argument("skinned Mesh artifact is truncated");
    }

    std::string_view m_bytes;
    size_t m_cursor = 0;
};

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

glm::quat ReadQuat(Reader &reader)
{
    const float w = reader.ReadFloat();
    const float x = reader.ReadFloat();
    const float y = reader.ReadFloat();
    const float z = reader.ReadFloat();
    const glm::quat value(w, x, y, z);
    const float length = glm::length(value);
    if (!std::isfinite(length) || length <= std::numeric_limits<float>::epsilon())
        throw std::invalid_argument("skinned Mesh artifact contains an invalid quaternion");
    return glm::normalize(value);
}

glm::mat4 ReadMat4(Reader &reader)
{
    glm::mat4 value(1.0f);
    for (glm::length_t column = 0; column < value.length(); ++column)
        value[column] = ReadVec4(reader);
    return value;
}

void AppendVertex(std::string &output, const Vertex &vertex)
{
    AppendVec3(output, vertex.pos);
    AppendVec3(output, vertex.normal);
    AppendVec4(output, vertex.tangent);
    AppendVec3(output, vertex.color);
    AppendVec2(output, vertex.texCoord);
    for (glm::length_t component = 0; component < vertex.boneIndices.length(); ++component)
        AppendU32(output, vertex.boneIndices[component]);
    AppendVec4(output, vertex.boneWeights);
}

Vertex ReadVertex(Reader &reader)
{
    Vertex vertex;
    vertex.pos = ReadVec3(reader);
    vertex.normal = ReadVec3(reader);
    vertex.tangent = ReadVec4(reader);
    vertex.color = ReadVec3(reader);
    vertex.texCoord = ReadVec2(reader);
    for (glm::length_t component = 0; component < vertex.boneIndices.length(); ++component)
        vertex.boneIndices[component] = reader.ReadU32();
    vertex.boneWeights = ReadVec4(reader);
    return vertex;
}

template <typename Value, typename AppendValue>
void AppendKeys(std::string &output, const std::vector<std::pair<double, Value>> &keys, AppendValue appendValue)
{
    AppendCount(output, keys.size(), MaximumKeys);
    double previousTime = -std::numeric_limits<double>::infinity();
    for (const auto &[time, value] : keys) {
        if (time < previousTime)
            throw std::invalid_argument("skinned Mesh animation keys are not ordered by time");
        AppendDouble(output, time);
        appendValue(output, value);
        previousTime = time;
    }
}

template <typename Value, typename ReadValue>
std::vector<std::pair<double, Value>> ReadKeys(Reader &reader, ReadValue readValue)
{
    std::vector<std::pair<double, Value>> keys;
    const uint32_t count = reader.ReadCount(MaximumKeys);
    keys.reserve(count);
    double previousTime = -std::numeric_limits<double>::infinity();
    for (uint32_t index = 0; index < count; ++index) {
        const double time = reader.ReadDouble();
        if (time < previousTime)
            throw std::invalid_argument("skinned Mesh animation keys are not ordered by time");
        keys.push_back({time, readValue(reader)});
        previousTime = time;
    }
    return keys;
}
} // namespace

std::string SkinnedMeshArtifact::Serialize(const InxSkinnedMesh &mesh, std::string_view sourceContentHash)
{
    if (!mesh.IsValid() || mesh.influences.size() != mesh.baseVertices.size())
        throw std::invalid_argument("cannot serialize invalid skinned Mesh data");
    if (sourceContentHash.empty() || sourceContentHash.size() > MaximumHashBytes)
        throw std::invalid_argument("skinned Mesh artifact requires a bounded source content hash");

    std::string bytes(Magic);
    AppendU32(bytes, EndianMarker);
    AppendString(bytes, sourceContentHash, false);
    AppendU32(bytes, 1);
    AppendFloat(bytes, mesh.scaleFactor);

    AppendCount(bytes, mesh.baseVertices.size(), MaximumVertices);
    for (const Vertex &vertex : mesh.baseVertices)
        AppendVertex(bytes, vertex);

    AppendCount(bytes, mesh.indices.size(), MaximumIndices);
    for (const uint32_t index : mesh.indices) {
        if (index >= mesh.baseVertices.size())
            throw std::invalid_argument("skinned Mesh contains an out-of-range index");
        AppendU32(bytes, index);
    }

    AppendCount(bytes, mesh.subMeshes.size(), MaximumObjects);
    for (const SubMesh &subMesh : mesh.subMeshes) {
        if (subMesh.indexStart > mesh.indices.size() || subMesh.indexCount > mesh.indices.size() - subMesh.indexStart ||
            subMesh.vertexStart > mesh.baseVertices.size() ||
            subMesh.vertexCount > mesh.baseVertices.size() - subMesh.vertexStart)
            throw std::invalid_argument("skinned Mesh contains an invalid submesh range");
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

    AppendCount(bytes, mesh.nodes.size(), MaximumObjects);
    std::unordered_set<std::string> nodeNames;
    for (size_t index = 0; index < mesh.nodes.size(); ++index) {
        const auto &node = mesh.nodes[index];
        if (node.name.empty() || !nodeNames.insert(node.name).second || node.parent < -1 ||
            (node.parent >= 0 && static_cast<size_t>(node.parent) >= index))
            throw std::invalid_argument("skinned Mesh contains an invalid node hierarchy");
        AppendString(bytes, node.name, false);
        AppendI32(bytes, node.parent);
        AppendMat4(bytes, node.bindLocal);
    }

    AppendCount(bytes, mesh.bones.size(), MaximumObjects);
    std::unordered_set<std::string> boneNames;
    for (const auto &bone : mesh.bones) {
        if (bone.name.empty() || !boneNames.insert(bone.name).second || bone.nodeIndex < -1 ||
            (bone.nodeIndex >= 0 && static_cast<size_t>(bone.nodeIndex) >= mesh.nodes.size()))
            throw std::invalid_argument("skinned Mesh contains an invalid bone");
        AppendString(bytes, bone.name, false);
        AppendI32(bytes, bone.nodeIndex);
        AppendMat4(bytes, bone.inverseBind);
    }
    for (size_t vertexIndex = 0; vertexIndex < mesh.baseVertices.size(); ++vertexIndex) {
        const Vertex &vertex = mesh.baseVertices[vertexIndex];
        const SkinInfluence &influence = mesh.influences[vertexIndex];
        for (glm::length_t component = 0; component < vertex.boneWeights.length(); ++component) {
            const float weight = vertex.boneWeights[component];
            if (weight < 0.0f || (weight > 0.0f && vertex.boneIndices[component] >= mesh.bones.size()))
                throw std::invalid_argument("skinned Mesh contains an invalid bone influence");
            if (vertex.boneIndices[component] != influence.boneIndex[component] ||
                std::abs(weight - influence.weight[component]) > 1e-6f)
                throw std::invalid_argument("skinned Mesh vertex and influence streams disagree");
        }
    }

    AppendCount(bytes, mesh.animations.size(), MaximumObjects);
    std::unordered_set<std::string> animationNames;
    for (const auto &animation : mesh.animations) {
        if (animation.name.empty() || !animationNames.insert(animation.name).second || animation.durationTicks < 0.0 ||
            animation.ticksPerSecond <= 0.0)
            throw std::invalid_argument("skinned Mesh contains an invalid animation");
        AppendString(bytes, animation.name, false);
        AppendDouble(bytes, animation.durationTicks);
        AppendDouble(bytes, animation.ticksPerSecond);
        AppendCount(bytes, animation.tracks.size(), MaximumObjects);
        std::unordered_set<std::string> trackNames;
        for (const auto &track : animation.tracks) {
            if (track.nodeName.empty() || mesh.nodeByName.find(track.nodeName) == mesh.nodeByName.end() ||
                !trackNames.insert(track.nodeName).second)
                throw std::invalid_argument("skinned Mesh contains an invalid animation track");
            AppendString(bytes, track.nodeName, false);
            AppendKeys(bytes, track.positions,
                       [](std::string &output, const glm::vec3 &value) { AppendVec3(output, value); });
            AppendKeys(bytes, track.rotations,
                       [](std::string &output, const glm::quat &value) { AppendQuat(output, value); });
            AppendKeys(bytes, track.scales,
                       [](std::string &output, const glm::vec3 &value) { AppendVec3(output, value); });
        }
    }
    if (bytes.size() > MaximumArtifactBytes - sizeof(uint64_t))
        throw std::overflow_error("skinned Mesh artifact exceeds its size limit");
    AppendU64(bytes, Fnv1a64(bytes));
    return bytes;
}

std::string SkinnedMeshArtifact::SerializeEmpty(std::string_view sourceContentHash)
{
    if (sourceContentHash.empty() || sourceContentHash.size() > MaximumHashBytes)
        throw std::invalid_argument("skinned Mesh artifact requires a bounded source content hash");
    std::string bytes(Magic);
    AppendU32(bytes, EndianMarker);
    AppendString(bytes, sourceContentHash, false);
    AppendU32(bytes, 0);
    AppendU64(bytes, Fnv1a64(bytes));
    return bytes;
}

std::shared_ptr<InxSkinnedMesh> SkinnedMeshArtifact::Deserialize(std::string_view bytes,
                                                                 std::string_view expectedSourceContentHash)
{
    if (expectedSourceContentHash.empty() || expectedSourceContentHash.size() > MaximumHashBytes)
        throw std::invalid_argument("skinned Mesh artifact requires an expected source content hash");
    if (bytes.size() > MaximumArtifactBytes || bytes.size() < Magic.size() + sizeof(uint32_t) + sizeof(uint64_t) ||
        bytes.substr(0, Magic.size()) != Magic)
        throw std::invalid_argument("skinned Mesh artifact has an invalid header");
    const size_t checksumOffset = bytes.size() - sizeof(uint64_t);
    Reader checksum(bytes.substr(checksumOffset));
    if (checksum.ReadU64() != Fnv1a64(bytes.substr(0, checksumOffset)))
        throw std::invalid_argument("skinned Mesh artifact checksum mismatch");

    Reader reader(bytes.substr(Magic.size(), checksumOffset - Magic.size()));
    if (reader.ReadU32() != EndianMarker)
        throw std::invalid_argument("skinned Mesh artifact has an invalid endian marker");
    const std::string sourceHash = reader.ReadString(false);
    if (sourceHash != expectedSourceContentHash)
        throw std::invalid_argument("skinned Mesh artifact does not match the imported source content");
    const uint32_t hasPayload = reader.ReadU32();
    if (hasPayload == 0) {
        if (!reader.AtEnd())
            throw std::invalid_argument("empty skinned Mesh artifact contains trailing data");
        return {};
    }
    if (hasPayload != 1)
        throw std::invalid_argument("skinned Mesh artifact has an invalid payload marker");

    auto mesh = std::make_shared<InxSkinnedMesh>();
    mesh->scaleFactor = reader.ReadFloat();
    if (mesh->scaleFactor <= 0.0f)
        throw std::invalid_argument("skinned Mesh artifact has an invalid scale factor");

    const uint32_t vertexCount = reader.ReadCount(MaximumVertices);
    mesh->baseVertices.reserve(vertexCount);
    mesh->influences.reserve(vertexCount);
    for (uint32_t index = 0; index < vertexCount; ++index) {
        Vertex vertex = ReadVertex(reader);
        SkinInfluence influence;
        for (uint32_t component = 0; component < kMaxSkinInfluences; ++component) {
            influence.boneIndex[component] = vertex.boneIndices[component];
            influence.weight[component] = vertex.boneWeights[component];
        }
        mesh->baseVertices.push_back(vertex);
        mesh->influences.push_back(influence);
    }

    const uint32_t indexCount = reader.ReadCount(MaximumIndices);
    mesh->indices.reserve(indexCount);
    for (uint32_t index = 0; index < indexCount; ++index) {
        const uint32_t vertexIndex = reader.ReadU32();
        if (vertexIndex >= mesh->baseVertices.size())
            throw std::invalid_argument("skinned Mesh artifact contains an out-of-range index");
        mesh->indices.push_back(vertexIndex);
    }

    mesh->subMeshes.resize(reader.ReadCount(MaximumObjects));
    for (SubMesh &subMesh : mesh->subMeshes) {
        subMesh.indexStart = reader.ReadU32();
        subMesh.indexCount = reader.ReadU32();
        subMesh.vertexStart = reader.ReadU32();
        subMesh.vertexCount = reader.ReadU32();
        subMesh.materialSlot = reader.ReadU32();
        subMesh.nodeGroup = reader.ReadU32();
        subMesh.boundsMin = ReadVec3(reader);
        subMesh.boundsMax = ReadVec3(reader);
        subMesh.name = reader.ReadString();
        if (subMesh.indexStart > mesh->indices.size() ||
            subMesh.indexCount > mesh->indices.size() - subMesh.indexStart ||
            subMesh.vertexStart > mesh->baseVertices.size() ||
            subMesh.vertexCount > mesh->baseVertices.size() - subMesh.vertexStart)
            throw std::invalid_argument("skinned Mesh artifact contains an invalid submesh range");
    }

    const uint32_t nodeCount = reader.ReadCount(MaximumObjects);
    mesh->nodes.reserve(nodeCount);
    for (uint32_t index = 0; index < nodeCount; ++index) {
        SkinnedRuntimeNode node;
        node.name = reader.ReadString(false);
        node.parent = reader.ReadI32();
        node.bindLocal = ReadMat4(reader);
        if (node.parent < -1 || (node.parent >= 0 && static_cast<size_t>(node.parent) >= index) ||
            !mesh->nodeByName.emplace(node.name, static_cast<int>(index)).second)
            throw std::invalid_argument("skinned Mesh artifact contains an invalid node hierarchy");
        node.bindGlobal = node.parent >= 0 ? mesh->nodes[static_cast<size_t>(node.parent)].bindGlobal * node.bindLocal
                                           : node.bindLocal;
        mesh->nodes.push_back(std::move(node));
    }

    const uint32_t boneCount = reader.ReadCount(MaximumObjects);
    mesh->bones.reserve(boneCount);
    for (uint32_t index = 0; index < boneCount; ++index) {
        SkinnedRuntimeBone bone;
        bone.name = reader.ReadString(false);
        bone.nodeIndex = reader.ReadI32();
        bone.inverseBind = ReadMat4(reader);
        if (bone.nodeIndex < -1 || (bone.nodeIndex >= 0 && static_cast<size_t>(bone.nodeIndex) >= mesh->nodes.size()) ||
            !mesh->boneByName.emplace(bone.name, static_cast<uint32_t>(index)).second)
            throw std::invalid_argument("skinned Mesh artifact contains an invalid bone");
        mesh->bones.push_back(std::move(bone));
    }
    for (const Vertex &vertex : mesh->baseVertices) {
        for (glm::length_t component = 0; component < vertex.boneWeights.length(); ++component) {
            if (vertex.boneWeights[component] < 0.0f ||
                (vertex.boneWeights[component] > 0.0f && vertex.boneIndices[component] >= mesh->bones.size()))
                throw std::invalid_argument("skinned Mesh artifact contains an invalid bone influence");
        }
    }

    const uint32_t animationCount = reader.ReadCount(MaximumObjects);
    mesh->animations.reserve(animationCount);
    std::unordered_set<std::string> animationNames;
    for (uint32_t animationIndex = 0; animationIndex < animationCount; ++animationIndex) {
        SkinnedRuntimeAnimation animation;
        animation.name = reader.ReadString(false);
        animation.durationTicks = reader.ReadDouble();
        animation.ticksPerSecond = reader.ReadDouble();
        if (!animationNames.insert(animation.name).second || animation.durationTicks < 0.0 ||
            animation.ticksPerSecond <= 0.0)
            throw std::invalid_argument("skinned Mesh artifact contains an invalid animation timing");
        const uint32_t trackCount = reader.ReadCount(MaximumObjects);
        animation.tracks.reserve(trackCount);
        for (uint32_t trackIndex = 0; trackIndex < trackCount; ++trackIndex) {
            SkinnedRuntimeTrack track;
            track.nodeName = reader.ReadString(false);
            if (mesh->nodeByName.find(track.nodeName) == mesh->nodeByName.end() ||
                !animation.trackByNode.emplace(track.nodeName, trackIndex).second)
                throw std::invalid_argument("skinned Mesh artifact contains an invalid animation track");
            track.positions = ReadKeys<glm::vec3>(reader, [](Reader &input) { return ReadVec3(input); });
            track.rotations = ReadKeys<glm::quat>(reader, [](Reader &input) { return ReadQuat(input); });
            track.scales = ReadKeys<glm::vec3>(reader, [](Reader &input) { return ReadVec3(input); });
            animation.tracks.push_back(std::move(track));
        }
        mesh->animations.push_back(std::move(animation));
    }
    if (!reader.AtEnd())
        throw std::invalid_argument("skinned Mesh artifact contains trailing data");
    if (!mesh->IsValid())
        throw std::invalid_argument("skinned Mesh artifact contains no renderable geometry");
    mesh->NormalizeInfluences();
    return mesh;
}

bool SkinnedMeshArtifact::HasCurrentHeader(std::string_view bytes) noexcept
{
    return bytes.size() >= Magic.size() + sizeof(uint32_t) && bytes.substr(0, Magic.size()) == Magic &&
           static_cast<unsigned char>(bytes[Magic.size()]) == 0x04 &&
           static_cast<unsigned char>(bytes[Magic.size() + 1]) == 0x03 &&
           static_cast<unsigned char>(bytes[Magic.size() + 2]) == 0x02 &&
           static_cast<unsigned char>(bytes[Magic.size() + 3]) == 0x01;
}

} // namespace infernux
