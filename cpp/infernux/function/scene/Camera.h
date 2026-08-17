#pragma once

#include "Component.h"
#include "Transform.h"
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>
#include <utility>

namespace infernux
{

/**
 * @brief Camera projection mode
 */
enum class CameraProjection
{
    Perspective,
    Orthographic
};

/**
 * @brief Camera clear flags (Unity URP-style)
 *
 * Controls how a camera clears the render target before rendering.
 */
enum class CameraClearFlags
{
    Skybox,     ///< Clear color+depth, then draw skybox (default)
    SolidColor, ///< Clear color+depth with backgroundColor
    DepthOnly,  ///< Clear depth only, preserve color (for Overlay cameras)
    DontClear   ///< No clearing (accumulative rendering)
};

/**
 * @brief Camera component for viewing the scene.
 *
 * Provides view and projection matrices for rendering.
 * Supports both perspective and orthographic projection.
 */
class Camera : public Component
{
  public:
    Camera() = default;
    ~Camera() override = default;

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "Camera";
    }

    // ========================================================================
    // Serialization
    // ========================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

    // ========================================================================
    // Projection settings
    // ========================================================================

    [[nodiscard]] CameraProjection GetProjectionMode() const
    {
        return m_projectionMode;
    }
    void SetProjectionMode(CameraProjection mode)
    {
        m_projectionMode = mode;
        m_projectionDirty = true;
    }

    // Perspective settings
    [[nodiscard]] float GetFieldOfView() const
    {
        return m_fov;
    }
    void SetFieldOfView(float fov)
    {
        m_fov = fov;
        m_projectionDirty = true;
    }

    [[nodiscard]] float GetAspectRatio() const
    {
        return m_aspectRatio;
    }
    void SetAspectRatio(float aspect)
    {
        m_aspectRatio = (aspect < 0.01f) ? 0.01f : aspect;
        m_projectionDirty = true;
    }

    // Orthographic settings
    [[nodiscard]] float GetOrthographicSize() const
    {
        return m_orthoSize;
    }
    void SetOrthographicSize(float size)
    {
        m_orthoSize = size;
        m_projectionDirty = true;
    }

    // Clipping planes
    [[nodiscard]] float GetNearClip() const
    {
        return m_nearClip;
    }
    void SetNearClip(float nearClip)
    {
        m_nearClip = nearClip;
        m_projectionDirty = true;
    }

    [[nodiscard]] float GetFarClip() const
    {
        return m_farClip;
    }
    void SetFarClip(float farClip)
    {
        m_farClip = farClip;
        m_projectionDirty = true;
    }

    // ========================================================================
    // Multi-camera support (depth ordering, layer culling)
    // ========================================================================

    /// @brief Camera rendering depth (lower depth renders first, like Unity)
    [[nodiscard]] float GetDepth() const
    {
        return m_depth;
    }
    void SetDepth(float depth)
    {
        m_depth = depth;
    }

    /// @brief Culling mask — which layers this camera renders (bitmask)
    [[nodiscard]] uint32_t GetCullingMask() const
    {
        return m_cullingMask;
    }
    void SetCullingMask(uint32_t mask)
    {
        m_cullingMask = mask;
    }

    // ========================================================================
    // Clear flags & background color
    // ========================================================================

    [[nodiscard]] CameraClearFlags GetClearFlags() const
    {
        return m_clearFlags;
    }
    void SetClearFlags(CameraClearFlags flags)
    {
        m_clearFlags = flags;
    }

    [[nodiscard]] glm::vec4 GetBackgroundColor() const
    {
        return m_backgroundColor;
    }
    void SetBackgroundColor(const glm::vec4 &color)
    {
        m_backgroundColor = color;
    }

    [[nodiscard]] bool GetDithering() const
    {
        return m_dithering;
    }
    void SetDithering(bool enabled)
    {
        m_dithering = enabled;
    }

    [[nodiscard]] bool GetStopNaNs() const
    {
        return m_stopNaNs;
    }
    void SetStopNaNs(bool enabled)
    {
        m_stopNaNs = enabled;
    }

    // ========================================================================
    // Screen dimensions used by ScreenToWorld / WorldToScreen
    // ========================================================================

    [[nodiscard]] uint32_t GetPixelWidth() const
    {
        return m_screenWidth;
    }
    [[nodiscard]] uint32_t GetPixelHeight() const
    {
        return m_screenHeight;
    }
    void SetScreenDimensions(uint32_t width, uint32_t height)
    {
        m_screenWidth = width;
        m_screenHeight = height;
    }

    // ========================================================================
    // Matrices
    // ========================================================================

    /// @brief Get view matrix (inverse of camera transform)
    [[nodiscard]] glm::mat4 GetViewMatrix() const;

    /// @brief Get projection matrix
    [[nodiscard]] glm::mat4 GetProjectionMatrix() const;

    /// @brief Get view-projection matrix
    [[nodiscard]] glm::mat4 GetViewProjectionMatrix() const
    {
        return GetProjectionMatrix() * GetViewMatrix();
    }

    // ========================================================================
    // Utility
    // ========================================================================

    /// @brief Convert screen coordinates to world ray
    [[nodiscard]] glm::vec3 ScreenToWorldPoint(const glm::vec2 &screenPos, float depth = 0.0f) const;

    /// @brief Convert world position to screen coordinates
    [[nodiscard]] glm::vec2 WorldToScreenPoint(const glm::vec3 &worldPos) const;

    /// @brief Build a ray from screen coordinates (Unity: Camera.ScreenPointToRay).
    ///        Returns (origin, direction) as a pair of vec3.
    [[nodiscard]] std::pair<glm::vec3, glm::vec3> ScreenPointToRay(const glm::vec2 &screenPos) const;

    /// @brief Overload that uses explicit viewport dimensions instead of m_screenWidth/m_screenHeight.
    ///        Used by the editor scene picker where the viewport may differ from the camera's render target.
    [[nodiscard]] std::pair<glm::vec3, glm::vec3> ScreenPointToRay(const glm::vec2 &screenPos, float viewportWidth,
                                                                   float viewportHeight) const;

  private:
    void UpdateProjectionMatrix() const;

    CameraProjection m_projectionMode = CameraProjection::Perspective;

    // Perspective
    float m_fov = 60.0f; // Field of view in degrees
    float m_aspectRatio = 16.0f / 9.0f;

    // Orthographic
    float m_orthoSize = 5.0f; // Half-height of the view

    // Clipping - use large range for editor camera
    float m_nearClip = 0.01f;
    float m_farClip = 5000.0f;

    // Multi-camera: depth ordering (lower = rendered first)
    float m_depth = 0.0f;

    // Layer culling mask (all layers by default)
    uint32_t m_cullingMask = 0xFFFFFFFF;

    // Clear flags
    CameraClearFlags m_clearFlags = CameraClearFlags::Skybox;
    glm::vec4 m_backgroundColor{0.1f, 0.1f, 0.1f, 1.0f};
    bool m_dithering = false;
    bool m_stopNaNs = false;

    // Screen dimensions updated by InxRenderer
    uint32_t m_screenWidth = 1920;
    uint32_t m_screenHeight = 1080;

    // Cached projection matrix
    mutable glm::mat4 m_cachedProjection{1.0f};
    mutable bool m_projectionDirty = true;
};

} // namespace infernux
