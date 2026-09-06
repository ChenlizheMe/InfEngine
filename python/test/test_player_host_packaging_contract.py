from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_player_host_loads_the_direct_runtime_tree():
    cmake = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "CMakeLists.txt",
            ROOT / "cmake/InfernuxPlayerHost.cmake",
            ROOT / "cpp/tests/CMakeLists.txt",
        )
    )
    host = (ROOT / "cpp/infernux/tools/launcher/PlayerHost.cpp").read_text(encoding="utf-8")
    launcher = (ROOT / "cpp/infernux/tools/launcher/InfernuxPlayerLauncher.cpp").read_text(
        encoding="utf-8"
    )
    assert "cpp/infernux/platform/filesystem/InxPack.cpp" not in cmake
    assert "INFERNUX_PLAYER_HOST_DEVELOPMENT_DIR" not in cmake
    assert 'set(INFERNUX_PLAYER_HOST_BUILD_PATH "$<TARGET_FILE:InfernuxPlayerHost>")' in cmake
    assert "python/Infernux/resources/player_runtime" not in cmake
    assert "target_link_libraries(InfernuxPlayerHost PRIVATE InfernuxFoundation" not in cmake
    assert "target_link_libraries(InfernuxPlayerHost PRIVATE ${CMAKE_DL_LIBS})" in cmake
    assert "PyConfig_InitIsolatedConfig" in host
    assert "PyConfig *, wchar_t **, const wchar_t *" in host
    assert "PyConfig_SetArgv" in host
    assert "Py_InitializeFromConfig" in host
    assert "_InfernuxPlayer." in host
    assert "directory_iterator" in host
    assert "Py_Main" not in host
    assert "PySys_SetArgvEx" not in host
    assert 'SetEnvironmentVariableW(L"PYTHONPATH"' not in host
    assert 'layout.runtimeRoot = layout.dataRoot / "Runtime"' in host
    assert "PrepareRuntime(layout)" in host
    assert "ReadManifest(" not in host
    assert "Extract(" not in host
    host_sources = cmake.split("target_sources(InfernuxPlayerHost PRIVATE", 1)[1].split(")", 1)[0]
    assert "cpp/infernux/core/log/InxLog.cpp" not in host_sources
    assert "/NODEFAULTLIB:python${Python3_VERSION_MAJOR}${Python3_VERSION_MINOR}.lib" in cmake
    assert cmake.count('MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"') == 2
    assert "target_link_libraries(InfernuxPlayerHost PRIVATE Python3::Python" not in cmake
    assert "target_link_libraries(InfernuxPlayerHost PRIVATE python313" not in cmake
    assert launcher.index("#include <windows.h>") < launcher.index("#include <shellapi.h>")


def test_player_pack_codec_builds_only_the_current_zstandard_format():
    external_cmake = (ROOT / "external/CMakeLists.txt").read_text(encoding="utf-8")

    assert 'set(ZSTD_LEGACY_SUPPORT OFF CACHE BOOL "" FORCE)' in external_cmake
    assert external_cmake.index("set(ZSTD_LEGACY_SUPPORT OFF") < external_cmake.index(
        "if(INFERNUX_ZSTD_SOURCE_DIR)"
    )


def test_player_host_loads_the_built_runtime_without_a_cache():
    host = (ROOT / "cpp/infernux/tools/launcher/PlayerHost.cpp").read_text(encoding="utf-8")
    assert 'layout.runtimeRoot = layout.dataRoot / "Runtime"' in host
    assert 'layout.runtimeRoot / "stdlib"' in host
    assert "PlayerCache" not in host
    assert "PrepareCache" not in host


def test_wheel_refreshes_player_native_contract_after_cache_restore():
    install = (ROOT / "cmake/InfernuxInstall.cmake").read_text(encoding="utf-8")

    refresh = install.split(
        "add_custom_target(refresh_player_native_contract", 1
    )[1].split(")\n", 1)[0]
    assert "stage_player_native_contract.cmake" in refresh
    assert '"-DTARGET_DIR=$<TARGET_FILE_DIR:_Infernux>"' in refresh
    assert '"-DTARGET_DIR=${PYTHON_TARGET_DIR}"' in refresh
    assert "DEPENDS _Infernux" in refresh
    assert "add_dependencies(prebuild_player_runtime refresh_player_native_contract)" in install
    assert '"-DNATIVE_MODULE_DIR=${INFERNUX_STAGE_DIR}/python-wheel-source/python/Infernux/lib"' in install


def test_runtime_pack_is_compiled_from_the_assembled_wheel_payload():
    packaging = (ROOT / "cmake/InfernuxPackaging.cmake").read_text(encoding="utf-8")
    install = (ROOT / "cmake/InfernuxInstall.cmake").read_text(encoding="utf-8")
    stage = packaging.split("add_custom_target(stage_python_package", 1)[1].split(")\n", 1)[0]
    wheel = packaging.split("add_custom_target(package_python", 1)[1].split(")\n", 1)[0]

    assert "prebuild_player_runtime" not in stage
    assert "refresh_player_native_contract" in stage
    assert "add_dependencies(prebuild_player_runtime stage_python_package)" in packaging
    assert "DEPENDS stage_python_package" in wheel
    assert "DEPENDS prebuild_player_runtime" not in wheel
    assert '"-DPLATFORM_PLAYER_OUTPUT=${INFERNUX_PLATFORM_PLAYER_OUTPUT_DIR}"' in install
    assert '/external/plugins/infernux_${_infernux_player_platform}/package/editor/infernux_${_infernux_player_platform}/player"' in install
    assert '"-DINFERNUX_SOURCE_DIR=${INFERNUX_STAGE_DIR}/python-wheel-source"' in install
    assert 'DIRECTORY "${INFERNUX_PREBUILT_RUNTIME_DIR}/"' not in install


def test_player_package_contract_has_bootstrap_archive_and_no_root_bootstrap_files():
    audit = (ROOT / "python/Infernux/engine/player_package_audit.py").read_text(encoding="utf-8")
    builder = (ROOT / "python/Infernux/engine/game_builder.py").read_text(encoding="utf-8")
    assert "Bootstrap.inxrt" in audit
    assert "BOOTSTRAP_REQUIRED_ARCHIVE_FILES" in audit
    assert "stdlib/encodings/__init__.pyc" in audit
    assert "_pack_player_bootstrap_archive" in builder
    assert "BOOTSTRAP_NATIVE_ROOT_ALLOWLIST: dict[str, dict[str, str]] = {}" in audit


def test_linux_bootstrap_foundation_is_a_sibling_of_the_bootstrap_module():
    builder = (ROOT / "python/Infernux/engine/game_builder.py").read_text(
        encoding="utf-8"
    )
    audit = (ROOT / "python/Infernux/engine/player_package_audit.py").read_text(
        encoding="utf-8"
    )
    assert '"libInfernuxFoundation.so": str(foundation)' in builder
    assert '"Infernux/lib/libInfernuxFoundation.so": str(foundation)' not in builder
    assert '"libInfernuxFoundation.so",' in audit
