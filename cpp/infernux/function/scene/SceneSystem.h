#pragma once

/**
 * @file SceneSystem.h
 * @brief Unified include for the Scene System.
 *
 * Include this file to get access to all scene-related classes:
 * - Component (base class for all components)
 * - Transform (position, rotation, scale)
 * - GameObject (entity container)
 * - Scene (scene container)
 * - SceneManager (scene management singleton)
 * - Camera (camera component)
 * - Light (lighting component - directional, point, spot)
 * - LightingData (GPU lighting structures and light collector)
 * - EditorCameraController (Unity-style camera controls)
 * - MeshRenderer (mesh rendering component)
 * - SceneRenderer (bridges scene to Vulkan rendering)
 * - PrimitiveMeshes (built-in mesh data)
 */

#include "Camera.h"
#include "Component.h"
#include "EditorCameraController.h"
#include "GameObject.h"
#include "Light.h"
#include "LightingData.h"
#include "MeshRenderer.h"
#include "PrimitiveMeshes.h"
#include "Scene.h"
#include "SceneManager.h"
#include "SceneRenderBridge.h"
#include "Transform.h"
