from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_player_host_is_a_static_inxpack_bootstrap():
    cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    host = (ROOT / "cpp/infernux/tools/launcher/PlayerHost.cpp").read_text(encoding="utf-8")
    launcher = (ROOT / "cpp/infernux/tools/launcher/InfernuxPlayerLauncher.cpp").read_text(
        encoding="utf-8"
    )
    assert "cpp/infernux/platform/filesystem/InxPack.cpp" in cmake
    assert "target_link_libraries(InfernuxPlayerHost PRIVATE InfernuxFoundation" not in cmake
    assert "${INFERNUX_ZSTD_TARGET}" in cmake
    assert "PyConfig_InitIsolatedConfig" in host
    assert "PyConfig *, wchar_t **, const wchar_t *" in host
    assert "PyConfig_SetArgv" in host
    assert "Py_InitializeFromConfig" in host
    assert "_InfernuxPlayer." in host
    assert "directory_iterator" in host
    assert "Py_Main" not in host
    assert "PySys_SetArgvEx" not in host
    assert 'SetEnvironmentVariableW(L"PYTHONPATH"' not in host
    assert "ReadManifest(layout.bootstrapArchive)" in host
    assert "Extract(layout.bootstrapArchive, staging)" in host
    assert "bootstrapArchive.string()" not in host
    host_sources = cmake.split("target_sources(InfernuxPlayerHost PRIVATE", 1)[1].split(")", 1)[0]
    assert "cpp/infernux/core/log/InxLog.cpp" not in host_sources
    assert "target_link_options(InfernuxPlayerHost PRIVATE /NODEFAULTLIB:python312.lib)" in cmake
    assert "target_link_options(infernux_player_host_tests PRIVATE /NODEFAULTLIB:python312.lib)" in cmake
    assert cmake.count('MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"') == 2
    assert "target_link_libraries(InfernuxPlayerHost PRIVATE Python3::Python" not in cmake
    assert "target_link_libraries(InfernuxPlayerHost PRIVATE python312" not in cmake
    assert launcher.index("#include <windows.h>") < launcher.index("#include <shellapi.h>")


def test_player_host_cache_is_outside_install_directory():
    host = (ROOT / "cpp/infernux/tools/launcher/PlayerHost.cpp").read_text(encoding="utf-8")
    assert 'L"Infernux" / L"PlayerCache"' in host
    assert 'L"PlayerWarmCache"' not in host
    assert 'layout.dataRoot / L"Library"' not in host
    assert 'cacheRoot / L"stdlib"' in host
    assert "ReclaimStale" in host
    assert "WriteMetadata" in host
    assert ".stale." in host


def test_player_package_contract_has_bootstrap_archive_and_no_root_bootstrap_files():
    audit = (ROOT / "python/Infernux/engine/player_package_audit.py").read_text(encoding="utf-8")
    builder = (ROOT / "python/Infernux/engine/game_builder.py").read_text(encoding="utf-8")
    assert "Bootstrap.inxrt" in audit
    assert "BOOTSTRAP_REQUIRED_ARCHIVE_FILES" in audit
    assert "stdlib/encodings/__init__.pyc" in audit
    assert "_pack_player_bootstrap_archive" in builder
    assert "BOOTSTRAP_NATIVE_ROOT_ALLOWLIST: dict[str, dict[str, str]] = {}" in audit
