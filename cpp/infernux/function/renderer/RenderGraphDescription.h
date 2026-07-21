/**
 * @file RenderGraphDescription.h
 * @brief Data structures for render-graph topology defined from Python
 *
 * These POD structures capture the render graph topology defined in Python,
 * allowing C++ to receive, compile, and execute the graph with automatic
 * Vulkan barrier insertion and transient resource management.
 *
 * Architecture:
 *   Python has "definition authority" — defines pass topology, resource
 *   connections, and per-pass render actions.
 *   C++ has "compilation authority" — performs DAG compilation, dead-pass
 *   culling, barrier generation, and transient resource allocation.
 */

#pragma once

#include "rhi/RhiTypes.h"
#include <core/types/ShaderTypes.h>

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace infernux
{

/**
 * @brief Backend-neutral command recorded by a graph pass.
 *
 * Commands describe engine rendering operations
 * rather than Vulkan calls so
 * the same graph artifact can be compiled by another RHI backend later.
 */
enum class GraphCommandType
{
    DrawRenderers,
    DrawSkybox,
    DrawShadowCasters,
    DrawScreenUI,
    FullscreenQuad,
    CopyTexture,
    CopyBuffer,
    Present
};

enum class GraphPassType
{
    Raster,
    Compute,
    Copy,
    Present
};

enum class GraphBufferUsage : uint32_t
{
    None = 0,
    Storage = 1 << 0,
    Indirect = 1 << 1,
    TransferSource = 1 << 2,
    TransferDestination = 1 << 3
};

enum class GraphBufferAccessType
{
    StorageRead,
    StorageWrite,
    IndirectRead,
    TransferRead,
    TransferWrite
};

struct GraphCommandDesc
{
    GraphCommandType type = GraphCommandType::DrawRenderers;
    ShaderCompileTarget shaderTarget = ShaderCompileTarget::Forward;

    int queueMin = 0;
    int queueMax = 5000;
    std::string sortMode;
    std::string passTag;
    std::string overrideMaterial;

    int32_t lightIndex = 0;
    int screenUIList = 0;

    std::string shaderName;
    /// Stable runtime parameter block. When non-empty, pushConstants define
    /// the block layout and initial values rather than immutable topology.
    std::string parameterBlock;
    std::vector<std::pair<std::string, float>> pushConstants;
    std::vector<std::pair<std::string, std::string>> inputBindings;

    std::string sourceResource;
    std::string destinationResource;
    uint64_t copyBytes = 0;
};

/**
 * @brief Revisioned values for one graph-owned runtime parameter block.
 *
 * Parameter updates are intentionally
 * separate from RenderGraphDescription so
 * ordinary effect edits do not rebuild or recompile graph topology.
 */
struct GraphParameterBlockUpdate
{
    std::string id;
    uint64_t revision = 0;
    std::vector<std::pair<std::string, float>> values;
};

// ============================================================================
// Texture Description
// ============================================================================

/**
 * @brief Description of a texture resource in the Python-defined graph
 */
struct GraphTextureDesc
{
    std::string name;                                       ///< Unique resource name
    rhi::PixelFormat format = rhi::PixelFormat::RGBA8UNorm; ///< Backend-neutral pixel format
    bool isBackbuffer = false;                              ///< If true, refers to the scene's main MSAA color target
    bool isDepth = false;                                   ///< If true, this is a depth/stencil texture
    uint32_t width = 0;                                     ///< Custom width (0 = use scene target size)
    uint32_t height = 0;                                    ///< Custom height (0 = use scene target size)
    uint32_t sizeDivisor = 0;                               ///< >0: actual = scene_size / divisor
    uint32_t samples = 1;                                   ///< 0 = inherit frame MSAA, otherwise 1/2/4/8
};

struct GraphBufferDesc
{
    std::string name;
    uint64_t byteSize = 0;
    uint32_t usage = static_cast<uint32_t>(GraphBufferUsage::None);
};

struct GraphBufferAccessDesc
{
    std::string resource;
    GraphBufferAccessType type = GraphBufferAccessType::StorageRead;
};

// ============================================================================
// Pass Description
// ============================================================================

/**
 * @brief Description of a single render pass in the Python-defined graph
 */
struct GraphPassDesc
{
    std::string name; ///< Pass name (must be unique within the graph)
    GraphPassType type = GraphPassType::Raster;

    // === Resource connections ===
    std::vector<std::string> readTextures; ///< Names of textures this pass reads
    /// MRT color outputs: list of (slot, texture_name) pairs.
    /// Slot 0 is the primary color output; higher slots enable deferred / GBuffer.
    std::vector<std::pair<int, std::string>> writeColors;
    std::string writeDepth;   ///< Name of depth output texture
    std::string resolveColor; ///< Optional 1x resolve target for color slot 0
    std::vector<GraphBufferAccessDesc> bufferAccesses;
    bool sideEffect = false;

    // === Clear settings ===
    bool clearColor = false;
    bool clearDepth = false;
    float clearColorR = 0.0f;
    float clearColorG = 0.0f;
    float clearColorB = 0.0f;
    float clearColorA = 1.0f;
    float clearDepthValue = 1.0f;

    // === Typed command IR ===
    // Empty is valid for a resource-only pass. The current executor accepts
    // one command while the IR is intentionally a list for the upcoming
    // raster/compute/copy command-list executor.
    std::vector<GraphCommandDesc> commands;
};

// ============================================================================
// RenderGraph Description (complete topology from Python)
// ============================================================================

/**
 * @brief Complete render graph topology defined by Python
 *
 * This structure is built by the Python RenderGraph API and sent to C++
 * via SceneRenderGraph::ApplyPythonGraph(). C++ uses it to configure
 * the SceneRenderGraph passes and underlying vk::RenderGraph.
 */
struct RenderGraphDescription
{
    std::string name; ///< Graph name for debugging

    /// Monotonic source artifact revision assigned when Python records the graph.
    /// Steady-state frames send only this value to reuse an already-applied graph.
    uint64_t sourceRevision = 0;

    std::vector<GraphTextureDesc> textures; ///< All texture resources
    std::vector<GraphBufferDesc> buffers;   ///< All buffer resources
    std::vector<GraphPassDesc> passes;      ///< All passes in declaration order
    std::string outputTexture;              ///< Name of the final output texture

    /// MSAA sample count requested by the pipeline (0 = don't change, 1/2/4/8).
    int msaaSamples = 0;
};

} // namespace infernux
