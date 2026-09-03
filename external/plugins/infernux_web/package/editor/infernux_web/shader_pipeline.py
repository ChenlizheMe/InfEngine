"""Deterministic GLSL-to-WGSL preparation for the Web Player cook."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


WEB_PUSH_CONSTANT_GROUP = 0
WEB_PUSH_CONSTANT_BINDING = 999
WEB_SAMPLER_BINDING_BASE = 500
WEB_LAST_ENGINE_BINDING = WEB_SAMPLER_BINDING_BASE - 1

_LAYOUT_RE = re.compile(r"layout\s*\((?P<qualifiers>[^)]*)\)", re.MULTILINE)
_COMBINED_SAMPLER_RE = re.compile(
    r"layout\s*\((?P<qualifiers>[^)]*)\)\s*"
    r"uniform\s+(?P<type>(?:[iu]?sampler)[A-Za-z0-9_]*)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_PUSH_BLOCK_RE = re.compile(
    r"layout\s*\((?P<qualifiers>[^)]*\bpush_constant\b[^)]*)\)\s*"
    r"uniform\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"\{(?P<body>.*?)\}",
    re.DOTALL,
)
_INTEGER_QUALIFIER_RE = re.compile(r"\b(?P<name>set|binding)\s*=\s*(?P<value>\d+)\b")
_WGSL_SAMPLER_RE = re.compile(
    r"@group\((?P<group>\d+)u?\)\s*"
    r"@binding\((?P<binding>\d+)u?\)\s*"
    r"var\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)_sampler\s*:\s*"
    r"(?P<type>sampler(?:_comparison)?)\s*;",
    re.MULTILINE,
)


class WebShaderCompatibilityError(ValueError):
    """Raised when authored GLSL cannot be represented by the Web shader ABI."""


class WebShaderToolError(RuntimeError):
    """Raised when glslang or Tint cannot produce a valid Web shader."""


@dataclass(frozen=True, slots=True)
class WebSamplerBinding:
    name: str
    group: int
    texture_binding: int
    sampler_binding: int


@dataclass(frozen=True, slots=True)
class PreparedWebShader:
    source: str
    sampler_bindings: tuple[WebSamplerBinding, ...]
    uses_push_constant_uniform: bool

    @property
    def tint_sampler_mapping(self) -> str:
        return " ".join(
            f"{item.group},{item.texture_binding}:"
            f"{item.group},{item.sampler_binding}"
            for item in self.sampler_bindings
        )


@dataclass(frozen=True, slots=True)
class CompiledWebShader:
    wgsl: str
    prepared: PreparedWebShader
    stage: str


def compile_shader_manifest(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    glslang: str | Path = "glslangValidator",
    tint: str | Path = "tint",
) -> dict[str, object]:
    """Compile a deterministic shader catalog for the browser runtime."""

    manifest_file = Path(manifest_path).resolve()
    output_root = Path(output_directory).resolve()
    try:
        document = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WebShaderToolError(
            f"Web shader manifest is unreadable: {manifest_file}: {error}"
        ) from error
    entries = document.get("shaders")
    if not isinstance(entries, list) or not entries:
        raise WebShaderCompatibilityError(
            "Web shader manifest must contain a non-empty shaders array"
        )

    normalized_entries: list[tuple[str, str, Path]] = []
    identities: set[tuple[str, str]] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise WebShaderCompatibilityError("Web shader entry must be an object")
        name = str(raw.get("name", "")).strip()
        stage = str(raw.get("stage", "")).strip().casefold()
        source_name = str(raw.get("source", "")).strip()
        if not name or stage not in {"vertex", "fragment", "compute"} or not source_name:
            raise WebShaderCompatibilityError(
                "Web shader entry requires name, source, and a supported stage"
            )
        identity = (name, stage)
        if identity in identities:
            raise WebShaderCompatibilityError(
                f"Duplicate Web shader identity: {name!r} ({stage})"
            )
        identities.add(identity)
        source_path = (manifest_file.parent / source_name).resolve()
        if source_path.parent != manifest_file.parent:
            raise WebShaderCompatibilityError(
                f"Web shader source escapes its manifest directory: {source_name}"
            )
        normalized_entries.append((name, stage, source_path))

    output_root.mkdir(parents=True, exist_ok=True)
    compiled_entries: list[dict[str, object]] = []
    for index, (name, stage, source_path) in enumerate(normalized_entries):
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError as error:
            raise WebShaderToolError(
                f"Web shader source is unreadable: {source_path.name}: {error}"
            ) from error
        compiled = compile_glsl_to_wgsl(
            source,
            stage,
            glslang=glslang,
            tint=tint,
        )
        filename = f"shader-{index:04d}.{compiled.stage}.wgsl"
        payload = compiled.wgsl.encode("utf-8")
        (output_root / filename).write_bytes(payload)
        compiled_entries.append(
            {
                "name": name,
                "stage": stage,
                "path": filename,
            }
        )

    catalog: dict[str, object] = {
        "$schema": "infernux.web_shader_catalog",
        "shaders": sorted(
            compiled_entries,
            key=lambda item: (str(item["name"]), str(item["stage"])),
        ),
    }
    (output_root / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return catalog


def prepare_glsl_for_webgpu(source: str) -> PreparedWebShader:
    """Lower Vulkan-only declarations while preserving the public RHI contract."""

    text = str(source)
    _validate_reserved_bindings(text)

    push_blocks = tuple(_PUSH_BLOCK_RE.finditer(text))
    if len(push_blocks) > 1:
        raise WebShaderCompatibilityError(
            "Web shaders support one push-constant block per stage"
        )
    if push_blocks and "[" in push_blocks[0].group("body"):
        raise WebShaderCompatibilityError(
            "Web push-constant fallback does not support array members"
        )

    lowered = _PUSH_BLOCK_RE.sub(_lower_push_block, text)
    samplers = _discover_combined_samplers(lowered)
    return PreparedWebShader(lowered, samplers, bool(push_blocks))


def compile_glsl_to_wgsl(
    source: str,
    stage: str,
    *,
    glslang: str | Path = "glslangValidator",
    tint: str | Path = "tint",
) -> CompiledWebShader:
    """Compile one stage through Vulkan 1.1 SPIR-V and pinned Dawn Tint."""

    normalized_stage = str(stage).strip().casefold()
    stage_suffixes = {
        "vertex": "vert",
        "vert": "vert",
        "fragment": "frag",
        "frag": "frag",
        "compute": "comp",
        "comp": "comp",
    }
    suffix = stage_suffixes.get(normalized_stage)
    if suffix is None:
        raise WebShaderCompatibilityError(f"Unsupported Web shader stage: {stage!r}")

    prepared = prepare_glsl_for_webgpu(source)
    with tempfile.TemporaryDirectory(prefix="infernux-web-shader-") as directory:
        root = Path(directory)
        source_path = root / f"shader.{suffix}"
        spirv_path = root / f"shader.{suffix}.spv"
        wgsl_path = root / f"shader.{suffix}.wgsl"
        source_path.write_text(prepared.source, encoding="utf-8", newline="\n")

        _run(
            (
                str(glslang),
                "-V",
                "--target-env",
                "vulkan1.1",
                "-DINX_WEBGPU=1",
                str(source_path),
                "-o",
                str(spirv_path),
            ),
            "glslang",
        )
        tint_command = [
            str(tint),
            "--format",
            "wgsl",
            "--output-name",
            str(wgsl_path),
        ]
        tint_command.append(str(spirv_path))
        _run(tint_command, "Tint")
        wgsl = _remap_tint_sampler_bindings(
            wgsl_path.read_text(encoding="utf-8"), prepared.sampler_bindings
        )
        wgsl_path.write_text(wgsl, encoding="utf-8", newline="\n")
        validated_path = root / f"shader.{suffix}.validated.wgsl"
        _run(
            (
                str(tint),
                "--format",
                "wgsl",
                "--output-name",
                str(validated_path),
                str(wgsl_path),
            ),
            "Tint WGSL validation",
        )
        wgsl = validated_path.read_text(encoding="utf-8")

    if "var<immediate>" in wgsl:
        raise WebShaderToolError(
            "Tint emitted immediate data; the Web fallback uniform was not applied"
        )
    if prepared.uses_push_constant_uniform and not re.search(
        rf"@group\({WEB_PUSH_CONSTANT_GROUP}u?\)\s*"
        rf"@binding\({WEB_PUSH_CONSTANT_BINDING}u?\)\s*var<uniform>",
        wgsl,
    ):
        raise WebShaderToolError(
            "Tint did not preserve the reserved push-constant uniform binding"
        )
    return CompiledWebShader(wgsl, prepared, suffix)


def _lower_push_block(match: re.Match[str]) -> str:
    replacement = (
        f"layout(std140, set = {WEB_PUSH_CONSTANT_GROUP}, "
        f"binding = {WEB_PUSH_CONSTANT_BINDING}) uniform {match.group('name')} "
        "{" + match.group("body") + "}"
    )
    return replacement


def _discover_combined_samplers(source: str) -> tuple[WebSamplerBinding, ...]:
    bindings: list[WebSamplerBinding] = []
    destinations: set[tuple[int, int]] = set()
    for match in _COMBINED_SAMPLER_RE.finditer(source):
        qualifiers = _parse_integer_qualifiers(match.group("qualifiers"))
        if "binding" not in qualifiers:
            raise WebShaderCompatibilityError(
                f"Combined sampler {match.group('name')!r} has no explicit binding"
            )
        group = qualifiers.get("set", 0)
        texture_binding = qualifiers["binding"]
        if texture_binding > WEB_LAST_ENGINE_BINDING:
            raise WebShaderCompatibilityError(
                f"Binding {texture_binding} is reserved by the Web shader ABI"
            )
        sampler_binding = WEB_SAMPLER_BINDING_BASE + texture_binding
        destination = (group, sampler_binding)
        if destination in destinations:
            raise WebShaderCompatibilityError(
                f"Sampler binding collision at group {group}, binding {sampler_binding}"
            )
        destinations.add(destination)
        bindings.append(
            WebSamplerBinding(
                match.group("name"), group, texture_binding, sampler_binding
            )
        )
    return tuple(bindings)


def _validate_reserved_bindings(source: str) -> None:
    for match in _LAYOUT_RE.finditer(source):
        qualifiers = _parse_integer_qualifiers(match.group("qualifiers"))
        binding = qualifiers.get("binding")
        if binding is None:
            continue
        if binding >= WEB_SAMPLER_BINDING_BASE:
            raise WebShaderCompatibilityError(
                f"Bindings {WEB_SAMPLER_BINDING_BASE} through "
                f"{WEB_PUSH_CONSTANT_BINDING} are reserved by the Web shader ABI"
            )


def _remap_tint_sampler_bindings(
    wgsl: str, bindings: Sequence[WebSamplerBinding]
) -> str:
    expected = {(item.group, item.name): item for item in bindings}
    remapped: set[tuple[int, str]] = set()

    def replace(match: re.Match[str]) -> str:
        key = (int(match.group("group")), match.group("name"))
        binding = expected.get(key)
        if binding is None:
            return match.group(0)
        remapped.add(key)
        return (
            f"@group({binding.group}u) @binding({binding.sampler_binding}u) "
            f"var {binding.name}_sampler : {match.group('type')};"
        )

    result = _WGSL_SAMPLER_RE.sub(replace, wgsl)
    missing = sorted(set(expected) - remapped)
    if missing:
        labels = ", ".join(f"{group}:{name}" for group, name in missing)
        raise WebShaderToolError(
            f"Tint output is missing combined sampler declarations: {labels}"
        )
    return result


def _parse_integer_qualifiers(qualifiers: str) -> dict[str, int]:
    return {
        match.group("name"): int(match.group("value"))
        for match in _INTEGER_QUALIFIER_RE.finditer(qualifiers)
    }


def _run(command: Sequence[str] | Iterable[str], label: str) -> None:
    arguments = tuple(str(item) for item in command)
    completed = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return
    detail = completed.stderr.strip() or completed.stdout.strip()
    raise WebShaderToolError(
        f"{label} failed with exit code {completed.returncode}: "
        f"{detail or 'no diagnostic output'}"
    )


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Compile an Infernux Web shader manifest to validated WGSL"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--glslang", default="glslangValidator")
    parser.add_argument("--tint", default="tint")
    arguments = parser.parse_args()
    catalog = compile_shader_manifest(
        arguments.manifest,
        arguments.output,
        glslang=arguments.glslang,
        tint=arguments.tint,
    )
    print(f"INFERNUX_WEB_SHADER_CATALOG_READY shaders={len(catalog['shaders'])}")
    return 0


__all__ = [
    "CompiledWebShader",
    "PreparedWebShader",
    "WEB_LAST_ENGINE_BINDING",
    "WEB_PUSH_CONSTANT_BINDING",
    "WEB_PUSH_CONSTANT_GROUP",
    "WEB_SAMPLER_BINDING_BASE",
    "WebSamplerBinding",
    "WebShaderCompatibilityError",
    "WebShaderToolError",
    "compile_glsl_to_wgsl",
    "compile_shader_manifest",
    "prepare_glsl_for_webgpu",
]


if __name__ == "__main__":
    raise SystemExit(_main())
