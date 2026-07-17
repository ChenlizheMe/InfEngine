#include "ParticleDrawCallBuffer.h"

#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace infernux
{

void ParticleDrawCallBuffer::SetBatch(uint64_t batchId, std::vector<ParticleInstance> instances,
                                      const std::string &materialGuid)
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
    m_batches[batchId] = Batch{std::move(instances), std::move(material), materialGuid};
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
            const glm::vec3 rotatedRight = (right * cosine + up * sine) * instance.size;
            const glm::vec3 rotatedUp = (-right * sine + up * cosine) * instance.size;

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
