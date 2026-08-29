#include "InxRenderer.h"
#include "CaptureService.h"
// Explicit includes for all subsystem types that were forward-declared in
// InxRenderer.h. These are required here so that unique_ptr destructors,
// make_unique calls, and member-function calls all have complete types.
#include "EditorGizmos.h"
#include "EditorTools.h"
#include "GizmosDrawCallBuffer.h"
#include "InxVkCoreModular.h"
#include "MsaaPolicy.h"
#include "OutlineRenderer.h"
#include "ScenePickingService.h"
#include "SceneRenderGraph.h"
#include "SceneRenderTarget.h"
#include "ScriptableRenderContext.h"
#include "TransientResourcePool.h"
#include "gui/InxGUI.h"
#include "gui/InxGUIContext.h"
#include "gui/InxGUISemantics.h"
#include "gui/InxScreenUIRenderer.h"
#include "particle/ParticleGpuBounds.h"
#include "particle/ParticleGpuCollisionScene.h"
#include "particle/ParticleGpuCuller.h"
#include "particle/ParticleGpuDrawRegistry.h"
#include "particle/ParticleGpuMigrator.h"
#include "particle/ParticleGpuRibbonRenderer.h"
#include "particle/ParticleGpuRibbonTopology.h"
#include "particle/ParticleGpuSorter.h"
#include "particle/ParticleGpuSystemManager.h"
#include "vk/RenderGraph.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkHandle.h"
#include "vk/VmaContext.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <core/config/MathConstants.h>
#include <core/threading/JobSystem.h>
#include <cstdlib>
#include <cstring>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxMesh/InxMesh.h>
#include <function/scene/BoxCollider.h>
#include <function/scene/Camera.h>
#include <function/scene/CapsuleCollider.h>
#include <function/scene/Collider.h>
#include <function/scene/GameObject.h>
#include <function/scene/Light.h>
#include <function/scene/LightingData.h>
#include <function/scene/MeshCollider.h>
#include <function/scene/MeshRenderer.h>
#include <function/scene/PrimitiveMeshes.h>
#include <function/scene/Rigidbody.h>
#include <function/scene/SceneManager.h>
#include <function/scene/SceneRenderBridge.h>
#include <function/scene/SkinnedMeshRenderer.h>
#include <function/scene/SphereCollider.h>
#include <function/scene/TransformECSStore.h>
#include <function/scene/physics/PhysicsECSStore.h>
#include <glm/gtc/matrix_transform.hpp>
#include <glm/gtc/type_ptr.hpp>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <platform/filesystem/InxPath.h>
#include <platform/window/InxView.h>
#include <sstream>
#include <stdexcept>
#include <unordered_set>

namespace infernux
{
namespace
{

uint64_t ParticleCollisionStableHash(std::string_view value) noexcept
{
    uint64_t hash = 1469598103934665603ull;
    for (const unsigned char byte : value) {
        hash ^= byte;
        hash *= 1099511628211ull;
    }
    return hash;
}

void ParticleCollisionHashAppend(uint64_t &hash, uint64_t value) noexcept
{
    for (uint32_t byte = 0; byte < 8; ++byte) {
        hash ^= static_cast<uint8_t>(value >> (byte * 8u));
        hash *= 1099511628211ull;
    }
}

bool ParticleCollisionFinite(const glm::mat4 &matrix) noexcept
{
    const float *values = glm::value_ptr(matrix);
    return std::all_of(values, values + 16, [](float value) { return std::isfinite(value); });
}

void StoreParticleCollisionAffine(particle::GpuParticleAffineTransform &destination, const glm::mat4 &matrix)
{
    destination.row0 = {matrix[0][0], matrix[1][0], matrix[2][0], matrix[3][0]};
    destination.row1 = {matrix[0][1], matrix[1][1], matrix[2][1], matrix[3][1]};
    destination.row2 = {matrix[0][2], matrix[1][2], matrix[2][2], matrix[3][2]};
}

void CopyParticleCollisionAffine(particle::GpuParticleAffineTransform &destination, const std::array<float, 12> &source)
{
    std::copy_n(source.begin(), 4, destination.row0.begin());
    std::copy_n(source.begin() + 4, 4, destination.row1.begin());
    std::copy_n(source.begin() + 8, 4, destination.row2.begin());
}

void CopyParticleCollisionAffine(std::array<float, 12> &destination, const particle::GpuParticleAffineTransform &source)
{
    std::copy(source.row0.begin(), source.row0.end(), destination.begin());
    std::copy(source.row1.begin(), source.row1.end(), destination.begin() + 4);
    std::copy(source.row2.begin(), source.row2.end(), destination.begin() + 8);
}

void StoreParticleCollisionAabb(particle::GpuParticleColliderRecord &record, const glm::mat4 &localToWorld,
                                const glm::vec3 &localMin, const glm::vec3 &localMax)
{
    const glm::vec3 localCenter = (localMin + localMax) * 0.5f;
    const glm::vec3 localExtent = (localMax - localMin) * 0.5f;
    const glm::vec3 worldCenter = glm::vec3(localToWorld * glm::vec4(localCenter, 1.0f));
    const glm::mat3 linear(localToWorld);
    const glm::vec3 worldExtent =
        glm::abs(linear[0]) * localExtent.x + glm::abs(linear[1]) * localExtent.y + glm::abs(linear[2]) * localExtent.z;
    const glm::vec3 worldMin = worldCenter - worldExtent;
    const glm::vec3 worldMax = worldCenter + worldExtent;
    record.worldAabbMin = {worldMin.x, worldMin.y, worldMin.z, 0.0f};
    record.worldAabbMax = {worldMax.x, worldMax.y, worldMax.z, 0.0f};
}

struct CompiledParticleSortProgram
{
    std::array<std::vector<uint32_t>, 5> shaders;

    [[nodiscard]] particle::GpuParticleSortProgram View() const noexcept
    {
        return {
            {shaders[0].data(), shaders[0].size()}, {shaders[1].data(), shaders[1].size()},
            {shaders[2].data(), shaders[2].size()}, {shaders[3].data(), shaders[3].size()},
            {shaders[4].data(), shaders[4].size()},
        };
    }
};

struct CompiledParticleCullProgram
{
    std::array<std::vector<uint32_t>, 3> shaders;

    [[nodiscard]] particle::GpuParticleCullProgram View() const noexcept
    {
        return {
            {shaders[0].data(), shaders[0].size()},
            {shaders[1].data(), shaders[1].size()},
            {shaders[2].data(), shaders[2].size()},
        };
    }
};

struct CompiledParticleBoundsProgram
{
    std::array<std::vector<uint32_t>, 3> shaders;

    [[nodiscard]] particle::GpuParticleBoundsProgram View() const noexcept
    {
        return {
            {shaders[0].data(), shaders[0].size()},
            {shaders[1].data(), shaders[1].size()},
            {shaders[2].data(), shaders[2].size()},
        };
    }
};

struct CompiledParticleMigrationProgram
{
    std::array<std::vector<uint32_t>, 2> shaders;

    [[nodiscard]] particle::GpuParticleMigrationProgram View() const noexcept
    {
        return {
            {shaders[0].data(), shaders[0].size()},
            {shaders[1].data(), shaders[1].size()},
        };
    }
};

struct CompiledParticleSpawnProgram
{
    std::array<std::vector<uint32_t>, 2> shaders;

    [[nodiscard]] particle::GpuParticleSpawnProgram View() const noexcept
    {
        return {
            {shaders[0].data(), shaders[0].size()},
            {shaders[1].data(), shaders[1].size()},
        };
    }
};

struct CompiledParticleRibbonTopologyProgram
{
    std::array<std::vector<uint32_t>, 5> shaders;

    [[nodiscard]] particle::GpuParticleRibbonProgram View() const noexcept
    {
        return {
            {shaders[0].data(), shaders[0].size()}, {shaders[1].data(), shaders[1].size()},
            {shaders[2].data(), shaders[2].size()}, {shaders[3].data(), shaders[3].size()},
            {shaders[4].data(), shaders[4].size()},
        };
    }
};

struct CompiledParticleRibbonRenderProgram
{
    std::array<std::vector<uint32_t>, 4> shaders;

    [[nodiscard]] particle::GpuParticleRibbonRenderProgram View() const noexcept
    {
        return {
            {shaders[0].data(), shaders[0].size()},
            {shaders[1].data(), shaders[1].size()},
            {shaders[2].data(), shaders[2].size()},
            {shaders[3].data(), shaders[3].size()},
        };
    }
};

struct CompiledParticleProgramBundle
{
    CompiledParticleSortProgram sort;
    CompiledParticleCullProgram cull;
    CompiledParticleBoundsProgram bounds;
    CompiledParticleMigrationProgram migration;
    CompiledParticleSpawnProgram spawn;
    CompiledParticleRibbonTopologyProgram ribbonTopology;
    CompiledParticleRibbonRenderProgram ribbonRender;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return sort.View().IsValid() && cull.View().IsValid() && bounds.View().IsValid() &&
               migration.View().IsValid() && spawn.View().IsValid() && ribbonTopology.View().IsValid() &&
               ribbonRender.View().IsValid();
    }
};

CompiledParticleSortProgram CompileParticleSortProgram()
{
    InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
    const std::array<std::pair<std::string_view, const char *>, 5> sources = {{
        {particle::GpuParticleSortShaderSources::Small(), "Infernux/ParticleSortSmall.comp"},
        {particle::GpuParticleSortShaderSources::Generate(), "Infernux/ParticleSortGenerate.comp"},
        {particle::GpuParticleSortShaderSources::Histogram(), "Infernux/ParticleSortHistogram.comp"},
        {particle::GpuParticleSortShaderSources::Scan(), "Infernux/ParticleSortScan.comp"},
        {particle::GpuParticleSortShaderSources::Scatter(), "Infernux/ParticleSortScatter.comp"},
    }};
    CompiledParticleSortProgram result;
    for (size_t index = 0; index < sources.size(); ++index) {
        const auto bytes = compiler.CompileComputeGlsl(std::string(sources[index].first), sources[index].second);
        if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0)
            return {};
        result.shaders[index].resize(bytes.size() / sizeof(uint32_t));
        std::memcpy(result.shaders[index].data(), bytes.data(), bytes.size());
    }
    return result;
}

CompiledParticleCullProgram CompileParticleCullProgram()
{
    InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
    const std::array<std::pair<std::string_view, const char *>, 3> sources = {{
        {particle::GpuParticleCullShaderSources::Reset(), "Infernux/ParticleCullReset.comp"},
        {particle::GpuParticleCullShaderSources::Cull(), "Infernux/ParticleCull.comp"},
        {particle::GpuParticleCullShaderSources::Finalize(), "Infernux/ParticleCullFinalize.comp"},
    }};
    CompiledParticleCullProgram result;
    for (size_t index = 0; index < sources.size(); ++index) {
        const auto bytes = compiler.CompileComputeGlsl(std::string(sources[index].first), sources[index].second);
        if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0)
            return {};
        result.shaders[index].resize(bytes.size() / sizeof(uint32_t));
        std::memcpy(result.shaders[index].data(), bytes.data(), bytes.size());
    }
    return result;
}

CompiledParticleBoundsProgram CompileParticleBoundsProgram()
{
    InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
    const std::array<std::pair<std::string_view, const char *>, 3> sources = {{
        {particle::GpuParticleBoundsShaderSources::Prepare(), "Infernux/ParticleVisibilityPrepare.comp"},
        {particle::GpuParticleBoundsShaderSources::Reset(), "Infernux/ParticleBoundsReset.comp"},
        {particle::GpuParticleBoundsShaderSources::Reduce(), "Infernux/ParticleBoundsReduce.comp"},
    }};
    CompiledParticleBoundsProgram result;
    for (size_t index = 0; index < sources.size(); ++index) {
        const auto bytes = compiler.CompileComputeGlsl(std::string(sources[index].first), sources[index].second);
        if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0)
            return {};
        result.shaders[index].resize(bytes.size() / sizeof(uint32_t));
        std::memcpy(result.shaders[index].data(), bytes.data(), bytes.size());
    }
    return result;
}

CompiledParticleMigrationProgram CompileParticleMigrationProgram()
{
    InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
    const std::array<std::pair<std::string_view, const char *>, 2> sources = {{
        {particle::GpuParticleMigrationShaderSources::Reset(), "Infernux/ParticleMigrationReset.comp"},
        {particle::GpuParticleMigrationShaderSources::Migrate(), "Infernux/ParticleMigration.comp"},
    }};
    CompiledParticleMigrationProgram result;
    for (size_t index = 0; index < sources.size(); ++index) {
        const auto bytes = compiler.CompileComputeGlsl(std::string(sources[index].first), sources[index].second);
        if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0)
            return {};
        result.shaders[index].resize(bytes.size() / sizeof(uint32_t));
        std::memcpy(result.shaders[index].data(), bytes.data(), bytes.size());
    }
    return result;
}

CompiledParticleSpawnProgram CompileParticleSpawnProgram()
{
    InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
    const std::array<std::pair<std::string_view, const char *>, 2> sources = {{
        {particle::GpuParticleSpawnShaderSources::Advance(), "Infernux/ParticleSpawnAdvance.comp"},
        {particle::GpuParticleSpawnShaderSources::Prepare(), "Infernux/ParticleSpawnPrepare.comp"},
    }};
    CompiledParticleSpawnProgram result;
    for (size_t index = 0; index < sources.size(); ++index) {
        const auto bytes = compiler.CompileComputeGlsl(std::string(sources[index].first), sources[index].second);
        if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0)
            return {};
        result.shaders[index].resize(bytes.size() / sizeof(uint32_t));
        std::memcpy(result.shaders[index].data(), bytes.data(), bytes.size());
    }
    return result;
}

CompiledParticleRibbonTopologyProgram CompileParticleRibbonTopologyProgram()
{
    InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
    const std::array<std::pair<std::string_view, const char *>, 5> sources = {{
        {particle::GpuParticleRibbonShaderSources::Reset(), "Infernux/ParticleRibbonReset.comp"},
        {particle::GpuParticleRibbonShaderSources::Initialize(), "Infernux/ParticleRibbonInitialize.comp"},
        {particle::GpuParticleRibbonShaderSources::Histogram(), "Infernux/ParticleRibbonHistogram.comp"},
        {particle::GpuParticleRibbonShaderSources::Scan(), "Infernux/ParticleRibbonScan.comp"},
        {particle::GpuParticleRibbonShaderSources::Scatter(), "Infernux/ParticleRibbonScatter.comp"},
    }};
    CompiledParticleRibbonTopologyProgram result;
    for (size_t index = 0; index < sources.size(); ++index) {
        const auto bytes = compiler.CompileComputeGlsl(std::string(sources[index].first), sources[index].second);
        if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0)
            return {};
        result.shaders[index].resize(bytes.size() / sizeof(uint32_t));
        std::memcpy(result.shaders[index].data(), bytes.data(), bytes.size());
    }
    return result;
}

CompiledParticleRibbonRenderProgram CompileParticleRibbonRenderProgram()
{
    InxShaderLoader compiler(false, true, false, true, false, true, false, false, false, false);
    CompiledParticleRibbonRenderProgram result;
    const auto vertex = compiler.CompileVertexGlsl(
        std::string(particle::GpuParticleRibbonRenderShaderSources::Vertex()), "Infernux/ParticleRibbon.vert");
    const auto picking =
        compiler.CompileFragmentGlsl(std::string(particle::GpuParticleRibbonRenderShaderSources::PickingFragment()),
                                     "Infernux/ParticleRibbonPicking.frag");
    const auto motionVertex =
        compiler.CompileVertexGlsl(std::string(particle::GpuParticleRibbonRenderShaderSources::MotionVertex()),
                                   "Infernux/ParticleRibbonMotion.vert");
    const auto motionFragment =
        compiler.CompileFragmentGlsl(std::string(particle::GpuParticleRibbonRenderShaderSources::MotionFragment()),
                                     "Infernux/ParticleRibbonMotion.frag");
    const std::array<const std::vector<char> *, 4> bytes = {&vertex, &picking, &motionVertex, &motionFragment};
    for (size_t index = 0; index < bytes.size(); ++index) {
        if (bytes[index]->size() < 5 * sizeof(uint32_t) || bytes[index]->size() % sizeof(uint32_t) != 0)
            return {};
        result.shaders[index].resize(bytes[index]->size() / sizeof(uint32_t));
        std::memcpy(result.shaders[index].data(), bytes[index]->data(), bytes[index]->size());
    }
    return result;
}

CompiledParticleProgramBundle CompileParticleProgramBundle()
{
    CompiledParticleProgramBundle result;
    result.sort = CompileParticleSortProgram();
    result.cull = CompileParticleCullProgram();
    result.bounds = CompileParticleBoundsProgram();
    result.migration = CompileParticleMigrationProgram();
    result.spawn = CompileParticleSpawnProgram();
    result.ribbonTopology = CompileParticleRibbonTopologyProgram();
    result.ribbonRender = CompileParticleRibbonRenderProgram();
    return result;
}

} // namespace

struct ParticleProgramBootstrap
{
    CompiledParticleProgramBundle programs;
    JobHandle job;
};

InxRenderer::InxRenderer()
{
    m_vkCore = std::make_unique<InxVkCoreModular>();
    m_view = std::make_unique<InxView>();
    m_captureService = std::make_unique<CaptureService>();
    m_scenePickingService = std::make_unique<ScenePickingService>();
}

InxRenderer::~InxRenderer()
{
    // ========================================================================
    // Shutdown order matters!  Every subsystem holds a raw VkCore/device
    // pointer, so they MUST be destroyed BEFORE m_vkCore.  We do ONE
    // vkDeviceWaitIdle up front, then tear down subsystems without further
    // GPU drains (each subsystem's individual WaitIdle is a no-op when the
    // device is already idle and the flag below is checked).
    // ========================================================================

    // 1. Single GPU drain — after this the device is idle, all subsequent
    //    vkDeviceWaitIdle calls inside subsystem destructors are redundant.
    const bool hasVulkanDevice = m_vkCore && m_vkCore->GetDeviceContext().IsValid();
    if (hasVulkanDevice) {
        m_vkCore->GetDeviceContext().WaitIdle();
        m_vkCore->SetShuttingDown(true);
        m_vkCore->ReleaseActiveDrawLists();
    }

    // 2. Destroy all subsystems that hold raw VkCore pointers — order:
    //    render graphs → screen UI → render targets → auxiliary renderers → GUI.
    m_gameRenderGraph = nullptr;
    m_gameRenderGraphs.clear();
    m_sceneRenderGraph.reset();

    m_particleGpuSystemManager.reset();

    m_screenUIRenderer.reset();

    m_captureService.reset();
    m_scenePickingService.reset();

    m_gameRenderTarget.reset();
    m_sceneRenderTarget.reset();

    m_transientResourcePool.reset();

    m_outlineRenderer.reset();
    m_editorGizmos.reset();
    m_editorTools.reset();
    m_componentGizmos.reset();
    m_particleGpuDrawRegistry.reset();

    if (hasVulkanDevice)
        m_vkCore->ReleaseGpuPreviews();
    m_gui.reset();

    // 3. Now safe to destroy the Vulkan device itself.
    m_vkCore.reset();

    // 4. Tear down the platform window last.
    if (m_view) {
        m_view->Quit();
    }
    m_view.reset();

    // 5. Stop the engine-wide worker pool. Done last so any
    //    last-minute teardown work scheduled by subsystem destructors
    //    above has a thread to run on.
    JobSystem::Shutdown();
}

void InxRenderer::SetCameraPos(float x, float y, float z)
{
    m_cameraPos[0] = x;
    m_cameraPos[1] = y;
    m_cameraPos[2] = z;
}

void InxRenderer::SetCameraLookAt(float x, float y, float z)
{
    m_cameraLookAt[0] = x;
    m_cameraLookAt[1] = y;
    m_cameraLookAt[2] = z;
}

void InxRenderer::SetCameraUp(float x, float y, float z)
{
    m_cameraUp[0] = x;
    m_cameraUp[1] = y;
    m_cameraUp[2] = z;
}

float *InxRenderer::GetCameraPos()
{
    return &m_cameraPos[0];
}
float *InxRenderer::GetCameraLookAt()
{
    return &m_cameraLookAt[0];
}
float *InxRenderer::GetCameraUp()
{
    return &m_cameraUp[0];
}

void InxRenderer::TranslateCamera(float x, float y, float z)
{
    m_cameraPos[0] += x;
    m_cameraPos[1] += y;
    m_cameraPos[2] += z;
}

void InxRenderer::SetAppMetadata(InxAppMetadata appMetaData)
{
    m_appMetadata = appMetaData;
    m_view->SetAppMetadata(appMetaData);
}

InxAppMetadata InxRenderer::GetAppMetadata()
{
    return m_appMetadata;
}

InxAppMetadata InxRenderer::GetRendererMetadata()
{
    return m_rendererMetadata;
}

void InxRenderer::Init(int width, int height, InxAppMetadata appMetaData)
{
    SetAppMetadata(appMetaData);
    if (!m_vkCore) {
        INXLOG_ERROR("Failed to create InxVkCoreModular.");
        return;
    }

    // Bring up the engine-wide C++ thread pool BEFORE Vulkan / scene init so
    // any subsystem that wants to schedule background work can. Idempotent
    // when called twice (e.g. when an external host already initialised it),
    // and Shutdown() in ~InxRenderer pairs with Initialize() here.
    if (!JobSystem::IsAvailable()) {
        JobSystem::Initialize();
    }

    m_vkCore->SetWindowSize(static_cast<uint32_t>(width), static_cast<uint32_t>(height));
    INXLOG_DEBUG("Init View.");
    m_view->Init(width, height);

    // Keep the native window hidden until the Player bootstrap has finished.
    // A game may provide its own splash sequence, but the engine must not
    // impose a visible loading window while Vulkan and runtime assets start.
    if (!PumpStartupEvents())
        throw std::runtime_error("Startup cancelled");

    uint32_t extCount = 0;
    auto ext = m_view->GetVkExtensions(&extCount);
    if (!ext) {
        throw std::runtime_error("Failed to get Vulkan extensions");
    }

    INXLOG_DEBUG("Vulkan extensions count: ", extCount);
    for (uint32_t i = 0; i < extCount; ++i) {
        INXLOG_DEBUG("Vulkan extension: ", ext[i]);
    }

    if (!m_vkCore->Init(m_appMetadata, m_rendererMetadata, extCount, const_cast<const char **>(ext))) {
        throw std::runtime_error("Failed to initialize Vulkan instance");
    }
    if (!PumpStartupEvents())
        throw std::runtime_error("Startup cancelled");

    m_view->CreateSurface(&m_vkCore->m_instance, &m_vkCore->m_surface);
    if (!PumpStartupEvents())
        throw std::runtime_error("Startup cancelled");

    INXLOG_DEBUG("Prepare surface.");
    if (!m_vkCore->PrepareSurface()) {
        throw std::runtime_error("Failed to initialize Vulkan device or swapchain");
    }
    if (!PumpStartupEvents())
        throw std::runtime_error("Startup cancelled");
}

void InxRenderer::PreparePipeline()
{
    auto pumpStartup = [this]() {
        if (!PumpStartupEvents())
            throw std::runtime_error("Startup cancelled");
    };

    if (m_vkCore) {
        m_vkCore->PreparePipeline();
        pumpStartup();
        m_scenePickingService->Initialize(m_vkCore.get());

        INXLOG_DEBUG("Init GUI.");
        m_gui = std::make_unique<InxGUI>(m_vkCore.get());
        m_gui->Init(m_view->m_window);
        pumpStartup();

        // Initialize scene render target with default size
        m_sceneRenderTarget = std::make_unique<SceneRenderTarget>(m_vkCore.get());
        m_sceneRenderTarget->SetMsaaSampleCount(m_vkCore->GetMaterialPipelineManager().GetSampleCount());
        m_sceneRenderTarget->Initialize(800, 600);
        ++m_sceneRenderTargetGeneration;
        pumpStartup();

        // Set initial scene render target size for aspect ratio calculation
        m_vkCore->SetSceneRenderTargetSize(800, 600);

        const char *playerModeFlag = std::getenv("_INFERNUX_PLAYER_MODE");
        const bool playerMode = playerModeFlag != nullptr && playerModeFlag[0] == '1' && playerModeFlag[1] == '\0';
        if (playerMode)
            InitializeRuntimeScene();
        else
            InitializeDefaultScene();
        pumpStartup();

        // These support shaders are CPU-compiled. Overlap that work with the
        // remaining renderer setup and Player catalog/scene publication. The
        // first real ParticleSystem consumes the finished bundle, while a
        // project without particles never blocks startup on it.
        if (JobSystem::IsAvailable() && !m_particleProgramBootstrap) {
            m_particleProgramBootstrap = std::make_shared<ParticleProgramBootstrap>();
            auto bootstrap = m_particleProgramBootstrap;
            bootstrap->job =
                JobSystem::Get().Schedule([bootstrap] { bootstrap->programs = CompileParticleProgramBundle(); },
                                          JobDomain::Render, JobPriority::Low);
        }

        // Initialize RenderGraph pipeline (scene rendering in pre-render pass)
        if (!m_sceneRenderGraph) {
            m_sceneRenderGraph = std::make_unique<SceneRenderGraph>();
        }
        if (m_sceneRenderGraph) {
            if (!m_sceneRenderGraph->Initialize(m_vkCore.get(), m_sceneRenderTarget.get(),
                                                rhi::RenderViewKind::Scene)) {
                INXLOG_ERROR("Failed to initialize the Scene View render graph");
                m_sceneRenderGraph.reset();
            } else {
                m_sceneRenderGraph->SetParticleGpuDrawRegistry(m_particleGpuDrawRegistry.get());
            }
        }
        pumpStartup();

        m_vkCore->SetFrameComputeExecutor([this](VkCommandBuffer cmdBuf) {
            if (!m_particleGpuSystemManager)
                return;
            UpdateParticleCollisionScene();
            m_particleGpuSystemManager->Execute(cmdBuf);
        });
        m_vkCore->SetFrameComputeWorkPredicate(
            [this] { return m_particleGpuSystemManager && m_particleGpuSystemManager->HasPendingGpuWork(); });
        m_vkCore->SetFrameAsyncComputeExecutors(
            [this](VkCommandBuffer cmdBuf) {
                if (!m_particleGpuSystemManager)
                    return false;
                UpdateParticleCollisionScene();
                return m_particleGpuSystemManager->RecordAsyncSimulation(cmdBuf);
            },
            [this](VkCommandBuffer cmdBuf) {
                return m_particleGpuSystemManager && m_particleGpuSystemManager->RecordAsyncExport(cmdBuf);
            },
            [this] {
                // Independent-compute submission has a real cross-queue and
                // cross-frame synchronization cost. Small systems are faster
                // and just as safe when their compute graph is recorded on
                // Graphics, where Vulkan queue order provides the dependency
                // without a CPU fence or a terminal timeline join.
                constexpr uint32_t kAsyncParticleCapacityThreshold = 4096;
                return m_particleGpuSystemManager && m_particleGpuSystemManager->CanExecuteAsync() &&
                       m_particleGpuSystemManager->TelemetrySnapshot().totalCapacity >= kAsyncParticleCapacityThreshold;
            },
            [this] { return m_particleGpuSystemManager ? m_particleGpuSystemManager->AsyncExecutionGeneration() : 0; },
            [this] {
                constexpr uint32_t kAsyncParticleCapacityThreshold = 4096;
                return m_particleGpuSystemManager && m_particleGpuSystemManager->CanRecordPartitioned() &&
                       m_particleGpuSystemManager->TelemetrySnapshot().totalCapacity >= kAsyncParticleCapacityThreshold;
            });

        // Hook RenderGraph execution into the pre-render callback
        m_vkCore->SetRenderGraphExecutor([this](VkCommandBuffer cmdBuf) {
            const bool sceneViewActive = ((m_sceneViewVisible || HasPendingCapture(CaptureSource::Scene) ||
                                           (m_scenePickingService && m_scenePickingService->HasPendingRecord())) &&
                                          m_sceneRenderTarget && m_sceneRenderTarget->IsReady() &&
                                          m_sceneRenderTarget->GetWidth() > 1 && m_sceneRenderTarget->GetHeight() > 1);

#if INFERNUX_FRAME_PROFILE
            using ExClock = std::chrono::high_resolution_clock;
            auto exT0 = ExClock::now();
#endif
            // ---- Scene View -------------------------------------------------
            if (sceneViewActive && m_sceneRenderGraph) {
                // Swap in scene-specific draw calls (includes gizmos)
                if (m_sceneRenderGraph->HasCachedDrawCalls()) {
                    m_vkCore->SetDrawCalls(&m_sceneRenderGraph->GetCachedDrawCalls());
                } else {
                    m_vkCore->SetDrawCalls(nullptr);
                }
                if (m_sceneRenderGraph->HasCachedShadowDrawCalls()) {
                    m_vkCore->SetShadowDrawCalls(&m_sceneRenderGraph->GetCachedShadowDrawCalls());
                } else {
                    m_vkCore->SetShadowDrawCalls(nullptr);
                }
                m_sceneRenderGraph->SetDrawViewMatrix(m_sceneRenderGraph->GetCachedView());
                m_sceneRenderGraph->Execute(cmdBuf);
                m_sceneRenderGraph->CommitCameraHistory();
            }
#if INFERNUX_FRAME_PROFILE
            auto exT1 = ExClock::now();
#endif
            if (sceneViewActive && m_scenePickingService && m_scenePickingService->HasPendingRecord() &&
                m_sceneRenderTarget) {
                m_scenePickingService->Record(cmdBuf, m_sceneRenderTarget->GetWidth(), m_sceneRenderTarget->GetHeight(),
                                              m_sceneRenderGraph->GetPerViewBindGroup(),
                                              m_sceneRenderGraph->GetCachedView());
            }
#if INFERNUX_FRAME_PROFILE
            auto exT2 = ExClock::now();
#endif

            // ---- Game View: execute each camera-owned graph in depth order.
            const bool gameViewActive = m_gameCameraEnabled || HasPendingCapture(CaptureSource::Game);
            if (gameViewActive && m_gameRenderTarget && m_gameRenderTarget->IsReady()) {
#if INFERNUX_FRAME_PROFILE
                auto exTg0 = ExClock::now();
#endif
                for (Camera *gameCam : FindGameCamerasCached()) {
                    SceneRenderGraph *graph = EnsureGameRenderGraph(gameCam);
                    if (!graph)
                        continue;
                    if (graph->HasCachedDrawCalls()) {
                        m_vkCore->SetDrawCalls(&graph->GetCachedDrawCalls());
                    } else {
                        m_vkCore->SetDrawCalls(nullptr);
                    }
                    if (graph->HasCachedShadowDrawCalls()) {
                        m_vkCore->SetShadowDrawCalls(&graph->GetCachedShadowDrawCalls());
                    } else {
                        m_vkCore->SetShadowDrawCalls(nullptr);
                    }

                    const glm::mat4 gameView =
                        graph->HasCachedCameraVP() ? graph->GetCachedView() : gameCam->GetViewMatrix();
                    graph->SetDrawViewMatrix(gameView);
                    graph->Execute(cmdBuf);
                    graph->CommitCameraHistory();
                }
#if INFERNUX_FRAME_PROFILE
                auto exTg1 = ExClock::now();
#endif

                if (sceneViewActive) {
                    if (m_sceneRenderGraph && m_sceneRenderGraph->HasCachedDrawCalls()) {
                        m_vkCore->SetDrawCalls(&m_sceneRenderGraph->GetCachedDrawCalls());
                    } else {
                        m_vkCore->SetDrawCalls(nullptr);
                    }

                    if (m_sceneRenderGraph && m_sceneRenderGraph->HasCachedShadowDrawCalls()) {
                        m_vkCore->SetShadowDrawCalls(&m_sceneRenderGraph->GetCachedShadowDrawCalls());
                    } else {
                        m_vkCore->SetShadowDrawCalls(nullptr);
                    }
                }
#if INFERNUX_FRAME_PROFILE
                auto exTg3 = ExClock::now();

                m_executorTiming.gameSetupMs += std::chrono::duration<double, std::milli>(exTg0 - exT2).count();
                m_executorTiming.gameExecMs += std::chrono::duration<double, std::milli>(exTg1 - exTg0).count();
                m_executorTiming.gameRestoreMs += std::chrono::duration<double, std::milli>(exTg3 - exTg1).count();
#endif
            }

#if INFERNUX_FRAME_PROFILE
            m_executorTiming.sceneExecMs += std::chrono::duration<double, std::milli>(exT1 - exT0).count();
            m_executorTiming.sceneMsaaMs += std::chrono::duration<double, std::milli>(exT2 - exT1).count();
#endif
        });

        m_vkCore->SetFramePreSetupBuilder(
            [this](vk::VulkanFrameSubmission &submission, std::vector<uint32_t> &extraSetupDependencies) {
                const auto collectOwnershipReleases = [&](SceneRenderGraph *graph) {
                    if (!graph || !graph->GetCompiledRenderGraph())
                        return;
                    vk::RenderGraph *compiled = graph->GetCompiledRenderGraph();
                    const auto &viewContext = graph->GetRenderViewContext();
                    for (const rhi::QueueRole sourceQueue :
                         {rhi::QueueRole::Graphics, rhi::QueueRole::Compute, rhi::QueueRole::Transfer}) {
                        if (!compiled->HasExternalQueueOwnershipReleases(sourceQueue))
                            continue;
                        extraSetupDependencies.push_back(submission.AddWork(
                            viewContext.device, sourceQueue, rhi::SubmissionDomain::Frame, viewContext.id,
                            rhi::PipelineStage::AllCommands, {},
                            [compiled, sourceQueue](VkCommandBuffer commandBuffer) {
                                return compiled->RecordExternalQueueOwnershipReleases(sourceQueue, commandBuffer);
                            },
                            "SceneGraph/OwnershipRelease"));
                    }
                };
                // AlwaysSimulate particles keep submitting compute after the user
                // hides Scene/Game. Those compiled graphs still owe last frame's
                // Graphics→Compute releases; skipping them hangs PrimeSimulation.
                collectOwnershipReleases(m_sceneRenderGraph.get());
                for (auto &[cameraId, graph] : m_gameRenderGraphs) {
                    (void)cameraId;
                    collectOwnershipReleases(graph.get());
                }
                return true;
            });
        m_vkCore->SetFrameSubmissionBuilder([this](vk::VulkanFrameSubmission &submission, uint32_t frameSetupWorkItem,
                                                   uint32_t particleComputeWorkItem) {
            const bool sceneViewActive = ((m_sceneViewVisible || HasPendingCapture(CaptureSource::Scene) ||
                                           (m_scenePickingService && m_scenePickingService->HasPendingRecord())) &&
                                          m_sceneRenderTarget && m_sceneRenderTarget->IsReady() &&
                                          m_sceneRenderTarget->GetWidth() > 1 && m_sceneRenderTarget->GetHeight() > 1);
            const bool gameViewActive = m_gameCameraEnabled || HasPendingCapture(CaptureSource::Game);
            std::vector<uint32_t> completedViews;

            const auto activateView = [this](SceneRenderGraph *graph) {
                if (!graph)
                    return;
                m_vkCore->SetDrawCalls(graph->HasCachedDrawCalls() ? &graph->GetCachedDrawCalls() : nullptr);
                m_vkCore->SetShadowDrawCalls(graph->HasCachedShadowDrawCalls() ? &graph->GetCachedShadowDrawCalls()
                                                                               : nullptr);
            };

            const auto appendView = [&](SceneRenderGraph *graph, bool sceneView, const glm::mat4 &view,
                                        uint32_t predecessor, uint32_t *finalWorkItem) {
                if (!graph || !graph->GetCompiledRenderGraph())
                    return true;
                graph->SetDrawViewMatrix(view);
                vk::RenderGraph *compiled = graph->GetCompiledRenderGraph();
                const auto &viewContext = graph->GetRenderViewContext();
                std::vector<uint32_t> graphDependencies{predecessor};
                if (particleComputeWorkItem != 0)
                    graphDependencies.push_back(particleComputeWorkItem);
                const auto range = submission.AppendRenderGraph(
                    *compiled, graphDependencies, [graph, activateView](uint32_t batchIndex, VkCommandBuffer) {
                        activateView(graph);
                        return batchIndex != 0 || graph->PrepareSubmissionExecution();
                    });
                if (range.Empty())
                    return false;

                const uint32_t finalize = submission.AddWork(
                    viewContext.device, rhi::QueueRole::Graphics, rhi::SubmissionDomain::Frame, viewContext.id,
                    rhi::PipelineStage::AllGraphics, range.terminals,
                    [this, graph, sceneView, activateView](VkCommandBuffer commandBuffer) {
                        activateView(graph);
                        if (!graph->CompleteSubmissionExecution(commandBuffer))
                            return false;
                        graph->CommitCameraHistory();
                        if (sceneView && m_scenePickingService && m_scenePickingService->HasPendingRecord() &&
                            m_sceneRenderTarget) {
                            m_scenePickingService->Record(commandBuffer, m_sceneRenderTarget->GetWidth(),
                                                          m_sceneRenderTarget->GetHeight(),
                                                          graph->GetPerViewBindGroup(), graph->GetCachedView());
                        }
                        return true;
                    },
                    "SceneGraph/Finalize");
                completedViews.push_back(finalize);
                if (finalWorkItem)
                    *finalWorkItem = finalize;
                return true;
            };

            if (sceneViewActive && m_sceneRenderGraph) {
                if (!appendView(m_sceneRenderGraph.get(), true, m_sceneRenderGraph->GetCachedView(), frameSetupWorkItem,
                                nullptr))
                    return false;
            }

            if (gameViewActive && m_gameRenderTarget && m_gameRenderTarget->IsReady()) {
                uint32_t cameraDependency = frameSetupWorkItem;
                for (Camera *gameCam : FindGameCamerasCached()) {
                    SceneRenderGraph *graph = EnsureGameRenderGraph(gameCam);
                    if (!graph)
                        continue;
                    const glm::mat4 gameView =
                        graph->HasCachedCameraVP() ? graph->GetCachedView() : gameCam->GetViewMatrix();
                    uint32_t cameraFinal = cameraDependency;
                    if (!appendView(graph, false, gameView, cameraDependency, &cameraFinal))
                        return false;
                    cameraDependency = cameraFinal;
                }
            }

            if (sceneViewActive && m_sceneRenderGraph)
                activateView(m_sceneRenderGraph.get());

            const auto &presentation = m_vkCore->m_presentationView;
            (void)submission.AddWork(presentation.device, rhi::QueueRole::Graphics, rhi::SubmissionDomain::Frame,
                                     presentation.id, rhi::PipelineStage::AllGraphics, std::move(completedViews),
                                     [](VkCommandBuffer commandBuffer) {
                                         (void)commandBuffer;
                                         return true;
                                     });
            return true;
        });

        // GUI is rendered via RenderGraph pass (no more imperative BeginRenderPass/EndRenderPass)
        m_vkCore->SetGuiRenderCallback(
            [this](vk::RenderContext &ctx) { m_gui->RecordCommand(ctx.GetCommandBuffer()); });

        // Create TransientResourcePool for CommandBuffer temporary render targets
        m_transientResourcePool = std::make_unique<TransientResourcePool>();
        m_transientResourcePool->Initialize(&m_vkCore->GetDeviceContext(), &m_vkCore->GetResourceManager());

        // The Scene RenderGraph owns the outline passes and target layouts.
        m_outlineRenderer = std::make_unique<OutlineRenderer>();
    }
}

void InxRenderer::DrawFrame()
{
    const bool samplePerformanceFrame = m_framePerformanceWindowActive;
    const auto performanceWindowFrameStart =
        samplePerformanceFrame ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};

    // Calculate delta time
    auto currentTime = std::chrono::high_resolution_clock::now();
    m_deltaTime = std::chrono::duration<float>(currentTime - m_lastFrameTime).count();
    m_lastFrameTime = currentTime;

    // Clamp delta time to avoid huge jumps
    if (m_deltaTime > 1.0f / 3.0f)
        m_deltaTime = 1.0f / 3.0f;

    // ========================================================================
    // Frame Profiler - aggregates per-phase timings and reports at a bounded cadence.
    // Controlled by INFERNUX_FRAME_PROFILE in ProfileConfig.h (0 = off, 1 = on).
    // ========================================================================

#if INFERNUX_FRAME_PROFILE
    struct FrameProfiler
    {
        using Clock = std::chrono::high_resolution_clock;
        using TimePoint = Clock::time_point;
        TimePoint stamps[16];
        int idx = 0;
        void stamp()
        {
            if (idx < 16)
                stamps[idx++] = Clock::now();
        }
        float ms(int a, int b) const
        {
            return std::chrono::duration<float, std::milli>(stamps[b] - stamps[a]).count();
        }
    };
    FrameProfiler _fp;
    static int _fpCounter = 0;
    static auto _fpLastReport = std::chrono::steady_clock::now();
    static double _fpAccum[12] = {};
    static double _deltaAccumMs = 0.0;
    static double _srpSceneViewMs = 0;
    static double _srpGameViewMs = 0;
    static std::vector<double> _frameTimeSamples;
    static std::vector<double> _fixedStepFrameSamples;
    static std::vector<double> _renderOnlyFrameSamples;
    static InxRenderer::FrameDetailTiming _detailAccum;
    static SceneManager::FrameProfile _sceneAccum;
    static std::unordered_map<std::string, double> _guiAccum;
    static std::unordered_map<std::string, double> _inspSubAccum;
    static std::unordered_map<std::string, double> _hierSubAccum;
    static std::unordered_map<std::string, double> _projectSubAccum;
    struct FramePacingAccum
    {
        double targetFps = 0.0;
        double elapsedBeforeSleepMs = 0.0;
        double frameBudgetMs = 0.0;
        double requestedSleepMs = 0.0;
        double actualSleepMs = 0.0;
        int sleptFrames = 0;
        int idleFrames = 0;
        int wokeByEventFrames = 0;
        int wokeByInputFrames = 0;
        int wokeByWindowFrames = 0;
        int wokeByOtherFrames = 0;
        int inputFrames = 0;
        int playBypassFrames = 0;
        uint64_t queuedEvents = 0;
        uint64_t mouseMotionEvents = 0;
        uint64_t dispatchedMouseMotions = 0;
        double inputBeginFrameMs = 0.0;
        double inputPumpEventsMs = 0.0;
        double inputPeepEventsMs = 0.0;
        double inputImGuiDispatchMs = 0.0;
        double inputManagerDispatchMs = 0.0;
        double inputSyntheticDispatchMs = 0.0;
        double inputWindowQueryMs = 0.0;
    };
    static FramePacingAccum _pacingAccum;
    _fp.stamp(); // [0] frame start
#endif

#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming = {};
#endif

    // Invalidate per-frame game camera cache
    m_gameCameraCacheValid = false;
    m_cachedGameCamera = nullptr;
    m_cachedGameCameras.clear();
    // NOTE: do NOT reset m_lastGameRenderMs here — Python panels read it
    // during BuildFrame() which runs BEFORE the game camera render pass.
    // The value from the previous frame is the correct one for display.

    // Window events
    m_view->ProcessEvent();
    const uint64_t syntheticSequence = m_view->GetLastProcessedSyntheticInputSequence();
    if (syntheticSequence != 0 && syntheticSequence != m_lastSemanticSyntheticInputSequence) {
        m_lastSemanticSyntheticInputSequence = syntheticSequence;
        // Synthetic SDL events are consumed before the GUI build below.
        // They must also wake the scheduler explicitly: semantic capture can
        // remain enabled across frames, so treating the capture request itself
        // as a level-triggered force would allow the scheduler's force edge to
        // be consumed only once. A new input sequence is the authoritative
        // wake boundary for both editor and Player GUI input.
        if (m_gui)
            m_gui->RequestSyntheticInputFrame();
        // Player mode has no semantic editor snapshot, but still needs the
        // same GUI wake/consumption boundary for its Python UI callbacks.
        if (!m_guiPlayerMode)
            InxGUISemantics::RequestSnapshot(syntheticSequence);
    }
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [1] after input/event processing
    _deltaAccumMs += static_cast<double>(m_deltaTime) * 1000.0;
    if (m_view) {
        const auto &pacing = m_view->GetLastPacingSample();
        _pacingAccum.targetFps += pacing.targetFps;
        _pacingAccum.elapsedBeforeSleepMs += pacing.elapsedBeforeSleepMs;
        _pacingAccum.frameBudgetMs += pacing.frameBudgetMs;
        _pacingAccum.requestedSleepMs += pacing.requestedSleepMs;
        _pacingAccum.actualSleepMs += pacing.actualSleepMs;
        _pacingAccum.sleptFrames += pacing.slept ? 1 : 0;
        _pacingAccum.idleFrames += pacing.idleMode ? 1 : 0;
        _pacingAccum.wokeByEventFrames += pacing.wokeByEvent ? 1 : 0;
        _pacingAccum.wokeByInputFrames += pacing.wokeByInputEvent ? 1 : 0;
        _pacingAccum.wokeByWindowFrames += pacing.wokeByWindowEvent ? 1 : 0;
        _pacingAccum.wokeByOtherFrames += pacing.wokeByOtherEvent ? 1 : 0;
        _pacingAccum.inputFrames += pacing.hadInputEvent ? 1 : 0;
        _pacingAccum.playBypassFrames += pacing.playModeBypass ? 1 : 0;
        _pacingAccum.queuedEvents += pacing.queuedEventCount;
        _pacingAccum.mouseMotionEvents += pacing.mouseMotionEventCount;
        _pacingAccum.dispatchedMouseMotions += pacing.dispatchedMouseMotionCount;
        _pacingAccum.inputBeginFrameMs += pacing.inputBeginFrameMs;
        _pacingAccum.inputPumpEventsMs += pacing.inputPumpEventsMs;
        _pacingAccum.inputPeepEventsMs += pacing.inputPeepEventsMs;
        _pacingAccum.inputImGuiDispatchMs += pacing.inputImGuiDispatchMs;
        _pacingAccum.inputManagerDispatchMs += pacing.inputManagerDispatchMs;
        _pacingAccum.inputSyntheticDispatchMs += pacing.inputSyntheticDispatchMs;
        _pacingAccum.inputWindowQueryMs += pacing.inputWindowQueryMs;
    }
#endif

    // Run the pre-GUI callback after input processing, even while the window
    // is minimized.  Remote MCP commands are drained there; returning before
    // it would make a minimized Editor impossible to control or recover.
    // Actual ImGui/Vulkan work remains below the minimized early return.
    if (m_preGuiCallback) {
        m_preGuiCallback();
    }

    // A minimized swapchain has no presentable images, but Agent UI work must
    // remain independent from desktop presentation. Build an ImGui-only frame
    // when an on-demand semantic snapshot is pending; it consumes synthetic
    // input and publishes targets without touching Vulkan acquire/present.
    if (m_view->IsMinimized()) {
        // A synthetic press may be waiting for a GUI consumption boundary
        // even in Player mode. Keep the ImGui-only build path available so a
        // deferred release can never be stranded behind a minimized window.
        if (m_view->NeedsImmediateGuiRefresh() || InxGUISemantics::HasPendingCaptureRequest()) {
            const auto guiBuildStart = std::chrono::high_resolution_clock::now();
            const bool guiBuilt = m_gui->BuildFrameIfDue(true);
            if (guiBuilt)
                m_view->NotifyGuiFrameBuilt();
            m_guiBuildMs =
                std::chrono::duration<double, std::milli>(std::chrono::high_resolution_clock::now() - guiBuildStart)
                    .count();
        }
        SDL_Delay(16);
        return;
    }

    // Update scene system
    auto _sceneUpdateStart = std::chrono::high_resolution_clock::now();
    auto _scenePhaseT0 = std::chrono::high_resolution_clock::now();
    if (m_preSceneUpdateCallback)
        m_preSceneUpdateCallback(m_deltaTime);
    SceneManager &sceneManager = SceneManager::Instance();
    const float sceneDeltaTime = sceneManager.ConsumeFrameDeltaTime(m_deltaTime);
    TransformECSStore::Instance().BeginFrameCache(sceneManager.GetActiveScene());
#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.frameCacheBeginMs =
        std::chrono::duration<double, std::milli>(std::chrono::high_resolution_clock::now() - _scenePhaseT0).count();
#endif

    _scenePhaseT0 = std::chrono::high_resolution_clock::now();
    sceneManager.Update(sceneDeltaTime);
#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.sceneUpdateCallMs =
        std::chrono::duration<double, std::milli>(std::chrono::high_resolution_clock::now() - _scenePhaseT0).count();
#endif

    // LateUpdate runs immediately after Update — before rendering — so that
    // camera-follow scripts see interpolated physics transforms and their
    // results are picked up by the same frame's render pass.  This matches
    // Unity's execution order: FixedUpdate → Update → LateUpdate → Render.
    _scenePhaseT0 = std::chrono::high_resolution_clock::now();
    sceneManager.LateUpdate(sceneDeltaTime);
#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.lateUpdateCallMs =
        std::chrono::duration<double, std::milli>(std::chrono::high_resolution_clock::now() - _scenePhaseT0).count();
#endif

    _scenePhaseT0 = std::chrono::high_resolution_clock::now();
    const bool publishedPhysicsPose = TransformECSStore::Instance().EndFrameCache();
    if (publishedPhysicsPose)
        sceneManager.PublishPhysicsTransformsToRenderer();
    if (Scene *activeScene = sceneManager.GetActiveScene())
        TransformECSStore::Instance().SyncSceneWorldMatrices(activeScene);
    sceneManager.PublishAuthoredTransformsToPhysics();
    sceneManager.EmitRuntimeFrameBarrier(SceneManager::RuntimeFrameBarrier::FinalTransformResolve);
    // Animation and Timeline currently execute as ordinary component updates.
    // This boundary publishes their finalized transform/pose output to all
    // downstream render and snapshot consumers without running them twice.
    sceneManager.EmitRuntimeFrameBarrier(SceneManager::RuntimeFrameBarrier::AnimationTimeline);
    ConsumeSceneTemporalDiscontinuity();
#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.frameCacheEndMs =
        std::chrono::duration<double, std::milli>(std::chrono::high_resolution_clock::now() - _scenePhaseT0).count();
#endif
    auto _sceneUpdateEnd = std::chrono::high_resolution_clock::now();
    m_sceneUpdateMs = std::chrono::duration<double, std::milli>(_sceneUpdateEnd - _sceneUpdateStart).count();
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [2] after SceneManager::Update + LateUpdate
#endif

    // ========================================================================
    // Per-Frame Fence Synchronization (Fix 1: replaces vkDeviceWaitIdle)
    //
    // Wait on the current frame slot's fence so that GPU resources bound
    // to this slot are guaranteed idle.  Then flush deferred deletions
    // that have aged past maxFramesInFlight, ensuring no in-flight
    // command buffer references them.
    //
    // This replaces the blanket vkDeviceWaitIdle() that was serialising
    // 100% of CPU/GPU work.  Combined with vkCmdUpdateBuffer-based UBO
    // writes, this allows true double-buffered CPU/GPU parallelism.
    // ========================================================================
    if (m_vkCore) {
        m_vkCore->WaitForCurrentFrame();
        m_vkCore->CollectRetiredGpuResources();
    }
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [3] after WaitForCurrentFrame (GPU fence)
#endif

    auto _guiBuildStart = std::chrono::high_resolution_clock::now();
    const bool forceGuiBuild =
        m_view->NeedsImmediateGuiRefresh() || (!m_guiPlayerMode && InxGUISemantics::HasPendingCaptureRequest());
    const bool guiBuilt = m_gui->BuildFrameIfDue(forceGuiBuild);
    if (guiBuilt)
        m_view->NotifyGuiFrameBuilt();
    auto _guiBuildEnd = std::chrono::high_resolution_clock::now();
    m_guiBuildMs = guiBuilt ? std::chrono::duration<double, std::milli>(_guiBuildEnd - _guiBuildStart).count() : 0.0;
    if (guiBuilt)
        RecordUIPerformanceFrame();
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [4] after GUI::BuildFrame (ImGui → Python panels)
#endif

    const bool sceneViewActive = ((m_sceneViewVisible || HasPendingCapture(CaptureSource::Scene) ||
                                   (m_scenePickingService && m_scenePickingService->HasPendingRecord())) &&
                                  m_sceneRenderTarget && m_sceneRenderTarget->IsReady() &&
                                  m_sceneRenderTarget->GetWidth() > 1 && m_sceneRenderTarget->GetHeight() > 1);
    const bool gameViewActive = m_gameCameraEnabled || HasPendingCapture(CaptureSource::Game);

    // Prepare scene rendering data (collect + cull + sort) AFTER GUI processing
    // so we always operate on the current scene state.
    SceneRenderBridge &bridge = SceneRenderBridge::Instance();
    auto _prepareStart = std::chrono::high_resolution_clock::now();
    sceneManager.EmitRuntimeFrameBarrier(SceneManager::RuntimeFrameBarrier::RenderExtraction);
    bridge.PrepareFrame(sceneViewActive);
    auto _prepareEnd = std::chrono::high_resolution_clock::now();
    m_prepareFrameMs = std::chrono::duration<double, std::milli>(_prepareEnd - _prepareStart).count();
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [5] after PrepareFrame (CollectRenderables)
#endif

    // Advance EnsureObjectBuffers frame counter so duplicate calls this frame are skipped.
    if (m_vkCore) {
        m_vkCore->AdvanceEnsureFrame();
    }

    // Update camera from scene system (uses PrepareFrame results)
    bridge.UpdateCameraData(m_cameraPos, m_cameraLookAt, m_cameraUp);

    if (CheckAndApplyMsaaRequest(false, sceneViewActive, gameViewActive)) {
        sceneManager.EndFrame();
        return;
    }

    // Render scene via Python SRP render pipeline

    if (m_renderPipeline) {
        EditorGizmosContext gizmoCtx;
        if (m_editorGizmos) {
            gizmoCtx.gizmos = m_editorGizmos.get();
            gizmoCtx.editorTools = m_editorTools.get();
            gizmoCtx.componentGizmos = m_componentGizmos.get();
            auto &registry = AssetRegistry::Instance();
            gizmoCtx.gizmoMaterial = registry.GetBuiltinMaterial("GizmoMaterial");
            gizmoCtx.gridMaterial = registry.GetBuiltinMaterial("GridMaterial");
            gizmoCtx.editorToolsMaterial = registry.GetBuiltinMaterial("EditorToolsMaterial");
            gizmoCtx.componentGizmosMaterial = registry.GetBuiltinMaterial("ComponentGizmosMaterial");
            gizmoCtx.componentGizmoIconMaterial = registry.GetBuiltinMaterial("ComponentGizmoIconMaterial");
            gizmoCtx.cameraGizmoIconMaterial = registry.GetBuiltinMaterial("ComponentGizmoCameraIconMaterial");
            gizmoCtx.lightGizmoIconMaterial = registry.GetBuiltinMaterial("ComponentGizmoLightIconMaterial");
            gizmoCtx.particleGizmoIconMaterial = registry.GetBuiltinMaterial("ComponentGizmoParticleIconMaterial");
            gizmoCtx.selectedObjectId = m_selectedObjectId;
            gizmoCtx.activeScene = SceneManager::Instance().GetActiveScene();
            gizmoCtx.cameraPos = glm::vec3(m_cameraPos[0], m_cameraPos[1], m_cameraPos[2]);
        }

        // ---- Scene View: always use editor camera ----
        if (sceneViewActive) {
            ScriptableRenderContext ctx(m_vkCore.get(), m_sceneRenderGraph.get(), gizmoCtx);
            if (m_transientResourcePool) {
                ctx.SetTransientResourcePool(m_transientResourcePool.get());
            }
            Camera *editorCam = SceneManager::Instance().GetEditorCameraController().GetCamera();

#if INFERNUX_FRAME_PROFILE
            auto _srpT0 = std::chrono::high_resolution_clock::now();
#endif
            if (editorCam)
                m_renderPipeline->Render(ctx, editorCam);
#if INFERNUX_FRAME_PROFILE
            auto _srpT1 = std::chrono::high_resolution_clock::now();
            _srpSceneViewMs += std::chrono::duration<double, std::milli>(_srpT1 - _srpT0).count();
#endif
        }

        // ---- Game View: one context and one RenderView per active camera ----
        if (gameViewActive && m_gameRenderTarget && m_gameRenderTarget->IsReady()) {
            const auto _srpT2 = std::chrono::high_resolution_clock::now();
            for (Camera *gameCam : FindGameCamerasCached()) {
                SceneRenderGraph *graph = EnsureGameRenderGraph(gameCam);
                if (!graph)
                    continue;
                // Set aspect ratio BEFORE SetupCameraProperties() so the
                // projection matrix snapshot matches the render target size.
                if (m_gameRenderTarget->GetHeight() > 0) {
                    float aspect = static_cast<float>(m_gameRenderTarget->GetWidth()) /
                                   static_cast<float>(m_gameRenderTarget->GetHeight());
                    gameCam->SetAspectRatio(aspect);
                    gameCam->SetScreenDimensions(m_gameRenderTarget->GetWidth(), m_gameRenderTarget->GetHeight());
                }

                // Game camera excludes editor-only gizmos/grid/outline.
                EditorGizmosContext gameContentCtx;
                ScriptableRenderContext gameCtx(m_vkCore.get(), graph, gameContentCtx);
                if (m_transientResourcePool) {
                    gameCtx.SetTransientResourcePool(m_transientResourcePool.get());
                }

                m_renderPipeline->Render(gameCtx, gameCam);
            }
            const auto _srpT3 = std::chrono::high_resolution_clock::now();
            m_lastGameRenderMs = std::chrono::duration<double, std::milli>(_srpT3 - _srpT2).count();
#if INFERNUX_FRAME_PROFILE
            _srpGameViewMs += m_lastGameRenderMs;
#endif
        }
    } else {
        INXLOG_ERROR("No render pipeline set — scene will not be rendered. "
                     "Call engine.set_render_pipeline(DefaultRenderPipelineAsset()) to activate rendering.");
    }

    // RenderPipeline::Render() applies the current Python graph. Re-check the
    // requested MSAA here so a newly selected pipeline can switch sample count
    // before any stale render graph executes this frame.
    if (CheckAndApplyMsaaRequest(true, sceneViewActive, gameViewActive)) {
        sceneManager.EndFrame();
        return;
    }
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [6] after RenderPipeline::Render (Python SRP)
#endif

    // Hidden views are allowed to go unused, but a frame with no cameras must
    // not treat every mesh as dead. Game/Scene reopen rebuilds from cache miss
    // instead of drawing an empty buffer table.
    if (sceneViewActive || gameViewActive)
        CleanupDrawCallBuffers();
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [7] after CleanupUnusedBuffers
#endif

    UpdateSceneLighting();
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [8] after UpdateSceneLighting
#endif

    // Lazy-init and update outline renderer
    if (sceneViewActive && m_outlineRenderer && m_sceneRenderTarget && m_vkCore) {
        m_outlineRenderer->Initialize(m_vkCore.get(), m_sceneRenderTarget.get());
        m_outlineRenderer->SetOutlineObjectIds(m_selectedOutlineObjectIds);
    } else if (m_outlineRenderer) {
        m_outlineRenderer->SetOutlineObjectId(0);
    }

    if (m_sceneRenderGraph)
        m_sceneRenderGraph->SetOutlineRenderer(sceneViewActive ? m_outlineRenderer.get() : nullptr);

    if (sceneViewActive && m_sceneRenderGraph) {
        m_sceneRenderGraph->EnsureGraphBuilt();
    }
    if (gameViewActive) {
        for (Camera *camera : FindGameCamerasCached()) {
            if (SceneRenderGraph *graph = EnsureGameRenderGraph(camera))
                graph->EnsureGraphBuilt();
        }
    }

    StageEngineGlobalsUBO();

    // Camera constants are immutable per RenderView/frame slot. Stage them
    // before any command buffer is recorded so Scene and Game no longer rely
    // on sequentially overwriting one shared UBO.
    if (sceneViewActive && m_sceneRenderGraph && m_sceneRenderGraph->HasCachedCameraVP()) {
        const glm::mat4 previous = m_sceneRenderGraph->GetPreviousViewProj();
        (void)m_sceneRenderGraph->StageCameraMatrices(m_sceneRenderGraph->GetCachedView(),
                                                      m_sceneRenderGraph->GetCachedProj(), &previous);
    }
    if (gameViewActive) {
        for (Camera *gameCam : FindGameCamerasCached()) {
            SceneRenderGraph *graph = EnsureGameRenderGraph(gameCam);
            if (!graph)
                continue;
            const glm::mat4 view = graph->HasCachedCameraVP() ? graph->GetCachedView() : gameCam->GetViewMatrix();
            const glm::mat4 proj = graph->HasCachedCameraVP() ? graph->GetCachedProj() : gameCam->GetProjectionMatrix();
            const glm::mat4 previous = graph->GetPreviousViewProj();
            (void)graph->StageCameraMatrices(view, proj, &previous);
        }
    }
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [9] after Outline+GraphBuild+UBO staging
#endif

    // Descriptor set 2 is shared by every render graph recorded this frame.
    // Grow all instance streams before vkBeginCommandBuffer so a picking or
    // motion pass cannot rewrite an already-bound descriptor set.
    {
        size_t requiredInstances = 0;
        if (sceneViewActive && m_sceneRenderGraph && m_sceneRenderGraph->HasCachedDrawCalls())
            requiredInstances += m_sceneRenderGraph->GetCachedDrawCalls().size();
        if (sceneViewActive && m_sceneRenderGraph && m_sceneRenderGraph->HasCachedShadowDrawCalls())
            requiredInstances +=
                m_sceneRenderGraph->GetCachedShadowDrawCalls().size() * m_sceneRenderGraph->GetCameraShadowViewCount();
        if (gameViewActive) {
            for (Camera *camera : FindGameCamerasCached()) {
                SceneRenderGraph *graph = EnsureGameRenderGraph(camera);
                if (!graph)
                    continue;
                if (graph->HasCachedDrawCalls())
                    requiredInstances += graph->GetCachedDrawCalls().size();
                if (graph->HasCachedShadowDrawCalls())
                    requiredInstances += graph->GetCachedShadowDrawCalls().size() * graph->GetCameraShadowViewCount();
            }
        }
        m_vkCore->PreallocateInstances(requiredInstances);
    }

    // Render frame with scene camera
    sceneManager.EmitRuntimeFrameBarrier(SceneManager::RuntimeFrameBarrier::RenderGraph);
    m_vkCore->DrawFrame(m_cameraPos, m_cameraLookAt, m_cameraUp);
    SubmitPendingCaptureReadbacks();
    if (m_captureService)
        m_captureService->Poll();
    sceneManager.EmitRuntimeFrameBarrier(SceneManager::RuntimeFrameBarrier::SnapshotPublication);
#if INFERNUX_FRAME_PROFILE
    _fp.stamp(); // [10] after VkCore::DrawFrame (GPU submit + present)
#endif

    sceneManager.EndFrame();

    if (++m_gpuResidencyCheckFrames == 60) {
        m_gpuResidencyCheckFrames = 0;
        (void)TrimGpuResidencyBudget();
    }

    // Compute game-only frame cost: sum of game phases, excluding editor UI.
    m_gameOnlyFrameMs = m_sceneUpdateMs + m_prepareFrameMs + m_lastGameRenderMs;

    // ========================================================================
    // Post-draw callback: scene loading / deferred tasks that must run
    // AFTER GPU submission.  The Python callback pumps OS events itself
    // (via engine.pump_events()) when performing heavy scene loads, so
    // we no longer sandwich every frame with SDL_PumpEvents here.
    // ========================================================================
    if (m_postDrawCallback) {
        m_postDrawCallback();
    }

    if (samplePerformanceFrame) {
        const double frameMs =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - performanceWindowFrameStart)
                .count();
        RecordFramePerformanceSample(frameMs);
    }

    // ========================================================================
    // Unified Frame Profiler output. Require enough samples, then throttle by
    // wall time so an uncapped Debug Player cannot flood the engine log.
    // ========================================================================
#if INFERNUX_FRAME_PROFILE
    {
        _fp.stamp();                    // [11] end of frame
        _fpAccum[0] += _fp.ms(0, 11);   // total frame
        _fpAccum[1] += _fp.ms(0, 1);    // Input
        _fpAccum[2] += _fp.ms(1, 2);    // Scene+Late
        _fpAccum[3] += _fp.ms(2, 3);    // GPUFence
        _fpAccum[4] += _fp.ms(3, 4);    // UI
        _fpAccum[5] += _fp.ms(4, 5);    // Prepare
        _fpAccum[6] += _fp.ms(5, 6);    // Render (Python SRP)
        _fpAccum[7] += _fp.ms(6, 7);    // CleanupBuffers
        _fpAccum[8] += _fp.ms(7, 8);    // Lighting
        _fpAccum[9] += _fp.ms(8, 9);    // Outline+Graph+UBO
        _fpAccum[10] += _fp.ms(9, 10);  // DrawFrame (submit+present)
        _fpAccum[11] += _fp.ms(10, 11); // End

        _detailAccum.frameCacheBeginMs += m_frameDetailTiming.frameCacheBeginMs;
        _detailAccum.sceneUpdateCallMs += m_frameDetailTiming.sceneUpdateCallMs;
        _detailAccum.lateUpdateCallMs += m_frameDetailTiming.lateUpdateCallMs;
        _detailAccum.frameCacheEndMs += m_frameDetailTiming.frameCacheEndMs;
        _detailAccum.cleanupCollectIdsMs += m_frameDetailTiming.cleanupCollectIdsMs;
        _detailAccum.cleanupReleaseMs += m_frameDetailTiming.cleanupReleaseMs;
        _detailAccum.cleanupActiveIds += m_frameDetailTiming.cleanupActiveIds;
        _detailAccum.lightingCollectMs += m_frameDetailTiming.lightingCollectMs;
        _detailAccum.lightingShadowEditorMs += m_frameDetailTiming.lightingShadowEditorMs;
        _detailAccum.lightingShadowGameMs += m_frameDetailTiming.lightingShadowGameMs;
        _detailAccum.lightingUploadMs += m_frameDetailTiming.lightingUploadMs;

        const auto &sceneProfile = SceneManager::Instance().GetLastFrameProfile();
        const double totalFrameMs = _fp.ms(0, 11);
        _frameTimeSamples.push_back(totalFrameMs);
        if (sceneProfile.fixedSteps > 0.0)
            _fixedStepFrameSamples.push_back(totalFrameMs);
        else
            _renderOnlyFrameSamples.push_back(totalFrameMs);
        _sceneAccum.editorCameraMs += sceneProfile.editorCameraMs;
        _sceneAccum.editorUpdateMs += sceneProfile.editorUpdateMs;
        _sceneAccum.pendingStartsMs += sceneProfile.pendingStartsMs;
        _sceneAccum.syncExternalMovesMs += sceneProfile.syncExternalMovesMs;
        _sceneAccum.syncCollidersMs += sceneProfile.syncCollidersMs;
        _sceneAccum.fixedUpdateMs += sceneProfile.fixedUpdateMs;
        _sceneAccum.physicsStepMs += sceneProfile.physicsStepMs;
        _sceneAccum.physicsEventsMs += sceneProfile.physicsEventsMs;
        _sceneAccum.syncRigidbodiesMs += sceneProfile.syncRigidbodiesMs;
        _sceneAccum.interpolationMs += sceneProfile.interpolationMs;
        _sceneAccum.gameplayUpdateMs += sceneProfile.gameplayUpdateMs;
        _sceneAccum.lateUpdateMs += sceneProfile.lateUpdateMs;
        _sceneAccum.audioMs += sceneProfile.audioMs;
        _sceneAccum.endFrameMs += sceneProfile.endFrameMs;
        _sceneAccum.fixedSteps += sceneProfile.fixedSteps;

        const auto &panelTimes = m_gui->GetLastPanelTimesMs();
        for (const auto &name : m_gui->GetRenderableOrder()) {
            auto it = panelTimes.find(name);
            if (it != panelTimes.end()) {
                _guiAccum[name] += it->second;
            }
        }

        const auto &panelSubTimes = m_gui->GetLastPanelSubTimesMs();
        const auto accumulatePanelSubTimes = [&panelSubTimes](const char *panelName, auto &destination) {
            const auto panel = panelSubTimes.find(panelName);
            if (panel == panelSubTimes.end())
                return;
            for (const auto &kv : panel->second)
                destination[kv.first] += kv.second;
        };
        accumulatePanelSubTimes("inspector", _inspSubAccum);
        accumulatePanelSubTimes("hierarchy", _hierSubAccum);
        accumulatePanelSubTimes("project", _projectSubAccum);

        ++_fpCounter;
        const auto _fpReportNow = std::chrono::steady_clock::now();
        if (_fpCounter >= INFERNUX_FRAME_PROFILE_WINDOW &&
            _fpReportNow - _fpLastReport >= std::chrono::seconds(INFERNUX_FRAME_PROFILE_REPORT_INTERVAL_SECONDS)) {
            const double kWindow = static_cast<double>(_fpCounter);
            const auto percentile = [](const std::vector<double> &samples, double fraction) {
                if (samples.empty())
                    return 0.0;
                std::vector<double> sorted = samples;
                std::sort(sorted.begin(), sorted.end());
                const size_t index = static_cast<size_t>(std::round(fraction * static_cast<double>(sorted.size() - 1)));
                return sorted[index];
            };
            const auto sampleMean = [](const std::vector<double> &samples) {
                return samples.empty()
                           ? 0.0
                           : std::accumulate(samples.begin(), samples.end(), 0.0) / static_cast<double>(samples.size());
            };
            std::ostringstream oss;
            oss << std::fixed << std::setprecision(2);
            oss << "[Profile] avg" << _fpCounter << " frame=" << (_fpAccum[0] / kWindow) << "ms"
                << " p50=" << percentile(_frameTimeSamples, 0.50) << "ms"
                << " p95=" << percentile(_frameTimeSamples, 0.95) << "ms"
                << " p99=" << percentile(_frameTimeSamples, 0.99) << "ms"
                << " min=" << percentile(_frameTimeSamples, 0.00) << "ms"
                << " max=" << percentile(_frameTimeSamples, 1.00) << "ms"
                << " | Delta=" << (_deltaAccumMs / kWindow) << "ms" << " | Input=" << (_fpAccum[1] / kWindow) << "ms"
                << " | Scene+Late=" << (_fpAccum[2] / kWindow) << "ms" << " | GPUFence=" << (_fpAccum[3] / kWindow)
                << "ms" << " | UI=" << (_fpAccum[4] / kWindow) << "ms" << " | Prepare=" << (_fpAccum[5] / kWindow)
                << "ms" << " | Render=" << (_fpAccum[6] / kWindow) << "ms" << "(SV=" << (_srpSceneViewMs / kWindow)
                << "ms GV=" << (_srpGameViewMs / kWindow) << "ms)" << " | Cleanup=" << (_fpAccum[7] / kWindow) << "ms"
                << " | Lighting=" << (_fpAccum[8] / kWindow) << "ms" << " | Graph+UBO=" << (_fpAccum[9] / kWindow)
                << "ms" << " | DrawFrame=" << (_fpAccum[10] / kWindow) << "ms" << " | End=" << (_fpAccum[11] / kWindow)
                << "ms";

            {
                const auto bridgeProfile = SceneRenderBridge::Instance().GetProfileSnapshot();
                const auto srcProfile = ScriptableRenderContext::GetProfileSnapshot();
                const auto deletionStats =
                    m_vkCore ? m_vkCore->GetRetirementQueue().GetStats() : GpuRetirementQueue::Stats{};

                oss << "\n  FrameCache: begin=" << (_detailAccum.frameCacheBeginMs / kWindow)
                    << "ms updateCall=" << (_detailAccum.sceneUpdateCallMs / kWindow)
                    << "ms lateCall=" << (_detailAccum.lateUpdateCallMs / kWindow)
                    << "ms end=" << (_detailAccum.frameCacheEndMs / kWindow) << "ms" << "\n  PrepareDetail: total="
                    << (bridgeProfile.prepareCalls ? bridgeProfile.prepareMs / bridgeProfile.prepareCalls : 0.0)
                    << "ms collect="
                    << (bridgeProfile.prepareCalls ? bridgeProfile.collectMs / bridgeProfile.prepareCalls : 0.0)
                    << "ms update="
                    << (bridgeProfile.prepareCalls ? bridgeProfile.updateMs / bridgeProfile.prepareCalls : 0.0)
                    << "ms cull="
                    << (bridgeProfile.prepareCalls ? bridgeProfile.cullMs / bridgeProfile.prepareCalls : 0.0)
                    << "ms sort="
                    << (bridgeProfile.prepareCalls ? bridgeProfile.sortMs / bridgeProfile.prepareCalls : 0.0)
                    << "ms fastCalls=" << bridgeProfile.prepareFastCalls
                    << " slowCalls=" << bridgeProfile.prepareSlowCalls << " renderables/frame="
                    << (bridgeProfile.prepareCalls ? bridgeProfile.renderables / bridgeProfile.prepareCalls : 0.0)
                    << " visible/frame="
                    << (bridgeProfile.prepareCalls ? bridgeProfile.visible / bridgeProfile.prepareCalls : 0.0)
                    << " cameraCache=" << bridgeProfile.cameraCacheHits << "/" << bridgeProfile.cameraCacheMisses
                    << " miss(world/structure/transform/mask/frustum/vp)=" << bridgeProfile.cameraMissWorld << "/"
                    << bridgeProfile.cameraMissStructural << "/" << bridgeProfile.cameraMissTransform << "/"
                    << bridgeProfile.cameraMissMask << "/" << bridgeProfile.cameraMissFrustum << "/"
                    << bridgeProfile.cameraMissViewProjection
                    << " conservative=" << bridgeProfile.conservativeFullListUses
                    << "\n  SRC: cull=" << (srcProfile.cullCalls ? srcProfile.cullMs / srcProfile.cullCalls : 0.0)
                    << "ms editorCull="
                    << (srcProfile.cullEditorCalls ? srcProfile.cullEditorMs / srcProfile.cullEditorCalls : 0.0)
                    << "ms gameCull="
                    << (srcProfile.cullGameCalls ? srcProfile.cullGameMs / srcProfile.cullGameCalls : 0.0)
                    << "ms applyGraph="
                    << (srcProfile.submitCalls ? srcProfile.applyGraphMs / srcProfile.submitCalls : 0.0)
                    << "ms submit=" << (srcProfile.submitCalls ? srcProfile.submitMs / srcProfile.submitCalls : 0.0)
                    << "ms base=" << (srcProfile.submitCalls ? srcProfile.submitBaseMs / srcProfile.submitCalls : 0.0)
                    << "ms editorAppend="
                    << (srcProfile.submitCalls ? srcProfile.submitEditorAppendMs / srcProfile.submitCalls : 0.0)
                    << "ms ensure="
                    << (srcProfile.submitCalls ? srcProfile.ensureBuffersMs / srcProfile.submitCalls : 0.0)
                    << "ms cache=" << (srcProfile.submitCalls ? srcProfile.cacheGraphMs / srcProfile.submitCalls : 0.0)
                    << "ms finalDraws/submit="
                    << (srcProfile.submitCalls ? srcProfile.finalDrawCalls / srcProfile.submitCalls : 0.0)
                    << " listBorrowed=" << srcProfile.borrowedRendererListSubmits
                    << " listOwned=" << srcProfile.ownedRendererListSubmits << " materialized/frame="
                    << (srcProfile.submitCalls ? srcProfile.materializedDrawCalls / srcProfile.submitCalls : 0.0)
                    << " reuse=" << srcProfile.cachedSubmissionReuses
                    << " reject(editor/visible/shadow/graph/upload/signature)="
                    << srcProfile.submissionRejectEditorAppenders << "/" << srcProfile.submissionRejectOwnedVisibleList
                    << "/" << srcProfile.submissionRejectOwnedShadowList << "/"
                    << srcProfile.submissionRejectMissingGraph << "/" << srcProfile.submissionRejectPendingUploads
                    << "/" << srcProfile.submissionRejectSignature
                    << "\n  CleanupDetail: collectIds=" << (_detailAccum.cleanupCollectIdsMs / kWindow)
                    << "ms release=" << (_detailAccum.cleanupReleaseMs / kWindow)
                    << "ms activeIds/frame=" << (_detailAccum.cleanupActiveIds / kWindow)
                    << " retirePending=" << deletionStats.pending << " retireHighWater=" << deletionStats.highWatermark
                    << " retired=" << deletionStats.retired << " retirePushed=" << deletionStats.pushed
                    << "\n  LightingDetail: collect=" << (_detailAccum.lightingCollectMs / kWindow)
                    << "ms shadowEditor=" << (_detailAccum.lightingShadowEditorMs / kWindow)
                    << "ms shadowGame=" << (_detailAccum.lightingShadowGameMs / kWindow)
                    << "ms upload=" << (_detailAccum.lightingUploadMs / kWindow) << "ms";
            }

            // DrawFrame sub-timing breakdown
            if (m_vkCore) {
                double drawSub[infernux::InxVkCoreModular::kDrawSubSlots];
                int drawN = m_vkCore->GetDrawSubTimings(drawSub);
                if (drawN > 0) {
                    double n = static_cast<double>(drawN);
                    const auto rgProfile = infernux::vk::RenderGraph::GetExecuteProfileSnapshot();
                    const auto topPassProfiles = infernux::vk::RenderGraph::GetTopCallbackProfiles(6);
                    const auto &gpuTimestamps = m_vkCore->GetLatestGpuTimestampFrame();
                    uint64_t filteredCalls = 0, filteredEligible = 0, filteredIssued = 0, filteredActualDraws = 0;
                    uint64_t shadowCalls = 0, shadowEligible = 0, shadowIssued = 0, shadowActualDraws = 0;
                    m_vkCore->GetDrawSubCounters(filteredCalls, filteredEligible, filteredIssued, filteredActualDraws,
                                                 shadowCalls, shadowEligible, shadowIssued, shadowActualDraws);
                    oss << "\n  DrawFrame: Acquire=" << (drawSub[0] / n) << "ms" << " Record=" << (drawSub[1] / n)
                        << "ms" << " Submit=" << (drawSub[2] / n) << "ms" << " Present=" << (drawSub[3] / n) << "ms"
                        << "\n    Record: UBO=" << (drawSub[4] / n) << "ms" << " SceneGraph=" << (drawSub[5] / n)
                        << "ms" << " GUIGraph=" << (drawSub[6] / n) << "ms"
                        << "\n    Filtered: total=" << (drawSub[8] / n) << "ms" << " filter=" << (drawSub[9] / n)
                        << "ms" << " sort=" << (drawSub[10] / n) << "ms" << " draw=" << (drawSub[11] / n) << "ms"
                        << " calls/frame=" << (static_cast<double>(filteredCalls) / n) << " eligible/call="
                        << (filteredCalls ? static_cast<double>(filteredEligible) / static_cast<double>(filteredCalls)
                                          : 0.0)
                        << " issued/call="
                        << (filteredCalls ? static_cast<double>(filteredIssued) / static_cast<double>(filteredCalls)
                                          : 0.0)
                        << " vkDraws/frame=" << (static_cast<double>(filteredActualDraws) / n)
                        << "\n    Shadow: total=" << (drawSub[12] / n) << "ms" << " filter=" << (drawSub[13] / n)
                        << "ms" << " sort=" << (drawSub[15] / n) << "ms" << " cull=" << (drawSub[16] / n) << "ms"
                        << " upload=" << (drawSub[17] / n) << "ms" << " batch=" << (drawSub[18] / n) << "ms"
                        << " calls/frame=" << (static_cast<double>(shadowCalls) / n) << " eligible/call="
                        << (shadowCalls ? static_cast<double>(shadowEligible) / static_cast<double>(shadowCalls) : 0.0)
                        << " issued/call="
                        << (shadowCalls ? static_cast<double>(shadowIssued) / static_cast<double>(shadowCalls) : 0.0)
                        << " vkDraws/frame=" << (static_cast<double>(shadowActualDraws) / n)
                        << "\n    RenderGraph: barrier="
                        << (rgProfile.executeCalls ? rgProfile.barrierMs / static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << "ms begin="
                        << (rgProfile.executeCalls ? rgProfile.beginPassMs / static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << "ms callback="
                        << (rgProfile.executeCalls ? rgProfile.callbackMs / static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << "ms end="
                        << (rgProfile.executeCalls ? rgProfile.endPassMs / static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << "ms passes/exec="
                        << (rgProfile.executeCalls
                                ? static_cast<double>(rgProfile.passCount) / static_cast<double>(rgProfile.executeCalls)
                                : 0.0)
                        << " gfx/exec="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.graphicsPassCount) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " dynamic/exec="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.dynamicRenderingPassCount) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " renderPass/exec="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.legacyRenderPassCount) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " barrierCalls/exec="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.barrierCallCount) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " sync2/exec="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.synchronization2BarrierBatchCount) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " legacy/exec="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.legacyBarrierBatchCount) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " rhiPipeline/exec="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.rhiPipelineBinds) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " skipped="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.rhiPipelineBindSkips) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " rhiGroups/exec="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.rhiGroupBinds) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0)
                        << " skipped="
                        << (rgProfile.executeCalls ? static_cast<double>(rgProfile.rhiGroupBindSkips) /
                                                         static_cast<double>(rgProfile.executeCalls)
                                                   : 0.0);

                    if (gpuTimestamps.available) {
                        oss << "\n    GPU#" << gpuTimestamps.serial << ':';
                        for (uint32_t i = 0; i < gpuTimestamps.sampleCount; ++i) {
                            const auto &sample = gpuTimestamps.samples[i];
                            oss << ' ' << sample.Name() << '=' << sample.milliseconds << "ms";
                        }
                    }

                    if (!topPassProfiles.empty()) {
                        oss << "\n    CallbackTop:";
                        for (const auto &entry : topPassProfiles) {
                            oss << ' ' << entry.name << '=' << (drawN ? entry.totalMs / n : 0.0) << "ms" << '('
                                << entry.calls << ')';
                        }
                    }

                    const auto particlePassProfiles = infernux::vk::RenderGraph::GetParticlePassProfiles(32);
                    if (!particlePassProfiles.empty()) {
                        oss << "\n    ParticleGPU:";
                        for (const auto &entry : particlePassProfiles) {
                            oss << ' ' << entry.name << "{passes=" << entry.passCalls
                                << ",dispatch=" << entry.dispatchCalls << ",direct=" << entry.directDispatchCalls
                                << ",indirect=" << entry.indirectDispatchCalls << ",workgroups=" << entry.workgroups
                                << ",inputs=" << entry.inputCount << ",barrierPhases=" << entry.barrierPhases
                                << ",draws=" << entry.drawCalls << ",indirectDraws=" << entry.indirectDrawCalls << '}';
                        }
                    }

                    // Executor sub-timing breakdown
                    oss << "\n    Executor: sceneExec=" << (m_executorTiming.sceneExecMs / n)
                        << "ms sceneMSAA=" << (m_executorTiming.sceneMsaaMs / n)
                        << "ms gameSetup=" << (m_executorTiming.gameSetupMs / n)
                        << "ms gameExec=" << (m_executorTiming.gameExecMs / n)
                        << "ms gameMSAA=" << (m_executorTiming.gameMsaaMs / n)
                        << "ms gameRestore=" << (m_executorTiming.gameRestoreMs / n) << "ms";
                }
            }

            oss << "\n  Pacing: target=" << (_pacingAccum.targetFps / kWindow)
                << "fps budget=" << (_pacingAccum.frameBudgetMs / kWindow)
                << "ms elapsed=" << (_pacingAccum.elapsedBeforeSleepMs / kWindow)
                << "ms reqSleep=" << (_pacingAccum.requestedSleepMs / kWindow)
                << "ms actualSleep=" << (_pacingAccum.actualSleepMs / kWindow)
                << "ms slept=" << _pacingAccum.sleptFrames << '/' << static_cast<int>(kWindow)
                << " idle=" << _pacingAccum.idleFrames << '/' << static_cast<int>(kWindow)
                << " wakeByEvent=" << _pacingAccum.wokeByEventFrames << " wakeInput=" << _pacingAccum.wokeByInputFrames
                << " wakeWindow=" << _pacingAccum.wokeByWindowFrames << " wakeOther=" << _pacingAccum.wokeByOtherFrames
                << " input=" << _pacingAccum.inputFrames << " playBypass=" << _pacingAccum.playBypassFrames
                << " events/frame=" << (static_cast<double>(_pacingAccum.queuedEvents) / kWindow)
                << " motion/frame=" << (static_cast<double>(_pacingAccum.mouseMotionEvents) / kWindow)
                << " motionDispatch/frame=" << (static_cast<double>(_pacingAccum.dispatchedMouseMotions) / kWindow);
            oss << "\n    InputDetail: begin=" << (_pacingAccum.inputBeginFrameMs / kWindow)
                << "ms pump=" << (_pacingAccum.inputPumpEventsMs / kWindow)
                << "ms peep+dispatch=" << (_pacingAccum.inputPeepEventsMs / kWindow)
                << "ms imgui=" << (_pacingAccum.inputImGuiDispatchMs / kWindow)
                << "ms manager=" << (_pacingAccum.inputManagerDispatchMs / kWindow)
                << "ms synthetic=" << (_pacingAccum.inputSyntheticDispatchMs / kWindow)
                << "ms windowQuery=" << (_pacingAccum.inputWindowQueryMs / kWindow) << "ms";

            oss << "\n  Scene: editorCam=" << (_sceneAccum.editorCameraMs / kWindow)
                << "ms editor=" << (_sceneAccum.editorUpdateMs / kWindow)
                << "ms pending=" << (_sceneAccum.pendingStartsMs / kWindow)
                << "ms syncExt=" << (_sceneAccum.syncExternalMovesMs / kWindow)
                << "ms syncCol=" << (_sceneAccum.syncCollidersMs / kWindow)
                << "ms fixed=" << (_sceneAccum.fixedUpdateMs / kWindow)
                << "ms physics=" << (_sceneAccum.physicsStepMs / kWindow)
                << "ms events=" << (_sceneAccum.physicsEventsMs / kWindow)
                << "ms syncRb=" << (_sceneAccum.syncRigidbodiesMs / kWindow)
                << "ms interp=" << (_sceneAccum.interpolationMs / kWindow)
                << "ms gameplay=" << (_sceneAccum.gameplayUpdateMs / kWindow)
                << "ms late=" << (_sceneAccum.lateUpdateMs / kWindow) << "ms audio=" << (_sceneAccum.audioMs / kWindow)
                << "ms end=" << (_sceneAccum.endFrameMs / kWindow)
                << "ms fixedSteps=" << (_sceneAccum.fixedSteps / kWindow)
                << " fixedFrame=" << sampleMean(_fixedStepFrameSamples) << "ms(" << _fixedStepFrameSamples.size()
                << ") renderFrame=" << sampleMean(_renderOnlyFrameSamples) << "ms(" << _renderOnlyFrameSamples.size()
                << ')';

            double guiTotal = 0.0;
            oss << "\n  UI:";
            for (const auto &name : m_gui->GetRenderableOrder()) {
                auto it = _guiAccum.find(name);
                if (it == _guiAccum.end()) {
                    continue;
                }
                const double avgMs = it->second / kWindow;
                guiTotal += avgMs;
                oss << ' ' << name << '=' << avgMs << "ms";
            }
            oss << " total=" << guiTotal << "ms";

            // Inspector sub-timing breakdown
            if (!_inspSubAccum.empty()) {
                std::vector<std::pair<std::string, double>> inspItems(_inspSubAccum.begin(), _inspSubAccum.end());
                std::sort(inspItems.begin(), inspItems.end(),
                          [](const auto &lhs, const auto &rhs) { return lhs.first < rhs.first; });
                auto isCountMetric = [](const std::string &key) {
                    constexpr char kSuffix[] = "_count";
                    return key.size() >= (sizeof(kSuffix) - 1) &&
                           key.compare(key.size() - (sizeof(kSuffix) - 1), sizeof(kSuffix) - 1, kSuffix) == 0;
                };
                oss << "\n    Inspector:";
                for (const auto &kv : inspItems) {
                    oss << ' ' << kv.first << '=' << (kv.second / kWindow);
                    if (!isCountMetric(kv.first))
                        oss << "ms";
                }
            }
            if (!_hierSubAccum.empty()) {
                std::vector<std::pair<std::string, double>> hierItems(_hierSubAccum.begin(), _hierSubAccum.end());
                std::sort(hierItems.begin(), hierItems.end(),
                          [](const auto &lhs, const auto &rhs) { return lhs.first < rhs.first; });
                oss << "\n    Hierarchy:";
                for (const auto &kv : hierItems)
                    oss << ' ' << kv.first << '=' << (kv.second / kWindow) << "ms";
            }
            if (!_projectSubAccum.empty()) {
                std::vector<std::pair<std::string, double>> projectItems(_projectSubAccum.begin(),
                                                                         _projectSubAccum.end());
                std::sort(projectItems.begin(), projectItems.end(),
                          [](const auto &lhs, const auto &rhs) { return lhs.first < rhs.first; });
                oss << "\n    Project:";
                for (const auto &kv : projectItems)
                    oss << ' ' << kv.first << '=' << (kv.second / kWindow) << "ms";
            }
            // Periodic profiling is diagnostic telemetry, not an engine fault.
            // Keep it visible without poisoning Console warning/error gates.
            INXLOG_INFO(oss.str());
#if INFERNUX_FRAME_PROFILE_TERMINAL
            std::cerr << oss.str() << std::endl;
#endif

            for (double &value : _fpAccum) {
                value = 0.0;
            }
            _deltaAccumMs = 0.0;
            _srpSceneViewMs = 0;
            _srpGameViewMs = 0;
            _frameTimeSamples.clear();
            _fixedStepFrameSamples.clear();
            _renderOnlyFrameSamples.clear();
            _sceneAccum = {};
            _guiAccum.clear();
            _inspSubAccum.clear();
            _hierSubAccum.clear();
            _projectSubAccum.clear();
            _pacingAccum = {};
            _detailAccum = {};
            if (m_vkCore) {
                m_vkCore->ResetDrawSubTimings();
            }
            infernux::vk::RenderGraph::ResetExecuteProfileSnapshot();
            SceneRenderBridge::Instance().ResetProfileSnapshot();
            ScriptableRenderContext::ResetProfileSnapshot();
            m_executorTiming = {};
            _fpCounter = 0;
            _fpLastReport = _fpReportNow;
        }
    }
#endif
}

// ============================================================================
// DrawFrame sub-methods
// ============================================================================

bool InxRenderer::CheckAndApplyMsaaRequest(bool finalFrameCheck, bool sceneViewActive, bool gameViewActive)
{
    m_sceneRequestedMsaaSamples = m_sceneRenderGraph ? m_sceneRenderGraph->GetRequestedMsaaSamples() : 0;
    m_gameRequestedMsaaSamples = 0;
    for (Camera *camera : FindGameCamerasCached()) {
        if (SceneRenderGraph *graph = EnsureGameRenderGraph(camera)) {
            const int request = graph->GetRequestedMsaaSamples();
            if (m_gameRequestedMsaaSamples == 0)
                m_gameRequestedMsaaSamples = request;
            else if (request != 0 && request != m_gameRequestedMsaaSamples)
                INXLOG_ERROR("Active game cameras requested conflicting MSAA sample counts");
        }
    }

    const MsaaRequestResolution rawResolution =
        ResolveMsaaRequests(m_sceneRequestedMsaaSamples, m_gameRequestedMsaaSamples);
    const int currentSamples = GetMsaaSamples();

    // A RenderStack topology is published to Scene and Game by two SRP calls.
    // Before those calls, one graph can still hold the previous revision. Do
    // not turn that transient disagreement into a rejected user edit. The
    // post-SRP check below considers only views that actually participated in
    // this frame; if both active views still disagree, the request is rejected
    // before command recording.
    if (!finalFrameCheck && rawResolution.status == MsaaRequestStatus::ConflictingRequests) {
        m_msaaRequestConflict = false;
        SetEffectiveGraphMsaaSamples(currentSamples);
        return false;
    }

    const MsaaRequestResolution resolution =
        finalFrameCheck ? ResolveActiveMsaaRequests(m_sceneRequestedMsaaSamples, sceneViewActive,
                                                    m_gameRequestedMsaaSamples, gameViewActive)
                        : rawResolution;

    auto rejectRequest = [this, resolution](MsaaRequestStatus status, uint32_t supportedMask) {
        const uint64_t signature = (static_cast<uint64_t>(status) << 56u) |
                                   (static_cast<uint64_t>(m_sceneRequestedMsaaSamples & 0xff) << 40u) |
                                   (static_cast<uint64_t>(m_gameRequestedMsaaSamples & 0xff) << 32u) | supportedMask;
        if (signature == m_lastMsaaRejectionSignature)
            return;
        m_lastMsaaRejectionSignature = signature;
        ++m_msaaRejectedRequestCount;

        if (status == MsaaRequestStatus::ConflictingRequests) {
            INXLOG_ERROR(
                "Scene and Game render graphs request incompatible global MSAA settings (Scene=",
                m_sceneRequestedMsaaSamples, "x, Game=", m_gameRequestedMsaaSamples,
                "x). Keeping the last-known-good setting; both graphs must agree while material pipelines are shared.");
        } else if (status == MsaaRequestStatus::InvalidSceneRequest ||
                   status == MsaaRequestStatus::InvalidGameRequest) {
            INXLOG_ERROR("Invalid render graph MSAA request (Scene=", m_sceneRequestedMsaaSamples,
                         "x, Game=", m_gameRequestedMsaaSamples, "x). Valid values are 0, 1, 2, 4, and 8.");
        } else {
            INXLOG_ERROR("Requested ", resolution.samples,
                         "x MSAA is unsupported by the HDR color/depth target pair (supported mask=", supportedMask,
                         "). Keeping the last-known-good setting.");
        }
    };

    if (resolution.status == MsaaRequestStatus::NoRequest) {
        m_msaaRequestConflict = false;
        m_lastMsaaRejectionSignature = 0;
        SetEffectiveGraphMsaaSamples(currentSamples);
        return false;
    }

    if (!resolution.IsAccepted()) {
        m_msaaRequestConflict = resolution.status == MsaaRequestStatus::ConflictingRequests;
        rejectRequest(resolution.status, GetSupportedMsaaSampleMask());
        SetEffectiveGraphMsaaSamples(currentSamples);
        return finalFrameCheck;
    }

    const uint32_t supportedMask = GetSupportedMsaaSampleMask();
    if (!SupportsMsaaSampleCount(static_cast<rhi::SampleCountMask>(supportedMask), resolution.samples)) {
        m_msaaRequestConflict = false;
        rejectRequest(MsaaRequestStatus::Accepted, supportedMask);
        SetEffectiveGraphMsaaSamples(currentSamples);
        return false;
    }

    m_msaaRequestConflict = false;
    m_lastMsaaRejectionSignature = 0;
    SetEffectiveGraphMsaaSamples(resolution.samples);
    const bool reconfigured = ApplyMsaaSamples(resolution.samples, "render graph");
    if (!reconfigured)
        SetEffectiveGraphMsaaSamples(GetMsaaSamples());
    return reconfigured;
}

void InxRenderer::StageEngineGlobalsUBO()
{
    m_totalTime += m_deltaTime;
    m_smoothDeltaTime += (m_deltaTime - m_smoothDeltaTime) * 0.1f;
    ++m_frameCount;

    EngineGlobalsUBO globals{};

    // Time
    float t = m_totalTime;
    globals.time = glm::vec4(t, std::sin(t), std::cos(t), m_deltaTime);
    globals.sinTime = glm::vec4(std::sin(t / 20.0f), std::sin(t / 4.0f), std::sin(t * 2.0f), std::sin(t * 3.0f));
    globals.cosTime = glm::vec4(std::cos(t / 20.0f), std::cos(t / 4.0f), std::cos(t * 2.0f), std::cos(t * 3.0f));

    // Screen params (from scene render target or window)
    float w = 1.0f, h = 1.0f;
    if (m_sceneRenderTarget && m_sceneRenderTarget->GetWidth() > 0) {
        w = static_cast<float>(m_sceneRenderTarget->GetWidth());
        h = static_cast<float>(m_sceneRenderTarget->GetHeight());
    }
    globals.screenParams = glm::vec4(w, h, 1.0f / w, 1.0f / h);

    // Camera
    globals.worldSpaceCameraPos = glm::vec4(m_cameraPos[0], m_cameraPos[1], m_cameraPos[2], 1.0f);

    float nearClip = 0.01f, farClip = 5000.0f;
    Camera *editorCam = SceneManager::Instance().GetEditorCameraController().GetCamera();
    if (editorCam) {
        nearClip = editorCam->GetNearClip();
        farClip = editorCam->GetFarClip();
    }
    globals.projectionParams = glm::vec4(nearClip, farClip, 1.0f / farClip, nearClip / farClip);

    // ZBuffer linearization helpers (reversed-Z compatible)
    float fn = farClip / nearClip;
    globals.zBufferParams = glm::vec4(1.0f - fn, fn, (1.0f - fn) / farClip, fn / farClip);

    // Frame
    float dt = m_deltaTime > kEpsilon ? m_deltaTime : kEpsilon;
    globals.frameParams = glm::vec4(static_cast<float>(m_frameCount), m_smoothDeltaTime, 1.0f / dt, 0.0f);

    m_vkCore->StageGlobals(globals);
}

void InxRenderer::CleanupDrawCallBuffers()
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    auto t0 = Clock::now();
#endif
#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.cleanupCollectIdsMs = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
    t0 = Clock::now();
#endif

    if (m_vkCore) {
        const size_t activeObjectCount = m_vkCore->CleanupUnusedBuffersByFrameStamp();
#if INFERNUX_FRAME_PROFILE
        m_frameDetailTiming.cleanupActiveIds = static_cast<double>(activeObjectCount);
#endif
    }

#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.cleanupReleaseMs = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
}

void InxRenderer::WaitForGpuIdle()
{
    if (!m_vkCore) {
        return;
    }

    if (m_sceneRenderGraph) {
        m_sceneRenderGraph->ClearCachedFrameState();
    }
    for (auto &[cameraId, graph] : m_gameRenderGraphs) {
        (void)cameraId;
        graph->ClearCachedFrameState();
    }

    // Drop the currently staged draw list as well. If the upcoming frame skips
    // re-submitting one of the graphs (for example because no valid game camera
    // exists yet), VkCore must not keep drawing the previous scene's objects.
    m_vkCore->SetDrawCalls(nullptr);

    // Scene replacement destroys meshes/materials/textures immediately.
    // A full device drain avoids probabilistic crashes from old-scene resources
    // still being referenced by previously submitted command buffers.
    m_vkCore->GetDeviceContext().WaitIdle();
    m_vkCore->FlushRetiredGpuResources();

    // Pump the OS message queue so Windows does not flag the application
    // as "Not Responding" during long GPU drains (scene switch, MSAA change).
    SDL_PumpEvents();
}

size_t InxRenderer::GetPendingMeshUploadCount() const
{
    return m_vkCore ? m_vkCore->GetPendingMeshUploadCount() : 0;
}

uint64_t InxRenderer::GetSubmittedMeshUploadCount() const
{
    return m_vkCore ? m_vkCore->GetSubmittedMeshUploadCount() : 0;
}

uint64_t InxRenderer::GetCompletedMeshUploadCount() const
{
    return m_vkCore ? m_vkCore->GetCompletedMeshUploadCount() : 0;
}

uint64_t InxRenderer::GetAsyncMeshUploadCount() const
{
    return m_vkCore ? m_vkCore->GetAsyncMeshUploadCount() : 0;
}

size_t InxRenderer::GetPendingTextureCpuLoadCount() const
{
    return m_vkCore ? m_vkCore->GetPendingTextureCpuLoadCount() : 0;
}

size_t InxRenderer::GetPendingTextureUploadCount() const
{
    return m_vkCore ? m_vkCore->GetPendingTextureUploadCount() : 0;
}

uint64_t InxRenderer::GetSubmittedTextureUploadCount() const
{
    return m_vkCore ? m_vkCore->GetSubmittedTextureUploadCount() : 0;
}

uint64_t InxRenderer::GetCompletedTextureUploadCount() const
{
    return m_vkCore ? m_vkCore->GetCompletedTextureUploadCount() : 0;
}

uint64_t InxRenderer::GetAsyncTextureUploadCount() const
{
    return m_vkCore ? m_vkCore->GetAsyncTextureUploadCount() : 0;
}

uint64_t InxRenderer::GetStagingPoolBytes() const
{
    return m_vkCore ? m_vkCore->GetStagingPoolBytes() : 0;
}

size_t InxRenderer::GetStagingPoolBufferCount() const
{
    return m_vkCore ? m_vkCore->GetStagingPoolBufferCount() : 0;
}

uint64_t InxRenderer::GetStagingAllocationCount() const
{
    return m_vkCore ? m_vkCore->GetStagingAllocationCount() : 0;
}

uint64_t InxRenderer::GetStagingReuseCount() const
{
    return m_vkCore ? m_vkCore->GetStagingReuseCount() : 0;
}

uint64_t InxRenderer::GetStagingDiscardCount() const
{
    return m_vkCore ? m_vkCore->GetStagingDiscardCount() : 0;
}

uint64_t InxRenderer::GetTextureGpuResidentBytes() const
{
    return m_vkCore ? m_vkCore->GetTextureGpuResidentBytes() : 0;
}

uint64_t InxRenderer::GetTextureGpuBudgetBytes() const
{
    return m_vkCore ? m_vkCore->GetTextureGpuBudgetBytes() : 0;
}

size_t InxRenderer::GetTextureGpuCacheEntryCount() const
{
    return m_vkCore ? m_vkCore->GetTextureGpuCacheEntryCount() : 0;
}

size_t InxRenderer::GetRetiredTextureGpuLeaseCount() const
{
    return m_vkCore ? m_vkCore->GetRetiredTextureGpuLeaseCount() : 0;
}

uint64_t InxRenderer::GetTextureGpuEvictionCount() const
{
    return m_vkCore ? m_vkCore->GetTextureGpuEvictionCount() : 0;
}

void InxRenderer::SetTextureGpuBudgetBytes(uint64_t bytes)
{
    if (!m_vkCore)
        throw std::logic_error("Cannot set the GPU texture budget before renderer initialization");
    m_vkCore->SetTextureGpuBudgetBytes(bytes);
}

size_t InxRenderer::TrimTextureGpuBudget()
{
    if (!m_vkCore)
        throw std::logic_error("Cannot trim the GPU texture budget before renderer initialization");
    return m_vkCore->TrimTextureGpuBudget();
}

uint64_t InxRenderer::GetMeshGpuResidentBytes() const
{
    return m_vkCore ? m_vkCore->GetMeshGpuResidentBytes() : 0;
}

uint64_t InxRenderer::GetMeshGpuBudgetBytes() const
{
    return m_vkCore ? m_vkCore->GetMeshGpuBudgetBytes() : 0;
}

size_t InxRenderer::GetMeshGpuCacheEntryCount() const
{
    return m_vkCore ? m_vkCore->GetMeshGpuCacheEntryCount() : 0;
}

size_t InxRenderer::GetRetiredMeshGpuLeaseCount() const
{
    return m_vkCore ? m_vkCore->GetRetiredMeshGpuLeaseCount() : 0;
}

uint64_t InxRenderer::GetMeshGpuEvictionCount() const
{
    return m_vkCore ? m_vkCore->GetMeshGpuEvictionCount() : 0;
}

void InxRenderer::SetMeshGpuBudgetBytes(uint64_t bytes)
{
    if (!m_vkCore)
        throw std::logic_error("Cannot set the GPU mesh budget before renderer initialization");
    m_vkCore->SetMeshGpuBudgetBytes(bytes);
}

size_t InxRenderer::TrimMeshGpuBudget()
{
    if (!m_vkCore)
        throw std::logic_error("Cannot trim the GPU mesh budget before renderer initialization");
    return m_vkCore->TrimMeshGpuBudget();
}

uint64_t InxRenderer::GetGpuResidencyBudgetBytes() const
{
    if (m_gpuResidencyBudgetBytes != 0)
        return m_gpuResidencyBudgetBytes;
    if (!m_vkCore)
        return 0;
    const auto statistics = vk::QueryVmaRuntimeStatistics(m_vkCore->GetDeviceContext().GetVmaAllocator(),
                                                          m_vkCore->GetDeviceContext().GetPhysicalDevice());
    const uint64_t totalManagedBudget = (statistics.deviceLocalBudgetBytes / 10ULL) * 7ULL;
    const uint64_t editorTextureBudget = m_gui && !m_guiPlayerMode ? m_gui->GetImGuiTextureBudgetBytes() : 0;
    return totalManagedBudget > editorTextureBudget ? totalManagedBudget - editorTextureBudget : totalManagedBudget;
}

GpuResidencySnapshot InxRenderer::GetGpuResidencySnapshot() const
{
    GpuResidencySnapshot snapshot;
    if (!m_vkCore)
        return snapshot;

    const auto statistics = vk::QueryVmaRuntimeStatistics(m_vkCore->GetDeviceContext().GetVmaAllocator(),
                                                          m_vkCore->GetDeviceContext().GetPhysicalDevice());
    snapshot.runtimeBudgetBytes = GetGpuResidencyBudgetBytes();
    snapshot.editorTextureBudgetBytes = m_gui && !m_guiPlayerMode ? m_gui->GetImGuiTextureBudgetBytes() : 0;
    snapshot.budgetBytes = snapshot.runtimeBudgetBytes + snapshot.editorTextureBudgetBytes;
    snapshot.allocatorAllocationBytes = statistics.allocationBytes;
    snapshot.allocatorBlockBytes = statistics.blockBytes;
    snapshot.deviceLocalAllocationBytes = statistics.deviceLocalAllocationBytes;
    snapshot.deviceLocalUsageBytes = statistics.deviceLocalUsageBytes;
    snapshot.deviceLocalBudgetBytes = statistics.deviceLocalBudgetBytes;
    snapshot.allocatorAllocationCount = statistics.allocationCount;
    snapshot.meshBytes = m_vkCore->GetMeshGpuResidentBytes();
    snapshot.textureBytes = m_vkCore->GetTextureGpuResidentBytes();
    snapshot.imguiTextureBytes = m_gui ? m_gui->GetImGuiTextureResidentBytes() : 0;
    snapshot.pendingImguiTextureBytes = m_gui ? m_gui->GetPendingImGuiTextureUploadBytes() : 0;
    snapshot.stagingPoolBytes = m_vkCore->GetStagingPoolBytes();
    snapshot.pendingReadbackBytes = m_vkCore->GetResourceManager().GetPendingImageReadbackBytes();
    snapshot.pendingReadbackCount = m_vkCore->GetResourceManager().GetPendingImageReadbackCount();
    snapshot.pendingGpuTransferCount = m_vkCore->GetResourceManager().GetPendingGpuTransferCount();
    snapshot.uploadTimelineEnabled = m_vkCore->GetResourceManager().IsUploadTimelineEnabled();
    snapshot.timelineUploadPublicationCount = m_vkCore->GetResourceManager().GetTimelineUploadPublicationCount();
    snapshot.requiredUploadTimelineValue = m_vkCore->GetResourceManager().GetRequiredUploadTimelineValue();
    snapshot.deviceWaitIdleCount = m_vkCore->GetDeviceContext().GetWaitIdleCount();
    snapshot.shaderHotReloadRetirementCount = m_vkCore->GetShaderHotReloadRetirementCount();
    snapshot.pendingAsyncGraphicsSubmissionCount =
        m_vkCore->GetResourceManager().GetPendingAsyncGraphicsSubmissionCount();
    snapshot.asyncGraphicsSubmissionCount = m_vkCore->GetResourceManager().GetAsyncGraphicsSubmissionCount();
    snapshot.renderTargetBytes = (m_sceneRenderTarget ? m_sceneRenderTarget->GetResidentBytes() : 0) +
                                 (m_gameRenderTarget ? m_gameRenderTarget->GetResidentBytes() : 0);
    snapshot.renderGraphBytes = m_sceneRenderGraph ? m_sceneRenderGraph->GetTransientResidentBytes() : 0;
    for (const auto &[cameraId, graph] : m_gameRenderGraphs) {
        (void)cameraId;
        snapshot.renderGraphBytes += graph->GetTransientResidentBytes();
    }
    snapshot.transientPoolBytes = m_transientResourcePool ? m_transientResourcePool->GetResidentBytes() : 0;
    const MaterialGpuResidencySnapshot materialResidency = m_vkCore->GetMaterialGpuResidency();
    snapshot.materialUboBytes = materialResidency.uboBytes;
    snapshot.materialRenderDataCount = materialResidency.renderDataCount;
    snapshot.runtimeMaterialCount = materialResidency.runtimeMaterialCount;
    snapshot.assetMaterialCount = materialResidency.assetMaterialCount;
    snapshot.materialDescriptorSetCount = materialResidency.descriptorSetCount;
    snapshot.pendingMaterialTextureDescriptorSetCount = materialResidency.pendingTextureDescriptorSetCount;
    snapshot.retiredMaterialDescriptorSetCount = materialResidency.retiredDescriptorSetCount;
    snapshot.materialDescriptorPoolCount = materialResidency.descriptorPoolCount;
    snapshot.materialPipelineCount = materialResidency.pipelineCount;
    snapshot.shadowMaterialDescriptorSetCount = materialResidency.shadowDescriptorSetCount;
    snapshot.shadowMaterialDescriptorPoolCount = materialResidency.shadowDescriptorPoolCount;
    snapshot.shadowMaterialBindingCacheHits = materialResidency.shadowBindingCacheHits;
    snapshot.shadowMaterialBindingCacheMisses = materialResidency.shadowBindingCacheMisses;
    snapshot.shadowMaterialBindingRetirements = materialResidency.shadowBindingRetirements;
    snapshot.runtimeMeshEntryCount = m_vkCore->GetRuntimeMeshGpuEntryCount();
    snapshot.runtimeMeshBytes = m_vkCore->GetRuntimeMeshGpuResidentBytes();
    snapshot.scheduledReleaseBytes = m_vkCore->GetRetiredMeshGpuLeaseBytes() +
                                     m_vkCore->GetRetiredTextureGpuLeaseBytes() +
                                     (m_gui ? m_gui->GetScheduledTextureReleaseBytes() : 0);
    snapshot.trackedBytes = snapshot.meshBytes + snapshot.textureBytes + snapshot.imguiTextureBytes +
                            snapshot.pendingImguiTextureBytes + snapshot.stagingPoolBytes +
                            snapshot.pendingReadbackBytes + snapshot.renderTargetBytes + snapshot.renderGraphBytes +
                            snapshot.transientPoolBytes + snapshot.materialUboBytes;
    snapshot.unclassifiedBytes = snapshot.allocatorAllocationBytes > snapshot.trackedBytes
                                     ? snapshot.allocatorAllocationBytes - snapshot.trackedBytes
                                     : 0;
    snapshot.effectiveAllocationBytes = snapshot.deviceLocalAllocationBytes > snapshot.scheduledReleaseBytes
                                            ? snapshot.deviceLocalAllocationBytes - snapshot.scheduledReleaseBytes
                                            : 0;
    snapshot.editorTextureEffectiveAllocationBytes =
        m_guiPlayerMode ? uint64_t{0} : snapshot.imguiTextureBytes + snapshot.pendingImguiTextureBytes;
    snapshot.runtimeEffectiveAllocationBytes =
        snapshot.effectiveAllocationBytes > snapshot.editorTextureEffectiveAllocationBytes
            ? snapshot.effectiveAllocationBytes - snapshot.editorTextureEffectiveAllocationBytes
            : 0;
    snapshot.runtimeOverBudgetBytes = snapshot.runtimeEffectiveAllocationBytes > snapshot.runtimeBudgetBytes
                                          ? snapshot.runtimeEffectiveAllocationBytes - snapshot.runtimeBudgetBytes
                                          : 0;
    snapshot.editorTextureOverBudgetBytes =
        snapshot.editorTextureEffectiveAllocationBytes > snapshot.editorTextureBudgetBytes
            ? snapshot.editorTextureEffectiveAllocationBytes - snapshot.editorTextureBudgetBytes
            : 0;
    snapshot.overBudgetBytes = snapshot.runtimeOverBudgetBytes + snapshot.editorTextureOverBudgetBytes;
    return snapshot;
}

void InxRenderer::PollGpuCompletions()
{
    if (m_vkCore)
        m_vkCore->CollectRetiredGpuResources();
}

void InxRenderer::SetGpuResidencyBudgetBytes(uint64_t bytes)
{
    if (bytes == 0)
        throw std::invalid_argument("GPU residency budget must be greater than zero");
    m_gpuResidencyBudgetBytes = bytes;
    (void)TrimGpuResidencyBudget();
}

size_t InxRenderer::TrimGpuResidencyBudget()
{
    if (!m_vkCore)
        throw std::logic_error("Cannot trim GPU residency before renderer initialization");

    auto snapshot = GetGpuResidencySnapshot();
    uint64_t projectedBytes = snapshot.runtimeEffectiveAllocationBytes;
    size_t evicted = m_gui ? m_gui->TrimImGuiTextureBudget() : 0;
    while (projectedBytes > snapshot.runtimeBudgetBytes) {
        enum class Domain
        {
            None,
            Mesh,
            Texture,
        };
        Domain domain = Domain::None;
        GpuEvictionCandidate selected;
        const auto consider = [&](Domain candidateDomain, GpuEvictionCandidate candidate) {
            if (!candidate.valid)
                return;
            if (!selected.valid || candidate.lastUsedFrame < selected.lastUsedFrame ||
                (candidate.lastUsedFrame == selected.lastUsedFrame &&
                 candidate.residentBytes > selected.residentBytes)) {
                selected = candidate;
                domain = candidateDomain;
            }
        };
        consider(Domain::Mesh, m_vkCore->PeekOldestMeshGpuEvictable());
        consider(Domain::Texture, m_vkCore->PeekOldestTextureGpuEvictable());
        if (!selected.valid)
            break;

        uint64_t releasedBytes = 0;
        switch (domain) {
        case Domain::Mesh:
            releasedBytes = m_vkCore->EvictOldestMeshGpu();
            break;
        case Domain::Texture:
            releasedBytes = m_vkCore->EvictOldestTextureGpu();
            break;
        case Domain::None:
            break;
        }
        if (releasedBytes == 0)
            break;
        projectedBytes -= std::min(projectedBytes, releasedBytes);
        ++evicted;
    }
    return evicted;
}

std::vector<GpuAssetResidencyRecord> InxRenderer::GetAssetGpuResidency() const
{
    return m_vkCore ? m_vkCore->GetAssetGpuResidency() : std::vector<GpuAssetResidencyRecord>{};
}

void InxRenderer::LoadShader(const char *name, const std::vector<char> &code, const char *type)
{
    m_vkCore->LoadShader(name, code, type);
}

void InxRenderer::SetShaderAssetResolver(std::function<bool(const std::string &, const std::string &)> resolver)
{
    if (m_vkCore)
        m_vkCore->SetShaderAssetResolver(std::move(resolver));
}

bool InxRenderer::PublishShaderProgramArtifact(const ShaderProgramArtifact &artifact)
{
    return m_vkCore && m_vkCore->PublishShaderProgramArtifact(artifact);
}

bool InxRenderer::HasShaderProgramArtifact(const ShaderProgramKey &programKey) const
{
    return m_vkCore && m_vkCore->HasShaderProgramArtifact(programKey);
}

std::shared_ptr<const ShaderProgramArtifact>
InxRenderer::ResolveShaderProgramArtifact(const std::shared_ptr<InxMaterial> &material)
{
    if (!m_vkCore || !material)
        return nullptr;
    if (m_shaderProgramArtifactResolver)
        m_shaderProgramArtifactResolver(material);
    return m_vkCore->CopyShaderProgramArtifact({material->GetVertShaderName(), material->GetFragShaderName()});
}

void InxRenderer::SetShaderProgramArtifactResolver(std::function<void(const std::shared_ptr<InxMaterial> &)> resolver)
{
    m_shaderProgramArtifactResolver = std::move(resolver);
    if (m_vkCore)
        m_vkCore->SetShaderProgramArtifactResolver(m_shaderProgramArtifactResolver);
}

void InxRenderer::StoreShaderRenderMeta(const std::string &shaderId, const std::string &cullMode,
                                        const std::string &depthWrite, const std::string &depthTest,
                                        const std::string &blend, int queue, const std::string &passTag,
                                        const std::string &stencil, const std::string &alphaClip)
{
    m_vkCore->StoreShaderRenderMeta(shaderId, cullMode, depthWrite, depthTest, blend, queue, passTag, stencil,
                                    alphaClip);
}

bool InxRenderer::HasShader(const std::string &name, const std::string &type) const
{
    if (!m_vkCore)
        return false;
    return m_vkCore->HasShader(name, type);
}

bool InxRenderer::GetUserEvent()
{
    return m_view->GetUserEvent();
}

uint64_t InxRenderer::QueueSyntheticKeyInput(int scancode, bool pressed, bool repeat)
{
    return m_view ? m_view->QueueSyntheticKeyInput(scancode, pressed, repeat) : 0;
}

uint64_t InxRenderer::QueueSyntheticMouseButtonInput(int button, bool pressed, float x, float y)
{
    return m_view ? m_view->QueueSyntheticMouseButtonInput(button, pressed, x, y) : 0;
}

uint64_t InxRenderer::QueueSyntheticMouseMotionInput(float x, float y, float deltaX, float deltaY)
{
    return m_view ? m_view->QueueSyntheticMouseMotionInput(x, y, deltaX, deltaY) : 0;
}

uint64_t InxRenderer::QueueSyntheticMouseWheelInput(float horizontal, float vertical)
{
    return m_view ? m_view->QueueSyntheticMouseWheelInput(horizontal, vertical) : 0;
}

uint64_t InxRenderer::QueueSyntheticTextInput(const std::string &text)
{
    return m_view ? m_view->QueueSyntheticTextInput(text) : 0;
}

uint64_t InxRenderer::QueueSyntheticCloseRequest()
{
    return m_view ? m_view->QueueSyntheticCloseRequest() : 0;
}

uint64_t InxRenderer::GetLastProcessedSyntheticInputSequence() const
{
    return m_view ? m_view->GetLastProcessedSyntheticInputSequence() : 0;
}

size_t InxRenderer::GetPendingSyntheticInputCount() const
{
    return m_view ? m_view->GetPendingSyntheticInputCount() : 0;
}

void InxRenderer::ShowWindow()
{
    m_view->Show();
}

bool InxRenderer::PumpStartupEvents()
{
    return !m_view || m_view->PumpStartupEvents();
}

void InxRenderer::HideWindow()
{
    m_view->Hide();
}

bool InxRenderer::IsWindowMinimized() const
{
    return m_view && m_view->IsMinimized();
}

void InxRenderer::SetWindowIcon(const std::string &iconPath)
{
    if (m_view) {
        m_view->SetWindowIcon(iconPath);
    }
}

void InxRenderer::SetWindowFullscreen(bool fullscreen)
{
    if (m_view) {
        m_view->SetWindowFullscreen(fullscreen);
    }
}

void InxRenderer::SetWindowTitle(const std::string &title)
{
    if (m_view) {
        m_view->SetWindowTitle(title);
    }
}

void InxRenderer::SetWindowMaximized(bool maximized)
{
    if (m_view) {
        m_view->SetWindowMaximized(maximized);
    }
}

void InxRenderer::SetWindowResizable(bool resizable)
{
    if (m_view) {
        m_view->SetWindowResizable(resizable);
    }
}

bool InxRenderer::IsCloseRequested() const
{
    return m_view && m_view->IsCloseRequested();
}

void InxRenderer::ConfirmClose()
{
    if (m_view) {
        m_view->ConfirmClose();
    }
}

void InxRenderer::CancelClose()
{
    if (m_view) {
        m_view->CancelClose();
    }
}

void InxRenderer::SetGUIFont(const char *fontPath, float fontSize)
{
    if (m_gui) {
        m_gui->SetGUIFont(fontPath, fontSize);
    } else {
        INXLOG_ERROR("InxGUI is not initialized.");
    }
}

float InxRenderer::GetDisplayScale() const
{
    if (m_gui) {
        return m_gui->GetDisplayScale();
    }
    return 1.0f;
}

void InxRenderer::RegisterGUIRenderable(const char *name, std::shared_ptr<InxGUIRenderable> renderable, int priority)
{
    if (m_gui) {
        m_gui->Register(name, std::move(renderable), priority);
    } else {
        INXLOG_ERROR("InxGUI is not initialized.");
    }
}

void InxRenderer::UnregisterGUIRenderable(const char *name)
{
    if (m_gui) {
        m_gui->Unregister(name);
    } else {
        INXLOG_ERROR("InxGUI is not initialized.");
    }
}

void InxRenderer::SetGUIPlayerMode(bool enabled)
{
    m_guiPlayerMode = enabled;
    if (m_gui) {
        m_gui->SetPlayerMode(enabled);
    }
}

void InxRenderer::QueueDockTabSelection(const char *windowId, bool allowDuringModal)
{
    if (m_gui) {
        m_gui->QueueDockTabSelection(windowId != nullptr ? windowId : "", allowDuringModal);
    } else {
        INXLOG_ERROR("InxGUI is not initialized.");
    }
}

uint64_t InxRenderer::SubmitTextureForImGui(const std::string &name, const unsigned char *pixels, size_t byteCount,
                                            int width, int height, rhi::FilterMode filter, bool pinned)
{
    if (!m_gui)
        throw std::logic_error("ImGui texture submission requires an initialized GUI");
    const VkFilter vkFilter = filter == rhi::FilterMode::Nearest ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
    return m_gui->SubmitTextureForImGui(name, pixels, byteCount, width, height, vkFilter, pinned);
}

uint64_t InxRenderer::QueryImportedTextureForImGui(const std::string &name, const std::string &textureGuid)
{
    if (!m_gui || !m_vkCore || textureGuid.empty())
        return 0;
    auto texture = m_vkCore->ResolveTextureForEditorPreview(textureGuid);
    return texture ? m_gui->PublishTextureViewForImGui(name, std::move(texture)) : 0;
}

void InxRenderer::SupersedePendingImGuiTextureUploads(const std::string &name)
{
    if (m_gui)
        m_gui->SupersedePendingImGuiTextureUploads(name);
}

void InxRenderer::RemoveImGuiTexture(const std::string &name)
{
    if (m_gui) {
        m_gui->RemoveImGuiTexture(name);
    } else {
        INXLOG_ERROR("InxGUI is not initialized.");
    }
}

bool InxRenderer::HasImGuiTexture(const std::string &name) const
{
    if (m_gui) {
        return m_gui->HasImGuiTexture(name);
    }
    return false;
}

uint64_t InxRenderer::GetImGuiTextureId(const std::string &name) const
{
    if (m_gui) {
        return m_gui->GetImGuiTextureId(name);
    }
    return 0;
}

uint64_t InxRenderer::GetImGuiTextureVersion(const std::string &name) const
{
    return m_gui ? m_gui->GetImGuiTextureVersion(name) : 0;
}

uint64_t InxRenderer::GetFailedImGuiTextureVersion(const std::string &name) const
{
    return m_gui ? m_gui->GetFailedImGuiTextureVersion(name) : 0;
}

void InxRenderer::SetImGuiTextureBudgetBytes(uint64_t bytes)
{
    if (!m_gui)
        throw std::logic_error("ImGui texture budgeting requires an initialized GUI");
    m_gui->SetImGuiTextureBudgetBytes(bytes);
}

size_t InxRenderer::TrimImGuiTextureBudget()
{
    if (!m_gui)
        throw std::logic_error("ImGui texture budgeting requires an initialized GUI");
    return m_gui->TrimImGuiTextureBudget();
}

uint64_t InxRenderer::GetImGuiTextureBudgetBytes() const
{
    return m_gui ? m_gui->GetImGuiTextureBudgetBytes() : 0;
}

uint64_t InxRenderer::GetImGuiTextureResidentBytes() const
{
    return m_gui ? m_gui->GetImGuiTextureResidentBytes() : 0;
}

size_t InxRenderer::GetImGuiTextureEntryCount() const
{
    return m_gui ? m_gui->GetImGuiTextureEntryCount() : 0;
}

size_t InxRenderer::GetPendingImGuiTextureUploadCount() const
{
    return m_gui ? m_gui->GetPendingImGuiTextureUploadCount() : 0;
}

uint64_t InxRenderer::GetPendingImGuiTextureUploadBytes() const
{
    return m_gui ? m_gui->GetPendingImGuiTextureUploadBytes() : 0;
}

uint64_t InxRenderer::GetSubmittedImGuiTextureUploadCount() const
{
    return m_gui ? m_gui->GetSubmittedImGuiTextureUploadCount() : 0;
}

uint64_t InxRenderer::GetCompletedImGuiTextureUploadCount() const
{
    return m_gui ? m_gui->GetCompletedImGuiTextureUploadCount() : 0;
}

uint64_t InxRenderer::GetAsyncImGuiTextureUploadCount() const
{
    return m_gui ? m_gui->GetAsyncImGuiTextureUploadCount() : 0;
}

uint64_t InxRenderer::GetImGuiTextureEvictionCount() const
{
    return m_gui ? m_gui->GetImGuiTextureEvictionCount() : 0;
}

ResourcePreviewManager *InxRenderer::GetResourcePreviewManager()
{
    if (m_gui) {
        return &m_gui->GetResourcePreviewManager();
    }
    return nullptr;
}

void InxRenderer::SetLogLevel(LogLevel level)
{
    INXLOG_SET_LEVEL(level);
}

void InxRenderer::InitializeDefaultScene()
{
    // Initialize editor gizmos
    m_editorGizmos = std::make_unique<EditorGizmos>();

    // Initialize editor tools (translate/rotate/scale handles)
    m_editorTools = std::make_unique<EditorTools>();

    // Initialize component gizmos buffer used by the scripting layer
    m_componentGizmos = std::make_unique<GizmosDrawCallBuffer>();
    m_particleGpuDrawRegistry = std::make_unique<particle::ParticleGpuDrawRegistry>();
    if (m_scenePickingService)
        m_scenePickingService->SetParticleGpuDrawRegistry(m_particleGpuDrawRegistry.get());

    // Pass gizmos reference to VkCore for rendering
    if (m_vkCore) {
        m_vkCore->SetEditorGizmos(m_editorGizmos.get());
    }

    // Create a default scene
    Scene *scene = SceneManager::Instance().CreateScene("Default Scene");

    // Create a directional light (sun)
    GameObject *lightObj = scene->CreateGameObject("Directional Light");
    lightObj->GetTransform()->SetEulerAngles(glm::vec3(-50.0f, -30.0f, 0.0f)); // Angled downward (degrees)
    Light *light = lightObj->AddComponent<Light>();
    light->SetLightType(LightType::Directional);
    light->SetColor(glm::vec3(1.0f, 0.95f, 0.9f)); // Warm white sunlight
    light->SetIntensity(1.0f);
    light->SetShadows(LightShadows::Hard); // Enable hard shadows by default

    // Create a cube game object with MeshRenderer
    GameObject *cube = scene->CreateGameObject("Cube");
    cube->GetTransform()->SetPosition(glm::vec3(0.0f, 0.5f, 0.0f)); // Raise above ground plane

    // Add MeshRenderer with cube mesh data
    MeshRenderer *renderer = cube->AddComponent<MeshRenderer>();
    renderer->SetMesh(
        std::vector<Vertex>(PrimitiveMeshes::GetCubeVertices().begin(), PrimitiveMeshes::GetCubeVertices().end()),
        std::vector<uint32_t>(PrimitiveMeshes::GetCubeIndices().begin(), PrimitiveMeshes::GetCubeIndices().end()));

    // Create a ground plane to receive shadows
    GameObject *ground = scene->CreateGameObject("Ground");
    ground->GetTransform()->SetPosition(glm::vec3(0.0f, -0.5f, 0.0f));
    ground->GetTransform()->SetScale(glm::vec3(20.0f, 0.01f, 20.0f)); // Thin, wide plane
    MeshRenderer *groundRenderer = ground->AddComponent<MeshRenderer>();
    groundRenderer->SetMesh(
        std::vector<Vertex>(PrimitiveMeshes::GetCubeVertices().begin(), PrimitiveMeshes::GetCubeVertices().end()),
        std::vector<uint32_t>(PrimitiveMeshes::GetCubeIndices().begin(), PrimitiveMeshes::GetCubeIndices().end()));

    // Setup editor camera
    EditorCameraController &editorCam = SceneManager::Instance().GetEditorCameraController();
    editorCam.Reset();

    // Position camera to see the cube - use FocusOn to keep yaw/pitch in sync
    editorCam.FocusOn(glm::vec3(0.0f, 0.0f, 0.0f), 5.0f);

    // Initialize last frame time
    m_lastFrameTime = std::chrono::high_resolution_clock::now();

    INXLOG_INFO("Default scene initialized with cube, directional light, and gizmos");
}

void InxRenderer::InitializeRuntimeScene()
{
    // The Player replaces this scene before its first rendered frame. Keep
    // only the state required by scene publication and GPU particle binding;
    // editor gizmos, tools, preview geometry, and their materials are not
    // runtime dependencies.
    m_particleGpuDrawRegistry = std::make_unique<particle::ParticleGpuDrawRegistry>();
    if (m_scenePickingService)
        m_scenePickingService->SetParticleGpuDrawRegistry(m_particleGpuDrawRegistry.get());

    SceneManager::Instance().CreateScene("Runtime Bootstrap");
    m_lastFrameTime = std::chrono::high_resolution_clock::now();
}

void InxRenderer::UpdateSceneLighting()
{
    if (!m_vkCore)
        return;

    Scene *activeScene = SceneManager::Instance().GetActiveScene();
    if (!activeScene)
        return;

    // Get camera position for light sorting
    glm::vec3 cameraPos(m_cameraPos[0], m_cameraPos[1], m_cameraPos[2]);

    // Collect lights from scene into the light collector
    SceneLightCollector &collector = m_vkCore->GetLightCollector();
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    auto t0 = Clock::now();
#endif
    collector.CollectLights(activeScene, cameraPos);
#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.lightingCollectMs = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif

    Camera *editorCam = SceneRenderBridge::Instance().GetEditorCamera();
#if INFERNUX_FRAME_PROFILE
    t0 = Clock::now();
#endif
    m_vkCore->UpdateLightingState();
    const ShaderLightingUBO environmentLighting = collector.GetShaderLightingUBO();
    if (m_sceneRenderGraph && editorCam)
        m_sceneRenderGraph->StageCameraLighting(activeScene, editorCam, cameraPos, environmentLighting);
#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.lightingShadowEditorMs = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif

#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.lightingShadowGameMs = 0.0;
#endif
    if (m_gameCameraEnabled || HasPendingCapture(CaptureSource::Game)) {
#if INFERNUX_FRAME_PROFILE
        t0 = Clock::now();
#endif
        for (Camera *gameCam : FindGameCamerasCached()) {
            glm::vec3 gameCameraPosition = cameraPos;
            if (const Transform *transform = gameCam->GetTransform())
                gameCameraPosition = transform->GetWorldPosition();
            if (SceneRenderGraph *graph = EnsureGameRenderGraph(gameCam))
                graph->StageCameraLighting(activeScene, gameCam, gameCameraPosition, environmentLighting);
        }
#if INFERNUX_FRAME_PROFILE
        m_frameDetailTiming.lightingShadowGameMs = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

#if INFERNUX_FRAME_PROFILE
    t0 = Clock::now();
#endif
#if INFERNUX_FRAME_PROFILE
    m_frameDetailTiming.lightingUploadMs = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
}

uint64_t InxRenderer::GetSceneTextureId() const
{
    if (m_sceneRenderTarget && m_sceneRenderTarget->IsReady()) {
        return m_sceneRenderTarget->GetImGuiTextureId();
    }
    return 0;
}

std::shared_ptr<vk::ImageReadbackTicket> InxRenderer::RequestRenderTargetReadback(bool gameView)
{
    SceneRenderTarget *target = gameView ? m_gameRenderTarget.get() : m_sceneRenderTarget.get();
    if (!m_vkCore || !target || !target->IsReady())
        throw std::logic_error(gameView ? "Game render target is not initialized"
                                        : "Scene render target is not initialized");

    return m_vkCore->GetResourceManager().BeginImageReadback(
        target->GetColorImage(), VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_ASPECT_COLOR_BIT,
        VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, VK_ACCESS_SHADER_READ_BIT, target->GetWidth(), target->GetHeight(),
        target->GetColorFormat());
}

RendererFrameTelemetrySnapshot InxRenderer::GetFrameTelemetrySnapshot()
{
    RendererFrameTelemetrySnapshot snapshot;
    const auto countShadowCasters = [](const std::vector<DrawCall> &drawCalls) {
        return static_cast<size_t>(std::count_if(drawCalls.begin(), drawCalls.end(),
                                                 [](const DrawCall &drawCall) { return drawCall.castsShadows; }));
    };
    snapshot.frame = m_frameCount;
    snapshot.sceneTemporalDiscontinuityRevision = m_observedSceneTemporalDiscontinuityRevision;
    snapshot.temporalHistoryInvalidationCount = m_temporalHistoryInvalidationCount;
    snapshot.sceneViewVisible = m_sceneViewVisible;
    snapshot.gameCameraEnabled = m_gameCameraEnabled;
    const auto &gameCameras = FindGameCamerasCached();
    snapshot.gameCameraAvailable = !gameCameras.empty();
    snapshot.gameCameraCount = gameCameras.size();
    snapshot.gameCameraIds.reserve(gameCameras.size());
    snapshot.gameRenderViewIds.reserve(gameCameras.size());
    for (Camera *camera : gameCameras) {
        snapshot.gameCameraIds.push_back(camera->GetComponentID());
        if (SceneRenderGraph *graph = EnsureGameRenderGraph(camera))
            snapshot.gameRenderViewIds.push_back(graph->GetRenderViewContext().id);
    }
    snapshot.sceneTargetReady = m_sceneRenderTarget && m_sceneRenderTarget->IsReady();
    snapshot.gameTargetReady = m_gameRenderTarget && m_gameRenderTarget->IsReady();
    if (m_sceneRenderTarget) {
        snapshot.sceneTargetWidth = m_sceneRenderTarget->GetWidth();
        snapshot.sceneTargetHeight = m_sceneRenderTarget->GetHeight();
    }
    if (m_gameRenderTarget) {
        snapshot.gameTargetWidth = m_gameRenderTarget->GetWidth();
        snapshot.gameTargetHeight = m_gameRenderTarget->GetHeight();
    }
    if (m_sceneRenderGraph) {
        snapshot.sceneRenderViewId = m_sceneRenderGraph->GetRenderViewContext().id;
        snapshot.sceneTemporalHistoryCount = m_sceneRenderGraph->GetTemporalHistoryCount();
        snapshot.sceneValidTemporalHistoryCount = m_sceneRenderGraph->GetValidTemporalHistoryCount();
        snapshot.sceneTemporalSampleIndex = m_sceneRenderGraph->GetTemporalSampleIndex();
        snapshot.sceneRenderGraphName = m_sceneRenderGraph->GetGraphName();
        snapshot.sceneRenderGraphExecutionCount = m_sceneRenderGraph->GetExecutionCount();
        snapshot.sceneRenderGraphCurrentExecuted = m_sceneRenderGraph->HasExecutedCurrentGraph();
        snapshot.sceneRenderGraphPassNames = m_sceneRenderGraph->GetExecutedPassNames();
        snapshot.sceneRenderGraphDebug = m_sceneRenderGraph->GetDebugString();
        snapshot.sceneShadowViewCount = m_sceneRenderGraph->GetCameraShadowViewCount();
        snapshot.sceneShadowAssignmentCount = m_sceneRenderGraph->GetCameraShadowAssignmentCount();
        snapshot.sceneShadowResourceIdentity = m_sceneRenderGraph->GetShadowResourceIdentity();
        if (m_sceneRenderGraph->HasCachedDrawCalls())
            snapshot.sceneDrawCallCount = m_sceneRenderGraph->GetCachedDrawCalls().size();
        if (m_sceneRenderGraph->HasCachedShadowDrawCalls())
            snapshot.sceneShadowDrawCallCount = countShadowCasters(m_sceneRenderGraph->GetCachedShadowDrawCalls());
    }
    if (m_gameRenderGraph) {
        snapshot.gameRenderViewId = m_gameRenderGraph->GetRenderViewContext().id;
        snapshot.gameTemporalHistoryCount = m_gameRenderGraph->GetTemporalHistoryCount();
        snapshot.gameValidTemporalHistoryCount = m_gameRenderGraph->GetValidTemporalHistoryCount();
        snapshot.gameTemporalSampleIndex = m_gameRenderGraph->GetTemporalSampleIndex();
        snapshot.gameRenderGraphName = m_gameRenderGraph->GetGraphName();
        snapshot.gameRenderGraphExecutionCount = m_gameRenderGraph->GetExecutionCount();
        snapshot.gameRenderGraphCurrentExecuted = m_gameRenderGraph->HasExecutedCurrentGraph();
        snapshot.gameRenderGraphPassNames = m_gameRenderGraph->GetExecutedPassNames();
        snapshot.gameRenderGraphDebug = m_gameRenderGraph->GetDebugString();
        snapshot.gameShadowViewCount = m_gameRenderGraph->GetCameraShadowViewCount();
        snapshot.gameShadowAssignmentCount = m_gameRenderGraph->GetCameraShadowAssignmentCount();
        snapshot.gameShadowResourceIdentity = m_gameRenderGraph->GetShadowResourceIdentity();
        if (m_gameRenderGraph->HasCachedDrawCalls())
            snapshot.gameDrawCallCount = m_gameRenderGraph->GetCachedDrawCalls().size();
        if (m_gameRenderGraph->HasCachedShadowDrawCalls())
            snapshot.gameShadowDrawCallCount = countShadowCasters(m_gameRenderGraph->GetCachedShadowDrawCalls());
    }
    if (gameCameras.size() > 1) {
        snapshot.gameTemporalHistoryCount = 0;
        snapshot.gameValidTemporalHistoryCount = 0;
        snapshot.gameDrawCallCount = 0;
        snapshot.gameShadowDrawCallCount = 0;
        snapshot.gameShadowViewCount = 0;
        snapshot.gameShadowAssignmentCount = 0;
        for (Camera *camera : gameCameras) {
            SceneRenderGraph *graph = EnsureGameRenderGraph(camera);
            if (!graph)
                continue;
            snapshot.gameTemporalHistoryCount += graph->GetTemporalHistoryCount();
            snapshot.gameValidTemporalHistoryCount += graph->GetValidTemporalHistoryCount();
            snapshot.gameShadowViewCount += graph->GetCameraShadowViewCount();
            snapshot.gameShadowAssignmentCount += graph->GetCameraShadowAssignmentCount();
            if (graph->HasCachedDrawCalls())
                snapshot.gameDrawCallCount += graph->GetCachedDrawCalls().size();
            if (graph->HasCachedShadowDrawCalls())
                snapshot.gameShadowDrawCallCount += countShadowCasters(graph->GetCachedShadowDrawCalls());
        }
    }
    if (m_vkCore) {
        snapshot.lightCount = m_vkCore->GetLightCollector().GetTotalLightCount();
        const FrameSubmissionTelemetry &submission = m_vkCore->GetFrameSubmissionTelemetry();
        snapshot.submissionGeneration = submission.generation;
        snapshot.submissionComposed = submission.composed;
        snapshot.submissionComputeQueueIndependent = submission.computeQueueIndependent;
        snapshot.submissionTransferQueueIndependent = submission.transferQueueIndependent;
        snapshot.submissionAsyncComputeActive = submission.asyncComputeActive;
        snapshot.submissionParallelComputeGraphics = submission.parallelComputeGraphics;
        snapshot.submissionBatchCount = submission.batchCount;
        snapshot.submissionGraphicsBatchCount = submission.graphicsBatchCount;
        snapshot.submissionComputeBatchCount = submission.computeBatchCount;
        snapshot.submissionTransferBatchCount = submission.transferBatchCount;
        snapshot.submissionCrossQueueDependencyCount = submission.crossQueueDependencyCount;
        snapshot.submissionUnorderedComputeGraphicsPairCount = submission.unorderedComputeGraphicsPairCount;
        if (const auto *lights = m_vkCore->GetCanonicalLightGpuFrame()) {
            snapshot.canonicalLightGpuBufferReady = lights->buffer.IsValid();
            snapshot.canonicalLightGpuBytes = lights->dataBytes;
            snapshot.canonicalLightGeneration = lights->generation;
            snapshot.canonicalDirectionalLightCount = lights->directionalCount;
            snapshot.canonicalLocalLightCount = lights->localCount;
        }
    }
    if (m_particleGpuSystemManager) {
        const auto gpuParticles = m_particleGpuSystemManager->TelemetrySnapshot();
        snapshot.gpuParticleSystemCount = gpuParticles.systemCount;
        snapshot.gpuParticleOutputCount = gpuParticles.outputCount;
        snapshot.gpuParticleCapacity = gpuParticles.totalCapacity;
        snapshot.gpuParticleLastScheduledFrame = gpuParticles.lastScheduledFrame;
        snapshot.gpuParticleScheduledSystemCount = gpuParticles.scheduledSystemCount;
        snapshot.gpuParticleSimulatingSystemCount = gpuParticles.simulatingSystemCount;
        snapshot.gpuParticleRenderingSystemCount = gpuParticles.renderingSystemCount;
        snapshot.gpuParticleRequestedSpawnCount = gpuParticles.requestedSpawnCount;
        snapshot.gpuParticleContinuationSystemCount = gpuParticles.continuationSystemCount;
        snapshot.gpuParticleContinuationCapacity = gpuParticles.totalContinuationCapacity;
        snapshot.gpuParticleContinuationProgramGeneration = gpuParticles.maximumContinuationProgramGeneration;
        snapshot.gpuParticleContinuationPrepareRecordCalls = gpuParticles.continuationPrepareRecordCalls;
        snapshot.gpuParticleContinuationClassifyRecordCalls = gpuParticles.continuationClassifyRecordCalls;
        snapshot.gpuParticleContinuationDispatchRecordCalls = gpuParticles.continuationDispatchRecordCalls;
        snapshot.gpuParticleContinuationResetPendingCount = gpuParticles.continuationResetPendingCount;
        snapshot.gpuParticleContactRuntimeSystemCount = gpuParticles.contactRuntimeSystemCount;
        snapshot.gpuParticleContactRecordCapacity = gpuParticles.totalContactRecordCapacity;
        snapshot.gpuParticleContactWorkItemCapacity = gpuParticles.totalContactWorkItemCapacity;
        snapshot.gpuParticleContactResidentBytes = gpuParticles.totalContactResidentBytes;
        snapshot.gpuParticleContactPrepareRecordCalls = gpuParticles.contactPrepareRecordCalls;
        snapshot.gpuParticleContactSolveRecordCalls = gpuParticles.contactSolveRecordCalls;
        snapshot.gpuParticleCollisionSceneRevision = gpuParticles.collisionSceneRevision;
        snapshot.gpuParticleCollisionSceneColliderCount = gpuParticles.collisionSceneColliderCount;
        snapshot.gpuParticleCollisionSceneTopologyRevision = gpuParticles.collisionSceneTopologyRevision;
        snapshot.gpuParticleCollisionSceneMeshVertexCount = gpuParticles.collisionSceneMeshVertexCount;
        snapshot.gpuParticleCollisionSceneMeshIndexCount = gpuParticles.collisionSceneMeshIndexCount;
        snapshot.gpuParticleCollisionSceneMeshBvhNodeCount = gpuParticles.collisionSceneMeshBvhNodeCount;
    }
    snapshot.gameRenderMs = m_lastGameRenderMs;
    snapshot.gameOnlyFrameMs = m_gameOnlyFrameMs;
    snapshot.sceneUpdateMs = m_sceneUpdateMs;
    snapshot.guiBuildMs = m_guiBuildMs;
    snapshot.prepareFrameMs = m_prepareFrameMs;
    if (m_gui) {
        const auto scheduler = m_gui->GetFrameSchedulerSnapshot();
        snapshot.guiFrame = m_gui->GetFrameCounter();
        snapshot.guiFrameRequested = scheduler.requested;
        snapshot.guiFrameIntervalMs = scheduler.intervalMs;
        snapshot.guiFrameUntilDueMs = scheduler.untilDueMs;
        snapshot.guiFrameConsumeCount = scheduler.consumeCount;
        snapshot.guiFrameApprovedCount = scheduler.approvedCount;
        snapshot.guiFrameForcedCount = scheduler.forcedCount;
        snapshot.guiFrameRequestCount = scheduler.requestCount;
        snapshot.guiPanelTimesMs = m_gui->GetLastPanelTimesMs();
        snapshot.guiPanelSubTimesMs = m_gui->GetLastPanelSubTimesMs();
    }
    return snapshot;
}

uint64_t InxRenderer::BeginFramePerformanceWindow()
{
    m_framePerformanceWriteIndex = 0;
    m_framePerformanceSampleCount = 0;
    m_framePerformanceDroppedSampleCount = 0;
    m_framePerformanceWindowActive = true;
    return m_frameCount + 1;
}

void InxRenderer::RecordFramePerformanceSample(double frameMs)
{
    auto &sample = m_framePerformanceHistory[m_framePerformanceWriteIndex];
    sample.frame = m_frameCount;
    sample.frameMs = frameMs;
    sample.gameOnlyMs = m_gameOnlyFrameMs;
    sample.renderMs = m_lastGameRenderMs;
    sample.sceneMs = m_sceneUpdateMs;
    sample.guiMs = m_guiBuildMs;
    sample.prepareMs = m_prepareFrameMs;

    m_framePerformanceWriteIndex = (m_framePerformanceWriteIndex + 1) % FRAME_PERFORMANCE_HISTORY_SIZE;
    if (m_framePerformanceSampleCount < FRAME_PERFORMANCE_HISTORY_SIZE) {
        ++m_framePerformanceSampleCount;
        if (m_framePerformanceSampleCount == FRAME_PERFORMANCE_HISTORY_SIZE)
            m_framePerformanceWindowActive = false;
    } else {
        ++m_framePerformanceDroppedSampleCount;
    }
}

RendererFramePerformanceSnapshot InxRenderer::GetFramePerformanceWindow() const
{
    RendererFramePerformanceSnapshot snapshot;
    const size_t count = m_framePerformanceSampleCount;
    snapshot.sampleCount = count;
    snapshot.droppedSampleCount = m_framePerformanceDroppedSampleCount;
    if (count == 0)
        return snapshot;

    const size_t firstSlot =
        (m_framePerformanceWriteIndex + FRAME_PERFORMANCE_HISTORY_SIZE - count) % FRAME_PERFORMANCE_HISTORY_SIZE;
    snapshot.firstFrame = m_framePerformanceHistory[firstSlot].frame;
    snapshot.lastFrame = m_framePerformanceHistory[(m_framePerformanceWriteIndex + FRAME_PERFORMANCE_HISTORY_SIZE - 1) %
                                                   FRAME_PERFORMANCE_HISTORY_SIZE]
                             .frame;

    const auto summarize = [](std::vector<double> values) {
        UIPerformanceMetricStats stats;
        stats.sampleCount = values.size();
        if (values.empty())
            return stats;

        stats.meanMs = std::accumulate(values.begin(), values.end(), 0.0) / static_cast<double>(values.size());
        std::sort(values.begin(), values.end());
        const auto percentile = [&values](double fraction) {
            if (values.size() == 1)
                return values.front();
            const double position = fraction * static_cast<double>(values.size() - 1);
            const size_t lower = static_cast<size_t>(std::floor(position));
            const size_t upper = static_cast<size_t>(std::ceil(position));
            if (lower == upper)
                return values[lower];
            return values[lower] + (values[upper] - values[lower]) * (position - static_cast<double>(lower));
        };
        stats.medianMs = percentile(0.50);
        stats.p95Ms = percentile(0.95);
        stats.p99Ms = percentile(0.99);
        stats.maxMs = values.back();
        return stats;
    };

    std::vector<double> frame;
    std::vector<double> gameOnly;
    std::vector<double> render;
    std::vector<double> scene;
    std::vector<double> gui;
    std::vector<double> prepare;
    frame.reserve(count);
    gameOnly.reserve(count);
    render.reserve(count);
    scene.reserve(count);
    gui.reserve(count);
    prepare.reserve(count);
    for (size_t index = 0; index < count; ++index) {
        const auto &sample = m_framePerformanceHistory[(firstSlot + index) % FRAME_PERFORMANCE_HISTORY_SIZE];
        frame.push_back(sample.frameMs);
        gameOnly.push_back(sample.gameOnlyMs);
        render.push_back(sample.renderMs);
        scene.push_back(sample.sceneMs);
        gui.push_back(sample.guiMs);
        prepare.push_back(sample.prepareMs);
    }
    snapshot.frame = summarize(std::move(frame));
    snapshot.gameOnly = summarize(std::move(gameOnly));
    snapshot.render = summarize(std::move(render));
    snapshot.scene = summarize(std::move(scene));
    snapshot.gui = summarize(std::move(gui));
    snapshot.prepare = summarize(std::move(prepare));
    return snapshot;
}

void InxRenderer::RecordUIPerformanceFrame()
{
    if (!m_gui)
        return;

    const size_t slot = m_uiPerformanceWriteIndex;
    // Store a non-zero stamp so default-initialized history slots never look valid.
    const uint64_t frame = m_frameCount + 1;
    m_uiBuildHistory.values[slot] = m_guiBuildMs;
    m_uiBuildHistory.frames[slot] = frame;

    for (const auto &[name, value] : m_gui->GetLastPanelTimesMs()) {
        auto &history = m_uiPanelHistory[name];
        history.values[slot] = value;
        history.frames[slot] = frame;
    }
    for (const auto &[panelName, stages] : m_gui->GetLastPanelSubTimesMs()) {
        auto &panelHistory = m_uiPanelSubHistory[panelName];
        for (const auto &[stageName, value] : stages) {
            auto &history = panelHistory[stageName];
            history.values[slot] = value;
            history.frames[slot] = frame;
        }
    }

    m_uiPerformanceWriteIndex = (slot + 1) % UI_PERFORMANCE_HISTORY_SIZE;
    m_uiPerformanceSampleCount = (std::min)(m_uiPerformanceSampleCount + 1, UI_PERFORMANCE_HISTORY_SIZE);
}

RendererUIPerformanceSnapshot InxRenderer::GetUIPerformanceSnapshot(size_t maxSamples) const
{
    RendererUIPerformanceSnapshot snapshot;
    const size_t count = (std::min)({maxSamples, m_uiPerformanceSampleCount, UI_PERFORMANCE_HISTORY_SIZE});
    if (count == 0)
        return snapshot;

    const size_t firstSlot =
        (m_uiPerformanceWriteIndex + UI_PERFORMANCE_HISTORY_SIZE - count) % UI_PERFORMANCE_HISTORY_SIZE;
    const size_t lastSlot = (m_uiPerformanceWriteIndex + UI_PERFORMANCE_HISTORY_SIZE - 1) % UI_PERFORMANCE_HISTORY_SIZE;
    snapshot.firstFrame = m_uiBuildHistory.frames[firstSlot] - 1;
    snapshot.lastFrame = m_uiBuildHistory.frames[lastSlot] - 1;
    snapshot.sampleCount = count;

    const auto summarize = [&](const UIMetricHistory &history) {
        std::vector<double> values;
        values.reserve(count);
        for (size_t index = 0; index < count; ++index) {
            const size_t slot = (firstSlot + index) % UI_PERFORMANCE_HISTORY_SIZE;
            if (history.frames[slot] == m_uiBuildHistory.frames[slot])
                values.push_back(history.values[slot]);
        }

        UIPerformanceMetricStats stats;
        stats.sampleCount = values.size();
        if (values.empty())
            return stats;
        stats.meanMs = std::accumulate(values.begin(), values.end(), 0.0) / static_cast<double>(values.size());
        std::sort(values.begin(), values.end());
        const size_t medianIndex = (values.size() - 1) / 2;
        stats.medianMs =
            values.size() % 2 == 0 ? (values[medianIndex] + values[medianIndex + 1]) * 0.5 : values[medianIndex];
        const size_t p95Index = static_cast<size_t>(std::ceil(static_cast<double>(values.size()) * 0.95)) - 1;
        stats.p95Ms = values[(std::min)(p95Index, values.size() - 1)];
        const size_t p99Index = static_cast<size_t>(std::ceil(static_cast<double>(values.size()) * 0.99)) - 1;
        stats.p99Ms = values[(std::min)(p99Index, values.size() - 1)];
        stats.maxMs = values.back();
        return stats;
    };

    snapshot.guiBuild = summarize(m_uiBuildHistory);
    for (const auto &[name, history] : m_uiPanelHistory) {
        auto stats = summarize(history);
        if (stats.sampleCount > 0)
            snapshot.panelTimes.emplace(name, stats);
    }
    for (const auto &[panelName, stages] : m_uiPanelSubHistory) {
        auto &outputStages = snapshot.panelSubTimes[panelName];
        for (const auto &[stageName, history] : stages) {
            auto stats = summarize(history);
            if (stats.sampleCount > 0)
                outputStages.emplace(stageName, stats);
        }
        if (outputStages.empty())
            snapshot.panelSubTimes.erase(panelName);
    }
    return snapshot;
}

uint64_t InxRenderer::RequestCapture(CaptureSource source, const std::string &outputPath)
{
    if (!m_captureService)
        throw std::logic_error("Capture service is not initialized");
    const bool gameView = source == CaptureSource::Game;
    if (gameView && (!m_gameRenderTarget || !m_gameRenderTarget->IsReady())) {
        if (!FindGameCameraCached())
            throw std::logic_error("Game camera is not available");
        constexpr uint32_t kBackgroundCaptureWidth = 1280;
        constexpr uint32_t kBackgroundCaptureHeight = 720;
        ResizeGameRenderTarget(kBackgroundCaptureWidth, kBackgroundCaptureHeight);
    }
    if (gameView)
        (void)FindGameCamerasCached();
    const uint64_t generation = gameView ? m_gameRenderTargetGeneration : m_sceneRenderTargetGeneration;
    SceneRenderTarget *target = gameView ? m_gameRenderTarget.get() : m_sceneRenderTarget.get();
    if (!target || !target->IsReady())
        throw std::logic_error(gameView ? "Game render target is not initialized"
                                        : "Scene render target is not initialized");

    SceneRenderGraph *sourceGraph = gameView ? m_gameRenderGraph : m_sceneRenderGraph.get();
    if (!sourceGraph || !sourceGraph->GetRenderViewContext().IsValid())
        throw std::logic_error("Capture source render view is not initialized");
    rhi::RenderViewContext captureView = sourceGraph->GetRenderViewContext();
    captureView.source = captureView.id;
    captureView.id = rhi::AllocateRenderViewId();
    captureView.kind = rhi::RenderViewKind::Capture;
    captureView.output = rhi::RenderOutputKind::Readback;
    // Capture metadata identifies the source but never owns its live handles.
    captureView.color = {};
    captureView.depth = {};
    captureView.motion = {};
    captureView.history = {};
    const uint64_t captureId = m_captureService->Request(source, captureView, generation, m_frameCount, outputPath);
    m_pendingCaptures.push_back({captureId, source, generation});
    return captureId;
}

bool InxRenderer::HasPendingCapture(CaptureSource source) const
{
    return std::any_of(m_pendingCaptures.begin(), m_pendingCaptures.end(),
                       [source](const PendingCapture &capture) { return capture.source == source; });
}

void InxRenderer::SubmitPendingCaptureReadbacks()
{
    if (!m_captureService || m_pendingCaptures.empty())
        return;

    std::vector<PendingCapture> pending;
    pending.swap(m_pendingCaptures);
    for (const PendingCapture &capture : pending) {
        try {
            const bool gameView = capture.source == CaptureSource::Game;
            const CaptureSnapshot snapshot = m_captureService->Query(capture.id);
            if (snapshot.status != CaptureStatus::PendingGpu)
                continue;

            const uint64_t currentGeneration = gameView ? m_gameRenderTargetGeneration : m_sceneRenderTargetGeneration;
            if (capture.sourceGeneration != currentGeneration) {
                m_captureService->InvalidateSource(capture.source, currentGeneration);
                continue;
            }

            if (gameView)
                (void)FindGameCamerasCached();
            SceneRenderGraph *graph = gameView ? m_gameRenderGraph : m_sceneRenderGraph.get();
            SceneRenderTarget *target = gameView ? m_gameRenderTarget.get() : m_sceneRenderTarget.get();
            if (!graph || !target || !target->IsReady() || !graph->HasExecutedCurrentGraph()) {
                // A target recreated for a dynamic MSAA/resize request is not a
                // valid capture source until its current graph has produced a
                // complete frame. Keep the request pending instead of reading
                // uninitialized or retired Vulkan resources.
                m_pendingCaptures.push_back(capture);
                RequestFullSpeedFrame();
                continue;
            }
            (void)m_captureService->AttachReadback(capture.id, RequestRenderTargetReadback(gameView), m_frameCount);
        } catch (const std::exception &exc) {
            const std::string error = std::string("Capture readback submission failed: ") + exc.what();
            INXLOG_ERROR(error);
            m_captureService->Fail(capture.id, error);
        }
    }
}

CaptureSnapshot InxRenderer::QueryCapture(uint64_t captureId) const
{
    if (!m_captureService)
        throw std::logic_error("Capture service is not initialized");
    return m_captureService->Query(captureId);
}

bool InxRenderer::CancelCapture(uint64_t captureId)
{
    if (!m_captureService || !m_captureService->Cancel(captureId))
        return false;
    m_pendingCaptures.erase(
        std::remove_if(m_pendingCaptures.begin(), m_pendingCaptures.end(),
                       [captureId](const PendingCapture &capture) { return capture.id == captureId; }),
        m_pendingCaptures.end());
    return true;
}

uint64_t InxRenderer::RequestScenePick(float x, float y, float viewportWidth, float viewportHeight)
{
    if (!m_scenePickingService)
        return 0;
    const uint64_t requestId = m_scenePickingService->Request(x, y, viewportWidth, viewportHeight);
    if (requestId != 0)
        RequestFullSpeedFrame();
    return requestId;
}

ScenePickSnapshot InxRenderer::QueryScenePick(uint64_t requestId) const
{
    if (!m_scenePickingService)
        return {requestId, ScenePickStatus::Unknown, 0, "Scene picking service is unavailable"};
    return m_scenePickingService->Query(requestId);
}

void InxRenderer::ResizeSceneRenderTarget(uint32_t width, uint32_t height)
{
    if (m_sceneRenderTarget && width > 0 && height > 0 &&
        (width != m_sceneRenderTarget->GetWidth() || height != m_sceneRenderTarget->GetHeight())) {
        auto replacementTarget = std::make_unique<SceneRenderTarget>(m_vkCore.get());
        replacementTarget->SetMsaaSampleCount(m_sceneRenderTarget->GetMsaaSampleCount());
        if (!replacementTarget->Initialize(width, height)) {
            INXLOG_ERROR("Failed to prepare replacement Scene render target for resize to ", width, "x", height,
                         "; keeping the current generation.");
            return;
        }

        std::unique_ptr<OutlineRenderer> replacementOutline;
        if (m_outlineRenderer) {
            replacementOutline = std::make_unique<OutlineRenderer>();
            if (m_outlineRenderer->IsReady() &&
                !replacementOutline->Initialize(m_vkCore.get(), replacementTarget.get())) {
                const rhi::SubmissionSerial failedCandidateEpoch =
                    m_vkCore->GetBackendContext().Queues().GetLastReservedCompletionEpoch();
                replacementTarget->RetireResourcesAfter(m_vkCore->GetRetirementQueue(), failedCandidateEpoch);
                INXLOG_ERROR("Failed to prepare replacement Outline generation for Scene resize to ", width, "x",
                             height, "; keeping the current generation.");
                return;
            }
        }

        const rhi::SubmissionSerial cutoverEpoch =
            m_vkCore->GetBackendContext().Queues().GetLastReservedCompletionEpoch();
        if (m_sceneRenderGraph)
            m_sceneRenderGraph->RetireFramebuffersBeforeTargetReplacement(cutoverEpoch);

        std::unique_ptr<SceneRenderTarget> retiredTarget = std::move(m_sceneRenderTarget);
        std::unique_ptr<OutlineRenderer> retiredOutline = std::move(m_outlineRenderer);
        m_sceneRenderTarget = std::move(replacementTarget);
        m_outlineRenderer = std::move(replacementOutline);
        if (m_sceneRenderGraph)
            m_sceneRenderGraph->ReplaceSceneTarget(m_sceneRenderTarget.get());

        auto &retirementQueue = m_vkCore->GetRetirementQueue();
        if (retiredOutline) {
            std::shared_ptr<OutlineRenderer> retired(retiredOutline.release());
            retirementQueue.RetireAfter(cutoverEpoch, [retired = std::move(retired)] { retired->Cleanup(false); });
        }
        if (retiredTarget)
            retiredTarget->RetireResourcesAfter(retirementQueue, cutoverEpoch);

        ++m_sceneRenderTargetGeneration;
        if (m_captureService)
            m_captureService->InvalidateSource(CaptureSource::Scene, m_sceneRenderTargetGeneration);
        // Update aspect ratio for projection matrix calculation
        if (m_vkCore) {
            m_vkCore->SetSceneRenderTargetSize(width, height);
        }

        // Update scene camera aspect ratio
        SceneRenderBridge::Instance().OnWindowResize(width, height);
        if (m_gui)
            m_gui->RequestFrame();
    }
}

void InxRenderer::SetShowGrid(bool show)
{
    if (m_editorGizmos) {
        m_editorGizmos->SetShowGrid(show);
        // Grid visibility change takes effect on next frame via SRP Submit()
    }
}

bool InxRenderer::IsShowGrid() const
{
    return m_editorGizmos ? m_editorGizmos->IsShowGrid() : false;
}

EditorGizmos &InxRenderer::GetEditorGizmos()
{
    if (!m_editorGizmos) {
        m_editorGizmos = std::make_unique<EditorGizmos>();
    }
    return *m_editorGizmos;
}

EditorTools *InxRenderer::GetEditorTools()
{
    return m_editorTools.get();
}

GizmosDrawCallBuffer *InxRenderer::GetGizmosDrawCallBuffer()
{
    return m_componentGizmos.get();
}

particle::ParticleGpuDrawRegistry *InxRenderer::GetParticleGpuDrawRegistry()
{
    return m_particleGpuDrawRegistry.get();
}

particle::ParticleGpuSystemManager *InxRenderer::GetParticleGpuSystemManager()
{
    if (m_particleGpuSystemManager)
        return m_particleGpuSystemManager.get();
    if (m_particleGpuSystemManagerInitializationAttempted || !m_vkCore || !m_particleGpuDrawRegistry)
        return nullptr;

    m_particleGpuSystemManagerInitializationAttempted = true;
    auto manager = std::make_unique<particle::ParticleGpuSystemManager>();
    auto particleTextureResolver = [core = m_vkCore.get()](const std::string &textureGuid,
                                                           const std::string &bindingName) {
        particle::GpuBillboardTextureLease lease;
        TextureResolveResult resolved;
        if (textureGuid.empty() || textureGuid == "white" || textureGuid == "black" || textureGuid == "normal") {
            const bool normal = textureGuid == "normal" || bindingName.find("normal") != std::string::npos ||
                                bindingName.find("Normal") != std::string::npos;
            auto residentSlot = core->GetTextureCache().Find(normal ? "_default_normal" : "white");
            auto resident = residentSlot ? residentSlot->Acquire() : nullptr;
            if (!resident || !resident->IsValid()) {
                lease.status = particle::GpuBillboardTextureStatus::Pending;
                return lease;
            }
            resolved.status = TextureResolveStatus::Ready;
            resolved.binding.gpuSlot = std::move(residentSlot);
            resolved.binding.gpuView = std::move(resident);
        } else {
            resolved = core->ResolveTextureForMaterial(textureGuid, bindingName);
        }

        if (resolved.status == TextureResolveStatus::Pending) {
            lease.status = particle::GpuBillboardTextureStatus::Pending;
            return lease;
        }
        if (resolved.status != TextureResolveStatus::Ready || !resolved.binding.gpuView ||
            !resolved.binding.gpuView->IsValid()) {
            lease.status = particle::GpuBillboardTextureStatus::Failed;
            return lease;
        }
        lease.texture = resolved.binding.gpuView->GetView();
        lease.sampler = resolved.binding.gpuView->GetSampler();
        lease.gpuSlot = std::move(resolved.binding.gpuSlot);
        lease.gpuView = std::move(resolved.binding.gpuView);
        lease.status = particle::GpuBillboardTextureStatus::Ready;
        return lease;
    };
    auto particleVectorFieldTextureResolver = [core = m_vkCore.get()](const std::string &textureGuid,
                                                                      bool linearFiltering, bool repeat) {
        particle::GpuBillboardTextureLease lease;
        auto resolved = core->ResolveTextureForVectorField(textureGuid, linearFiltering, repeat);
        if (resolved.status == TextureResolveStatus::Pending) {
            lease.status = particle::GpuBillboardTextureStatus::Pending;
            return lease;
        }
        if (resolved.status != TextureResolveStatus::Ready || !resolved.binding.gpuView ||
            !resolved.binding.gpuView->IsValid()) {
            lease.status = particle::GpuBillboardTextureStatus::Failed;
            return lease;
        }
        lease.texture = resolved.binding.gpuView->GetView();
        lease.sampler = resolved.binding.gpuView->GetSampler();
        lease.gpuSlot = std::move(resolved.binding.gpuSlot);
        lease.gpuView = std::move(resolved.binding.gpuView);
        lease.status = particle::GpuBillboardTextureStatus::Ready;
        return lease;
    };
    CompiledParticleProgramBundle programs;
    if (m_particleProgramBootstrap) {
        JobSystem::Get().WaitPassive(m_particleProgramBootstrap->job);
        programs = std::move(m_particleProgramBootstrap->programs);
        m_particleProgramBootstrap.reset();
    } else {
        programs = CompileParticleProgramBundle();
    }
    if (!programs.IsValid()) {
        INXLOG_ERROR("Failed to compile one or more GPU particle support programs");
        return nullptr;
    }
    const auto particleSkinnedMeshResolver =
        [](const ObjectHandle &handle) -> std::optional<particle::GpuParticleSkinnedMeshSnapshot> {
        Scene *scene = SceneManager::Instance().GetActiveScene();
        auto *renderer = scene ? dynamic_cast<SkinnedMeshRenderer *>(scene->ResolveComponent(handle)) : nullptr;
        if (!renderer || !renderer->GetGameObject() || !renderer->GetGameObject()->GetTransform())
            return std::nullopt;
        const auto pose = renderer->GetRuntimeSkinPoseSnapshot();
        auto mesh = renderer->GetMeshAssetRef().Get();
        auto model = renderer->GetRuntimeModelSnapshot();
        if (!pose || !pose->IsValid() || !mesh || !model)
            return std::nullopt;
        particle::GpuParticleSkinnedMeshSnapshot snapshot;
        snapshot.mesh = std::move(mesh);
        snapshot.model = std::move(model);
        snapshot.currentPalette = pose->current;
        snapshot.previousPalette = pose->previous;
        snapshot.revision = pose->revision;
        const glm::mat4 &world = renderer->GetGameObject()->GetTransform()->GetWorldMatrix();
        for (uint32_t row = 0; row < 4; ++row) {
            for (uint32_t column = 0; column < 4; ++column)
                snapshot.sourceToWorld[row * 4 + column] = world[column][row];
        }
        return snapshot;
    };
    if (!manager->Initialize(
            m_vkCore->GetDeviceContext(), m_vkCore->GetPipelineManager(), m_vkCore->GetResourceManager(),
            m_vkCore->GetRetirementQueue(), *m_particleGpuDrawRegistry, std::move(particleTextureResolver),
            std::move(particleVectorFieldTextureResolver), std::move(particleSkinnedMeshResolver), programs.sort.View(),
            programs.cull.View(), programs.bounds.View(), programs.migration.View(), programs.spawn.View(),
            programs.ribbonTopology.View(), programs.ribbonRender.View(), m_vkCore->GetMaxFramesInFlight())) {
        INXLOG_ERROR("Failed to initialize the GPU particle system manager");
        return nullptr;
    }
    m_particleGpuSystemManager = std::move(manager);
    return m_particleGpuSystemManager.get();
}

void InxRenderer::UpdateParticleCollisionScene()
{
    if (!m_particleGpuSystemManager || !m_particleGpuSystemManager->RequiresCollisionScene())
        return;

    auto &store = PhysicsECSStore::Instance();
    Scene *scene = SceneManager::Instance().GetActiveScene();
    const uint64_t sourceRevision = store.GetCollisionSceneRevision();
    if (scene == m_particleCollisionSourceScene && sourceRevision == m_particleCollisionSourceRevision)
        return;

    const bool sceneChanged = scene != m_particleCollisionSourceScene;
    if (sceneChanged)
        m_particleCollisionPublishedState.clear();

    uint64_t topologyFingerprint = 1469598103934665603ull;
    store.ForEachAliveCollider([&](const ColliderECSData &data) {
        const auto *mesh = dynamic_cast<const MeshCollider *>(data.owner);
        if (!mesh || !mesh->IsEnabled())
            return;
        const GameObject *gameObject = mesh->GetGameObject();
        if (!gameObject || gameObject->GetScene() != scene || !gameObject->IsActiveInHierarchy() ||
            mesh->GetCollisionPositions().empty() || mesh->GetCollisionIndices().empty())
            return;
        ParticleCollisionHashAppend(topologyFingerprint, ParticleCollisionStableHash(mesh->GetInstanceGuid()));
        ParticleCollisionHashAppend(topologyFingerprint, mesh->GetCollisionGeometryRevision());
        ParticleCollisionHashAppend(topologyFingerprint, mesh->GetCollisionPositions().size());
        ParticleCollisionHashAppend(topologyFingerprint, mesh->GetCollisionIndices().size());
    });
    const bool replaceTopology = sceneChanged || topologyFingerprint != m_particleCollisionTopologyFingerprint;
    const uint64_t topologyRevision =
        replaceTopology ? m_particleCollisionTopologyRevision + 1u : m_particleCollisionTopologyRevision;

    particle::GpuParticleCollisionSceneSnapshot snapshot;
    ++m_particleCollisionPublishedRevision;
    if (m_particleCollisionPublishedRevision == 0)
        ++m_particleCollisionPublishedRevision;
    snapshot.revision = m_particleCollisionPublishedRevision;
    snapshot.topologyRevision = topologyRevision;
    snapshot.replaceMeshTopology = replaceTopology;
    if (replaceTopology) {
        store.ForEachAliveCollider([&](const ColliderECSData &data) {
            const auto *mesh = dynamic_cast<const MeshCollider *>(data.owner);
            if (!mesh || !mesh->IsEnabled())
                return;
            const GameObject *gameObject = mesh->GetGameObject();
            if (!gameObject || gameObject->GetScene() != scene || !gameObject->IsActiveInHierarchy())
                return;
            const auto &positions = mesh->GetCollisionPositions();
            const auto &indices = mesh->GetCollisionIndices();
            if (positions.empty() || indices.empty())
                return;
            particle::GpuParticleCollisionMeshGeometry geometry;
            geometry.identity = ParticleCollisionStableHash(mesh->GetInstanceGuid());
            geometry.positions.reserve(positions.size());
            for (const auto &position : positions)
                geometry.positions.push_back({position.x, position.y, position.z, 0.0f});
            geometry.indices = indices;
            snapshot.meshGeometries.push_back(std::move(geometry));
        });
    }
    snapshot.staticColliders.reserve(store.GetAliveColliderCount());
    snapshot.dynamicColliders.reserve(store.GetAliveColliderCount());
    std::unordered_map<uint64_t, ParticleCollisionPublishedState> nextPublishedState;
    nextPublishedState.reserve(store.GetAliveColliderCount());

    store.ForEachAliveCollider([&](const ColliderECSData &data) {
        Collider *collider = data.owner;
        if (!collider || !collider->IsEnabled())
            return;
        GameObject *gameObject = collider->GetGameObject();
        if (!gameObject || gameObject->GetScene() != scene || !gameObject->IsActiveInHierarchy())
            return;
        Transform *transform = gameObject->GetTransform();
        if (!transform)
            return;

        particle::GpuParticleColliderRecord record;
        particle::GpuParticleColliderType type;
        glm::vec3 localBoundsMin(-0.5f);
        glm::vec3 localBoundsMax(0.5f);
        if (const auto *box = dynamic_cast<const BoxCollider *>(collider)) {
            type = particle::GpuParticleColliderType::Box;
            const glm::vec3 size = box->GetSize();
            record.shape = {size.x, size.y, size.z, 0.0f};
            localBoundsMin = -size * 0.5f;
            localBoundsMax = size * 0.5f;
        } else if (const auto *sphere = dynamic_cast<const SphereCollider *>(collider)) {
            type = particle::GpuParticleColliderType::Sphere;
            record.shape = {sphere->GetRadius(), 0.0f, 0.0f, 0.0f};
            localBoundsMin = glm::vec3(-sphere->GetRadius());
            localBoundsMax = glm::vec3(sphere->GetRadius());
        } else if (const auto *capsule = dynamic_cast<const CapsuleCollider *>(collider)) {
            type = particle::GpuParticleColliderType::Capsule;
            record.shape = {capsule->GetRadius(), capsule->GetHeight(), static_cast<float>(capsule->GetDirection()),
                            0.0f};
            const float halfHeight = std::max(capsule->GetHeight() * 0.5f, capsule->GetRadius());
            localBoundsMin = glm::vec3(-capsule->GetRadius());
            localBoundsMax = glm::vec3(capsule->GetRadius());
            const auto axis = static_cast<size_t>(capsule->GetDirection());
            localBoundsMin[axis] = -halfHeight;
            localBoundsMax[axis] = halfHeight;
        } else if (const auto *mesh = dynamic_cast<const MeshCollider *>(collider)) {
            type = particle::GpuParticleColliderType::Mesh;
            const auto &positions = mesh->GetCollisionPositions();
            if (positions.empty() || mesh->GetCollisionIndices().empty())
                return;
            localBoundsMin = positions.front();
            localBoundsMax = positions.front();
            for (const auto &position : positions) {
                localBoundsMin = glm::min(localBoundsMin, position);
                localBoundsMax = glm::max(localBoundsMax, position);
            }
            record.shape = {0.0f, 0.0f, 0.0f, 0.0f};
        } else {
            return;
        }

        const glm::mat4 colliderToWorld =
            transform->GetWorldMatrix() * glm::translate(glm::mat4(1.0f), collider->GetCenter());
        const float determinant = glm::determinant(glm::mat3(colliderToWorld));
        if (!ParticleCollisionFinite(colliderToWorld) || !std::isfinite(determinant) ||
            std::abs(determinant) <= 1.0e-8f)
            return;
        const glm::mat4 worldToCollider = glm::inverse(colliderToWorld);
        if (!ParticleCollisionFinite(worldToCollider))
            return;
        StoreParticleCollisionAffine(record.colliderToWorld, colliderToWorld);
        StoreParticleCollisionAffine(record.worldToCollider, worldToCollider);
        StoreParticleCollisionAabb(record, colliderToWorld, localBoundsMin, localBoundsMax);

        uint32_t flags = collider->IsTrigger() ? particle::GpuParticleColliderTrigger : 0u;
        if (const auto *mesh = dynamic_cast<const MeshCollider *>(collider); mesh && mesh->IsConvex())
            flags |= particle::GpuParticleColliderConvex;
        if (Rigidbody *rigidbody = collider->GetCachedRigidbody(); rigidbody && rigidbody->IsEnabled()) {
            flags |= particle::GpuParticleColliderDynamic;
            const glm::vec3 velocity = rigidbody->GetVelocity();
            record.linearVelocity = {velocity.x, velocity.y, velocity.z, 0.0f};
        }
        record.material = {collider->GetFriction(), collider->GetBounciness(),
                           static_cast<float>(collider->GetFrictionCombine()),
                           static_cast<float>(collider->GetBounceCombine())};
        record.metadata = {static_cast<uint32_t>(type), 1u << static_cast<uint32_t>(gameObject->GetLayer()), flags, 0u};
        const uint64_t componentIdentity = ParticleCollisionStableHash(collider->GetInstanceGuid());
        const uint64_t objectIdentity = gameObject->GetID();
        record.identity = {static_cast<uint32_t>(componentIdentity), static_cast<uint32_t>(componentIdentity >> 32u),
                           static_cast<uint32_t>(objectIdentity), static_cast<uint32_t>(objectIdentity >> 32u)};
        const auto previous = m_particleCollisionPublishedState.find(componentIdentity);
        if (previous != m_particleCollisionPublishedState.end()) {
            CopyParticleCollisionAffine(record.previousWorldToCollider, previous->second.worldToCollider);
            record.previousWorldAabbMin = previous->second.worldAabbMin;
            record.previousWorldAabbMax = previous->second.worldAabbMax;
        } else {
            record.previousWorldToCollider = record.worldToCollider;
            record.previousWorldAabbMin = record.worldAabbMin;
            record.previousWorldAabbMax = record.worldAabbMax;
        }
        auto &nextState = nextPublishedState[componentIdentity];
        CopyParticleCollisionAffine(nextState.worldToCollider, record.worldToCollider);
        nextState.worldAabbMin = record.worldAabbMin;
        nextState.worldAabbMax = record.worldAabbMax;
        if ((flags & particle::GpuParticleColliderDynamic) != 0)
            snapshot.dynamicColliders.push_back(record);
        else
            snapshot.staticColliders.push_back(record);
    });

    std::string error;
    const bool published = m_particleGpuSystemManager->PublishCollisionScene(snapshot, &error);
    if (published) {
        m_particleCollisionSourceScene = scene;
        m_particleCollisionSourceRevision = sourceRevision;
        m_particleCollisionPublishedState = std::move(nextPublishedState);
        if (replaceTopology) {
            m_particleCollisionTopologyFingerprint = topologyFingerprint;
            m_particleCollisionTopologyRevision = topologyRevision;
        }
    } else {
        INXLOG_ERROR("Failed to publish GPU particle collision scene: ", error);
    }
}

uint32_t InxRenderer::GetMaxFramesInFlight() const noexcept
{
    return m_vkCore ? m_vkCore->GetMaxFramesInFlight() : 0;
}

bool InxRenderer::RefreshMaterialPipeline(std::shared_ptr<InxMaterial> material)
{
    INXLOG_DEBUG("RefreshMaterialPipeline called");

    if (!material) {
        INXLOG_ERROR("RefreshMaterialPipeline: material is null");
        return false;
    }

    INXLOG_DEBUG("RefreshMaterialPipeline: material name = ", material->GetName());

    if (!m_vkCore) {
        INXLOG_ERROR("RefreshMaterialPipeline: VkCore is null");
        return false;
    }

    const auto shaderProgram = ResolveShaderProgramArtifact(material);

    // Get shader names from material
    const std::string &vertName = material->GetVertShaderName();
    const std::string &fragName = material->GetFragShaderName();

    INXLOG_DEBUG("RefreshMaterialPipeline: vert='", vertName, "' frag='", fragName, "'");

    if (fragName.empty()) {
        INXLOG_ERROR("RefreshMaterialPipeline: fragment shader name is empty");
        return false;
    }

    bool refreshed = true;
    if (!shaderProgram || shaderProgram->domain != ShaderProgramDomain::ParticleSprite)
        refreshed = m_vkCore->RefreshMaterialPipeline(material, vertName, fragName);

    if (m_particleGpuSystemManager) {
        std::string particleError;
        if (!m_particleGpuSystemManager->RefreshMaterialProgram(material, shaderProgram, &particleError)) {
            INXLOG_ERROR("RefreshMaterialPipeline: ", particleError);
            refreshed = false;
        }
    }
    return refreshed;
}

std::shared_ptr<vk::ImageReadbackTicket>
InxRenderer::BeginMaterialPreviewGPU(const std::shared_ptr<InxMaterial> &material, int size, bool *texturePending)
{
    if (texturePending)
        *texturePending = false;
    if (!m_vkCore || !material)
        return nullptr;
    if (m_shaderProgramArtifactResolver)
        m_shaderProgramArtifactResolver(material);
    return m_vkCore->BeginMaterialPreviewGPU(material, size, texturePending);
}

bool InxRenderer::TryCompleteMaterialPreviewGPU(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                                std::vector<unsigned char> &outPixels)
{
    if (!m_vkCore)
        return false;
    return m_vkCore->TryCompleteMaterialPreviewGPU(ticket, outputSize, outPixels);
}

std::shared_ptr<vk::ImageReadbackTicket>
InxRenderer::BeginMeshPreviewGPU(const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials,
                                 int size)
{
    if (!m_vkCore)
        return nullptr;
    if (m_shaderProgramArtifactResolver) {
        for (const auto &material : materials) {
            if (material)
                m_shaderProgramArtifactResolver(material);
        }
    }
    return m_vkCore->BeginMeshPreviewGPU(mesh, materials, size);
}

bool InxRenderer::TryCompleteMeshPreviewGPU(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                            std::vector<unsigned char> &outPixels)
{
    if (!m_vkCore)
        return false;
    return m_vkCore->TryCompleteMeshPreviewGPU(ticket, outputSize, outPixels);
}

uint64_t InxRenderer::RenderMeshPreviewGPUImGuiCamera(const InxMesh &mesh,
                                                      const std::vector<std::shared_ptr<InxMaterial>> &materials,
                                                      int size, const glm::mat4 &view, const glm::mat4 &proj,
                                                      const glm::vec3 &cameraPos, bool cloneMaterials)
{
    if (!m_vkCore)
        return 0;
    return m_vkCore->RenderMeshPreviewGPUImGuiCamera(mesh, materials, size, view, proj, cameraPos, cloneMaterials);
}

uint64_t InxRenderer::GetMeshPreviewDisplayTextureId() const
{
    return m_vkCore ? m_vkCore->GetMeshPreviewDisplayTextureId() : 0;
}

void InxRenderer::InvalidateShaderCache(const std::string &shaderId)
{
    INXLOG_DEBUG("InvalidateShaderCache called: ", shaderId);

    if (!m_vkCore) {
        INXLOG_ERROR("InvalidateShaderCache: VkCore is null");
        return;
    }

    // Fullscreen RenderEffect pipelines are not part of the material cache.
    // Retire their old shader revision before publishing the replacement raw
    // module so the next graph record cannot silently keep the old effect.
    if (m_sceneRenderGraph)
        m_sceneRenderGraph->InvalidateFullscreenShader(shaderId);
    for (auto &[cameraId, graph] : m_gameRenderGraphs) {
        (void)cameraId;
        graph->InvalidateFullscreenShader(shaderId);
    }

    m_vkCore->InvalidateShaderCache(shaderId);
}

void InxRenderer::InvalidateTextureCache(const std::string &texturePath)
{
    INXLOG_DEBUG("InvalidateTextureCache called: ", texturePath);

    if (!m_vkCore) {
        INXLOG_ERROR("InvalidateTextureCache: VkCore is null");
        return;
    }

    m_vkCore->InvalidateTextureCache(texturePath);
}

void InxRenderer::RemoveMaterialPipeline(const std::string &materialName)
{
    if (!m_vkCore) {
        INXLOG_ERROR("RemoveMaterialPipeline: VkCore is null");
        return;
    }
    m_vkCore->RemoveMaterialPipeline(materialName);
}

bool InxRenderer::RefreshMaterialsUsingShader(const std::string &shaderId)
{
    INXLOG_DEBUG("RefreshMaterialsUsingShader called: ", shaderId);

    if (!m_vkCore) {
        INXLOG_ERROR("RefreshMaterialsUsingShader: VkCore is null");
        return false;
    }

    // Get all materials from AssetRegistry
    auto &registry = AssetRegistry::Instance();
    auto materials = registry.GetAllMaterials();

    bool anyRefreshed = false;
    for (auto &material : materials) {
        if (!material)
            continue;

        // Check if this material uses the shader
        const std::string &vertName = material->GetVertShaderName();
        const std::string &fragName = material->GetFragShaderName();

        INXLOG_DEBUG("RefreshMaterialsUsingShader: checking material '", material->GetName(), "' with vert='", vertName,
                     "' frag='", fragName, "' against shaderId='", shaderId, "'");

        // Check for match: either vert or frag uses this shader
        auto matchesId = [&](const std::string &name) {
            if (name == shaderId)
                return true;
            if (name.empty())
                return false;
            return PortablePathStem(name) == shaderId;
        };
        bool matches = matchesId(vertName) || matchesId(fragName);

        if (matches) {
            INXLOG_DEBUG("RefreshMaterialsUsingShader: refreshing material '", material->GetName(), "'");
            if (RefreshMaterialPipeline(material)) {
                anyRefreshed = true;
            }
        }
    }

    INXLOG_INFO("RefreshMaterialsUsingShader: refreshed materials using shader '", shaderId, "'");
    return anyRefreshed;
}

SceneRenderGraph *InxRenderer::GetSceneRenderGraph()
{
    return m_sceneRenderGraph.get();
}

uint64_t InxRenderer::RequestGpuParticleViewDiagnostics(bool gameView, uint64_t graphInstanceId,
                                                        uint64_t cameraComponentId)
{
    SceneRenderGraph *graph = m_sceneRenderGraph.get();
    if (gameView) {
        (void)FindGameCamerasCached();
        if (cameraComponentId == 0) {
            graph = m_gameRenderGraph;
        } else if (const auto found = m_gameRenderGraphs.find(cameraComponentId); found != m_gameRenderGraphs.end()) {
            graph = found->second.get();
        } else {
            graph = nullptr;
        }
    } else if (cameraComponentId != 0) {
        return 0;
    }
    return graph ? graph->RequestParticleViewDiagnostics(graphInstanceId) : 0;
}

particle::GpuParticleViewDiagnosticSnapshot
InxRenderer::QueryGpuParticleViewDiagnostics(bool gameView, uint64_t requestId, uint64_t cameraComponentId) const
{
    const SceneRenderGraph *graph = m_sceneRenderGraph.get();
    uint64_t resolvedCameraComponentId = 0;
    if (gameView) {
        if (cameraComponentId == 0) {
            graph = m_gameRenderGraph;
            resolvedCameraComponentId = m_cachedGameCamera ? m_cachedGameCamera->GetComponentID() : 0;
        } else if (const auto found = m_gameRenderGraphs.find(cameraComponentId); found != m_gameRenderGraphs.end()) {
            graph = found->second.get();
            resolvedCameraComponentId = cameraComponentId;
        } else {
            graph = nullptr;
        }
    } else if (cameraComponentId != 0) {
        graph = nullptr;
    }
    if (!graph)
        return {};
    auto snapshot = graph->QueryParticleViewDiagnostics(requestId);
    snapshot.cameraComponentId = resolvedCameraComponentId;
    snapshot.renderViewId = graph->GetRenderViewContext().id;
    return snapshot;
}

std::shared_ptr<InxMaterial> InxRenderer::GetFirstMeshRendererMaterial()
{
    Scene *activeScene = SceneManager::Instance().GetActiveScene();
    if (!activeScene) {
        return nullptr;
    }

    std::vector<GameObject *> allObjects = activeScene->GetAllObjects();
    for (GameObject *obj : allObjects) {
        if (!obj->IsActiveInHierarchy()) {
            continue;
        }

        MeshRenderer *renderer = obj->GetComponent<MeshRenderer>();
        if (!renderer || !renderer->IsEnabled()) {
            continue;
        }

        // Return the first valid material found
        auto material = renderer->GetEffectiveMaterial();
        if (material) {
            return material;
        }
    }

    return nullptr;
}

void InxRenderer::SetRenderPipeline(std::shared_ptr<RenderPipelineCallback> pipeline)
{
    // Dispose the old pipeline if replacing
    if (m_renderPipeline) {
        m_renderPipeline->Dispose();
    }
    m_renderPipeline = std::move(pipeline);

    if (m_renderPipeline) {
        INXLOG_INFO("Render pipeline set (Python SRP path active)");
    } else {
        INXLOG_INFO("Render pipeline cleared (C++ default path active)");
    }
}

// ============================================================================
// Game Camera Render Target
// ============================================================================

Camera *InxRenderer::FindGameCamera()
{
    Scene *activeScene = SceneManager::Instance().GetActiveScene();
    if (!activeScene)
        return nullptr;

    Camera *editorCam = SceneManager::Instance().GetEditorCameraController().GetCamera();
    return activeScene->FindGameCamera(editorCam);
}

std::vector<Camera *> InxRenderer::FindGameCameras()
{
    Scene *activeScene = SceneManager::Instance().GetActiveScene();
    if (!activeScene)
        return {};
    Camera *editorCam = SceneManager::Instance().GetEditorCameraController().GetCamera();
    return activeScene->GetActiveGameCameras(editorCam);
}

SceneRenderGraph *InxRenderer::EnsureGameRenderGraph(Camera *camera)
{
    if (!camera || !m_gameRenderTarget || !m_gameRenderTarget->IsReady() || !m_vkCore)
        return nullptr;

    const uint64_t cameraId = camera->GetComponentID();
    auto found = m_gameRenderGraphs.find(cameraId);
    if (found != m_gameRenderGraphs.end())
        return found->second.get();

    auto graph = std::make_unique<SceneRenderGraph>();
    if (!graph->Initialize(m_vkCore.get(), m_gameRenderTarget.get(), rhi::RenderViewKind::Game)) {
        INXLOG_ERROR("Failed to initialize Game RenderGraph for Camera component ", cameraId);
        return nullptr;
    }
    graph->SetParticleGpuDrawRegistry(m_particleGpuDrawRegistry.get());
    graph->SetEffectiveMsaaSamples(GetMsaaSamples());
    SceneRenderGraph *result = graph.get();
    m_gameRenderGraphs.emplace(cameraId, std::move(graph));
    return result;
}

const std::vector<Camera *> &InxRenderer::FindGameCamerasCached()
{
    if (m_gameCameraCacheValid)
        return m_cachedGameCameras;

    m_cachedGameCameras = FindGameCameras();
    m_cachedGameCamera = FindGameCamera();
    m_gameCameraCacheValid = true;

    for (auto it = m_gameRenderGraphs.begin(); it != m_gameRenderGraphs.end();) {
        const bool active = std::any_of(
            m_cachedGameCameras.begin(), m_cachedGameCameras.end(),
            [cameraId = it->first](const Camera *camera) { return camera && camera->GetComponentID() == cameraId; });
        if (active) {
            ++it;
            continue;
        }
        if (m_gameRenderGraph == it->second.get())
            m_gameRenderGraph = nullptr;
        it = m_gameRenderGraphs.erase(it);
    }

    // Screen-space UI is a property of the final Game output, not of every
    // camera layer. Keep it attached only to the highest-depth active camera.
    for (auto &[cameraId, graph] : m_gameRenderGraphs) {
        (void)cameraId;
        graph->SetScreenUIRenderer(nullptr);
    }
    for (Camera *camera : m_cachedGameCameras)
        (void)EnsureGameRenderGraph(camera);
    if (!m_cachedGameCameras.empty()) {
        if (SceneRenderGraph *last = EnsureGameRenderGraph(m_cachedGameCameras.back()))
            last->SetScreenUIRenderer(m_screenUIRenderer.get());
    }

    m_gameRenderGraph = m_cachedGameCamera ? EnsureGameRenderGraph(m_cachedGameCamera) : nullptr;
    if (!m_gameRenderGraph && !m_cachedGameCameras.empty())
        m_gameRenderGraph = EnsureGameRenderGraph(m_cachedGameCameras.front());

    return m_cachedGameCameras;
}

Camera *InxRenderer::FindGameCameraCached()
{
    (void)FindGameCamerasCached();
    return m_cachedGameCamera;
}

uint64_t InxRenderer::GetGameTextureId()
{
    if (m_gameRenderTarget && m_gameRenderTarget->IsReady()) {
        // Resolving here makes the GUI's no-camera state change in the same
        // frame instead of briefly presenting the previous camera image.
        if (FindGameCameraCached())
            return m_gameRenderTarget->GetImGuiTextureId();
        return 0;
    }
    return 0;
}

void InxRenderer::ResizeGameRenderTarget(uint32_t width, uint32_t height)
{
    if (width == 0 || height == 0) {
        return;
    }

    // Lazy-initialize game render target on first resize request
    if (!m_gameRenderTarget) {
        // INXLOG_INFO("Game render target: lazy-initializing (", width, "x", height, ")");
        m_gameRenderTarget = std::make_unique<SceneRenderTarget>(m_vkCore.get());
        // Match the current MSAA setting from the scene render target.
        // Without this, the game render target defaults to 4x MSAA even
        // when the engine has already switched to 1x, causing a layout
        // mismatch (COLOR_ATTACHMENT_OPTIMAL vs SHADER_READ_ONLY_OPTIMAL)
        // that triggers VK_ERROR_DEVICE_LOST.
        if (m_sceneRenderTarget) {
            m_gameRenderTarget->SetMsaaSampleCount(m_sceneRenderTarget->GetMsaaSampleCount());
        }
        m_gameRenderTarget->Initialize(width, height);
        ++m_gameRenderTargetGeneration;

        // Create the screen UI renderer for GPU-based 2D UI in the game render graph
        if (!m_screenUIRenderer) {
            m_screenUIRenderer = std::make_unique<InxScreenUIRenderer>();
            const bool dynamicCommandsAvailable = rhi::ResolveDynamicRenderingCommands(m_vkCore->GetDevice()).IsValid();
            const bool useDynamicRendering = rhi::SelectDynamicRenderingPath(
                m_vkCore->GetDeviceContext().GetRhiDevice().GetCapabilityState().dynamicRendering.enabled,
                dynamicCommandsAvailable, false);
            m_screenUIRenderer->Initialize(m_vkCore->GetDevice(), m_vkCore->GetDeviceContext().GetVmaAllocator(),
                                           m_gameRenderTarget->GetColorFormat(),
                                           m_gameRenderTarget->GetMsaaSampleCount(), useDynamicRendering);
            m_screenUIRenderer->SetRetirementQueue(&m_vkCore->GetRetirementQueue());
            m_screenUIRenderer->SetTextureUsageValidator(
                [this](uint64_t textureId) { return m_gui && m_gui->TouchImGuiTextureId(textureId); });
        }
        // Pre-discover the ordered camera stack and create one graph per
        // camera so the first Game frame cannot reuse Scene-view state.
        m_gameCameraCacheValid = false;
        (void)FindGameCamerasCached();

        return;
    }

    if (m_gameRenderTarget && (width != m_gameRenderTarget->GetWidth() || height != m_gameRenderTarget->GetHeight())) {
        auto replacementTarget = std::make_unique<SceneRenderTarget>(m_vkCore.get());
        replacementTarget->SetMsaaSampleCount(m_gameRenderTarget->GetMsaaSampleCount());
        if (!replacementTarget->Initialize(width, height)) {
            INXLOG_ERROR("Failed to prepare replacement Game render target for resize to ", width, "x", height,
                         "; keeping the current generation.");
            return;
        }

        const rhi::SubmissionSerial cutoverEpoch =
            m_vkCore->GetBackendContext().Queues().GetLastReservedCompletionEpoch();
        for (auto &[cameraId, graph] : m_gameRenderGraphs) {
            (void)cameraId;
            graph->RetireFramebuffersBeforeTargetReplacement(cutoverEpoch);
        }

        std::unique_ptr<SceneRenderTarget> retiredTarget = std::move(m_gameRenderTarget);
        m_gameRenderTarget = std::move(replacementTarget);
        for (auto &[cameraId, graph] : m_gameRenderGraphs) {
            (void)cameraId;
            graph->ReplaceSceneTarget(m_gameRenderTarget.get());
        }
        auto &retirementQueue = m_vkCore->GetRetirementQueue();
        if (retiredTarget)
            retiredTarget->RetireResourcesAfter(retirementQueue, cutoverEpoch);

        ++m_gameRenderTargetGeneration;
        if (m_captureService)
            m_captureService->InvalidateSource(CaptureSource::Game, m_gameRenderTargetGeneration);
        if (m_gui)
            m_gui->RequestFrame();
    }
}

void InxRenderer::InvalidateTemporalHistory(bool sceneView, bool gameView)
{
    bool invalidated = false;
    if (sceneView && m_sceneRenderGraph) {
        m_sceneRenderGraph->InvalidateTemporalHistory();
        invalidated = true;
    }
    if (gameView) {
        for (auto &[cameraId, graph] : m_gameRenderGraphs) {
            (void)cameraId;
            graph->InvalidateTemporalHistory();
            invalidated = true;
        }
    }
    if (invalidated)
        ++m_temporalHistoryInvalidationCount;
}

void InxRenderer::ConsumeSceneTemporalDiscontinuity()
{
    Scene *scene = SceneManager::Instance().GetActiveScene();
    const uint64_t worldId = scene ? scene->GetWorldId() : 0;
    if (worldId != m_temporalHistoryWorldId) {
        m_temporalHistoryWorldId = worldId;
        m_observedSceneTemporalDiscontinuityRevision = scene ? scene->GetTemporalDiscontinuityRevision() : 0;
        InvalidateGpuViewStateForSceneBoundary();
        m_gameRenderGraph = nullptr;
        m_gameRenderGraphs.clear();
        m_gameCameraCacheValid = false;
        m_cachedGameCamera = nullptr;
        m_cachedGameCameras.clear();
        return;
    }
    if (!scene)
        return;

    const uint64_t revision = scene->GetTemporalDiscontinuityRevision();
    if (revision == m_observedSceneTemporalDiscontinuityRevision)
        return;
    m_observedSceneTemporalDiscontinuityRevision = revision;
    // Play/Stop keeps the same world id. Treat that edge like opening Scene:
    // drop compiled Scene/Game graphs and rebuild from the current generation.
    InvalidateGpuViewStateForSceneBoundary();
    InvalidateTemporalHistory();
}

void InxRenderer::InvalidateGpuViewStateForSceneBoundary()
{
    if (m_particleGpuSystemManager) {
        m_particleGpuSystemManager->AbortAsyncRecording();
        m_particleGpuSystemManager->ResetAll();
    }
    if (m_sceneRenderGraph)
        m_sceneRenderGraph->InvalidateParticleViews();
    for (auto &[cameraId, graph] : m_gameRenderGraphs) {
        (void)cameraId;
        graph->InvalidateParticleViews();
    }
    WaitForGpuIdle();
    // Game cameras keep the same IDs across Play/Stop. Destroy the compiled
    // graphs only after the device is idle: SceneRenderGraph::Destroy releases
    // RHI objects immediately. The next Game frame must import the current
    // particle generation instead of recycled handles from two Play cycles ago.
    m_gameRenderGraph = nullptr;
    m_gameRenderGraphs.clear();
    RecreateSceneRenderGraph();
    if (m_vkCore)
        m_vkCore->FlushRetiredGpuResources();
}

void InxRenderer::RecreateSceneRenderGraph()
{
    if (!m_sceneRenderGraph)
        return;
    const int samples = GetMsaaSamples();
    m_sceneRenderGraph.reset();
    if (!m_vkCore || !m_sceneRenderTarget)
        return;
    m_sceneRenderGraph = std::make_unique<SceneRenderGraph>();
    if (!m_sceneRenderGraph->Initialize(m_vkCore.get(), m_sceneRenderTarget.get(), rhi::RenderViewKind::Scene)) {
        INXLOG_ERROR("Failed to reinitialize the Scene View render graph after a scene boundary");
        m_sceneRenderGraph.reset();
        return;
    }
    if (m_particleGpuDrawRegistry)
        m_sceneRenderGraph->SetParticleGpuDrawRegistry(m_particleGpuDrawRegistry.get());
    if (samples > 0)
        m_sceneRenderGraph->SetEffectiveMsaaSamples(samples);
    if (m_sceneViewVisible && m_outlineRenderer)
        m_sceneRenderGraph->SetOutlineRenderer(m_outlineRenderer.get());
}

void InxRenderer::SetSceneViewVisible(bool visible)
{
    if (m_sceneViewVisible == visible)
        return;
    m_sceneViewVisible = visible;

    // Hiding a panel only invalidates its camera-dependent submissions.  The
    // compiled graph description remains authoritative for shared pipeline
    // settings such as the shadow atlas resolution.
    if (!visible && m_sceneRenderGraph) {
        m_sceneRenderGraph->ClearCachedViewSubmission();
    }
}

void InxRenderer::SetGameCameraEnabled(bool enabled)
{
    if (m_gameCameraEnabled == enabled)
        return;
    m_gameCameraEnabled = enabled;
    if (enabled) {
        // Match Scene-view show: drop any leftover Game cache and rebuild
        // particle views on the next submit instead of reusing retired buffers.
        for (auto &[cameraId, graph] : m_gameRenderGraphs) {
            (void)cameraId;
            graph->ClearCachedViewSubmission();
            graph->InvalidateParticleViews();
            graph->MarkDirty();
        }
        INXLOG_DEBUG("Game camera rendering enabled");
        return;
    }

    for (auto &[cameraId, graph] : m_gameRenderGraphs) {
        (void)cameraId;
        graph->ClearCachedViewSubmission();
    }
    INXLOG_DEBUG("Game camera rendering disabled");
}

InxScreenUIRenderer *InxRenderer::GetScreenUIRenderer()
{
    return m_screenUIRenderer.get();
}

// ============================================================================
// MSAA Configuration
// ============================================================================

uint32_t InxRenderer::GetSupportedMsaaSampleMask() const
{
    if (!m_vkCore)
        return 0;
    const VkFormat colorFormat =
        m_sceneRenderTarget ? m_sceneRenderTarget->GetColorFormat() : VK_FORMAT_R16G16B16A16_SFLOAT;
    const VkFormat depthFormat =
        m_sceneRenderTarget ? m_sceneRenderTarget->GetDepthFormat() : m_vkCore->GetDeviceContext().FindDepthFormat();
    const auto &deviceContext = m_vkCore->GetDeviceContext();
    const auto colorSamples = deviceContext.GetImageSampleCountMask(colorFormat, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT |
                                                                                     VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
    const auto resolveSamples = deviceContext.GetImageSampleCountMask(
        colorFormat, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                         VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
    const auto depthSamples =
        deviceContext.GetImageSampleCountMask(depthFormat, VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT);
    return GetSceneTargetSampleCountMask(colorSamples, resolveSamples, depthSamples);
}

void InxRenderer::SetMsaaSamples(int samples)
{
    if (!ApplyMsaaSamples(samples, "engine API"))
        SetEffectiveGraphMsaaSamples(GetMsaaSamples());
}

bool InxRenderer::ApplyMsaaSamples(int samples, const char *source)
{
    if (!IsValidMsaaSampleCount(samples)) {
        ++m_msaaRejectedRequestCount;
        INXLOG_ERROR("Rejected invalid ", source, " MSAA request ", samples, "x; valid values are 1, 2, 4, and 8.");
        return false;
    }

    const uint32_t supportedMask = GetSupportedMsaaSampleMask();
    if (!SupportsMsaaSampleCount(static_cast<rhi::SampleCountMask>(supportedMask), samples)) {
        ++m_msaaRejectedRequestCount;
        INXLOG_ERROR("Rejected unsupported ", source, " MSAA request ", samples,
                     "x for the HDR color/depth target pair (supported mask=", supportedMask,
                     "). The active setting remains unchanged.");
        return false;
    }

    const VkSampleCountFlagBits vkSamples = rhi::ToVkSampleCount(ToRhiSampleCount(samples));
    const bool sceneAligned = !m_sceneRenderTarget || m_sceneRenderTarget->GetMsaaSampleCount() == vkSamples;
    const bool gameAligned = !m_gameRenderTarget || m_gameRenderTarget->GetMsaaSampleCount() == vkSamples;
    const bool materialsAligned = !m_vkCore || m_vkCore->GetMaterialPipelineManager().GetSampleCount() == vkSamples;

    if (sceneAligned && gameAligned && materialsAligned)
        return false;

    std::unique_ptr<SceneRenderTarget> replacementSceneTarget;
    std::unique_ptr<SceneRenderTarget> replacementGameTarget;
    std::unique_ptr<InxScreenUIRenderer> replacementScreenUI;
    std::unique_ptr<OutlineRenderer> replacementOutline;

    auto createReplacement = [this, vkSamples](const SceneRenderTarget &source) {
        auto replacement = std::make_unique<SceneRenderTarget>(m_vkCore.get());
        replacement->SetMsaaSampleCount(vkSamples);
        if (!replacement->Initialize(source.GetWidth(), source.GetHeight()))
            replacement.reset();
        return replacement;
    };

    if (!sceneAligned) {
        replacementSceneTarget = createReplacement(*m_sceneRenderTarget);
        if (!replacementSceneTarget) {
            INXLOG_ERROR("Failed to create the replacement Scene target for ", samples,
                         "x MSAA; keeping all current resources.");
            return false;
        }
    }

    if (!gameAligned) {
        replacementGameTarget = createReplacement(*m_gameRenderTarget);
        if (!replacementGameTarget) {
            INXLOG_ERROR("Failed to create the replacement Game target for ", samples,
                         "x MSAA; keeping all current resources.");
            return false;
        }
    }

    if (m_gameRenderTarget && (!gameAligned || !materialsAligned)) {
        replacementScreenUI = std::make_unique<InxScreenUIRenderer>();
        const bool dynamicCommandsAvailable = rhi::ResolveDynamicRenderingCommands(m_vkCore->GetDevice()).IsValid();
        const bool useDynamicRendering = rhi::SelectDynamicRenderingPath(
            m_vkCore->GetDeviceContext().GetRhiDevice().GetCapabilityState().dynamicRendering.enabled,
            dynamicCommandsAvailable, false);
        if (!replacementScreenUI->Initialize(m_vkCore->GetDevice(), m_vkCore->GetDeviceContext().GetVmaAllocator(),
                                             m_gameRenderTarget->GetColorFormat(), vkSamples, useDynamicRendering)) {
            INXLOG_ERROR("Failed to create the replacement Screen UI pipeline for ", samples,
                         "x MSAA; keeping all current resources.");
            return false;
        }
        replacementScreenUI->SetRetirementQueue(&m_vkCore->GetRetirementQueue());
        replacementScreenUI->SetTextureUsageValidator(
            [this](uint64_t textureId) { return m_gui && m_gui->TouchImGuiTextureId(textureId); });
    }

    if (replacementSceneTarget && m_outlineRenderer) {
        replacementOutline = std::make_unique<OutlineRenderer>();
        if (m_outlineRenderer->IsReady() &&
            !replacementOutline->Initialize(m_vkCore.get(), replacementSceneTarget.get())) {
            INXLOG_ERROR("Failed to prepare the replacement Outline generation for ", samples,
                         "x MSAA; keeping all current resources.");
            return false;
        }
    }

    // No command buffer is submitted after this snapshot and before the
    // generation commit. Every old object therefore shares one cross-queue
    // retirement boundary, including asynchronous particle compute/export.
    const rhi::SubmissionSerial cutoverEpoch = m_vkCore->GetBackendContext().Queues().GetLastReservedCompletionEpoch();

    // Material pipelines are the only fallible part of the publication step.
    // Prepare and commit their complete generation before replacing any live
    // render target so a pipeline build failure leaves the old frame coherent.
    if (m_vkCore && !m_vkCore->CommitMaterialPipelineGeneration(vkSamples)) {
        ++m_msaaRejectedRequestCount;
        INXLOG_ERROR("Failed to prepare the material pipeline generation for ", samples,
                     "x MSAA; keeping the previous setting.");
        return false;
    }

    std::unique_ptr<SceneRenderTarget> retiredSceneTarget;
    std::unique_ptr<SceneRenderTarget> retiredGameTarget;
    std::unique_ptr<InxScreenUIRenderer> retiredScreenUI;
    std::unique_ptr<OutlineRenderer> retiredOutline;

    // Framebuffers own references to target image views. Detach them before
    // publishing the replacement target and before queuing target retirement.
    if (replacementSceneTarget && m_sceneRenderGraph)
        m_sceneRenderGraph->RetireFramebuffersBeforeTargetReplacement(cutoverEpoch);
    if (replacementGameTarget) {
        for (auto &[cameraId, graph] : m_gameRenderGraphs) {
            (void)cameraId;
            graph->RetireFramebuffersBeforeTargetReplacement(cutoverEpoch);
        }
    }

    if (replacementSceneTarget) {
        retiredSceneTarget = std::move(m_sceneRenderTarget);
        m_sceneRenderTarget = std::move(replacementSceneTarget);
        if (m_sceneRenderGraph)
            m_sceneRenderGraph->ReplaceSceneTarget(m_sceneRenderTarget.get());
        ++m_sceneRenderTargetGeneration;
        if (m_captureService)
            m_captureService->InvalidateSource(CaptureSource::Scene, m_sceneRenderTargetGeneration);

        retiredOutline = std::move(m_outlineRenderer);
        m_outlineRenderer = std::move(replacementOutline);
    }
    if (replacementGameTarget) {
        retiredGameTarget = std::move(m_gameRenderTarget);
        m_gameRenderTarget = std::move(replacementGameTarget);
        for (auto &[cameraId, graph] : m_gameRenderGraphs) {
            (void)cameraId;
            graph->ReplaceSceneTarget(m_gameRenderTarget.get());
        }
        ++m_gameRenderTargetGeneration;
        if (m_captureService)
            m_captureService->InvalidateSource(CaptureSource::Game, m_gameRenderTargetGeneration);
    }
    if (replacementScreenUI) {
        retiredScreenUI = std::move(m_screenUIRenderer);
        m_screenUIRenderer = std::move(replacementScreenUI);
        m_gameCameraCacheValid = false;
        (void)FindGameCamerasCached();
    }

    auto &retirementQueue = m_vkCore->GetRetirementQueue();
    if (retiredOutline) {
        std::shared_ptr<OutlineRenderer> retired(retiredOutline.release());
        retirementQueue.RetireAfter(cutoverEpoch, [retired = std::move(retired)] { retired->Cleanup(false); });
    }
    if (retiredScreenUI) {
        std::shared_ptr<InxScreenUIRenderer> retired(retiredScreenUI.release());
        retirementQueue.RetireAfter(cutoverEpoch, [retired = std::move(retired)] {});
    }
    if (retiredSceneTarget)
        retiredSceneTarget->RetireResourcesAfter(retirementQueue, cutoverEpoch);
    if (retiredGameTarget)
        retiredGameTarget->RetireResourcesAfter(retirementQueue, cutoverEpoch);

    // BuildFrame() ran before the MSAA request was observed. Replacing render
    // targets and preview renderers invalidates texture descriptor sets already
    // baked into that ImDrawData. Make the next editor frame rebuild the GUI
    // before it can submit those stale handles.
    if (m_gui)
        m_gui->RequestFrame();

    SetEffectiveGraphMsaaSamples(samples);
    ++m_msaaReconfigurationCount;
    INXLOG_INFO("Applied ", samples, "x MSAA from ", source,
                " to Scene, Game, material, preview, and Screen UI paths.");
    return true;
}

int InxRenderer::GetMsaaSamples() const
{
    if (m_sceneRenderTarget)
        return static_cast<int>(m_sceneRenderTarget->GetMsaaSampleCount());
    if (m_gameRenderTarget)
        return static_cast<int>(m_gameRenderTarget->GetMsaaSampleCount());
    if (m_vkCore)
        return static_cast<int>(m_vkCore->GetMaterialPipelineManager().GetSampleCount());
    return 4; // default
}

void InxRenderer::SetEffectiveGraphMsaaSamples(int samples)
{
    if (m_sceneRenderGraph)
        m_sceneRenderGraph->SetEffectiveMsaaSamples(samples);
    for (auto &[cameraId, graph] : m_gameRenderGraphs) {
        (void)cameraId;
        graph->SetEffectiveMsaaSamples(samples);
    }
}

MsaaStateSnapshot InxRenderer::GetMsaaStateSnapshot() const
{
    MsaaStateSnapshot snapshot;
    snapshot.activeSamples = GetMsaaSamples();
    snapshot.sceneRequestedSamples = m_sceneRequestedMsaaSamples;
    snapshot.gameRequestedSamples = m_gameRequestedMsaaSamples;
    snapshot.supportedSampleMask = GetSupportedMsaaSampleMask();
    snapshot.requestConflict = m_msaaRequestConflict;
    snapshot.sceneTargetAligned =
        !m_sceneRenderTarget || static_cast<int>(m_sceneRenderTarget->GetMsaaSampleCount()) == snapshot.activeSamples;
    snapshot.gameTargetAligned =
        !m_gameRenderTarget || static_cast<int>(m_gameRenderTarget->GetMsaaSampleCount()) == snapshot.activeSamples;
    snapshot.materialPipelinesAligned =
        !m_vkCore ||
        static_cast<int>(m_vkCore->GetMaterialPipelineManager().GetSampleCount()) == snapshot.activeSamples;
    snapshot.sceneMsaaColorBytes = m_sceneRenderTarget ? m_sceneRenderTarget->GetMsaaColorResidentBytes() : 0;
    snapshot.gameMsaaColorBytes = m_gameRenderTarget ? m_gameRenderTarget->GetMsaaColorResidentBytes() : 0;
    snapshot.reconfigurationCount = m_msaaReconfigurationCount;
    snapshot.rejectedRequestCount = m_msaaRejectedRequestCount;
    return snapshot;
}

void InxRenderer::SetPresentMode(int mode)
{
    if (m_vkCore)
        m_vkCore->SetPresentMode(mode);
}

int InxRenderer::GetPresentMode() const
{
    if (m_vkCore)
        return m_vkCore->GetPresentMode();
    return 1; // MAILBOX default
}

// ========================================================================
// Editor Power-Save / Idle Mode
// ========================================================================

void InxRenderer::SetEditorIdleEnabled(bool enabled)
{
    if (m_view)
        m_view->GetIdling().enableIdling = enabled;
}

bool InxRenderer::IsEditorIdleEnabled() const
{
    return m_view ? m_view->GetIdling().enableIdling : false;
}

void InxRenderer::SetEditorIdleFps(float fps)
{
    if (m_view)
        m_view->GetIdling().fpsIdle = fps;
}

float InxRenderer::GetEditorIdleFps() const
{
    return m_view ? m_view->GetIdling().fpsIdle : 0.0f;
}

bool InxRenderer::IsEditorIdling() const
{
    return m_view ? m_view->GetIdling().isIdling : false;
}

void InxRenderer::SetEditorFpsCap(float fps)
{
    if (m_view)
        m_view->GetIdling().editorFpsCap = fps;
}

float InxRenderer::GetEditorFpsCap() const
{
    return m_view ? m_view->GetIdling().editorFpsCap : 0.0f;
}

void InxRenderer::SetPlayModeRendering(bool play)
{
    const bool changed = m_view && m_view->IsPlayMode() != play;
    if (m_view)
        m_view->SetPlayMode(play);
    if (changed)
        InvalidateGpuViewStateForSceneBoundary();
}

bool InxRenderer::IsPlayModeRendering() const
{
    return m_view ? m_view->IsPlayMode() : false;
}

void InxRenderer::SetSelectedObjectId(uint64_t objectId)
{
    SetSelectionState(objectId, objectId == 0 ? std::vector<uint64_t>{} : std::vector<uint64_t>{objectId});
}

void InxRenderer::SetSelectionState(uint64_t primaryObjectId, const std::vector<uint64_t> &outlineObjectIds)
{
    if (m_selectedObjectId == primaryObjectId && m_selectedOutlineObjectIds == outlineObjectIds)
        return;

    m_selectedObjectId = primaryObjectId;
    m_selectedOutlineObjectIds = outlineObjectIds;

    // Selection changes are editor state publications, not ordinary scene
    // mutations. Publish them to the outline owner immediately so an idle or
    // throttled Scene view cannot keep a stale graph topology until another
    // operation (such as capture) happens to wake rendering.
    if (m_outlineRenderer)
        m_outlineRenderer->SetOutlineObjectIds(m_selectedOutlineObjectIds);
    if (m_sceneRenderGraph)
        m_sceneRenderGraph->SetOutlineRenderer(m_sceneViewVisible ? m_outlineRenderer.get() : nullptr);

    RequestFullSpeedFrame();
}

void InxRenderer::RequestFullSpeedFrame()
{
    if (m_view)
        m_view->RequestFullSpeedFrame();
}

void InxRenderer::RequestExternalWake()
{
    if (m_view)
        m_view->RequestExternalWake();
}

} // namespace infernux
