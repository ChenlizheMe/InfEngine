"""Read-only Emscripten, WebGPU, and CPython 3.13 diagnostics."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from Infernux.engine.build import (
    BuildDiagnostic,
    BuildTargetId,
    CapabilityReport,
    DiagnosticSeverity,
)


EMSCRIPTEN_VERSION = "6.0.8"
WEB_PYTHON_SERIES = "3.13"
EMDAWNWEBGPU_VERSION = "v20260423.175430"
DAWN_REVISION = "31e25af254ab572c77054edec4946d2244e184dd"
DEFAULT_WSL_DISTRIBUTION = "Ubuntu-22.04"
DEFAULT_EMSDK_ROOT = "~/.local/share/emsdk"
DEFAULT_CPYTHON_ROOT = "~/.infernux-toolchains/src/Python-3.13.15"
DEFAULT_TINT_PATH = "~/.infernux-toolchains/build/dawn-tint-31e25af/tint"


def inspect_web_toolchain(
    target: BuildTargetId | str,
    environ: Mapping[str, str] | None = None,
) -> CapabilityReport:
    values = os.environ if environ is None else environ
    target_id = BuildTargetId(target)
    diagnostics: list[BuildDiagnostic] = []
    details: dict[str, object] = {
        "target": str(target_id),
        "emscripten_version": EMSCRIPTEN_VERSION,
        "python_series": WEB_PYTHON_SERIES,
        "emdawnwebgpu_version": EMDAWNWEBGPU_VERSION,
        "dawn_revision": DAWN_REVISION,
    }
    source_root = _source_root(values)
    details["source_root"] = str(source_root) if source_root is not None else ""
    if source_root is None:
        diagnostics.append(
            _error(
                "web.source.checkout",
                "Set INFERNUX_SOURCE_ROOT to an Infernux source checkout for Web host bring-up.",
            )
        )

    result = _run_toolchain_probe(values)
    details.update(result["details"])
    if result["error"]:
        diagnostics.append(
            _error(
                str(result["code"]),
                str(result["error"]),
                command=str(result["command"]),
            )
        )
        return CapabilityReport(False, tuple(diagnostics), details)

    detected = str(result["details"].get("detected_emscripten_version", ""))
    if detected != EMSCRIPTEN_VERSION:
        diagnostics.append(
            _error(
                "web.emscripten.version",
                f"Emscripten {EMSCRIPTEN_VERSION} is required; detected "
                f"{detected or 'unknown'}.",
            )
        )
    for key, code, message in (
        (
            "emdawn_port",
            "web.webgpu.emdawn-port",
            "The pinned Emdawnwebgpu port is unavailable in the Emscripten SDK.",
        ),
        (
            "python_wasm",
            "web.python.runtime",
            "Build CPython 3.13 for the emscripten-browser target.",
        ),
        (
            "python_data",
            "web.python.stdlib",
            "The CPython 3.13 browser standard-library archive is missing.",
        ),
        (
            "python_library",
            "web.python.static-library",
            "The CPython 3.13 wasm static library is missing.",
        ),
        (
            "glslang_validator",
            "web.shader.glslang",
            "Install glslangValidator for the Web shader cook.",
        ),
        (
            "tint",
            "web.shader.tint",
            "Build the pinned Dawn Tint shader translator.",
        ),
    ):
        if not bool(result["details"].get(key)):
            diagnostics.append(_error(code, message))

    return CapabilityReport(
        not any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics),
        tuple(diagnostics),
        details,
    )


def _source_root(values: Mapping[str, str]) -> Path | None:
    explicit = str(values.get("INFERNUX_SOURCE_ROOT", "") or "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())

    spec = importlib.util.find_spec("Infernux")
    if spec is not None and spec.origin:
        package = Path(str(spec.origin)).resolve().parent
        candidates.extend((package.parent.parent, package.parent))

    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        resolved = candidate.resolve()
        if (
            (resolved / "CMakeLists.txt").is_file()
            and (resolved / "python" / "Infernux" / "__init__.py").is_file()
            and (resolved / "external" / "SDL" / "CMakeLists.txt").is_file()
        ):
            return resolved
    return None


def _run_toolchain_probe(values: Mapping[str, str]) -> dict[str, object]:
    if os.name == "nt":
        return _run_wsl_probe(values)
    return _run_posix_probe(values)


def _run_wsl_probe(values: Mapping[str, str]) -> dict[str, object]:
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    distribution = str(
        values.get("INFERNUX_WEB_WSL_DISTRIBUTION", DEFAULT_WSL_DISTRIBUTION)
        or DEFAULT_WSL_DISTRIBUTION
    ).strip()
    details: dict[str, object] = {
        "execution_mode": "wsl",
        "distribution": distribution,
    }
    if not wsl:
        return _probe_failure(
            "web.wsl.launcher", "Install WSL2 for the Web build toolchain.", "wsl"
        ) | {"details": details}

    emsdk_root = str(
        values.get("INFERNUX_EMSDK_ROOT", DEFAULT_EMSDK_ROOT) or DEFAULT_EMSDK_ROOT
    ).strip()
    cpython_root = str(
        values.get("INFERNUX_WEB_CPYTHON_ROOT", DEFAULT_CPYTHON_ROOT)
        or DEFAULT_CPYTHON_ROOT
    ).strip()
    tint_path = str(
        values.get("INFERNUX_WEB_TINT", DEFAULT_TINT_PATH) or DEFAULT_TINT_PATH
    ).strip()
    command = [
        wsl,
        "-d",
        distribution,
        "--",
        "bash",
        "-s",
        "--",
        emsdk_root,
        cpython_root,
        tint_path,
    ]
    return _execute_probe(command, details, _probe_script())


def _run_posix_probe(values: Mapping[str, str]) -> dict[str, object]:
    emsdk_root = str(
        values.get("INFERNUX_EMSDK_ROOT", "~/.local/share/emsdk")
        or "~/.local/share/emsdk"
    ).strip()
    cpython_root = str(
        values.get("INFERNUX_WEB_CPYTHON_ROOT", DEFAULT_CPYTHON_ROOT)
        or DEFAULT_CPYTHON_ROOT
    ).strip()
    tint_path = str(
        values.get("INFERNUX_WEB_TINT", DEFAULT_TINT_PATH) or DEFAULT_TINT_PATH
    ).strip()
    return _execute_probe(
        ["bash", "-s", "--", emsdk_root, cpython_root, tint_path],
        {"execution_mode": "posix"},
        _probe_script(),
    )


def _probe_script() -> str:
    return r'''set -e
infernux_expand_home() {
    case "$1" in
        '~/'*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
        *) printf '%s' "$1" ;;
    esac
}
infernux_emsdk_root=$(infernux_expand_home "$1")
infernux_cpython_root=$(infernux_expand_home "$2")
infernux_tint_path=$(infernux_expand_home "$3")
test -f "$infernux_emsdk_root/emsdk_env.sh"
version=$(head -1 "$infernux_emsdk_root/upstream/emscripten/emscripten-version.txt")
printf 'detected_emscripten_version=%s\n' "$version"
infernux_node=$(find "$infernux_emsdk_root/node" -path '*/bin/node' -type f | sort -V | tail -1)
test -x "$infernux_node"
printf 'node_version=%s\n' "$("$infernux_node" --version)"
test -x "$infernux_emsdk_root/upstream/emscripten/emcc"
printf 'emcc=1\n'
test -f "$infernux_emsdk_root/upstream/emscripten/tools/ports/emdawnwebgpu.py" && printf 'emdawn_port=1\n' || printf 'emdawn_port=0\n'
infernux_wasm_root="$infernux_cpython_root/builddir/emscripten-browser"
test -f "$infernux_wasm_root/python.wasm" && printf 'python_wasm=1\n' || printf 'python_wasm=0\n'
test -f "$infernux_wasm_root/python.data" && printf 'python_data=1\n' || printf 'python_data=0\n'
test -f "$infernux_wasm_root/libpython3.13.a" && printf 'python_library=1\n' || printf 'python_library=0\n'
command -v glslangValidator >/dev/null 2>&1 && printf 'glslang_validator=1\n' || printf 'glslang_validator=0\n'
test -x "$infernux_tint_path" && printf 'tint=1\n' || printf 'tint=0\n'
printf 'emsdk_root=%s\n' "$infernux_emsdk_root"
printf 'cpython_root=%s\n' "$infernux_cpython_root"
printf 'tint_path=%s\n' "$infernux_tint_path"
'''


def _execute_probe(
    command: list[str], details: dict[str, object], script: str
) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            input=script.replace("\r", "").encode("utf-8"),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _probe_failure(
            "web.toolchain.probe", f"Unable to inspect the Web toolchain: {error}", command
        ) | {"details": details}
    if completed.returncode != 0:
        message = (
            _decode_output(completed.stderr).strip()
            or _decode_output(completed.stdout).strip()
        )
        return _probe_failure(
            "web.toolchain.unavailable",
            message or "The Web toolchain probe failed.",
            command,
        ) | {"details": details}

    for line in _decode_output(completed.stdout).splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        normalized = key.strip()
        text = value.strip().strip('"')
        details[normalized] = text not in {"0", ""} if normalized in {
            "emcc",
            "emdawn_port",
            "python_wasm",
            "python_data",
            "python_library",
            "glslang_validator",
            "tint",
        } else text
    return {"error": "", "code": "", "command": command, "details": details}


def _decode_output(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").replace("\x00", "")


def _probe_failure(code: str, error: str, command: object) -> dict[str, object]:
    return {"error": error, "code": code, "command": command, "details": {}}


def _error(code: str, message: str, **detail: object) -> BuildDiagnostic:
    return BuildDiagnostic(
        DiagnosticSeverity.ERROR,
        code,
        message,
        source="infernux/platform-web",
        detail=detail,
    )


__all__ = [
    "DEFAULT_CPYTHON_ROOT",
    "DEFAULT_EMSDK_ROOT",
    "DEFAULT_TINT_PATH",
    "DEFAULT_WSL_DISTRIBUTION",
    "DAWN_REVISION",
    "EMDAWNWEBGPU_VERSION",
    "EMSCRIPTEN_VERSION",
    "WEB_PYTHON_SERIES",
    "inspect_web_toolchain",
]
