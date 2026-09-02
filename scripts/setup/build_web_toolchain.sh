#!/usr/bin/env bash
set -euo pipefail

EMSCRIPTEN_VERSION="6.0.8"
EMSDK_REVISION="e5bd3d0874e302a18f13c5b41f5bacf9a40c8e59"
CPYTHON_VERSION="3.13.15"
CPYTHON_SHA256="1e66a7945a48390ee4c2a4268a0e4185884059a13c4aab6d148aa208deea4a76"
CPYTHON_URL="https://www.python.org/ftp/python/${CPYTHON_VERSION}/Python-${CPYTHON_VERSION}.tar.xz"
DAWN_REVISION="31e25af254ab572c77054edec4946d2244e184dd"
DAWN_SHA256="b439c354642fa7f19249e62b0e58fc7e4810442e2740998b586f9901eed58d68"
DAWN_URL="https://codeload.github.com/google/dawn/tar.gz/${DAWN_REVISION}"

usage() {
    echo "Usage: $0 <toolchain-root>" >&2
}

if [[ $# -ne 1 ]]; then
    usage
    exit 2
fi
if [[ "$(uname -s)" != "Linux" ]]; then
    echo "The Web toolchain setup requires a Linux host or WSL2." >&2
    exit 2
fi
for command in cmake curl git make ninja python3 sha256sum tar; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is unavailable: $command" >&2
        exit 2
    fi
done

toolchain_root="$(realpath -m "$1")"
downloads="$toolchain_root/downloads"
sources="$toolchain_root/src"
builds="$toolchain_root/build"
emsdk_root="$toolchain_root/emsdk"
cpython_root="$sources/Python-${CPYTHON_VERSION}"
dawn_source="$sources/dawn-${DAWN_REVISION}"
tint_build="$builds/dawn-tint-${DAWN_REVISION}"
mkdir -p "$downloads" "$sources" "$builds"

fetch_and_verify() {
    local url="$1"
    local sha256="$2"
    local destination="$3"
    if [[ ! -f "$destination" ]]; then
        curl -Lf --retry 5 --retry-all-errors --output "$destination" "$url"
    fi
    echo "$sha256  $destination" | sha256sum --check --status || {
        echo "Downloaded file does not match its pinned SHA-256: $destination" >&2
        exit 1
    }
}

if [[ ! -d "$emsdk_root/.git" ]]; then
    git clone https://github.com/emscripten-core/emsdk.git "$emsdk_root"
fi
if [[ "$(git -C "$emsdk_root" rev-parse HEAD)" != "$EMSDK_REVISION" ]]; then
    if [[ -n "$(git -C "$emsdk_root" status --porcelain)" ]]; then
        echo "The Emscripten SDK checkout contains local changes: $emsdk_root" >&2
        exit 1
    fi
    git -C "$emsdk_root" fetch origin "$EMSDK_REVISION"
    git -C "$emsdk_root" checkout --detach "$EMSDK_REVISION"
fi
"$emsdk_root/emsdk" install "$EMSCRIPTEN_VERSION"
"$emsdk_root/emsdk" activate "$EMSCRIPTEN_VERSION"
export EMSDK_QUIET=1
# shellcheck disable=SC1091
source "$emsdk_root/emsdk_env.sh"
export EM_CONFIG="$emsdk_root/.emscripten"
if [[ ! -f "$EM_CONFIG" ]]; then
    echo "The active Emscripten configuration is missing: $EM_CONFIG" >&2
    exit 1
fi

cpython_archive="$downloads/Python-${CPYTHON_VERSION}.tar.xz"
fetch_and_verify "$CPYTHON_URL" "$CPYTHON_SHA256" "$cpython_archive"
if [[ ! -f "$cpython_root/Tools/wasm/wasm_build.py" ]]; then
    tar -xf "$cpython_archive" -C "$sources"
fi
(
    cd "$cpython_root"
    python3 Tools/wasm/wasm_build.py emscripten-browser
)

dawn_archive="$downloads/dawn-${DAWN_REVISION}.tar.gz"
fetch_and_verify "$DAWN_URL" "$DAWN_SHA256" "$dawn_archive"
if [[ ! -f "$dawn_source/CMakeLists.txt" ]]; then
    staging="$(mktemp -d "$sources/.dawn-${DAWN_REVISION}.XXXXXX")"
    cleanup_dawn_staging() {
        rm -rf -- "$staging"
    }
    trap cleanup_dawn_staging EXIT
    tar -xzf "$dawn_archive" -C "$staging" --strip-components=1
    python3 "$staging/tools/fetch_dawn_dependencies.py" --directory "$staging"
    mv "$staging" "$dawn_source"
    trap - EXIT
fi

cmake -S "$dawn_source" -B "$tint_build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DDAWN_FETCH_DEPENDENCIES=OFF \
    -DDAWN_BUILD_BENCHMARKS=OFF \
    -DDAWN_BUILD_FUZZERS=OFF \
    -DDAWN_BUILD_NODE_BINDINGS=OFF \
    -DDAWN_BUILD_SAMPLES=OFF \
    -DDAWN_BUILD_TESTS=OFF \
    -DDAWN_ENABLE_D3D11=OFF \
    -DDAWN_ENABLE_D3D12=OFF \
    -DDAWN_ENABLE_DESKTOP_GL=OFF \
    -DDAWN_ENABLE_METAL=OFF \
    -DDAWN_ENABLE_NULL=OFF \
    -DDAWN_ENABLE_OPENGLES=OFF \
    -DDAWN_ENABLE_VULKAN=OFF \
    -DDAWN_USE_GLFW=OFF \
    -DDAWN_USE_WAYLAND=OFF \
    -DDAWN_USE_X11=OFF \
    -DTINT_BUILD_BENCHMARKS=OFF \
    -DTINT_BUILD_CMD_TOOLS=ON \
    -DTINT_BUILD_FUZZERS=OFF \
    -DTINT_BUILD_GLSL_READER=OFF \
    -DTINT_BUILD_GLSL_WRITER=OFF \
    -DTINT_BUILD_HLSL_WRITER=OFF \
    -DTINT_BUILD_MSL_WRITER=OFF \
    -DTINT_BUILD_SPV_READER=ON \
    -DTINT_BUILD_SPV_WRITER=OFF \
    -DTINT_BUILD_TESTS=OFF \
    -DTINT_BUILD_TINTD=OFF \
    -DTINT_BUILD_WGSL_READER=ON \
    -DTINT_BUILD_WGSL_WRITER=ON
cmake --build "$tint_build" --target tint --parallel 2

wasm_root="$cpython_root/builddir/emscripten-browser"
for path in \
    "$emsdk_root/upstream/emscripten/emcc" \
    "$emsdk_root/upstream/emscripten/tools/ports/emdawnwebgpu.py" \
    "$wasm_root/python.wasm" \
    "$wasm_root/python.data" \
    "$wasm_root/libpython3.13.a" \
    "$tint_build/tint"; do
    if [[ ! -f "$path" ]]; then
        echo "Web toolchain output is missing: $path" >&2
        exit 1
    fi
done

python3 - "$toolchain_root/infernux-web-toolchain.json" \
    "$emsdk_root" "$cpython_root" "$tint_build/tint" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

manifest_path, emsdk_root, cpython_root, tint_path = map(Path, sys.argv[1:])
files = {
    "emcc": emsdk_root / "upstream/emscripten/emcc",
    "emdawn_port": emsdk_root / "upstream/emscripten/tools/ports/emdawnwebgpu.py",
    "python_wasm": cpython_root / "builddir/emscripten-browser/python.wasm",
    "python_data": cpython_root / "builddir/emscripten-browser/python.data",
    "python_library": cpython_root / "builddir/emscripten-browser/libpython3.13.a",
    "tint": tint_path,
}
payload = {
    "schema": "infernux.web_toolchain",
    "kind": "infernux-web-toolchain",
    "emscripten": {"version": "6.0.8", "revision": "e5bd3d0874e302a18f13c5b41f5bacf9a40c8e59"},
    "cpython": {
        "version": "3.13.15",
        "source_url": "https://www.python.org/ftp/python/3.13.15/Python-3.13.15.tar.xz",
        "source_sha256": "1e66a7945a48390ee4c2a4268a0e4185884059a13c4aab6d148aa208deea4a76",
    },
    "dawn": {
        "revision": "31e25af254ab572c77054edec4946d2244e184dd",
        "source_url": "https://codeload.github.com/google/dawn/tar.gz/31e25af254ab572c77054edec4946d2244e184dd",
        "source_sha256": "b439c354642fa7f19249e62b0e58fc7e4810442e2740998b586f9901eed58d68",
    },
    "files": {},
}
for name, path in files.items():
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    payload["files"][name] = {"sha256": digest.hexdigest(), "size": path.stat().st_size}
manifest_path.parent.mkdir(parents=True, exist_ok=True)
temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, manifest_path)
PY

echo "Web toolchain ready: $toolchain_root"
echo "  INFERNUX_EMSDK_ROOT=$emsdk_root"
echo "  INFERNUX_WEB_CPYTHON_ROOT=$cpython_root"
echo "  INFERNUX_WEB_TINT=$tint_build/tint"
