/**
 * @file CylinderCollider.h
 * @brief Cylinder collider component backed by Jolt CylinderShape.
 */

#pragma once

#include "Collider.h"

namespace infernux
{

class CylinderCollider : public Collider
{
  public:
    CylinderCollider() = default;
    ~CylinderCollider() override = default;

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "CylinderCollider";
    }

    [[nodiscard]] float GetRadius() const
    {
        return m_radius;
    }
    void SetRadius(float radius);

    /// @brief Total end-to-end cylinder height.
    [[nodiscard]] float GetHeight() const
    {
        return m_height;
    }
    void SetHeight(float height);

    /// @brief Direction axis: 0 = X, 1 = Y (default), 2 = Z.
    [[nodiscard]] int GetDirection() const
    {
        return m_direction;
    }
    void SetDirection(int direction);

    [[nodiscard]] void *CreateJoltShapeRaw() const override;

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

    void AutoFitToMesh() override;

  private:
    float m_radius = 0.5f;
    float m_height = 1.0f;
    int m_direction = 1;
};

} // namespace infernux
