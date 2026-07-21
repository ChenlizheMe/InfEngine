#pragma once

#include "InxRenderStruct.h"

#include <glm/glm.hpp>

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{

class InxMaterial;

struct ParticleInstance
{
    glm::vec3 position{0.0f};
    float size = 1.0f;
    glm::vec4 color{1.0f};
    float rotation = 0.0f;
};

class ParticleDrawCallBuffer
{
  public:
    void SetBatch(uint64_t batchId, std::vector<ParticleInstance> instances, const std::string &materialGuid,
                  uint64_t ownerObjectId = 0);
    void SetBatchInterleaved(uint64_t batchId, const float *instances, size_t instanceCount,
                             const std::string &materialGuid, const glm::vec3 &origin = glm::vec3(0.0f),
                             bool validate = true, uint64_t ownerObjectId = 0);
    void RemoveBatch(uint64_t batchId);
    void Clear();

    [[nodiscard]] DrawCallResult GetDrawCalls(const glm::vec3 &cameraRight, const glm::vec3 &cameraUp) const;
    [[nodiscard]] size_t GetParticleCount() const;
    [[nodiscard]] uint64_t GetResidentBytes() const;

  private:
    struct Batch
    {
        std::vector<ParticleInstance> instances;
        std::shared_ptr<InxMaterial> material;
        std::string materialGuid;
        uint64_t ownerObjectId = 0;
    };

    static const std::vector<Vertex> &QuadVertices();
    static const std::vector<uint32_t> &QuadIndices();

    mutable std::mutex m_mutex;
    std::unordered_map<uint64_t, Batch> m_batches;
};

} // namespace infernux
