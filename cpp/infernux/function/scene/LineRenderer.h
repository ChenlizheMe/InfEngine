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
    DistributePerSegment = 2,
    RepeatPerSegment = 3,
    Static = 4,
};

enum class LineCurveWrapMode : uint8_t
{
    Clamp = 0,
    Repeat = 1,
    PingPong = 2,
};

enum class LineGradientMode : uint8_t
{
    Linear = 0,
    Fixed = 1,
    PerceptualBlend = 2,
};

struct LineWidthKey
{
    float time = 0.0f;
    float value = 1.0f;
    float inTangent = 0.0f;
    float outTangent = 0.0f;
};

struct LineColorKey
{
    float time = 0.0f;
    glm::vec4 color{1.0f};
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

    [[nodiscard]] float GetStartWidth() const;
    void SetStartWidth(float width);
    [[nodiscard]] float GetEndWidth() const;
    void SetEndWidth(float width);
    [[nodiscard]] float GetWidthMultiplier() const
    {
        return m_widthMultiplier;
    }
    void SetWidthMultiplier(float multiplier);
    [[nodiscard]] const std::vector<LineWidthKey> &GetWidthCurve() const
    {
        return m_widthCurve;
    }
    void SetWidthCurve(const std::vector<LineWidthKey> &keys);
    [[nodiscard]] LineCurveWrapMode GetWidthCurvePreWrap() const
    {
        return m_widthCurvePreWrap;
    }
    void SetWidthCurvePreWrap(LineCurveWrapMode mode);
    [[nodiscard]] LineCurveWrapMode GetWidthCurvePostWrap() const
    {
        return m_widthCurvePostWrap;
    }
    void SetWidthCurvePostWrap(LineCurveWrapMode mode);

    [[nodiscard]] glm::vec4 GetStartColor() const;
    void SetStartColor(const glm::vec4 &color);
    [[nodiscard]] glm::vec4 GetEndColor() const;
    void SetEndColor(const glm::vec4 &color);
    [[nodiscard]] const std::vector<LineColorKey> &GetColorGradient() const
    {
        return m_colorGradient;
    }
    void SetColorGradient(const std::vector<LineColorKey> &keys);
    [[nodiscard]] LineGradientMode GetColorGradientMode() const
    {
        return m_colorGradientMode;
    }
    void SetColorGradientMode(LineGradientMode mode);

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
    [[nodiscard]] const glm::vec2 &GetTextureScale() const
    {
        return m_textureScale;
    }
    void SetTextureScale(const glm::vec2 &scale);
    [[nodiscard]] uint32_t GetNumCornerVertices() const
    {
        return m_numCornerVertices;
    }
    void SetNumCornerVertices(uint32_t count);
    [[nodiscard]] uint32_t GetNumCapVertices() const
    {
        return m_numCapVertices;
    }
    void SetNumCapVertices(uint32_t count);
    [[nodiscard]] float GetShadowBias() const
    {
        return m_shadowBias;
    }
    void SetShadowBias(float bias);
    [[nodiscard]] bool GetGenerateLightingData() const
    {
        return m_generateLightingData;
    }
    void SetGenerateLightingData(bool generate);

    /// Snapshot the expanded ribbon into another renderer's inline mesh.
    void BakeMesh(MeshRenderer &target, const glm::vec3 &cameraPosition, bool useTransform) const;

    void Simplify(float tolerance);

    [[nodiscard]] std::shared_ptr<InxMaterial> GetEffectiveMaterial(uint32_t slot = 0) const override;
    [[nodiscard]] glm::mat4 ResolveRenderWorldMatrix(const glm::mat4 &objectWorldMatrix) const override;
    void RefreshProceduralGeometry(const glm::mat4 &objectWorldMatrix) override;
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
    void UpdateMaximumWidth();
    void RebuildMesh();

    std::vector<glm::vec3> m_positions{{0.0f, 0.0f, 0.0f}, {1.0f, 0.0f, 0.0f}};
    float m_widthMultiplier = 1.0f;
    std::vector<LineWidthKey> m_widthCurve{{0.0f, 0.1f, 0.0f, 0.0f}, {1.0f, 0.1f, 0.0f, 0.0f}};
    LineCurveWrapMode m_widthCurvePreWrap = LineCurveWrapMode::Clamp;
    LineCurveWrapMode m_widthCurvePostWrap = LineCurveWrapMode::Clamp;
    std::vector<LineColorKey> m_colorGradient{{0.0f, glm::vec4(1.0f)}, {1.0f, glm::vec4(1.0f)}};
    LineGradientMode m_colorGradientMode = LineGradientMode::Linear;
    bool m_loop = false;
    // World space by default (Unity LineRenderer parity). The dominant runtime
    // use case feeds world positions (trails); with a local-space default those
    // points get re-transformed by the owner's world matrix, so a moving owner
    // draws the ribbon displaced and its culling AABB drifts out of the
    // frustum — the line "disappears" or pops per frame. Serialized documents
    // always carry an explicit useWorldSpace value, so existing scenes keep
    // their authored behavior.
    bool m_useWorldSpace = true;
    LineAlignment m_alignment = LineAlignment::View;
    LineTextureMode m_textureMode = LineTextureMode::Stretch;
    glm::vec2 m_textureScale{1.0f};
    uint32_t m_numCornerVertices = 0;
    uint32_t m_numCapVertices = 0;
    float m_shadowBias = 0.5f;
    bool m_generateLightingData = false;
    float m_maximumWidth = 0.1f;
    glm::mat3 m_geometryMetric{1.0f};
};

} // namespace infernux
