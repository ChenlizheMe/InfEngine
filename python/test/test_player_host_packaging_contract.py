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
    assert "INFERNUX_PLAYER_HOST_DEVELOPMENT_DIR" in cmake
    assert '"$<TARGET_FILE:InfernuxPlayerHost>"' in cmake
    assert '"${INFERNUX_PLAYER_HOST_DEVELOPMENT_DIR}/$<TARGET_FILE_NAME:InfernuxPlayerHost>"' in cmake
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
