/**
 * @file VkCore.h
 * @brief Unified header for the modular Vulkan rendering system
 *
 * This is the main include header for the Infernux Vulkan backend.
 * It provides a clean, modular architecture for Vulkan rendering with:
 *
 * - RAII Resource Management: All Vulkan objects are automatically cleaned up
 * - Modular Design: Device, Swapchain, Pipeline, Resources are separate concerns
 * - RenderGraph Ready: Architecture prepared for declarative frame graphs
 * - Python Binding Ready: Designed for easy pybind11 integration
 *
 * Module Overview:
 *
 * VkTypes.h         - Common types, forward declarations, DeletionQueue
 * VkHandle.h        - RAII wrappers for Vulkan handles (Buffer, Image, Texture)
 * VkDeviceContext.h - Instance, Physical/Logical Device, Queues management
 * VkSwapchainManager.h - Swapchain lifecycle and frame synchronization
 * VkPipelineManager.h  - Pipeline, RenderPass, Shader management
 * VkResourceManager.h  - Buffer, Image, Texture, Descriptor management
 * RenderGraph.h        - Frame graph for modern rendering pipelines
 *
 * Quick Start:
 *
 *   #include "vk/VkCore.h"
 *   using namespace infernux::vk;
 *
 *   // Initialize device
 *   VkDeviceContext device;
 *   DeviceConfig config;
 *   config.appName = "MyApp";
 *   config.enableValidation = true;
 *   device.Initialize(window, config);
 *
 *   // Create swapchain
 *   VkSwapchainManager swapchain;
 *   swapchain.Create(device, width, height);
 *
 *   // Create resources
 *   VkResourceManager resources;
 *   resources.Initialize(device);
 *
 *   // Create pipelines
 *   VkPipelineManager pipelines;
 *   pipelines.Initialize(device.GetDevice());
 *
 * Architecture Diagram:
 *
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │                     Application Layer                       │
 *   │                  (Infernux, InxRenderer)                   │
 *   └──────────────────────────┬──────────────────────────────────┘
 *                              │
 *   ┌──────────────────────────▼──────────────────────────────────┐
 *   │                      RenderGraph                             │
 *   │        (Declarative frame description, pass culling)        │
 *   └──────────────────────────┬──────────────────────────────────┘
 *                              │
 *   ┌──────────────────────────▼──────────────────────────────────┐
 *   │                   Pipeline Manager                           │
 *   │         (Shaders, Pipelines, RenderPasses, Layouts)         │
 *   └──────────────────────────┬──────────────────────────────────┘
 *                              │
 *   ┌──────────────────────────▼──────────────────────────────────┐
 *   │                   Resource Manager                           │
 *   │    (Buffers, Images, Textures, Descriptors, Samplers)       │
 *   └──────────────────────────┬──────────────────────────────────┘
 *                              │
 *   ┌──────────────────────────▼──────────────────────────────────┐
 *   │                  Swapchain Manager                           │
 *   │    (Swapchain lifecycle, Frame sync, Image acquisition)     │
 *   └──────────────────────────┬──────────────────────────────────┘
 *                              │
 *   ┌──────────────────────────▼──────────────────────────────────┐
 *   │                   Device Context                             │
 *   │      (Instance, Physical Device, Logical Device, Queues)    │
 *   └──────────────────────────┬──────────────────────────────────┘
 *                              │
 *   ┌──────────────────────────▼──────────────────────────────────┐
 *   │                      Vulkan API                              │
 *   └─────────────────────────────────────────────────────────────┘
 *
 * @note Include only this header for full Vulkan backend access
 */

#pragma once

// Core types and utilities
#include "VkHandle.h"
#include "VkTypes.h"

// Device and infrastructure
#include "VkDeviceContext.h"
#include "VkSwapchainManager.h"
#include "VulkanBackendContext.h"

// Resource management
#include "AsyncTransferContext.h"
#include "VkPipelineManager.h"
#include "VkResourceManager.h"

// Advanced rendering
#include "RenderGraph.h"

namespace infernux
{
namespace vk
{

/**
 * @brief Version information for the Vulkan backend
 */
constexpr int VK_BACKEND_VERSION_MAJOR = 1;
constexpr int VK_BACKEND_VERSION_MINOR = 0;
constexpr int VK_BACKEND_VERSION_PATCH = 0;

/**
 * @brief Get version string
 */
inline const char *GetVkBackendVersionString()
{
    return "1.0.0";
}

} // namespace vk
} // namespace infernux
