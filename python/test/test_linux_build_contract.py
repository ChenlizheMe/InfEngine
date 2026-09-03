import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_linux_clang_companion_tools_are_resolved_before_project() -> None:
    root_cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    tool_module = (ROOT / "cmake/InfernuxCompilerTools.cmake").read_text(
        encoding="utf-8"
    )

    include = 'include("${CMAKE_CURRENT_LIST_DIR}/cmake/InfernuxCompilerTools.cmake")'
    assert root_cmake.index(include) < root_cmake.index("project(Infernux")
    assert "llvm-ar-18" in tool_module
    assert "llvm-ranlib-18" in tool_module
    assert "${CMAKE_BINARY_DIR}/toolchain-bin" in tool_module
    assert 'set(ENV{PATH} "${_infernux_llvm_tool_dir}:$ENV{PATH}")' in tool_module
    assert 'set(CMAKE_C_COMPILER_AR "${INFERNUX_LLVM_AR_EXECUTABLE}"' in tool_module
    assert 'set(CMAKE_CXX_COMPILER_RANLIB "${INFERNUX_LLVM_RANLIB_EXECUTABLE}"' in tool_module


def test_linux_presets_use_the_system_pkg_config() -> None:
    presets = json.loads(
        (ROOT / "cmake/presets/Linux.json").read_text(encoding="utf-8")
    )
    base = next(
        preset
        for preset in presets["configurePresets"]
        if preset["name"] == "linux-base"
    )

    assert base["cacheVariables"]["PKG_CONFIG_EXECUTABLE"] == "/usr/bin/pkg-config"


def test_linux_setup_installs_and_checks_the_native_toolchain() -> None:
    installer = (ROOT / "scripts/setup/install_linux_dependencies.sh").read_text(
        encoding="utf-8"
    )
    configurator = (ROOT / "scripts/setup/configure_development.sh").read_text(
        encoding="utf-8"
    )

    for dependency in (
        "clang-format",
        "glslang-tools",
        "lld",
        "llvm",
        "mesa-vulkan-drivers",
        "pkg-config",
        "vulkan-validationlayers",
        "xauth",
        "xvfb",
    ):
        assert dependency in installer
    for executable in ("clang", "clang++", "ld.lld", "ninja", "glslangValidator"):
        assert executable in configurator
    assert "find_llvm_tool llvm-ar" in configurator
    assert "find_llvm_tool llvm-ranlib" in configurator
    assert "install_linux_dependencies.sh" in configurator


def test_linux_ci_reuses_the_repository_dependency_installer() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    linux_job = workflow.split("  linux-desktop:", 1)[1]

    assert "run: scripts/setup/install_linux_dependencies.sh" in linux_job
    assert "sudo apt-get install" not in linux_job
    assert "VK_ICD_FILENAMES: /usr/share/vulkan/icd.d/lvp_icd.x86_64.json" in linux_job
    assert "xvfb-run --auto-servernum python -m pytest python/test" in linux_job


def test_clang_format_is_an_explicit_developer_target_dependency() -> None:
    module = (ROOT / "cmake/InfernuxDeveloperTools.cmake").read_text(encoding="utf-8")

    assert "clang-format-18" in module
    assert "find_program(INFERNUX_CLANG_FORMAT_EXECUTABLE" in module
    find_block = module.split("find_program(INFERNUX_CLANG_FORMAT_EXECUTABLE", 1)[1].split(
        ")", 1
    )[0]
    assert "REQUIRED" not in find_block
    assert 'COMMAND "${CMAKE_COMMAND}" -E false' in module


def test_linux_wheel_native_modules_are_relocatable_across_virtual_environments() -> None:
    native_targets = (ROOT / "cmake/InfernuxNativeTargets.cmake").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "cmake/InfernuxInstall.cmake").read_text(encoding="utf-8")
    verifier = (ROOT / "cmake/verify_python_wheel.cmake").read_text(
        encoding="utf-8"
    )
    linux_block = installer.split("    # Linux: copy shared libraries (.so) and set RPATH", 1)[1]
    linux_block = linux_block.split("# ------------------------------------------------------------------------------", 1)[0]

    assert "pybind11::headers Python3::Module" in native_targets
    assert (
        "target_link_libraries(InfernuxRuntime PRIVATE pybind11::embed)"
        not in native_targets
    )
    assert 'set(_infernux_linux_rpath "$ORIGIN")' in linux_block
    assert "$ORIGIN/../../../.." not in linux_block
    for target in ("_Infernux", "_InfernuxBootstrap"):
        block = linux_block.split(f"set_target_properties({target} PROPERTIES", 1)[1]
        block = block.split(")", 1)[0]
        assert 'INSTALL_RPATH "${_infernux_linux_rpath}"' in block
    assert "readelf" in verifier
    assert "direct libpython dependency" in verifier


def test_wheel_build_requires_cmake_staging_and_a_native_extension() -> None:
    setup_script = (ROOT / "setup.py").read_text(encoding="utf-8")
    packaging = (ROOT / "cmake/InfernuxPackaging.cmake").read_text(encoding="utf-8")
    installer = (ROOT / "cmake/InfernuxInstall.cmake").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert 'os.environ.get("INFERNUX_STAGED_WHEEL_BUILD") != "1"' in setup_script
    assert 'native_source = Path.cwd() / "python" / "Infernux" / "lib"' in setup_script
    assert 'native_source.glob("_Infernux*.pyd")' in setup_script
    assert 'native_source.glob("_Infernux*.so")' in setup_script
    assert '"INFERNUX_STAGED_WHEEL_BUILD=1"' in packaging
    assert '"${CMAKE_SOURCE_DIR}/python/infernux.pyi"' in installer
    assert 'public_stub = Path.cwd() / "python" / "infernux.pyi"' in setup_script
    assert 'shutil.copy2(public_stub, Path(self.build_lib) / "infernux.pyi")' in setup_script
    assert "include python/infernux.pyi" in manifest


def test_official_packages_are_rebuilt_before_wheel_staging_without_source_globs() -> None:
    plugins = (ROOT / "external/plugins/CMakeLists.txt").read_text(encoding="utf-8")
    installer = (ROOT / "cmake/InfernuxInstall.cmake").read_text(encoding="utf-8")
    packaging = (ROOT / "cmake/InfernuxPackaging.cmake").read_text(
        encoding="utf-8"
    )

    assert "add_custom_target(infernux_official_plugins ALL" in plugins
    assert "BYPRODUCTS ${INFERNUX_OFFICIAL_PLUGIN_ARTIFACTS}" in plugins
    assert "file(GLOB_RECURSE INFERNUX_OFFICIAL_PLUGIN_SOURCES" not in plugins
    stage = packaging.split("add_custom_target(stage_python_package", 1)[1].split(
        "add_custom_target(package_python", 1
    )[0]
    assert "prebuild_player_runtime" in stage
    assert "infernux_official_plugins" in stage
    wheel_package_install = installer.split(
        '"${INFERNUX_OFFICIAL_PLUGIN_OUTPUT_DIR}/default-libraries.json"', 1
    )[1].split("if(TARGET InfernuxPlayerHost)", 1)[0]
    assert "infernux.mcp.inxpkg" in wheel_package_install
    assert "infernux.platform-" not in wheel_package_install
    assert "INFERNUX_HOST_PLATFORM_PACKAGE" not in installer
