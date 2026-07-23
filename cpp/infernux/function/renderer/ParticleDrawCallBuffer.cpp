#include "ParticleDrawCallBuffer.h"

#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <stdexcept>
#include <type_traits>

namespace infernux
{

static_assert(std::is_standard_layout_v<ParticleInstance>);
static_assert(sizeof(ParticleInstance) == 12 * sizeof(float));
static_assert(offsetof(ParticleInstance, position) == 0);
static_assert(offsetof(ParticleInstance, size) == 3 * sizeof(float));
static_assert(offsetof(ParticleInstance, color) == 4 * sizeof(float));
static_assert(offsetof(ParticleInstance, rotation) == 8 * sizeof(float));
static_assert(offsetof(ParticleInstance, scale) == 9 * sizeof(float));

void ParticleDrawCallBuffer::SetBatch(uint64_t batchId, std::vector<ParticleInstance> instances,
                                      const std::string &materialGuid, uint64_t ownerObjectId)
{
    if (batchId == 0)
        throw std::invalid_argument("particle batch id must be non-zero");

    std::shared_ptr<InxMaterial> material;
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        auto existing = m_batches.find(batchId);
        if (existing != m_batches.end() && existing->second.materialGuid == materialGuid)
            material = existing->second.material;
    }
    if (!material && !materialGuid.empty()) {
        material = AssetRegistry::Instance().GetAsset<InxMaterial>(materialGuid);
        if (!material)
            material = AssetRegistry::Instance().LoadAsset<InxMaterial>(materialGuid, ResourceType::Material);
        if (!material)
            throw std::invalid_argument("particle material GUID could not be resolved");
        if (material->GetVertShaderName() != "particle_billboard")
            throw std::invalid_argument("particle material must use the particle_billboard vertex shader");
    }
    if (!material)
        material = AssetRegistry::Instance().GetBuiltinMaterial("ParticleBillboardMaterial");
    if (!material)
        throw std::runtime_error("built-in particle billboard material is unavailable");

    std::lock_guard<std::mutex> lock(m_mutex);
    m_batches[batchId] = Batch{std::move(instances), std::move(material), materialGuid, ownerObjectId};
}

void ParticleDrawCallBuffer::SetBatchInterleaved(uint64_t batchId, const float *instances, size_t instanceCount,
                                                 const std::string &materialGuid, const glm::vec3 &origin,
                                                 bool validate, uint64_t ownerObjectId)
{
    if (batchId == 0)
        throw std::invalid_argument("particle batch id must be non-zero");
    if (instanceCount > 0 && instances == nullptr)
        throw std::invalid_argument("particle instance data cannot be null");

    constexpr size_t kStride = 12;
    if (validate) {
        for (size_t index = 0; index < instanceCount; ++index) {
            const float *row = instances + index * kStride;
            for (size_t component = 0; component < kStride; ++component) {
                if (!std::isfinite(row[component]))
                    throw std::invalid_argument("particle instances must contain only finite values");
            }
            if (row[3] < 0.0f)
                throw std::invalid_argument("particle instance size must be non-negative");
        }
    }

    std::shared_ptr<InxMaterial> material;
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        auto existing = m_batches.find(batchId);
        if (existing != m_batches.end() && existing->second.materialGuid == materialGuid)
            material = existing->second.material;
    }
    if (!material && !materialGuid.empty()) {
        material = AssetRegistry::Instance().GetAsset<InxMaterial>(materialGuid);
        if (!material)
            material = AssetRegistry::Instance().LoadAsset<InxMaterial>(materialGuid, ResourceType::Material);
        if (!material)
            throw std::invalid_argument("particle material GUID could not be resolved");
        if (material->GetVertShaderName() != "particle_billboard")
            throw std::invalid_argument("particle material must use the particle_billboard vertex shader");
    }
    if (!material)
        material = AssetRegistry::Instance().GetBuiltinMaterial("ParticleBillboardMaterial");
    if (!material)
        throw std::runtime_error("built-in particle billboard material is unavailable");

    std::lock_guard<std::mutex> lock(m_mutex);
    Batch &batch = m_batches[batchId];
    batch.instances.resize(instanceCount);
    batch.material = std::move(material);
    batch.materialGuid = materialGuid;
    batch.ownerObjectId = ownerObjectId;
    if (origin == glm::vec3(0.0f)) {
        if (instanceCount > 0)
            std::memcpy(batch.instances.data(), instances, instanceCount * sizeof(ParticleInstance));
        return;
    }
    for (size_t index = 0; index < instanceCount; ++index) {
        const float *row = instances + index * kStride;
        ParticleInstance &instance = batch.instances[index];
        instance.position = glm::vec3(row[0], row[1], row[2]) + origin;
        instance.size = row[3];
        instance.color = glm::vec4(row[4], row[5], row[6], row[7]);
        instance.rotation = row[8];
        instance.scale = glm::vec3(row[9], row[10], row[11]);
    }
}

void ParticleDrawCallBuffer::RemoveBatch(uint64_t batchId)
{
    std::lock_guard<std::mutex> lock(m_mutex);
    m_batches.erase(batchId);
}

void ParticleDrawCallBuffer::Clear()
{
    std::lock_guard<std::mutex> lock(m_mutex);
    m_batches.clear();
}

DrawCallResult ParticleDrawCallBuffer::GetDrawCalls(const glm::vec3 &cameraRight, const glm::vec3 &cameraUp) const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    DrawCallResult result;
    size_t instanceCount = 0;
    for (const auto &[_, batch] : m_batches)
        instanceCount += batch.instances.size();
    result.drawCalls.reserve(instanceCount);

    glm::vec3 right =
        glm::dot(cameraRight, cameraRight) > 1e-8f ? glm::normalize(cameraRight) : glm::vec3(1.0f, 0.0f, 0.0f);
    glm::vec3 up = glm::dot(cameraUp, cameraUp) > 1e-8f ? glm::normalize(cameraUp) : glm::vec3(0.0f, 1.0f, 0.0f);

    for (const auto &[batchId, batch] : m_batches) {
        if (!batch.material)
            continue;
        for (const ParticleInstance &instance : batch.instances) {
            const float cosine = std::cos(instance.rotation);
            const float sine = std::sin(instance.rotation);
            const glm::vec3 rotatedRight = (right * cosine + up * sine) * (instance.size * instance.scale.x);
            const glm::vec3 rotatedUp = (-right * sine + up * cosine) * (instance.size * instance.scale.y);

            glm::mat4 packed(1.0f);
            packed[0] = glm::vec4(rotatedRight, instance.color.r);
            packed[1] = glm::vec4(rotatedUp, instance.color.g);
            packed[2] = glm::vec4(instance.color.b, instance.color.a, 0.0f, 0.0f);
            packed[3] = glm::vec4(instance.position, 1.0f);

            DrawCall drawCall;
            drawCall.indexCount = static_cast<uint32_t>(QuadIndices().size());
            drawCall.worldMatrix = packed;
            drawCall.material = batch.material;
            drawCall.objectId = 0x5041525400000000ULL | batchId;
            drawCall.pickingObjectId = batch.ownerObjectId;
            drawCall.identity =
                RenderProxyHandle::Synthetic(RenderDomain::Particle, drawCall.objectId).MakeDrawIdentity();
            drawCall.meshVertices = &QuadVertices();
            drawCall.meshIndices = &QuadIndices();
            drawCall.allowTransparentInstancing = true;
            result.drawCalls.push_back(drawCall);
        }
    }
    return result;
}

size_t ParticleDrawCallBuffer::GetParticleCount() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    size_t count = 0;
    for (const auto &[_, batch] : m_batches)
        count += batch.instances.size();
    return count;
}

uint64_t ParticleDrawCallBuffer::GetResidentBytes() const
{
    return static_cast<uint64_t>(GetParticleCount()) * sizeof(glm::mat4);
}

const std::vector<Vertex> &ParticleDrawCallBuffer::QuadVertices()
{
    static const std::vector<Vertex> vertices = {
        Vertex::CreateFull({-0.5f, -0.5f, 0.0f}, {0.0f, 0.0f, 1.0f}, {1.0f, 0.0f, 0.0f, 1.0f}, {1.0f, 1.0f, 1.0f},
                           {0.0f, 1.0f}),
        Vertex::CreateFull({-0.5f, 0.5f, 0.0f}, {0.0f, 0.0f, 1.0f}, {1.0f, 0.0f, 0.0f, 1.0f}, {1.0f, 1.0f, 1.0f},
                           {0.0f, 0.0f}),
        Vertex::CreateFull({0.5f, 0.5f, 0.0f}, {0.0f, 0.0f, 1.0f}, {1.0f, 0.0f, 0.0f, 1.0f}, {1.0f, 1.0f, 1.0f},
                           {1.0f, 0.0f}),
        Vertex::CreateFull({0.5f, -0.5f, 0.0f}, {0.0f, 0.0f, 1.0f}, {1.0f, 0.0f, 0.0f, 1.0f}, {1.0f, 1.0f, 1.0f},
                           {1.0f, 1.0f}),
    };
    return vertices;
}

const std::vector<uint32_t> &ParticleDrawCallBuffer::QuadIndices()
{
    static const std::vector<uint32_t> indices = {0, 1, 2, 0, 2, 3};
    return indices;
}

} // namespace infernux
