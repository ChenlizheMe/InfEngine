#include "WebSceneRenderer.h"

#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/Camera.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>

#include <glm/gtc/matrix_inverse.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>

namespace infernux::web
{
namespace
{

constexpr char kSceneShader[] = R"wgsl(
struct CameraData {
    view_projection: mat4x4<f32>,
};

@group(0) @binding(0) var<uniform> camera: CameraData;

struct VertexInput {
    @location(0) position: vec3<f32>,
    @location(1) normal: vec3<f32>,
    @location(2) color: vec4<f32>,
};

struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) normal: vec3<f32>,
    @location(1) color: vec4<f32>,
};

@vertex
fn vertex_main(input: VertexInput) -> VertexOutput {
    var output: VertexOutput;
    output.position = camera.view_projection * vec4<f32>(input.position, 1.0);
    output.normal = input.normal;
    output.color = input.color;
    return output;
}

@fragment
fn fragment_main(input: VertexOutput) -> @location(0) vec4<f32> {
    let normal = normalize(input.normal);
    let light_direction = normalize(vec3<f32>(0.35, 0.82, 0.45));
    let diffuse = max(dot(normal, light_direction), 0.0);
    let lighting = 0.24 + diffuse * 0.76;
    return vec4<f32>(input.color.rgb * lighting, input.color.a);
}
)wgsl";

glm::vec4 MaterialColor(const std::shared_ptr<InxMaterial> &material)
{
    if (!material)
        return glm::vec4(1.0f);
    const MaterialProperty *property = material->GetProperty("baseColor");
    if (!property)
        return glm::vec4(1.0f);
    if (property->type == MaterialPropertyType::Color || property->type == MaterialPropertyType::Float4) {
        if (const auto *value = std::get_if<glm::vec4>(&property->value))
            return *value;
    }
    if (property->type == MaterialPropertyType::Float3) {
        if (const auto *value = std::get_if<glm::vec3>(&property->value))
            return glm::vec4(*value, 1.0f);
    }
    return glm::vec4(1.0f);
}

bool Finite(const glm::vec3 &value)
{
    return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

glm::mat4 SkinMatrix(const Vertex &vertex, const std::vector<glm::mat4> *palette)
{
    if (!palette)
        return glm::mat4(1.0f);
    glm::mat4 result(0.0f);
    float totalWeight = 0.0f;
    for (uint32_t slot = 0; slot < 4; ++slot) {
        const float weight = vertex.boneWeights[slot];
        const uint32_t bone = vertex.boneIndices[slot];
        if (!(weight > 0.0f) || bone >= palette->size())
            continue;
        result += (*palette)[bone] * weight;
        totalWeight += weight;
    }
    return totalWeight > 0.0f ? result : glm::mat4(1.0f);
}

uint64_t GrowCapacity(uint64_t required)
{
    uint64_t capacity = 4096;
    while (capacity < required && capacity <= std::numeric_limits<uint64_t>::max() / 2)
        capacity *= 2;
    return std::max(capacity, required);
}

} // namespace

bool WebSceneRenderer::Initialize(wgpu::Device device, wgpu::Queue queue, wgpu::TextureFormat colorFormat)
{
    m_device = std::move(device);
    m_queue = std::move(queue);
    m_colorFormat = colorFormat;
    if (!m_device || !m_queue || colorFormat == wgpu::TextureFormat::Undefined)
        return false;

    wgpu::BufferDescriptor cameraBufferDescriptor;
    cameraBufferDescriptor.size = sizeof(glm::mat4);
    cameraBufferDescriptor.usage = wgpu::BufferUsage::Uniform | wgpu::BufferUsage::CopyDst;
    m_cameraBuffer = m_device.CreateBuffer(&cameraBufferDescriptor);
    if (!m_cameraBuffer)
        return false;

    wgpu::BindGroupLayoutEntry cameraEntry;
    cameraEntry.binding = 0;
    cameraEntry.visibility = wgpu::ShaderStage::Vertex;
    cameraEntry.buffer.type = wgpu::BufferBindingType::Uniform;
    cameraEntry.buffer.minBindingSize = sizeof(glm::mat4);
    wgpu::BindGroupLayoutDescriptor cameraLayoutDescriptor;
    cameraLayoutDescriptor.entryCount = 1;
    cameraLayoutDescriptor.entries = &cameraEntry;
    m_cameraLayout = m_device.CreateBindGroupLayout(&cameraLayoutDescriptor);

    wgpu::BindGroupEntry cameraBinding;
    cameraBinding.binding = 0;
    cameraBinding.buffer = m_cameraBuffer;
    cameraBinding.size = sizeof(glm::mat4);
    wgpu::BindGroupDescriptor cameraGroupDescriptor;
    cameraGroupDescriptor.layout = m_cameraLayout;
    cameraGroupDescriptor.entryCount = 1;
    cameraGroupDescriptor.entries = &cameraBinding;
    m_cameraGroup = m_device.CreateBindGroup(&cameraGroupDescriptor);
    return m_cameraLayout && m_cameraGroup && CreatePipeline();
}

bool WebSceneRenderer::CreatePipeline()
{
    wgpu::ShaderSourceWGSL shaderSource;
    shaderSource.code = kSceneShader;
    wgpu::ShaderModuleDescriptor shaderDescriptor;
    shaderDescriptor.nextInChain = &shaderSource;
    const wgpu::ShaderModule shader = m_device.CreateShaderModule(&shaderDescriptor);
    if (!shader)
        return false;

    std::array<wgpu::VertexAttribute, 3> attributes{};
    attributes[0].format = wgpu::VertexFormat::Float32x3;
    attributes[0].offset = offsetof(WebVertex, position);
    attributes[0].shaderLocation = 0;
    attributes[1].format = wgpu::VertexFormat::Float32x3;
    attributes[1].offset = offsetof(WebVertex, normal);
    attributes[1].shaderLocation = 1;
    attributes[2].format = wgpu::VertexFormat::Float32x4;
    attributes[2].offset = offsetof(WebVertex, color);
    attributes[2].shaderLocation = 2;
    wgpu::VertexBufferLayout vertexLayout;
    vertexLayout.arrayStride = sizeof(WebVertex);
    vertexLayout.stepMode = wgpu::VertexStepMode::Vertex;
    vertexLayout.attributeCount = attributes.size();
    vertexLayout.attributes = attributes.data();

    wgpu::ColorTargetState colorTarget;
    colorTarget.format = m_colorFormat;
    colorTarget.writeMask = wgpu::ColorWriteMask::All;
    wgpu::FragmentState fragment;
    fragment.module = shader;
    fragment.entryPoint = "fragment_main";
    fragment.targetCount = 1;
    fragment.targets = &colorTarget;

    wgpu::DepthStencilState depth;
    depth.format = wgpu::TextureFormat::Depth24Plus;
    depth.depthWriteEnabled = wgpu::OptionalBool::True;
    depth.depthCompare = wgpu::CompareFunction::LessEqual;

    wgpu::PipelineLayoutDescriptor pipelineLayoutDescriptor;
    pipelineLayoutDescriptor.bindGroupLayoutCount = 1;
    pipelineLayoutDescriptor.bindGroupLayouts = &m_cameraLayout;

    wgpu::RenderPipelineDescriptor pipelineDescriptor;
    pipelineDescriptor.layout = m_device.CreatePipelineLayout(&pipelineLayoutDescriptor);
    pipelineDescriptor.vertex.module = shader;
    pipelineDescriptor.vertex.entryPoint = "vertex_main";
    pipelineDescriptor.vertex.bufferCount = 1;
    pipelineDescriptor.vertex.buffers = &vertexLayout;
    pipelineDescriptor.fragment = &fragment;
    pipelineDescriptor.primitive.topology = wgpu::PrimitiveTopology::TriangleList;
    pipelineDescriptor.primitive.frontFace = wgpu::FrontFace::CW;
    pipelineDescriptor.primitive.cullMode = wgpu::CullMode::None;
    pipelineDescriptor.depthStencil = &depth;
    pipelineDescriptor.multisample.count = 1;
    m_pipeline = m_device.CreateRenderPipeline(&pipelineDescriptor);
    return static_cast<bool>(m_pipeline);
}

void WebSceneRenderer::Resize(uint32_t width, uint32_t height)
{
    width = std::max(1u, width);
    height = std::max(1u, height);
    if (m_depthTexture && width == m_depthWidth && height == m_depthHeight)
        return;

    wgpu::TextureDescriptor descriptor;
    descriptor.dimension = wgpu::TextureDimension::e2D;
    descriptor.size = {width, height, 1};
    descriptor.format = wgpu::TextureFormat::Depth24Plus;
    descriptor.mipLevelCount = 1;
    descriptor.sampleCount = 1;
    descriptor.usage = wgpu::TextureUsage::RenderAttachment;
    m_depthTexture = m_device.CreateTexture(&descriptor);
    m_depthView = m_depthTexture ? m_depthTexture.CreateView() : wgpu::TextureView{};
    m_depthWidth = width;
    m_depthHeight = height;
}

bool WebSceneRenderer::HasDepthTarget() const noexcept
{
    return static_cast<bool>(m_depthView);
}

wgpu::TextureView WebSceneRenderer::GetDepthView() const noexcept
{
    return m_depthView;
}

bool WebSceneRenderer::EnsureBuffer(wgpu::Buffer &buffer, uint64_t &capacity, uint64_t required,
                                    wgpu::BufferUsage usage)
{
    if (required == 0)
        return false;
    if (buffer && required <= capacity)
        return true;
    capacity = GrowCapacity(required);
    wgpu::BufferDescriptor descriptor;
    descriptor.size = capacity;
    descriptor.usage = usage | wgpu::BufferUsage::CopyDst;
    buffer = m_device.CreateBuffer(&descriptor);
    return static_cast<bool>(buffer);
}

bool WebSceneRenderer::BuildFrame(uint32_t width, uint32_t height)
{
    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene)
        return false;
    Camera *camera = scene->FindGameCamera(nullptr);
    if (!camera)
        return false;

    camera->SetAspectRatio(static_cast<float>(width) / static_cast<float>(std::max(1u, height)));
    const size_t visibleCount = m_extractor.ExtractCameraFrame(m_world, camera);
    if (visibleCount == 0)
        return false;
    const auto frame = m_world.Acquire();
    if (!frame || !frame->PrimaryView().valid)
        return false;

    m_vertices.clear();
    m_indices.clear();
    const auto &drawCalls = frame->DrawCalls().drawCalls;
    for (const DrawCall &draw : drawCalls) {
        if (!draw.frustumVisible || !draw.meshVertices || !draw.meshIndices || draw.indexCount == 0)
            continue;
        const auto &sourceVertices = *draw.meshVertices;
        const auto &sourceIndices = *draw.meshIndices;
        if (sourceVertices.empty() || sourceIndices.empty() || draw.indexStart >= sourceIndices.size())
            continue;
        const uint32_t indexCount =
            static_cast<uint32_t>(std::min<size_t>(draw.indexCount, sourceIndices.size() - draw.indexStart));
        const uint32_t vertexBase = static_cast<uint32_t>(m_vertices.size());
        const glm::vec4 materialColor = MaterialColor(draw.material);
        const glm::mat3 worldNormal = glm::inverseTranspose(glm::mat3(draw.worldMatrix));

        m_vertices.reserve(m_vertices.size() + sourceVertices.size());
        for (const Vertex &source : sourceVertices) {
            const glm::mat4 skin = SkinMatrix(source, draw.skinBoneMatrices);
            const glm::vec3 localPosition = glm::vec3(skin * glm::vec4(source.pos, 1.0f));
            const glm::vec3 worldPosition = glm::vec3(draw.worldMatrix * glm::vec4(localPosition, 1.0f));
            glm::vec3 localNormal = glm::mat3(skin) * source.normal;
            glm::vec3 worldNormalValue = worldNormal * localNormal;
            if (!Finite(worldNormalValue) || glm::dot(worldNormalValue, worldNormalValue) < 1.0e-8f)
                worldNormalValue = glm::vec3(0.0f, 1.0f, 0.0f);
            else
                worldNormalValue = glm::normalize(worldNormalValue);
            const glm::vec4 color = glm::vec4(source.color, 1.0f) * materialColor;
            WebVertex vertex{};
            std::memcpy(vertex.position, &worldPosition, sizeof(vertex.position));
            std::memcpy(vertex.normal, &worldNormalValue, sizeof(vertex.normal));
            std::memcpy(vertex.color, &color, sizeof(vertex.color));
            m_vertices.push_back(vertex);
        }

        m_indices.reserve(m_indices.size() + indexCount);
        for (uint32_t index = 0; index < indexCount; ++index) {
            const uint32_t sourceIndex = sourceIndices[draw.indexStart + index];
            if (sourceIndex >= sourceVertices.size())
                continue;
            m_indices.push_back(vertexBase + sourceIndex);
        }
    }

    if (m_vertices.empty() || m_indices.empty())
        return false;
    m_queue.WriteBuffer(m_cameraBuffer, 0, &frame->PrimaryView().viewProjection, sizeof(glm::mat4));
    return true;
}

bool WebSceneRenderer::Render(wgpu::RenderPassEncoder pass, uint32_t width, uint32_t height)
{
    if (!m_pipeline || !pass || !BuildFrame(width, height))
        return false;
    const uint64_t vertexBytes = m_vertices.size() * sizeof(WebVertex);
    const uint64_t indexBytes = m_indices.size() * sizeof(uint32_t);
    if (!EnsureBuffer(m_vertexBuffer, m_vertexCapacity, vertexBytes, wgpu::BufferUsage::Vertex) ||
        !EnsureBuffer(m_indexBuffer, m_indexCapacity, indexBytes, wgpu::BufferUsage::Index))
        return false;
    m_queue.WriteBuffer(m_vertexBuffer, 0, m_vertices.data(), vertexBytes);
    m_queue.WriteBuffer(m_indexBuffer, 0, m_indices.data(), indexBytes);

    pass.SetPipeline(m_pipeline);
    pass.SetBindGroup(0, m_cameraGroup);
    pass.SetVertexBuffer(0, m_vertexBuffer, 0, vertexBytes);
    pass.SetIndexBuffer(m_indexBuffer, wgpu::IndexFormat::Uint32, 0, indexBytes);
    pass.DrawIndexed(static_cast<uint32_t>(m_indices.size()), 1, 0, 0, 0);
    if (!m_reportedFirstFrame) {
        std::printf("INFERNUX_WEB_SCENE_RENDER_READY vertices=%zu indices=%zu\n", m_vertices.size(), m_indices.size());
        m_reportedFirstFrame = true;
    }
    return true;
}

} // namespace infernux::web
