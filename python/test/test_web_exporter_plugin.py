from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

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
        BuildProfile(),
    )


def _probe_result(**overrides: object) -> dict[str, object]:
    details: dict[str, object] = {
        "detected_emscripten_version": "6.0.8",
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
    } <= capabilities.features


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
        "_stage_web_shader_sources",
        lambda _request, _staging, _assets, _source_root: None,
    )
    revision = "1234567890abcdef12345678"

    def _build_host(_request, staging, _assets, _details, _source_root):
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
        return ("host built",)

    monkeypatch.setattr(exporter_module, "_configure_and_build_host", _build_host)

    result = exporter.execute(request, exporter.create_plan(request))

    assert result.success
    assert result.diagnostics == ()
    assert [item.kind for item in result.artifacts] == ["html", "js", "wasm", "data"]
    assert all(Path(item.path).is_file() for item in result.artifacts)
    assert result.manifest["scope"] == "cooked-player"
    assert result.manifest["game"] == "Balance"
    assert result.manifest["asset_revision"] == revision
    assert result.manifest["entry_point"] == "infernux-player.html"
    assert result.logs == ("host built",)


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
    assert "MAIN_MODULE" not in cmake
    assert "FullscreenRenderer.cpp" in cmake
    assert "WebSceneRenderer.cpp" in cmake
    assert 'EXCLUDE REGEX "SceneRenderExtractor\\\\.cpp$"' not in cmake
    assert "InxPack.cpp" in cmake
    assert "35016bc1c0b9a2f7121b7ecc312100aad7d9f2ad" in cmake
    assert "INFERNUX_WEB_ZSTD_SOURCE_DIR" in cmake
    assert "SDL3::SDL3-static" in cmake
    assert "AudioEngine.cpp" in cmake
    assert "Py_Initialize" in main
    assert "JobSystem::InitializeInline" in main
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
    assert "SampleCount::Four" not in rhi_backend
    assert "INFERNUX_WEBGPU_FULLSCREEN_RHI_READY" in main
    assert "g_sceneRenderer.Render" in main
    assert "INFERNUX_WEB_SCENE_RENDER_READY" in scene_renderer
    assert "ExtractCameraFrame" in scene_renderer
    assert "frame->DrawCalls()" in scene_renderer
    assert "skinBoneMatrices" in scene_renderer
    assert "SetDepthStencilAttachment" not in scene_renderer
    assert "ShaderModuleDesc::FromWgsl" in main
    assert "CreateRenderPipeline" not in main
    assert "CreateRenderPipeline" in rhi_backend
    assert "encoder.Draw(3)" in fullscreen
    assert "InfernuxWebPointerEvent" in main
    assert "emscripten_set_touchstart_callback" not in main
    assert "emscripten_set_mousedown_callback" not in main
    assert "emscripten_set_keydown_callback" in main
    assert "emscripten_set_wheel_callback" in main
    assert "emscripten_set_visibilitychange_callback" in main
    assert "emscripten_sample_gamepad_data" in main
    assert "InfernuxWebTextInput" in main
    assert "infernuxBeginTextInput" in shell
    assert "setPointerCapture" in shell
    assert "lostpointercapture" in shell
    assert "compositionstart" in shell
    assert "compositionend" in shell
    assert "visualViewport" in shell
    assert "safe-area-inset-top" in shell
    assert "InfernuxWebViewportChanged" in main
    assert "InfernuxWebPageLifecycle" in main
    assert "InfernuxWebUserActivation" in main
    assert "INFERNUX_WEB_AUDIO_READY" in main
    assert "pagehide" in shell
    assert "pageshow" in shell
    assert 'install_runtime_service("text-input", web_host)' in bootstrap
    assert '"AudioSource": "audio_source"' in bootstrap
    assert "INFERNUX_WEB_USER_ACTIVATION_REQUIRED" in bootstrap
    assert 'importlib.import_module("Infernux.screen")' in bootstrap
    assert '"begin_text_input"' in host_module
    assert "viewport-fit=cover" in shell
    assert '<link rel="icon" href="data:,">' in shell
    assert "locateFile(path)" in shell
    assert "assetRevision" in shell
    assert "versionedAssets[path]" in shell
    assert "@INFERNUX_WEB_ASSET_REVISION@" in shell
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

def test_web_exporter_reuses_verified_native_zstd_checkout(monkeypatch, tmp_path):
    _web_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_web.exporter")
    checkout = tmp_path / "out" / "build" / "windows" / "_deps" / "infernux_zstd-src"
    (checkout / "build" / "cmake").mkdir(parents=True)
    (checkout / "lib").mkdir()
    (checkout / "build" / "cmake" / "CMakeLists.txt").write_text(
        "project(zstd)\n", encoding="utf-8"
    )
    (checkout / "lib" / "zstd.h").write_text("", encoding="utf-8")

    assert exporter_module._find_local_zstd_source(tmp_path) == checkout.resolve()


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

    first = exporter_module._web_asset_revision(staging, player, runtime)
    (runtime / "main.cpp").write_bytes(b"runtime changed")
    second = exporter_module._web_asset_revision(staging, player, runtime)
    bytecode = player / "python" / "site-packages" / "Infernux" / "__pycache__"
    bytecode.mkdir(parents=True)
    (bytecode / "runtime.cpython-313.opt-1.pyc").write_bytes(b"compiled")
    third = exporter_module._web_asset_revision(staging, player, runtime)

    assert re.fullmatch(r"[0-9a-f]{24}", first)
    assert first != second
    assert second != third
