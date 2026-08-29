"""Bootstrap contract between the browser host and the Infernux wasm runtime."""

from __future__ import annotations

from collections import deque
import hashlib
import importlib.util
import json
import marshal
import os
import sys
from types import CodeType
from typing import Any


_events: deque[tuple[str, dict[str, Any]]] = deque(maxlen=4096)
_seen_event_kinds: set[str] = set()
_frame_count = 0
_player_root = "/infernux/player"
_player_python = f"{_player_root}/python/site-packages"
_runtime_data_root = ""
if os.path.isdir(_player_python) and _player_python not in sys.path:
    sys.path.insert(0, _player_python)
if os.path.isdir(_player_root):
    print("INFERNUX_WEB_COOKED_CONTENT_READY root=/infernux/player")


def _prepare_cooked_player_content() -> str:
    """Validate and extract packaged content into the browser filesystem."""

    if not os.path.isdir(_player_root):
        return ""
    data_roots = sorted(
        os.path.join(_player_root, name)
        for name in os.listdir(_player_root)
        if name.endswith("_Data")
        and os.path.isdir(os.path.join(_player_root, name))
    )
    if len(data_roots) != 1:
        raise RuntimeError(
            "Web Player requires exactly one cooked *_Data directory; "
            f"found {len(data_roots)}"
        )
    data_root = data_roots[0]
    catalog_path = os.path.join(data_root, "Library", "RuntimeAssetCatalog.json")
    with open(catalog_path, encoding="utf-8") as stream:
        catalog = json.load(stream)

    from _InfernuxWebHost import extract_package, read_entry

    packages = catalog.get("packages")
    if not isinstance(packages, list) or not packages:
        raise RuntimeError("Web Player runtime catalog has no packages")
    extracted_packages: set[str] = set()
    extracted_entries = 0
    for package_record in packages:
        if not isinstance(package_record, dict):
            raise RuntimeError("Web Player runtime catalog package is invalid")
        relative = str(package_record.get("path", ""))
        normalized = os.path.normpath(relative).replace("\\", "/")
        if not relative or normalized.startswith("../") or os.path.isabs(relative):
            raise RuntimeError("Web Player runtime package path is invalid")
        package = os.path.join(_player_root, *normalized.split("/"))
        summary = extract_package(package, data_root)
        if (
            not isinstance(summary, dict)
            or int(summary.get("archive_bytes", -1))
            != int(package_record.get("archive_bytes", -2))
            or str(summary.get("archive_sha256", "")).casefold()
            != str(package_record.get("archive_sha256", "")).casefold()
        ):
            raise RuntimeError(f"Web Player package identity mismatch: {relative}")
        extracted_packages.add(normalized.casefold())
        extracted_entries += int(summary.get("entries", 0))

    scripts = 0
    scenes = 0
    for artifact in catalog.get("artifacts", ()):
        logical_type = artifact.get("logical_type")
        if logical_type not in {"compiled_script", "scene_artifact"}:
            continue
        package = os.path.join(_player_root, artifact["package"])
        package_key = os.path.normpath(str(artifact["package"])).replace("\\", "/").casefold()
        if package_key not in extracted_packages:
            raise RuntimeError(
                f"Web Player artifact references an undeclared package: {artifact['package']}"
            )
        payload = read_entry(package, artifact["runtime_path"])
        if logical_type == "compiled_script":
            if len(payload) < 16 or payload[:4] != importlib.util.MAGIC_NUMBER:
                raise RuntimeError(
                    f"Web Player script ABI mismatch: {artifact['runtime_path']}"
                )
            code = marshal.loads(payload[16:])
            if not isinstance(code, CodeType):
                raise RuntimeError(
                    f"Web Player script is not a code object: {artifact['runtime_path']}"
                )
            scripts += 1
        else:
            json.loads(payload)
            scenes += 1

    if scripts == 0 or scenes == 0:
        raise RuntimeError(
            "Web Player content must contain at least one compiled script and scene"
        )
    print(
        "INFERNUX_WEB_CONTENT_INDEX_READY "
        f"artifacts={len(catalog.get('artifacts', ()))} scripts={scripts} scenes={scenes} "
        f"extracted={extracted_entries}"
    )
    return data_root


_runtime_data_root = _prepare_cooked_player_content()


def _register_web_shaders() -> None:
    """Load the deterministic WGSL catalog produced by the Web cook."""

    shader_root = os.path.join(_player_root, "web-shaders")
    catalog_path = os.path.join(shader_root, "catalog.json")
    if not os.path.isfile(catalog_path):
        raise RuntimeError("Web Player shader catalog is missing")
    with open(catalog_path, encoding="utf-8") as stream:
        catalog = json.load(stream)
    if (
        catalog.get("$schema") != "infernux.web_shader_catalog"
        or catalog.get("version") != 1
        or not isinstance(catalog.get("shaders"), list)
        or not catalog["shaders"]
    ):
        raise RuntimeError("Web Player shader catalog is invalid")

    from _InfernuxWebHost import register_shader

    identities: set[tuple[str, str]] = set()
    for entry in catalog["shaders"]:
        if not isinstance(entry, dict):
            raise RuntimeError("Web Player shader catalog entry is invalid")
        name = str(entry.get("name", ""))
        stage = str(entry.get("stage", ""))
        relative = str(entry.get("path", ""))
        identity = (name, stage)
        normalized = os.path.normpath(relative).replace("\\", "/")
        if (
            not name
            or stage not in {"vertex", "fragment", "compute"}
            or identity in identities
            or not relative
            or normalized.startswith("../")
            or os.path.isabs(relative)
        ):
            raise RuntimeError("Web Player shader catalog identity is invalid")
        identities.add(identity)
        shader_path = os.path.join(shader_root, *normalized.split("/"))
        with open(shader_path, "rb") as stream:
            payload = stream.read()
        if (
            len(payload) != int(entry.get("bytes", -1))
            or hashlib.sha256(payload).hexdigest() != entry.get("sha256")
        ):
            raise RuntimeError(f"Web Player shader identity mismatch: {name} ({stage})")
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError(
                f"Web Player shader is not UTF-8 WGSL: {name} ({stage})"
            ) from error
        register_shader(name, stage, source)
    print(f"INFERNUX_WEB_SHADER_CATALOG_READY shaders={len(identities)}")


_register_web_shaders()


def infernux_web_ready(details: dict[str, Any]) -> None:
    """Receive the browser graphics and viewport contract from the native host."""

    print(
        "INFERNUX_WEB_HOST_READY "
        f"python=3.13 graphics={details.get('graphics_api')} "
        f"viewport={details.get('width')}x{details.get('height')}"
    )


def infernux_web_input(kind: str, payload: dict[str, Any]) -> None:
    """Queue one normalized browser event for the engine input adapter."""

    _events.append((kind, payload))
    if kind not in _seen_event_kinds:
        _seen_event_kinds.add(kind)
        print(f"INFERNUX_WEB_INPUT_READY kind={kind}")


def infernux_web_drain_input() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return and clear pending events without exposing the mutable queue."""

    events = tuple(_events)
    _events.clear()
    return events


def infernux_web_tick() -> None:
    """Advance the Python side once per browser animation frame."""

    global _frame_count
    _frame_count += 1


print("INFERNUX_WEB_PYTHON_READY version=3.13 runtime_stage=content-indexed")
