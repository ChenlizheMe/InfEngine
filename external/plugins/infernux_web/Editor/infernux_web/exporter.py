"""Web target and its staged exporter implementation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from Infernux.engine.build import (
    BuildDiagnostic,
    BuildPlan,
    BuildRequest,
    BuildResult,
    BuildStep,
    BuildTarget,
    CapabilityReport,
    DiagnosticSeverity,
    PlatformCapabilities,
    PlatformExporter,
)

from .doctor import inspect_web_toolchain


_WEB_CAPABILITIES = PlatformCapabilities(
    graphics_api="webgpu",
    threads=False,
    dynamic_loading=False,
    filesystem=True,
    network=False,
    audio=False,
    pointer_input=True,
    text_input=True,
    gamepad_input=True,
    python_native_modules=False,
    numba=False,
    persistent_storage=False,
    features=frozenset(
        {
            "browser-lifecycle",
            "css-pixel-viewport",
            "dom-text-input",
            "multi-pointer",
            "safe-area",
        }
    ),
)


class WebPlatformExporter(PlatformExporter):
    @property
    def exporter_id(self) -> str:
        return "infernux/platform-web"

    def targets(self):
        return (
            BuildTarget(
                "web-wasm32",
                "Web WebAssembly",
                "web",
                "wasm32",
                _WEB_CAPABILITIES,
            ),
        )

    def doctor(self, request: BuildRequest) -> CapabilityReport:
        return inspect_web_toolchain(request.target)

    def create_plan(self, request: BuildRequest) -> BuildPlan:
        return BuildPlan(
            request.target,
            (
                BuildStep("cook", "Cook project content", "cook"),
                BuildStep(
                    "shaders",
                    "Translate portable GLSL shaders to validated WGSL",
                    "compile",
                ),
                BuildStep("imports", "Analyze browser Python imports", "analyze"),
                BuildStep("runtime", "Link CPython 3.13 and Infernux wasm runtime", "compile"),
                BuildStep("webgpu", "Link WebGPU browser host", "compile"),
                BuildStep(
                    "package",
                    "Publish HTML, JavaScript, WebAssembly, and content",
                    "package",
                ),
                BuildStep("audit", "Audit browser package and server contract", "audit"),
            ),
            {
                "architecture": "wasm32",
                "graphics_api": "webgpu",
                "python": "3.13",
            },
        )

    def execute(self, request: BuildRequest, plan: BuildPlan) -> BuildResult:
        started = time.perf_counter()
        report = self.doctor(request)
        if not report.available:
            return BuildResult(
                request.target,
                False,
                diagnostics=report.diagnostics,
                manifest={"toolchain": dict(report.details)},
                elapsed_seconds=time.perf_counter() - started,
            )
        details = dict(report.details)
        staging = _web_staging_directory(request)
        logs: tuple[str, ...] = ()
        try:
            request.report("prepare", 0, 1, "Preparing Web Player staging")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            game_name, player_assets = _cook_web_player_assets(request, staging)
            _stage_engine_python_package(request, player_assets)
            _stage_web_shader_sources(request, staging)
            logs = _configure_and_build_host(request, staging, player_assets, details)
            request.report("prepare", 1, 1, "Cooked Web Player host ready")
        except (OSError, RuntimeError, ValueError) as error:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "web.player.stage-failed",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                manifest={
                    "exporter": self.exporter_id,
                    "abi": "wasm32",
                    "graphics_api": "webgpu",
                    "python": "3.13",
                    "scope": "staging-failed",
                    "staging": str(staging),
                },
                logs=logs,
                elapsed_seconds=time.perf_counter() - started,
            )

        return BuildResult(
            request.target,
            False,
            diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "web.engine.lifecycle-pending",
                        "The cooked content, CPython 3.13 runtime, WebGPU RHI host, "
                        "and supported Infernux native gameplay module are linked, "
                        "but scene/script/render lifecycle activation is not complete.",
                    source=self.exporter_id,
                ),
            ),
            manifest={
                "exporter": self.exporter_id,
                "abi": "wasm32",
                "graphics_api": "webgpu",
                "python": "3.13",
                "scope": "engine-linked-host-ready",
                "game": game_name,
                "staging": str(staging),
                "player_assets": str(player_assets),
            },
            logs=logs,
            elapsed_seconds=time.perf_counter() - started,
        )


def _web_staging_directory(request: BuildRequest) -> Path:
    configured = str(
        request.profile.options.get("build_cache_root", "") or ""
    ).strip()
    if not configured:
        configured = os.environ.get("INFERNUX_BUILD_CACHE_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    elif os.name == "nt":
        root = (
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
            / "Infernux"
            / "BuildCache"
        )
    else:
        root = Path(
            os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
        ) / "infernux"
    project_identity = str(Path(request.project_root).resolve())
    if os.name == "nt":
        project_identity = project_identity.casefold()
    project_key = hashlib.sha256(project_identity.encode("utf-8")).hexdigest()[:16]
    return root / "WebHost" / project_key / str(request.target)


def _cook_web_player_assets(
    request: BuildRequest,
    staging: Path,
) -> tuple[str, Path]:
    from Infernux.engine.platform_content_cook import cook_platform_content

    cook_root = staging / ".infernux-player-cook"
    cook_root.mkdir(parents=True)
    cooked = cook_platform_content(
        request,
        cook_root,
        platform_host={
            "identity": "webgpu-cpython-wasm-player-host",
            "entry_point": "infernux-player.html",
            "platform": "web",
            "architecture": "wasm32",
        },
    )
    player_assets = staging / "player-assets"
    player_assets.mkdir(parents=True)
    shutil.copytree(
        cooked.data_directory,
        player_assets / cooked.data_directory.name,
    )
    return cooked.game_name, player_assets


def _stage_engine_python_package(
    request: BuildRequest,
    player_assets: Path,
) -> None:
    source_root = Path(__file__).resolve().parents[5]
    source_package = source_root / "python" / "Infernux"
    if not (source_package / "engine" / "platform_player_bootstrap.py").is_file():
        raise ValueError(f"Infernux Player Python sources are incomplete: {source_package}")
    site_packages = player_assets / "python" / "site-packages"
    destination = site_packages / "Infernux"
    request.report("analyze", 0, 2, "Staging Infernux Web Player modules")
    shutil.copytree(
        source_package,
        destination,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "*.pyi",
            "*.pyd",
            "*.dll",
            "*.dylib",
            "*.so",
            "*.lib",
            "*.exp",
            "*.obj",
            "_runtime_modules",
            "_runtime_packs",
            "official_packages",
            "player_runtime",
            "project_templates",
            "test",
        ),
    )
    packaging_spec = importlib.util.find_spec("packaging")
    packaging_source = (
        Path(str(packaging_spec.origin)).resolve().parent
        if packaging_spec is not None and packaging_spec.origin
        else None
    )
    if packaging_source is None or not (packaging_source / "__init__.py").is_file():
        raise ValueError("The Web Player staging environment has no packaging module")
    shutil.copytree(
        packaging_source,
        site_packages / "packaging",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyi"),
    )
    request.report("analyze", 2, 2, "Web Player Python modules staged")


def _stage_web_shader_sources(request: BuildRequest, staging: Path) -> None:
    """Generate the shared renderer's GLSL inputs before WGSL translation."""

    from Infernux.lib import _Infernux as native

    source_root = Path(__file__).resolve().parents[5]
    vertex_path = (
        source_root
        / "python"
        / "Infernux"
        / "resources"
        / "shaders"
        / "fullscreen_triangle.vert"
    )
    if not vertex_path.is_file():
        raise ValueError(f"Infernux fullscreen shader is missing: {vertex_path}")
    request.report("shaders", 0, 2, "Preparing shared fullscreen shader")
    generated_vertex = native._prepare_authored_shader_glsl(
        vertex_path.read_text(encoding="utf-8"),
        str(vertex_path),
    )
    generated_fragment = """#version 450
layout(location = 0) in vec2 inUV;
layout(location = 0) out vec4 outColor;
void main() {
    outColor = vec4(inUV.x, inUV.y, 0.20 + 0.12 * inUV.x, 1.0);
}
"""
    shader_root = staging / "shader-cook"
    shader_root.mkdir(parents=True)
    (shader_root / "fullscreen_triangle.vert").write_text(
        generated_vertex,
        encoding="utf-8",
        newline="\n",
    )
    (shader_root / "web_host.frag").write_text(
        generated_fragment,
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "$schema": "infernux.web_shader_cook",
        "version": 1,
        "shaders": [
            {
                "name": "Fullscreen Triangle",
                "stage": "vertex",
                "source": "fullscreen_triangle.vert",
            },
            {
                "name": "Web Host",
                "stage": "fragment",
                "source": "web_host.frag",
            },
        ],
    }
    (shader_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    request.report("shaders", 1, 2, "Shared fullscreen GLSL prepared")


def _configure_and_build_host(
    request: BuildRequest,
    staging: Path,
    player_assets: Path,
    details: dict[str, object],
) -> tuple[str, ...]:
    if os.name != "nt":
        raise RuntimeError(
            "The current Web Player build executor is configured for the WSL2 toolchain"
        )
    distribution = str(details.get("distribution", "Ubuntu-22.04"))
    source_root = Path(__file__).resolve().parents[5]
    runtime_source = Path(__file__).resolve().parents[2] / "Runtime" / "web"
    build_root = staging / "host-build"
    wsl_source_root = _wsl_path(source_root, distribution)
    wsl_runtime_source = _wsl_path(runtime_source, distribution)
    wsl_build_root = _wsl_path(build_root, distribution)
    wsl_player_assets = _wsl_path(player_assets, distribution)
    wsl_shader_manifest = _wsl_path(
        staging / "shader-cook" / "manifest.json", distribution
    )
    wsl_shader_output = _wsl_path(player_assets / "web-shaders", distribution)
    shader_pipeline = (
        Path(__file__).resolve().parent / "shader_pipeline.py"
    )
    wsl_shader_pipeline = _wsl_path(shader_pipeline, distribution)
    emsdk_root = str(details["emsdk_root"])
    cpython_root = str(details["cpython_root"])
    tint_path = str(details["tint_path"])
    asset_revision = _web_asset_revision(staging, player_assets, runtime_source)
    zstd_source = _find_local_zstd_source(source_root)
    zstd_argument = ""
    if zstd_source is not None:
        zstd_argument = (
            " -DINFERNUX_WEB_ZSTD_SOURCE_DIR="
            + shlex.quote(_wsl_path(zstd_source, distribution))
        )
    configuration = (
        "Release"
        if request.profile.configuration.value == "release"
        else "RelWithDebInfo"
    )
    configure = (
        "emcmake cmake "
        f"-S {shlex.quote(wsl_runtime_source)} "
        f"-B {shlex.quote(wsl_build_root)} -G Ninja "
        f"-DCMAKE_BUILD_TYPE={configuration} "
        f"-DINFERNUX_WEB_CPYTHON_SOURCE={shlex.quote(cpython_root)} "
        "-DINFERNUX_WEB_CPYTHON_BUILD="
        f"{shlex.quote(cpython_root + '/builddir/emscripten-browser')} "
        f"-DINFERNUX_ENGINE_SOURCE_ROOT={shlex.quote(wsl_source_root)} "
        f"-DINFERNUX_WEB_PLAYER_ASSETS={shlex.quote(wsl_player_assets)} "
        "-DINFERNUX_WEB_LINK_ENGINE_RUNTIME=ON "
        f"-DINFERNUX_WEB_ASSET_REVISION={asset_revision}"
        f"{zstd_argument}"
    )
    script = "\n".join(
        (
            "set -e",
            'source "$HOME/miniforge3/etc/profile.d/conda.sh"',
            "conda activate infernux",
            f"source {shlex.quote(emsdk_root + '/emsdk_env.sh')} >/dev/null",
            "python "
            f"{shlex.quote(wsl_shader_pipeline)} "
            f"--manifest {shlex.quote(wsl_shader_manifest)} "
            f"--output {shlex.quote(wsl_shader_output)} "
            f"--tint {shlex.quote(tint_path)}",
            configure,
            f"cmake --build {shlex.quote(wsl_build_root)} --parallel 2",
        )
    )
    request.report("compile", 0, 1, "Building cooked WebGPU Player host")
    logs = _run_wsl_build(request, distribution, script)
    request.report("shaders", 2, 2, "Validated WGSL shader catalog ready")
    required = (
        "infernux-player.html",
        "infernux-player.js",
        "infernux-player.wasm",
        "infernux-player.data",
    )
    missing = [name for name in required if not (build_root / name).is_file()]
    if missing:
        raise RuntimeError(
            "Web host build completed without required files: " + ", ".join(missing)
        )
    request.report("compile", 1, 1, "Cooked WebGPU Player host compiled")
    return logs


def _find_local_zstd_source(source_root: Path) -> Path | None:
    """Reuse a verified source checkout from an existing native build."""

    build_root = source_root / "out" / "build"
    if not build_root.is_dir():
        return None
    for candidate in sorted(build_root.glob("*/_deps/infernux_zstd-src")):
        if (
            (candidate / "build" / "cmake" / "CMakeLists.txt").is_file()
            and (candidate / "lib" / "zstd.h").is_file()
        ):
            return candidate.resolve()
    return None


def _web_asset_revision(
    staging: Path,
    player_assets: Path,
    runtime_source: Path,
) -> str:
    """Hash every source that can change the browser's linked asset offsets."""

    digest = hashlib.sha256()
    roots = (
        ("player", player_assets),
        ("runtime", runtime_source),
        ("shaders", staging / "shader-cook"),
    )
    files: list[tuple[str, Path]] = []
    for label, root in roots:
        if not root.is_dir():
            raise RuntimeError(f"Web asset revision input is missing: {root}")
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            files.append((f"{label}/{path.relative_to(root).as_posix()}", path))
    for relative, path in sorted(files, key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _wsl_path(path: Path, distribution: str) -> str:
    del distribution
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").casefold()
    if len(drive) == 1 and drive.isalpha():
        tail = resolved.as_posix()[2:].lstrip("/")
        return f"/mnt/{drive}/{tail}" if tail else f"/mnt/{drive}"
    raise RuntimeError(
        f"The Web WSL2 build requires a local drive path, got: {resolved}"
    )


def _run_wsl_build(
    request: BuildRequest,
    distribution: str,
    script: str,
) -> tuple[str, ...]:
    launcher = shutil.which("wsl") or shutil.which("wsl.exe")
    if not launcher:
        raise RuntimeError("WSL2 is required for the configured Web toolchain")
    process = subprocess.Popen(
        [launcher, "-d", distribution, "--", "bash", "-lc", script],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    logs: list[str] = []
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.replace("\x00", "").rstrip()
                if not line:
                    continue
                logs.append(line)
                request.report("compile", 0, 0, line[:500], source="web-toolchain")
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(
                f"Web host build failed with exit code {return_code}: "
                f"{logs[-1] if logs else 'no diagnostic output'}"
            )
        return tuple(logs)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


__all__ = ["WebPlatformExporter"]
