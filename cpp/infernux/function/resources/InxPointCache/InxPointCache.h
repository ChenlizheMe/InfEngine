#pragma once

#include "PointCacheArtifact.h"

#include <cstddef>
#include <memory>
#include <string>
#include <utility>

namespace infernux
{

class InxPointCache final
{
  public:
    [[nodiscard]] const std::string &GetGuid() const noexcept
    {
        return m_guid;
    }
    [[nodiscard]] const std::string &GetFilePath() const noexcept
    {
        return m_filePath;
    }
    [[nodiscard]] const std::string &GetName() const noexcept
    {
        return m_name;
    }
    [[nodiscard]] const std::shared_ptr<const PointCacheCpuData> &GetCpuData() const noexcept
    {
        return m_cpuData;
    }
    [[nodiscard]] uint64_t GetGeneration() const noexcept
    {
        return m_generation;
    }

    void SetGuid(std::string value)
    {
        m_guid = std::move(value);
    }
    void SetFilePath(std::string value)
    {
        m_filePath = std::move(value);
    }
    void SetName(std::string value)
    {
        m_name = std::move(value);
    }
    void SetCpuData(std::shared_ptr<const PointCacheCpuData> value)
    {
        m_cpuData = std::move(value);
        ++m_generation;
    }

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept;

  private:
    std::string m_guid;
    std::string m_filePath;
    std::string m_name;
    std::shared_ptr<const PointCacheCpuData> m_cpuData;
    uint64_t m_generation = 0;
};

} // namespace infernux
