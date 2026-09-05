from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("preset", ["windows-msvc-release", "linux-clang-release"])
def test_release_output_paths_follow_version_and_preserve_other_outputs(tmp_path, preset):
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("CMake executable is required")
    source = tmp_path / "source"
    source.mkdir()
    binary = source / "out/build" / preset
    (source / "CMakeLists.txt").write_text(
        'cmake_minimum_required(VERSION 3.25)\nproject(OutputOwnership NONE)\n'
        f'set(Python3_EXECUTABLE "{Path(sys.executable).as_posix()}")\n'
        f'include("{ROOT.as_posix()}/cmake/InfernuxOutputPaths.cmake")\n'
        'file(WRITE "${CMAKE_BINARY_DIR}/paths.txt" '
        '"${INFERNUX_STAGE_DIR}\\n${INFERNUX_RELEASE_DIR}\\n")\n',
        encoding="utf-8",
    )
    existing = source / "dist/releases/0.4.0/other-platform.bin"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"preserve the other producer")
    for version in ("0.4.0", "0.4.1"):
        (source / "pyproject.toml").write_text(
            f'[project]\nversion = "{version}"\n', encoding="utf-8"
        )
        result = subprocess.run(
            [cmake, "-S", str(source), "-B", str(binary)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        stage, release = (binary / "paths.txt").read_text(encoding="utf-8").splitlines()
        assert Path(stage) == source / "out/stage" / preset
        assert Path(release) == source / "dist/releases" / version
        assert existing.read_bytes() == b"preserve the other producer"


def test_product_targets_do_not_clean_source_python_caches():
    install = (ROOT / "cmake/InfernuxInstall.cmake").read_text(encoding="utf-8")
    maintenance = (ROOT / "cmake/InfernuxDeveloperTools.cmake").read_text(encoding="utf-8")
    assert "clean_python_pycache" not in install
    assert "add_custom_target(clean_python_pycache" in maintenance
    assert "add_dependencies(_Infernux" not in maintenance


def test_runtime_pack_cmake_does_not_write_bytecode_into_its_source(tmp_path):
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("CMake executable is required for build-entry ownership test")
    source = tmp_path / "source"
    package = source / "python/Infernux"
    engine = package / "engine"
    engine.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (engine / "__init__.py").write_text("", encoding="utf-8")
    (engine / "prebuilt_runtime.py").write_text(
        "import os\nassert os.environ['INFERNUX_NATIVE_MODULE_DIR']\n", encoding="utf-8"
    )
    sentinel = package / "__pycache__/author.sentinel"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"existing source cache must remain untouched")

    def snapshot():
        return {path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob("*") if path.is_file()}

    before = snapshot()
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    result = subprocess.run([
        cmake, "-DINFERNUX_BUILD_CONFIG=Release",
        f"-DINFERNUX_SOURCE_DIR={source}", f"-DPYTHON_EXECUTABLE={sys.executable}",
        f"-DNATIVE_MODULE_DIR={tmp_path / 'native'}",
        f"-DOUTPUT_ROOT={tmp_path / 'runtime-packs'}",
        f"-DMODULE_OUTPUT_ROOT={tmp_path / 'runtime-modules'}",
        f"-DBUILD_CACHE_ROOT={tmp_path / 'cache'}",
        "-P", str(ROOT / "cmake/prebuild_player_runtime.cmake"),
    ], env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot() == before
