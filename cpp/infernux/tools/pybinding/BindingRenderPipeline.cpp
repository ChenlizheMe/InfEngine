#include <function/renderer/CommandBuffer.h>
#include <function/renderer/RenderGraphDescription.h>
#include <function/renderer/ScriptableRenderContext.h>
#include <function/scene/Camera.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

using namespace infernux;
namespace py = pybind11;

// ============================================================================
// Trampoline for Python-overridable RenderPipelineCallback
// ============================================================================

class PyRenderPipelineCallback : public RenderPipelineCallback
{
  public:
    using RenderPipelineCallback::RenderPipelineCallback;

    void Render(ScriptableRenderContext &context, const std::vector<Camera *> &cameras) override
    {
        // Look for Python method "render" (lowercase, matching Python convention)
        PYBIND11_OVERRIDE_PURE_NAME(void, RenderPipelineCallback, "render", Render, &context, cameras);
    }

    void Dispose() override
    {
        PYBIND11_OVERRIDE_NAME(void, RenderPipelineCallback, "dispose", Dispose);
    }
};

// ============================================================================
// Registration
// ============================================================================

namespace infernux
{
void RegisterRenderPipelineBindings(py::module_ &m)
{
    // ---- CullingResults ----
    py::class_<CullingResults>(m, "CullingResults")
        .def_property_readonly("visible_object_count",
                               [](const CullingResults &self) { return self.visibleObjectCount(); })
        .def_property_readonly("visible_light_count",
                               [](const CullingResults &self) { return self.visibleLightCount(); });

    // ---- Camera is now bound in BindingScene.cpp as Camera(Component) ----
    // No duplicate registration needed here.

    // ---- ScriptableRenderContext ----
    py::class_<ScriptableRenderContext>(m, "ScriptableRenderContext")
        .def_property_readonly("scene", &ScriptableRenderContext::GetScene, py::return_value_policy::reference,
                               "Get the scene associated with this render context")
        .def("setup_camera_properties", &ScriptableRenderContext::SetupCameraProperties, py::arg("camera"),
             "Set camera VP matrices for rendering")
        .def("cull", &ScriptableRenderContext::Cull, py::arg("camera"), py::return_value_policy::reference_internal,
             "Cull scene objects, return context-owned CullingResults")
        // RenderGraph-driven API
        .def("apply_graph", &ScriptableRenderContext::ApplyGraph, py::arg("description"),
             "Apply a Python-defined RenderGraph topology to the scene render graph")
        .def("submit_culling", &ScriptableRenderContext::SubmitCulling, py::arg("culling"),
             "Submit all culling results as full draw calls (filtering done by graph pass callbacks)")
        .def("render_with_graph", &ScriptableRenderContext::RenderWithGraph, py::arg("camera"), py::arg("description"),
             "Single-call render: setup + cull + apply_graph + submit (avoids Python round-trips)")
        .def("is_graph_revision_current", &ScriptableRenderContext::IsGraphRevisionCurrent, py::arg("source_revision"),
             "Check whether the active graph already owns a Python artifact revision")
        .def("render_compiled", &ScriptableRenderContext::RenderCompiled, py::arg("camera"), py::arg("source_revision"),
             "Render with an already-applied graph without uploading topology")
        // CommandBuffer integration
        .def("execute_command_buffer", &ScriptableRenderContext::ExecuteCommandBuffer, py::arg("cmd"),
             "Execute a deferred CommandBuffer (commands are buffered until submit)")
        .def("get_camera_target", &ScriptableRenderContext::GetCameraTarget, py::arg("camera"),
             "Get a handle representing the final camera render target")
        // Global shader parameters (immediate mode)
        .def(
            "set_global_texture",
            [](ScriptableRenderContext &self, const std::string &name, RenderTargetHandle handle) {
                self.SetGlobalTexture(name, handle);
            },
            py::arg("name"), py::arg("handle"), "Set a global texture shader parameter")
        .def("set_global_float", &ScriptableRenderContext::SetGlobalFloat, py::arg("name"), py::arg("value"),
             "Set a global float shader parameter")
        .def("set_global_vector", &ScriptableRenderContext::SetGlobalVector, py::arg("name"), py::arg("x"),
             py::arg("y"), py::arg("z"), py::arg("w"), "Set a global vec4 shader parameter");

    // ---- RenderPipelineCallback (abstract, Python inherits via trampoline) ----
    py::class_<RenderPipelineCallback, PyRenderPipelineCallback, std::shared_ptr<RenderPipelineCallback>>(
        m, "RenderPipelineCallback")
        .def(py::init<>())
        .def("render", &RenderPipelineCallback::Render, py::arg("context"), py::arg("cameras"),
             "Called once per frame to define rendering pass sequence")
        .def("dispose", &RenderPipelineCallback::Dispose, "Called when the pipeline is being replaced");
}
} // namespace infernux
