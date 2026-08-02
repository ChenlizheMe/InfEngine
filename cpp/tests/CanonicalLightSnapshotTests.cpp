#ifdef NDEBUG
#undef NDEBUG
#endif

#include <function/renderer/lighting/CanonicalLightGpuBuffer.h>
#include <function/renderer/lighting/ForwardPlusLightGrid.h>
#include <function/renderer/lighting/ShadowFrame.h>
#include <function/scene/LightingData.h>

#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <vector>

using infernux::CanonicalLightAffectsGeometry;
using infernux::CanonicalLightAffectsParticles;
using infernux::CanonicalLightData;
using infernux::CanonicalLightSnapshot;
using infernux::CanonicalLightType;
using infernux::lighting::BuildCanonicalLightUpload;
using infernux::lighting::CanonicalLightGpuHeader;

namespace
{

bool RectanglesOverlap(const infernux::lighting::ShadowAtlasRect &left,
                       const infernux::lighting::ShadowAtlasRect &right)
{
    return left.x < right.x + right.size && right.x < left.x + left.size && left.y < right.y + right.size &&
           right.y < left.y + left.size;
}

bool MatricesNear(const glm::mat4 &left, const glm::mat4 &right, float epsilon)
{
    for (int column = 0; column < 4; ++column) {
        for (int row = 0; row < 4; ++row) {
            if (std::abs(left[column][row] - right[column][row]) > epsilon)
                return false;
        }
    }
    return true;
}

struct FakeDevice final : infernux::rhi::Device
{
    std::vector<infernux::rhi::BufferDesc> buffers;
    std::vector<infernux::rhi::BindingLayoutDesc> layouts;
    std::vector<infernux::rhi::BindGroupDesc> groups;
    std::vector<infernux::rhi::ComputePipelineDesc> pipelines;
    uint32_t next = 1;
    uint32_t releasedBuffers = 0;
    uint32_t releasedGroups = 0;

    infernux::rhi::BufferHandle CreateBuffer(const infernux::rhi::BufferDesc &desc) override
    {
        buffers.push_back(desc);
        return {next++, 1};
    }
    infernux::rhi::TextureHandle CreateTexture(const infernux::rhi::TextureDesc &) override
    {
        return {};
    }
    infernux::rhi::TextureViewHandle CreateTextureView(const infernux::rhi::TextureViewDesc &) override
    {
        return {};
    }
    infernux::rhi::SamplerHandle CreateSampler(const infernux::rhi::SamplerDesc &) override
    {
        return {};
    }
    infernux::rhi::ShaderModuleHandle CreateShaderModule(const infernux::rhi::ShaderModuleDesc &desc) override
    {
        assert(desc.spirv && desc.wordCount >= 5);
        return {next++, 1};
    }
    infernux::rhi::BindingLayoutHandle CreateBindingLayout(const infernux::rhi::BindingLayoutDesc &desc) override
    {
        layouts.push_back(desc);
        return {next++, 1};
    }
    infernux::rhi::BindGroupHandle CreateBindGroup(const infernux::rhi::BindGroupDesc &desc) override
    {
        groups.push_back(desc);
        return {next++, 1};
    }
    infernux::rhi::GraphicsPipelineHandle CreateGraphicsPipeline(const infernux::rhi::GraphicsPipelineDesc &) override
    {
        return {};
    }
    infernux::rhi::ComputePipelineHandle CreateComputePipeline(const infernux::rhi::ComputePipelineDesc &desc) override
    {
        pipelines.push_back(desc);
        return {next++, 1};
    }
    bool WriteBuffer(infernux::rhi::BufferHandle, uint64_t, const void *, uint64_t) override
    {
        return true;
    }
    void Release(infernux::rhi::BufferHandle handle) noexcept override
    {
        releasedBuffers += handle.IsValid();
    }
    void Release(infernux::rhi::TextureHandle) noexcept override
    {
    }
    void Release(infernux::rhi::TextureViewHandle) noexcept override
    {
    }
    void Release(infernux::rhi::SamplerHandle) noexcept override
    {
    }
    void Release(infernux::rhi::ShaderModuleHandle) noexcept override
    {
    }
    void Release(infernux::rhi::BindingLayoutHandle) noexcept override
    {
    }
    void Release(infernux::rhi::BindGroupHandle handle) noexcept override
    {
        releasedGroups += handle.IsValid();
    }
    void Release(infernux::rhi::GraphicsPipelineHandle) noexcept override
    {
    }
    void Release(infernux::rhi::ComputePipelineHandle) noexcept override
    {
    }
};

struct DispatchTrace
{
    infernux::lighting::ForwardPlusGridConstants constants{};
    uint32_t x = 0;
    uint32_t y = 0;
    uint32_t z = 0;

    static void BindPipeline(void *, infernux::rhi::ComputePipelineHandle)
    {
    }
    static void BindGroup(void *, infernux::rhi::ComputePipelineHandle, uint32_t, infernux::rhi::BindGroupHandle)
    {
    }
    static void PushConstants(void *context, infernux::rhi::ComputePipelineHandle, uint32_t bytes, const void *data)
    {
        auto &self = *static_cast<DispatchTrace *>(context);
        assert(bytes == sizeof(self.constants));
        std::memcpy(&self.constants, data, bytes);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        auto &self = *static_cast<DispatchTrace *>(context);
        self.x = x;
        self.y = y;
        self.z = z;
    }
    static void DispatchIndirect(void *, infernux::rhi::BufferHandle, uint64_t)
    {
    }
};

} // namespace

int main()
{
    {
        // Convention lock: the engine compiles GLM with GLM_FORCE_LEFT_HANDED
        // and GLM_FORCE_DEPTH_ZERO_TO_ONE. Camera::GetViewMatrix uses
        // glm::lookAt, so a point in front of the camera must land at a
        // *positive* view-space z, and glm::perspective must map the near
        // plane to NDC z = 0. The lit-pass vertex template (v_ViewDepth) and
        // the CSM frustum-corner reconstruction both depend on this.
        const glm::vec3 eye(1.0f, 2.0f, 3.0f);
        const glm::vec3 forward(0.0f, 0.0f, 1.0f);
        const glm::mat4 view = glm::lookAt(eye, eye + forward, glm::vec3(0.0f, 1.0f, 0.0f));
        const glm::vec4 inFront = view * glm::vec4(eye + forward * 5.0f, 1.0f);
        assert(inFront.z > 4.99f && inFront.z < 5.01f);

        const glm::mat4 proj = glm::perspective(glm::radians(60.0f), 1.5f, 0.1f, 100.0f);
        const glm::vec4 nearClip = proj * (view * glm::vec4(eye + forward * 0.1f, 1.0f));
        assert(std::abs(nearClip.z / nearClip.w) < 0.001f);
        const glm::vec4 farClip = proj * (view * glm::vec4(eye + forward * 100.0f, 1.0f));
        assert(std::abs(farClip.z / farClip.w - 1.0f) < 0.001f);
    }

    {
        infernux::lighting::ShadowAtlasAllocator atlas(4096);
        const auto first = atlas.Allocate(1024, 4);
        const auto second = atlas.Allocate(1024, 4);
        const auto local = atlas.Allocate(512, 4);
        assert(first && second && local);
        assert(first->size == 1024 && first->InnerSize() == 1016);
        assert(second->size == 1024 && !RectanglesOverlap(*first, *second));
        assert(!RectanglesOverlap(*first, *local));
        assert(!RectanglesOverlap(*second, *local));
        assert(local->size == 512 && local->InnerSize() == 504);
        assert(!atlas.Allocate(4096, 4));

        infernux::lighting::ShadowAtlasAllocator productionAtlas(4096);
        const std::array<uint32_t, 4> primaryCascadeSizes{2048, 2048, 1024, 1024};
        const auto primaryCascades = productionAtlas.AllocateBatch(primaryCascadeSizes, 4);
        assert(primaryCascades);
        assert((*primaryCascades)[0].InnerSize() == 2040);
        assert((*primaryCascades)[1].InnerSize() == 2040);
        const auto spotTile = productionAtlas.Allocate(1024, 4);
        assert(spotTile && spotTile->InnerSize() == 1016);
        const std::array<uint32_t, 6> pointFaceSizes{512, 512, 512, 512, 512, 512};
        const auto pointFaceTiles = productionAtlas.AllocateBatch(pointFaceSizes, 4);
        assert(pointFaceTiles);

        infernux::lighting::ShadowAtlasAllocator isolatedSpotAtlas(4096);
        const auto isolatedSpot = isolatedSpotAtlas.Allocate(2048, 4);
        assert(isolatedSpot && isolatedSpot->InnerSize() == 2040);

        infernux::lighting::ShadowAtlasAllocator isolatedPointAtlas(4096);
        const std::array<uint32_t, 6> isolatedPointFaceSizes{1024, 1024, 1024, 1024, 1024, 1024};
        const auto isolatedPointFaces = isolatedPointAtlas.AllocateBatch(isolatedPointFaceSizes, 4);
        assert(isolatedPointFaces);
        for (const auto &face : *isolatedPointFaces)
            assert(face.InnerSize() == 1016);

        infernux::lighting::ShadowAtlasAllocator transactionalAtlas(1024);
        const std::array<uint32_t, 2> impossibleBatch{512, 1024};
        assert(!transactionalAtlas.AllocateBatch(impossibleBatch, 4));
        assert(transactionalAtlas.Allocate(1024, 4));

        infernux::lighting::ShadowAtlasAllocator fallbackAtlas(1024);
        assert(fallbackAtlas.Allocate(768, 4));
        const std::array<uint32_t, 1> preferredFallbackSize{512};
        const auto fallbackTile = fallbackAtlas.AllocateBatchWithFallback(preferredFallbackSize, 128, 4);
        assert(fallbackTile && (*fallbackTile)[0].size == 256);

        infernux::lighting::ShadowAtlasAllocator fallbackBatchAtlas(1024);
        assert(fallbackBatchAtlas.Allocate(768, 4));
        const std::array<uint32_t, 2> preferredFallbackBatch{512, 256};
        const auto fallbackBatch = fallbackBatchAtlas.AllocateBatchWithFallback(preferredFallbackBatch, 128, 4);
        assert(fallbackBatch);
        assert((*fallbackBatch)[0].size == 256 && (*fallbackBatch)[1].size == 128);

        const infernux::lighting::ShadowDepthRange visibleDepth{2.0f, 3000.0f};
        const auto splits = infernux::lighting::AdaptiveCascadeSplits(0.1f, visibleDepth, 5000.0f);
        // The first cascade anchors at half the nearest visible receiver
        // depth, not at the camera near plane, so an empty foreground does
        // not consume cascade resolution.
        assert(splits.front() == 1.0f);
        // Huge visible ranges are capped: four cascades never stretch across
        // kilometers of depth, the lit pass fades shadows out past the cap.
        assert(splits.back() <= infernux::lighting::MaxStableShadowDistance + 0.001f);
        assert(splits[1] < 25.0f); // The camera-near cascade stays tight.
        for (size_t index = 1; index < splits.size(); ++index)
            assert(splits[index] > splits[index - 1]);

        // A tightly fitted small scene keeps fade headroom past the farthest
        // receiver so geometry at the end of the range stays shadowed.
        const infernux::lighting::ShadowDepthRange roomDepth{1.0f, 20.0f};
        const auto roomSplits = infernux::lighting::AdaptiveCascadeSplits(0.1f, roomDepth, 1000.0f);
        assert(roomSplits.back() > 20.0f && roomSplits.back() < 30.0f);
        for (size_t index = 1; index < roomSplits.size(); ++index)
            assert(roomSplits[index] > roomSplits[index - 1]);

        // Distant-only content scales the distance cap with the nearest
        // visible receiver instead of losing shadows entirely.
        const infernux::lighting::ShadowDepthRange distantDepth{500.0f, 900.0f};
        const auto distantSplits = infernux::lighting::AdaptiveCascadeSplits(0.1f, distantDepth, 1000.0f);
        assert(distantSplits.back() > 900.0f);

        infernux::lighting::ShadowCamera camera;
        const auto directional = infernux::lighting::BuildStableDirectionalCascade(
            7, 0, camera, glm::vec3(0.2f, -1.0f, 0.3f), splits[0], splits[1], *first);
        assert(directional.lightId == 7 && directional.worldUnitsPerTexel > 0.0f);
        assert(std::isfinite(directional.viewProjection[0][0]));
        assert(!directional.lightVectorIsPosition);
        assert(glm::dot(directional.lightVector, glm::normalize(glm::vec3(-0.2f, 1.0f, -0.3f))) > 0.999f);

        // The explicit-corners overload (fed from the camera's real inverse
        // view-projection at runtime) matches the ShadowCamera-derived path
        // and exposes an orthonormal light-space basis for billboard casters.
        const auto explicitCorners = infernux::lighting::FrustumSliceCorners(camera, splits[0], splits[1]);
        const auto cornerDirectional = infernux::lighting::BuildStableDirectionalCascade(
            7, 0, explicitCorners, glm::vec3(0.2f, -1.0f, 0.3f), splits[0], splits[1], *first);
        assert(MatricesNear(cornerDirectional.viewProjection, directional.viewProjection, 0.000001f));
        assert(std::abs(glm::dot(cornerDirectional.viewRight, cornerDirectional.viewUp)) < 0.001f);
        assert(std::abs(glm::length(cornerDirectional.viewRight) - 1.0f) < 0.001f);
        assert(std::abs(glm::dot(cornerDirectional.viewRight, glm::normalize(glm::vec3(0.2f, -1.0f, 0.3f)))) < 0.001f);
        assert(std::abs(glm::dot(cornerDirectional.viewUp, glm::normalize(glm::vec3(0.2f, -1.0f, 0.3f)))) < 0.001f);

        infernux::lighting::ShadowCamera movedCamera = camera;
        movedCamera.position.x += directional.worldUnitsPerTexel * 0.25f;
        const auto movedDirectional = infernux::lighting::BuildStableDirectionalCascade(
            7, 0, movedCamera, glm::vec3(0.0f, -1.0f, 0.0f), splits[0], splits[1], *first);
        const auto stableDirectional = infernux::lighting::BuildStableDirectionalCascade(
            7, 0, camera, glm::vec3(0.0f, -1.0f, 0.0f), splits[0], splits[1], *first);
        assert(MatricesNear(stableDirectional.viewProjection, movedDirectional.viewProjection, 0.000001f));

        infernux::lighting::ShadowCamera orthographicCamera = camera;
        orthographicCamera.orthographic = true;
        orthographicCamera.orthographicHalfHeight = 7.0f;
        orthographicCamera.aspect = 2.0f;
        const auto orthographicCascade = infernux::lighting::BuildStableDirectionalCascade(
            10, 1, orthographicCamera, glm::vec3(0.2f, -1.0f, 0.3f), 15.0f, 40.0f, *second);
        for (const glm::vec3 &corner : infernux::lighting::FrustumSliceCorners(orthographicCamera, 15.0f, 40.0f)) {
            const glm::vec4 clip = orthographicCascade.viewProjection * glm::vec4(corner, 1.0f);
            const glm::vec3 ndc = glm::vec3(clip) / clip.w;
            assert(std::abs(ndc.x) <= 1.001f);
            assert(std::abs(ndc.y) <= 1.001f);
            assert(ndc.z >= -0.001f && ndc.z <= 1.001f);
        }

        const auto spot = infernux::lighting::BuildSpotShadowView(8, {}, {0, 0, -1}, 45.0f, 10.0f, *local);
        assert(spot.lightVectorIsPosition && glm::length(spot.lightVector) < 0.0001f);
        assert(std::abs(glm::dot(spot.viewRight, spot.viewUp)) < 0.001f);
        assert(std::abs(glm::dot(spot.viewRight, glm::vec3(0, 0, -1))) < 0.001f);
        const glm::vec4 spotEdge =
            spot.viewProjection * glm::vec4(10.0f * std::tan(glm::radians(22.5f)), 0.0f, -10.0f, 1.0f);
        assert(std::abs(spotEdge.x / spotEdge.w - 1.0f) < 0.001f);

        std::array<infernux::lighting::ShadowAtlasRect, 6> pointTiles{};
        infernux::lighting::ShadowAtlasAllocator pointAtlas(4096);
        for (auto &tile : pointTiles) {
            const auto allocation = pointAtlas.Allocate(512, 4);
            assert(allocation);
            tile = *allocation;
        }
        const auto point = infernux::lighting::BuildPointShadowViews(9, {}, 12.0f, pointTiles);
        assert(point[0].subView == 0 && point[5].subView == 5);
        assert(point[0].nearPlane > 0.0f && point[0].farPlane == 12.0f);
        assert(point[0].lightVectorIsPosition && glm::length(point[0].lightVector) < 0.0001f);
        constexpr std::array<glm::vec3, 6> faceDirections = {glm::vec3(1, 0, 0), glm::vec3(-1, 0, 0),
                                                             glm::vec3(0, 1, 0), glm::vec3(0, -1, 0),
                                                             glm::vec3(0, 0, 1), glm::vec3(0, 0, -1)};
        for (size_t face = 0; face < point.size(); ++face) {
            const glm::vec4 clip = point[face].viewProjection * glm::vec4(faceDirections[face], 1.0f);
            const glm::vec3 ndc = glm::vec3(clip) / clip.w;
            assert(std::abs(ndc.x) < 0.0001f && std::abs(ndc.y) < 0.0001f);
            assert(ndc.z > 0.0f && ndc.z < 1.0f);
        }
    }

    static_assert(sizeof(CanonicalLightData) == 128);
    static_assert(alignof(CanonicalLightData) == 16);

    CanonicalLightSnapshot snapshot;
    snapshot.Clear(41);

    CanonicalLightData directional{};
    directional.metadata = glm::uvec4(static_cast<uint32_t>(CanonicalLightType::Directional), 0xFFFFFFFFu, 2u,
                                      CanonicalLightAffectsGeometry | CanonicalLightAffectsParticles);
    snapshot.Add(directional);

    for (uint32_t index = 0; index < 1000; ++index) {
        CanonicalLightData local{};
        local.positionRange = glm::vec4(static_cast<float>(index), 0.0f, 0.0f, 12.0f);
        local.metadata = glm::uvec4(index % 2u == 0u ? static_cast<uint32_t>(CanonicalLightType::Point)
                                                     : static_cast<uint32_t>(CanonicalLightType::Spot),
                                    1u << (index % 31u), 0u, CanonicalLightAffectsGeometry);
        snapshot.Add(local);
    }

    assert(snapshot.generation == 41);
    assert(snapshot.directionalLights.size() == 1);
    assert(snapshot.localLights.size() == 1000);
    assert(snapshot.Size() == 1001);
    assert(snapshot.localLights[999].positionRange.x == 999.0f);

    const auto upload = BuildCanonicalLightUpload(snapshot);
    assert(upload.header.countsAndGeneration.x == 1);
    assert(upload.header.countsAndGeneration.y == 1000);
    assert(upload.header.countsAndGeneration.z == 41);
    assert(upload.bytes.size() == sizeof(CanonicalLightGpuHeader) + 1001 * sizeof(CanonicalLightData));
    CanonicalLightData packedDirectional{};
    std::memcpy(&packedDirectional, upload.bytes.data() + sizeof(CanonicalLightGpuHeader), sizeof(CanonicalLightData));
    assert(packedDirectional.metadata.w == (CanonicalLightAffectsGeometry | CanonicalLightAffectsParticles));

    snapshot.Clear(42);
    assert(snapshot.generation == 42);
    assert(snapshot.Size() == 0);
    assert(snapshot.directionalLights.capacity() >= 1);
    assert(snapshot.localLights.capacity() >= 1000);

    const auto gridConfig =
        infernux::lighting::BuildForwardPlusGridConfig(1920, 1080, 1000, CanonicalLightAffectsGeometry);
    assert(gridConfig.IsValid());
    assert(gridConfig.tileCountX == 120 && gridConfig.tileCountY == 68 && gridConfig.tileCount == 8160);
    assert(gridConfig.maskWordStride == 32);
    assert(gridConfig.headerBytes == 8161ull * 16ull);
    assert(gridConfig.maskBytes == 8160ull * 32ull * sizeof(uint32_t));
    assert(gridConfig.maskBytes * 31ull < 8160ull * 1000ull * sizeof(uint32_t));
    assert(infernux::lighting::BuildForwardPlusGridConfig(16, 16, 0, CanonicalLightAffectsGeometry).maskWordStride ==
           1);
    assert(infernux::lighting::BuildForwardPlusGridConfig(16, 16, 32, CanonicalLightAffectsGeometry).maskWordStride ==
           1);
    assert(infernux::lighting::BuildForwardPlusGridConfig(16, 16, 33, CanonicalLightAffectsGeometry).maskWordStride ==
           2);

    FakeDevice device;
    std::array<uint32_t, 5> shaderWords{0x07230203u};
    infernux::lighting::ForwardPlusLightGrid grid;
    assert(grid.Initialize(device, 2, {shaderWords.data(), shaderWords.size()}));
    const infernux::rhi::BufferHandle canonicalBuffer{900, 1};
    assert(grid.PrepareFrame(0, 1920, 1080, 1000, CanonicalLightAffectsGeometry, canonicalBuffer));
    assert(grid.PrepareFrame(1, 1280, 720, 4, CanonicalLightAffectsParticles, canonicalBuffer));
    assert(device.layouts.size() == 2 && device.layouts[0].entryCount == 3 && device.layouts[1].entryCount == 3);
    assert(grid.ConsumerLayout().IsValid());
    assert(device.pipelines.size() == 1 && device.pipelines[0].pushConstantBytes == 112);
    assert(device.groups.size() == 4 && device.groups[0].bufferCount == 3 && device.groups[1].bufferCount == 3);
    assert(grid.Frame(0).headers != grid.Frame(1).headers && grid.Frame(0).lightMasks != grid.Frame(1).lightMasks);

    DispatchTrace trace;
    const infernux::rhi::ComputeCommandEncoder::DispatchTable dispatch = {
        &DispatchTrace::BindPipeline, &DispatchTrace::BindGroup, &DispatchTrace::PushConstants,
        &DispatchTrace::Dispatch, &DispatchTrace::DispatchIndirect};
    const infernux::rhi::ComputeCommandEncoder encoder(&trace, &dispatch);
    infernux::lighting::ForwardPlusGridConstants constants{};
    constants.viewportAndProjectionScale[2] = 1.0f;
    constants.viewportAndProjectionScale[3] = 1.0f;
    grid.Record(1, encoder, constants);
    assert(trace.x == 80 && trace.y == 45 && trace.z == 1);
    assert(trace.constants.gridAndLights[2] == 4 && trace.constants.gridAndLights[3] == 16);
    assert(trace.constants.domainAndMaskWords[0] == CanonicalLightAffectsParticles);
    assert(trace.constants.domainAndMaskWords[1] == 1);

    const std::string_view source = infernux::lighting::ForwardPlusLightGrid::ShaderSource();
    assert(source.find("local_size_x = 64") != std::string_view::npos);
    assert(source.find("metadata.w & pc.domain_mask_words.x") != std::string_view::npos);
    assert(source.find("atomicOr(tile_light_masks") != std::string_view::npos);
    assert(source.find("MAX_LIGHTS") == std::string_view::npos);
    grid.Shutdown();

    assert(device.releasedBuffers == 4 && device.releasedGroups == 4);
    return 0;
}
