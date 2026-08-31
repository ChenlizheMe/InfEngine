#include "WebParticleRuntime.h"

#include "InfernuxWebHostModule.h"
#include "WebGpuRhiDevice.h"

#include <function/renderer/particle/ParticleGpuRuntime.h>
#include <function/scene/Camera.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>

#include <glm/glm.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <memory>
#include <unordered_map>
#include <utility>
#include <vector>

namespace infernux::web
{
namespace
{

constexpr std::array<const char *, static_cast<size_t>(particle::GpuKernelStage::Count)> kStageNames = {
    "bootstrap",        "init",         "update",    "update_rendering_fused", "contact_prepare", "contact_solve",
    "contact_dispatch", "render_reset", "rendering",
};

constexpr char kParticleShader[] = R"wgsl(
struct CameraData {
    view_projection: mat4x4<f32>,
    camera_right: vec4<f32>,
    camera_up: vec4<f32>,
};

struct ParticleInstance {
    position_size: vec4<f32>,
    color: vec4<f32>,
    rotation_custom: vec4<f32>,
    scale_custom: vec4<f32>,
    ribbon_data: vec4<u32>,
    custom_data: vec4<f32>,
    previous_position_history: vec4<f32>,
};

@group(0) @binding(0) var<uniform> camera: CameraData;
@group(1) @binding(0) var<storage, read> instances: array<ParticleInstance>;
@group(1) @binding(1) var<storage, read> render_indices: array<u32>;

struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) color: vec4<f32>,
    @location(1) uv: vec2<f32>,
};

@vertex
fn vertex_main(@builtin(vertex_index) vertex_index: u32,
               @builtin(instance_index) instance_index: u32) -> VertexOutput {
    let corners = array<vec2<f32>, 6>(
        vec2<f32>(-1.0, -1.0), vec2<f32>(1.0, -1.0), vec2<f32>(1.0, 1.0),
        vec2<f32>(-1.0, -1.0), vec2<f32>(1.0, 1.0), vec2<f32>(-1.0, 1.0));
    let particle = instances[render_indices[instance_index]];
    let corner = corners[vertex_index];
    let angle = particle.rotation_custom.x;
    let rotated = vec2<f32>(
        corner.x * cos(angle) - corner.y * sin(angle),
        corner.x * sin(angle) + corner.y * cos(angle));
    let extent = max(abs(particle.position_size.w), 0.001) *
                 max(abs(particle.scale_custom.xy), vec2<f32>(0.001));
    let world = particle.position_size.xyz +
                camera.camera_right.xyz * rotated.x * extent.x +
                camera.camera_up.xyz * rotated.y * extent.y;
    var output: VertexOutput;
    output.position = camera.view_projection * vec4<f32>(world, 1.0);
    output.color = particle.color;
    output.uv = corner * 0.5 + vec2<f32>(0.5);
    return output;
}

@fragment
fn fragment_main(input: VertexOutput) -> @location(0) vec4<f32> {
    // Sprite geometry is a quad. Coverage belongs to the selected particle
    // material/texture; imposing a radial mask here made every untextured
    // particle circular and diverged from the Vulkan renderer.
    return input.color;
}
)wgsl";

glm::mat4 ToWebClipSpace(const glm::mat4 &vulkanViewProjection)
{
    glm::mat4 correction(1.0f);
    correction[1][1] = -1.0f;
    return correction * vulkanViewProjection;
}

template <typename T> bool ReadUnsigned(PyObject *value, T &result)
{
    if (!value || !PyLong_Check(value))
        return false;
    const unsigned long long decoded = PyLong_AsUnsignedLongLong(value);
    if (PyErr_Occurred() || decoded > std::numeric_limits<T>::max()) {
        PyErr_Clear();
        return false;
    }
    result = static_cast<T>(decoded);
    return true;
}

PyObject *Field(PyObject *mapping, const char *name)
{
    return mapping && PyDict_Check(mapping) ? PyDict_GetItemString(mapping, name) : nullptr;
}

bool ReadBool(PyObject *value, bool &result)
{
    if (!value || !PyBool_Check(value))
        return false;
    result = value == Py_True;
    return true;
}

bool ReadFloat(PyObject *value, float &result)
{
    if (!value)
        return false;
    const double decoded = PyFloat_AsDouble(value);
    if (PyErr_Occurred() || !std::isfinite(decoded)) {
        PyErr_Clear();
        return false;
    }
    result = static_cast<float>(decoded);
    return true;
}

bool ReadWordVector(PyObject *value, std::vector<uint32_t> &result)
{
    PyObject *sequence = PySequence_Fast(value, "particle parameter words must be a sequence");
    if (!sequence) {
        PyErr_Clear();
        return false;
    }
    const Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    result.clear();
    result.reserve(static_cast<size_t>(std::max<Py_ssize_t>(0, count)));
    bool valid = count >= 0 && count % 4 == 0;
    for (Py_ssize_t index = 0; valid && index < count; ++index) {
        uint32_t word = 0;
        valid = ReadUnsigned(PySequence_Fast_GET_ITEM(sequence, index), word);
        if (valid)
            result.push_back(word);
    }
    Py_DECREF(sequence);
    return valid;
}

bool ReadTransforms(PyObject *value, particle::GpuParticleTransforms &result)
{
    Py_buffer view{};
    if (value && PyObject_GetBuffer(value, &view, PyBUF_CONTIG_RO | PyBUF_FORMAT) == 0) {
        const bool valid = view.buf && view.len == static_cast<Py_ssize_t>(sizeof(result)) && view.itemsize == 4 &&
                           (!view.format || std::strcmp(view.format, "f") == 0);
        if (valid)
            std::memcpy(&result, view.buf, sizeof(result));
        PyBuffer_Release(&view);
        return valid;
    }
    if (PyErr_Occurred())
        PyErr_Clear();
    PyObject *sequence = PySequence_Fast(value, "particle transforms must be a sequence");
    if (!sequence) {
        PyErr_Clear();
        return false;
    }
    constexpr Py_ssize_t FloatCount = static_cast<Py_ssize_t>(sizeof(result) / sizeof(float));
    bool valid = PySequence_Fast_GET_SIZE(sequence) == FloatCount;
    auto *destination = reinterpret_cast<float *>(&result);
    for (Py_ssize_t index = 0; valid && index < FloatCount; ++index)
        valid = ReadFloat(PySequence_Fast_GET_ITEM(sequence, index), destination[index]);
    Py_DECREF(sequence);
    return valid;
}

bool EmptySequence(PyObject *value)
{
    return value && PySequence_Check(value) && PySequence_Size(value) == 0;
}

bool ProgramUsesPortableSubset(PyObject *program, std::string &error)
{
    bool collision = false;
    if (!ReadBool(Field(program, "collision_enabled"), collision) || collision) {
        error = "WebGPU particles currently require collision_enabled=false";
        return false;
    }
    PyObject *continuation = Field(program, "continuation");
    if (!continuation || continuation != Py_None) {
        error = "WebGPU particles currently require continuation=null";
        return false;
    }
    PyObject *layout = Field(program, "data_interface_layout");
    if (!layout || !PyDict_Check(layout) || !EmptySequence(Field(layout, "mesh_interfaces")) ||
        !EmptySequence(Field(layout, "texture2d_parameters")) || !EmptySequence(Field(layout, "volume_interfaces"))) {
        error = "WebGPU particles currently require an empty data-interface layout";
        return false;
    }
    PyObject *fusion = Field(program, "update_render_fusion");
    bool eligible = false;
    if (!fusion || !PyDict_Check(fusion) || !ReadBool(Field(fusion, "eligible"), eligible) || !eligible) {
        error = "WebGPU particles currently require fused update/rendering";
        return false;
    }
    PyObject *outputs = Field(program, "outputs");
    PyObject *sequence = PySequence_Fast(outputs, "particle outputs must be a sequence");
    if (!sequence) {
        PyErr_Clear();
        error = "WebGPU particle outputs are invalid";
        return false;
    }
    bool valid = PySequence_Fast_GET_SIZE(sequence) > 0;
    for (Py_ssize_t index = 0; valid && index < PySequence_Fast_GET_SIZE(sequence); ++index) {
        PyObject *output = PySequence_Fast_GET_ITEM(sequence, index);
        PyObject *type = Field(output, "output_type");
        valid = type && PyUnicode_Check(type) && std::strcmp(PyUnicode_AsUTF8(type), "sprite") == 0;
    }
    Py_DECREF(sequence);
    if (!valid)
        error = "WebGPU particles currently render sprite outputs only";
    return valid;
}

struct FrameRequest
{
    uint32_t spawnCount = 0;
    uint32_t spawnBaseId = 0;
    uint32_t spawnGeneration = 0;
    uint32_t systemSeed = 0;
    uint32_t simulationStep = 0;
    float deltaTime = 0.0f;
    bool simulate = true;
    bool render = true;
};

struct alignas(16) WebSpawnMetadata
{
    uint32_t words[8]{};
};
static_assert(sizeof(WebSpawnMetadata) == 32);

bool DecodeRequest(PyObject *mapping, FrameRequest &request, bool preroll)
{
    if (!mapping || !PyDict_Check(mapping) || !ReadUnsigned(Field(mapping, "spawn_count"), request.spawnCount) ||
        !ReadUnsigned(Field(mapping, "spawn_base_id"), request.spawnBaseId) ||
        !ReadUnsigned(Field(mapping, "spawn_generation"), request.spawnGeneration) ||
        !ReadUnsigned(Field(mapping, "system_seed"), request.systemSeed) ||
        !ReadUnsigned(Field(mapping, "simulation_step"), request.simulationStep) ||
        !ReadFloat(Field(mapping, "delta_time"), request.deltaTime))
        return false;
    if (preroll) {
        request.simulate = true;
        request.render = false;
        return true;
    }
    return ReadBool(Field(mapping, "simulate"), request.simulate) && ReadBool(Field(mapping, "render"), request.render);
}

rhi::BufferHandle CreateStorageBuffer(WebGpuRhiDevice &device, uint64_t bytes, bool indirect = false,
                                      const void *initialData = nullptr)
{
    rhi::BufferDesc desc;
    desc.byteSize = std::max<uint64_t>(bytes, 16);
    desc.usage = rhi::BufferUsageFlags::Storage;
    if (indirect)
        desc.usage = desc.usage | rhi::BufferUsageFlags::Indirect;
    desc.memory = rhi::BufferMemory::Upload;
    desc.queueAccess = rhi::QueueAccessFlags::Graphics | rhi::QueueAccessFlags::Compute;
    desc.initialData = initialData;
    desc.initialDataBytes = initialData ? bytes : 0;
    return device.CreateBuffer(desc);
}

} // namespace

struct WebParticleRuntime::State
{
    struct Graph
    {
        uint64_t id = 0;
        uint32_t slotCount = 0;
        std::vector<uint32_t> parameterWords;
        rhi::BufferHandle burstRequests;
        rhi::BufferHandle spawnMetadata;
        rhi::BufferHandle parameters;
        rhi::BufferHandle playingRequests;
        rhi::BufferHandle playingStates;
    };

    struct Emitter
    {
        uint64_t id = 0;
        uint64_t graphId = 0;
        uint64_t artifactRevision = 0;
        bool stateWasPreserved = false;
        uint32_t slot = 0;
        bool playing = true;
        bool resetPending = false;
        bool drawReady = false;
        particle::GpuParticleTransforms transforms{};
        std::vector<FrameRequest> pending;
        std::unique_ptr<particle::ParticleGpuRuntime> runtime;
        rhi::BindGroupHandle spawnGroup;
        wgpu::BindGroup renderGroup;
    };

    WebGpuRhiDevice *rhi = nullptr;
    wgpu::Device device;
    wgpu::Queue queue;
    rhi::PixelFormat colorFormat = rhi::PixelFormat::Undefined;
    wgpu::RenderPipeline renderPipeline;
    wgpu::BindGroupLayout cameraLayout;
    wgpu::BindGroupLayout geometryLayout;
    wgpu::Buffer cameraBuffer;
    wgpu::BindGroup cameraGroup;
    std::unordered_map<uint64_t, Graph> graphs;
    std::unordered_map<uint64_t, std::unique_ptr<Emitter>> emitters;
    std::string error;
    bool reportedReady = false;
    bool reportedDraw = false;

    void ReleaseGraph(Graph &graph) noexcept
    {
        if (!rhi)
            return;
        rhi->Release(graph.playingStates);
        rhi->Release(graph.playingRequests);
        rhi->Release(graph.parameters);
        rhi->Release(graph.spawnMetadata);
        rhi->Release(graph.burstRequests);
        graph = {};
    }

    void ReleaseEmitter(Emitter &emitter) noexcept
    {
        emitter.renderGroup = {};
        if (rhi)
            rhi->Release(emitter.spawnGroup);
        emitter.spawnGroup = {};
        emitter.runtime.reset();
    }

    void Clear() noexcept
    {
        for (auto &[_, emitter] : emitters)
            ReleaseEmitter(*emitter);
        emitters.clear();
        for (auto &[_, graph] : graphs)
            ReleaseGraph(graph);
        graphs.clear();
        renderPipeline = {};
        cameraGroup = {};
        cameraBuffer = {};
        geometryLayout = {};
        cameraLayout = {};
        queue = {};
        device = {};
        rhi = nullptr;
    }
};

WebParticleRuntime::WebParticleRuntime() : m_state(std::make_unique<State>())
{
}

WebParticleRuntime::~WebParticleRuntime()
{
    Shutdown();
}

bool WebParticleRuntime::Initialize(WebGpuRhiDevice &device, rhi::PixelFormat colorFormat)
{
    Shutdown();
    m_state->rhi = &device;
    m_state->device = device.NativeDevice();
    m_state->queue = device.NativeQueue();
    m_state->colorFormat = colorFormat;
    if (!m_state->device || !m_state->queue || colorFormat == rhi::PixelFormat::Undefined) {
        m_state->error = "Web particle runtime requires a ready WebGPU device";
        return false;
    }

    wgpu::BufferDescriptor cameraBufferDesc;
    cameraBufferDesc.size = sizeof(glm::mat4) + sizeof(glm::vec4) * 2;
    cameraBufferDesc.usage = wgpu::BufferUsage::Uniform | wgpu::BufferUsage::CopyDst;
    m_state->cameraBuffer = m_state->device.CreateBuffer(&cameraBufferDesc);

    wgpu::BindGroupLayoutEntry cameraEntry;
    cameraEntry.binding = 0;
    cameraEntry.visibility = wgpu::ShaderStage::Vertex;
    cameraEntry.buffer.type = wgpu::BufferBindingType::Uniform;
    cameraEntry.buffer.minBindingSize = cameraBufferDesc.size;
    wgpu::BindGroupLayoutDescriptor cameraLayoutDesc;
    cameraLayoutDesc.entryCount = 1;
    cameraLayoutDesc.entries = &cameraEntry;
    m_state->cameraLayout = m_state->device.CreateBindGroupLayout(&cameraLayoutDesc);
    wgpu::BindGroupEntry cameraBinding;
    cameraBinding.binding = 0;
    cameraBinding.buffer = m_state->cameraBuffer;
    cameraBinding.size = cameraBufferDesc.size;
    wgpu::BindGroupDescriptor cameraGroupDesc;
    cameraGroupDesc.layout = m_state->cameraLayout;
    cameraGroupDesc.entryCount = 1;
    cameraGroupDesc.entries = &cameraBinding;
    m_state->cameraGroup = m_state->device.CreateBindGroup(&cameraGroupDesc);

    std::array<wgpu::BindGroupLayoutEntry, 2> geometryEntries{};
    for (uint32_t index = 0; index < geometryEntries.size(); ++index) {
        geometryEntries[index].binding = index;
        geometryEntries[index].visibility = wgpu::ShaderStage::Vertex;
        geometryEntries[index].buffer.type = wgpu::BufferBindingType::ReadOnlyStorage;
    }
    wgpu::BindGroupLayoutDescriptor geometryLayoutDesc;
    geometryLayoutDesc.entryCount = geometryEntries.size();
    geometryLayoutDesc.entries = geometryEntries.data();
    m_state->geometryLayout = m_state->device.CreateBindGroupLayout(&geometryLayoutDesc);

    wgpu::ShaderSourceWGSL shaderSource;
    shaderSource.code = kParticleShader;
    wgpu::ShaderModuleDescriptor shaderDesc;
    shaderDesc.nextInChain = &shaderSource;
    const auto shader = m_state->device.CreateShaderModule(&shaderDesc);
    const std::array<wgpu::BindGroupLayout, 2> layouts = {m_state->cameraLayout, m_state->geometryLayout};
    wgpu::PipelineLayoutDescriptor pipelineLayoutDesc;
    pipelineLayoutDesc.bindGroupLayoutCount = layouts.size();
    pipelineLayoutDesc.bindGroupLayouts = layouts.data();
    wgpu::ColorTargetState colorTarget;
    colorTarget.format = colorFormat == rhi::PixelFormat::BGRA8Srgb      ? wgpu::TextureFormat::BGRA8UnormSrgb
                         : colorFormat == rhi::PixelFormat::RGBA8Srgb    ? wgpu::TextureFormat::RGBA8UnormSrgb
                         : colorFormat == rhi::PixelFormat::RGBA8UNorm   ? wgpu::TextureFormat::RGBA8Unorm
                         : colorFormat == rhi::PixelFormat::RGBA16SFloat ? wgpu::TextureFormat::RGBA16Float
                                                                         : wgpu::TextureFormat::BGRA8Unorm;
    wgpu::BlendState blend;
    blend.color.srcFactor = wgpu::BlendFactor::SrcAlpha;
    blend.color.dstFactor = wgpu::BlendFactor::OneMinusSrcAlpha;
    blend.alpha.srcFactor = wgpu::BlendFactor::One;
    blend.alpha.dstFactor = wgpu::BlendFactor::OneMinusSrcAlpha;
    colorTarget.blend = &blend;
    wgpu::FragmentState fragment;
    fragment.module = shader;
    fragment.entryPoint = "fragment_main";
    fragment.targetCount = 1;
    fragment.targets = &colorTarget;
    wgpu::DepthStencilState depth;
    depth.format = wgpu::TextureFormat::Depth24Plus;
    depth.depthWriteEnabled = wgpu::OptionalBool::False;
    depth.depthCompare = wgpu::CompareFunction::LessEqual;
    wgpu::RenderPipelineDescriptor pipelineDesc;
    pipelineDesc.layout = m_state->device.CreatePipelineLayout(&pipelineLayoutDesc);
    pipelineDesc.vertex.module = shader;
    pipelineDesc.vertex.entryPoint = "vertex_main";
    pipelineDesc.fragment = &fragment;
    pipelineDesc.primitive.topology = wgpu::PrimitiveTopology::TriangleList;
    pipelineDesc.primitive.cullMode = wgpu::CullMode::None;
    pipelineDesc.depthStencil = &depth;
    m_state->renderPipeline = m_state->device.CreateRenderPipeline(&pipelineDesc);
    if (!m_state->cameraBuffer || !m_state->cameraLayout || !m_state->cameraGroup || !m_state->geometryLayout ||
        !m_state->renderPipeline) {
        m_state->error = "WebGPU failed to create the particle render pipeline";
        Shutdown();
        return false;
    }
    std::printf("INFERNUX_WEBGPU_PARTICLE_RUNTIME_READY\n");
    m_state->reportedReady = true;
    return true;
}

void WebParticleRuntime::Shutdown() noexcept
{
    if (m_state)
        m_state->Clear();
}

std::string WebParticleRuntime::ReplaceGraph(uint64_t graphInstanceId, PyObject *programs, PyObject *removeIds)
{
    if (!m_state->rhi || graphInstanceId == 0)
        return "WebGPU particle runtime is not initialized";
    PyObject *programSequence = PySequence_Fast(programs, "particle programs must be a sequence");
    PyObject *removeSequence = PySequence_Fast(removeIds, "particle remove_ids must be a sequence");
    if (!programSequence || !removeSequence) {
        PyErr_Clear();
        Py_XDECREF(programSequence);
        Py_XDECREF(removeSequence);
        return "WebGPU particle graph publication contains an invalid sequence";
    }
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(removeSequence); ++index) {
        uint64_t id = 0;
        if (!ReadUnsigned(PySequence_Fast_GET_ITEM(removeSequence, index), id)) {
            Py_DECREF(programSequence);
            Py_DECREF(removeSequence);
            return "WebGPU particle remove_ids contains an invalid emitter id";
        }
        auto found = m_state->emitters.find(id);
        if (found != m_state->emitters.end()) {
            m_state->ReleaseEmitter(*found->second);
            m_state->emitters.erase(found);
        }
    }
    Py_DECREF(removeSequence);

    const Py_ssize_t programCount = PySequence_Fast_GET_SIZE(programSequence);
    if (programCount == 0) {
        Py_DECREF(programSequence);
        auto graph = m_state->graphs.find(graphInstanceId);
        if (graph != m_state->graphs.end()) {
            m_state->ReleaseGraph(graph->second);
            m_state->graphs.erase(graph);
        }
        return {};
    }

    uint32_t slotCount = 0;
    std::vector<uint32_t> parameters;
    std::vector<uint64_t> graphEmitterIds;
    for (Py_ssize_t index = 0; index < programCount; ++index) {
        PyObject *program = PySequence_Fast_GET_ITEM(programSequence, index);
        std::string error;
        uint64_t id = 0;
        uint64_t encodedGraph = 0;
        uint32_t slot = 0;
        if (!ProgramUsesPortableSubset(program, error) || !ReadUnsigned(Field(program, "id"), id) || id == 0 ||
            !ReadUnsigned(Field(program, "graph_instance_id"), encodedGraph) || encodedGraph != graphInstanceId ||
            !ReadUnsigned(Field(program, "graph_emitter_index"), slot) || slot >= 1024) {
            Py_DECREF(programSequence);
            return error.empty() ? "WebGPU particle program identity is invalid" : error;
        }
        std::vector<uint32_t> words;
        if (!ReadWordVector(Field(program, "parameter_words"), words)) {
            Py_DECREF(programSequence);
            return "WebGPU particle parameter block is invalid";
        }
        if (parameters.empty())
            parameters = std::move(words);
        else if (parameters != words) {
            Py_DECREF(programSequence);
            return "WebGPU particle emitters in one graph disagree on parameters";
        }
        slotCount = std::max(slotCount, slot + 1);
        graphEmitterIds.push_back(id);
    }
    if (parameters.empty())
        parameters.assign(4, 0);

    for (auto iterator = m_state->emitters.begin(); iterator != m_state->emitters.end();) {
        if (iterator->second->graphId != graphInstanceId) {
            ++iterator;
            continue;
        }
        m_state->ReleaseEmitter(*iterator->second);
        iterator = m_state->emitters.erase(iterator);
    }
    auto oldGraph = m_state->graphs.find(graphInstanceId);
    if (oldGraph != m_state->graphs.end()) {
        m_state->ReleaseGraph(oldGraph->second);
        m_state->graphs.erase(oldGraph);
    }

    State::Graph graph;
    graph.id = graphInstanceId;
    graph.slotCount = slotCount;
    graph.parameterWords = parameters;
    std::vector<uint32_t> zeros(slotCount, 0);
    std::vector<uint32_t> playing(slotCount, 1);
    graph.burstRequests = CreateStorageBuffer(*m_state->rhi, slotCount * sizeof(uint32_t), false, zeros.data());
    graph.spawnMetadata = CreateStorageBuffer(*m_state->rhi, slotCount * sizeof(WebSpawnMetadata), true);
    graph.parameters =
        CreateStorageBuffer(*m_state->rhi, parameters.size() * sizeof(uint32_t), false, parameters.data());
    graph.playingRequests = CreateStorageBuffer(*m_state->rhi, slotCount * sizeof(uint32_t), false, zeros.data());
    graph.playingStates = CreateStorageBuffer(*m_state->rhi, slotCount * sizeof(uint32_t), false, playing.data());
    if (!graph.burstRequests.IsValid() || !graph.spawnMetadata.IsValid() || !graph.parameters.IsValid() ||
        !graph.playingRequests.IsValid() || !graph.playingStates.IsValid()) {
        m_state->ReleaseGraph(graph);
        Py_DECREF(programSequence);
        return "WebGPU particle graph buffers could not be created";
    }
    m_state->graphs.emplace(graphInstanceId, std::move(graph));
    auto &publishedGraph = m_state->graphs.at(graphInstanceId);

    for (Py_ssize_t index = 0; index < programCount; ++index) {
        PyObject *program = PySequence_Fast_GET_ITEM(programSequence, index);
        auto emitter = std::make_unique<State::Emitter>();
        uint32_t capacity = 0;
        uint32_t stateStride = 0;
        uint32_t eventTypes = 0;
        uint32_t slot = 0;
        if (!ReadUnsigned(Field(program, "id"), emitter->id) ||
            !ReadUnsigned(Field(program, "graph_emitter_index"), slot) ||
            !ReadUnsigned(Field(program, "artifact_revision"), emitter->artifactRevision) ||
            !ReadUnsigned(Field(program, "capacity"), capacity) || capacity == 0 || capacity > (1u << 24u) ||
            !ReadUnsigned(Field(program, "state_stride"), stateStride) || stateStride == 0 || stateStride % 16 != 0 ||
            !ReadUnsigned(Field(program, "event_type_count"), eventTypes)) {
            Py_DECREF(programSequence);
            return "WebGPU particle program layout is invalid";
        }
        PyObject *hashObject = Field(program, "kernel_hash");
        if (!hashObject || !PyUnicode_Check(hashObject)) {
            Py_DECREF(programSequence);
            return "WebGPU particle kernel hash is missing";
        }
        const char *kernelHash = PyUnicode_AsUTF8(hashObject);
        if (!kernelHash || std::strlen(kernelHash) != 64) {
            Py_DECREF(programSequence);
            return "WebGPU particle kernel hash is invalid";
        }
        particle::GpuEmitterDesc desc;
        desc.capacity = capacity;
        desc.stateStride = stateStride;
        desc.eventTypeCount = eventTypes;
        // The fused Vulkan kernel exceeds the portable WebGPU per-stage
        // storage-buffer budget. Keep simulation and render export as distinct
        // GPU stages so each pipeline publishes only the resources it uses.
        desc.supportsFusedUpdateRendering = false;
        std::array<std::string, static_cast<size_t>(particle::GpuKernelStage::Count)> sources;
        for (size_t stage = 0; stage < sources.size(); ++stage) {
            const std::string name = std::string("Particle/") + kernelHash + "/" + kStageNames[stage];
            if (!InfernuxWebFindShaderSource(name, "compute", sources[stage])) {
                Py_DECREF(programSequence);
                return "WebGPU particle shader catalog is missing " + name;
            }
            desc.kernels[stage] = particle::ShaderBytecode::FromWgsl(sources[stage].data(), sources[stage].size());
        }
        emitter->graphId = graphInstanceId;
        emitter->slot = slot;
        emitter->runtime = std::make_unique<particle::ParticleGpuRuntime>();
        if (!emitter->runtime->Create(*m_state->rhi, desc)) {
            Py_DECREF(programSequence);
            return "WebGPU particle compute pipelines could not be created: " + m_state->rhi->LastError();
        }
        const particle::GpuParticleSimulationControl simulationControl{};
        particle::GpuParticleSimulationControl enabledControl = simulationControl;
        enabledControl.anyViewVisible = 1;
        enabledControl.simulationAllowed = 1;
        if (!m_state->rhi->WriteBuffer(emitter->runtime->SimulationControlBuffer(), 0, &enabledControl,
                                       sizeof(enabledControl))) {
            Py_DECREF(programSequence);
            return "WebGPU particle simulation control could not be initialized";
        }
        rhi::BindGroupDesc spawnGroupDesc;
        spawnGroupDesc.layout = emitter->runtime->GraphSpawnLayout();
        spawnGroupDesc.buffers[spawnGroupDesc.bufferCount++] = {0, rhi::BindingType::StorageBuffer,
                                                                publishedGraph.burstRequests};
        spawnGroupDesc.buffers[spawnGroupDesc.bufferCount++] = {
            1, rhi::BindingType::StorageBuffer, publishedGraph.spawnMetadata,
            static_cast<uint64_t>(slot) * sizeof(WebSpawnMetadata), sizeof(WebSpawnMetadata)};
        spawnGroupDesc.buffers[spawnGroupDesc.bufferCount++] = {2, rhi::BindingType::StorageBuffer,
                                                                publishedGraph.parameters};
        spawnGroupDesc.buffers[spawnGroupDesc.bufferCount++] = {3, rhi::BindingType::StorageBuffer,
                                                                publishedGraph.playingRequests};
        spawnGroupDesc.buffers[spawnGroupDesc.bufferCount++] = {4, rhi::BindingType::StorageBuffer,
                                                                publishedGraph.playingStates};
        emitter->spawnGroup = m_state->rhi->CreateBindGroup(spawnGroupDesc);

        const auto instances = m_state->rhi->GetNativeBuffer(emitter->runtime->InstanceBuffer());
        const auto indices = m_state->rhi->GetNativeBuffer(emitter->runtime->RenderIndexBuffer());
        std::array<wgpu::BindGroupEntry, 2> geometryBindings{};
        geometryBindings[0].binding = 0;
        geometryBindings[0].buffer = instances;
        geometryBindings[0].size = static_cast<uint64_t>(capacity) * particle::ParticleGpuRuntime::RenderInstanceStride;
        geometryBindings[1].binding = 1;
        geometryBindings[1].buffer = indices;
        geometryBindings[1].size = static_cast<uint64_t>(capacity) * sizeof(uint32_t);
        wgpu::BindGroupDescriptor geometryGroupDesc;
        geometryGroupDesc.layout = m_state->geometryLayout;
        geometryGroupDesc.entryCount = geometryBindings.size();
        geometryGroupDesc.entries = geometryBindings.data();
        emitter->renderGroup = m_state->device.CreateBindGroup(&geometryGroupDesc);
        if (!emitter->spawnGroup.IsValid() || !emitter->renderGroup) {
            Py_DECREF(programSequence);
            return "WebGPU particle bind groups could not be created";
        }
        m_state->emitters.emplace(emitter->id, std::move(emitter));
    }
    Py_DECREF(programSequence);
    return {};
}

std::string WebParticleRuntime::UpdateParameters(uint64_t graphInstanceId, PyObject *words)
{
    auto found = m_state->graphs.find(graphInstanceId);
    std::vector<uint32_t> decoded;
    if (found == m_state->graphs.end() || !ReadWordVector(words, decoded) ||
        decoded.size() != found->second.parameterWords.size())
        return "WebGPU particle parameter update does not match a live graph";
    if (!m_state->rhi->WriteBuffer(found->second.parameters, 0, decoded.data(), decoded.size() * sizeof(uint32_t)))
        return "WebGPU particle parameter upload failed";
    found->second.parameterWords = std::move(decoded);
    return {};
}

bool WebParticleRuntime::BeginBatch(uint64_t graphInstanceId, PyObject *items)
{
    if (!m_state->rhi || m_state->graphs.find(graphInstanceId) == m_state->graphs.end())
        return false;
    PyObject *sequence = PySequence_Fast(items, "particle frame items must be a sequence");
    if (!sequence) {
        PyErr_Clear();
        return false;
    }
    bool accepted = true;
    for (Py_ssize_t index = 0; accepted && index < PySequence_Fast_GET_SIZE(sequence); ++index) {
        PyObject *item = PySequence_Fast_GET_ITEM(sequence, index);
        uint64_t emitterId = 0;
        accepted = ReadUnsigned(Field(item, "emitter_id"), emitterId);
        auto found = m_state->emitters.find(emitterId);
        accepted = accepted && found != m_state->emitters.end() && found->second->graphId == graphInstanceId &&
                   ReadTransforms(Field(item, "transforms"), found->second->transforms);
        if (!accepted)
            break;
        found->second->pending.clear();
        PyObject *preroll = PySequence_Fast(Field(item, "preroll_steps"), "particle preroll must be a sequence");
        if (!preroll || PySequence_Fast_GET_SIZE(preroll) > 4096) {
            PyErr_Clear();
            Py_XDECREF(preroll);
            accepted = false;
            break;
        }
        for (Py_ssize_t step = 0; accepted && step < PySequence_Fast_GET_SIZE(preroll); ++step) {
            FrameRequest request;
            accepted = DecodeRequest(PySequence_Fast_GET_ITEM(preroll, step), request, true);
            if (accepted)
                found->second->pending.push_back(request);
        }
        Py_DECREF(preroll);
        FrameRequest request;
        accepted = accepted && DecodeRequest(item, request, false);
        if (accepted)
            found->second->pending.push_back(request);
    }
    Py_DECREF(sequence);
    return accepted;
}

bool WebParticleRuntime::SetPlaying(uint64_t emitterId, bool playing)
{
    auto found = m_state->emitters.find(emitterId);
    if (found == m_state->emitters.end())
        return false;
    auto graph = m_state->graphs.find(found->second->graphId);
    if (graph == m_state->graphs.end())
        return false;
    const uint32_t value = playing ? 1u : 0u;
    if (!m_state->rhi->WriteBuffer(graph->second.playingStates,
                                   static_cast<uint64_t>(found->second->slot) * sizeof(uint32_t), &value,
                                   sizeof(value)))
        return false;
    found->second->playing = playing;
    return true;
}

bool WebParticleRuntime::Reset(uint64_t emitterId)
{
    auto found = m_state->emitters.find(emitterId);
    if (found == m_state->emitters.end())
        return false;
    found->second->resetPending = true;
    return true;
}

uint64_t WebParticleRuntime::ArtifactRevision(uint64_t emitterId) const noexcept
{
    const auto found = m_state->emitters.find(emitterId);
    return found == m_state->emitters.end() ? 0 : found->second->artifactRevision;
}

bool WebParticleRuntime::StateWasPreserved(uint64_t emitterId) const noexcept
{
    const auto found = m_state->emitters.find(emitterId);
    return found != m_state->emitters.end() && found->second->stateWasPreserved;
}

void WebParticleRuntime::RecordCompute(wgpu::CommandEncoder commandEncoder)
{
    if (!m_state->rhi || !commandEncoder)
        return;
    bool hasWork = false;
    for (const auto &[_, emitter] : m_state->emitters)
        hasWork = hasWork || !emitter->pending.empty() || emitter->resetPending;
    if (!hasWork)
        return;
    const auto recordStage = [&](const auto &record) {
        wgpu::ComputePassDescriptor passDesc;
        auto pass = commandEncoder.BeginComputePass(&passDesc);
        WebGpuComputeCommandContext context;
        const auto commands = m_state->rhi->MakeComputeCommandEncoder(context, pass);
        const bool recorded = record(commands);
        pass.End();
        return recorded;
    };
    for (auto &[_, emitter] : m_state->emitters) {
        State::Emitter *currentEmitter = emitter.get();
        if (currentEmitter->resetPending) {
            currentEmitter->runtime->RequestBootstrap();
            currentEmitter->resetPending = false;
            currentEmitter->drawReady = false;
        }
        if (currentEmitter->pending.empty())
            continue;
        if (!currentEmitter->runtime->UpdateTransforms(currentEmitter->transforms)) {
            m_state->error = "WebGPU particle transform upload failed";
            currentEmitter->pending.clear();
            continue;
        }
        for (const FrameRequest &request : currentEmitter->pending) {
            if (currentEmitter->runtime->NeedsBootstrap() && !recordStage([&](const auto &commands) {
                    return currentEmitter->runtime->RecordBootstrap(commands, request.systemSeed,
                                                                    currentEmitter->spawnGroup);
                })) {
                m_state->error = "WebGPU particle bootstrap recording failed";
                break;
            }
            if (request.simulate && request.spawnCount > 0) {
                if (!recordStage([&](const auto &commands) {
                        currentEmitter->runtime->RecordInitIndirect(commands, request.spawnCount, request.spawnBaseId,
                                                                    request.spawnGeneration, request.systemSeed,
                                                                    request.simulationStep, request.deltaTime,
                                                                    currentEmitter->spawnGroup, {}, 0);
                        return true;
                    })) {
                    m_state->error = "WebGPU particle initialization recording failed";
                    break;
                }
            }
            if (request.simulate) {
                const bool resetRecorded = recordStage([&](const auto &commands) {
                    return currentEmitter->runtime->RecordRenderReset(commands, currentEmitter->spawnGroup, false,
                                                                      true);
                });
                const bool updateRecorded = resetRecorded && recordStage([&](const auto &commands) {
                                                return currentEmitter->runtime->RecordUpdate(
                                                    commands, request.systemSeed, request.simulationStep,
                                                    request.deltaTime, currentEmitter->spawnGroup);
                                            });
                if (!updateRecorded) {
                    m_state->error = "WebGPU particle simulation recording failed";
                    break;
                }
                currentEmitter->runtime->PublishAliveWrite();
                if (request.render && !recordStage([&](const auto &commands) {
                        return currentEmitter->runtime->RecordRendering(
                            commands, request.systemSeed, request.simulationStep, currentEmitter->spawnGroup);
                    })) {
                    m_state->error = "WebGPU particle rendering export failed";
                    break;
                }
            } else if (request.render) {
                const bool resetRecorded = recordStage([&](const auto &commands) {
                    return currentEmitter->runtime->RecordRenderReset(commands, currentEmitter->spawnGroup);
                });
                const bool renderingRecorded =
                    resetRecorded && recordStage([&](const auto &commands) {
                        return currentEmitter->runtime->RecordRendering(
                            commands, request.systemSeed, request.simulationStep, currentEmitter->spawnGroup);
                    });
                if (!renderingRecorded) {
                    m_state->error = "WebGPU particle rendering export failed";
                    break;
                }
            }
            currentEmitter->drawReady = request.render;
        }
        currentEmitter->pending.clear();
    }
}

bool WebParticleRuntime::Render(wgpu::RenderPassEncoder pass, uint32_t width, uint32_t height)
{
    if (!m_state->renderPipeline || !pass)
        return false;
    Scene *scene = SceneManager::Instance().GetActiveScene();
    Camera *camera = scene ? scene->FindGameCamera(nullptr) : nullptr;
    if (!camera)
        return false;
    camera->SetAspectRatio(static_cast<float>(width) / static_cast<float>(std::max(1u, height)));
    struct CameraData
    {
        glm::mat4 viewProjection{1.0f};
        glm::vec4 right{1.0f, 0.0f, 0.0f, 0.0f};
        glm::vec4 up{0.0f, 1.0f, 0.0f, 0.0f};
    } data;
    data.viewProjection = ToWebClipSpace(camera->GetViewProjectionMatrix());
    const glm::mat4 cameraToWorld = glm::inverse(camera->GetViewMatrix());
    data.right = glm::vec4(glm::normalize(glm::vec3(cameraToWorld[0])), 0.0f);
    data.up = glm::vec4(glm::normalize(glm::vec3(cameraToWorld[1])), 0.0f);
    m_state->queue.WriteBuffer(m_state->cameraBuffer, 0, &data, sizeof(data));
    pass.SetPipeline(m_state->renderPipeline);
    pass.SetBindGroup(0, m_state->cameraGroup);
    bool drew = false;
    for (const auto &[_, emitter] : m_state->emitters) {
        if (!emitter->drawReady || !emitter->playing || !emitter->renderGroup)
            continue;
        const auto indirect = m_state->rhi->GetNativeBuffer(emitter->runtime->IndirectBuffer());
        if (!indirect)
            continue;
        pass.SetBindGroup(1, emitter->renderGroup);
        pass.DrawIndirect(indirect, 0);
        drew = true;
    }
    if (drew && !m_state->reportedDraw) {
        std::printf("INFERNUX_WEBGPU_PARTICLE_DRAW_READY emitters=%zu\n", m_state->emitters.size());
        m_state->reportedDraw = true;
    }
    return drew;
}

const std::string &WebParticleRuntime::LastError() const noexcept
{
    return m_state->error;
}

} // namespace infernux::web
