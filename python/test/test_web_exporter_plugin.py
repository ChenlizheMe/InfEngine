from __future__ import annotations

import importlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from Infernux.engine.build import BuildProfile, BuildRequest
from Infernux.plugins import player_file_exported


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_EDITOR = ROOT / "external" / "plugins" / "infernux_web" / "Editor"


def _web_module(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN_EDITOR))
    for name in tuple(sys.modules):
        if name == "infernux_web" or name.startswith("infernux_web."):
            sys.modules.pop(name)
    return importlib.import_module("infernux_web")


def _request(tmp_path: Path) -> BuildRequest:
    return BuildRequest(
        str(tmp_path / "project"),
        "web-wasm32",
        str(tmp_path / "output"),
        BuildProfile(options={"build_settings": {"scenes": []}}),
    )


def _probe_result(**overrides: object) -> dict[str, object]:
    details: dict[str, object] = {
        "detected_emscripten_version": "4.0.10",
        "node_version": "v24.19.0",
        "emcc": True,
        "emdawn_port": True,
        "python_wasm": True,
        "python_data": True,
        "python_library": True,
        "glslang_validator": True,
        "tint": True,
    }
    details.update(overrides)
    return {"error": "", "code": "", "command": ["fixture"], "details": details}


def test_web_exporter_contributes_only_webgpu_target(monkeypatch):
    module = _web_module(monkeypatch)
    targets = module.WebPlatformExporter().targets()

    assert [target.id for target in targets] == ["web-wasm32"]
    capabilities = targets[0].capabilities
    assert capabilities.graphics_api == "webgpu"
    assert not capabilities.threads
    assert not capabilities.dynamic_loading
    assert not capabilities.python_native_modules
    assert not capabilities.numba
    assert not capabilities.network
    assert capabilities.audio
    assert capabilities.text_input
    assert capabilities.gamepad_input
    assert not capabilities.persistent_storage
    assert {
        "dom-text-input",
        "gesture-gated-webaudio",
        "multi-pointer",
        "safe-area",
        "webgpu-capability-inventory",
    } <= capabilities.features


def test_web_build_cache_is_project_owned_by_default(monkeypatch, tmp_path):
    _web_module(monkeypatch)
    exporter = importlib.import_module("infernux_web.exporter")
    request = _request(tmp_path)

    assert exporter._web_staging_directory(request) == (
        (tmp_path / "project").resolve() / "Cache/Build/WebHost/web-wasm32"
    )


def test_web_build_cache_accepts_an_explicit_shared_root(monkeypatch, tmp_path):
    _web_module(monkeypatch)
    exporter = importlib.import_module("infernux_web.exporter")
    shared = tmp_path / "fast-build-disk"
    request = BuildRequest(
        str(tmp_path / "project"),
        "web-wasm32",
        str(tmp_path / "output"),
        BuildProfile(options={"build_cache_root": str(shared)}),
    )

    assert exporter._web_staging_directory(request) == (
        shared.resolve() / "WebHost/web-wasm32"
    )


def test_webgpu_capability_inventory_is_explicit_and_fire_forced(monkeypatch):
    _web_module(monkeypatch)
    capability_module = importlib.import_module("infernux_web.capabilities")
    document = capability_module.webgpu_capability_inventory()

    capability_module.validate_webgpu_capability_inventory(document)
    features = {feature["id"]: feature for feature in document["features"]}
    assert document["policy"] == "fire-forced"
    assert document["reference_backend"] == "vulkan"
    assert set(document["required_feature_ids"]) == {
        feature_id
        for feature_id, feature in features.items()
        if feature["required_for_040"]
    }
    assert all(
        features[feature_id]["status"] != "unsupported"
        for feature_id in document["required_feature_ids"]
    )
    assert features["material.custom-surface"]["status"] == "unsupported"
    assert features["material.custom-surface"]["required_for_040"] is False
    fixed_frame_features = {
        "render.pbr-material",
        "animation.skeletal",
        "shadow.directional",
        "render.transparent",
        "render.line-renderer",
        "particles.gpu-sprite",
        "post.bloom-hdr",
        "post.aces",
    }
    assert all(
        "web.smoke.vulkan-webgpu-fixed-frame" in features[feature_id]["validation"]
        for feature_id in fixed_frame_features
    )
    assert all(
        features[feature_id]["parity_gate"] == "closed"
        for feature_id in fixed_frame_features
    )
    assert features["lighting.point-lights"]["required_for_040"] is True
    assert features["lighting.point-lights"]["parity_gate"] == "closed"
    assert features["lighting.spot-lights"]["status"] == "unsupported"


def test_webgpu_capability_inventory_rejects_required_unsupported_feature(monkeypatch):
    _web_module(monkeypatch)
    capability_module = importlib.import_module("infernux_web.capabilities")
    document = capability_module.webgpu_capability_inventory()
    pbr = next(
        feature
        for feature in document["features"]
        if feature["id"] == "render.pbr-material"
    )
    pbr["status"] = "unsupported"
    pbr["reason"] = "fixture"

    with pytest.raises(ValueError, match="render.pbr-material is unsupported"):
        capability_module.validate_webgpu_capability_inventory(document)


def test_webgpu_capability_inventory_rejects_required_open_parity_gate(monkeypatch):
    _web_module(monkeypatch)
    capability_module = importlib.import_module("infernux_web.capabilities")
    document = capability_module.webgpu_capability_inventory()
    pbr = next(
        feature
        for feature in document["features"]
        if feature["id"] == "render.pbr-material"
    )
    pbr["parity_gate"] = "open"

    with pytest.raises(ValueError, match="open parity gate"):
        capability_module.validate_webgpu_capability_inventory(document)


def test_web_doctor_accepts_pinned_toolchain(monkeypatch):
    module = _web_module(monkeypatch)
    doctor = importlib.import_module("infernux_web.doctor")
    monkeypatch.setattr(doctor, "_run_toolchain_probe", lambda _values: _probe_result())

    report = module.inspect_web_toolchain("web-wasm32", {})

    assert report.available
    assert report.diagnostics == ()
    assert report.details["python_series"] == "3.13"


def test_web_doctor_rejects_wrong_emscripten_version(monkeypatch):
    module = _web_module(monkeypatch)
    doctor = importlib.import_module("infernux_web.doctor")
    monkeypatch.setattr(
        doctor,
        "_run_toolchain_probe",
        lambda _values: _probe_result(detected_emscripten_version="5.0.0"),
    )

    report = module.inspect_web_toolchain("web-wasm32", {})

    assert not report.available
    assert [item.code for item in report.diagnostics] == ["web.emscripten.version"]


def test_web_doctor_reports_missing_runtime_parts(monkeypatch):
    module = _web_module(monkeypatch)
    doctor = importlib.import_module("infernux_web.doctor")
    monkeypatch.setattr(
        doctor,
        "_run_toolchain_probe",
        lambda _values: _probe_result(
            emdawn_port=False,
            python_wasm=False,
            python_data=False,
            python_library=False,
            glslang_validator=False,
            tint=False,
        ),
    )

    report = module.inspect_web_toolchain("web-wasm32", {})

    assert not report.available
    assert {item.code for item in report.diagnostics} == {
        "web.webgpu.emdawn-port",
        "web.python.runtime",
        "web.python.stdlib",
        "web.python.static-library",
        "web.shader.glslang",
        "web.shader.tint",
    }


def test_web_exporter_plan_exposes_real_runtime_stages(monkeypatch, tmp_path):
    module = _web_module(monkeypatch)
    plan = module.WebPlatformExporter().create_plan(_request(tmp_path))

    assert [step.id for step in plan.steps] == [
        "cook",
        "shaders",
        "imports",
        "runtime",
        "webgpu",
        "package",
        "audit",
    ]
    assert plan.metadata == {
        "architecture": "wasm32",
        "graphics_api": "webgpu",
        "python": "3.13",
    }


def test_web_template_uses_default_or_requires_complete_project_shell(
    monkeypatch, tmp_path
):
    _web_module(monkeypatch)
    exporter = importlib.import_module("infernux_web.exporter")
    request = _request(tmp_path)
    default_shell = tmp_path / "default-shell.html"
    default_shell.write_text("default", encoding="utf-8")

    assert exporter._resolve_web_template(request, default_shell) == (
        default_shell.resolve(),
        None,
    )

    template = Path(request.project_root) / "ProjectSettings" / "WebTemplate"
    template.mkdir(parents=True)
    with pytest.raises(ValueError, match="missing shell.html"):
        exporter._resolve_web_template(request, default_shell)

    (template / "shell.html").write_text(
        '<canvas id="canvas"></canvas>\n{{{ SCRIPT }}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required markers"):
        exporter._resolve_web_template(request, default_shell)

    (template / "shell.html").write_text(
        "\n".join(
            (
                '<canvas id="canvas"></canvas>',
                "@INFERNUX_WEB_ASSET_REVISION@",
                "@INFERNUX_WEB_DISPLAY_MODE@",
                "@INFERNUX_WEB_CANVAS_WIDTH@",
                "@INFERNUX_WEB_CANVAS_HEIGHT@",
                "{{{ SCRIPT }}}",
            )
        ),
        encoding="utf-8",
    )
    assert exporter._resolve_web_template(request, default_shell) == (
        (template / "shell.html").resolve(),
        template.resolve(),
    )


def test_web_staging_refresh_preserves_native_build_cache(monkeypatch, tmp_path):
    _web_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_web.exporter")
    staging = tmp_path / "web-staging"
    host_marker = staging / "host-build" / "CMakeCache.txt"
    host_marker.parent.mkdir(parents=True)
    host_marker.write_text("cache", encoding="utf-8")
    for name in (".infernux-player-cook", "player-assets", "shader-cook"):
        path = staging / name
        path.mkdir()
        (path / "stale.txt").write_text("stale", encoding="utf-8")

    exporter_module._prepare_web_staging(staging)

    assert host_marker.read_text(encoding="utf-8") == "cache"
    assert not (staging / ".infernux-player-cook").exists()
    assert not (staging / "player-assets").exists()
    assert not (staging / "shader-cook").exists()


def test_web_export_publishes_versioned_cooked_player(monkeypatch, tmp_path):
    module = _web_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_web.exporter")
    exporter = module.WebPlatformExporter()
    monkeypatch.setattr(
        exporter,
        "doctor",
        lambda _request: importlib.import_module("Infernux.engine.build").CapabilityReport(
            True,
            details={
                "distribution": "fixture",
                "emsdk_root": "/fixture/emsdk",
                "cpython_root": "/fixture/cpython",
                "source_root": str(ROOT),
            },
        ),
    )
    request = _request(tmp_path)
    project_template = (
        Path(request.project_root) / "ProjectSettings" / "WebTemplate"
    )
    project_template.mkdir(parents=True)
    (project_template / "shell.html").write_text(
        "\n".join(
            (
                '<canvas id="canvas"></canvas>',
                "@INFERNUX_WEB_ASSET_REVISION@",
                "@INFERNUX_WEB_DISPLAY_MODE@",
                "@INFERNUX_WEB_CANVAS_WIDTH@",
                "@INFERNUX_WEB_CANVAS_HEIGHT@",
                "{{{ SCRIPT }}}",
            )
        ),
        encoding="utf-8",
    )
    (project_template / "theme").mkdir()
    (project_template / "theme" / "site.css").write_text(
        "body { background: black; }\n",
        encoding="utf-8",
    )
    player_assets = tmp_path / "player-assets"
    player_assets.mkdir()
    monkeypatch.setattr(
        exporter_module,
        "_cook_web_player_assets",
        lambda _request, _staging: ("Balance", player_assets),
    )
    monkeypatch.setattr(
        exporter_module,
        "_stage_engine_python_package",
        lambda _request, _assets, _source_root: None,
    )
    monkeypatch.setattr(
        exporter_module,
        "_stage_web_branding",
        lambda _assets, _source_root, _game_name: None,
    )
    monkeypatch.setattr(
        exporter_module,
        "_stage_web_shader_sources",
        lambda _request, _staging, _assets, _source_root: None,
    )
    revision = "1234567890abcdef12345678"

    captured_presentation = {}
    captured_template = {}

    def _build_host(
        _request,
        staging,
        _assets,
        _details,
        _source_root,
        presentation,
        web_shell,
        web_template,
    ):
        captured_presentation.update(presentation)
        captured_template.update(shell=web_shell, root=web_template)
        host_build = staging / "host-build"
        host_build.mkdir(parents=True)
        (host_build / "infernux-player.html").write_text(
            "\n".join(
                (
                    f"const assetRevision = '{revision}';",
                    f"infernux-player.{revision}.js",
                    "'infernux-player.wasm': `infernux-player.${assetRevision}.wasm`,",
                    "'infernux-player.data': `infernux-player.${assetRevision}.data`,",
                )
            ),
            encoding="utf-8",
        )
        for suffix in ("js", "wasm", "data"):
            (host_build / f"infernux-player.{revision}.{suffix}").write_bytes(
                suffix.encode("ascii")
            )
        (host_build / "infernux-logo.png").write_bytes(b"png")
        for name in (
            "infernux-favicon.png",
            "infernux-icon-192.png",
            "infernux-icon-512.png",
            "infernux.webmanifest",
            "infernux-branding.js",
        ):
            (host_build / name).write_bytes(name.encode("ascii"))
        shutil.copy2(
            player_assets / "infernux-webgpu-capabilities.json",
            host_build / "infernux-webgpu-capabilities.json",
        )
        return ("host built",)

    monkeypatch.setattr(exporter_module, "_configure_and_build_host", _build_host)

    result = exporter.execute(request, exporter.create_plan(request))

    assert result.success
    assert result.diagnostics == ()
    assert [item.kind for item in result.artifacts] == [
        "html",
        "js",
        "wasm",
        "data",
        "png",
        "png",
        "png",
        "png",
        "webmanifest",
        "js",
        "json",
        "css",
    ]
    assert all(Path(item.path).is_file() for item in result.artifacts)
    assert result.manifest["scope"] == "cooked-player"
    assert result.manifest["game"] == "Balance"
    assert result.manifest["asset_revision"] == revision
    assert result.manifest["entry_point"] == "infernux-player.html"
    assert result.manifest["web_template"] == "project"
    assert result.manifest["presentation"] == {
        "display_mode": "fullscreen_borderless",
        "window_width": 1280,
        "window_height": 720,
    }
    capability_manifest = result.manifest["webgpu_capabilities"]
    assert capability_manifest["path"] == "infernux-webgpu-capabilities.json"
    assert capability_manifest["open_parity_gates"] == []
    published_capabilities = json.loads(
        (
            Path(request.output_dir) / "infernux-webgpu-capabilities.json"
        ).read_text(encoding="utf-8")
    )
    assert published_capabilities["backend"] == "webgpu"
    assert captured_presentation == result.manifest["presentation"]
    assert captured_template == {
        "shell": (project_template / "shell.html").resolve(),
        "root": project_template.resolve(),
    }
    assert (
        Path(request.output_dir) / "web-template" / "theme" / "site.css"
    ).read_text(encoding="utf-8") == "body { background: black; }\n"
    assert result.logs == ("host built",)


def test_web_branding_uses_cooked_project_icon_and_game_name(monkeypatch, tmp_path):
    _web_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_web.exporter")
    from PIL import Image

    player_assets = tmp_path / "player-assets"
    data_root = player_assets / "BrandingGame_Data"
    icon = data_root / "Branding" / "project-icon.png"
    icon.parent.mkdir(parents=True)
    Image.new("RGBA", (96, 48), (240, 96, 48, 255)).save(icon)
    from Infernux.engine.player_package_native import write_pack

    write_pack(
        (("Branding/project-icon.png", icon),),
        data_root / "Content.inxpkg",
    )
    (data_root / "BuildManifest.json").write_text(
        json.dumps({"icon_path": "Branding/project-icon.png"}),
        encoding="utf-8",
    )

    branding = exporter_module._stage_web_branding(
        player_assets,
        ROOT,
        "Branding Game",
    )

    for name, size in (
        ("infernux-favicon.png", 32),
        ("infernux-icon-192.png", 192),
        ("infernux-icon-512.png", 512),
    ):
        with Image.open(branding / name) as generated:
            assert generated.mode == "RGBA"
            assert generated.size == (size, size)
    manifest = json.loads(
        (branding / "infernux.webmanifest").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "Branding Game"
    assert [item["sizes"] for item in manifest["icons"]] == ["192x192", "512x512"]
    branding_script = (branding / "infernux-branding.js").read_text(
        encoding="utf-8"
    )
    assert '"game_name":"Branding Game"' in branding_script
    assert "document.title" in branding_script


def test_web_host_contract_embeds_python_and_uses_only_webgpu(monkeypatch):
    _web_module(monkeypatch)
    host_templates = (
        ROOT
        / "external"
        / "plugins"
        / "infernux_web"
        / "Editor"
        / "infernux_web"
        / "templates"
        / "host"
    )
    cmake = (host_templates / "CMakeLists.txt").read_text(encoding="utf-8")
    main = (host_templates / "main.cpp").read_text(encoding="utf-8")
    rhi_backend = (host_templates / "WebGpuRhiDevice.cpp").read_text(encoding="utf-8")
    scene_renderer = (host_templates / "WebSceneRenderer.cpp").read_text(encoding="utf-8")
    particle_runtime = (host_templates / "WebParticleRuntime.cpp").read_text(encoding="utf-8")
    post_process_renderer = (host_templates / "WebPostProcessRenderer.cpp").read_text(
        encoding="utf-8"
    )
    screen_ui_renderer = (host_templates / "WebScreenUIRenderer.cpp").read_text(
        encoding="utf-8"
    )
    fullscreen = (
        ROOT / "cpp" / "infernux" / "function" / "renderer" / "FullscreenRenderer.cpp"
    ).read_text(encoding="utf-8")
    shell = (host_templates / "shell.html").read_text(encoding="utf-8")
    bootstrap = (host_templates / "bootstrap.py").read_text(encoding="utf-8")
    host_module = (host_templates / "InfernuxWebHostModule.cpp").read_text(encoding="utf-8")
    revision_stamp = (host_templates / "stamp_asset_revision.cmake").read_text(
        encoding="utf-8"
    )

    assert "libpython3.13.a" in cmake
    assert "--use-port=emdawnwebgpu" in cmake
    assert "infernux-webgpu-capabilities.json" in cmake
    assert "-sSTACK_SIZE=2097152" in cmake
    assert "set(CROSS_PLATFORM_DETERMINISTIC ON" in cmake
    assert "target_link_libraries(${_web_jolt_consumer} PRIVATE Jolt)" in cmake
    assert "MAIN_MODULE" not in cmake
    assert "FullscreenRenderer.cpp" in cmake
    assert "WebSceneRenderer.cpp" in cmake
    assert "WebPostProcessRenderer.cpp" in cmake
    assert "dv_smith_joint_ggx" in scene_renderer
    assert 'MaterialFloat(draw.material, "metallic", 0.0f)' in scene_renderer
    assert 'MaterialFloat(draw.material, "smoothness", 0.5f)' in scene_renderer
    assert 'MaterialVector(draw.material, "emissionColor"' in scene_renderer
    assert "material=pbr" in scene_renderer
    assert "geometric_specular_aa" in scene_renderer
    assert "max(vec3<f32>(1.0 - perceptual_roughness), f0)" in scene_renderer
    assert "horizon_occlusion(reflection_direction, geometric_normal)" in scene_renderer
    assert "specular_highlights" in scene_renderer
    assert "evaluate_toon_light" in scene_renderer
    assert "MaterialIsToon" in scene_renderer
    assert "alphaClipThreshold" in scene_renderer
    assert "discard;" in scene_renderer
    assert "material_base_color" in scene_renderer
    assert "material_metallic_map" in scene_renderer
    assert "material_smoothness_map" in scene_renderer
    assert "material_ao_map" in scene_renderer
    assert "material_normal_map" in scene_renderer
    assert "MaterialTextureGuids" in scene_renderer
    assert "ResolveMaterialTextureSet" in scene_renderer
    assert 'MaterialFloat(draw.material, "normalScale", 1.0f)' in scene_renderer
    assert "INFERNUX_WEB_MATERIAL_TEXTURE_READY" in scene_renderer
    assert "DecodeBcTextureToRgba8" in scene_renderer
    assert "TextureFormat::BC1RgbaSrgb" in scene_renderer
    assert "TextureFormat::BC3Srgb" in scene_renderer
    assert "TextureFormat::BC4UNorm" in scene_renderer
    assert "TextureFormat::BC5UNorm" in scene_renderer
    assert "descriptor.mipLevelCount" in scene_renderer
    assert "samplerDescriptor.mipmapFilter" in scene_renderer
    assert "samplerDescriptor.maxAnisotropy" in scene_renderer
    assert "offsetof(WebVertex, tangent)" in scene_renderer
    assert "source.texCoord" in scene_renderer
    assert "horizon_glow" in scene_renderer
    assert "smoothstep(-0.10, 0.45, y)" in scene_renderer
    assert 'EXCLUDE REGEX "SceneRenderExtractor\\\\.cpp$"' not in cmake
    assert "InxPack.cpp" in cmake
    assert "794ea1b0afca0f020f4e57b6732332231fb23c70" in cmake
    assert "INFERNUX_WEB_ZSTD_SOURCE_DIR" in cmake
    assert "SDL3::SDL3-static" in cmake
    assert "AudioEngine.cpp" in cmake
    assert "Py_Initialize" in main
    assert "JobSystem::InitializeInline" in main
    assert "PhysicsWorld::Instance().Initialize()" in main
    assert "infernux_web_configure_physics" in main
    assert "INFERNUX_WEB_PHYSICS_READY" in main
    assert main.index("Py_Initialize()") < main.index(
        "PhysicsWorld::Instance().Initialize()"
    ) < main.index('PyObject_GetAttrString(mainModule, "infernux_web_tick")')
    assert "def infernux_web_configure_physics()" in bootstrap
    assert "_install_platform_runtime_api(native_module)" in bootstrap
    assert "if _runtime_api_installed:" in bootstrap
    assert "configure_physics(json.dumps(configuration" in bootstrap
    assert 'Time._fixed_delta_time = configuration["fixed_delta_time"]' in bootstrap
    assert "source={'project' if authored else 'web-default'}" in bootstrap
    assert 'configuration["max_concurrency"] = 1' in bootstrap
    assert "RunPendingJobs(64)" in main
    assert "wgpuCreateInstance" in main
    assert "WebGpuRhiDevice" in main
    assert "g_fullscreenRenderer.EnsurePipeline" in main
    assert "g_fullscreenRenderer.Draw" in main
    assert "CreateGraphicsPipeline" not in main
    assert "CreateGraphicsPipeline" in fullscreen
    assert "ShaderSourceWGSL" not in main
    assert "ShaderSourceWGSL" in rhi_backend
    assert "TextureSampleType::Depth" in rhi_backend
    assert "SamplerBindingType::NonFiltering" in rhi_backend
    assert "IsBlockCompressedFormat" in rhi_backend
    assert "format == rhi::PixelFormat::RGBA16SFloat" in rhi_backend
    assert "SampleCount::Four" in rhi_backend
    assert "INFERNUX_WEBGPU_FULLSCREEN_RHI_READY" in main
    assert "g_sceneRenderer.Render" in main
    assert "!g_webGpuValidationFailed && g_sceneRenderer.Prepare" in main
    assert "scenePrepared && g_sceneRenderer.HasDepthTarget()" in main
    assert "INFERNUX_WEB_SCENE_RENDER_READY" in scene_renderer
    assert "INFERNUX_WEB_SKY_READY" in scene_renderer
    assert "INFERNUX_WEB_SHADOW_READY" in scene_renderer
    assert "SetSkyEnabledForDiagnostics" in scene_renderer
    assert "SetShadowsEnabledForDiagnostics" in scene_renderer
    assert "textureSampleCompareLevel" in scene_renderer
    assert "shadowPass.SetBindGroup(0, m_shadowCameraGroup)" in scene_renderer
    assert "shadowPass.SetBindGroup(0, m_cameraGroup)" not in scene_renderer
    assert "descriptor.colorAttachmentCount = 0" in scene_renderer
    assert "ExtractCameraFrame" in scene_renderer
    assert "frame->DrawCalls()" in scene_renderer
    assert "m_transparentPipeline" in scene_renderer
    assert "range.transparent" in scene_renderer
    assert "state.renderQueue >= 3000" in scene_renderer
    assert "kLineVertexMarker" in scene_renderer
    assert "source.boneWeights.x" in scene_renderer
    assert "source.boneWeights.y" in scene_renderer
    assert "INFERNUX_WEB_LINE_DRAW_READY" in scene_renderer
    assert "ToWebClipSpace(frame->PrimaryView().viewProjection)" in scene_renderer
    assert "ToWebClipSpace(camera->GetViewProjectionMatrix())" in particle_runtime
    assert "rhi::PixelFormat::RGBA16SFloat" in particle_runtime
    assert "wgpu::TextureFormat::RGBA16Float" in particle_runtime
    assert "let coverage =" not in particle_runtime
    assert "return input.color * output_data.material_tint;" in particle_runtime
    assert "ReadOutputMaterialTint(program)" in particle_runtime
    assert "INFERNUX_WEBGPU_PARTICLE_OUTPUT_READY" in particle_runtime
    assert "correction[1][1] = -1.0f" in scene_renderer
    assert "correction[1][1] = -1.0f" in particle_runtime
    assert "skinBoneMatrices" in scene_renderer
    assert "SetDepthStencilAttachment" not in scene_renderer
    assert "ShaderModuleDesc::FromWgsl" in main
    assert "CreateRenderPipeline" not in main
    assert "CreateRenderPipeline" in rhi_backend
    assert "encoder.Draw(3)" in fullscreen
    assert "InfernuxWebPointerEvent" in main
    assert "xPixels * surfaceScaleX" in main
    assert "yPixels * surfaceScaleY" in main
    assert "emscripten_set_touchstart_callback" not in main
    assert "emscripten_set_mousedown_callback" not in main
    assert "emscripten_set_keydown_callback" in main
    assert "emscripten_set_wheel_callback" in main
    assert "InfernuxWebGetKeyState" in main
    assert "InfernuxWebGetObjectPositionAxis" in main
    assert "InfernuxWebGetRuntimeDiagnostic" in main
    assert "return float(Input.touch_count)" in bootstrap
    assert "touch = Input.get_touch(int(argument))" in bootstrap
    assert "touch.normalized_position[0]" in bootstrap
    assert "touch.normalized_position[1]" in bootstrap
    assert "touch.is_primary" in bootstrap
    assert "phase_codes[touch.phase.value]" in bootstrap
    assert "emscripten_set_visibilitychange_callback" in main
    assert "emscripten_sample_gamepad_data" in main
    assert "InfernuxWebTextInput" in main
    assert '"configure_physics", ConfigurePhysics' in host_module
    assert '"_gpu_particle_artifact_revision", GpuParticleArtifactRevision' in host_module
    assert '"_gpu_particle_state_was_preserved", GpuParticleStateWasPreserved' in host_module
    assert "StateWasPreserved(uint64_t emitterId) const noexcept" in particle_runtime
    assert "config.physicsMaxBodies" in host_module
    assert "infernuxBeginTextInput" in shell
    assert "setPointerCapture" in shell
    assert "lostpointercapture" in shell
    assert "compositionstart" in shell
    assert "compositionend" in shell
    assert "visualViewport" in shell
    assert "safe-area-inset-top" in shell
    assert "const canvasRect = canvasNode.getBoundingClientRect()" in shell
    assert "safeViewportLeft - canvasRect.left" in shell
    assert "canvasRect.right - safeViewportRight" in shell
    assert "[safeLeft, safeTop, safeRight, safeBottom" in shell
    assert "InfernuxWebViewportChanged" in main
    assert "InfernuxWebPageLifecycle" in main
    assert "InfernuxWebUserActivation" in main
    assert "InfernuxWebSetRenderDiagnostic" in main
    assert "g_particleRenderingEnabledForDiagnostics" in main
    assert "SetBloomEnabledForDiagnostics" in main
    assert "INFERNUX_WEB_AUDIO_READY" in main
    assert "pagehide" in shell
    assert "pageshow" in shell
    assert 'install_runtime_service("text-input", web_host)' in bootstrap
    assert '"AudioSource": "audio_source"' in bootstrap
    assert 'import_module("Infernux.components.decorators")' in bootstrap
    assert 'import_module("Infernux.components.ref_wrappers")' in bootstrap
    assert '"SerializableObject": serializable_module.SerializableObject' in bootstrap
    assert '"int_field": fields_module.int_field' in bootstrap
    assert '"list_field": fields_module.list_field' in bootstrap
    assert '"GameObjectRef": ref_wrappers_module.GameObjectRef' in bootstrap
    assert '"disallow_multiple": decorators_module.disallow_multiple' in bootstrap
    assert '"add_component_menu": decorators_module.add_component_menu' in bootstrap
    assert '"Time": timing_module.Time' in bootstrap
    assert '"SceneManager": scene_module.SceneManager' in bootstrap
    assert "def _publish_screen_ui()" not in bootstrap
    assert "submit_screen_ui(payload)" not in bootstrap
    assert "INFERNUX_WEB_USER_ACTIVATION_REQUIRED" not in bootstrap
    assert "INFERNUX_WEB_AUDIO_USER_ACTIVATION_PENDING" in bootstrap
    assert "_player_session.activate()" in bootstrap
    assert "class _WebSplashPlayer:" in bootstrap
    assert "INFERNUX_WEB_SPLASH_READY" in bootstrap
    assert "INFERNUX_WEB_SPLASH_COMPLETE" in bootstrap
    assert "screen_ui_upload_image" in bootstrap
    assert "screen_ui_release_texture" in bootstrap
    assert "document.createElement" not in bootstrap
    assert "!g_splashActive && !g_webGpuValidationFailed" in main
    assert 'importlib.import_module("Infernux.screen")' in bootstrap
    assert '"begin_text_input"' in host_module
    assert "viewport-fit=cover" in shell
    assert 'rel="icon" type="image/png"' in shell
    assert 'rel="manifest"' in shell
    assert 'rel="apple-touch-icon"' in shell
    assert "infernux-branding.js" in shell
    assert "locateFile(path)" in shell
    assert "assetRevision" in shell
    assert "versionedAssets[path]" in shell
    assert "@INFERNUX_WEB_ASSET_REVISION@" in shell
    assert "infernux-logo.png" in shell
    assert "monitorRunDependencies(left)" in shell
    assert "infernux-progress-track" in shell
    assert "infernux-screen-ui" not in shell
    assert "infernuxSubmitScreenUI" not in shell
    assert "INFERNUX_WEB_SCREEN_UI_BRIDGE_READY" not in shell
    assert "contextmenu" in shell
    assert "event.preventDefault()" in shell
    assert 'autofocus aria-label="Infernux game canvas"' in shell
    assert "installKeyboardFocusBridge" in shell
    assert "focusGameplayCanvas()" in shell
    assert "INFERNUX_WEB_KEYBOARD_FOCUS_READY" in shell
    assert "Tap, click, or press a key to play" not in shell
    assert "INFERNUX_WEB_FIRST_FRAME_READY" in shell
    assert "playerPresentation.mode === 'windowed'" in shell
    assert "document.body.dataset.infernuxPresentation" in shell
    assert "@INFERNUX_WEB_DISPLAY_MODE@" in shell
    assert "@INFERNUX_WEB_CANVAS_WIDTH@" in shell
    assert "@INFERNUX_WEB_CANVAS_HEIGHT@" in shell
    assert "copy_if_different" in revision_stamp
    assert "infernux-player.${INFERNUX_WEB_ASSET_REVISION}.js" in revision_stamp
    assert "INFERNUX_WEB_PYTHON_ARCHIVE_INVALID" in main
    assert "infernux_web_input" in bootstrap
    assert "INFERNUX_WEB_CONTENT_INDEX_READY" in bootstrap
    assert "INFERNUX_WEB_SHADER_CATALOG_READY" in bootstrap
    assert "marshal.loads(payload[16:])" in bootstrap
    assert "infernux::inxpack::ReadEntry" in host_module
    assert "infernux::inxpack::Extract" in host_module
    assert '"extract_package"' in host_module
    assert '"submit_screen_ui"' not in host_module
    assert "INFERNUX_WEB_FIXED_CANVAS" in main
    assert "g_screenUIRenderer.Render" in main
    assert "g_postProcessRenderer.SceneColorAttachmentView()" in main
    assert "colorAttachment.resolveTarget = g_postProcessRenderer.SceneColorView()" in main
    assert "wgpu::StoreOp::Discard" in main
    assert "ReadRenderSettings(renderSettings" in main
    assert 'ReadSettingUInt(settings, "msaa_samples"' in main
    assert "pipelineDescriptor.multisample.count = m_sceneSampleCount" in scene_renderer
    assert "skyPipelineDescriptor.multisample.count = m_sceneSampleCount" in scene_renderer
    assert "descriptor.sampleCount = m_sceneSampleCount" in scene_renderer
    assert "pipelineDesc.multisample.count = static_cast<uint32_t>(sceneSampleCount)" in particle_runtime
    assert "descriptor.sampleCount = m_sceneSampleCount" in post_process_renderer
    assert "m_sceneColorMultisampled" in post_process_renderer
    assert '"msaa_samples": 4' in bootstrap
    assert 'sample_names = {"X1": 1, "X4": 4}' in bootstrap
    assert "return infernux_web_render_settings()" not in bootstrap
    assert "g_postProcessRenderer.PrepareBloom(encoder)" in main
    assert "g_postProcessRenderer.Configure(postProcessSettings)" in main
    assert "INFERNUX_WEB_POST_PROCESS_READY" in post_process_renderer
    assert "bloom_threshold" in post_process_renderer
    assert "downsample_13" in post_process_renderer
    assert "quadratic_threshold" in post_process_renderer
    assert "m_settings.bloomScatter" in post_process_renderer
    assert "BloomEnabled()" in post_process_renderer
    assert "aces_film" in post_process_renderer
    assert "mat3x3<f32>" in post_process_renderer
    assert "0.59719" in post_process_renderer
    assert "linear_to_srgb_channel" in post_process_renderer
    assert "def infernux_web_render_settings()" in bootstrap
    assert "def _web_render_stack_document()" in bootstrap
    assert "def _iter_web_render_effects(" in bootstrap
    assert "database.get_path_from_guid(guid)" in bootstrap
    render_settings = bootstrap[
        bootstrap.index("def infernux_web_render_settings()") : bootstrap.index(
            "def _web_render_stack_document()"
        )
    ]
    assert render_settings.index("_prepare_player_asset_contract()") < render_settings.index(
        "stack = _web_render_stack_document()"
    )
    asset_contract = bootstrap[
        bootstrap.index("def _prepare_player_asset_contract()") : bootstrap.index(
            "def _prepare_player_runtime()"
        )
    ]
    player_runtime = bootstrap[
        bootstrap.index("def _prepare_player_runtime()") : bootstrap.index(
            "class _WebSplashPlayer"
        )
    ]
    assert "session.load_scene(" not in asset_contract
    assert "_player_initial_scene_path = scene_path" in asset_contract
    assert "_prepare_player_asset_contract()" in player_runtime
    assert "session.load_scene(scene_path)" in player_runtime
    assert player_runtime.index("_install_runtime_lifecycle_bridge(") < player_runtime.index(
        "session.load_scene(scene_path)"
    )
    lifecycle_bridge = bootstrap[
        bootstrap.index("def _install_runtime_lifecycle_bridge(") : bootstrap.index(
            "def _prepare_player_asset_contract()"
        )
    ]
    assert lifecycle_bridge.index("scheduler.bind_native_bridge(scene_manager)") < lifecycle_bridge.index(
        "scene_manager.set_runtime_lifecycle_callbacks("
    )
    assert main.index("g_particleRuntime.Initialize(") < main.index(
        'PyObject_GetAttrString(mainModule, "infernux_web_ready")'
    )
    particle_initialization = main[
        main.index("if (!g_particleRuntime.Initialize(") : main.index(
            'std::printf("INFERNUX_WEBGPU_FULLSCREEN_RHI_READY'
        )
    ]
    assert 'std::fprintf(stderr, "INFERNUX_WEBGPU_PARTICLE_RUNTIME_FAILED' in particle_initialization
    assert "return;" in particle_initialization
    ready_contract = main[
        main.index('PyObject *ready = PyObject_GetAttrString(mainModule, "infernux_web_ready")') :
        main.index('std::printf("INFERNUX_WEBGPU_DEVICE_READY')
    ]
    assert "if (ready == nullptr)" in ready_contract
    assert 'PrintPythonError("ready-contract")' in ready_contract
    assert 'PrintPythonError("ready")' in ready_contract
    assert ready_contract.count("return;") == 2
    assert 'raise RuntimeError("Web Player initial scene requires an authored RenderStack")' in render_settings
    render_effect_path = bootstrap[
        bootstrap.index("def _web_render_effect_path(") : bootstrap.index(
            "def infernux_web_activate("
        )
    ]
    assert "if not guid:" in render_effect_path
    assert "path = path_hint" not in render_effect_path
    assert "render_effect_compiler" not in bootstrap
    assert '"infernux.post.bloom"' in bootstrap
    assert '"infernux.post.tonemapping"' in bootstrap
    assert "WebScreenUIRenderer.cpp" in cmake
    assert 'set(ZSTD_LEGACY_SUPPORT OFF CACHE BOOL "" FORCE)' in cmake
    assert "INFERNUX_WEB_SCREEN_UI_READY" in screen_ui_renderer
    assert "descriptor.depthStencil" not in screen_ui_renderer
    assert "INFERNUX_WEB_SCREEN_UI_TEXTURE_READY" in host_module
    assert "screen_ui_resolve_texture" in host_module
    assert '"screen_ui_upload_image", ScreenUIUploadImage' in host_module
    assert '"screen_ui_release_texture", ScreenUIReleaseTexture' in host_module
    assert "stbi_load_from_memory" in host_module
    assert "AddImageRounded" in screen_ui_renderer
    assert "command.GetTexID()" in screen_ui_renderer
    assert "screen_ui_add_text" in host_module
    assert "_WebScreenUITextureCache" in bootstrap
    assert "_screen_ui_texture_cache.get" in bootstrap
    assert "RuntimeScreenUISubmission._submit_canvas" in bootstrap
    assert "def _process_screen_ui_events(delta_time: float)" in bootstrap
    assert "UIEventProcessor()" in bootstrap
    assert "Input.get_game_mouse_frame_state(0)" in bootstrap
    assert bootstrap.index("_process_screen_ui_events(delta_time)") < bootstrap.index(
        "_submit_screen_ui()", bootstrap.index("def infernux_web_tick")
    )
    assert "INFERNUX_WEB_DISPLAY_MODE" in cmake
    assert "INFERNUX_WEB_CANVAS_WIDTH" in cmake
    assert "INFERNUX_WEB_CANVAS_HEIGHT" in cmake
    assert "INFERNUX_WEB_SHELL_FILE" in cmake
    assert "extract_package(package, data_root)" in bootstrap
    assert "INFERNUX_SINGLE_THREADED_RUNTIME=1" in cmake
    assert "register_shader" in host_module
    assert "InfernuxWebFindShaderSource" in main
    assert "static constexpr char vertexSource" not in main
    combined = "\n".join(
        (cmake, main, rhi_backend, scene_renderer, shell, bootstrap)
    ).casefold()
    assert re.search(r"\bopengl\b", combined) is None
    assert re.search(r"\bwebgl\b", combined) is None
    assert re.search(r"\bgles\b", combined) is None


def test_web_host_build_templates_are_editor_only():
    plugin_root = ROOT / "external" / "plugins" / "infernux_web"
    host_templates = (
        plugin_root / "Editor" / "infernux_web" / "templates" / "host"
    )
    assert not (plugin_root / "Runtime" / "web").exists()
    assert (host_templates / "CMakeLists.txt").is_file()
    assert (host_templates / "main.cpp").is_file()
    assert (host_templates / "shell.html").is_file()
    for path in host_templates.rglob("*"):
        if path.is_file():
            relative = path.relative_to(plugin_root).as_posix()
            assert player_file_exported({}, relative) is False


def test_web_shader_stage_deduplicates_shared_particle_kernel(monkeypatch, tmp_path):
    _web_module(monkeypatch)
    exporter = importlib.import_module("infernux_web.exporter")
    native = importlib.import_module("Infernux.lib")._Infernux
    monkeypatch.setattr(
        native,
        "_prepare_authored_shader_glsl",
        lambda source, _path: source,
    )
    request = _request(tmp_path)
    source_root = tmp_path / "source"
    shader_dir = source_root / "python" / "Infernux" / "resources" / "shaders"
    shader_dir.mkdir(parents=True)
    (shader_dir / "fullscreen_triangle.vert").write_text(
        "#version 450\nvoid main() {}\n", encoding="utf-8"
    )
    player_assets = tmp_path / "player-assets"
    (player_assets / "Balance_Data").mkdir(parents=True)
    particle_dir = (
        Path(request.project_root) / "Library" / "Artifacts" / "Particle"
    )
    particle_dir.mkdir(parents=True)
    kernel_hash = "a" * 64
    stage_names = {
        "bootstrap",
        "init",
        "update",
        "update_rendering_fused",
        "contact_prepare",
        "contact_solve",
        "contact_dispatch",
        "render_reset",
        "rendering",
    }
    emitters = []
    for stable_id in ("emitter-b", "emitter-a"):
        emitters.append(
            {
                "stable_id": stable_id,
                "kernel_hash": kernel_hash,
                "state_stride": 80,
                "event_type_count": 0,
                "collision_enabled": False,
                "continuation": None,
                "update_render_fusion": {"eligible": True},
                "data_interface_layout": {
                    "mesh_interfaces": [],
                    "texture2d_parameters": [],
                    "volume_interfaces": [],
                },
                "stages": {
                    name: "#version 450\nlayout(local_size_x=1) in; void main() {}\n"
                    for name in stage_names
                },
            }
        )
    (particle_dir / "shared.inxparticle").write_text(
        json.dumps({"gpu_glsl": {"emitters": emitters}}), encoding="utf-8"
    )

    exporter._stage_web_shader_sources(
        request, tmp_path / "staging", player_assets, source_root
    )

    catalog = json.loads(
        (player_assets / "web-particles" / "catalog.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(catalog["kernels"]) == 1
    assert catalog["kernels"][0]["stable_ids"] == ["emitter-a", "emitter-b"]
    assert len(catalog["kernels"][0]["stages"]) == len(stage_names)

def test_web_exporter_declares_one_shared_zstd_source(monkeypatch):
    _web_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_web.exporter")

    source = exporter_module._WEB_ZSTD_SOURCE
    assert source.name == "zstd"
    assert source.repository == "https://github.com/facebook/zstd.git"
    assert source.revision == "794ea1b0afca0f020f4e57b6732332231fb23c70"


def test_web_asset_revision_covers_content_runtime_and_shader_inputs(
    monkeypatch, tmp_path
):
    _web_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_web.exporter")
    staging = tmp_path / "staging"
    player = staging / "player-assets"
    runtime = tmp_path / "runtime"
    shaders = staging / "shader-cook"
    for root in (player, runtime, shaders):
        root.mkdir(parents=True)
    (player / "Content.inxpkg").write_bytes(b"content")
    (runtime / "main.cpp").write_bytes(b"runtime")
    (shaders / "manifest.json").write_bytes(b"shader")
    project_template = tmp_path / "project-template"
    project_template.mkdir()
    (project_template / "shell.html").write_bytes(b"custom shell")

    fullscreen = {
        "display_mode": "fullscreen_borderless",
        "window_width": 1280,
        "window_height": 720,
    }
    first = exporter_module._web_asset_revision(
        staging, player, runtime, presentation=fullscreen
    )
    (runtime / "main.cpp").write_bytes(b"runtime changed")
    second = exporter_module._web_asset_revision(
        staging, player, runtime, presentation=fullscreen
    )
    bytecode = player / "python" / "site-packages" / "Infernux" / "__pycache__"
    bytecode.mkdir(parents=True)
    (bytecode / "runtime.cpython-313.opt-1.pyc").write_bytes(b"compiled")
    third = exporter_module._web_asset_revision(
        staging, player, runtime, presentation=fullscreen
    )
    windowed = exporter_module._web_asset_revision(
        staging,
        player,
        runtime,
        presentation={**fullscreen, "display_mode": "windowed"},
    )
    custom = exporter_module._web_asset_revision(
        staging,
        player,
        runtime,
        presentation=fullscreen,
        project_template_root=project_template,
    )
    (project_template / "shell.html").write_bytes(b"custom shell changed")
    custom_changed = exporter_module._web_asset_revision(
        staging,
        player,
        runtime,
        presentation=fullscreen,
        project_template_root=project_template,
    )

    assert re.fullmatch(r"[0-9a-f]{24}", first)
    assert first != second
    assert second != third
    assert third != windowed
    assert custom != custom_changed
