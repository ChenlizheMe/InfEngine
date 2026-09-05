from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


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
