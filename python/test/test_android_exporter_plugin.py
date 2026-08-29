from __future__ import annotations

import importlib
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from Infernux.engine.build import (
    BuildConfiguration,
    BuildExporterRegistry,
    BuildProfile,
    BuildRequest,
)


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_EDITOR = ROOT / "external" / "plugins" / "infernux_android" / "Editor"


def _write_android_numpy_wheel(prefix: Path, *, abi: str = "x86_64") -> Path:
    architecture = "x86_64" if abi == "x86_64" else "arm64_v8a"
    wheel = (
        prefix
        / "wheels"
        / f"numpy-2.5.2-cp313-cp313-android_26_{architecture}.whl"
    )
    wheel.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("numpy/__init__.py", "__version__ = '2.5.2'\n")
        archive.writestr(
            "numpy-2.5.2.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            "Root-Is-Purelib: false\n"
            f"Tag: cp313-cp313-android_26_{architecture}\n",
        )
    return wheel


def _android_module(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN_EDITOR))
    for name in tuple(sys.modules):
        if name == "infernux_android" or name.startswith("infernux_android."):
            sys.modules.pop(name)
    return importlib.import_module("infernux_android")


def _toolchain(tmp_path: Path) -> dict[str, str]:
    sdk = tmp_path / "sdk"
    java = tmp_path / "jdk"
    avd = tmp_path / "avd"
    for directory in (
        sdk / "platforms" / "android-36",
        sdk / "build-tools" / "36.0.0",
        sdk / "cmake" / "3.30.5",
        sdk / "ndk" / "29.0.14206865" / "build" / "cmake",
        sdk / "platform-tools",
        sdk / "emulator",
        java / "bin",
        avd,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    for path in (
        sdk / "platform-tools" / f"adb{suffix}",
        sdk / "emulator" / f"emulator{suffix}",
        java / "bin" / f"java{suffix}",
        sdk
        / "ndk"
        / "29.0.14206865"
        / "build"
        / "cmake"
        / "android.toolchain.cmake",
    ):
        path.write_text("fixture\n", encoding="utf-8")
    (java / "release").write_text('JAVA_VERSION="17.0.20"\n', encoding="utf-8")
    (avd / "Infernux_API_36.ini").write_text("target=android-36\n", encoding="utf-8")
    source = tmp_path / "source"
    (source / "external" / "SDL" / "android-project").mkdir(parents=True)
    (source / "CMakeLists.txt").write_text("project(Infernux)\n", encoding="utf-8")
    (source / "external" / "SDL" / "CMakeLists.txt").write_text(
        "project(SDL)\n", encoding="utf-8"
    )
    wrapper = source / "external" / "SDL" / "android-project" / (
        "gradlew.bat" if sys.platform == "win32" else "gradlew"
    )
    wrapper.write_text("fixture\n", encoding="utf-8")
    return {
        "ANDROID_SDK_ROOT": str(sdk),
        "ANDROID_AVD_HOME": str(avd),
        "JAVA_HOME": str(java),
        "INFERNUX_SOURCE_ROOT": str(source),
    }


def test_android_exporter_contributes_only_vulkan_targets(monkeypatch):
    module = _android_module(monkeypatch)
    targets = module.AndroidPlatformExporter().targets()

    assert [target.id for target in targets] == [
        "android-x64-emulator",
        "android-arm64",
    ]
    assert {target.capabilities.graphics_api for target in targets} == {"vulkan"}
    assert all(not target.capabilities.numba for target in targets)


def test_android_doctor_accepts_the_pinned_local_toolchain(monkeypatch, tmp_path):
    module = _android_module(monkeypatch)
    report = module.inspect_android_toolchain(
        "android-x64-emulator",
        _toolchain(tmp_path),
    )

    assert report.available
    assert report.diagnostics == ()
    assert report.details["avds"] == ["Infernux_API_36"]


def test_android_doctor_reports_every_missing_root(monkeypatch):
    module = _android_module(monkeypatch)
    report = module.inspect_android_toolchain("android-arm64", {})

    assert not report.available
    assert {item.code for item in report.diagnostics} == {
        "android.sdk.environment",
        "android.jdk.environment",
    }


def test_android_exporter_plan_is_inspectable(monkeypatch, tmp_path):
    module = _android_module(monkeypatch)
    exporter = module.AndroidPlatformExporter()
    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(),
    )
    plan = exporter.create_plan(request)
    assert [step.id for step in plan.steps] == [
        "cook",
        "imports",
        "native",
        "package",
        "audit",
    ]
    assert plan.metadata["graphics_api"] == "vulkan"


def test_android_host_template_disables_opengl_and_configures_vulkan(
    monkeypatch, tmp_path
):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    templates = PLUGIN_EDITOR / "infernux_android" / "templates" / "host"
    project = tmp_path / "generated"
    shutil.copytree(templates, project)

    exporter_module._configure_project(
        project,
        ROOT,
        tmp_path / "sdk",
        "x86_64",
    )

    cmake = (project / "app/src/main/cpp/CMakeLists.txt").read_text(
        encoding="utf-8"
    )
    manifest = (project / "app/src/main/AndroidManifest.xml").read_text(
        encoding="utf-8"
    )
    gradle = (project / "app/build.gradle").read_text(encoding="utf-8")
    root_gradle = (project / "build.gradle").read_text(encoding="utf-8")
    host_source = (project / "app/src/main/cpp/main.cpp").read_text(encoding="utf-8")
    activity = (
        project
        / "app/src/main/java/com/infernux/bootstrap/InfernuxActivity.java"
    ).read_text(encoding="utf-8")
    assert "SDL_OPENGL OFF" in cmake
    assert "SDL_OPENGLES OFF" in cmake
    assert "SDL_VULKAN ON" in cmake
    assert 'INFERNUX_ANDROID_ORIENTATIONS="LandscapeLeft LandscapeRight"' in cmake
    assert '"${CMAKE_CURRENT_SOURCE_DIR}/../python/include/' in cmake
    assert '"${CMAKE_CURRENT_SOURCE_DIR}/../jniLibs/' in cmake
    assert "android.hardware.vulkan.version" in manifest
    assert 'android:screenOrientation="sensorLandscape"' in manifest
    assert "glEsVersion" not in manifest
    assert "if (mScreenKeyboardShown)" in activity
    assert "registerOnBackInvokedCallback" in activity
    assert "OnBackInvokedDispatcher.PRIORITY_DEFAULT" in activity
    assert "this::dispatchInfernuxBack" in activity
    assert "sendCommand(COMMAND_TEXTEDIT_HIDE, null)" in activity
    assert "onNativeKeyDown(KeyEvent.KEYCODE_BACK)" in activity
    assert "onNativeKeyUp(KeyEvent.KEYCODE_BACK)" in activity
    assert "super.onBackPressed()" not in activity
    assert 'abiFilters "x86_64"' in gradle
    assert 'ignoreAssetsPattern = "!.svn:!.git:!.ds_store:' in gradle
    assert 'version "8.10.1"' in root_gradle
    assert "run_platform_player" in host_source
    assert "SDL_SetHint(SDL_HINT_ORIENTATIONS" in host_source
    assert "SDL_CreateWindow" not in host_source
    assert 'SDL_setenv_unsafe("INFERNUX_NATIVE_MODULE_DIR"' in host_source
    assert "INFERNUX_PLAYER_ASSET_ROOT" in activity
    assert "infernux-content.id" in activity
    assert 'identityName + ".complete"' in activity
    assert 'assetRoot + ".installing"' in activity
    assert "stagedRoot.renameTo(installedRoot)" in activity
    assert "stream.getFD().sync()" in activity
    assert not list(project.rglob("*.in"))
    assert "@INFERNUX_" not in cmake + gradle + root_gradle
    assert "@ANDROID_" not in cmake + gradle + root_gradle


def test_android_host_template_excludes_asset_database_sidecars(
    monkeypatch, tmp_path
):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    source = tmp_path / "installed-plugin-template"
    values = source / "app/src/main/res/values"
    values.mkdir(parents=True)
    (values / "strings.xml").write_text("<resources />\n", encoding="utf-8")
    (values / "strings.xml.meta").write_text("{}\n", encoding="utf-8")
    staging = tmp_path / "host-cache"
    stale = staging / "app/src/main/res/values/styles.xml.meta"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}\n", encoding="utf-8")

    exporter_module._stage_host_template(source, staging)

    assert (staging / "app/src/main/res/values/strings.xml").is_file()
    assert not list(staging.rglob("*.meta"))


@pytest.mark.parametrize(
    ("option", "width", "height", "expected"),
    (
        (
            "auto",
            1920,
            1080,
            ("LandscapeLeft LandscapeRight", "sensorLandscape"),
        ),
        ("auto", 720, 1280, ("Portrait PortraitUpsideDown", "sensorPortrait")),
        (
            "sensor",
            1280,
            720,
            (
                "LandscapeLeft LandscapeRight Portrait PortraitUpsideDown",
                "fullUser",
            ),
        ),
    ),
)
def test_android_orientation_contract(monkeypatch, tmp_path, option, width, height, expected):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(options={"android_orientation": option}),
    )

    assert exporter_module._android_orientation_contract(
        request,
        {"window_width": width, "window_height": height},
    ) == expected


def test_android_orientation_contract_rejects_unknown_policy(monkeypatch, tmp_path):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(options={"android_orientation": "diagonal"}),
    )

    with pytest.raises(ValueError, match="android_orientation"):
        exporter_module._android_orientation_contract(request, {})


@pytest.mark.parametrize(
    ("configuration", "option", "kind", "variant", "relative"),
    (
        (
            BuildConfiguration.DEVELOPMENT,
            "",
            "apk",
            "Debug",
            "app/build/outputs/apk/debug/app-debug.apk",
        ),
        (
            BuildConfiguration.RELEASE,
            "",
            "aab",
            "Release",
            "app/build/outputs/bundle/release/app-release.aab",
        ),
        (
            BuildConfiguration.RELEASE,
            "apk",
            "apk",
            "Release",
            "app/build/outputs/apk/release/app-release-unsigned.apk",
        ),
    ),
)
def test_android_artifact_plan(
    monkeypatch,
    tmp_path,
    configuration,
    option,
    kind,
    variant,
    relative,
):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    options = {"android_artifact": option} if option else {}
    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(configuration=configuration, options=options),
    )
    staging = tmp_path / "staging"

    actual_kind, actual_variant, source = exporter_module._android_artifact_plan(
        request,
        staging,
    )

    assert (actual_kind, actual_variant) == (kind, variant)
    assert source == staging / Path(relative)


def test_android_python_runtime_staging_is_exact_and_versioned(monkeypatch, tmp_path):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    prefix = tmp_path / "python-prefix"
    include = prefix / "include" / "python3.13"
    stdlib = prefix / "lib" / "python3.13"
    include.mkdir(parents=True)
    (stdlib / "encodings").mkdir(parents=True)
    (stdlib / "__pycache__").mkdir()
    (include / "Python.h").write_text("fixture header\n", encoding="utf-8")
    (stdlib / "encodings" / "__init__.py").write_text("fixture\n", encoding="utf-8")
    (stdlib / "removed.py").write_text("removed later\n", encoding="utf-8")
    (stdlib / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
    (prefix / "lib" / "libpython3.13.so").write_bytes(b"python")
    (prefix / "lib" / "libssl_python.so").write_bytes(b"ssl")
    _write_android_numpy_wheel(prefix)

    staging = tmp_path / "staging"
    stale_include = staging / "app/src/main/python/include/python3.13"
    stale_include.mkdir(parents=True)
    (stale_include / "Python.h").write_text("stale\n", encoding="utf-8")
    stale_assets = staging / "app/src/main/assets/python"
    stale_assets.mkdir(parents=True)
    (stale_assets / "stale.py").write_text("stale\n", encoding="utf-8")
    stale_native = staging / "app/src/main/jniLibs/x86_64"
    stale_native.mkdir(parents=True)
    (stale_native / "libpython3.13.so").write_bytes(b"stale")
    (stale_native / "libengine.so").write_bytes(b"keep")

    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(),
    )
    version = exporter_module._stage_python_runtime(
        request, staging, prefix, "x86_64"
    )

    runtime_id = stale_assets / "infernux-runtime.id"
    first_identity = runtime_id.read_text(encoding="utf-8")
    assert version == "3.13"
    assert (stale_include / "Python.h").read_text(encoding="utf-8") == "fixture header\n"
    assert not (stale_assets / "stale.py").exists()
    assert not (stale_assets / "lib/python3.13/__pycache__").exists()
    assert (stale_native / "libpython3.13.so").is_file()
    assert (stale_native / "libssl_python.so").is_file()
    assert (stale_native / "libengine.so").is_file()
    assert (stale_assets / "site-packages/numpy/__init__.py").is_file()
    assert len(first_identity.strip()) == 64

    (stdlib / "encodings" / "__init__.py").write_text(
        "changed runtime fixture\n", encoding="utf-8"
    )
    exporter_module._stage_python_runtime(request, staging, prefix, "x86_64")
    assert runtime_id.read_text(encoding="utf-8") != first_identity


def test_android_engine_staging_excludes_desktop_runtime_payloads(
    monkeypatch,
    tmp_path,
):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    source_root = tmp_path / "source"
    package = source_root / "python/Infernux"
    bootstrap = package / "engine/platform_player_bootstrap.py"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("def run_platform_player(): pass\n", encoding="utf-8")
    shader = package / "resources/shaders/standard.vert"
    shader.parent.mkdir(parents=True)
    shader.write_text("void main() {}\n", encoding="utf-8")
    excluded = (
        package / "_runtime_packs/Runtime.inxrt",
        package / "_runtime_modules/Parallel.inxmod",
        package / "resources/official_packages/default.inxpkg",
        package / "resources/player_runtime/InfernuxPlayerHost.exe",
        package / "resources/project_templates/EmptyProject.json",
        package / "test/test_runtime.py",
        package / "engine/platform_player_bootstrap.pyi",
    )
    for path in excluded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"desktop-only")
    staging = tmp_path / "staging"
    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(),
    )

    exporter_module._stage_engine_python_package(request, staging, source_root)

    destination = staging / "app/src/main/assets/python/site-packages/Infernux"
    assert (destination / "engine/platform_player_bootstrap.py").is_file()
    assert (destination / "resources/shaders/standard.vert").is_file()
    assert not (destination / "_runtime_packs").exists()
    assert not (destination / "_runtime_modules").exists()
    assert not (destination / "resources/official_packages").exists()
    assert not (destination / "resources/player_runtime").exists()
    assert not (destination / "resources/project_templates").exists()
    assert not (destination / "test").exists()
    assert not (destination / "engine/platform_player_bootstrap.pyi").exists()


def test_android_python_runtime_identity_tracks_layout_contract(monkeypatch, tmp_path):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    prefix = tmp_path / "python-prefix"
    stdlib = prefix / "lib/python3.13"
    stdlib.mkdir(parents=True)
    (stdlib / "os.py").write_text("fixture\n", encoding="utf-8")
    runtime_library = prefix / "lib/libpython3.13.so"
    runtime_library.write_bytes(b"python")

    first = exporter_module._python_runtime_identity(
        prefix,
        stdlib,
        (runtime_library,),
        "3.13",
        "x86_64",
    )
    monkeypatch.setattr(
        exporter_module,
        "_ANDROID_PYTHON_RUNTIME_LAYOUT",
        exporter_module._ANDROID_PYTHON_RUNTIME_LAYOUT + 1,
    )
    second = exporter_module._python_runtime_identity(
        prefix,
        stdlib,
        (runtime_library,),
        "3.13",
        "x86_64",
    )

    assert second != first


def test_android_python_runtime_identity_includes_engine_modules(monkeypatch, tmp_path):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    staging = tmp_path / "staging"
    python_assets = staging / "app/src/main/assets/python"
    site_packages = python_assets / "site-packages"
    (site_packages / "Infernux").mkdir(parents=True)
    (site_packages / "packaging").mkdir()
    (python_assets / "infernux-runtime.id").write_text(
        "a" * 64 + "\n", encoding="ascii"
    )
    engine_module = site_packages / "Infernux/__init__.py"
    engine_module.write_text("ENGINE = 1\n", encoding="utf-8")
    (site_packages / "packaging/__init__.py").write_text(
        "VERSION = 1\n", encoding="utf-8"
    )

    first = exporter_module._finalize_python_runtime_identity(staging)
    (python_assets / "infernux-runtime.id").write_text(
        "a" * 64 + "\n", encoding="ascii"
    )
    engine_module.write_text("ENGINE = 2\n", encoding="utf-8")
    second = exporter_module._finalize_python_runtime_identity(staging)

    assert first != second
    assert (python_assets / "infernux-runtime.id").read_text(
        encoding="ascii"
    ) == second + "\n"


def test_android_python_runtime_rejects_unsafe_numpy_wheel(monkeypatch, tmp_path):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    prefix = tmp_path / "python-prefix"
    include = prefix / "include" / "python3.13"
    stdlib = prefix / "lib" / "python3.13"
    include.mkdir(parents=True)
    (stdlib / "encodings").mkdir(parents=True)
    (include / "Python.h").write_text("fixture header\n", encoding="utf-8")
    (stdlib / "encodings" / "__init__.py").write_text(
        "fixture\n", encoding="utf-8"
    )
    (prefix / "lib" / "libpython3.13.so").write_bytes(b"python")
    wheel = _write_android_numpy_wheel(prefix)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../outside.py", "unsafe\n")
    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(),
    )

    with pytest.raises(ValueError, match="unsafe entry"):
        exporter_module._stage_python_runtime(
            request,
            tmp_path / "staging",
            prefix,
            "x86_64",
        )


def test_android_python_runtime_rejects_non_313_prefix(monkeypatch, tmp_path):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    prefix = tmp_path / "python-prefix"
    include = prefix / "include" / "python3.11"
    stdlib = prefix / "lib" / "python3.11"
    include.mkdir(parents=True)
    (stdlib / "encodings").mkdir(parents=True)
    (include / "Python.h").write_text("fixture header\n", encoding="utf-8")
    (prefix / "lib" / "libpython3.11.so").write_bytes(b"python")
    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(),
    )

    with pytest.raises(ValueError, match="requires CPython 3.13"):
        exporter_module._stage_python_runtime(
            request,
            tmp_path / "staging",
            prefix,
            "x86_64",
        )


def test_android_python_runtime_rejects_missing_extension_sidecars(
    monkeypatch, tmp_path
):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    prefix = tmp_path / "python-prefix"
    include = prefix / "include" / "python3.13"
    stdlib = prefix / "lib" / "python3.13"
    extension_dir = stdlib / "lib-dynload"
    include.mkdir(parents=True)
    (stdlib / "encodings").mkdir(parents=True)
    extension_dir.mkdir()
    (include / "Python.h").write_text("fixture header\n", encoding="utf-8")
    (prefix / "lib" / "libpython3.13.so").write_bytes(b"python")
    (extension_dir / "_ssl.cpython-313-x86_64-linux-android.so").write_bytes(
        b"ELF payload libssl_python.so libcrypto_python.so"
    )
    _write_android_numpy_wheel(prefix)
    request = BuildRequest(
        str(tmp_path / "project"),
        "android-x64-emulator",
        str(tmp_path / "output"),
        BuildProfile(),
    )

    with pytest.raises(
        ValueError,
        match="libcrypto_python.so, libssl_python.so",
    ):
        exporter_module._stage_python_runtime(
            request,
            tmp_path / "staging",
            prefix,
            "x86_64",
        )


def test_android_engine_native_staging_is_exact(monkeypatch, tmp_path):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    build = tmp_path / "engine-build"
    sync = build / "python-sync"
    sync.mkdir(parents=True)
    (sync / "_Infernux.so").write_bytes(b"engine")
    (sync / "_InfernuxBootstrap.so").write_bytes(b"bootstrap")
    (sync / "libInfernuxFoundation.so").write_bytes(b"foundation")
    assimp = build / "external" / "assimp" / "libassimp.so"
    jolt = build / "external" / "Jolt" / "libJolt.so"
    assimp.parent.mkdir(parents=True)
    jolt.parent.mkdir(parents=True)
    assimp.write_bytes(b"assimp")
    jolt.write_bytes(b"jolt")

    staging = tmp_path / "host"
    native = staging / "app/src/main/jniLibs/x86_64"
    native.mkdir(parents=True)
    (native / "libInfernuxOld.so").write_bytes(b"stale")
    (native / "libpython3.13.so").write_bytes(b"keep")

    staged = exporter_module._stage_engine_native_libraries(
        build, staging, "x86_64"
    )

    assert {path.name for path in staged} == {
        "_Infernux.so",
        "_InfernuxBootstrap.so",
        "libInfernuxFoundation.so",
        "libassimp.so",
        "libJolt.so",
    }
    assert not (native / "libInfernuxOld.so").exists()
    assert (native / "libpython3.13.so").is_file()


def test_android_engine_native_staging_rejects_incomplete_build(monkeypatch, tmp_path):
    _android_module(monkeypatch)
    exporter_module = importlib.import_module("infernux_android.exporter")
    build = tmp_path / "engine-build"
    sync = build / "python-sync"
    sync.mkdir(parents=True)
    (sync / "_Infernux.so").write_bytes(b"engine")

    try:
        exporter_module._stage_engine_native_libraries(
            build, tmp_path / "host", "x86_64"
        )
    except FileNotFoundError as error:
        assert "libassimp.so" in str(error)
        assert "libJolt.so" in str(error)
    else:
        raise AssertionError("incomplete Android runtime was accepted")


def test_android_registration_can_be_removed_without_residue(monkeypatch):
    module = _android_module(monkeypatch)
    registry = BuildExporterRegistry()
    registration = registry.register(
        "package:infernux/platform-android",
        module.AndroidPlatformExporter(),
    )

    assert len(registry.targets()) == 2
    assert registry.unregister(registration)
    assert registry.targets() == ()
