"""Android targets and the staged exporter implementation."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from Infernux.engine.build import (
    BuildArtifact,
    BuildConfiguration,
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

from .doctor import (
    ANDROID_CMAKE,
    ANDROID_GRADLE_PLUGIN,
    ANDROID_NDK,
    inspect_android_toolchain,
)


_ANDROID_CAPABILITIES = PlatformCapabilities(
    graphics_api="vulkan",
    threads=True,
    dynamic_loading=True,
    filesystem=True,
    network=True,
    audio=True,
    pointer_input=True,
    text_input=True,
    gamepad_input=True,
    python_native_modules=True,
    numba=False,
    persistent_storage=True,
    features=frozenset(
        {
            "android-lifecycle",
            "density-aware-viewport",
            "multi-touch",
            "software-keyboard",
        }
    ),
)

_ANDROID_PYTHON_SERIES = "3.13"
# Bump when the packaged runtime layout or Android asset extraction contract changes.
# The value is part of the on-device cache identity, so APK upgrades cannot silently
# retain a runtime written by an older packaging policy.
_ANDROID_PYTHON_RUNTIME_LAYOUT = 2


class AndroidPlatformExporter(PlatformExporter):
    @property
    def exporter_id(self) -> str:
        return "infernux/platform-android"

    def targets(self):
        return (
            BuildTarget(
                "android-x64-emulator",
                "Android x86_64 Emulator",
                "android",
                "x86_64",
                _ANDROID_CAPABILITIES,
            ),
            BuildTarget(
                "android-arm64",
                "Android arm64",
                "android",
                "arm64-v8a",
                _ANDROID_CAPABILITIES,
            ),
        )

    def doctor(self, request: BuildRequest) -> CapabilityReport:
        return inspect_android_toolchain(request.target)

    def create_plan(self, request: BuildRequest) -> BuildPlan:
        architecture = (
            "x86_64" if request.target == "android-x64-emulator" else "arm64-v8a"
        )
        return BuildPlan(
            request.target,
            (
                BuildStep("cook", "Cook project content", "cook"),
                BuildStep("imports", "Analyze Python imports", "analyze"),
                BuildStep(
                    "native",
                    f"Build Android native runtime ({architecture})",
                    "compile",
                    {"abi": architecture},
                ),
                BuildStep("package", "Assemble Android package", "package"),
                BuildStep("audit", "Audit Android package", "audit"),
            ),
            {"abi": architecture, "graphics_api": "vulkan"},
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
            )
        details = dict(report.details)
        source_root = Path(str(details["source_root"]))
        sdk_root = Path(str(details["sdk_root"]))
        abi = str(plan.metadata["abi"])
        output_root = Path(request.output_dir).resolve()
        staging = _android_staging_directory(request)
        try:
            artifact_kind, configuration_name, source_artifact = (
                _android_artifact_plan(request, staging)
            )
        except ValueError as error:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.artifact.invalid",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                elapsed_seconds=time.perf_counter() - started,
            )
        request.report("prepare", 0, 1, "Preparing Android SDL host project")
        staging.parent.mkdir(parents=True, exist_ok=True)
        _stage_host_template(
            Path(__file__).with_name("templates") / "host",
            staging,
        )
        python_prefix = _python_prefix(request, abi)
        if python_prefix is None:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.python.runtime-missing",
                        "Provide an Android CPython prefix through the build profile option "
                        "android_python_prefix or INFERNUX_ANDROID_PYTHON_PREFIX_X86_64.",
                        source=self.exporter_id,
                        detail={"abi": abi},
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                elapsed_seconds=time.perf_counter() - started,
            )
        try:
            python_version = _stage_python_runtime(request, staging, python_prefix, abi)
        except ValueError as error:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.python.runtime-invalid",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                elapsed_seconds=time.perf_counter() - started,
            )
        try:
            _stage_engine_python_package(request, staging, source_root)
            _finalize_python_runtime_identity(staging)
            game_name, sdl_orientations, android_orientation = (
                _cook_player_content(request, staging, abi)
            )
        except (OSError, RuntimeError, ValueError) as error:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.player.cook-failed",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                elapsed_seconds=time.perf_counter() - started,
            )
        _configure_project(
            staging,
            source_root,
            sdk_root,
            abi,
            python_version=python_version,
            sdl_orientations=sdl_orientations,
            android_orientation=android_orientation,
        )
        request.report("prepare", 1, 1, "Android SDL host project ready")

        try:
            engine_return_code, engine_logs = _build_engine_runtime(
                request,
                staging,
                source_root,
                sdk_root,
                python_version,
                abi,
            )
        except (FileNotFoundError, RuntimeError) as error:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.engine.runtime-incomplete",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                elapsed_seconds=time.perf_counter() - started,
            )
        if engine_return_code != 0:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.engine.cmake",
                        f"Android engine build failed with exit code {engine_return_code}.",
                        source=self.exporter_id,
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                logs=engine_logs,
                elapsed_seconds=time.perf_counter() - started,
            )

        gradle = Path(str(details["gradle"]))
        task_prefix = "bundle" if artifact_kind == "aab" else "assemble"
        task = f":app:{task_prefix}{configuration_name}"
        request.report("compile", 0, 1, f"Running Gradle {task}", abi=abi)
        return_code, gradle_logs = _run_command(
            request,
            [str(gradle), "-p", str(staging), "--console=plain", task],
            staging,
            {
                **os.environ,
                "ANDROID_SDK_ROOT": str(sdk_root),
                "ANDROID_HOME": str(sdk_root),
                "JAVA_HOME": str(details["java_home"]),
            },
            source="gradle",
        )
        logs = engine_logs + gradle_logs
        if return_code != 0:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.host.gradle",
                        f"Gradle failed with exit code {return_code}.",
                        source=self.exporter_id,
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                logs=logs,
                elapsed_seconds=time.perf_counter() - started,
            )
        request.report("compile", 1, 1, "Android SDL host compiled", abi=abi)

        if not source_artifact.is_file():
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.host.artifact-missing",
                        f"Gradle completed without producing {source_artifact.name}.",
                        source=self.exporter_id,
                    ),
                ),
                logs=logs,
                elapsed_seconds=time.perf_counter() - started,
            )
        artifact_stem = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in game_name
        ).strip("_") or "InfernuxPlayer"
        configuration_slug = configuration_name.casefold()
        artifact_path = output_root / (
            f"{artifact_stem}-android-{abi}-{configuration_slug}.{artifact_kind}"
        )
        output_root.mkdir(parents=True, exist_ok=True)
        temporary = artifact_path.with_suffix(f".{artifact_kind}.tmp")
        shutil.copy2(source_artifact, temporary)
        os.replace(temporary, artifact_path)
        payload = artifact_path.read_bytes()
        request.report(
            "package",
            1,
            1,
            f"Android SDL host {artifact_kind.upper()} published",
        )
        return BuildResult(
            request.target,
            True,
            artifacts=(
                BuildArtifact(
                    str(artifact_path),
                    artifact_kind,
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                ),
            ),
            manifest={
                "exporter": self.exporter_id,
                "contract_version": self.contract_version,
                "abi": abi,
                "graphics_api": "vulkan",
                "scope": "cooked-player",
                "python": python_version,
                "game": game_name,
                "configuration": configuration_slug,
                "artifact_kind": artifact_kind,
            },
            logs=logs,
            elapsed_seconds=time.perf_counter() - started,
        )


def _android_artifact_plan(
    request: BuildRequest,
    staging: Path,
) -> tuple[str, str, Path]:
    """Select the Gradle publication contract for one build request."""

    configured = str(
        request.profile.options.get("android_artifact", "") or ""
    ).strip().casefold()
    if not configured:
        configured = (
            "apk"
            if request.profile.configuration is BuildConfiguration.DEVELOPMENT
            else "aab"
        )
    if configured not in {"apk", "aab"}:
        raise ValueError("android_artifact must be apk or aab")

    configuration_name = (
        "Debug"
        if request.profile.configuration is BuildConfiguration.DEVELOPMENT
        else "Release"
    )
    configuration_slug = configuration_name.casefold()
    if configured == "aab":
        source = (
            staging
            / "app"
            / "build"
            / "outputs"
            / "bundle"
            / configuration_slug
            / f"app-{configuration_slug}.aab"
        )
    else:
        apk_name = (
            "app-debug.apk"
            if configuration_name == "Debug"
            else "app-release-unsigned.apk"
        )
        source = (
            staging
            / "app"
            / "build"
            / "outputs"
            / "apk"
            / configuration_slug
            / apk_name
        )
    return configured, configuration_name, source


def _configure_project(
    project_root: Path,
    source_root: Path,
    sdk_root: Path,
    abi: str,
    *,
    python_version: str = _ANDROID_PYTHON_SERIES,
    sdl_orientations: str = "LandscapeLeft LandscapeRight",
    android_orientation: str = "landscape",
) -> None:
    replacements = {
        "@INFERNUX_SOURCE_ROOT@": source_root.as_posix(),
        "@ANDROID_ABI@": abi,
        "@ANDROID_NDK_VERSION@": ANDROID_NDK,
        "@ANDROID_CMAKE_VERSION@": ANDROID_CMAKE,
        "@ANDROID_GRADLE_PLUGIN_VERSION@": ANDROID_GRADLE_PLUGIN,
        "@ANDROID_PYTHON_VERSION@": python_version,
        "@ANDROID_ORIENTATIONS@": sdl_orientations,
        "@ANDROID_SCREEN_ORIENTATION@": android_orientation,
    }
    for path in project_root.rglob("*.in"):
        payload = path.read_text(encoding="utf-8")
        for marker, value in replacements.items():
            payload = payload.replace(marker, value)
        destination = path.with_suffix("")
        destination.write_text(payload, encoding="utf-8", newline="\n")
        path.unlink()
    escaped_sdk = str(sdk_root).replace("\\", "\\\\").replace(":", "\\:")
    (project_root / "local.properties").write_text(
        f"sdk.dir={escaped_sdk}\n",
        encoding="utf-8",
        newline="\n",
    )


def _stage_engine_python_package(
    request: BuildRequest,
    staging: Path,
    source_root: Path,
) -> None:
    """Stage the shared Player Python runtime behind the Android native host."""

    source_package = source_root / "python" / "Infernux"
    if not (source_package / "engine" / "platform_player_bootstrap.py").is_file():
        raise ValueError(f"Infernux Player Python sources are incomplete: {source_package}")
    site_packages = staging / "app" / "src" / "main" / "assets" / "python" / "site-packages"
    destination = site_packages / "Infernux"
    request.report("analyze", 0, 2, "Staging Infernux Android Player modules")
    shutil.rmtree(destination, ignore_errors=True)
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
        raise ValueError("The Android Player staging environment has no packaging module")
    packaging_destination = site_packages / "packaging"
    shutil.rmtree(packaging_destination, ignore_errors=True)
    shutil.copytree(
        packaging_source,
        packaging_destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyi"),
    )
    request.report("analyze", 2, 2, "Android Player Python modules staged")


def _stage_host_template(source: Path, staging: Path) -> None:
    """Copy build templates without project AssetDatabase sidecars.

    Installed plugins live under ``Packages`` and therefore receive ``.meta``
    identity files.  Those sidecars are meaningful to Infernux, but Android's
    resource merger treats every file below ``res`` as a platform resource.
    Ignore new sidecars and prune stale copies left in the reusable host cache.
    """

    shutil.copytree(
        source,
        staging,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.meta"),
    )
    for sidecar in staging.rglob("*.meta"):
        sidecar.unlink()


def _cook_player_content(
    request: BuildRequest,
    staging: Path,
    abi: str,
) -> tuple[str, str, str]:
    """Run the shared GUID-based Player cook and stage its native package."""

    from Infernux.engine.build_settings import load_build_settings
    from Infernux.engine.platform_content_cook import cook_platform_content

    settings = load_build_settings(request.project_root)
    sdl_orientations, android_orientation = _android_orientation_contract(
        request,
        settings,
    )
    cook_root = staging / ".infernux-player-cook"
    shutil.rmtree(cook_root, ignore_errors=True)
    cook_root.mkdir(parents=True)
    try:
        cooked = cook_platform_content(
            request,
            cook_root,
            platform_host={
                "identity": "android-sdl-python-player-host",
                "entry_point": "com.infernux.bootstrap/.InfernuxActivity",
                "platform": "android",
                "architecture": abi,
            },
        )
        cooked_data = cooked.data_directory
        player_assets = staging / "app" / "src" / "main" / "assets" / "player"
        shutil.rmtree(player_assets, ignore_errors=True)
        player_assets.mkdir(parents=True)
        shutil.copytree(cooked_data, player_assets / cooked_data.name)

        digest = hashlib.sha256(b"INFERNUX_ANDROID_PLAYER_ASSETS_V1\n")
        for path in sorted(
            (item for item in player_assets.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix().casefold(),
        ):
            relative = path.relative_to(player_assets).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        (player_assets / "infernux-content.id").write_text(
            digest.hexdigest() + "\n",
            encoding="ascii",
            newline="\n",
        )
    finally:
        shutil.rmtree(cook_root, ignore_errors=True)
    return cooked.game_name, sdl_orientations, android_orientation


def _android_orientation_contract(
    request: BuildRequest,
    settings: dict[str, object],
) -> tuple[str, str]:
    """Resolve one SDL/Activity orientation policy for this Android build."""

    configured = str(
        request.profile.options.get("android_orientation", "auto") or "auto"
    ).strip().casefold()
    if configured == "auto":
        width = int(settings.get("window_width", 1280))
        height = int(settings.get("window_height", 720))
        configured = "landscape" if width >= height else "portrait"
    policies = {
        "landscape": ("LandscapeLeft LandscapeRight", "landscape"),
        "portrait": ("Portrait PortraitUpsideDown", "portrait"),
        "sensor": (
            "LandscapeLeft LandscapeRight Portrait PortraitUpsideDown",
            "fullUser",
        ),
    }
    try:
        return policies[configured]
    except KeyError as error:
        raise ValueError(
            "android_orientation must be auto, landscape, portrait, or sensor"
        ) from error


def _python_prefix(request: BuildRequest, abi: str) -> Path | None:
    configured = str(request.profile.options.get("android_python_prefix", "") or "").strip()
    if not configured:
        suffix = "X86_64" if abi == "x86_64" else "ARM64"
        configured = os.environ.get(
            f"INFERNUX_ANDROID_PYTHON_PREFIX_{suffix}", ""
        ).strip()
    if not configured:
        return None
    prefix = Path(configured).expanduser().resolve()
    return prefix if prefix.is_dir() else None


def _android_staging_directory(request: BuildRequest) -> Path:
    configured = str(request.profile.options.get("build_cache_root", "") or "").strip()
    if not configured:
        configured = os.environ.get("INFERNUX_BUILD_CACHE_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    elif os.name == "nt":
        root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Infernux" / "BuildCache"
    else:
        root = Path(
            os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
        ) / "infernux"
    project_identity = str(Path(request.project_root).resolve())
    if os.name == "nt":
        project_identity = project_identity.casefold()
    project_key = hashlib.sha256(project_identity.encode("utf-8")).hexdigest()[:16]
    return root / "AndroidHost" / project_key / str(request.target)


def _stage_python_runtime(
    request: BuildRequest,
    staging: Path,
    prefix: Path,
    abi: str,
) -> str:
    include_roots = sorted((prefix / "include").glob("python*"))
    library_roots = sorted(
        path
        for path in (prefix / "lib").glob("python*")
        if path.is_dir() and (path / "encodings").is_dir()
    )
    if not include_roots or not library_roots:
        raise ValueError(f"Android Python prefix is incomplete: {prefix}")
    version = library_roots[0].name.removeprefix("python")
    if version != _ANDROID_PYTHON_SERIES:
        raise ValueError(
            "Android Player requires CPython "
            f"{_ANDROID_PYTHON_SERIES}, but the configured prefix provides {version}: "
            f"{prefix}"
        )
    include_root = prefix / "include" / f"python{version}"
    library_root = prefix / "lib" / f"python{version}"
    if not (include_root / "Python.h").is_file() or not library_root.is_dir():
        raise ValueError(f"Android Python {version} prefix is inconsistent: {prefix}")
    python_library = prefix / "lib" / f"libpython{version}.so"
    if not python_library.is_file():
        raise ValueError(f"Android Python {version} shared library is missing: {prefix}")

    staged_include = staging / "app" / "src" / "main" / "python" / "include" / f"python{version}"
    staged_python = staging / "app" / "src" / "main" / "assets" / "python"
    staged_library = staged_python / "lib" / f"python{version}"
    native_library = staging / "app" / "src" / "main" / "jniLibs" / abi
    request.report("python-runtime", 0, 4, f"Staging Android Python {version} headers")
    shutil.rmtree(staged_include.parent.parent, ignore_errors=True)
    shutil.copytree(include_root, staged_include)
    request.report("python-runtime", 1, 4, f"Staging Android Python {version} standard library")
    shutil.rmtree(staged_python, ignore_errors=True)
    shutil.copytree(
        library_root,
        staged_library,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "test",
            "idlelib",
            "tkinter",
            "turtledemo",
            "ensurepip",
            f"config-{version}-*",
        ),
    )
    request.report("python-runtime", 2, 4, f"Staging Android Python {version} native libraries")
    native_library.mkdir(parents=True, exist_ok=True)
    for stale_library in (*native_library.glob("libpython*.so"), *native_library.glob("lib*_python.so")):
        stale_library.unlink()
    required_sidecars = _required_python_sidecar_libraries(library_root)
    missing_sidecars = [
        name for name in required_sidecars if not (prefix / "lib" / name).is_file()
    ]
    if missing_sidecars:
        raise ValueError(
            f"Android Python {version} prefix is missing native dependencies "
            f"required by its extension modules: {', '.join(missing_sidecars)}"
        )
    runtime_libraries = (
        python_library,
        *sorted((prefix / "lib").glob("lib*_python.so")),
    )
    for library in runtime_libraries:
        shutil.copy2(library, native_library / library.name)

    request.report("python-runtime", 3, 4, "Staging Android NumPy runtime")
    numpy_wheel = _find_android_numpy_wheel(request, prefix, abi, version)
    site_packages = staged_python / "site-packages"
    _extract_wheel(numpy_wheel, site_packages)

    runtime_identity = _python_runtime_identity(
        prefix,
        library_root,
        (*runtime_libraries, numpy_wheel),
        version,
        abi,
    )
    (staged_python / "infernux-runtime.id").write_text(
        runtime_identity + "\n",
        encoding="utf-8",
        newline="\n",
    )

    (staging / "app" / "src" / "main" / "python-runtime.properties").write_text(
        f"version={version}\nabi={abi}\n",
        encoding="utf-8",
        newline="\n",
    )
    request.report("python-runtime", 4, 4, f"Android Python {version} runtime staged")
    return version


def _required_python_sidecar_libraries(library_root: Path) -> tuple[str, ...]:
    """Find Android sidecar libraries named by CPython extension binaries."""
    dependency_pattern = re.compile(rb"lib[A-Za-z0-9_.+-]+_python\.so")
    dependencies: set[str] = set()
    for extension in library_root.rglob("*.so"):
        try:
            payload = extension.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"Unable to inspect Android Python extension dependencies: {extension}"
            ) from exc
        dependencies.update(
            match.decode("ascii") for match in dependency_pattern.findall(payload)
        )
    return tuple(sorted(dependencies))


def _find_android_numpy_wheel(
    request: BuildRequest,
    prefix: Path,
    abi: str,
    python_version: str,
) -> Path:
    configured = str(
        request.profile.options.get("android_numpy_wheel", "") or ""
    ).strip()
    if not configured:
        suffix = "X86_64" if abi == "x86_64" else "ARM64"
        configured = os.environ.get(
            f"INFERNUX_ANDROID_NUMPY_WHEEL_{suffix}", ""
        ).strip()

    candidates = (
        [Path(configured).expanduser().resolve()]
        if configured
        else sorted((prefix / "wheels").glob("numpy-*.whl"))
    )
    expected_interpreter = "cp" + python_version.replace(".", "")
    expected_architecture = "x86_64" if abi == "x86_64" else "arm64_v8a"
    compatible: list[tuple[object, Path]] = []
    for wheel in candidates:
        if not wheel.is_file():
            continue
        try:
            distribution, package_version, _build, tags = parse_wheel_filename(
                wheel.name
            )
        except InvalidWheelFilename:
            continue
        if distribution != "numpy":
            continue
        if any(
            tag.interpreter == expected_interpreter
            and tag.abi == expected_interpreter
            and tag.platform.startswith("android_")
            and tag.platform.endswith("_" + expected_architecture)
            for tag in tags
        ):
            compatible.append((package_version, wheel))
    if not compatible:
        raise ValueError(
            "Android Player requires a NumPy wheel matching "
            f"{expected_interpreter} and {abi}. Place it in {prefix / 'wheels'} "
            "or configure android_numpy_wheel."
        )
    return max(compatible, key=lambda item: item[0])[1]


def _extract_wheel(wheel: Path, destination: Path) -> None:
    """Extract one validated wheel without permitting links or path traversal."""

    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    try:
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                normalized = entry.filename.replace("\\", "/")
                if entry.is_dir():
                    normalized = normalized.rstrip("/")
                parts = normalized.split("/")
                if (
                    not normalized
                    or normalized.startswith("/")
                    or any(part in {"", ".", ".."} for part in parts)
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise ValueError(
                        f"Android Python wheel contains an unsafe entry: {entry.filename}"
                    )
                target = destination.joinpath(*parts).resolve()
                if not target.is_relative_to(destination_root):
                    raise ValueError(
                        f"Android Python wheel escapes site-packages: {entry.filename}"
                    )
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"Android Python wheel is unreadable: {wheel}") from error


def _build_engine_runtime(
    request: BuildRequest,
    staging: Path,
    source_root: Path,
    sdk_root: Path,
    python_version: str,
    abi: str,
) -> tuple[int, tuple[str, ...]]:
    """Cross-build the native Python module and stage its Android dependencies."""

    executable_suffix = ".exe" if os.name == "nt" else ""
    cmake = sdk_root / "cmake" / ANDROID_CMAKE / "bin" / f"cmake{executable_suffix}"
    ninja = sdk_root / "cmake" / ANDROID_CMAKE / "bin" / f"ninja{executable_suffix}"
    ndk_root = sdk_root / "ndk" / ANDROID_NDK
    python_include = (
        staging
        / "app"
        / "src"
        / "main"
        / "python"
        / "include"
        / f"python{python_version}"
    )
    python_library = (
        staging
        / "app"
        / "src"
        / "main"
        / "jniLibs"
        / abi
        / f"libpython{python_version}.so"
    )
    pybind11_dir = _host_pybind11_cmake_dir()
    build_type = (
        "Release"
        if request.profile.configuration is BuildConfiguration.RELEASE
        else "Debug"
    )
    build_root = staging.parents[2] / "AndroidEngine" / abi / build_type
    configure_command = [
        str(cmake),
        "-S",
        str(source_root),
        "-B",
        str(build_root),
        "-G",
        "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={ninja}",
        f"-DCMAKE_TOOLCHAIN_FILE={ndk_root / 'build' / 'cmake' / 'android.toolchain.cmake'}",
        f"-DANDROID_ABI={abi}",
        "-DANDROID_PLATFORM=android-26",
        f"-DCMAKE_BUILD_TYPE={build_type}",
        f"-DINFERNUX_HOST_PYTHON_EXECUTABLE={sys.executable}",
        f"-DPython3_EXECUTABLE={sys.executable}",
        f"-Dpybind11_DIR={pybind11_dir}",
        "-DINFERNUX_USE_TARGET_PYTHON=ON",
        f"-DINFERNUX_TARGET_PYTHON_INCLUDE_DIR={python_include}",
        f"-DINFERNUX_TARGET_PYTHON_LIBRARY={python_library}",
        f"-DINFERNUX_PYTHON_SYNC_DIR={build_root / 'python-sync'}",
        "-DINFERNUX_BUILD_PLAYER_HOST=OFF",
        "-DINFERNUX_BUILD_TESTS=OFF",
        "-DINFERNUX_RELEASE_LTO=OFF",
        "-DINFERNUX_ENABLE_VULKAN_VALIDATION=OFF",
    ]
    for option_name, environment_name, cmake_name in (
        (
            "zstd_source_dir",
            "INFERNUX_ZSTD_SOURCE_DIR",
            "INFERNUX_ZSTD_SOURCE_DIR",
        ),
        (
            "spirv_cross_source_dir",
            "INFERNUX_SPIRV_CROSS_SOURCE_DIR",
            "INFERNUX_SPIRV_CROSS_SOURCE_DIR",
        ),
        (
            "volk_source_dir",
            "INFERNUX_VOLK_SOURCE_DIR",
            "INFERNUX_VOLK_SOURCE_DIR",
        ),
    ):
        source_directory = str(
            request.profile.options.get(option_name, "")
            or os.environ.get(environment_name, "")
        ).strip()
        if source_directory:
            configure_command.append(
                f"-D{cmake_name}={Path(source_directory).expanduser().resolve()}"
            )
    environment = {
        **os.environ,
        "ANDROID_SDK_ROOT": str(sdk_root),
        "ANDROID_HOME": str(sdk_root),
    }
    request.report("native", 0, 2, f"Configuring Android engine ({abi})")
    return_code, configure_logs = _run_command(
        request,
        configure_command,
        source_root,
        environment,
        source="cmake",
    )
    if return_code != 0:
        return return_code, configure_logs

    request.report("native", 1, 2, f"Building Android engine ({abi})")
    return_code, build_logs = _run_command(
        request,
        [str(cmake), "--build", str(build_root), "--target", "_Infernux"],
        source_root,
        environment,
        source="cmake",
    )
    logs = configure_logs + build_logs
    if return_code != 0:
        return return_code, logs

    _stage_engine_native_libraries(build_root, staging, abi)
    request.report("native", 2, 2, f"Android engine runtime staged ({abi})")
    return 0, logs


def _host_pybind11_cmake_dir() -> Path:
    try:
        import pybind11
    except ImportError as error:
        raise RuntimeError(
            "The host Python environment must provide pybind11 to build Android."
        ) from error
    return Path(pybind11.get_cmake_dir()).resolve()


def _stage_engine_native_libraries(
    build_root: Path,
    staging: Path,
    abi: str,
) -> tuple[Path, ...]:
    native_root = staging / "app" / "src" / "main" / "jniLibs" / abi
    native_root.mkdir(parents=True, exist_ok=True)
    engine_names = {
        "_Infernux.so",
        "_InfernuxBootstrap.so",
        "libassimp.so",
        "libJolt.so",
    }
    for path in (build_root / "python-sync").glob("*.so"):
        engine_names.add(path.name)
    for stale in native_root.glob("*.so"):
        if stale.name in engine_names or stale.name.startswith("libInfernux"):
            stale.unlink()

    candidates: dict[str, Path] = {}
    for path in (build_root / "python-sync").glob("*.so"):
        candidates[path.name] = path
    for name in ("libassimp.so", "libJolt.so"):
        matches = tuple(build_root.rglob(name))
        if matches:
            candidates[name] = matches[0]
    required = {"_Infernux.so", "libassimp.so", "libJolt.so"}
    missing = sorted(required.difference(candidates))
    if missing:
        raise FileNotFoundError(
            "Android engine build did not produce required libraries: "
            + ", ".join(missing)
        )
    staged: list[Path] = []
    for name, source in sorted(candidates.items()):
        destination = native_root / name
        shutil.copy2(source, destination)
        staged.append(destination)
    return tuple(staged)


def _python_runtime_identity(
    prefix: Path,
    library_root: Path,
    runtime_libraries: tuple[Path, ...],
    version: str,
    abi: str,
) -> str:
    """Fingerprint the packaged runtime without rereading hundreds of MB of payload."""

    digest = hashlib.sha256(
        (
            f"layout={_ANDROID_PYTHON_RUNTIME_LAYOUT}\n"
            f"python={version}\n"
            f"abi={abi}\n"
        ).encode("utf-8")
    )
    paths = [path for path in library_root.rglob("*") if path.is_file()]
    paths.extend(runtime_libraries)
    for path in sorted(paths, key=lambda item: item.as_posix().casefold()):
        stat = path.stat()
        try:
            relative = path.relative_to(prefix).as_posix()
        except ValueError:
            relative = path.name
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def _finalize_python_runtime_identity(staging: Path) -> str:
    """Include staged engine modules in the Android Python cache identity."""

    staged_python = staging / "app" / "src" / "main" / "assets" / "python"
    identity_path = staged_python / "infernux-runtime.id"
    if not identity_path.is_file():
        raise ValueError("Android Python runtime identity is missing before finalization")

    digest = hashlib.sha256(b"INFERNUX_ANDROID_PYTHON_ASSETS_V1\n")
    digest.update(identity_path.read_bytes().strip() + b"\n")
    site_packages = staged_python / "site-packages"
    for package_name in ("Infernux", "packaging"):
        package_root = site_packages / package_name
        if not package_root.is_dir():
            raise ValueError(
                f"Android Player Python package is missing before finalization: {package_root}"
            )
        for path in sorted(
            (item for item in package_root.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix().casefold(),
        ):
            relative = path.relative_to(site_packages).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)

    identity = digest.hexdigest()
    identity_path.write_text(identity + "\n", encoding="ascii", newline="\n")
    return identity


def _run_command(
    request: BuildRequest,
    command: list[str],
    working_directory: Path,
    environment: dict[str, str],
    *,
    source: str,
) -> tuple[int, tuple[str, ...]]:
    """Run one build tool with live, cancellable output for every frontend."""

    process = subprocess.Popen(
        command,
        cwd=str(working_directory),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    logs: list[str] = []
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip()
                if not line:
                    continue
                logs.append(line)
                request.report(
                    "compile",
                    0,
                    0,
                    line[:500],
                    source=source,
                )
        return process.wait(), tuple(logs)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


__all__ = ["AndroidPlatformExporter"]
