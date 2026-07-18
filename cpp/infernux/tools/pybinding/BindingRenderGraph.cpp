/**
 * @file BindingRenderGraph.cpp
 * @brief Python bindings for RenderGraph and pass output access
 *
 * Provides Python-side control over the render graph, allowing:
 * - Configuration of render passes
 * - Reading pass output pixels to NumPy arrays
 * - Getting pass texture IDs for ImGui display
 * - Render graph topology definition from Python
 */

#include "Infernux.h"
#include "function/renderer/RenderGraphDescription.h"
#include "function/renderer/SceneRenderGraph.h"

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

namespace infernux
{

void RegisterRenderGraphBindings(py::module_ &m)
{
    // NOTE: ScenePassType and ScenePassConfig are intentionally not exposed to
    // Python. They are internal C++ implementation details of SceneRenderGraph.
    // Python uses GraphPassDesc / RenderGraphDescription (via the RenderGraph
    // builder in Infernux.rendergraph) as its exclusive pass-definition API.

    // ========================================================================
    // RenderGraph topology binding types
    // ========================================================================

    // GraphPassActionType enum
    py::enum_<GraphPassActionType>(m, "GraphPassActionType", "The rendering action a graph pass should perform")
        .value("NONE", GraphPassActionType::None, "No rendering (resource-only pass)")
        .value("DRAW_RENDERERS", GraphPassActionType::DrawRenderers, "Draw scene renderers filtered by queue range")
        .value("DRAW_SKYBOX", GraphPassActionType::DrawSkybox, "Draw the procedural skybox")
        .value("CUSTOM", GraphPassActionType::Custom, "Reserved for future Python callback support")
        .value("DRAW_SHADOW_CASTERS", GraphPassActionType::DrawShadowCasters,
               "Draw shadow casters into a depth-only shadow map")
        .value("DRAW_SCREEN_UI", GraphPassActionType::DrawScreenUI, "Draw screen-space UI (Camera or Overlay list)")
        .value("FULLSCREEN_QUAD", GraphPassActionType::FullscreenQuad,
               "Draw a fullscreen triangle with a named shader (post-process)")
        .export_values();

    py::enum_<ShaderCompileTarget>(m, "MaterialPassType", "Material shader program selected by a render pass")
        .value("FORWARD", ShaderCompileTarget::Forward)
        .value("GBUFFER", ShaderCompileTarget::GBuffer)
        .value("SHADOW", ShaderCompileTarget::Shadow)
        .value("DEPTH", ShaderCompileTarget::Depth)
        .value("PICKING", ShaderCompileTarget::Picking)
        .value("MOTION", ShaderCompileTarget::Motion);

    py::enum_<GraphCommandType>(m, "GraphCommandType", "Backend-neutral command recorded by a graph pass")
        .value("DRAW_RENDERERS", GraphCommandType::DrawRenderers)
        .value("DRAW_SKYBOX", GraphCommandType::DrawSkybox)
        .value("DRAW_SHADOW_CASTERS", GraphCommandType::DrawShadowCasters)
        .value("DRAW_SCREEN_UI", GraphCommandType::DrawScreenUI)
        .value("FULLSCREEN_QUAD", GraphCommandType::FullscreenQuad)
        .value("COPY_TEXTURE", GraphCommandType::CopyTexture)
        .value("COPY_BUFFER", GraphCommandType::CopyBuffer)
        .value("PRESENT", GraphCommandType::Present);

    py::enum_<GraphPassType>(m, "GraphPassType", "Execution domain of a graph pass")
        .value("RASTER", GraphPassType::Raster)
        .value("COMPUTE", GraphPassType::Compute)
        .value("COPY", GraphPassType::Copy)
        .value("PRESENT", GraphPassType::Present);

    py::enum_<GraphBufferUsage>(m, "GraphBufferUsage", py::arithmetic(), "Allowed uses of a graph buffer")
        .value("NONE", GraphBufferUsage::None)
        .value("STORAGE", GraphBufferUsage::Storage)
        .value("INDIRECT", GraphBufferUsage::Indirect)
        .value("TRANSFER_SOURCE", GraphBufferUsage::TransferSource)
        .value("TRANSFER_DESTINATION", GraphBufferUsage::TransferDestination);

    py::enum_<GraphBufferAccessType>(m, "GraphBufferAccessType", "Per-pass graph buffer access")
        .value("STORAGE_READ", GraphBufferAccessType::StorageRead)
        .value("STORAGE_WRITE", GraphBufferAccessType::StorageWrite)
        .value("INDIRECT_READ", GraphBufferAccessType::IndirectRead)
        .value("TRANSFER_READ", GraphBufferAccessType::TransferRead)
        .value("TRANSFER_WRITE", GraphBufferAccessType::TransferWrite);

    py::class_<GraphCommandDesc>(m, "GraphCommandDesc", "Typed command in the Python-defined graph IR")
        .def(py::init<>())
        .def_readwrite("type", &GraphCommandDesc::type)
        .def_readwrite("material_pass", &GraphCommandDesc::shaderTarget)
        .def_readwrite("queue_min", &GraphCommandDesc::queueMin)
        .def_readwrite("queue_max", &GraphCommandDesc::queueMax)
        .def_readwrite("sort_mode", &GraphCommandDesc::sortMode)
        .def_readwrite("pass_tag", &GraphCommandDesc::passTag)
        .def_readwrite("override_material", &GraphCommandDesc::overrideMaterial)
        .def_readwrite("light_index", &GraphCommandDesc::lightIndex)
        .def_readwrite("screen_ui_list", &GraphCommandDesc::screenUIList)
        .def_readwrite("shader_name", &GraphCommandDesc::shaderName)
        .def_readwrite("parameter_block", &GraphCommandDesc::parameterBlock)
        .def_readwrite("push_constants", &GraphCommandDesc::pushConstants)
        .def_readwrite("input_bindings", &GraphCommandDesc::inputBindings)
        .def_readwrite("source_resource", &GraphCommandDesc::sourceResource)
        .def_readwrite("destination_resource", &GraphCommandDesc::destinationResource)
        .def_readwrite("copy_bytes", &GraphCommandDesc::copyBytes);

    py::class_<GraphParameterBlockUpdate>(m, "GraphParameterBlockUpdate",
                                          "Revisioned runtime values for a graph parameter block")
        .def(py::init<>())
        .def_readwrite("id", &GraphParameterBlockUpdate::id)
        .def_readwrite("revision", &GraphParameterBlockUpdate::revision)
        .def_readwrite("values", &GraphParameterBlockUpdate::values);

    // GraphTextureDesc struct
    py::class_<GraphTextureDesc>(m, "GraphTextureDesc", "Description of a texture resource in the Python-defined graph")
        .def(py::init<>())
        .def_readwrite("name", &GraphTextureDesc::name, "Unique resource name")
        .def_readwrite("format", &GraphTextureDesc::format, "Backend-neutral pixel format")
        .def_readwrite("is_backbuffer", &GraphTextureDesc::isBackbuffer,
                       "If true, refers to the scene's main color target")
        .def_readwrite("is_depth", &GraphTextureDesc::isDepth, "If true, this is a depth/stencil texture")
        .def_readwrite("width", &GraphTextureDesc::width, "Custom width (0 = use scene target size)")
        .def_readwrite("height", &GraphTextureDesc::height, "Custom height (0 = use scene target size)")
        .def_readwrite("size_divisor", &GraphTextureDesc::sizeDivisor,
                       "Size divisor relative to scene target (>0: actual = scene / divisor)");

    py::class_<GraphBufferDesc>(m, "GraphBufferDesc", "Description of a buffer resource in the graph")
        .def(py::init<>())
        .def_readwrite("name", &GraphBufferDesc::name)
        .def_readwrite("byte_size", &GraphBufferDesc::byteSize)
        .def_readwrite("usage", &GraphBufferDesc::usage);

    py::class_<GraphBufferAccessDesc>(m, "GraphBufferAccessDesc", "Typed buffer access declared by a graph pass")
        .def(py::init<>())
        .def_readwrite("resource", &GraphBufferAccessDesc::resource)
        .def_readwrite("type", &GraphBufferAccessDesc::type);

    // GraphPassDesc struct
    py::class_<GraphPassDesc>(m, "GraphPassDesc", "Description of a single render pass in the Python-defined graph")
        .def(py::init<>())
        .def_readwrite("name", &GraphPassDesc::name, "Pass name (must be unique)")
        .def_readwrite("type", &GraphPassDesc::type, "Pass execution domain")
        .def_readwrite("read_textures", &GraphPassDesc::readTextures, "Names of textures this pass reads")
        .def_readwrite("write_colors", &GraphPassDesc::writeColors,
                       "MRT color outputs: list of (slot, texture_name) pairs")
        .def_readwrite("write_depth", &GraphPassDesc::writeDepth, "Name of depth output texture")
        .def_readwrite("buffer_accesses", &GraphPassDesc::bufferAccesses, "Typed buffer accesses")
        .def_readwrite("side_effect", &GraphPassDesc::sideEffect, "Retain the pass for externally observable work")
        .def_readwrite("clear_color", &GraphPassDesc::clearColor, "Whether to clear color buffer")
        .def_readwrite("clear_depth", &GraphPassDesc::clearDepth, "Whether to clear depth buffer")
        .def_readwrite("clear_color_r", &GraphPassDesc::clearColorR, "Clear color red")
        .def_readwrite("clear_color_g", &GraphPassDesc::clearColorG, "Clear color green")
        .def_readwrite("clear_color_b", &GraphPassDesc::clearColorB, "Clear color blue")
        .def_readwrite("clear_color_a", &GraphPassDesc::clearColorA, "Clear color alpha")
        .def_readwrite("clear_depth_value", &GraphPassDesc::clearDepthValue, "Depth clear value")
        .def_readwrite("commands", &GraphPassDesc::commands, "Typed commands recorded by this pass")
        .def_readwrite("action", &GraphPassDesc::action, "Deprecated render action compatibility field")
        .def_readwrite("material_pass", &GraphPassDesc::shaderTarget, "Material shader program for DrawRenderers")
        .def_readwrite("queue_min", &GraphPassDesc::queueMin, "Minimum render queue (inclusive)")
        .def_readwrite("queue_max", &GraphPassDesc::queueMax, "Maximum render queue (inclusive)")
        .def_readwrite("sort_mode", &GraphPassDesc::sortMode, "Sort mode: 'front_to_back', 'back_to_front', 'none'")
        .def_readwrite("pass_tag", &GraphPassDesc::passTag, "Filter draw calls by shader pass tag (empty = no filter)")
        .def_readwrite("override_material", &GraphPassDesc::overrideMaterial,
                       "Force all objects to use this material name (empty = per-object)")
        .def_readwrite("input_bindings", &GraphPassDesc::inputBindings,
                       "Shader input bindings: list of (sampler_name, texture_name) pairs")
        .def_readwrite("light_index", &GraphPassDesc::lightIndex, "Shadow-casting light index (0 = first directional)")
        .def_readwrite("screen_ui_list", &GraphPassDesc::screenUIList,
                       "Screen UI list: 0 = Camera (before post-process), 1 = Overlay (after post-process)")
        .def_readwrite("shader_name", &GraphPassDesc::shaderName, "Shader id for FullscreenQuad action")
        .def_readwrite("push_constants", &GraphPassDesc::pushConstants,
                       "Push-constant values: list of (name, float) pairs");

    // RenderGraphDescription struct
    py::class_<RenderGraphDescription>(m, "RenderGraphDescription", "Complete render graph topology defined by Python")
        .def(py::init<>())
        .def_readwrite("name", &RenderGraphDescription::name, "Graph name for debugging")
        .def_readwrite("source_revision", &RenderGraphDescription::sourceRevision,
                       "Monotonic Python graph artifact revision")
        .def_readwrite("textures", &RenderGraphDescription::textures, "All texture resources")
        .def_readwrite("buffers", &RenderGraphDescription::buffers, "All buffer resources")
        .def_readwrite("passes", &RenderGraphDescription::passes, "All passes in declaration order")
        .def_readwrite("output_texture", &RenderGraphDescription::outputTexture, "Name of the final output texture")
        .def_readwrite("msaa_samples", &RenderGraphDescription::msaaSamples,
                       "MSAA sample count (0=no change, 1=off, 2, 4, 8)");

    // SceneRenderGraph class
    py::class_<SceneRenderGraph>(m, "SceneRenderGraph")
        // Pass configuration
        .def("mark_dirty", &SceneRenderGraph::MarkDirty, "Force rebuild of the render graph on next frame")
        // Render graph topology defined from Python
        .def("apply_python_graph", &SceneRenderGraph::ApplyPythonGraph, py::arg("description"),
             "Apply a render graph topology defined in Python")
        .def("update_parameter_blocks", &SceneRenderGraph::UpdateParameterBlocks, py::arg("updates"),
             "Upload changed runtime parameter blocks without rebuilding topology")
        .def("has_python_graph", &SceneRenderGraph::HasPythonGraph, "Check if a Python graph topology has been applied")
        .def("get_pass_count", &SceneRenderGraph::GetPassCount, "Get number of configured passes")
        .def("get_debug_string", &SceneRenderGraph::GetDebugString, "Get debug visualization of the render graph");

    // Add RenderGraph access methods to Infernux
    // These are defined in BindingInfernux.cpp but we document them here for clarity:
    // - get_scene_render_graph() -> SceneRenderGraph
}

} // namespace infernux
