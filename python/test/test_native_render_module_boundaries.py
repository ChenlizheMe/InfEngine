from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RENDERER = ROOT / "cpp" / "infernux" / "function" / "renderer"
RENDER_CORE = ROOT / "cpp" / "infernux" / "function" / "renderer" / "rhi"
VULKAN_BACKEND = ROOT / "cpp" / "infernux" / "function" / "renderer" / "vk"
SCENE = ROOT / "cpp" / "infernux" / "function" / "scene"
RENDERER_FRONTEND = (SCENE / "SceneRenderer.h", SCENE / "SceneRenderer.cpp")


def _native_sources(directory: Path) -> list[Path]:
    return sorted((*directory.rglob("*.h"), *directory.rglob("*.cpp")))


def _function_body(source: str, signature: str) -> str:
    """Return one C++ function body, including nested lambda/class braces."""

    signature_offset = source.find(signature)
    assert signature_offset >= 0, f"Unable to locate C++ function: {signature}"
    body_start = source.find("{", signature_offset + len(signature))
    assert body_start >= 0, f"Unable to locate body for C++ function: {signature}"

    depth = 0
    for offset in range(body_start, len(source)):
        token = source[offset]
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                return source[body_start : offset + 1]
    raise AssertionError(f"Unterminated C++ function body: {signature}")


def test_material_texture_sampler_honors_device_max_anisotropy_and_filter_mode() -> None:
    source = (RENDERER / "VkCoreMaterial.cpp").read_text(encoding="utf-8")
    importer = (
        ROOT
        / "cpp"
        / "infernux"
        / "function"
        / "resources"
        / "AssetImporter"
        / "ConcreteImporters.h"
    ).read_text(encoding="utf-8")

    assert "anisoLevel < 0 ? deviceMaxAnisotropy" in source
    assert "sampler.maxAnisotropy" in source
    assert 'filterMode == "linear" || filterMode == "bilinear"' in source
    assert "sampler.mipFilter = rhi::FilterMode::Nearest" in source
    assert 'meta.GetDataAs<int>("aniso_level") == 1' in importer
    assert 'meta.AddMetadata("aniso_level", -1)' in importer


def test_native_texture_settings_migrate_the_legacy_anisotropy_default() -> None:
    source = (
        ROOT
        / "cpp"
        / "infernux"
        / "function"
        / "resources"
        / "InxTexture"
        / "InxTexture.cpp"
    ).read_text(encoding="utf-8")

    assert "importedLevel == 1 ? -1 : importedLevel" in source


def test_lit_geometry_uses_the_camera_local_lighting_domain() -> None:
    source = (
        ROOT
        / "cpp"
        / "infernux"
        / "function"
        / "resources"
        / "InxFileLoader"
        / "InxShaderLoader.cpp"
    ).read_text(encoding="utf-8")

    assert 'if (needsLightingUBO || needsCameraLocalView)' in source
    assert '#define INX_SHADING_CAMERA_POSITION lighting.cameraPos.xyz' in source
    assert source.index('if (needsLightingUBO || needsCameraLocalView)') < source.index(
        '#define INX_SHADING_CAMERA_POSITION lighting.cameraPos.xyz'
    )


def test_texture_hot_reload_keeps_last_gpu_publication_during_async_decode() -> None:
    engine = (ROOT / "cpp" / "infernux" / "Infernux.cpp").read_text(encoding="utf-8")
    renderer = (RENDERER / "InxVkCoreModular.cpp").read_text(encoding="utf-8")
    reload_texture = _function_body(engine, "void Infernux::ReloadTexture")
    invalidate_texture = _function_body(
        renderer,
        "void InxVkCoreModular::InvalidateTextureCache",
    )

    assert "registry.InvalidateAsset(guid);" in reload_texture
    assert "registry.ReloadAsset(guid);" not in reload_texture
    assert "registry.LoadAsset<InxTexture>" not in reload_texture
    assert "RequestAssetRevision(textureGuid, requestedRevision)" in invalidate_texture
    assert "runtimeVersion + 1" in invalidate_texture
    assert "RefreshMaterialsUsingTexture(textureGuid)" in invalidate_texture


def test_render_core_remains_backend_neutral() -> None:
    violations: list[str] = []
    native_type = re.compile(r"\bVk[A-Z][A-Za-z0-9_]*\b")

    for path in _native_sources(RENDER_CORE):
        text = path.read_text(encoding="utf-8")
        if "<vulkan/" in text or '"vulkan/' in text or native_type.search(text):
            violations.append(path.relative_to(ROOT).as_posix())

    assert not violations, "RenderCore contains Vulkan API details: " + ", ".join(violations)


def test_vulkan_backend_does_not_depend_on_runtime_feature_domains() -> None:
    forbidden_includes = (
        "function/scene/",
        "function/editor/",
        "tools/pybinding/",
        "python/",
    )
    violations: list[str] = []

    for path in _native_sources(VULKAN_BACKEND):
        text = path.read_text(encoding="utf-8").replace("\\", "/")
        if any(fragment in text for fragment in forbidden_includes):
            violations.append(path.relative_to(ROOT).as_posix())

    assert not violations, "VulkanBackend depends on a higher-level runtime domain: " + ", ".join(violations)


def test_render_world_publication_contains_no_scene_component_pointers() -> None:
    world = (RENDERER / "RenderWorld.h").read_text(encoding="utf-8")
    scene_renderer = (
        ROOT / "cpp" / "infernux" / "function" / "scene" / "SceneRenderer.cpp"
    ).read_text(encoding="utf-8")

    structural = world.split("struct RenderProxyStructuralData", 1)[1].split(
        "struct RenderProxyFrameData", 1
    )[0]
    for forbidden in (
        "MeshRenderer *",
        "SkinnedMeshRenderer *",
        "Transform *",
        "GameObject *",
        "InxMaterial *",
    ):
        assert forbidden not in structural

    assert "MutableProxies" not in world
    assert "InxMaterial/InxMaterial.h" not in world
    assert "std::shared_ptr<const RenderWorldFrame> Acquire()" in world
    assert "std::atomic_exchange_explicit" in world

    camera_build = _function_body(
        scene_renderer, "CameraDrawCallResult SceneRenderer::BuildDrawCallsForCamera"
    )
    assert "m_renderWorld.Acquire()" in camera_build
    assert "GetGameObject" not in camera_build
    assert "GetLayer" not in camera_build
    assert "MeshRenderer" not in camera_build

    published_build = _function_body(
        scene_renderer, "const DrawCallResult &SceneRenderer::BuildDrawCalls"
    )
    assert "m_buildOwner = m_renderWorld.Acquire()" in published_build
    assert "m_buildOwner->DrawCalls()" in published_build
    assert "EmitDrawCallsForRenderable" not in published_build


def test_renderer_frontend_does_not_reacquire_scene_objects() -> None:
    forbidden_types = re.compile(
        r"\b(?:Scene|SceneManager|GameObject|Component|Transform|MeshRenderer|"
        r"SkinnedMeshRenderer|Camera)\s*\*"
    )
    forbidden_headers = (
        "SceneSystem.h",
        "SceneManager.h",
        "GameObject.h",
        "Component.h",
        "Transform.h",
        "MeshRenderer.h",
        "SkinnedMeshRenderer.h",
        "Camera.h",
    )
    violations: list[str] = []

    for path in RENDERER_FRONTEND:
        source = path.read_text(encoding="utf-8")
        includes = "\n".join(
            line for line in source.splitlines() if line.lstrip().startswith("#include")
        )
        if any(header in includes for header in forbidden_headers) or forbidden_types.search(source):
            violations.append(path.relative_to(ROOT).as_posix())

    assert not violations, (
        "Renderer frontend crossed back into mutable Scene ownership: "
        + ", ".join(violations)
    )


def test_descriptor_pool_ownership_is_centralized() -> None:
    owner = VULKAN_BACKEND / "VkDescriptorManager.cpp"
    operations = ("vkCreateDescriptorPool", "vkAllocateDescriptorSets", "vkDestroyDescriptorPool")
    violations: list[str] = []

    renderer_root = ROOT / "cpp" / "infernux" / "function" / "renderer"
    for path in _native_sources(renderer_root):
        if path == owner:
            continue
        text = path.read_text(encoding="utf-8")
        used = [operation for operation in operations if operation in text]
        if used:
            violations.append(f"{path.relative_to(ROOT).as_posix()}: {', '.join(used)}")

    assert not violations, "Descriptor pool ownership escaped VkDescriptorManager:\n" + "\n".join(violations)


def test_native_render_dll_dependency_direction() -> None:
    cmake = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "CMakeLists.txt",
            ROOT / "cmake" / "InfernuxSources.cmake",
            ROOT / "cmake" / "InfernuxNativeTargets.cmake",
        )
    )

    assert re.search(
        r"target_link_libraries\(InfernuxRenderCore\s+PUBLIC\s+InfernuxFoundation\s*\)",
        cmake,
        re.DOTALL,
    )
    assert re.search(
        r"target_link_libraries\(InfernuxVulkanBackend\s+PUBLIC\s+InfernuxRenderCore\s*\)",
        cmake,
        re.DOTALL,
    )
    assert re.search(
        r"target_link_libraries\(InfernuxRendererRuntime\s+PUBLIC\s+InfernuxFoundation\s*\)",
        cmake,
        re.DOTALL,
    )
    assert "add_library(InfernuxRendererRuntime SHARED ${INFERNUX_RENDERER_RUNTIME_SOURCES})" in cmake
    assert "add_library(InfernuxRuntime STATIC ${INFERNUX_RUNTIME_SOURCES})" in cmake
    assert "WINDOWS_EXPORT_ALL_SYMBOLS ON" in cmake
    runtime_dll_list = cmake.split("set(INFERNUX_RUNTIME_DLL_TARGETS", 1)[1].split(")", 1)[0]
    assert "InfernuxRuntime" not in runtime_dll_list
    assert re.search(
        r"set\(INFERNUX_RENDERER_RUNTIME_SOURCES.*?SceneRenderer\.cpp.*?\)",
        cmake,
        re.DOTALL,
    )
    assert re.search(
        r"list\(REMOVE_ITEM INFERNUX_RUNTIME_SOURCES.*?\$\{INFERNUX_RENDERER_RUNTIME_SOURCES\}",
        cmake,
        re.DOTALL,
    )
    assert not re.search(
        r"target_link_libraries\(InfernuxRenderCore\b[^)]*\bInfernuxVulkanBackend\b",
        cmake,
        re.DOTALL,
    )
    assert not re.search(
        r"target_link_libraries\(InfernuxVulkanBackend\b[^)]*\bInfernuxRuntime\b",
        cmake,
        re.DOTALL,
    )
    assert not re.search(
        r"target_link_libraries\(InfernuxRendererRuntime\b[^)]*\bInfernuxRuntime\b",
        cmake,
        re.DOTALL,
    )


def test_material_hot_publication_never_idles_the_device() -> None:
    source = (
        ROOT / "cpp" / "infernux" / "function" / "renderer" / "MaterialDescriptor.cpp"
    ).read_text(encoding="utf-8")

    hot_path = source.split("bool MaterialDescriptorManager::PublishDescriptorReplacement", 1)[1].split(
        "void MaterialDescriptorManager::RemoveDescriptorSet", 1
    )[0]
    assert "WaitForGpuIdleBeforeSharedDescriptorWrite" not in source
    assert "vkDeviceWaitIdle" not in hot_path
    assert "PublishDescriptorReplacement" in hot_path


def test_standalone_shader_modules_remain_pipeline_manager_owned() -> None:
    cache = (RENDERER / "VkShaderCache.cpp").read_text(encoding="utf-8")
    load = _function_body(cache, "void VkShaderCache::LoadShader")
    unload = _function_body(cache, "void VkShaderCache::UnloadShader")

    assert "pm.DestroyShaderModule(replaced)" in load
    assert "pm.DestroyShaderModule(vertIt->second)" in unload
    assert "pm.DestroyShaderModule(fragIt->second)" in unload
    assert "vkDestroyShaderModule" not in unload, (
        "VkShaderCache must not destroy a module behind VkPipelineManager's "
        "ownership table; doing so leaves a shutdown double-destroy."
    )


def test_fullscreen_effect_shader_reload_retires_cached_pipeline_revision() -> None:
    fullscreen = (RENDERER / "FullscreenRenderer.cpp").read_text(encoding="utf-8")
    vulkan_adapter = (RENDERER / "FullscreenRendererVkAdapter.cpp").read_text(
        encoding="utf-8"
    )
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    invalidate = _function_body(fullscreen, "void FullscreenRenderer::InvalidateShader")
    renderer_invalidate = _function_body(
        renderer, "void InxRenderer::InvalidateShaderCache"
    )

    assert "m_impl->DestroyPipeline" in invalidate
    assert "device->Release(entry.rhi.pipeline)" in fullscreen
    assert "vkDeviceWaitIdle" not in invalidate
    assert "vkDeviceWaitIdle" not in fullscreen
    assert "VkPipeline" not in fullscreen
    assert "VulkanRhiDevice" not in fullscreen
    assert "VulkanFullscreenRendererHost" in vulkan_adapter
    assert renderer_invalidate.count("InvalidateFullscreenShader(shaderId)") == 2
    assert renderer_invalidate.index("InvalidateFullscreenShader(shaderId)") < (
        renderer_invalidate.index("m_vkCore->InvalidateShaderCache(shaderId)")
    )


def test_fullscreen_passes_use_render_graph_dynamic_rendering_contract() -> None:
    fullscreen_header = (RENDERER / "FullscreenRenderer.h").read_text(encoding="utf-8")
    fullscreen = (RENDERER / "FullscreenRenderer.cpp").read_text(encoding="utf-8")
    scene_graph = (RENDERER / "SceneRenderGraph.cpp").read_text(encoding="utf-8")

    assert "bool useDynamicRendering = false;" in fullscreen_header
    assert "desc.useDynamicRendering = key.useDynamicRendering;" in fullscreen
    assert "desc.renderingSignature.colorFormats[0] = key.colorFormat;" in fullscreen
    assert "device->CreateGraphicsPipeline(desc)" in fullscreen
    assert "VkRenderPass" not in fullscreen
    assert "const bool useDynamicFullscreen = m_renderGraph->SupportsDynamicRendering();" in scene_graph
    assert "key.useDynamicRendering = useDynamicFullscreen;" in scene_graph
    assert "perViewShadowTextureName" in scene_graph
    assert "builder.ReadSampledDepth(fullscreenShadowInput);" in scene_graph
    assert "fullscreenShadowDependencyDeclared = true;" in scene_graph


def test_fullscreen_passes_discard_undefined_attachment_contents() -> None:
    scene_graph = (RENDERER / "SceneRenderGraph.cpp").read_text(encoding="utf-8")

    fullscreen_setup = scene_graph.index(
        "fsWrittenVersion = builder.WriteColor(fsOutputTarget, 0);"
    )
    clear = scene_graph.index(
        "builder.SetClearColor(0.0F, 0.0F, 0.0F, 0.0F);",
        fullscreen_setup,
    )
    render_area = scene_graph.index(
        "builder.SetRenderArea(fsPassWidth, fsPassHeight);",
        clear,
    )

    assert fullscreen_setup < clear < render_area


def test_presentation_recreation_uses_queue_scoped_drain() -> None:
    swapchain = (VULKAN_BACKEND / "VkSwapchainManager.cpp").read_text(encoding="utf-8")
    core = (
        ROOT / "cpp" / "infernux" / "function" / "renderer" / "InxVkCoreModular.cpp"
    ).read_text(encoding="utf-8")
    recreate = core.split("void InxVkCoreModular::RecreateSwapchain()", 1)[1].split(
        "void InxVkCoreModular::SetPresentMode", 1
    )[0]

    assert "context.WaitIdle()" not in swapchain
    assert "WaitIdleForPresentation" in swapchain
    assert "WaitIdleForPresentation" not in recreate
    assert ".Device().WaitIdle()" not in recreate


def test_dynamic_msaa_uses_generation_cutover_without_queue_drain() -> None:
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    apply_msaa = _function_body(renderer, "bool InxRenderer::ApplyMsaaSamples")

    forbidden_drains = (
        "WaitIdleForAllQueues",
        "WaitIdleForGraphics",
        "WaitIdleForPresentation",
        "vkDeviceWaitIdle",
        ".Device().WaitIdle",
    )
    used_drains = [name for name in forbidden_drains if name in apply_msaa]
    assert not used_drains, (
        "Dynamic MSAA must publish a replacement generation without draining GPU "
        f"queues; ApplyMsaaSamples still uses: {', '.join(used_drains)}"
    )

    forbidden_reinitialization = (
        "ShutdownMaterialSystem",
        "InitializeMaterialSystem",
        "ReinitializeMaterialPipelines",
        ".Shutdown(",
        ".Initialize(",
    )
    used_reinitialization = [
        name for name in forbidden_reinitialization if name in apply_msaa
    ]
    assert not used_reinitialization, (
        "Dynamic MSAA must preserve resident material state and publish a generation; "
        "ApplyMsaaSamples still performs destructive reinitialization through: "
        + ", ".join(used_reinitialization)
    )

    call_names = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", apply_msaa))
    generation_calls = sorted(
        name
        for name in call_names
        if "generation" in name.lower()
        and any(
            verb in name.lower()
            for verb in ("build", "prepare", "create", "commit", "publish", "install", "swap")
        )
    )
    assert generation_calls, (
        "ApplyMsaaSamples must call an explicit MSAA generation build/commit API so "
        "all replacement resources are prepared before publication."
    )

    retirement_calls = sorted(
        name
        for name in call_names
        if any(token in name.lower() for token in ("retire", "retirement", "defer"))
    )
    retirement_queue_call = re.search(
        r"\b\w*retirement\w*\s*(?:\.|->)\s*(?:Enqueue|Retire|Schedule)\w*\s*\(",
        apply_msaa,
        re.IGNORECASE,
    )
    assert retirement_calls or retirement_queue_call, (
        "ApplyMsaaSamples must hand the previous MSAA generation to a deferred "
        "retirement API instead of destroying it at function exit."
    )

    completion_epoch_calls = sorted(
        name
        for name in call_names
        if "completionepoch" in name.lower()
        or ("completion" in name.lower() and "epoch" in apply_msaa.lower())
    )
    assert completion_epoch_calls, (
        "Deferred MSAA retirement must be anchored to a GPU completion epoch; no "
        "completion-epoch query or publication call was found in ApplyMsaaSamples."
    )


def test_scene_and_game_resize_publish_target_generations_without_device_drain() -> None:
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    target_header = (RENDERER / "SceneRenderTarget.h").read_text(encoding="utf-8")
    target_source = (RENDERER / "SceneRenderTarget.cpp").read_text(encoding="utf-8")

    assert "void Resize(uint32_t width, uint32_t height)" not in target_header
    assert "void SceneRenderTarget::Resize" not in target_source

    for signature in (
        "void InxRenderer::ResizeSceneRenderTarget",
        "void InxRenderer::ResizeGameRenderTarget",
    ):
        body = _function_body(renderer, signature)
        assert "WaitIdle" not in body and "vkDeviceWaitIdle" not in body
        assert "std::make_unique<SceneRenderTarget>" in body
        assert "GetLastReservedCompletionEpoch" in body
        assert "RetireFramebuffersBeforeTargetReplacement" in body
        assert "ReplaceSceneTarget" in body
        assert "RetireResourcesAfter" in body


def test_scene_render_target_depth_is_sampleable_across_pipeline_switches() -> None:
    target_source = (RENDERER / "SceneRenderTarget.cpp").read_text(encoding="utf-8")
    device_header = (VULKAN_BACKEND / "VkDeviceContext.h").read_text(encoding="utf-8")
    device_source = (VULKAN_BACKEND / "VkDeviceContext.cpp").read_text(encoding="utf-8")

    create_depth = _function_body(
        target_source, "void SceneRenderTarget::CreateDepthAttachment"
    )
    assert "FindSampledDepthFormat" in create_depth
    assert "VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT" in create_depth
    assert "VK_IMAGE_USAGE_SAMPLED_BIT" in create_depth

    assert "FindSampledDepthFormat() const" in device_header
    sampled_format = _function_body(
        device_source, "VkFormat VkDeviceContext::FindSampledDepthFormat"
    )
    assert "VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT" in sampled_format
    assert "VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT" in sampled_format


def test_scene_picking_target_resize_uses_deferred_generation_publication() -> None:
    picking = (RENDERER / "ScenePickingService.cpp").read_text(encoding="utf-8")
    ensure_target = _function_body(picking, "bool ScenePickingService::EnsureTarget")

    assert "WaitIdle" not in ensure_target and "vkDeviceWaitIdle" not in ensure_target
    assert "std::make_unique<TargetGeneration>" in ensure_target
    assert "GetLastReservedCompletionEpoch" in ensure_target
    assert "RetireAfter" in ensure_target
    assert "m_target = std::move(candidate)" in ensure_target


def test_scene_picking_pass_publishes_dynamic_viewport_before_any_draw() -> None:
    picking = (RENDERER / "ScenePickingService.cpp").read_text(encoding="utf-8")
    record = _function_body(picking, "void ScenePickingService::Record")

    begin = record.index("dynamicCommands.begin")
    viewport = record.index("vkCmdSetViewport", begin)
    scissor = record.index("vkCmdSetScissor", viewport)
    geometry = record.index("DrawSceneFiltered", scissor)
    particles = record.index("RecordPickingDraw", geometry)
    assert begin < viewport < scissor < geometry < particles


def test_floating_editor_windows_move_only_from_their_title_bar() -> None:
    gui = (RENDERER / "gui" / "InxGUI.cpp").read_text(encoding="utf-8")

    assert "io.ConfigWindowsMoveFromTitleBarOnly = true;" in gui


def test_imgui_preview_textures_publish_only_after_async_upload_completion() -> None:
    gui = (RENDERER / "gui" / "InxGUI.cpp").read_text(encoding="utf-8")
    pump = _function_body(gui, "void InxGUI::PumpTextureUploads")
    submit = _function_body(gui, "uint64_t InxGUI::SubmitTextureForImGui")

    completion_guard = "pending.ticket->IsAsync() && !pending.ticket->IsComplete()"
    assert completion_guard in pump
    assert pump.index(completion_guard) < pump.index("TryPublishTextureUpload")
    assert "RequestFrame();" in pump
    assert "RequestFrame();" in submit


def test_imgui_preview_uploads_use_one_validated_mip_for_all_editor_sizes() -> None:
    gui = (RENDERER / "gui" / "InxGUI.cpp").read_text(encoding="utf-8")
    submit = _function_body(gui, "uint64_t InxGUI::SubmitTextureForImGui")

    assert "static_cast<uint32_t>(height), false" in submit
    assert "sampler.maxLod = 0.0f" in submit


def test_resident_texture_previews_keep_their_display_encoding_semantic() -> None:
    gui_header = (RENDERER / "gui" / "InxGUI.h").read_text(encoding="utf-8")
    gui = (RENDERER / "gui" / "InxGUI.cpp").read_text(encoding="utf-8")
    publish = _function_body(gui, "uint64_t InxGUI::PublishTextureViewForImGui")
    get_texture = _function_body(gui, "uint64_t InxGUI::GetImGuiTextureId")
    touch_texture = _function_body(gui, "bool InxGUI::TouchImGuiTextureId")

    assert "bool requiresDisplayEncoding = false;" in gui_header
    assert "resource.requiresDisplayEncoding = requiresDisplayEncoding;" in publish
    assert "ImGui_ImplVulkan_SetTextureLinearColor" in get_texture
    assert "requiresDisplayEncoding" in get_texture
    assert "ImGui_ImplVulkan_SetTextureLinearColor" in touch_texture
    assert "requiresDisplayEncoding" in touch_texture


def test_imgui_display_shader_is_compiled_from_the_current_source() -> None:
    external_cmake = (ROOT / "external" / "CMakeLists.txt").read_text(encoding="utf-8")
    shader = (
        RENDERER / "gui" / "backend" / "infernux_imgui_frag.frag"
    ).read_text(encoding="utf-8")
    backend_patch = (ROOT / "cmake" / "patch_imgui_vulkan_backend.py").read_text(
        encoding="utf-8"
    )

    assert "Vulkan_GLSLANG_VALIDATOR_EXECUTABLE" in external_cmake
    assert "INFERNUX_IMGUI_FRAGMENT_SOURCE" in external_cmake
    assert "INFERNUX_IMGUI_FRAGMENT_HEADER" in external_cmake
    assert "linear_to_srgb" in shader
    assert "sampled.rgb = linear_to_srgb(sampled.rgb);" in shader
    assert 'include "infernux_imgui_frag.u32"' in backend_patch


def test_completed_async_uploads_retain_timeline_dependency_until_publication() -> None:
    manager_header = (RENDERER / "vk" / "VkResourceManager.h").read_text(encoding="utf-8")
    manager = (RENDERER / "vk" / "VkResourceManager.cpp").read_text(encoding="utf-8")
    publish_texture = _function_body(manager, "bool VkResourceManager::TryPublishTextureUpload")
    poll = _function_body(manager, "void VkResourceManager::PollGpuUploads")

    assert "uint64_t m_uploadTimelineValue = 0;" in manager_header
    assert "ticket->m_uploadTimelineValue = ticket->m_upload.timelineValue;" in manager
    assert manager.count("m_asyncTransfer->GetTimelineSemaphore() != VK_NULL_HANDLE") >= 4
    assert "ticket->m_upload = {};" in poll
    assert "publishTimelineDependency();" in publish_texture
    assert publish_texture.index("if (ticket->m_complete)") < publish_texture.index("ticket->m_published = true;")
    assert publish_texture.index("publishTimelineDependency();") < publish_texture.index("ticket->m_published = true;")


def test_runtime_screen_ui_uses_dynamic_rendering_with_msaa_resolve() -> None:
    screen_ui = (RENDERER / "gui" / "InxScreenUIRenderer.cpp").read_text(encoding="utf-8")
    scene_graph = (RENDERER / "SceneRenderGraph.cpp").read_text(encoding="utf-8")
    graph_compile = (RENDERER / "vk" / "RenderGraphCompile.cpp").read_text(encoding="utf-8")

    assert "VkPipelineRenderingCreateInfo renderingInfo" in screen_ui
    assert "pipeInfo.renderPass = VK_NULL_HANDLE" in screen_ui
    assert "const bool usesScreenUI" in scene_graph
    assert "builder.UseDynamicRendering();" in scene_graph
    assert "attachment.resolveImageView" in graph_compile
    assert "VK_RESOLVE_MODE_AVERAGE_BIT" in graph_compile


def test_per_view_descriptor_publication_only_waits_for_its_frame_slot() -> None:
    graph = (RENDERER / "SceneRenderGraph.cpp").read_text(encoding="utf-8")
    forward_plus = _function_body(graph, "bool SceneRenderGraph::PrepareForwardPlusFrame")
    shadows = _function_body(
        graph, "void SceneRenderGraph::RefreshPerViewShadowDescriptor"
    )

    for name, body in (("Forward+", forward_plus), ("Shadow", shadows)):
        assert "WaitIdle" not in body and "vkDeviceWaitIdle" not in body, (
            f"{name} per-view descriptor publication runs after the renderer has "
            "waited for the current frame-slot fence; it must not drain the device."
        )
        assert "GetCurrentFrameSlot() % kMaxFramesInFlight" in body

    assert "m_perViewFrames[frameIndex]" in forward_plus
    assert "viewFrame.GeometrySet()" in forward_plus
    assert "viewFrame.ParticleSet()" in forward_plus
    assert "UpdatePerViewShadowMap(graphShadowDesc" in shadows
    assert "UpdatePerViewShadowMap(particleShadowDesc" in shadows

    core_header = (RENDERER / "InxVkCoreModular.h").read_text(encoding="utf-8")
    assert "AllocatePerViewDescriptorSet" not in core_header
    assert "m_perViewDescriptorLeases" not in core_header
    assert "AllocatePerViewDescriptorLease" in core_header


def test_forward_plus_grid_orders_ssbo_clear_before_atomic_population() -> None:
    source = (
        RENDERER / "lighting" / "ForwardPlusLightGrid.cpp"
    ).read_text(encoding="utf-8")
    shader = _function_body(
        source, "std::string_view ForwardPlusLightGrid::ShaderSource"
    )

    clear = "tile_light_masks[offset + word] = 0u;"
    atomic = "atomicOr(tile_light_masks[offset + (local_index >> 5u)]"
    first_memory_barrier = shader.index("memoryBarrierBuffer();", shader.index(clear))
    first_control_barrier = shader.index("barrier();", first_memory_barrier)

    assert shader.index(clear) < first_memory_barrier < first_control_barrier
    assert first_control_barrier < shader.index(atomic)
    assert shader.count("memoryBarrierBuffer();") >= 2


def test_render_graph_images_do_not_use_unsynchronized_memory_aliasing() -> None:
    source = (VULKAN_BACKEND / "RenderGraphCompile.cpp").read_text(encoding="utf-8")

    assert "imageInfo.flags |= VK_IMAGE_CREATE_ALIAS_BIT;" not in source
    assert "vkBindImageMemory(device, resource.allocatedImage, heap.memory" not in source
    assert "Allocate every graph image independently" in source


def test_render_graph_preserves_internal_resource_state_between_in_flight_frames() -> None:
    source = (VULKAN_BACKEND / "RenderGraph.cpp").read_text(encoding="utf-8")
    prepare = _function_body(
        source, "void RenderGraph::PrepareExecutionResourceStates"
    )
    begin = _function_body(source, "void RenderGraph::BeginExecution")
    execute = _function_body(source, "void RenderGraph::Execute")

    assert "resource.isExternal" in prepare
    assert "resource.type == ResourceType::RendererList" in prepare
    assert "previous.writerPassId == UINT32_MAX" in prepare
    assert "m_resourceStates[index] = m_initialResourceStates[index]" in prepare
    assert "PrepareExecutionResourceStates();" in begin
    assert "PrepareExecutionResourceStates();" in execute
    assert "m_resourceStates = m_initialResourceStates;" not in begin
    assert "m_resourceStates = m_initialResourceStates;" not in execute


def test_fullscreen_triangle_uses_the_standard_three_vertex_extent() -> None:
    source = (
        ROOT
        / "python"
        / "Infernux"
        / "resources"
        / "shaders"
        / "fullscreen_triangle.vert"
    ).read_text(encoding="utf-8")

    assert "(gl_VertexIndex & 1) << 2" in source
    assert "(gl_VertexIndex & 2) << 1" in source
    assert "(gl_VertexIndex & 2) << 2" not in source


def test_per_view_descriptor_leases_follow_view_and_preview_lifetimes() -> None:
    graph_header = (RENDERER / "SceneRenderGraph.h").read_text(encoding="utf-8")
    graph_source = (RENDERER / "SceneRenderGraph.cpp").read_text(encoding="utf-8")
    core_source = (RENDERER / "VkCoreDraw.cpp").read_text(encoding="utf-8")

    assert "struct PerViewFrameState" in graph_header
    assert "vk::DescriptorLease geometryDescriptor" in graph_header
    assert "vk::DescriptorLease particleDescriptor" in graph_header
    assert "std::array<PerViewFrameState, kMaxFramesInFlight> m_perViewFrames" in graph_header
    assert "descriptorManager.Retire(frame.geometryDescriptor)" in graph_source
    assert "descriptorManager.Retire(frame.particleDescriptor)" in graph_source
    assert "m_perViewDescriptorLeases" not in core_source

    for preview in ("GPUMaterialPreview", "GPUMeshPreview"):
        header = (RENDERER / "gui" / f"{preview}.h").read_text(encoding="utf-8")
        source = (RENDERER / "gui" / f"{preview}.cpp").read_text(encoding="utf-8")
        assert "vk::DescriptorLease m_fallbackShadowDescLease" in header
        assert "AllocatePerViewDescriptorLease()" in source
        assert "descriptorManager.Retire(m_fallbackShadowDescLease)" in source


def test_material_msaa_generation_preserves_resident_subsystems() -> None:
    manager = (RENDERER / "MaterialPipelineManager.cpp").read_text(encoding="utf-8")
    core = (RENDERER / "VkCoreMaterial.cpp").read_text(encoding="utf-8")
    reconfigure = _function_body(
        manager, "bool MaterialPipelineManager::ReconfigureSampleCount"
    )
    commit = _function_body(
        core, "bool InxVkCoreModular::CommitMaterialPipelineGeneration"
    )

    forbidden = (
        "Shutdown(",
        "Initialize(",
        "vkDeviceWaitIdle",
        ".WaitIdle(",
        "ReinitializeMaterialPipelines",
    )
    violations = [
        token for token in forbidden if token in reconfigure or token in commit
    ]
    assert not violations, (
        "MSAA material generation must retain shader caches, descriptor managers, and "
        "material objects instead of rebuilding the subsystem: " + ", ".join(violations)
    )

    assert "struct PreparedGeneration" in reconfigure
    assert reconfigure.index("struct PreparedGeneration") < reconfigure.index(
        "m_sampleCount = sampleCount"
    )
    assert reconfigure.index("destroyPrepared();") < reconfigure.index(
        "m_sampleCount = sampleCount"
    )
    assert "m_materialPipelineManager.ReconfigureSampleCount(newSampleCount)" in commit


def test_dynamic_msaa_retires_every_old_resource_at_one_cutover_epoch() -> None:
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    apply_msaa = _function_body(renderer, "bool InxRenderer::ApplyMsaaSamples")

    epoch_definition = re.findall(
        r"const\s+rhi::SubmissionSerial\s+(\w+)\s*=\s*[^;]*"
        r"GetLastReservedCompletionEpoch\s*\(\s*\)\s*;",
        apply_msaa,
    )
    assert epoch_definition == ["cutoverEpoch"], (
        "ApplyMsaaSamples must capture exactly one device-wide completion epoch for "
        f"the complete cutover; found {epoch_definition!r}."
    )
    assert apply_msaa.count("GetLastReservedCompletionEpoch()") == 1

    required_publications = {
        "Scene framebuffer": (
            "m_sceneRenderGraph->RetireFramebuffersBeforeTargetReplacement(cutoverEpoch)"
        ),
        "Game framebuffer": (
            "graph->RetireFramebuffersBeforeTargetReplacement(cutoverEpoch)"
        ),
        "Outline": "retirementQueue.RetireAfter(cutoverEpoch",
        "Scene target": (
            "retiredSceneTarget->RetireResourcesAfter(retirementQueue, cutoverEpoch)"
        ),
        "Game target": (
            "retiredGameTarget->RetireResourcesAfter(retirementQueue, cutoverEpoch)"
        ),
    }
    missing = [
        resource
        for resource, publication in required_publications.items()
        if publication not in apply_msaa
    ]
    assert not missing, (
        "The dynamic MSAA cutover does not retire every old resource family at the "
        "shared completion epoch: " + ", ".join(missing)
    )

    outline_retirement = re.search(
        r"if \(retiredOutline\).*?RetireAfter\(cutoverEpoch,.*?Cleanup\(false\)",
        apply_msaa,
        re.DOTALL,
    )
    screen_ui_retirement = re.search(
        r"if \(retiredScreenUI\).*?RetireAfter\(cutoverEpoch,",
        apply_msaa,
        re.DOTALL,
    )
    assert outline_retirement, "Outline must be retired without an idle wait."
    assert screen_ui_retirement, "Screen UI must retire at the shared cutover epoch."
    assert apply_msaa.count("retirementQueue.RetireAfter(cutoverEpoch") == 2

    scene_framebuffer_retirement = (
        "m_sceneRenderGraph->RetireFramebuffersBeforeTargetReplacement(cutoverEpoch)"
    )
    game_framebuffer_retirement = (
        "graph->RetireFramebuffersBeforeTargetReplacement(cutoverEpoch)"
    )
    assert apply_msaa.index(scene_framebuffer_retirement) < apply_msaa.index(
        "m_sceneRenderGraph->ReplaceSceneTarget"
    )
    assert apply_msaa.index(game_framebuffer_retirement) < apply_msaa.index(
        "graph->ReplaceSceneTarget"
    )
    assert apply_msaa.index("CommitMaterialPipelineGeneration") < apply_msaa.index(
        "m_sceneRenderGraph->ReplaceSceneTarget"
    )
    assert apply_msaa.index(scene_framebuffer_retirement) < apply_msaa.index(
        "retiredSceneTarget->RetireResourcesAfter"
    )
    assert apply_msaa.index(game_framebuffer_retirement) < apply_msaa.index(
        "retiredGameTarget->RetireResourcesAfter"
    )


def test_msaa_retirement_helpers_defer_destruction_without_idle_waits() -> None:
    target_source = (RENDERER / "SceneRenderTarget.cpp").read_text(encoding="utf-8")
    graph_source = (VULKAN_BACKEND / "RenderGraph.cpp").read_text(encoding="utf-8")
    outline_source = (RENDERER / "OutlineRenderer.cpp").read_text(encoding="utf-8")

    retire_target = _function_body(
        target_source, "void SceneRenderTarget::RetireResourcesAfter"
    )
    retire_framebuffers = _function_body(
        graph_source, "void RenderGraph::RetireFramebufferCacheAfter"
    )
    cleanup_outline = _function_body(
        outline_source, "void OutlineRenderer::Cleanup(bool waitForIdle)"
    )

    for name, body in (
        ("SceneRenderTarget", retire_target),
        ("RenderGraph framebuffer cache", retire_framebuffers),
    ):
        assert "WaitIdle" not in body, f"{name} retirement must not wait for the GPU."
        assert "RetireAfter" in body, f"{name} destruction must be completion-gated."
        assert "retirementSerial" in body

    assert retire_target.index("RetireAfter(retirementSerial") < retire_target.index(
        "m_imguiDescriptorSet = VK_NULL_HANDLE"
    )
    assert retire_framebuffers.index("m_framebufferCache.clear()") < (
        retire_framebuffers.index("RetireAfter(retirementSerial")
    )
    assert "if (waitForIdle && !m_core->IsShuttingDown())" in cleanup_outline
    assert "WaitIdle" in cleanup_outline


def test_outline_fallback_material_descriptor_uses_its_actual_buffer_range() -> None:
    outline_source = (RENDERER / "OutlineRenderer.cpp").read_text(encoding="utf-8")

    assert "vertMatBufInfo.range = VK_WHOLE_SIZE;" in outline_source
    assert "vertMatBufInfo.range = sizeof(UniformBufferObject);" not in outline_source


def test_particle_contact_diagnostics_are_explicit_bounded_readbacks() -> None:
    manager = (
        RENDERER / "particle" / "ParticleGpuSystemManager.cpp"
    ).read_text(encoding="utf-8")
    binding = (
        ROOT / "cpp" / "infernux" / "tools" / "pybinding" / "BindingInfernux.cpp"
    ).read_text(encoding="utf-8")
    contact_runtime = (
        RENDERER / "particle" / "ParticleGpuContactRuntime.h"
    ).read_text(encoding="utf-8")

    record_diagnostics = _function_body(
        manager, "void RecordDiagnostics(VkCommandBuffer commandBuffer)"
    )
    assert "if (pendingDiagnostics.empty()" in record_diagnostics
    assert "capture.contactCounterBytes = sizeof(GpuParticleContactCounters);" in record_diagnostics
    assert "ContactResources().counters" in record_diagnostics
    assert "std::min(contactCounters.currentRecordCount, capture.contactRecordCapacity)" in record_diagnostics
    assert "std::min(contactCounters.workItemCount, capture.contactWorkItemCapacity)" in record_diagnostics
    assert 'item["contact_current_record_count"]' in binding
    assert 'item["contact_work_item_count"]' in binding
    assert 'item["contact_overflow_count"]' in binding
    assert 'item["contact_max_per_particle"]' in binding
    assert 'item["multi_contact_particle_count"]' in binding
    assert 'item["contact_retained_order_hash"]' in binding
    assert 'item["contact_dropped_order_hash"]' in binding
    assert 'item["contact_min_particle_index"]' in binding
    assert 'item["contact_max_particle_index"]' in binding
    assert 'item["prepared_spawn_count"]' in binding
    assert 'item["prepared_spawn_base_id"]' in binding
    assert 'item["prepared_spawn_generation"]' in binding
    assert 'item["spawn_overflow_count"]' in binding
    assert 'item["accepted_spawn_total"]' in binding
    assert 'item["queued_burst_count"]' in binding
    assert 'item["consuming_burst_count"]' in binding
    assert 'item["accepting_burst_requests"]' in binding
    assert 'item["gpu_emitter_playing"]' in binding
    assert "static_assert(sizeof(GpuParticleContactCounters) == 48);" in contact_runtime


def test_collision_free_particle_graphs_skip_scene_collider_extraction() -> None:
    manager_header = (
        RENDERER / "particle" / "ParticleGpuSystemManager.h"
    ).read_text(encoding="utf-8")
    manager = (
        RENDERER / "particle" / "ParticleGpuSystemManager.cpp"
    ).read_text(encoding="utf-8")
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")

    assert "RequiresCollisionScene() const noexcept" in manager_header
    assert "m_impl->requiresCollisionScene =" in manager
    assert "entry.second->sourceProgram.collisionEnabled" in manager
    update_collision_scene = _function_body(
        renderer, "void InxRenderer::UpdateParticleCollisionScene()"
    )
    assert "!m_particleGpuSystemManager->RequiresCollisionScene()" in update_collision_scene


def test_msaa_public_contract_is_exactly_1_2_4_8() -> None:
    policy = (RENDERER / "MsaaPolicy.h").read_text(encoding="utf-8")
    pipeline = (
        ROOT
        / "python"
        / "Infernux"
        / "renderstack"
        / "default_forward_pipeline.py"
    ).read_text(encoding="utf-8")
    binding = (
        ROOT / "cpp" / "infernux" / "tools" / "pybinding" / "BindingInfernux.cpp"
    ).read_text(encoding="utf-8")

    validation = _function_body(policy, "IsValidMsaaSampleCount")
    accepted = {int(value) for value in re.findall(r"samples\s*==\s*(\d+)", validation)}
    assert accepted == {1, 2, 4, 8}

    enum_body = re.search(
        r"class\s+MSAASamples\s*\(IntEnum\)\s*:\s*(.*?)(?=\n\nclass\s)",
        pipeline,
        re.DOTALL,
    )
    assert enum_body, "Unable to locate the public MSAASamples enum."
    enum_values = {
        int(value)
        for value in re.findall(r"^\s*[A-Z][A-Z0-9_]*\s*=\s*(\d+)\s*$", enum_body.group(1), re.MULTILINE)
    }
    assert enum_values == accepted

    renderer_state = _function_body(binding, '"msaa_state"')
    assert "for (const int samples : {1, 2, 4, 8})" in renderer_state


def test_presentation_recreation_publishes_a_complete_generation() -> None:
    swapchain = (VULKAN_BACKEND / "VkSwapchainManager.cpp").read_text(encoding="utf-8")
    core = (
        ROOT / "cpp" / "infernux" / "function" / "renderer" / "InxVkCoreModular.cpp"
    ).read_text(encoding="utf-8")

    recreate = swapchain.split("bool VkSwapchainManager::Recreate", 1)[1].split(
        "bool VkSwapchainManager::BuildGeneration", 1
    )[0]
    assert recreate.index("BuildGeneration") < recreate.index("if (beforeCommit)")
    assert recreate.index("if (beforeCommit)") < recreate.index(
        "m_generation = std::move(candidate)"
    )
    assert recreate.index("m_generation = std::move(candidate)") < recreate.index(
        "DestroyGeneration(retired)"
    )
    assert "CleanupSwapchain" not in recreate

    core_recreate = core.split("void InxVkCoreModular::RecreateSwapchain()", 1)[1].split(
        "void InxVkCoreModular::SetPresentMode", 1
    )[0]
    assert "DestroyGuiRenderGraphs" in core_recreate
    assert "m_depthImage.reset" in core_recreate
    assert core_recreate.index("Presentation().Recreate") < core_recreate.index(
        "m_renderGraph.Initialize"
    )


def test_swapchain_presentation_is_an_explicit_render_graph_pass() -> None:
    core = (
        ROOT / "cpp" / "infernux" / "function" / "renderer" / "InxVkCoreModular.cpp"
    ).read_text(encoding="utf-8")
    graph_header = (VULKAN_BACKEND / "RenderGraph.h").read_text(encoding="utf-8")
    graph_compile = (VULKAN_BACKEND / "RenderGraphCompile.cpp").read_text(
        encoding="utf-8"
    )

    assert 'AddPresentPass("Present"' in core
    assert "builder.PresentRead(backbuffer)" in core
    assert "SetBackbufferFinalLayout" not in core
    assert "SetBackbufferFinalLayout" not in graph_header
    assert "m_backbufferFinalLayout" not in graph_header
    assert "m_backbufferFinalLayout" not in graph_compile


def test_material_consumers_resolve_after_property_publication() -> None:
    draw = (
        ROOT / "cpp" / "infernux" / "function" / "renderer" / "VkCoreDraw.cpp"
    ).read_text(encoding="utf-8")
    preview = (
        ROOT
        / "cpp"
        / "infernux"
        / "function"
        / "renderer"
        / "gui"
        / "GPUMaterialPreview.cpp"
    ).read_text(encoding="utf-8")

    draw_commit = draw.split("Commit CPU-side material changes", 1)[1].split(
        "VkPipeline pipeline = resolved.pipeline", 1
    )[0]
    assert draw_commit.index("UpdateMaterialUBO") < draw_commit.index("resolveMaterialPass")

    preview_publish = preview.split("Material property synchronization may publish", 1)[1].split(
        "passBindings.push_back(binding)", 1
    )[0]
    assert preview_publish.index("UpdateMaterialUBO") < preview_publish.index("refreshPassBinding")


def test_dynamic_rendering_migration_covers_outline_gizmos_and_previews() -> None:
    graph = (RENDERER / "SceneRenderGraph.cpp").read_text(encoding="utf-8")
    graph_header = (RENDERER / "SceneRenderGraph.h").read_text(encoding="utf-8")
    outline_header = (RENDERER / "OutlineRenderer.h").read_text(encoding="utf-8")
    outline = (RENDERER / "OutlineRenderer.cpp").read_text(encoding="utf-8")
    core_header = (RENDERER / "InxVkCoreModular.h").read_text(encoding="utf-8")
    core = (RENDERER / "VkCoreMaterial.cpp").read_text(encoding="utf-8")

    assert "GetEditorOverlayMaterialPass() const" in graph_header
    assert "const auto editorOverlayPass = GetEditorOverlayMaterialPass();" in graph
    assert graph.count("&editorOverlayPass") >= 3
    assert graph.count("builder.UseDynamicRendering();") >= 3

    assert "GraphicsRenderingSignature &maskSignature" in outline_header
    assert "BuildVkPipelineRenderingInfo(m_outlineMaskRenderingSignature" in outline
    assert "BuildVkPipelineRenderingInfo(m_outlineCompositeRenderingSignature" in outline
    assert "m_outlineMaskUsesDynamicRendering" in outline
    assert "m_outlineCompositeUsesDynamicRendering" in outline

    assert "GetOrCreatePreviewMaterialPass" in core_header
    assert "GetOrCreatePreviewMaterialPass" in core
    for preview in ("GPUMaterialPreview", "GPUMeshPreview"):
        header = (RENDERER / "gui" / f"{preview}.h").read_text(encoding="utf-8")
        source = (RENDERER / "gui" / f"{preview}.cpp").read_text(encoding="utf-8")
        assert "DynamicRenderingCommands m_dynamicRenderingCommands" in header
        assert "ResolveDynamicRenderingCommands" in source
        assert "GetOrCreatePreviewMaterialPass" in source
        assert "m_dynamicRenderingCommands.begin" in source
        assert "m_dynamicRenderingCommands.end" in source
        assert "SelectDynamicRenderingPath" not in source
        assert "vkCmdBeginRenderPass" not in source
        assert "vkCreateRenderPass" not in source
        assert "vkCreateFramebuffer" not in source


def test_game_camera_stack_owns_one_render_graph_per_camera() -> None:
    header = (RENDERER / "InxRenderer.h").read_text(encoding="utf-8")
    source = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")

    assert "m_gameRenderGraphs" in header
    assert "EnsureGameRenderGraph(class Camera *camera)" in header
    assert "for (Camera *gameCam : FindGameCamerasCached())" in source
    assert "appendView(graph, false, gameView, cameraDependency, &cameraFinal)" in source
    assert "cameraDependency = cameraFinal" in source


def test_render_graph_rebuild_preserves_compatible_material_pipelines() -> None:
    source = (RENDERER / "SceneRenderGraph.cpp").read_text(encoding="utf-8")
    body = _function_body(source, "void SceneRenderGraph::BuildRenderGraph")

    # Switching a custom RenderPipeline must rebuild its graph topology, but
    # it must not make every compatible scene material compile again. Format,
    # sample-count, and shader compatibility changes own their invalidation.
    assert "InvalidateAllMaterialPipelines" not in body


def test_camera_stack_uses_target_owned_color_and_depth_attachments() -> None:
    graph = (RENDERER / "SceneRenderGraph.cpp").read_text(encoding="utf-8")
    backend = (VULKAN_BACKEND / "RenderGraph.cpp").read_text(encoding="utf-8")

    assert 'm_renderGraph->ImportTexture(\n        "SceneDepth"' in graph
    assert "vk::ResourceHandle sharedDepth = m_importedDepthTarget" in graph
    assert "builder.PrepareDepthStencilAttachment(m_importedDepthTarget)" in graph
    assert "PassBuilder::PrepareDepthStencilAttachment" in backend


def test_player_shader_scan_uses_catalog_metadata_for_opaque_artifacts() -> None:
    source = (ROOT / "cpp" / "infernux" / "Infernux.cpp").read_text(
        encoding="utf-8"
    )
    body = _function_body(source, "void Infernux::LoadAndRegisterShaders")

    assert "adb->GetMetaByPath(assetPath)" in body
    assert "metadata->GetResourceType()" in body
    assert body.index("metadata->GetResourceType()") < body.index(
        "processPath(ToFsPath(assetPath))"
    )


def test_player_init_skips_full_asset_refresh_when_runtime_catalog_exists() -> None:
    source = (ROOT / "cpp" / "infernux" / "Infernux.cpp").read_text(encoding="utf-8")
    body = _function_body(source, "void Infernux::InitRenderer")
    catalog_install = body.index("InstallRuntimeAssetCatalog(runtimeAssetCatalog, true)")
    refresh = body.index("->Refresh()")
    player_guard = body.index("playerMode && hasRuntimeCatalog")
    assert player_guard < catalog_install < refresh


def test_player_init_pumps_events_around_shader_and_pipeline_work() -> None:
    source = (ROOT / "cpp" / "infernux" / "Infernux.cpp").read_text(encoding="utf-8")
    body = _function_body(source, "void Infernux::InitRenderer")
    assert "PumpStartupEvents" in body
    assert body.index("PumpStartupEvents") < body.index("LoadAndRegisterShaders")
    assert body.index("PreparePipeline") < body.rindex("PumpStartupEvents")


def test_player_window_stays_hidden_during_vulkan_device_init() -> None:
    source = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    body = _function_body(source, "void InxRenderer::Init")
    assert "RevealStartupWindow" not in source
    assert "ShowWindow()" not in body
    assert body.index("m_view->Init") < body.index("PumpStartupEvents")
    assert body.index("PumpStartupEvents") < body.index("m_vkCore->Init")


def test_player_window_skips_startup_maximize() -> None:
    source = (
        ROOT / "cpp" / "infernux" / "platform" / "window" / "InxView.cpp"
    ).read_text(encoding="utf-8")
    body = _function_body(source, "void InxView::SDLInit")
    assert "SDL_WINDOW_HIDDEN" in body
    assert "SDL_MaximizeWindow" in body
    assert "_INFERNUX_PLAYER_MODE" in body
    assert body.index("_INFERNUX_PLAYER_MODE") < body.index("SDL_MaximizeWindow")


def test_touch_events_wake_the_frame_loop_and_request_ui_refresh() -> None:
    source = (
        ROOT / "cpp" / "infernux" / "platform" / "window" / "InxView.cpp"
    ).read_text(encoding="utf-8")
    process_events = _function_body(source, "void InxView::ProcessEvent")
    process_one = _function_body(source, "bool InxView::ProcessOneEvent")
    for event_name in (
        "SDL_EVENT_FINGER_DOWN",
        "SDL_EVENT_FINGER_MOTION",
        "SDL_EVENT_FINGER_UP",
        "SDL_EVENT_FINGER_CANCELED",
    ):
        assert event_name in process_events
        assert event_name in process_one


def test_play_mode_supports_an_explicit_frame_cap() -> None:
    view_header = (
        ROOT / "cpp" / "infernux" / "platform" / "window" / "InxView.h"
    ).read_text(encoding="utf-8")
    view_source = (
        ROOT / "cpp" / "infernux" / "platform" / "window" / "InxView.cpp"
    ).read_text(encoding="utf-8")
    process_events = _function_body(view_source, "void InxView::ProcessEvent")
    engine = (ROOT / "python" / "Infernux" / "engine" / "engine.py").read_text(
        encoding="utf-8"
    )

    assert "float playFpsCap = 0.0f" in view_header
    assert "m_isPlayMode ? m_idling.playFpsCap" in process_events
    assert "m_isPlayMode && targetFps <= 0.0f" in process_events
    assert 'os.environ.get("INFERNUX_PLAYER_FPS_CAP")' in engine
    assert "self._engine.set_play_fps_cap(fps)" in engine


def test_renderer_honors_the_configured_frames_in_flight() -> None:
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    constructor = _function_body(renderer, "InxRenderer::InxRenderer")

    assert "auto &config = EngineConfig::Get()" in renderer
    assert "config.maxFramesInFlight" in renderer
    assert 'std::getenv("INFERNUX_MAX_FRAMES_IN_FLIGHT")' in renderer
    assert "ResolveMaxFramesInFlight()" in constructor


def test_mobile_lifecycle_suspends_and_rebinds_vulkan_presentation() -> None:
    view = (
        ROOT / "cpp" / "infernux" / "platform" / "window" / "InxView.cpp"
    ).read_text(encoding="utf-8")
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    core = (RENDERER / "InxVkCoreModular.cpp").read_text(encoding="utf-8")

    assert "SDL_AddEventWatch(&InxView::WatchApplicationEvents" in view
    assert "SDL_EVENT_WILL_ENTER_BACKGROUND" in view
    assert "m_applicationInBackground.store(true" in view
    assert "m_surfaceRecreationPending.store(true" in view
    assert "SDL_EVENT_WINDOW_PIXEL_SIZE_CHANGED" in view
    assert "m_hasCreatedSurface.load" in view
    assert "NeedsSurfaceRecreation()" in renderer
    assert "RecreatePresentationSurface" in renderer
    assert core.index("m_backend.Presentation().Destroy()") < core.index(
        "SDL_Vulkan_DestroySurface"
    )
    assert core.index("SDL_Vulkan_DestroySurface") < core.index(
        "createSurface(m_instance"
    )


def test_vulkan_surface_loss_escalates_beyond_swapchain_recreation() -> None:
    swapchain_h = (RENDERER / "vk" / "VkSwapchainManager.h").read_text(
        encoding="utf-8"
    )
    swapchain_cpp = (RENDERER / "vk" / "VkSwapchainManager.cpp").read_text(
        encoding="utf-8"
    )
    draw = (RENDERER / "VkCoreDraw.cpp").read_text(encoding="utf-8")
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")

    assert "SurfaceLost" in swapchain_h
    assert swapchain_cpp.count("VK_ERROR_SURFACE_LOST_KHR") >= 2
    assert "m_presentationSurfaceLost = true" in draw
    assert "ConsumePresentationSurfaceLost()" in renderer
    assert "RequestSurfaceRecreation()" in renderer


def test_prepare_pipeline_pumps_between_engine_shader_compiles() -> None:
    source = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    body = _function_body(source, "void InxRenderer::PreparePipeline")
    assert "PumpStartupEvents" in body
    assert body.count("pumpStartup()") >= 5
    assert "JobSystem::Get().Schedule" in body
    assert "CompileParticleProgramBundle" in body
    assert body.index("CompileParticleProgramBundle") < body.index(
        "m_sceneRenderGraph->Initialize"
    )


def test_native_pack_writers_release_the_gil_during_compression() -> None:
    """Background Player packing must not freeze editor Python/UI ticks."""

    sources = (
        ROOT / "cpp" / "infernux" / "tools" / "pybinding" / "BindingResources.cpp",
        ROOT
        / "cpp"
        / "infernux"
        / "tools"
        / "pybinding"
        / "BindingInfernuxBootstrap.cpp",
    )
    for path in sources:
        source = path.read_text(encoding="utf-8")
        binding = source[source.index('"_inxpack_write"') :]
        release = binding.index("py::gil_scoped_release release")
        write_call = binding.index("inxpack::Write(")
        manifest_conversion = binding.index("InxPackManifestToPython(manifest)")
        assert release < write_call < manifest_conversion


def test_renderer_keeps_primary_selection_separate_from_outline_subtree() -> None:
    renderer = (RENDERER / "InxRenderer.cpp").read_text(encoding="utf-8")
    renderer_body = _function_body(renderer, "void InxRenderer::SetSelectionState")
    engine = (ROOT / "cpp" / "infernux" / "Infernux.cpp").read_text(
        encoding="utf-8"
    )
    outline_body = _function_body(engine, "void Infernux::SetSelectionOutlines")

    assert "m_selectedObjectId = primaryObjectId" in renderer_body
    assert "m_selectedOutlineObjectIds = outlineObjectIds" in renderer_body
    assert "outlineObjectIds.back()" not in renderer_body
    assert "SetSelectionState(primaryObjectId, expandedIds)" in outline_body
