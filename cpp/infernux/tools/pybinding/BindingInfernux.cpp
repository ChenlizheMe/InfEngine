#include "Infernux.h"
// Explicit includes for types now only forward-declared in InxRenderer.h
#include <SDL3/SDL.h>
#include <array>
#include <cmath>
#include <core/config/EngineConfig.h>
#include <core/log/InxLog.h>
#include <cstring>
#include <function/renderer/EditorTools.h>
#include <function/renderer/GizmosDrawCallBuffer.h>
#include <function/renderer/ParticleDrawCallBuffer.h>
#include <function/renderer/SceneRenderGraph.h>
#include <function/renderer/ScriptableRenderContext.h>
#include <function/renderer/gui/InxGUIContext.h>
#include <function/renderer/gui/InxGUIRenderable.h>
#include <function/renderer/gui/InxResourcePreviewer.h>
#include <function/renderer/gui/InxScreenUIRenderer.h>
#include <function/renderer/particle/ParticleGpuSystemManager.h>
#include <function/renderer/vk/VkResourceManager.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/EditorCameraController.h>
#include <glm/glm.hpp>
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <stdexcept>

using namespace infernux;
namespace py = pybind11;

namespace
{

std::vector<uint32_t> DecodeParticleSpirv(const py::handle &value, const std::string &label)
{
    if (!py::isinstance<py::bytes>(value))
        throw std::invalid_argument(label + " must be SPIR-V bytes");
    const std::string bytes = py::cast<py::bytes>(value);
    if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0)
        throw std::invalid_argument(label + " has an invalid SPIR-V byte size");
    std::vector<uint32_t> words(bytes.size() / sizeof(uint32_t));
    std::memcpy(words.data(), bytes.data(), bytes.size());
    if (words.front() != 0x07230203u)
        throw std::invalid_argument(label + " has an invalid SPIR-V magic number");
    return words;
}

particle::GpuParticleEmitterProgram DecodeGpuParticleProgram(const py::dict &value)
{
    static constexpr std::array<const char *, static_cast<size_t>(particle::GpuKernelStage::Count)> StageNames = {
        "bootstrap", "init", "update", "render_reset", "rendering"};
    for (const char *field :
         {"id", "artifact_revision", "stable_id", "capacity", "state_stride", "stages", "billboard", "outputs"}) {
        if (!value.contains(field))
            throw std::invalid_argument(std::string("GPU particle program is missing ") + field);
    }

    particle::GpuParticleEmitterProgram program;
    program.id = py::cast<uint64_t>(value["id"]);
    program.artifactRevision = py::cast<uint64_t>(value["artifact_revision"]);
    program.stableId = py::cast<std::string>(value["stable_id"]);
    program.capacity = py::cast<uint32_t>(value["capacity"]);
    program.stateStride = py::cast<uint32_t>(value["state_stride"]);

    const py::dict stages = py::cast<py::dict>(value["stages"]);
    for (size_t index = 0; index < StageNames.size(); ++index) {
        const char *stage = StageNames[index];
        if (!stages.contains(stage))
            throw std::invalid_argument(std::string("GPU particle program is missing stage ") + stage);
        program.kernels[index] = DecodeParticleSpirv(stages[stage], std::string("particle stage ") + stage);
    }

    const py::dict billboard = py::cast<py::dict>(value["billboard"]);
    if (!billboard.contains("vertex") || !billboard.contains("fragment"))
        throw std::invalid_argument("GPU particle billboard shaders are incomplete");
    program.billboardVertexShader = DecodeParticleSpirv(billboard["vertex"], "particle billboard vertex shader");
    program.billboardFragmentShader = DecodeParticleSpirv(billboard["fragment"], "particle billboard fragment shader");

    const py::sequence outputs = py::cast<py::sequence>(value["outputs"]);
    program.outputs.reserve(outputs.size());
    for (const py::handle item : outputs) {
        if (!py::isinstance<py::dict>(item))
            throw std::invalid_argument("GPU particle outputs must contain dictionaries");
        const py::dict output = py::reinterpret_borrow<py::dict>(item);
        for (const char *field : {"id", "stable_id", "material"}) {
            if (!output.contains(field))
                throw std::invalid_argument(std::string("GPU particle output is missing ") + field);
        }
        particle::GpuParticleOutputProgram decoded;
        decoded.id = py::cast<uint64_t>(output["id"]);
        decoded.stableId = py::cast<std::string>(output["stable_id"]);
        const py::dict material = py::cast<py::dict>(output["material"]);
        for (const char *field : {"render_queue", "blend_enabled", "depth_test_enabled", "depth_write_enabled"}) {
            if (!material.contains(field))
                throw std::invalid_argument(std::string("GPU particle material is missing ") + field);
        }
        decoded.fallbackMaterial.renderQueue = py::cast<int32_t>(material["render_queue"]);
        decoded.fallbackMaterial.blendEnabled = py::cast<bool>(material["blend_enabled"]);
        decoded.fallbackMaterial.depthTestEnabled = py::cast<bool>(material["depth_test_enabled"]);
        decoded.fallbackMaterial.depthWriteEnabled = py::cast<bool>(material["depth_write_enabled"]);
        if (material.contains("native") && !material["native"].is_none())
            decoded.material = py::cast<std::shared_ptr<InxMaterial>>(material["native"]);
        program.outputs.push_back(std::move(decoded));
    }
    return program;
}

particle::GpuParticleTransforms DecodeGpuParticleTransforms(const py::buffer &value)
{
    const py::buffer_info info = value.request();
    if (info.ndim != 1 || info.shape[0] != 64 || info.itemsize != sizeof(float) ||
        info.format != py::format_descriptor<float>::format() || info.strides[0] != sizeof(float)) {
        throw std::invalid_argument("GPU particle transforms must be a contiguous float32 array shaped (64,)");
    }
    particle::GpuParticleTransforms transforms;
    std::memcpy(&transforms, info.ptr, sizeof(transforms));
    return transforms;
}

} // namespace

namespace infernux
{
void RegisterGUIBindings(py::module_ &m);
void RegisterVector2Bindings(py::module_ &m);
void RegisterVector3Bindings(py::module_ &m);
void RegisterVec4fBindings(py::module_ &m);
void RegisterResourceBindings(py::module_ &m);
void RegisterSceneBindings(py::module_ &m);
void RegisterAssetDatabaseBindings(py::module_ &m);
void RegisterAssetRegistryBindings(py::module_ &m);
void RegisterRhiBindings(py::module_ &m);
void RegisterRenderGraphBindings(py::module_ &m);
void RegisterRenderPipelineBindings(py::module_ &m);
void RegisterCommandBufferBindings(py::module_ &m);
void RegisterTagLayerBindings(py::module_ &m);
void RegisterInputBindings(py::module_ &m);
void RegisterPhysicsBindings(py::module_ &m);
void RegisterAudioBindings(py::module_ &m);
void RegisterBatchBindings(py::module_ &m);
} // namespace infernux

PYBIND11_MODULE(_Infernux, m)
{
    m.doc() = "Python bindings for Infernux";

    // ---- Editor gizmo handle IDs (exposed so Python can identify gizmo picks) ----
    m.attr("GIZMO_X_AXIS_ID") = EditorTools::X_AXIS_ID;
    m.attr("GIZMO_Y_AXIS_ID") = EditorTools::Y_AXIS_ID;
    m.attr("GIZMO_Z_AXIS_ID") = EditorTools::Z_AXIS_ID;
    m.attr("GIZMO_XY_PLANE_ID") = EditorTools::XY_PLANE_ID;
    m.attr("GIZMO_XZ_PLANE_ID") = EditorTools::XZ_PLANE_ID;
    m.attr("GIZMO_YZ_PLANE_ID") = EditorTools::YZ_PLANE_ID;

    py::enum_<LogLevel>(m, "LogLevel")
        .value("Debug", LogLevel::LOG_DEBUG)
        .value("Info", LogLevel::LOG_INFO)
        .value("Warn", LogLevel::LOG_WARN)
        .value("Error", LogLevel::LOG_ERROR)
        .value("Fatal", LogLevel::LOG_FATAL)
        .export_values();

    py::class_<AssetRuntimeRecord>(m, "AssetRuntimeRecord")
        .def_readonly("guid", &AssetRuntimeRecord::guid)
        .def_readonly("resource_type", &AssetRuntimeRecord::type)
        .def_readonly("runtime_type_name", &AssetRuntimeRecord::runtimeTypeName)
        .def_readonly("runtime_version", &AssetRuntimeRecord::runtimeVersion)
        .def_readonly("cpu_resident", &AssetRuntimeRecord::cpuResident)
        .def_readonly("cpu_bytes", &AssetRuntimeRecord::cpuBytes)
        .def_readonly("explicit_cpu_pin_count", &AssetRuntimeRecord::explicitCpuPinCount)
        .def_readonly("external_cpu_reference_count", &AssetRuntimeRecord::externalCpuReferenceCount)
        .def_readonly("cpu_evictable", &AssetRuntimeRecord::cpuEvictable)
        .def_readonly("gpu_resident_bytes", &AssetRuntimeRecord::gpuResidentBytes)
        .def_readonly("gpu_pending_bytes", &AssetRuntimeRecord::gpuPendingBytes)
        .def_readonly("stale_gpu_bytes", &AssetRuntimeRecord::staleGpuBytes)
        .def_readonly("gpu_allocation_count", &AssetRuntimeRecord::gpuAllocationCount)
        .def_readonly("stale_gpu_allocation_count", &AssetRuntimeRecord::staleGpuAllocationCount)
        .def_readonly("gpu_pinned", &AssetRuntimeRecord::gpuPinned)
        .def_readonly("gpu_version_synchronized", &AssetRuntimeRecord::gpuVersionSynchronized);

    // ---- EngineConfig (centralised runtime configuration) ----
    py::class_<EngineConfig>(m, "EngineConfig",
                             "Centralised engine configuration singleton.\n"
                             "Modify values BEFORE the corresponding subsystem initializes.\n"
                             "Access via EngineConfig.get().")
        .def_static("get", &EngineConfig::Get, py::return_value_policy::reference,
                    "Get the singleton EngineConfig instance.")
        // Rendering — Descriptor Pools
        .def_readwrite("max_materials_per_pool", &EngineConfig::maxMaterialsPerPool)
        .def_readwrite("ubo_descriptors_per_material", &EngineConfig::uboDescriptorsPerMaterial)
        .def_readwrite("sampler_descriptors_per_material", &EngineConfig::samplerDescriptorsPerMaterial)
        .def_readwrite("fullscreen_descriptor_sets_per_frame", &EngineConfig::fullscreenDescriptorSetsPerFrame)
        .def_readwrite("fullscreen_sampler_descriptors_per_frame", &EngineConfig::fullscreenSamplerDescriptorsPerFrame)
        // Rendering — Textures
        .def_readwrite("enable_mipmap", &EngineConfig::enableMipmap)
        .def_readwrite("anisotropy_scale", &EngineConfig::anisotropyScale)
        // Rendering — Swapchain
        .def_readwrite("preferred_swapchain_image_count", &EngineConfig::preferredSwapchainImageCount)
        .def_readwrite("max_frames_in_flight", &EngineConfig::maxFramesInFlight)
        // Physics — Jolt Configuration
        .def_readwrite("physics_temp_allocator_size", &EngineConfig::physicsTempAllocatorSize)
        .def_readwrite("physics_max_jobs", &EngineConfig::physicsMaxJobs)
        .def_readwrite("physics_max_barriers", &EngineConfig::physicsMaxBarriers)
        .def_readwrite("physics_max_bodies", &EngineConfig::physicsMaxBodies)
        .def_readwrite("physics_max_body_pairs", &EngineConfig::physicsMaxBodyPairs)
        .def_readwrite("physics_max_contact_constraints", &EngineConfig::physicsMaxContactConstraints)
        .def_readwrite("physics_collision_steps", &EngineConfig::physicsCollisionSteps)
        .def_readwrite("physics_velocity_steps", &EngineConfig::physicsVelocitySteps)
        .def_readwrite("physics_position_steps", &EngineConfig::physicsPositionSteps)
        .def_readwrite("physics_penetration_slop", &EngineConfig::physicsPenetrationSlop)
        .def_readwrite("physics_speculative_contact_distance", &EngineConfig::physicsSpeculativeContactDistance)
        .def_readwrite("physics_linear_cast_max_penetration", &EngineConfig::physicsLinearCastMaxPenetration)
        .def_readwrite("physics_baumgarte", &EngineConfig::physicsBaumgarte)
        .def_readwrite("physics_max_penetration_distance", &EngineConfig::physicsMaxPenetrationDistance)
        .def_readwrite("physics_linear_cast_threshold", &EngineConfig::physicsLinearCastThreshold)
        .def_readwrite("physics_min_velocity_for_restitution", &EngineConfig::physicsMinVelocityForRestitution)
        .def_readwrite("physics_time_before_sleep", &EngineConfig::physicsTimeBeforeSleep)
        .def_readwrite("physics_point_velocity_sleep_threshold", &EngineConfig::physicsPointVelocitySleepThreshold)
        .def_property(
            "physics_gravity", [](EngineConfig &self) { return self.physicsGravity; },
            [](EngineConfig &self, const glm::vec3 &v) { self.physicsGravity = v; },
            "Default gravity vector (applied on physics init)")
        .def_readwrite("physics_max_worker_threads", &EngineConfig::physicsMaxWorkerThreads)
        // Physics — Default Collider Properties
        .def_readwrite("default_collider_friction", &EngineConfig::defaultColliderFriction)
        .def_readwrite("default_collider_bounciness", &EngineConfig::defaultColliderBounciness)
        // Physics — Default Rigidbody Properties
        .def_readwrite("default_rigidbody_mass", &EngineConfig::defaultRigidbodyMass)
        .def_readwrite("default_rigidbody_drag", &EngineConfig::defaultRigidbodyDrag)
        .def_readwrite("default_rigidbody_angular_drag", &EngineConfig::defaultRigidbodyAngularDrag)
        .def_readwrite("default_max_angular_velocity", &EngineConfig::defaultMaxAngularVelocity)
        .def_readwrite("default_max_linear_velocity", &EngineConfig::defaultMaxLinearVelocity)
        // Physics — Layers
        .def_readwrite("physics_layer_count", &EngineConfig::physicsLayerCount)
        .def_readwrite("default_query_layer_mask", &EngineConfig::defaultQueryLayerMask)
        // Render Queue Ranges (read-only from Python; change via code if needed)
        .def_readwrite("opaque_queue_min", &EngineConfig::opaqueQueueMin)
        .def_readwrite("opaque_queue_max", &EngineConfig::opaqueQueueMax)
        .def_readwrite("transparent_queue_min", &EngineConfig::transparentQueueMin)
        .def_readwrite("transparent_queue_max", &EngineConfig::transparentQueueMax)
        .def_readwrite("shadow_caster_queue_min", &EngineConfig::shadowCasterQueueMin)
        .def_readwrite("shadow_caster_queue_max", &EngineConfig::shadowCasterQueueMax)
        .def_readwrite("component_gizmo_queue_min", &EngineConfig::componentGizmoQueueMin)
        .def_readwrite("component_gizmo_queue_max", &EngineConfig::componentGizmoQueueMax)
        .def_readwrite("editor_gizmo_queue_min", &EngineConfig::editorGizmoQueueMin)
        .def_readwrite("editor_gizmo_queue_max", &EngineConfig::editorGizmoQueueMax)
        .def_readwrite("editor_tools_queue_min", &EngineConfig::editorToolsQueueMin)
        .def_readwrite("editor_tools_queue_max", &EngineConfig::editorToolsQueueMax)
        .def_readwrite("skybox_queue", &EngineConfig::skyboxQueue);

    // ---- EditorCamera (property-based camera access) ----
    py::class_<EditorCameraController>(m, "EditorCamera",
                                       "Editor camera controller with property-based access.\n"
                                       "Access via engine.editor_camera.")
        .def_property(
            "fov",
            [](EditorCameraController &self) -> float {
                auto *cam = self.GetCamera();
                return cam ? cam->GetFieldOfView() : 60.0f;
            },
            [](EditorCameraController &self, float v) {
                auto *cam = self.GetCamera();
                if (cam)
                    cam->SetFieldOfView(v);
            },
            "Vertical field of view in degrees")
        .def_property(
            "orthographic",
            [](EditorCameraController &self) -> bool {
                auto *cam = self.GetCamera();
                return cam && cam->GetProjectionMode() == CameraProjection::Orthographic;
            },
            [](EditorCameraController &self, bool value) {
                auto *cam = self.GetCamera();
                if (cam)
                    cam->SetProjectionMode(value ? CameraProjection::Orthographic : CameraProjection::Perspective);
            },
            "Whether the Scene camera uses orthographic projection")
        .def_property(
            "orthographic_size",
            [](EditorCameraController &self) -> float {
                auto *cam = self.GetCamera();
                return cam ? cam->GetOrthographicSize() : 5.0f;
            },
            [](EditorCameraController &self, float value) {
                auto *cam = self.GetCamera();
                if (cam)
                    cam->SetOrthographicSize(value);
            },
            "Orthographic Scene camera half-height")
        .def_property(
            "near_clip",
            [](EditorCameraController &self) -> float {
                auto *cam = self.GetCamera();
                return cam ? cam->GetNearClip() : 0.01f;
            },
            [](EditorCameraController &self, float v) {
                auto *cam = self.GetCamera();
                if (cam)
                    cam->SetNearClip(v);
            },
            "Near clipping distance")
        .def_property(
            "far_clip",
            [](EditorCameraController &self) -> float {
                auto *cam = self.GetCamera();
                return cam ? cam->GetFarClip() : 1000.0f;
            },
            [](EditorCameraController &self, float v) {
                auto *cam = self.GetCamera();
                if (cam)
                    cam->SetFarClip(v);
            },
            "Far clipping distance")
        .def_property_readonly(
            "position",
            [](EditorCameraController &self) -> glm::vec3 {
                auto *cam = self.GetCamera();
                if (cam && cam->GetGameObject()) {
                    return cam->GetGameObject()->GetTransform()->GetPosition();
                }
                return glm::vec3(0.0f);
            },
            "Camera position as Vector3")
        .def_property_readonly(
            "rotation",
            [](EditorCameraController &self) -> py::tuple { return py::make_tuple(self.GetYaw(), self.GetPitch()); },
            "Camera rotation as (yaw, pitch) tuple")
        .def("reset", &EditorCameraController::Reset, "Reset camera to default position and orientation")
        .def(
            "focus_on",
            [](EditorCameraController &self, float x, float y, float z, float distance) {
                self.FocusOn(glm::vec3(x, y, z), distance);
            },
            py::arg("x"), py::arg("y"), py::arg("z"), py::arg("distance") = 10.0f,
            "Focus camera on a world-space point")
        .def_readwrite("rotation_speed", &EditorCameraController::rotationSpeed, "Mouse rotation sensitivity")
        .def_readwrite("pan_speed", &EditorCameraController::panSpeed, "Middle-mouse pan sensitivity")
        .def_readwrite("zoom_speed", &EditorCameraController::zoomSpeed, "Scroll wheel zoom sensitivity")
        .def_readwrite("move_speed", &EditorCameraController::moveSpeed, "WASD movement speed")
        .def_readwrite("move_speed_boost", &EditorCameraController::moveSpeedBoost, "Shift speed multiplier")
        .def_property_readonly(
            "focus_point", [](EditorCameraController &self) -> glm::vec3 { return self.GetFocusPoint(); },
            "Camera focus/orbit point as Vector3")
        .def_property_readonly(
            "focus_distance", [](EditorCameraController &self) -> float { return self.GetFocusDistance(); },
            "Distance from the camera to the focus/orbit point")
        .def(
            "restore_state",
            [](EditorCameraController &self, float pos_x, float pos_y, float pos_z, float focus_x, float focus_y,
               float focus_z, float focus_dist, float yaw, float pitch) {
                self.RestoreState(glm::vec3(pos_x, pos_y, pos_z), glm::vec3(focus_x, focus_y, focus_z), focus_dist, yaw,
                                  pitch);
            },
            py::arg("pos_x"), py::arg("pos_y"), py::arg("pos_z"), py::arg("focus_x"), py::arg("focus_y"),
            py::arg("focus_z"), py::arg("focus_dist"), py::arg("yaw"), py::arg("pitch"),
            "Restore full camera state (position, focus, orientation)")
        .def(
            "world_to_screen_point",
            [](EditorCameraController &self, float x, float y, float z) -> glm::vec2 {
                auto *camera = self.GetCamera();
                if (!camera)
                    return glm::vec2(0.0f);
                return camera->WorldToScreenPoint(glm::vec3(x, y, z));
            },
            py::arg("x"), py::arg("y"), py::arg("z"),
            "Project world position into current Scene View render target coordinates");

    // ========================================================================
    // ScreenUIList enum and InxScreenUIRenderer bindings
    // ========================================================================
    py::enum_<ScreenUIList>(m, "ScreenUIList")
        .value("Camera", ScreenUIList::Camera)
        .value("Overlay", ScreenUIList::Overlay);

    py::class_<InxScreenUIRenderer>(m, "InxScreenUIRenderer")
        .def("begin_frame", &InxScreenUIRenderer::BeginFrame, py::arg("width"), py::arg("height"),
             "Reset draw lists for a new frame")
        .def("begin_frame_cached", &InxScreenUIRenderer::BeginFrameCached, py::arg("width"), py::arg("height"),
             py::arg("content_revision"), "Reuse draw lists when the UI content revision is unchanged")
        .def("add_filled_rect", &InxScreenUIRenderer::AddFilledRect, py::arg("list"), py::arg("min_x"),
             py::arg("min_y"), py::arg("max_x"), py::arg("max_y"), py::arg("r") = 1.0f, py::arg("g") = 1.0f,
             py::arg("b") = 1.0f, py::arg("a") = 1.0f, py::arg("rounding") = 0.0f,
             "Add a filled rectangle to the specified draw list")
        .def("add_image", &InxScreenUIRenderer::AddImage, py::arg("list"), py::arg("texture_id"), py::arg("min_x"),
             py::arg("min_y"), py::arg("max_x"), py::arg("max_y"), py::arg("uv0_x") = 0.0f, py::arg("uv0_y") = 0.0f,
             py::arg("uv1_x") = 1.0f, py::arg("uv1_y") = 1.0f, py::arg("r") = 1.0f, py::arg("g") = 1.0f,
             py::arg("b") = 1.0f, py::arg("a") = 1.0f, py::arg("rotation") = 0.0f, py::arg("mirror_h") = false,
             py::arg("mirror_v") = false, py::arg("rounding") = 0.0f,
             "Add a textured image quad to the specified draw list with optional rotation, mirroring and rounding")
        .def("add_text", &InxScreenUIRenderer::AddText, py::arg("list"), py::arg("min_x"), py::arg("min_y"),
             py::arg("max_x"), py::arg("max_y"), py::arg("text"), py::arg("r") = 1.0f, py::arg("g") = 1.0f,
             py::arg("b") = 1.0f, py::arg("a") = 1.0f, py::arg("align_x") = 0.5f, py::arg("align_y") = 0.5f,
             py::arg("font_size") = 0.0f, py::arg("wrap_width") = 0.0f, py::arg("rotation") = 0.0f,
             py::arg("mirror_h") = false, py::arg("mirror_v") = false, py::arg("font_path") = std::string(),
             py::arg("line_height") = 1.0f, py::arg("letter_spacing") = 0.0f,
             "Add text within a bounding box to the specified draw list with optional rotation and mirroring")
        .def(
            "measure_text",
            [](const InxScreenUIRenderer &renderer, const std::string &text, float font_size, float wrap_width,
               const std::string &font_path, float line_height, float letter_spacing) -> py::tuple {
                auto [w, h] = renderer.MeasureText(text, font_size, wrap_width, font_path, line_height, letter_spacing);
                return py::make_tuple(py::float_(w), py::float_(h));
            },
            py::arg("text"), py::arg("font_size") = 0.0f, py::arg("wrap_width") = 0.0f,
            py::arg("font_path") = std::string(), py::arg("line_height") = 1.0f, py::arg("letter_spacing") = 0.0f,
            "Measure text size using the active UI font. Returns (width, height).")
        .def("has_commands", &InxScreenUIRenderer::HasCommands, py::arg("list"),
             "Check if the specified draw list has any draw commands")
        .def("set_enabled", &InxScreenUIRenderer::SetEnabled, py::arg("enabled"),
             "Enable or disable rendering (commands still accumulate)")
        .def("is_enabled", &InxScreenUIRenderer::IsEnabled, "Check if rendering is enabled");

    py::enum_<RuntimeMode>(m, "RuntimeMode")
        .value("Graphical", RuntimeMode::Graphical)
        .value("Headless", RuntimeMode::Headless)
        .export_values();

    py::enum_<vk::ImageReadbackStatus>(m, "ImageReadbackStatus")
        .value("Pending", vk::ImageReadbackStatus::Pending)
        .value("Completed", vk::ImageReadbackStatus::Completed)
        .value("Failed", vk::ImageReadbackStatus::Failed)
        .value("Cancelled", vk::ImageReadbackStatus::Cancelled);

    py::class_<vk::ImageReadbackTicket, std::shared_ptr<vk::ImageReadbackTicket>>(m, "ImageReadbackTicket")
        .def_property_readonly("status", &vk::ImageReadbackTicket::GetStatus)
        .def_property_readonly("done", &vk::ImageReadbackTicket::IsDone)
        .def_property_readonly("width", &vk::ImageReadbackTicket::GetWidth)
        .def_property_readonly("height", &vk::ImageReadbackTicket::GetHeight)
        .def_property_readonly("channel_count", &vk::ImageReadbackTicket::GetChannelCount)
        .def_property_readonly("element_type", &vk::ImageReadbackTicket::GetElementType)
        .def_property_readonly("byte_size", &vk::ImageReadbackTicket::GetByteSize)
        .def_property_readonly("error", &vk::ImageReadbackTicket::GetError)
        .def("cancel", &vk::ImageReadbackTicket::Cancel)
        .def("result_bytes",
             [](const vk::ImageReadbackTicket &ticket) {
                 const auto &data = ticket.GetData();
                 return py::bytes(reinterpret_cast<const char *>(data.data()), data.size());
             })
        .def("result_numpy", [](const std::shared_ptr<vk::ImageReadbackTicket> &ticket) {
            const auto &data = ticket->GetData();
            const py::ssize_t channels = static_cast<py::ssize_t>(ticket->GetChannelCount());
            const py::ssize_t width = static_cast<py::ssize_t>(ticket->GetWidth());
            const py::ssize_t height = static_cast<py::ssize_t>(ticket->GetHeight());
            const py::ssize_t elementBytes =
                static_cast<py::ssize_t>(ticket->GetByteSize()) / (height * width * channels);
            const std::vector<py::ssize_t> shape{height, width, channels};
            const std::vector<py::ssize_t> strides{width * channels * elementBytes, channels * elementBytes,
                                                   elementBytes};
            py::array result(py::dtype(ticket->GetElementType()), shape, strides, data.data(), py::cast(ticket));
            result.attr("setflags")(false);
            return result;
        });

    py::class_<Infernux>(m, "Infernux")
        .def(py::init<std::string, RuntimeMode>(), py::arg("dll_path"), py::arg("mode") = RuntimeMode::Graphical)
        .def("init_renderer", &Infernux::InitRenderer, py::arg("width"), py::arg("height"), py::arg("project_path"),
             py::arg("builtin_resource_path") = std::string())
        .def("init_headless", &Infernux::InitHeadless, py::arg("project_path"),
             py::arg("builtin_resource_path") = std::string(),
             "Initialize scene, physics, assets, and workers without SDL/Vulkan/ImGui/audio")
        .def("tick", &Infernux::Tick, py::arg("delta_time"), "Advance one deterministic headless frame")
        .def_property_readonly("exit_requested", &Infernux::IsExitRequested, "Whether shutdown has been requested")
        .def_property_readonly("runtime_mode", &Infernux::GetRuntimeMode)
        .def_property_readonly("has_renderer", [](const Infernux &self) { return self.GetRenderer() != nullptr; })
        .def_property_readonly("pending_mesh_gpu_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetPendingMeshUploadCount() : size_t{0};
                               })
        .def_property_readonly("submitted_mesh_gpu_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetSubmittedMeshUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("completed_mesh_gpu_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetCompletedMeshUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("async_mesh_gpu_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetAsyncMeshUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("pending_texture_cpu_load_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetPendingTextureCpuLoadCount() : size_t{0};
                               })
        .def_property_readonly("pending_texture_gpu_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetPendingTextureUploadCount() : size_t{0};
                               })
        .def_property_readonly("submitted_texture_gpu_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetSubmittedTextureUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("completed_texture_gpu_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetCompletedTextureUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("async_texture_gpu_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetAsyncTextureUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("staging_pool_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetStagingPoolBytes() : uint64_t{0};
                               })
        .def_property_readonly("staging_pool_buffer_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetStagingPoolBufferCount() : size_t{0};
                               })
        .def_property_readonly("staging_buffer_allocation_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetStagingAllocationCount() : uint64_t{0};
                               })
        .def_property_readonly("staging_buffer_reuse_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetStagingReuseCount() : uint64_t{0};
                               })
        .def_property_readonly("staging_buffer_discard_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetStagingDiscardCount() : uint64_t{0};
                               })
        .def_property_readonly("pending_imgui_texture_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetPendingImGuiTextureUploadCount() : size_t{0};
                               })
        .def_property_readonly("pending_imgui_texture_upload_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetPendingImGuiTextureUploadBytes() : uint64_t{0};
                               })
        .def_property_readonly("submitted_imgui_texture_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetSubmittedImGuiTextureUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("completed_imgui_texture_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetCompletedImGuiTextureUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("async_imgui_texture_upload_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetAsyncImGuiTextureUploadCount() : uint64_t{0};
                               })
        .def_property_readonly("imgui_texture_resident_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetImGuiTextureResidentBytes() : uint64_t{0};
                               })
        .def_property_readonly("imgui_texture_budget_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetImGuiTextureBudgetBytes() : uint64_t{0};
                               })
        .def_property_readonly("imgui_texture_entry_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetImGuiTextureEntryCount() : size_t{0};
                               })
        .def_property_readonly("imgui_texture_eviction_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetImGuiTextureEvictionCount() : uint64_t{0};
                               })
        .def(
            "set_imgui_texture_budget_bytes",
            [](Infernux &self, uint64_t bytes) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Cannot set the ImGui texture budget without an initialized renderer");
                renderer->SetImGuiTextureBudgetBytes(bytes);
            },
            py::arg("bytes"))
        .def(
            "trim_imgui_texture_budget",
            [](Infernux &self) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Cannot trim the ImGui texture budget without an initialized renderer");
                return renderer->TrimImGuiTextureBudget();
            })
        .def_property_readonly("texture_gpu_resident_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetTextureGpuResidentBytes() : uint64_t{0};
                               })
        .def_property_readonly("texture_gpu_budget_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetTextureGpuBudgetBytes() : uint64_t{0};
                               })
        .def_property_readonly("texture_gpu_cache_entry_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetTextureGpuCacheEntryCount() : size_t{0};
                               })
        .def_property_readonly("retired_texture_gpu_lease_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetRetiredTextureGpuLeaseCount() : size_t{0};
                               })
        .def_property_readonly("texture_gpu_eviction_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetTextureGpuEvictionCount() : uint64_t{0};
                               })
        .def(
            "set_texture_gpu_budget_bytes",
            [](Infernux &self, uint64_t bytes) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Cannot set the GPU texture budget without an initialized renderer");
                renderer->SetTextureGpuBudgetBytes(bytes);
            },
            py::arg("bytes"))
        .def(
            "trim_texture_gpu_budget",
            [](Infernux &self) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Cannot trim the GPU texture budget without an initialized renderer");
                return renderer->TrimTextureGpuBudget();
            })
        .def_property_readonly("mesh_gpu_resident_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetMeshGpuResidentBytes() : uint64_t{0};
                               })
        .def_property_readonly("mesh_gpu_budget_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetMeshGpuBudgetBytes() : uint64_t{0};
                               })
        .def_property_readonly("mesh_gpu_cache_entry_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetMeshGpuCacheEntryCount() : size_t{0};
                               })
        .def_property_readonly("retired_mesh_gpu_lease_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetRetiredMeshGpuLeaseCount() : size_t{0};
                               })
        .def_property_readonly("mesh_gpu_eviction_count",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetMeshGpuEvictionCount() : uint64_t{0};
                               })
        .def(
            "set_mesh_gpu_budget_bytes",
            [](Infernux &self, uint64_t bytes) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Cannot set the GPU mesh budget without an initialized renderer");
                renderer->SetMeshGpuBudgetBytes(bytes);
            },
            py::arg("bytes"))
        .def(
            "trim_mesh_gpu_budget",
            [](Infernux &self) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Cannot trim the GPU mesh budget without an initialized renderer");
                return renderer->TrimMeshGpuBudget();
            })
        .def_property_readonly("gpu_residency_snapshot",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   const GpuResidencySnapshot snapshot =
                                       renderer ? renderer->GetGpuResidencySnapshot() : GpuResidencySnapshot{};
                                   py::dict result;
                                   result["budget_bytes"] = snapshot.budgetBytes;
                                   result["allocator_allocation_bytes"] = snapshot.allocatorAllocationBytes;
                                   result["allocator_block_bytes"] = snapshot.allocatorBlockBytes;
                                   result["allocator_allocation_count"] = snapshot.allocatorAllocationCount;
                                   result["device_local_allocation_bytes"] = snapshot.deviceLocalAllocationBytes;
                                   result["device_local_usage_bytes"] = snapshot.deviceLocalUsageBytes;
                                   result["device_local_budget_bytes"] = snapshot.deviceLocalBudgetBytes;
                                    result["mesh_bytes"] = snapshot.meshBytes;
                                    result["particle_bytes"] = snapshot.particleBytes;
                                   result["texture_bytes"] = snapshot.textureBytes;
                                   result["imgui_texture_bytes"] = snapshot.imguiTextureBytes;
                                   result["pending_imgui_texture_bytes"] = snapshot.pendingImguiTextureBytes;
                                    result["staging_pool_bytes"] = snapshot.stagingPoolBytes;
                                    result["pending_readback_bytes"] = snapshot.pendingReadbackBytes;
                                    result["pending_readback_count"] = snapshot.pendingReadbackCount;
                                    result["pending_gpu_transfer_count"] = snapshot.pendingGpuTransferCount;
                                    result["upload_timeline_enabled"] = snapshot.uploadTimelineEnabled;
                                    result["timeline_upload_publication_count"] =
                                        snapshot.timelineUploadPublicationCount;
                                    result["required_upload_timeline_value"] = snapshot.requiredUploadTimelineValue;
                                    result["device_wait_idle_count"] = snapshot.deviceWaitIdleCount;
                                    result["shader_hot_reload_retirement_count"] =
                                        snapshot.shaderHotReloadRetirementCount;
                                    result["pending_async_graphics_submission_count"] =
                                        snapshot.pendingAsyncGraphicsSubmissionCount;
                                    result["async_graphics_submission_count"] = snapshot.asyncGraphicsSubmissionCount;
                                   result["render_target_bytes"] = snapshot.renderTargetBytes;
                                   result["render_graph_bytes"] = snapshot.renderGraphBytes;
                                   result["transient_pool_bytes"] = snapshot.transientPoolBytes;
                                   result["material_ubo_bytes"] = snapshot.materialUboBytes;
                                   result["material_render_data_count"] = snapshot.materialRenderDataCount;
                                   result["runtime_material_count"] = snapshot.runtimeMaterialCount;
                                   result["asset_material_count"] = snapshot.assetMaterialCount;
                                   result["material_descriptor_set_count"] = snapshot.materialDescriptorSetCount;
                                   result["retired_material_descriptor_set_count"] =
                                       snapshot.retiredMaterialDescriptorSetCount;
                                   result["material_descriptor_pool_count"] = snapshot.materialDescriptorPoolCount;
                                   result["material_pipeline_count"] = snapshot.materialPipelineCount;
                                   result["shadow_material_descriptor_set_count"] =
                                       snapshot.shadowMaterialDescriptorSetCount;
                                   result["shadow_material_descriptor_pool_count"] =
                                       snapshot.shadowMaterialDescriptorPoolCount;
                                   result["shadow_material_binding_cache_hits"] =
                                       snapshot.shadowMaterialBindingCacheHits;
                                   result["shadow_material_binding_cache_misses"] =
                                       snapshot.shadowMaterialBindingCacheMisses;
                                   result["shadow_material_binding_retirements"] =
                                       snapshot.shadowMaterialBindingRetirements;
                                   result["runtime_mesh_entry_count"] = snapshot.runtimeMeshEntryCount;
                                   result["runtime_mesh_bytes"] = snapshot.runtimeMeshBytes;
                                   result["scheduled_release_bytes"] = snapshot.scheduledReleaseBytes;
                                   result["tracked_bytes"] = snapshot.trackedBytes;
                                   result["unclassified_bytes"] = snapshot.unclassifiedBytes;
                                   result["effective_allocation_bytes"] = snapshot.effectiveAllocationBytes;
                                   result["over_budget_bytes"] = snapshot.overBudgetBytes;
                                   return result;
                               })
        .def_property_readonly("renderer_frame_snapshot",
                               [](Infernux &self) {
                                   auto *renderer = self.GetRenderer();
                                   const RendererFrameTelemetrySnapshot snapshot =
                                       renderer ? renderer->GetFrameTelemetrySnapshot() : RendererFrameTelemetrySnapshot{};
                                   py::dict result;
                                   result["frame"] = snapshot.frame;
                                   result["scene_view_visible"] = snapshot.sceneViewVisible;
                                   result["scene_target_ready"] = snapshot.sceneTargetReady;
                                   result["game_camera_enabled"] = snapshot.gameCameraEnabled;
                                   result["game_camera_available"] = snapshot.gameCameraAvailable;
                                   result["game_target_ready"] = snapshot.gameTargetReady;
                                   result["scene_target_width"] = snapshot.sceneTargetWidth;
                                   result["scene_target_height"] = snapshot.sceneTargetHeight;
                                   result["game_target_width"] = snapshot.gameTargetWidth;
                                   result["game_target_height"] = snapshot.gameTargetHeight;
                                   result["scene_draw_call_count"] = snapshot.sceneDrawCallCount;
                                   result["scene_shadow_draw_call_count"] = snapshot.sceneShadowDrawCallCount;
                                   result["game_draw_call_count"] = snapshot.gameDrawCallCount;
                                   result["game_shadow_draw_call_count"] = snapshot.gameShadowDrawCallCount;
                                   result["light_count"] = snapshot.lightCount;
                                   result["particle_count"] = snapshot.particleCount;
                                   result["scene_render_graph_name"] = snapshot.sceneRenderGraphName;
                                   result["game_render_graph_name"] = snapshot.gameRenderGraphName;
                                   result["scene_render_graph_execution_count"] =
                                       snapshot.sceneRenderGraphExecutionCount;
                                   result["game_render_graph_execution_count"] =
                                       snapshot.gameRenderGraphExecutionCount;
                                   result["scene_render_graph_current_executed"] =
                                       snapshot.sceneRenderGraphCurrentExecuted;
                                   result["game_render_graph_current_executed"] =
                                       snapshot.gameRenderGraphCurrentExecuted;
                                   result["scene_render_graph_pass_names"] = snapshot.sceneRenderGraphPassNames;
                                   result["game_render_graph_pass_names"] = snapshot.gameRenderGraphPassNames;
                                   result["game_render_ms"] = snapshot.gameRenderMs;
                                   result["game_only_frame_ms"] = snapshot.gameOnlyFrameMs;
                                   result["scene_update_ms"] = snapshot.sceneUpdateMs;
                                   result["gui_build_ms"] = snapshot.guiBuildMs;
                                   result["prepare_frame_ms"] = snapshot.prepareFrameMs;
                                   result["ui_panel_times_ms"] = snapshot.guiPanelTimesMs;
                                   result["ui_panel_sub_times_ms"] = snapshot.guiPanelSubTimesMs;
                                   return result;
                               })
        .def_property_readonly("renderer_ui_performance_snapshot",
                               [](Infernux &self) {
                                   auto *renderer = self.GetRenderer();
                                   const RendererUIPerformanceSnapshot snapshot =
                                       renderer ? renderer->GetUIPerformanceSnapshot()
                                                : RendererUIPerformanceSnapshot{};
                                   const auto encodeStats = [](const UIPerformanceMetricStats &stats) {
                                       py::dict result;
                                       result["sample_count"] = stats.sampleCount;
                                       result["mean_ms"] = stats.meanMs;
                                       result["median_ms"] = stats.medianMs;
                                       result["p95_ms"] = stats.p95Ms;
                                       result["max_ms"] = stats.maxMs;
                                       return result;
                                   };
                                   py::dict panelTimes;
                                   for (const auto &[name, stats] : snapshot.panelTimes)
                                       panelTimes[py::str(name)] = encodeStats(stats);
                                   py::dict panelSubTimes;
                                   for (const auto &[panelName, stages] : snapshot.panelSubTimes) {
                                       py::dict encodedStages;
                                       for (const auto &[stageName, stats] : stages)
                                           encodedStages[py::str(stageName)] = encodeStats(stats);
                                       panelSubTimes[py::str(panelName)] = std::move(encodedStages);
                                   }
                                   py::dict result;
                                   result["first_frame"] = snapshot.firstFrame;
                                   result["last_frame"] = snapshot.lastFrame;
                                   result["sample_count"] = snapshot.sampleCount;
                                   result["gui_build"] = encodeStats(snapshot.guiBuild);
                                   result["panel_times"] = std::move(panelTimes);
                                   result["panel_sub_times"] = std::move(panelSubTimes);
                                   return result;
                               })
        .def_property_readonly("asset_runtime_records", &Infernux::GetAssetRuntimeRecords)
        .def_property_readonly("gpu_residency_budget_bytes",
                               [](const Infernux &self) {
                                   const auto *renderer = self.GetRenderer();
                                   return renderer ? renderer->GetGpuResidencyBudgetBytes() : uint64_t{0};
                               })
        .def(
            "set_gpu_residency_budget_bytes",
            [](Infernux &self, uint64_t bytes) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Cannot set GPU residency budget without an initialized renderer");
                renderer->SetGpuResidencyBudgetBytes(bytes);
            },
            py::arg("bytes"))
        .def(
            "trim_gpu_residency_budget",
            [](Infernux &self) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Cannot trim GPU residency without an initialized renderer");
                return renderer->TrimGpuResidencyBudget();
            })
        .def(
            "set_gui_font",
            [](Infernux &self, const std::string &fontPath, float fontSize) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetGUIFont(fontPath.c_str(), fontSize);
            },
            py::arg("font_path"), py::arg("font_size"))
        .def(
            "get_display_scale",
            [](Infernux &self) -> float {
                auto *r = self.GetRenderer();
                return r ? r->GetDisplayScale() : 1.0f;
            },
            "Get the OS display scale factor (e.g. 2.0 for 200%% scaling)")
        .def(
            "run",
            [](Infernux &self) {
                // The native frame loop can run for the lifetime of the Editor.
                // Release the GIL so loopback MCP and other Python worker threads
                // can make progress between Python callback boundaries.
                py::gil_scoped_release release;
                self.Run();
            })
        .def(
            "set_pre_scene_update_callback",
            [](Infernux &self, py::object callback) {
                if (callback.is_none()) {
                    self.SetPreSceneUpdateCallback(nullptr);
                } else {
                    py::function fn = py::cast<py::function>(callback);
                    self.SetPreSceneUpdateCallback([fn](float deltaTime) {
                        py::gil_scoped_acquire acquire;
                        try {
                            fn(deltaTime);
                        } catch (py::error_already_set &e) {
                            e.restore();
                        }
                    });
                }
            },
            py::arg("callback"),
            "Set a Python callback invoked before scene Update in graphical and headless modes.")
        .def(
            "set_pre_gui_callback",
            [](Infernux &self, py::object callback) {
                auto *r = self.GetRenderer();
                if (!r)
                    return;
                if (callback.is_none()) {
                    r->SetPreGuiCallback(nullptr);
                } else {
                    py::function fn = py::cast<py::function>(callback);
                    r->SetPreGuiCallback([fn]() {
                        py::gil_scoped_acquire acquire;
                        try {
                            fn();
                        } catch (py::error_already_set &e) {
                            e.restore();
                        }
                    });
                }
            },
            py::arg("callback"),
            "Set a Python callback invoked each frame before GUI rendering.\n"
            "Used for DeferredTaskRunner to ensure scene mutations finish before panels render.")
        .def(
            "set_post_draw_callback",
            [](Infernux &self, py::object callback) {
                auto *r = self.GetRenderer();
                if (!r)
                    return;
                if (callback.is_none()) {
                    r->SetPostDrawCallback(nullptr);
                } else {
                    py::function fn = py::cast<py::function>(callback);
                    r->SetPostDrawCallback([fn]() {
                        py::gil_scoped_acquire acquire;
                        try {
                            fn();
                        } catch (py::error_already_set &e) {
                            e.restore();
                        }
                    });
                }
            },
            py::arg("callback"),
            "Set a Python callback invoked each frame after GPU submit + present.\n"
            "Heavy scene loads run here, sandwiched by SDL_PumpEvents to prevent\n"
            "Windows from flagging the application as Not Responding.")
        .def(
            "pump_events", [](Infernux & /*self*/) { SDL_PumpEvents(); },
            "Pump the OS message queue to prevent Windows Not Responding during long operations")
        .def("queue_synthetic_key_input", &Infernux::QueueSyntheticKeyInput, py::arg("scancode"),
             py::arg("pressed"), py::arg("repeat") = false,
             "Queue a synthetic key event for the next graphical input frame")
        .def("queue_synthetic_mouse_button_input", &Infernux::QueueSyntheticMouseButtonInput, py::arg("button"),
             py::arg("pressed"), py::arg("x"), py::arg("y"),
             "Queue a synthetic mouse-button event for the next graphical input frame")
        .def("queue_synthetic_mouse_motion_input", &Infernux::QueueSyntheticMouseMotionInput, py::arg("x"),
             py::arg("y"), py::arg("delta_x"), py::arg("delta_y"),
             "Queue a synthetic mouse-motion event for the next graphical input frame")
        .def("queue_synthetic_mouse_wheel_input", &Infernux::QueueSyntheticMouseWheelInput, py::arg("horizontal"),
             py::arg("vertical"), "Queue a synthetic mouse-wheel event for the next graphical input frame")
        .def("queue_synthetic_text_input", &Infernux::QueueSyntheticTextInput, py::arg("text"),
             "Queue UTF-8 text input for the next graphical input frame")
        .def("queue_synthetic_close_request", &Infernux::QueueSyntheticCloseRequest,
             "Queue a window-close request through the graphical event loop")
        .def_property_readonly("last_processed_synthetic_input_sequence",
                               &Infernux::GetLastProcessedSyntheticInputSequence,
                               "Sequence of the last synthetic input event consumed by the graphical event loop")
        .def_property_readonly("pending_synthetic_input_count", &Infernux::GetPendingSyntheticInputCount,
                               "Number of queued synthetic input events")
        .def("set_log_level", &Infernux::SetLogLevel)
        .def(
            "register_gui_renderable",
            [](Infernux &self, const std::string &name, std::shared_ptr<InxGUIRenderable> renderable) {
                auto *r = self.GetRenderer();
                if (r)
                    r->RegisterGUIRenderable(name.c_str(), renderable);
            },
            py::arg("name"), py::arg("renderable"))
        .def(
            "unregister_gui_renderable",
            [](Infernux &self, const std::string &name) {
                auto *r = self.GetRenderer();
                if (r)
                    r->UnregisterGUIRenderable(name.c_str());
            },
            py::arg("name"))
        .def("select_docked_window", &Infernux::SelectDockedWindow,
             "Select and focus a docked ImGui window by its stable window_id", py::arg("window_id"))
        .def("reset_imgui_layout", &Infernux::ResetImGuiLayout, "Clear ImGui docking layout and delete saved ini")
        .def("exit", &Infernux::Exit, "Exit the Infernux application")
        .def("cleanup", &Infernux::Cleanup, "Destroy renderer and release all GPU resources")
        .def(
            "is_close_requested",
            [](Infernux &self) -> bool {
                auto *r = self.GetRenderer();
                return r && r->IsCloseRequested();
            },
            "True when the user clicked the window close button but Python has not yet confirmed")
        .def(
            "confirm_close",
            [](Infernux &self) {
                auto *r = self.GetRenderer();
                if (r)
                    r->ConfirmClose();
            },
            "Actually close the engine (call after save dialogs are handled)")
        .def(
            "cancel_close",
            [](Infernux &self) {
                auto *r = self.GetRenderer();
                if (r)
                    r->CancelClose();
            },
            "Cancel a pending close request (user chose Cancel in save dialog)")
        .def(
            "show",
            [](Infernux &self) {
                auto *r = self.GetRenderer();
                if (r)
                    r->ShowWindow();
            },
            "Show the Infernux window")
        .def(
            "hide",
            [](Infernux &self) {
                auto *r = self.GetRenderer();
                if (r)
                    r->HideWindow();
            },
            "Hide the Infernux window")
        .def(
            "is_window_minimized",
            [](Infernux &self) -> bool {
                auto *r = self.GetRenderer();
                return r && r->IsWindowMinimized();
            },
            "Return whether the Infernux window is currently minimized or occluded")
        .def(
            "set_window_icon",
            [](Infernux &self, const std::string &iconPath) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetWindowIcon(iconPath);
            },
            py::arg("icon_path"), "Set the window icon from a PNG file")
        .def(
            "set_fullscreen",
            [](Infernux &self, bool fullscreen) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetWindowFullscreen(fullscreen);
            },
            py::arg("fullscreen"), "Set the window to fullscreen or windowed mode")
        .def(
            "set_window_title",
            [](Infernux &self, const std::string &title) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetWindowTitle(title);
            },
            py::arg("title"), "Set the window title bar text")
        .def(
            "set_maximized",
            [](Infernux &self, bool maximized) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetWindowMaximized(maximized);
            },
            py::arg("maximized"), "Maximize or restore the window")
        .def(
            "set_resizable",
            [](Infernux &self, bool resizable) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetWindowResizable(resizable);
            },
            py::arg("resizable"), "Set whether the window is resizable")
        .def("reload_shader_runtime", &Infernux::ReloadShaderRuntime, py::arg("shader_path"),
             py::arg("previous_shader_id"),
             "Compile an already-imported shader and refresh renderer state. Returns empty string on success.")
        .def("reload_texture", &Infernux::ReloadTexture, py::arg("texture_path"),
             "Invalidate cached texture and force materials to reload it")
        .def("reload_mesh", &Infernux::ReloadMesh, py::arg("mesh_path"),
             "Reload a mesh asset and notify dependent MeshRenderers")
        .def("reload_audio", &Infernux::ReloadAudio, py::arg("audio_path"),
             "Reload an audio clip asset and notify dependents")
        .def("get_asset_database", &Infernux::GetAssetDatabase, py::return_value_policy::reference,
             "Get the asset database instance")
        .def(
            "submit_imgui_texture",
            [](Infernux &self, const std::string &name, const py::buffer &pixels, int width, int height, bool nearest,
               bool pinned) -> uint64_t {
                auto *r = self.GetRenderer();
                if (!r)
                    throw std::logic_error("Cannot submit an ImGui texture without an initialized renderer");
                const py::buffer_info info = pixels.request();
                if (info.ndim != 1 || info.itemsize != 1 || info.strides[0] != 1)
                    throw std::invalid_argument("ImGui pixels must be a contiguous one-dimensional byte buffer");
                VkFilter f = nearest ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
                py::gil_scoped_release release;
                return r->SubmitTextureForImGui(name, static_cast<const unsigned char *>(info.ptr), info.size, width,
                                                height, f, pinned);
            },
            py::arg("name"), py::arg("pixels"), py::arg("width"), py::arg("height"), py::arg("nearest") = false,
            py::arg("pinned") = false,
            "Submit RGBA8 data for ImGui display and return its monotonic upload version")
        .def(
            "remove_imgui_texture",
            [](Infernux &self, const std::string &name) {
                auto *r = self.GetRenderer();
                if (r)
                    r->RemoveImGuiTexture(name);
            },
            py::arg("name"), "Remove a previously uploaded ImGui texture")
        .def(
            "has_imgui_texture",
            [](Infernux &self, const std::string &name) -> bool {
                auto *r = self.GetRenderer();
                return r && r->HasImGuiTexture(name);
            },
            py::arg("name"), "Check if an ImGui texture with the given name exists")
        .def(
            "get_imgui_texture_id",
            [](Infernux &self, const std::string &name) -> uint64_t {
                auto *r = self.GetRenderer();
                return r ? r->GetImGuiTextureId(name) : 0;
            },
            py::arg("name"), "Get texture ID for an already uploaded texture")
        .def(
            "get_imgui_texture_version",
            [](Infernux &self, const std::string &name) -> uint64_t {
                auto *r = self.GetRenderer();
                return r ? r->GetImGuiTextureVersion(name) : 0;
            },
            py::arg("name"), "Get the published upload version for an ImGui texture")
        .def(
            "get_failed_imgui_texture_version",
            [](Infernux &self, const std::string &name) -> uint64_t {
                auto *r = self.GetRenderer();
                return r ? r->GetFailedImGuiTextureVersion(name) : 0;
            },
            py::arg("name"), "Get the latest failed upload version for an ImGui texture")
        .def(
            "get_resource_preview_manager",
            [](Infernux &self) -> ResourcePreviewManager * {
                auto *r = self.GetRenderer();
                return r ? r->GetResourcePreviewManager() : nullptr;
            },
            py::return_value_policy::reference, "Get the resource preview manager for file previews")
        .def("query_or_schedule_material_preview", &Infernux::QueryOrScheduleMaterialPreview, py::arg("resource_key"),
             py::arg("mat_file_path"), py::arg("material_json") = "", py::arg("file_mtime_hint") = 0,
             py::call_guard<py::gil_scoped_release>(),
             "Combined query + schedule for material preview. Returns ImGui texture id.")
        .def("query_or_schedule_mesh_preview", &Infernux::QueryOrScheduleMeshPreview, py::arg("resource_key"),
             py::arg("mesh_file_path"), py::arg("file_mtime_hint") = 0, py::call_guard<py::gil_scoped_release>(),
             "Combined query + schedule for mesh/model preview. Returns ImGui texture id.")
        .def("pump_preview_tasks", &Infernux::PumpPreviewTasks, py::call_guard<py::gil_scoped_release>(),
             "Pump completed preview work and submit texture upload tickets")
        .def("get_material_preview_texture_id", &Infernux::GetMaterialPreviewTextureId, py::arg("resource_key"),
             py::call_guard<py::gil_scoped_release>(),
             "Get texture id for material preview (stale-return for anti-flicker)")
        .def("is_material_preview_ready", &Infernux::IsMaterialPreviewReady, py::arg("resource_key"),
             py::call_guard<py::gil_scoped_release>(),
             "Check whether a material preview matches its latest requested generation")
        .def_property_readonly(
            "preview_task_snapshots",
            [](const Infernux &self) {
                py::list result;
                for (const auto &snapshot : self.GetPreviewTaskSnapshots()) {
                    py::dict item;
                    item["kind"] = snapshot.kind;
                    item["resource_key"] = snapshot.resourceKey;
                    item["texture_name"] = snapshot.textureName;
                    item["generation"] = snapshot.generation;
                    item["ready_generation"] = snapshot.readyGeneration;
                    item["pending_upload_version"] = snapshot.pendingUploadVersion;
                    item["pending_preview_generation"] = snapshot.pendingPreviewGeneration;
                    item["published_upload_version"] = snapshot.publishedUploadVersion;
                    item["failed_upload_version"] = snapshot.failedUploadVersion;
                    item["texture_id"] = snapshot.textureId;
                    item["in_flight"] = snapshot.inFlight;
                    item["has_render_ticket"] = snapshot.hasRenderTicket;
                    item["render_ticket_done"] = snapshot.renderTicketDone;
                    item["pending_width"] = snapshot.pendingWidth;
                    item["pending_height"] = snapshot.pendingHeight;
                    item["ready_width"] = snapshot.readyWidth;
                    item["ready_height"] = snapshot.readyHeight;
                    item["pixel_generation"] = snapshot.pixelGeneration;
                    item["pixel_hash"] = snapshot.pixelHash;
                    item["non_transparent_pixel_count"] = snapshot.nonTransparentPixelCount;
                    item["min_rgb"] = snapshot.minRgb;
                    item["max_rgb"] = snapshot.maxRgb;
                    item["imgui_draw_command_count"] = snapshot.imguiDrawCommandCount;
                    result.append(std::move(item));
                }
                return result;
            },
            "Read-only diagnostics for active material, texture, and mesh preview tasks")
        .def("render_timeline_cube_preview", &Infernux::RenderTimelineCubePreview, py::arg("px"), py::arg("py"),
             py::arg("pz"), py::arg("rx"), py::arg("ry"), py::arg("rz"), py::arg("sx"), py::arg("sy"), py::arg("sz"),
             py::arg("cam_yaw"), py::arg("cam_pitch"), py::arg("cam_distance"), py::arg("size") = 192,
             py::call_guard<py::gil_scoped_release>(),
             "Render a cube on a grid floor with the given transform (rotation in degrees), viewed by an orbit "
             "camera (yaw/pitch radians, distance=zoom), using the real engine renderer; returns an ImGui texture id "
             "(cached + throttled).")
        .def("get_texture_preview_texture_id", &Infernux::GetTexturePreviewTextureId, py::arg("resource_key"),
             py::call_guard<py::gil_scoped_release>(),
             "Get texture id for texture preview (stale-return for anti-flicker)")
        .def("get_texture_preview_size", &Infernux::GetTexturePreviewSize, py::arg("resource_key"),
             py::call_guard<py::gil_scoped_release>(), "Get texture preview dimensions (stale-return for anti-flicker)")
        .def("invalidate_material_preview_task", &Infernux::InvalidateMaterialPreviewTask, py::arg("resource_key"),
             py::call_guard<py::gil_scoped_release>(), "Invalidate one material preview task/cache entry")
        .def("invalidate_texture_preview_task", &Infernux::InvalidateTexturePreviewTask, py::arg("resource_key"),
             py::call_guard<py::gil_scoped_release>(), "Invalidate one texture preview task/cache entry")
        .def("query_or_schedule_texture_preview", &Infernux::QueryOrScheduleTexturePreview, py::arg("resource_key"),
             py::arg("texture_file_path"), py::arg("content_stamp_hint"), py::arg("nearest") = false,
             py::arg("srgb") = false, py::arg("pump") = true, py::call_guard<py::gil_scoped_release>(),
             "Combined pump + query + schedule for texture preview. Returns (tex_id, width, height). C++ manages "
             "caching via generation counterching via generation counter.")
        .def(
            "schedule_texture_preview_from_memory",
            [](Infernux &self, const std::string &resourceKey, const py::buffer &imageData, uint64_t stamp,
               bool nearest) {
                const py::buffer_info info = imageData.request();
                if (info.ndim != 1 || info.itemsize != 1 || info.strides[0] != 1)
                    throw std::invalid_argument("encoded image data must be a contiguous one-dimensional byte buffer");
                const auto *begin = static_cast<const unsigned char *>(info.ptr);
                std::vector<unsigned char> owned(begin, begin + info.size);
                py::gil_scoped_release release;
                return self.ScheduleTexturePreviewFromMemory(resourceKey, std::move(owned), stamp, nearest);
            },
            py::arg("resource_key"), py::arg("image_data"), py::arg("stamp"), py::arg("nearest") = false,
            "Schedule texture preview from an encoded in-memory image buffer (JPEG/PNG/etc.)")
        .def("schedule_material_save_snapshot_task", &Infernux::ScheduleMaterialSaveSnapshotTask, py::arg("key"),
             py::arg("file_path"), py::arg("json_snapshot"), "Schedule async material save task from JSON snapshot")
        // ========================================================================
        // Editor Camera (property-based object access — preferred API)
        // ========================================================================
        .def_property_readonly("editor_camera", &Infernux::GetEditorCamera, py::return_value_policy::reference,
                               "Get the editor camera controller (EditorCamera object with property access)")
        // ========================================================================
        // Scene Camera Control API - for Scene View with Unity-style controls
        // ========================================================================
        .def("process_scene_view_input", &Infernux::ProcessSceneViewInput, py::arg("delta_time"),
             py::arg("right_mouse_down"), py::arg("middle_mouse_down"), py::arg("mouse_delta_x"),
             py::arg("mouse_delta_y"), py::arg("scroll_delta"), py::arg("key_w"), py::arg("key_a"), py::arg("key_s"),
             py::arg("key_d"), py::arg("key_q"), py::arg("key_e"), py::arg("key_shift"),
             "Process scene view input for editor camera control")
        // ========================================================================
        // Scene Render Target API - for offscreen scene rendering to ImGui
        // ========================================================================
        .def(
            "get_scene_texture_id",
            [](Infernux &self) -> uint64_t {
                auto *r = self.GetRenderer();
                return r ? r->GetSceneTextureId() : 0;
            },
            "Get scene render target texture ID for ImGui display")
        .def(
            "wait_for_gpu_idle",
            [](Infernux &self) {
                auto *r = self.GetRenderer();
                if (r)
                    r->WaitForGpuIdle();
            },
            "Drain pending GPU work before destructive scene replacement")
        .def(
            "resize_scene_render_target",
            [](Infernux &self, uint32_t width, uint32_t height) {
                auto *r = self.GetRenderer();
                if (r)
                    r->ResizeSceneRenderTarget(width, height);
            },
            py::arg("width"), py::arg("height"), "Resize the scene render target to match viewport size")
        // ========================================================================
        // Game Camera Render Target API - for Game View panel
        // ========================================================================
        .def(
            "get_game_texture_id",
            [](Infernux &self) -> uint64_t {
                auto *r = self.GetRenderer();
                return r ? r->GetGameTextureId() : 0;
            },
            "Get game render target texture ID for ImGui display")
        .def(
            "request_render_target_readback",
            [](Infernux &self, bool gameView) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("GPU image readback requires graphical renderer initialization");
                return renderer->RequestRenderTargetReadback(gameView);
            },
            py::arg("game_view") = true,
            "Asynchronously read the most recently submitted scene or game render target")
        .def(
            "request_capture",
            [](Infernux &self, const std::string &source, const std::string &outputPath) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Capture requires graphical renderer initialization");
                CaptureSource captureSource;
                if (source == "scene")
                    captureSource = CaptureSource::Scene;
                else if (source == "game")
                    captureSource = CaptureSource::Game;
                else
                    throw std::invalid_argument("Capture source must be 'scene' or 'game'");
                return renderer->RequestCapture(captureSource, outputPath);
            },
            py::arg("source"), py::arg("output_path"),
            "Capture an engine-owned render target asynchronously without reading OS window or desktop pixels")
        .def(
            "query_capture",
            [](Infernux &self, uint64_t captureId) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    throw std::logic_error("Capture requires graphical renderer initialization");
                const CaptureSnapshot value = renderer->QueryCapture(captureId);
                py::dict result;
                result["capture_id"] = value.id;
                result["source"] = CaptureSourceName(value.source);
                result["pixel_origin"] = "engine_render_target";
                result["os_capture_fallback"] = false;
                result["status"] = CaptureStatusName(value.status);
                result["source_generation"] = value.sourceGeneration;
                result["engine_frame"] = value.engineFrame;
                result["width"] = value.width;
                result["height"] = value.height;
                result["output_path"] = value.outputPath;
                result["error"] = value.error;
                return result;
            },
            py::arg("capture_id"), "Return status and metadata for an engine capture request")
        .def(
            "cancel_capture",
            [](Infernux &self, uint64_t captureId) {
                auto *renderer = self.GetRenderer();
                return renderer && renderer->CancelCapture(captureId);
            },
            py::arg("capture_id"), "Cancel an unfinished engine capture request")
        .def(
            "resize_game_render_target",
            [](Infernux &self, uint32_t width, uint32_t height) {
                auto *r = self.GetRenderer();
                if (r)
                    r->ResizeGameRenderTarget(width, height);
            },
            py::arg("width"), py::arg("height"), "Resize the game render target (lazy-initializes on first call)")
        .def(
            "set_game_camera_enabled",
            [](Infernux &self, bool enabled) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetGameCameraEnabled(enabled);
            },
            py::arg("enabled"), "Enable/disable game camera rendering")
        .def(
            "set_scene_view_visible",
            [](Infernux &self, bool visible) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetSceneViewVisible(visible);
            },
            py::arg("visible"), "Enable/disable scene view rendering")
        .def(
            "set_gui_player_mode",
            [](Infernux &self, bool enabled) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetGUIPlayerMode(enabled);
            },
            py::arg("enabled"), "Skip DockSpace/layout overhead in standalone player mode")
        .def(
            "is_game_camera_enabled",
            [](Infernux &self) -> bool {
                auto *r = self.GetRenderer();
                return r && r->IsGameCameraEnabled();
            },
            "Check if game camera rendering is enabled")
        .def(
            "get_last_game_render_ms",
            [](Infernux &self) -> double {
                auto *r = self.GetRenderer();
                return r ? r->GetLastGameRenderMs() : 0.0;
            },
            "Get last frame's game view render time (CPU command recording) in ms, excluding editor panels")
        .def(
            "get_game_only_frame_ms",
            [](Infernux &self) -> double {
                auto *r = self.GetRenderer();
                return r ? r->GetGameOnlyFrameMs() : 0.0;
            },
            "Get game-only frame cost in ms (SceneUpdate + PrepareFrame + GameRender), excluding editor panels")
        .def(
            "get_scene_update_ms",
            [](Infernux &self) -> double {
                auto *r = self.GetRenderer();
                return r ? r->GetSceneUpdateMs() : 0.0;
            },
            "Get SceneManager::Update + LateUpdate time in ms")
        .def(
            "get_gui_build_ms",
            [](Infernux &self) -> double {
                auto *r = self.GetRenderer();
                return r ? r->GetGuiBuildMs() : 0.0;
            },
            "Get GUI::BuildFrame (all ImGui panels) time in ms")
        .def(
            "get_prepare_frame_ms",
            [](Infernux &self) -> double {
                auto *r = self.GetRenderer();
                return r ? r->GetPrepareFrameMs() : 0.0;
            },
            "Get PrepareFrame (collect/cull renderables) time in ms")
        .def(
            "get_screen_ui_renderer",
            [](Infernux &self) -> InxScreenUIRenderer * {
                auto *r = self.GetRenderer();
                return r ? r->GetScreenUIRenderer() : nullptr;
            },
            py::return_value_policy::reference,
            "Get the screen UI renderer for GPU-based 2D screen-space UI (returns None before game RT init)")
        // ========================================================================
        // MSAA Configuration
        // ========================================================================
        .def(
            "set_msaa_samples",
            [](Infernux &self, int samples) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetMsaaSamples(samples);
            },
            py::arg("samples"), "Set MSAA sample count (1=off, 2, 4, 8) for both scene and game render targets")
        .def(
            "get_msaa_samples",
            [](Infernux &self) -> int {
                auto *r = self.GetRenderer();
                return r ? r->GetMsaaSamples() : 4;
            },
            "Get current MSAA sample count (1=off)")
        .def_property_readonly(
            "msaa_state",
            [](const Infernux &self) {
                const auto *renderer = self.GetRenderer();
                const MsaaStateSnapshot snapshot = renderer ? renderer->GetMsaaStateSnapshot() : MsaaStateSnapshot{};
                py::list supportedSamples;
                for (const int samples : {1, 2, 4, 8}) {
                    if ((snapshot.supportedSampleMask & static_cast<uint32_t>(samples)) != 0)
                        supportedSamples.append(samples);
                }
                py::dict result;
                result["active_samples"] = snapshot.activeSamples;
                result["scene_requested_samples"] = snapshot.sceneRequestedSamples;
                result["game_requested_samples"] = snapshot.gameRequestedSamples;
                result["supported_samples"] = std::move(supportedSamples);
                result["request_conflict"] = snapshot.requestConflict;
                result["scene_target_aligned"] = snapshot.sceneTargetAligned;
                result["game_target_aligned"] = snapshot.gameTargetAligned;
                result["material_pipelines_aligned"] = snapshot.materialPipelinesAligned;
                result["scene_msaa_color_bytes"] = snapshot.sceneMsaaColorBytes;
                result["game_msaa_color_bytes"] = snapshot.gameMsaaColorBytes;
                result["reconfiguration_count"] = snapshot.reconfigurationCount;
                result["rejected_request_count"] = snapshot.rejectedRequestCount;
                return result;
            },
            "Validated MSAA requests, device support, resource alignment, and reconfiguration counters")
        // ========================================================================
        // Present Mode
        // ========================================================================
        .def(
            "set_present_mode",
            [](Infernux &self, int mode) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetPresentMode(mode);
            },
            py::arg("mode"), "Set present mode: 0=IMMEDIATE, 1=MAILBOX, 2=FIFO, 3=FIFO_RELAXED")
        .def(
            "get_present_mode",
            [](Infernux &self) -> int {
                auto *r = self.GetRenderer();
                return r ? r->GetPresentMode() : 1;
            },
            "Get current present mode (0=IMMEDIATE, 1=MAILBOX, 2=FIFO, 3=FIFO_RELAXED)")
        // ========================================================================
        // Editor Power-Save / Idle Mode
        // ========================================================================
        .def(
            "set_editor_idle_enabled",
            [](Infernux &self, bool enabled) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetEditorIdleEnabled(enabled);
            },
            py::arg("enabled"), "Enable/disable editor idle mode (reduced FPS when no input)")
        .def(
            "is_editor_idle_enabled",
            [](Infernux &self) -> bool {
                auto *r = self.GetRenderer();
                return r && r->IsEditorIdleEnabled();
            },
            "Check if editor idle mode is enabled")
        .def(
            "set_editor_idle_fps",
            [](Infernux &self, float fps) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetEditorIdleFps(fps);
            },
            py::arg("fps"), "Set idle-mode target FPS (e.g. 10). 0 disables idling.")
        .def(
            "get_editor_idle_fps",
            [](Infernux &self) -> float {
                auto *r = self.GetRenderer();
                return r ? r->GetEditorIdleFps() : 0.0f;
            },
            "Get idle-mode target FPS")
        .def(
            "is_editor_idling",
            [](Infernux &self) -> bool {
                auto *r = self.GetRenderer();
                return r && r->IsEditorIdling();
            },
            "Check if editor is currently in idle (reduced FPS) state")
        .def(
            "request_full_speed_frame",
            [](Infernux &self) {
                auto *r = self.GetRenderer();
                if (r)
                    r->RequestFullSpeedFrame();
            },
            "Force full-speed rendering for the next few frames")
        .def(
            "set_editor_fps_cap",
            [](Infernux &self, float fps) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetEditorFpsCap(fps);
            },
            py::arg("fps"), "Set editor-mode FPS cap (e.g. 60). 0 = uncapped. Only applies outside play mode.")
        .def(
            "get_editor_fps_cap",
            [](Infernux &self) -> float {
                auto *r = self.GetRenderer();
                return r ? r->GetEditorFpsCap() : 0.0f;
            },
            "Get editor-mode FPS cap")
        .def(
            "set_play_mode_rendering",
            [](Infernux &self, bool play) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetPlayModeRendering(play);
            },
            py::arg("play"), "Enable/disable play-mode rendering (uncapped FPS, no idle)")
        .def(
            "is_play_mode_rendering",
            [](Infernux &self) -> bool {
                auto *r = self.GetRenderer();
                return r && r->IsPlayModeRendering();
            },
            "Check if renderer is in play-mode (uncapped FPS)")
        // ========================================================================
        // Scene Picking API - for editor selection
        // ========================================================================
        .def("pick_scene_object_id", &Infernux::PickSceneObjectId, py::arg("screen_x"), py::arg("screen_y"),
             py::arg("viewport_width"), py::arg("viewport_height"),
             "Pick a scene object or gizmo arrow by screen-space coordinates and return its ID (0 if none)")
        .def("pick_scene_object_ids", &Infernux::PickSceneObjectIds, py::arg("screen_x"), py::arg("screen_y"),
             py::arg("viewport_width"), py::arg("viewport_height"),
             "Pick ordered scene object candidate IDs from screen coordinates")
        .def(
            "request_scene_object_pick",
            [](Infernux &self, float x, float y, float viewportWidth, float viewportHeight) {
                auto *renderer = self.GetRenderer();
                return renderer ? renderer->RequestScenePick(x, y, viewportWidth, viewportHeight) : 0;
            },
            py::arg("screen_x"), py::arg("screen_y"), py::arg("viewport_width"), py::arg("viewport_height"),
            "Request an on-demand GPU object-ID pick. Returns an asynchronous request ID.")
        .def(
            "query_scene_object_pick",
            [](Infernux &self, uint64_t requestId) {
                auto *renderer = self.GetRenderer();
                const ScenePickSnapshot snapshot =
                    renderer ? renderer->QueryScenePick(requestId)
                             : ScenePickSnapshot{requestId, ScenePickStatus::Unknown, 0,
                                                 "Graphical renderer is unavailable"};
                const char *status = "unknown";
                switch (snapshot.status) {
                case ScenePickStatus::Pending:
                    status = "pending";
                    break;
                case ScenePickStatus::Completed:
                    status = "completed";
                    break;
                case ScenePickStatus::Failed:
                    status = "failed";
                    break;
                case ScenePickStatus::Cancelled:
                    status = "cancelled";
                    break;
                case ScenePickStatus::Unknown:
                    break;
                }
                py::dict result;
                result["request_id"] = snapshot.requestId;
                result["status"] = status;
                result["object_id"] = snapshot.objectId;
                result["error"] = snapshot.error;
                return result;
            },
            py::arg("request_id"), "Query an asynchronous GPU object-ID pick.")
        .def("pick_gizmo_axis", &Infernux::PickGizmoAxis, py::arg("screen_x"), py::arg("screen_y"),
             py::arg("viewport_width"), py::arg("viewport_height"),
             "Lightweight gizmo axis proximity test for hover highlighting (no scene raycast)")
        .def("set_editor_tool_highlight", &Infernux::SetEditorToolHighlight, py::arg("axis"),
             "Set the highlighted gizmo axis. 0=None, 1=X, 2=Y, 3=Z.")
        .def("set_editor_tool_mode", &Infernux::SetEditorToolMode, py::arg("mode"),
             "Set the active tool mode. 0=None, 1=Translate, 2=Rotate, 3=Scale.")
        .def("get_editor_tool_mode", &Infernux::GetEditorToolMode,
             "Get the active tool mode. 0=None, 1=Translate, 2=Rotate, 3=Scale.")
        .def("set_editor_tool_local_mode", &Infernux::SetEditorToolLocalMode, py::arg("local"),
             "Enable/disable local coordinate mode for editor tools (gizmo aligns to object rotation)")
        .def("screen_to_world_ray", &Infernux::ScreenToWorldRay, py::arg("screen_x"), py::arg("screen_y"),
             py::arg("viewport_width"), py::arg("viewport_height"),
             "Build a world-space ray from screen coords. Returns (ox,oy,oz, dx,dy,dz).")
        // ========================================================================
        // Editor Gizmos API - for toggling visual aids in scene view
        // ========================================================================
        .def(
            "set_show_grid",
            [](Infernux &self, bool show) {
                auto *r = self.GetRenderer();
                if (r)
                    r->SetShowGrid(show);
            },
            py::arg("show"), "Set visibility of ground grid")
        .def(
            "is_show_grid",
            [](Infernux &self) -> bool {
                auto *r = self.GetRenderer();
                return r && r->IsShowGrid();
            },
            "Get visibility of ground grid")
        .def("set_selection_outline", &Infernux::SetSelectionOutline, py::arg("object_id"),
             "Set selection outline for a game object (Unity-style orange wireframe). Pass 0 to clear.")
        .def("set_selection_outlines", &Infernux::SetSelectionOutlines, py::arg("object_ids"),
             "Set combined selection outline for multiple game objects.")
        .def("get_selected_object_id", &Infernux::GetSelectedObjectId,
             "Get the currently selected object ID (0 if none).")
        .def("clear_selection_outline", &Infernux::ClearSelectionOutline, "Clear selection outline")
        // ========================================================================
        // Component Gizmos API — upload per-component gizmo geometry from Python
        // ========================================================================
        .def(
            "upload_component_gizmos",
            [](Infernux &self, py::buffer vertices, int64_t vertexCount, py::buffer indices, py::buffer descriptors,
               int64_t descriptorCount) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    return;
                GizmosDrawCallBuffer *buf = renderer->GetGizmosDrawCallBuffer();
                if (!buf)
                    return;

                // vertices: flat float buffer, stride 6 (pos3 + color3) per vertex
                constexpr int64_t kVertStride = 6;
                py::buffer_info vInfo = vertices.request();
                const float *vPtr = static_cast<const float *>(vInfo.ptr);

                std::vector<Vertex> verts;
                verts.reserve(static_cast<size_t>(vertexCount));
                for (int64_t i = 0; i < vertexCount; ++i) {
                    const float *b = vPtr + i * kVertStride;
                    Vertex v;
                    v.pos = glm::vec3(b[0], b[1], b[2]);
                    v.normal = glm::vec3(0.0f, 1.0f, 0.0f);
                    v.tangent = glm::vec4(1.0f, 0.0f, 0.0f, 1.0f);
                    v.color = glm::vec3(b[3], b[4], b[5]);
                    v.texCoord = glm::vec2(0.0f);
                    verts.push_back(v);
                }

                // indices: flat uint32 buffer
                py::buffer_info iInfo = indices.request();
                const uint32_t *iPtr = static_cast<const uint32_t *>(iInfo.ptr);
                std::vector<uint32_t> idx(iPtr, iPtr + iInfo.size);

                // descriptors: flat float buffer, stride 18 (indexStart + indexCount + mat4x4)
                constexpr int64_t kDescStride = 18;
                py::buffer_info dInfo = descriptors.request();
                const float *dPtr = static_cast<const float *>(dInfo.ptr);

                std::vector<GizmosDrawCallBuffer::DrawDescriptor> descs;
                descs.reserve(static_cast<size_t>(descriptorCount));
                for (int64_t i = 0; i < descriptorCount; ++i) {
                    const float *b = dPtr + i * kDescStride;
                    GizmosDrawCallBuffer::DrawDescriptor d;
                    d.indexStart = static_cast<uint32_t>(b[0]);
                    d.indexCount = static_cast<uint32_t>(b[1]);
                    for (int j = 0; j < 16; ++j) {
                        d.worldMatrix[j] = b[2 + j];
                    }
                    descs.push_back(d);
                }

                buf->SetData(std::move(verts), std::move(idx), std::move(descs));
            },
            py::arg("vertices"), py::arg("vertex_count"), py::arg("indices"), py::arg("descriptors"),
            py::arg("descriptor_count"),
            "Upload per-component gizmo geometry via buffer protocol (no numpy). "
            "vertices: flat float32 (N*6), indices: flat uint32, descriptors: flat float32 (D*18)")
        .def(
            "clear_component_gizmos",
            [](Infernux &self) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    return;
                GizmosDrawCallBuffer *buf = renderer->GetGizmosDrawCallBuffer();
                if (buf)
                    buf->Clear();
            },
            "Clear all component gizmo geometry")
        .def(
            "upload_component_gizmo_icons",
            [](Infernux &self, py::buffer positions, py::buffer objectIds, py::buffer iconKinds, int64_t iconCount) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    return;
                GizmosDrawCallBuffer *buf = renderer->GetGizmosDrawCallBuffer();
                if (!buf || iconCount <= 0)
                    return;

                // positions: flat float buffer, stride 6 (pos3 + color3) per icon
                constexpr int64_t kPosStride = 6;
                py::buffer_info posInfo = positions.request();
                const float *posPtr = static_cast<const float *>(posInfo.ptr);

                // objectIds: flat uint32 buffer, stride 2 (lo + hi) per icon
                py::buffer_info idInfo = objectIds.request();
                const uint32_t *idPtr = static_cast<const uint32_t *>(idInfo.ptr);

                py::buffer_info kindInfo = iconKinds.request();
                const uint32_t *kindPtr = static_cast<const uint32_t *>(kindInfo.ptr);

                std::vector<GizmosDrawCallBuffer::IconEntry> entries;
                entries.reserve(static_cast<size_t>(iconCount));
                for (int64_t i = 0; i < iconCount; ++i) {
                    const float *p = posPtr + i * kPosStride;
                    const uint32_t *id = idPtr + i * 2;

                    GizmosDrawCallBuffer::IconEntry entry;
                    entry.position = glm::vec3(p[0], p[1], p[2]);
                    entry.color = glm::vec3(p[3], p[4], p[5]);
                    entry.objectId = (static_cast<uint64_t>(id[1]) << 32) | static_cast<uint64_t>(id[0]);
                    entry.iconKind = kindPtr[i];
                    entries.push_back(entry);
                }

                static int64_t s_lastIconUploadCount = -1;
                if (s_lastIconUploadCount != iconCount) {
                    uint32_t firstKind = entries.empty() ? 0u : entries.front().iconKind;
                    s_lastIconUploadCount = iconCount;
                }

                buf->SetIconData(std::move(entries));
            },
            py::arg("positions"), py::arg("object_ids"), py::arg("icon_kinds"), py::arg("icon_count"),
            "Upload component gizmo icon entries via buffer protocol (no numpy). "
            "positions: flat float32 (N*6: x,y,z,r,g,b), object_ids: flat uint32 (N*2: lo,hi), "
            "icon_kinds: flat uint32 (N)")
        .def(
            "clear_component_gizmo_icons",
            [](Infernux &self) {
                auto *renderer = self.GetRenderer();
                if (!renderer)
                    return;
                GizmosDrawCallBuffer *buf = renderer->GetGizmosDrawCallBuffer();
                if (buf)
                    buf->ClearIcons();
            },
            "Clear all component gizmo icon data")
        .def(
            "submit_particle_instances",
            [](Infernux &self, uint64_t batchId, py::buffer instanceBuffer, const std::string &materialGuid,
               float originX, float originY, float originZ, bool validate) {
                auto *renderer = self.GetRenderer();
                if (!renderer || !renderer->GetParticleDrawCallBuffer())
                    throw std::logic_error("particle submission requires graphical renderer initialization");
                py::buffer_info info = instanceBuffer.request();
                if (info.ndim != 2 || info.shape[1] != 9 || info.itemsize != sizeof(float) ||
                    info.format != py::format_descriptor<float>::format()) {
                    throw std::invalid_argument("particle instances must be a contiguous float32 array shaped (N, 9)");
                }
                if (info.strides[1] != static_cast<py::ssize_t>(sizeof(float)) ||
                    info.strides[0] != static_cast<py::ssize_t>(9 * sizeof(float))) {
                    throw std::invalid_argument("particle instances must be C-contiguous");
                }

                renderer->GetParticleDrawCallBuffer()->SetBatchInterleaved(
                    batchId, static_cast<const float *>(info.ptr), static_cast<size_t>(info.shape[0]), materialGuid,
                    glm::vec3(originX, originY, originZ), validate);
            },
            py::arg("batch_id"), py::arg("instances"), py::arg("material_guid") = "", py::arg("origin_x") = 0.0f,
            py::arg("origin_y") = 0.0f, py::arg("origin_z") = 0.0f, py::arg("validate") = true,
            "Submit one contiguous particle instance batch (position3, size, color4, rotation)")
        .def(
            "remove_particle_batch",
            [](Infernux &self, uint64_t batchId) {
                auto *renderer = self.GetRenderer();
                if (renderer && renderer->GetParticleDrawCallBuffer())
                    renderer->GetParticleDrawCallBuffer()->RemoveBatch(batchId);
            },
            py::arg("batch_id"), "Remove a persistent particle instance batch")
        .def(
            "_replace_gpu_particle_emitters",
            [](Infernux &self, const py::sequence &encodedPrograms, const std::vector<uint64_t> &removeIds) {
                auto *renderer = self.GetRenderer();
                auto *manager = renderer ? renderer->GetParticleGpuSystemManager() : nullptr;
                if (!manager)
                    return std::string("GPU particle runtime requires graphical renderer initialization");

                std::vector<particle::GpuParticleEmitterProgram> programs;
                programs.reserve(encodedPrograms.size());
                for (const py::handle item : encodedPrograms) {
                    if (!py::isinstance<py::dict>(item))
                        throw std::invalid_argument("GPU particle program batch must contain dictionaries");
                    programs.push_back(DecodeGpuParticleProgram(py::reinterpret_borrow<py::dict>(item)));
                }
                std::string error;
                if (!manager->ApplyBatch(programs, removeIds, &error))
                    return error.empty() ? std::string("failed to publish GPU particle program batch") : error;
                return std::string{};
            },
            py::arg("programs"), py::arg("remove_ids") = std::vector<uint64_t>{},
            "Internal control-plane publication for one saved ParticleGraph revision")
        .def(
            "_begin_gpu_particle_frame",
            [](Infernux &self, uint64_t emitterId, uint32_t spawnCount, uint32_t spawnBaseId,
               uint32_t spawnGeneration, uint32_t systemSeed, uint32_t simulationStep, float deltaTime,
               const py::buffer &transforms, bool simulate, bool render) {
                auto *renderer = self.GetRenderer();
                auto *manager = renderer ? renderer->GetParticleGpuSystemManager() : nullptr;
                if (!manager)
                    return false;
                particle::GpuParticleFrameRequest request;
                request.frameIndex = renderer->GetNextFrameIndex();
                request.spawnCount = spawnCount;
                request.spawnBaseId = spawnBaseId;
                request.spawnGeneration = spawnGeneration;
                request.systemSeed = systemSeed;
                request.simulationStep = simulationStep;
                request.deltaTime = deltaTime;
                request.simulate = simulate;
                request.render = render;
                return manager->BeginFrame(emitterId, request, DecodeGpuParticleTransforms(transforms));
            },
            py::arg("emitter_id"), py::arg("spawn_count"), py::arg("spawn_base_id"),
            py::arg("spawn_generation"), py::arg("system_seed"), py::arg("simulation_step"),
            py::arg("delta_time"), py::arg("transforms"), py::arg("simulate") = true, py::arg("render") = true,
            "Internal once-per-engine-frame GPU particle scheduling hook")
        .def(
            "_remove_gpu_particle_emitter",
            [](Infernux &self, uint64_t emitterId) {
                auto *renderer = self.GetRenderer();
                auto *manager = renderer ? renderer->GetParticleGpuSystemManager() : nullptr;
                return manager && manager->Remove(emitterId);
            },
            py::arg("emitter_id"), "Internal GPU particle emitter removal hook")
        .def(
            "_reset_gpu_particle_emitter",
            [](Infernux &self, uint64_t emitterId) {
                auto *renderer = self.GetRenderer();
                auto *manager = renderer ? renderer->GetParticleGpuSystemManager() : nullptr;
                return manager && manager->Reset(emitterId);
            },
            py::arg("emitter_id"), "Reset one GPU emitter at the next simulation boundary")
        .def(
            "_gpu_particle_artifact_revision",
            [](Infernux &self, uint64_t emitterId) {
                auto *renderer = self.GetRenderer();
                auto *manager = renderer ? renderer->GetParticleGpuSystemManager() : nullptr;
                return manager ? manager->ActiveArtifactRevision(emitterId) : uint64_t{0};
            },
            py::arg("emitter_id"), "Return the active GPU particle artifact revision")
        .def(
            "_gpu_particle_output_count",
            [](Infernux &self, uint64_t emitterId) {
                auto *renderer = self.GetRenderer();
                auto *manager = renderer ? renderer->GetParticleGpuSystemManager() : nullptr;
                return manager ? manager->ActiveOutputCount(emitterId) : size_t{0};
            },
            py::arg("emitter_id"), "Return the active GPU particle rendering output count")
        .def(
            "_gpu_particle_output_render_queue",
            [](Infernux &self, uint64_t emitterId, uint64_t outputId) {
                auto *renderer = self.GetRenderer();
                auto *manager = renderer ? renderer->GetParticleGpuSystemManager() : nullptr;
                return manager ? manager->ActiveOutputRenderQueue(emitterId, outputId) : int32_t{-1};
            },
            py::arg("emitter_id"), py::arg("output_id"), "Return the active GPU particle output render queue")
        // ========================================================================
        // Material Pipeline API - for refreshing material shaders at runtime
        // ========================================================================
        .def("refresh_material_pipeline", &Infernux::RefreshMaterialPipeline, py::arg("material"),
             "Refresh a material's rendering pipeline by reloading its shaders")
        .def(
            "remove_material_pipeline",
            [](Infernux &self, const std::string &materialName) {
                auto *r = self.GetRenderer();
                if (r)
                    r->RemoveMaterialPipeline(materialName);
            },
            py::arg("material_name"), "Remove pipeline render data for a deleted material (releases GPU resources)")
        // ========================================================================
        // Render Pipeline API - for custom Python render pipelines (SRP)
        // ========================================================================
        .def(
            "set_render_pipeline",
            [](Infernux &self, py::object pipeline) {
                auto *r = self.GetRenderer();
                if (!r)
                    return;
                if (pipeline.is_none()) {
                    r->SetRenderPipeline(nullptr);
                } else {
                    r->SetRenderPipeline(pipeline.cast<std::shared_ptr<RenderPipelineCallback>>());
                }
            },
            py::arg("pipeline"),
            "Set a custom RenderPipelineCallback to control rendering from Python. Pass None to revert to default.")
        // ========================================================================
        // Render Graph API
        // ========================================================================
        .def(
            "get_scene_render_graph",
            [](Infernux &self) -> SceneRenderGraph * {
                auto *r = self.GetRenderer();
                return r ? r->GetSceneRenderGraph() : nullptr;
            },
            py::return_value_policy::reference, "Get the scene render graph for pass configuration");

    // ── Logging bridge: let Python write to the C++ InxLog (engine.log) ──
    m.def(
        "inflog_warn", [](const std::string &msg) { INXLOG_WARN(msg); }, py::arg("msg"),
        "Write a WARN-level message to the engine log.");

    m.def(
        "inflog_internal", [](const std::string &msg) { INXLOG_INFO_INTERNAL(msg); }, py::arg("msg"),
        "Write an internal INFO-level message to the engine log without surfacing it in the editor console.");

    // Register all binding modules
    RegisterGUIBindings(m);
    RegisterVector2Bindings(m);
    RegisterVector3Bindings(m);
    RegisterVec4fBindings(m);
    RegisterResourceBindings(m);
    RegisterAssetDatabaseBindings(m);
    RegisterAssetRegistryBindings(m);
    RegisterSceneBindings(m);
    RegisterTagLayerBindings(m);
    RegisterRhiBindings(m);
    RegisterRenderGraphBindings(m);
    RegisterCommandBufferBindings(m);
    RegisterRenderPipelineBindings(m);
    RegisterInputBindings(m);
    RegisterPhysicsBindings(m);
    RegisterAudioBindings(m);
    RegisterBatchBindings(m);

    // ====================================================================
    // Gizmo geometry generation helpers (pure math, no engine state needed)
    // Returns pre-packed flat float arrays ready for Python Gizmos system.
    // ====================================================================

    m.def(
        "generate_wire_sphere",
        [](float cx, float cy, float cz, float radius, int segments, float cr, float cg, float cb) -> py::tuple {
            // Generate 3 axis-aligned circles: YZ, XZ, XY
            const int totalVerts = segments * 3;
            const int totalIndices = segments * 3 * 2; // 2 indices per line segment

            // Pre-compute trig table
            std::vector<float> cosTab(segments), sinTab(segments);
            const float twoPi = 2.0f * 3.14159265358979323846f;
            for (int i = 0; i < segments; ++i) {
                float angle = twoPi * static_cast<float>(i) / static_cast<float>(segments);
                cosTab[i] = std::cos(angle);
                sinTab[i] = std::sin(angle);
            }

            // Flat vertex buffer: x,y,z,r,g,b per vertex
            std::vector<float> verts(totalVerts * 6);
            std::vector<int32_t> indices(totalIndices);

            int vi = 0; // vertex float index
            int ii = 0; // index index
            for (int axis = 0; axis < 3; ++axis) {
                int base = axis * segments;
                for (int i = 0; i < segments; ++i) {
                    float ca = cosTab[i] * radius;
                    float sa = sinTab[i] * radius;
                    float px, py, pz;
                    if (axis == 0) {
                        px = cx;
                        py = cy + ca;
                        pz = cz + sa;
                    } else if (axis == 1) {
                        px = cx + ca;
                        py = cy;
                        pz = cz + sa;
                    } else {
                        px = cx + ca;
                        py = cy + sa;
                        pz = cz;
                    }
                    verts[vi++] = px;
                    verts[vi++] = py;
                    verts[vi++] = pz;
                    verts[vi++] = cr;
                    verts[vi++] = cg;
                    verts[vi++] = cb;

                    indices[ii++] = base + i;
                    indices[ii++] = base + (i + 1) % segments;
                }
            }

            auto vertArr = py::array_t<float>(verts.size(), verts.data());
            auto idxArr = py::array_t<int32_t>(indices.size(), indices.data());
            return py::make_tuple(vertArr, totalVerts, idxArr);
        },
        py::arg("cx"), py::arg("cy"), py::arg("cz"), py::arg("radius"), py::arg("segments"), py::arg("cr"),
        py::arg("cg"), py::arg("cb"),
        "Generate wire sphere vertices and indices. Returns (vert_flat, vert_count, idx_flat).");

    m.def(
        "generate_wire_arc",
        [](float cx, float cy, float cz, float nx, float ny, float nz, float radius, float startDeg, float arcDeg,
           int segments, float cr, float cg, float cb) -> py::tuple {
            // Normalize normal
            float len = std::sqrt(nx * nx + ny * ny + nz * nz);
            if (len < 1e-8f)
                return py::make_tuple(py::array_t<float>(0), 0, py::array_t<int32_t>(0));
            nx /= len;
            ny /= len;
            nz /= len;

            // Build basis from normal
            float ax, ay, az;
            if (std::fabs(ny) < 0.99f) {
                ax = 0;
                ay = 1;
                az = 0;
            } else {
                ax = 1;
                ay = 0;
                az = 0;
            }

            // u = normalize(cross(normal, arbitrary))
            float ux = ny * az - nz * ay;
            float uy = nz * ax - nx * az;
            float uz = nx * ay - ny * ax;
            float ul = std::sqrt(ux * ux + uy * uy + uz * uz);
            ux /= ul;
            uy /= ul;
            uz /= ul;

            // v = cross(normal, u)
            float vx = ny * uz - nz * uy;
            float vy = nz * ux - nx * uz;
            float vz = nx * uy - ny * ux;

            int numPts = segments + 1;
            std::vector<float> verts(numPts * 6);
            std::vector<int32_t> indices(segments * 2);

            float startRad = startDeg * 3.14159265358979323846f / 180.0f;
            float arcRad = arcDeg * 3.14159265358979323846f / 180.0f;

            int vi = 0, ii = 0;
            for (int i = 0; i <= segments; ++i) {
                float angle = startRad + arcRad * static_cast<float>(i) / static_cast<float>(segments);
                float ca = std::cos(angle), sa = std::sin(angle);
                verts[vi++] = cx + radius * (ca * ux + sa * vx);
                verts[vi++] = cy + radius * (ca * uy + sa * vy);
                verts[vi++] = cz + radius * (ca * uz + sa * vz);
                verts[vi++] = cr;
                verts[vi++] = cg;
                verts[vi++] = cb;
                if (i > 0) {
                    indices[ii++] = i - 1;
                    indices[ii++] = i;
                }
            }

            auto vertArr = py::array_t<float>(verts.size(), verts.data());
            auto idxArr = py::array_t<int32_t>(indices.size(), indices.data());
            return py::make_tuple(vertArr, numPts, idxArr);
        },
        py::arg("cx"), py::arg("cy"), py::arg("cz"), py::arg("nx"), py::arg("ny"), py::arg("nz"), py::arg("radius"),
        py::arg("start_deg"), py::arg("arc_deg"), py::arg("segments"), py::arg("cr"), py::arg("cg"), py::arg("cb"),
        "Generate wire arc vertices and indices. Returns (vert_flat, vert_count, idx_flat).");
}
