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

    for dependency in ("clang-format", "glslang-tools", "lld", "llvm", "pkg-config"):
        assert dependency in installer
    for executable in ("clang", "clang++", "ld.lld", "ninja", "glslangValidator"):
        assert executable in configurator
    assert "find_llvm_tool llvm-ar" in configurator
    assert "find_llvm_tool llvm-ranlib" in configurator
    assert "install_linux_dependencies.sh" in configurator


def test_linux_ci_reuses_the_repository_dependency_installer() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    linux_job = workflow.split("  linux-headless:", 1)[1]

    assert "run: scripts/setup/install_linux_dependencies.sh" in linux_job
    assert "sudo apt-get install" not in linux_job


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
