from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "setup" / "build_web_toolchain.sh"


def test_web_toolchain_setup_pins_every_downloaded_source():
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'EMSCRIPTEN_VERSION="6.0.8"' in source
    assert 'EMSDK_REVISION="e5bd3d0874e302a18f13c5b41f5bacf9a40c8e59"' in source
    assert 'CPYTHON_VERSION="3.13.15"' in source
    assert 'DAWN_REVISION="31e25af254ab572c77054edec4946d2244e184dd"' in source
    assert len(re.findall(r'^[A-Z_]+_SHA256="[0-9a-f]{64}"$', source, re.MULTILINE)) == 2
    assert "sha256sum --check --status" in source
    assert "--retry 5 --retry-all-errors" in source
    assert "max-filesize" not in source.casefold()
    assert "timeout" not in source.casefold()


def test_web_toolchain_setup_builds_webgpu_tools_without_gl_fallbacks():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "Tools/wasm/wasm_build.py emscripten-browser" in source
    assert 'export EM_CONFIG="$emsdk_root/.emscripten"' in source
    assert '[[ ! -f "$EM_CONFIG" ]]' in source
    assert "--target tint" in source
    assert "-DDAWN_ENABLE_DESKTOP_GL=OFF" in source
    assert "-DDAWN_ENABLE_OPENGLES=OFF" in source
    assert "infernux-web-toolchain.json" in source
