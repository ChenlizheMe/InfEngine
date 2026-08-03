#pragma once

#include "MeshRenderer.h"
#include <glm/glm.hpp>
#include <string>

namespace infernux
{

/**
 * @brief SpriteRenderer — renders a single frame of a sprite-sheet on a Quad.
 *
 * Inherits MeshRenderer for rendering pipeline compatibility (CollectRenderables
 * iterates MeshRenderers).  Auto-sets a Quad inline mesh on construction.
 * Sprite-specific properties (sprite GUID, frame index, color, flip) are
 * serialized alongside the MeshRenderer payload so scene files are self-contained.
 */
class SpriteRenderer : public MeshRenderer
{
  public:
    [[nodiscard]] static ComponentTypeConstraints GetTypeConstraints()
    {
        ComponentTypeConstraints constraints;
        constraints.incompatibleTypes = {"MeshRenderer", "SkinnedMeshRenderer"};
        return constraints;
    }

    SpriteRenderer();

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "SpriteRenderer";
    }

    // ====================================================================
    // Sprite properties
    // ====================================================================

    void SetSpriteGuid(const std::string &guid)
    {
        m_spriteGuid = guid;
    }
    [[nodiscard]] const std::string &GetSpriteGuid() const
    {
        return m_spriteGuid;
    }

    void SetFrameIndex(int index)
    {
        m_frameIndex = (index < 0) ? 0 : index;
    }
    [[nodiscard]] int GetFrameIndex() const
    {
        return m_frameIndex;
    }

    void SetColor(const glm::vec4 &color)
    {
        m_color = color;
    }
    [[nodiscard]] const glm::vec4 &GetColor() const
    {
        return m_color;
    }

    void SetFlipX(bool flip)
    {
        m_flipX = flip;
    }
    [[nodiscard]] bool GetFlipX() const
    {
        return m_flipX;
    }

    void SetFlipY(bool flip)
    {
        m_flipY = flip;
    }
    [[nodiscard]] bool GetFlipY() const
    {
        return m_flipY;
    }

    // ====================================================================
    // Rendering
    // ====================================================================

    [[nodiscard]] std::shared_ptr<InxMaterial> GetEffectiveMaterial(uint32_t slot = 0) const override;

    // ====================================================================
    // Serialization
    // ====================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

  private:
    std::string m_spriteGuid;
    int m_frameIndex = 0;
    glm::vec4 m_color{1.0f, 1.0f, 1.0f, 1.0f};
    bool m_flipX = false;
    bool m_flipY = false;
};

} // namespace infernux
