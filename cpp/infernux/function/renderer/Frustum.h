#pragma once

#include <cstdint>
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>
#include <limits>

namespace infernux
{

/**
 * @brief Represents a 3D plane in the form Ax + By + Cz + D = 0
 */
struct Plane
{
    glm::vec3 normal{0.0f, 1.0f, 0.0f}; // A, B, C (normalized)
    float distance = 0.0f;              // D

    Plane() = default;

    Plane(const glm::vec3 &n, float d) : normal(glm::normalize(n)), distance(d)
    {
    }

    /// @brief Create plane from three points (counter-clockwise winding)
    static Plane FromPoints(const glm::vec3 &p0, const glm::vec3 &p1, const glm::vec3 &p2)
    {
        glm::vec3 v1 = p1 - p0;
        glm::vec3 v2 = p2 - p0;
        glm::vec3 n = glm::normalize(glm::cross(v1, v2));
        return Plane(n, -glm::dot(n, p0));
    }

    /// @brief Normalize the plane equation
    void Normalize()
    {
        float len = glm::length(normal);
        if (len > 0.0f) {
            normal /= len;
            distance /= len;
        }
    }

    /// @brief Signed distance from point to plane (positive = in front)
    [[nodiscard]] float DistanceToPoint(const glm::vec3 &point) const
    {
        return glm::dot(normal, point) + distance;
    }
};

/**
 * @brief Axis-Aligned Bounding Box
 */
struct AABB
{
    glm::vec3 min{0.0f};
    glm::vec3 max{0.0f};

    AABB() = default;
    AABB(const glm::vec3 &minPt, const glm::vec3 &maxPt) : min(minPt), max(maxPt)
    {
    }

    /// @brief Get center of the AABB
    [[nodiscard]] glm::vec3 GetCenter() const
    {
        return (min + max) * 0.5f;
    }

    /// @brief Get half-extents (size from center to each face)
    [[nodiscard]] glm::vec3 GetExtents() const
    {
        return (max - min) * 0.5f;
    }

    /// @brief Get size of the AABB
    [[nodiscard]] glm::vec3 GetSize() const
    {
        return max - min;
    }

    /// @brief Check if AABB is valid (min <= max on all axes)
    [[nodiscard]] bool IsValid() const
    {
        return min.x <= max.x && min.y <= max.y && min.z <= max.z;
    }

    /// @brief Check if AABB contains a point
    [[nodiscard]] bool Contains(const glm::vec3 &point) const
    {
        return point.x >= min.x && point.x <= max.x && point.y >= min.y && point.y <= max.y && point.z >= min.z &&
               point.z <= max.z;
    }

    /// @brief Transform AABB by a matrix (returns new AABB enclosing the transformed corners)
    [[nodiscard]] AABB Transform(const glm::mat4 &matrix) const
    {
        // Transform all 8 corners and find new bounds
        glm::vec3 corners[8] = {
            glm::vec3(min.x, min.y, min.z), glm::vec3(max.x, min.y, min.z), glm::vec3(min.x, max.y, min.z),
            glm::vec3(max.x, max.y, min.z), glm::vec3(min.x, min.y, max.z), glm::vec3(max.x, min.y, max.z),
            glm::vec3(min.x, max.y, max.z), glm::vec3(max.x, max.y, max.z),
        };

        glm::vec3 newMin(std::numeric_limits<float>::max());
        glm::vec3 newMax(std::numeric_limits<float>::lowest());

        for (const auto &corner : corners) {
            glm::vec4 transformed = matrix * glm::vec4(corner, 1.0f);
            glm::vec3 pt(transformed.x, transformed.y, transformed.z);
            newMin = glm::min(newMin, pt);
            newMax = glm::max(newMax, pt);
        }

        return AABB(newMin, newMax);
    }
};

/**
 * @brief Bounding Sphere
 */
struct BoundingSphere
{
    glm::vec3 center{0.0f};
    float radius = 0.0f;

    BoundingSphere() = default;
    BoundingSphere(const glm::vec3 &c, float r) : center(c), radius(r)
    {
    }

    /// @brief Create bounding sphere from AABB
    static BoundingSphere FromAABB(const AABB &aabb)
    {
        glm::vec3 center = aabb.GetCenter();
        float radius = glm::length(aabb.GetExtents());
        return BoundingSphere(center, radius);
    }
};

/**
 * @brief Frustum planes for view frustum culling
 *
 * Uses 6 planes: Left, Right, Top, Bottom, Near, Far
 */
class Frustum
{
  public:
    enum class AABBRelation : uint8_t
    {
        Outside,
        Intersecting,
        Inside
    };

    enum PlaneIndex
    {
        Left = 0,
        Right,
        Bottom,
        Top,
        Near,
        Far,
        Count
    };

    Frustum() = default;

    /// @brief Extract frustum planes from a view-projection matrix
    /// @param viewProj The combined View * Projection matrix
    void ExtractFromMatrix(const glm::mat4 &viewProj)
    {
        // Gribb & Hartmann method for extracting frustum planes
        // Reference: http://www.cs.otago.ac.nz/postgrads/alexis/planeExtraction.pdf

        // Left plane: row3 + row0
        m_planes[Left].normal.x = viewProj[0][3] + viewProj[0][0];
        m_planes[Left].normal.y = viewProj[1][3] + viewProj[1][0];
        m_planes[Left].normal.z = viewProj[2][3] + viewProj[2][0];
        m_planes[Left].distance = viewProj[3][3] + viewProj[3][0];

        // Right plane: row3 - row0
        m_planes[Right].normal.x = viewProj[0][3] - viewProj[0][0];
        m_planes[Right].normal.y = viewProj[1][3] - viewProj[1][0];
        m_planes[Right].normal.z = viewProj[2][3] - viewProj[2][0];
        m_planes[Right].distance = viewProj[3][3] - viewProj[3][0];

        // Bottom plane: row3 + row1
        m_planes[Bottom].normal.x = viewProj[0][3] + viewProj[0][1];
        m_planes[Bottom].normal.y = viewProj[1][3] + viewProj[1][1];
        m_planes[Bottom].normal.z = viewProj[2][3] + viewProj[2][1];
        m_planes[Bottom].distance = viewProj[3][3] + viewProj[3][1];

        // Top plane: row3 - row1
        m_planes[Top].normal.x = viewProj[0][3] - viewProj[0][1];
        m_planes[Top].normal.y = viewProj[1][3] - viewProj[1][1];
        m_planes[Top].normal.z = viewProj[2][3] - viewProj[2][1];
        m_planes[Top].distance = viewProj[3][3] - viewProj[3][1];

        // Zero-to-one clip depth: near is row2 (z >= 0), not row3 + row2.
        m_planes[Near].normal.x = viewProj[0][2];
        m_planes[Near].normal.y = viewProj[1][2];
        m_planes[Near].normal.z = viewProj[2][2];
        m_planes[Near].distance = viewProj[3][2];

        // Far plane: row3 - row2
        m_planes[Far].normal.x = viewProj[0][3] - viewProj[0][2];
        m_planes[Far].normal.y = viewProj[1][3] - viewProj[1][2];
        m_planes[Far].normal.z = viewProj[2][3] - viewProj[2][2];
        m_planes[Far].distance = viewProj[3][3] - viewProj[3][2];

        // Normalize all planes
        for (auto &plane : m_planes) {
            plane.Normalize();
        }
    }

    /// @brief Test if a point is inside the frustum
    [[nodiscard]] bool ContainsPoint(const glm::vec3 &point) const
    {
        for (const auto &plane : m_planes) {
            if (plane.DistanceToPoint(point) < 0.0f) {
                return false;
            }
        }
        return true;
    }

    /// @brief Test if a sphere intersects or is inside the frustum
    [[nodiscard]] bool IntersectsSphere(const BoundingSphere &sphere) const
    {
        for (const auto &plane : m_planes) {
            if (plane.DistanceToPoint(sphere.center) < -sphere.radius) {
                return false; // Sphere is fully outside this plane
            }
        }
        return true;
    }

    /// @brief Test if an AABB intersects or is inside the frustum
    /// Uses the "positive vertex" optimization
    /// @param ignoreNearPlane Skip the near-plane test. Directional shadow
    /// cascades pancake casters between the light and the cascade volume in
    /// the vertex shader, so casters beyond the near plane must survive
    /// CPU culling to still stamp their shadow into the map.
    [[nodiscard]] bool IntersectsAABB(const AABB &aabb, bool ignoreNearPlane = false) const
    {
        for (int index = 0; index < PlaneIndex::Count; ++index) {
            if (ignoreNearPlane && index == Near)
                continue;
            const Plane &plane = m_planes[index];
            // Find the "positive vertex" - the corner farthest along the plane normal
            glm::vec3 pVertex;
            pVertex.x = (plane.normal.x >= 0.0f) ? aabb.max.x : aabb.min.x;
            pVertex.y = (plane.normal.y >= 0.0f) ? aabb.max.y : aabb.min.y;
            pVertex.z = (plane.normal.z >= 0.0f) ? aabb.max.z : aabb.min.z;

            // If the positive vertex is outside, the AABB is fully outside
            if (plane.DistanceToPoint(pVertex) < 0.0f) {
                return false;
            }
        }
        return true;
    }

    /// @brief Classify an AABB against the frustum.
    ///
    /// Unlike IntersectsAABB(), this also identifies boxes that are completely
    /// inside. Render systems can use that result to accept a coarse group and
    /// avoid repeating the same plane tests for every member.
    [[nodiscard]] AABBRelation ClassifyAABB(const AABB &aabb, bool ignoreNearPlane = false) const
    {
        bool fullyInside = true;
        for (int index = 0; index < PlaneIndex::Count; ++index) {
            if (ignoreNearPlane && index == Near)
                continue;
            const Plane &plane = m_planes[index];
            const glm::vec3 positive((plane.normal.x >= 0.0f) ? aabb.max.x : aabb.min.x,
                                     (plane.normal.y >= 0.0f) ? aabb.max.y : aabb.min.y,
                                     (plane.normal.z >= 0.0f) ? aabb.max.z : aabb.min.z);
            if (plane.DistanceToPoint(positive) < 0.0f)
                return AABBRelation::Outside;

            const glm::vec3 negative((plane.normal.x >= 0.0f) ? aabb.min.x : aabb.max.x,
                                     (plane.normal.y >= 0.0f) ? aabb.min.y : aabb.max.y,
                                     (plane.normal.z >= 0.0f) ? aabb.min.z : aabb.max.z);
            if (plane.DistanceToPoint(negative) < 0.0f)
                fullyInside = false;
        }
        return fullyInside ? AABBRelation::Inside : AABBRelation::Intersecting;
    }

    /// @brief Get a specific plane
    [[nodiscard]] const Plane &GetPlane(PlaneIndex index) const
    {
        return m_planes[index];
    }

  private:
    Plane m_planes[PlaneIndex::Count];
};

} // namespace infernux
