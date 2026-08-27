#pragma once

#include "MeshRenderer.h"
#include <cstddef>
#include <glm/glm.hpp>
#include <vector>

namespace infernux
{

enum class LineAlignment : uint8_t
{
    View = 0,
    TransformZ = 1,
};

enum class LineTextureMode : uint8_t
{
    Stretch = 0,
    Tile = 1,
};

/**
 * @brief Camera-facing procedural ribbon built from an authored point list.
 *
 * Positions may be local to the owning GameObject or already expressed in
 * world space. Width, colour, UVs, and orientation metadata are baked into a
 * compact two-vertex-per-point stream; the final ribbon offset is evaluated
 * per camera in the standard vertex pipeline.
 */
class LineRenderer final : public MeshRenderer
{
  public:
    [[nodiscard]] static ComponentTypeConstraints GetTypeConstraints()
    {
        return MeshRenderer::GetTypeConstraints();
    }

    LineRenderer();

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "LineRenderer";
    }

    [[nodiscard]] size_t GetPositionCount() const
    {
        return m_positions.size();
    }
    void SetPositionCount(size_t count);
    [[nodiscard]] glm::vec3 GetPosition(size_t index) const;
    void SetPosition(size_t index, const glm::vec3 &position);
    [[nodiscard]] const std::vector<glm::vec3> &GetPositions() const
    {
        return m_positions;
    }
    void SetPositions(const std::vector<glm::vec3> &positions);

    [[nodiscard]] float GetStartWidth() const
    {
        return m_startWidth;
    }
    void SetStartWidth(float width);
    [[nodiscard]] float GetEndWidth() const
    {
        return m_endWidth;
    }
    void SetEndWidth(float width);
    [[nodiscard]] float GetWidthMultiplier() const
    {
        return m_widthMultiplier;
    }
    void SetWidthMultiplier(float multiplier);

    [[nodiscard]] const glm::vec4 &GetStartColor() const
    {
        return m_startColor;
    }
    void SetStartColor(const glm::vec4 &color);
    [[nodiscard]] const glm::vec4 &GetEndColor() const
    {
        return m_endColor;
    }
    void SetEndColor(const glm::vec4 &color);

    [[nodiscard]] bool GetLoop() const
    {
        return m_loop;
    }
    void SetLoop(bool loop);
    [[nodiscard]] bool GetUseWorldSpace() const
    {
        return m_useWorldSpace;
    }
    void SetUseWorldSpace(bool useWorldSpace);
    [[nodiscard]] LineAlignment GetAlignment() const
    {
        return m_alignment;
    }
    void SetAlignment(LineAlignment alignment);
    [[nodiscard]] LineTextureMode GetTextureMode() const
    {
        return m_textureMode;
    }
    void SetTextureMode(LineTextureMode mode);
    [[nodiscard]] float GetTextureScale() const
    {
        return m_textureScale;
    }
    void SetTextureScale(float scale);

    void Simplify(float tolerance);

    [[nodiscard]] glm::mat4 ResolveRenderWorldMatrix(const glm::mat4 &objectWorldMatrix) const override;
    void ComputeWorldBounds(const glm::mat4 &worldMatrix, glm::vec3 &outMin, glm::vec3 &outMax) const override;

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

  private:
    [[nodiscard]] bool ShouldSerializeInlineMeshData() const override
    {
        return false;
    }
    void RebuildMesh();

    std::vector<glm::vec3> m_positions{{0.0f, 0.0f, 0.0f}, {1.0f, 0.0f, 0.0f}};
    float m_startWidth = 0.1f;
    float m_endWidth = 0.1f;
    float m_widthMultiplier = 1.0f;
    glm::vec4 m_startColor{1.0f};
    glm::vec4 m_endColor{1.0f};
    bool m_loop = false;
    bool m_useWorldSpace = false;
    LineAlignment m_alignment = LineAlignment::View;
    LineTextureMode m_textureMode = LineTextureMode::Stretch;
    float m_textureScale = 1.0f;
};

} // namespace infernux
