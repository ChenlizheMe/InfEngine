/**
 * @file ScenePicker.cpp
 * @brief Infernux — Raycast helpers and scene object picking
 *
 * Split from Infernux.cpp for maintainability.
 *
 * Picking philosophy:
 *   - Scene View editor selection should work on visible scene objects, not
 *     only physics colliders. We therefore combine collider hits with a
 *     lightweight MeshRenderer bounds test.
 *   - Component icon billboards (lights, cameras, etc.) are always pickable
 *     via a lightweight screen-space proximity test.
 *   - Gizmo handles (translate/rotate/scale) are picked via a dedicated
 *     lightweight `PickGizmoAxis()` that tests both axes and plane squares.
 *
 * Contains: CollectIconHits,
 *           Infernux::PickSceneObjectId, Infernux::PickSceneObjectIds,
 *           Infernux::PickGizmoAxis,
 *           Infernux::SetEditorToolHighlight, Infernux::ScreenToWorldRay.
 */

#include "Infernux.h"

#include <algorithm>
#include <cmath>
#include <core/config/MathConstants.h>
#include <function/renderer/EditorTools.h>
#include <function/renderer/GizmosDrawCallBuffer.h>
#include <function/renderer/InxRenderer.h>
#include <function/scene/MeshRenderer.h>
#include <function/scene/SceneRenderer.h>
#include <function/scene/physics/PhysicsWorld.h>
#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>
#include <limits>
#include <tuple>
#include <unordered_set>

namespace infernux
{

// ----------------------------------
// Shared picking helpers
// ----------------------------------

/// Collect all icon hits within icon radius, appending to `hits`.
static void CollectIconHits(InxRenderer *rendererPtr, const glm::vec3 &rayOrigin, const glm::vec3 &rayDirection,
                            std::vector<std::pair<float, uint64_t>> &hits)
{
    if (!rendererPtr)
        return;
    GizmosDrawCallBuffer *buf = rendererPtr->GetGizmosDrawCallBuffer();
    if (!buf || !buf->HasIconData())
        return;

    const auto &icons = buf->GetIconEntries();
    for (const auto &icon : icons) {
        float t = glm::dot(icon.position - rayOrigin, rayDirection);
        if (t < 0.0f)
            continue;
        glm::vec3 closestOnRay = rayOrigin + rayDirection * t;
        float dist = glm::length(closestOnRay - icon.position);
        float camDist = glm::length(icon.position - rayOrigin);
        float iconRadius = camDist * GizmosDrawCallBuffer::ICON_SIZE_FACTOR;
        if (dist < iconRadius) {
            hits.emplace_back(t, icon.objectId);
        }
    }
}

static bool IntersectRayAabb(const glm::vec3 &rayOrigin, const glm::vec3 &rayDirection, const glm::vec3 &boundsMin,
                             const glm::vec3 &boundsMax, float &outDistance)
{
    float tMin = 0.0f;
    float tMax = std::numeric_limits<float>::max();

    for (int axis = 0; axis < 3; ++axis) {
        float origin = rayOrigin[axis];
        float direction = rayDirection[axis];
        float minV = boundsMin[axis];
        float maxV = boundsMax[axis];

        if (std::abs(direction) < 1e-8f) {
            if (origin < minV || origin > maxV)
                return false;
            continue;
        }

        float invDir = 1.0f / direction;
        float t1 = (minV - origin) * invDir;
        float t2 = (maxV - origin) * invDir;
        if (t1 > t2)
            std::swap(t1, t2);

        tMin = std::max(tMin, t1);
        tMax = std::min(tMax, t2);
        if (tMin > tMax)
            return false;
    }

    outDistance = (tMin >= 0.0f) ? tMin : tMax;
    return outDistance >= 0.0f;
}

static void CollectMeshRendererHits(const glm::vec3 &rayOrigin, const glm::vec3 &rayDirection,
                                    std::vector<std::pair<float, uint64_t>> &hits)
{
    const auto &renderers = SceneManager::Instance().GetActiveMeshRenderers();
    for (MeshRenderer *renderer : renderers) {
        if (!renderer || !renderer->IsEnabled())
            continue;

        GameObject *object = renderer->GetGameObject();
        if (!object || !object->IsActiveInHierarchy())
            continue;

        glm::vec3 boundsMin;
        glm::vec3 boundsMax;
        renderer->GetWorldBounds(boundsMin, boundsMax);

        float hitDistance = 0.0f;
        if (IntersectRayAabb(rayOrigin, rayDirection, boundsMin, boundsMax, hitDistance)) {
            hits.emplace_back(hitDistance, object->GetID());
        }
    }
}

// ----------------------------------
// Scene Picking
// ----------------------------------

/// Internal: Test gizmo handle proximity against a ray. Returns handle ID or 0.
static uint64_t TestGizmoAxes(const glm::vec3 &rayOrigin, const glm::vec3 &rayDirection, EditorTools *tools,
                              Transform *selTransform, Camera *camera, float viewportHeight)
{
    EditorTools::ToolMode toolMode = tools->GetToolMode();
    glm::vec3 objPos = selTransform->GetPosition();
    float camDist = glm::length(rayOrigin - objPos);
    float scale = camDist * 0.15f * tools->GetHandleSize();
    if (scale < 0.01f)
        scale = 0.01f;

    float arrowLen = 1.0f * scale;

    constexpr float PIXEL_THRESHOLD = 12.0f;
    float tanHalfFov = std::tan(glm::radians(camera->GetFieldOfView()) * 0.5f);
    float worldPerPixel = (2.0f * camDist * tanHalfFov) / viewportHeight;
    float worldThreshold = PIXEL_THRESHOLD * worldPerPixel;

    static const glm::vec3 WORLD_AXIS_DIRS[3] = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}};
    static const uint64_t AXIS_IDS[3] = {EditorTools::X_AXIS_ID, EditorTools::Y_AXIS_ID, EditorTools::Z_AXIS_ID};

    glm::vec3 localAxisDirs[3];
    const glm::vec3 *axisDirs = WORLD_AXIS_DIRS;
    if (tools->GetLocalMode()) {
        glm::quat worldRot = selTransform->GetWorldRotation();
        localAxisDirs[0] = worldRot * WORLD_AXIS_DIRS[0];
        localAxisDirs[1] = worldRot * WORLD_AXIS_DIRS[1];
        localAxisDirs[2] = worldRot * WORLD_AXIS_DIRS[2];
        axisDirs = localAxisDirs;
    }

    float bestDist = worldThreshold;
    uint64_t gizmoPickedId = 0;

    if (toolMode == EditorTools::ToolMode::Rotate) {
        constexpr float MAJOR_RADIUS = 0.85f;
        float ringRadius = MAJOR_RADIUS * scale;
        float ringThreshold = worldThreshold * 1.2f;
        bestDist = ringThreshold;

        for (int ai = 0; ai < 3; ++ai) {
            glm::vec3 normal = axisDirs[ai];
            float dDotN = glm::dot(rayDirection, normal);

            float tPlane;
            if (std::abs(dDotN) > kEpsilon) {
                tPlane = glm::dot(objPos - rayOrigin, normal) / dDotN;
            } else {
                tPlane = glm::dot(objPos - rayOrigin, rayDirection);
            }
            if (tPlane < 0.0f)
                tPlane = 0.0f;

            glm::vec3 P = rayOrigin + rayDirection * tPlane;
            glm::vec3 toP = P - objPos;
            glm::vec3 toPinPlane = toP - glm::dot(toP, normal) * normal;

            float lenInPlane = glm::length(toPinPlane);
            glm::vec3 Q;
            if (lenInPlane < 1e-8f) {
                glm::vec3 arbitrary = (std::abs(normal.x) < 0.9f) ? glm::vec3(1, 0, 0) : glm::vec3(0, 1, 0);
                glm::vec3 perp = glm::normalize(glm::cross(normal, arbitrary));
                Q = objPos + perp * ringRadius;
            } else {
                Q = objPos + (toPinPlane / lenInPlane) * ringRadius;
            }

            float tQ = glm::dot(Q - rayOrigin, rayDirection);
            if (tQ < 0.0f)
                tQ = 0.0f;
            glm::vec3 closestOnRay = rayOrigin + rayDirection * tQ;
            float dist = glm::length(closestOnRay - Q);

            if (dist < bestDist) {
                bestDist = dist;
                gizmoPickedId = AXIS_IDS[ai];
            }
        }
    } else {
        // Scale: the uniform cube at the origin takes priority when the ray
        // hits its AABB — it sits where the three plane quads meet.
        if (toolMode == EditorTools::ToolMode::Scale) {
            const float half = EditorTools::CENTER_CUBE_HALF * scale;
            const glm::vec3 boundsMin = objPos - glm::vec3(half);
            const glm::vec3 boundsMax = objPos + glm::vec3(half);
            float centerHitT = 0.0f;
            if (IntersectRayAabb(rayOrigin, rayDirection, boundsMin, boundsMax, centerHitT)) {
                return EditorTools::CENTER_ID;
            }
        }

        // Translate / Scale: ray-to-line-segment proximity
        for (int ai = 0; ai < 3; ++ai) {
            glm::vec3 axisDir = axisDirs[ai];
            glm::vec3 segStart = objPos;

            glm::vec3 w = rayOrigin - segStart;
            float a = glm::dot(rayDirection, rayDirection);
            float b = glm::dot(rayDirection, axisDir);
            float c = glm::dot(axisDir, axisDir);
            float d = glm::dot(rayDirection, w);
            float e = glm::dot(axisDir, w);

            float denom = a * c - b * b;
            float t, s;
            if (std::abs(denom) < 1e-8f) {
                t = 0.0f;
                s = e / c;
            } else {
                t = (b * e - c * d) / denom;
                s = (a * e - b * d) / denom;
            }

            s = std::max(0.0f, std::min(s, arrowLen));
            t = glm::dot((segStart + axisDir * s) - rayOrigin, rayDirection);
            if (t < 0.0f)
                t = 0.0f;

            glm::vec3 closestOnRay = rayOrigin + rayDirection * t;
            glm::vec3 closestOnAxis = segStart + axisDir * s;
            float dist = glm::length(closestOnRay - closestOnAxis);

            if (dist < bestDist) {
                bestDist = dist;
                gizmoPickedId = AXIS_IDS[ai];
            }
        }

        // Plane handles are Translate-only; Scale uses axes + center cube.
        if (toolMode == EditorTools::ToolMode::Translate) {
            struct PlaneCandidate
            {
                glm::vec3 axisU;
                glm::vec3 axisV;
                uint64_t id;
            };

            const PlaneCandidate planes[3] = {
                {axisDirs[0], axisDirs[1], EditorTools::XY_PLANE_ID},
                {axisDirs[0], axisDirs[2], EditorTools::XZ_PLANE_ID},
                {axisDirs[1], axisDirs[2], EditorTools::YZ_PLANE_ID},
            };

            float bestPlaneT = std::numeric_limits<float>::max();
            uint64_t bestPlaneId = 0;
            for (const PlaneCandidate &plane : planes) {
                glm::vec3 normal = glm::normalize(glm::cross(plane.axisU, plane.axisV));
                float denom = glm::dot(rayDirection, normal);
                if (std::abs(denom) < kEpsilon) {
                    continue;
                }

                float tPlane = glm::dot(objPos - rayOrigin, normal) / denom;
                if (tPlane < 0.0f) {
                    continue;
                }

                glm::vec3 point = rayOrigin + rayDirection * tPlane;
                glm::vec3 rel = point - objPos;
                float u = glm::dot(rel, plane.axisU);
                float v = glm::dot(rel, plane.axisV);
                const float minCoord = EditorTools::PLANE_OFFSET * scale;
                const float maxCoord = (EditorTools::PLANE_OFFSET + EditorTools::PLANE_SIZE) * scale;

                if (u >= minCoord && u <= maxCoord && v >= minCoord && v <= maxCoord) {
                    if (tPlane < bestPlaneT) {
                        bestPlaneT = tPlane;
                        bestPlaneId = plane.id;
                    }
                }
            }

            if (bestPlaneId != 0) {
                gizmoPickedId = bestPlaneId;
            }
        }
    }

    return gizmoPickedId;
}

uint64_t Infernux::PickGizmoAxis(float screenX, float screenY, float viewportWidth, float viewportHeight)
{
    if (!m_renderer || viewportWidth <= 0.0f || viewportHeight <= 0.0f)
        return 0;

    EditorTools *tools = m_renderer->GetEditorTools();
    if (!tools || tools->GetToolMode() == EditorTools::ToolMode::None || m_selectedObjectId == 0)
        return 0;

    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene)
        return 0;

    Camera *camera = SceneRenderBridge::Instance().GetEditorCamera();
    if (!camera)
        return 0;

    GameObject *selObj = scene->FindByID(m_selectedObjectId);
    if (!selObj || !selObj->IsActiveInHierarchy() || !selObj->GetTransform())
        return 0;

    auto [rayOrigin, rayDirection] =
        camera->ScreenPointToRay(glm::vec2(screenX, screenY), viewportWidth, viewportHeight);

    return TestGizmoAxes(rayOrigin, rayDirection, tools, selObj->GetTransform(), camera, viewportHeight);
}

uint64_t Infernux::PickSceneObjectId(float screenX, float screenY, float viewportWidth, float viewportHeight)
{
    if (!CheckEngineValid("pick scene object")) {
        return 0;
    }

    if (viewportWidth <= 0.0f || viewportHeight <= 0.0f) {
        return 0;
    }

    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene) {
        return 0;
    }

    Camera *camera = SceneRenderBridge::Instance().GetEditorCamera();
    if (!camera) {
        return 0;
    }

    auto [rayOrigin, rayDirection] =
        camera->ScreenPointToRay(glm::vec2(screenX, screenY), viewportWidth, viewportHeight);

    // =========================================================================
    // Physics raycast against collider hits
    // =========================================================================
    float closestDistance = std::numeric_limits<float>::max();
    uint64_t pickedId = 0;

    if (PhysicsWorld::Instance().IsInitialized()) {
        PhysicsWorld::Instance().EnsureSceneBodiesRegistered(scene);

        RaycastHit hit;
        if (PhysicsWorld::Instance().Raycast(rayOrigin, rayDirection, 10000.0f, hit)) {
            if (hit.gameObject) {
                pickedId = hit.gameObject->GetID();
                closestDistance = hit.distance;
            }
        }
    }

    // =========================================================================
    // Visible renderer hits (Scene View should pick visible objects)
    // =========================================================================
    std::vector<std::pair<float, uint64_t>> rendererHits;
    CollectMeshRendererHits(rayOrigin, rayDirection, rendererHits);
    for (const auto &[dist, objId] : rendererHits) {
        if (dist < closestDistance) {
            closestDistance = dist;
            pickedId = objId;
        }
    }

    // =========================================================================
    // Gizmo proximity test (absolute priority — gizmos are always on top)
    // =========================================================================
    if (m_renderer) {
        EditorTools *tools = m_renderer->GetEditorTools();
        bool gizmoTestActive = tools && tools->GetToolMode() != EditorTools::ToolMode::None && m_selectedObjectId != 0;

        if (gizmoTestActive) {
            GameObject *selObj = scene->FindByID(m_selectedObjectId);
            if (selObj && selObj->IsActiveInHierarchy() && selObj->GetTransform()) {
                uint64_t gizmoId =
                    TestGizmoAxes(rayOrigin, rayDirection, tools, selObj->GetTransform(), camera, viewportHeight);
                if (gizmoId != 0) {
                    return gizmoId;
                }
            }
        }
    }

    // =========================================================================
    // Component icon picking (lights, cameras, etc.)
    // =========================================================================
    if (m_renderer) {
        std::vector<std::pair<float, uint64_t>> iconHits;
        CollectIconHits(m_renderer.get(), rayOrigin, rayDirection, iconHits);
        for (const auto &[dist, objId] : iconHits) {
            if (dist < closestDistance) {
                closestDistance = dist;
                pickedId = objId;
            }
        }
    }

    return pickedId;
}

std::vector<uint64_t> Infernux::PickSceneObjectIds(float screenX, float screenY, float viewportWidth,
                                                   float viewportHeight)
{
    std::vector<uint64_t> orderedIds;

    if (!CheckEngineValid("pick scene objects")) {
        return orderedIds;
    }

    if (viewportWidth <= 0.0f || viewportHeight <= 0.0f) {
        return orderedIds;
    }

    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene) {
        return orderedIds;
    }

    Camera *camera = SceneRenderBridge::Instance().GetEditorCamera();
    if (!camera) {
        return orderedIds;
    }

    auto [rayOrigin, rayDirection] =
        camera->ScreenPointToRay(glm::vec2(screenX, screenY), viewportWidth, viewportHeight);

    std::vector<std::pair<float, uint64_t>> hits;
    hits.reserve(64);

    // Physics candidates
    if (PhysicsWorld::Instance().IsInitialized()) {
        PhysicsWorld::Instance().EnsureSceneBodiesRegistered(scene);
        std::vector<RaycastHit> physicsHits = PhysicsWorld::Instance().RaycastAll(rayOrigin, rayDirection, 10000.0f);
        for (const RaycastHit &hit : physicsHits) {
            if (hit.gameObject) {
                hits.emplace_back(hit.distance, hit.gameObject->GetID());
            }
        }
    }

    // Visible renderer candidates for Scene View selection.
    CollectMeshRendererHits(rayOrigin, rayDirection, hits);

    // Icon candidates
    CollectIconHits(m_renderer.get(), rayOrigin, rayDirection, hits);

    if (hits.empty()) {
        return orderedIds;
    }

    std::sort(hits.begin(), hits.end(), [](const std::pair<float, uint64_t> &a, const std::pair<float, uint64_t> &b) {
        return a.first < b.first;
    });

    std::unordered_set<uint64_t> seen;
    seen.reserve(hits.size());
    for (const auto &entry : hits) {
        uint64_t objectId = entry.second;
        if (objectId == 0)
            continue;
        if (seen.insert(objectId).second) {
            orderedIds.push_back(objectId);
        }
    }

    return orderedIds;
}

// ============================================================================
// Editor Tools — highlight + world ray for Python-side gizmo interaction
// ============================================================================

void Infernux::SetEditorToolHighlight(int axis)
{
    if (!m_renderer) {
        return;
    }
    EditorTools *tools = m_renderer->GetEditorTools();
    if (!tools) {
        return;
    }

    EditorTools::HandleAxis ha = EditorTools::HandleAxis::None;
    switch (axis) {
    case 1:
        ha = EditorTools::HandleAxis::X;
        break;
    case 2:
        ha = EditorTools::HandleAxis::Y;
        break;
    case 3:
        ha = EditorTools::HandleAxis::Z;
        break;
    case 4:
        ha = EditorTools::HandleAxis::XY;
        break;
    case 5:
        ha = EditorTools::HandleAxis::XZ;
        break;
    case 6:
        ha = EditorTools::HandleAxis::YZ;
        break;
    case 7:
        ha = EditorTools::HandleAxis::Center;
        break;
    default:
        ha = EditorTools::HandleAxis::None;
        break;
    }
    tools->SetHighlightedAxis(ha);
}

void Infernux::SetEditorToolMode(int mode)
{
    if (!m_renderer)
        return;
    EditorTools *tools = m_renderer->GetEditorTools();
    if (!tools)
        return;

    EditorTools::ToolMode tm = EditorTools::ToolMode::None;
    switch (mode) {
    case 1:
        tm = EditorTools::ToolMode::Translate;
        break;
    case 2:
        tm = EditorTools::ToolMode::Rotate;
        break;
    case 3:
        tm = EditorTools::ToolMode::Scale;
        break;
    default:
        tm = EditorTools::ToolMode::None;
        break;
    }
    tools->SetToolMode(tm);
}

int Infernux::GetEditorToolMode() const
{
    if (!m_renderer)
        return 0;
    EditorTools *tools = m_renderer->GetEditorTools();
    if (!tools)
        return 0;

    switch (tools->GetToolMode()) {
    case EditorTools::ToolMode::Translate:
        return 1;
    case EditorTools::ToolMode::Rotate:
        return 2;
    case EditorTools::ToolMode::Scale:
        return 3;
    default:
        return 0;
    }
}

void Infernux::SetEditorToolLocalMode(bool local)
{
    if (!m_renderer)
        return;
    EditorTools *tools = m_renderer->GetEditorTools();
    if (!tools)
        return;
    tools->SetLocalMode(local);
}

std::tuple<float, float, float, float, float, float>
Infernux::ScreenToWorldRay(float screenX, float screenY, float viewportWidth, float viewportHeight)
{
    Camera *camera = SceneRenderBridge::Instance().GetEditorCamera();
    if (!camera || !camera->GetGameObject()) {
        return {0.f, 0.f, 0.f, 0.f, 0.f, -1.f};
    }

    auto [origin, dir] = camera->ScreenPointToRay(glm::vec2(screenX, screenY), viewportWidth, viewportHeight);

    return {origin.x, origin.y, origin.z, dir.x, dir.y, dir.z};
}

} // namespace infernux
