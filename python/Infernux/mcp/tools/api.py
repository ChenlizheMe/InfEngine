"""Subsystem API and shader/audio knowledge tools for agents."""

from __future__ import annotations

import inspect
import ast
import os
import threading
from typing import Any

from Infernux.engine.path_utils import portable_path, relative_path, resolved_path
from Infernux.mcp.tools.common import issue_knowledge_token, ok, register_tool_metadata, serialize_value


PROPERTY_TYPES = {
    "Float": "material.set_float(name, value)",
    "Float2": "material.set_vector2(name, x, y)",
    "Float3": "material.set_vector3(name, x, y, z)",
    "Float4": "material.set_vector4(name, x, y, z, w)",
    "Color": "material.set_color(name, r, g, b, a)",
    "Int": "material.set_int(name, value)",
    "Mat4": "material.set_param(name, matrix_value)",
    "Texture2D": "material.set_texture(name, texture_or_guid)",
}


SUBSYSTEM_GUIDES: dict[str, dict[str, Any]] = {
    "api": {
        "summary": "Agent-facing API discovery policy for a young, fast-moving engine.",
        "concepts": [
            "Do not guess unfamiliar Infernux APIs from Unity or other engines.",
            "For Python-layer APIs, call api_search(query), then api_get(symbol_or_module). The index is generated from .pyi stubs first, then .py source.",
            "For components, call component_list_types and component_describe_type before setting fields.",
            "For shader authoring, call shader_guide, shader_catalog, and shader_describe because shader behavior is C++/compiler-backed and schema-driven.",
            "For MCP tools, call mcp_catalog_search or mcp_catalog_recommend before choosing tools.",
        ],
        "workflow": [
            "1. Search: api_search('audio source play one shot') or mcp_catalog_recommend(intent).",
            "2. Inspect: api_get('AudioSource') or component_describe_type('AudioSource').",
            "3. Act: use scene/component/asset tools with the exact fields and signatures returned.",
            "4. Validate: use runtime_read_errors, console_read, or subsystem-specific describe/report tools.",
        ],
        "symbols": ["api_search", "api_get", "mcp_catalog_search", "component_describe_type", "shader_guide"],
    },
    "scripting": {
        "summary": "Stable public Python surface for ordinary game-component scripts.",
        "concepts": [
            "Game scripts must import from documented public modules, never from private underscore modules or wheel internals.",
            "Use InxComponent lifecycle methods such as start(), update(delta_time), and fixed_update(fixed_delta_time).",
            "Use the delta_time argument supplied to update instead of inventing a frame timer.",
            "Persistent scene content must be created through the Editor UI in global-validation work; scripts should define behavior for components already attached there.",
        ],
        "stable_imports": [
            "from Infernux import InxComponent, Vector3, Time, serialized_field",
            "from Infernux.input import Input, KeyCode",
            "from Infernux.components import InxComponent, serialized_field",
            "from Infernux.math import Vector3",
        ],
        "workflow": [
            "Call api_get('scripting'), api_get('input'), api_get('Transform'), and api_get('InxComponent') before writing a behavior script.",
            "Write only under Assets/ with project_script_write, then run public_api_validate_script before attaching the component through the Editor UI.",
            "Use Debug feedback to report an unavailable public API instead of inspecting wheel internals or guessing an equivalent API.",
        ],
        "symbols": ["InxComponent", "serialized_field", "Vector3", "Time", "Transform", "Input", "KeyCode"],
    },
    "input": {
        "summary": "Public keyboard, mouse, and virtual-axis queries for game scripts.",
        "stable_imports": ["from Infernux.input import Input, KeyCode"],
        "concepts": [
            "Input.get_key() accepts either a KeyCode constant or a documented string such as 'w', 'space', 'left', or 'right'.",
            "Use Input.get_key_down() for a first-press action and Input.get_key() for held movement.",
            "KeyCode.W, KeyCode.A, KeyCode.S, KeyCode.D, KeyCode.SPACE, and arrow-key constants are public SDL-scancode values.",
            "Input.get_axis_raw('Horizontal') maps A/D and left/right; Input.get_axis_raw('Vertical') maps W/S and up/down.",
            "Input values are idle until the Game View has focus during Play Mode.",
        ],
        "examples": [
            "if Input.get_key(KeyCode.W): self.transform.translate_local(Vector3(0.0, 0.0, speed * delta_time))",
            "steering = Input.get_axis_raw('Horizontal')",
        ],
        "symbols": ["Input", "KeyCode", "Input.get_key", "Input.get_key_down", "Input.get_axis_raw"],
    },
    "shader": {
        "summary": "Three-layer shader authoring model: surface fragment/vertex shaders, shading models, and GLSL libraries/templates.",
        "concepts": [
            "Shader authoring is NOT a normal Python-reflection API. It is parsed and compiled by the C++ shader/material pipeline, so this guide is manually curated.",
            "New .frag and .vert assets use one versioned ShaderInfo block for Name, properties, render state, imports, interfaces, and capabilities.",
            "Vertex and fragment stages remain separate assets. Materials may set vert_shader_name and frag_shader_name independently.",
            "Users do not write Vulkan layout, descriptor set, binding, or location declarations for the normal ShaderInfo path.",
            "The native importer emits the canonical property/interface schema consumed by the Inspector and MCP catalog; do not parse ShaderInfo again in Python scripts.",
            "ShaderInfo and ShadingModelInfo are the only authored formats; old annotation syntax is rejected.",
            "Surface shaders implement void surface(out SurfaceData s). Start with s = InitSurfaceData().",
            "Common built-in varyings include v_TexCoord, v_Color, v_WorldPos, and normal/tangent data supplied by the standard vertex path.",
            "RenderGraph fullscreen effects use fullscreen_triangle.vert automatically and bind fragment shader IDs through p.fullscreen_quad(shader_id).",
        ],
        "workflow": [
            "Call shader_catalog to discover built-in shader IDs and examples.",
            "Call shader_describe(shader_id, kind='fragment') before binding a material to a custom fragment shader.",
            "Create .frag/.vert through asset_write_text using the ShaderInfo example returned by this guide.",
            "After editing shader files, call asset_refresh and use Shader.reload(shader_id) from scripts if runtime reload is needed.",
            "For material assets, call material_create, material_set_shader, and material_set_render_queue. Runtime scripts may use the corresponding public Material properties.",
        ],
        "common_mistakes": [
            "Do not mix two competing ShaderInfo blocks in one asset. Name is the stable shader ID.",
            "Do not write layout(set=...), layout(binding=...), or layout(location=...) in normal ShaderInfo shaders.",
            "Do not assign a .shadingmodel or .glsl library as Material.frag_shader_name.",
            "Do not invent property names in Material unless the shader declares them or intentionally uses dynamic properties.",
            "Texture2D defaults are symbolic names such as white; material values must be texture GUIDs or wrappers, not paths or vec4 values.",
            "Do not assume Shader.reload is fully bound for every runtime path; prefer asset_refresh and material/pipeline refresh tools when available.",
        ],
        "rules": {
            "file_kinds": {
                ".frag": "Surface fragment shader. Uses ShaderInfo Name, optional ShadingModel/render state, and a typed Properties block.",
                ".vert": "Vertex shader. Uses ShaderInfo Name and optional Properties/Outputs. Use for custom vertex deformation or varyings.",
                ".shadingmodel": "Pipeline-independent lighting/evaluation model declared with ShadingModelInfo and one fixed shading() function; do not assign one directly to Material.",
                ".glsl": "Pure shared GLSL function library. ShaderInfo and ShadingModelInfo assets name libraries through Imports; do not assign one directly to Material.",
            },
            "shader_info": {
                "Name": "Stable shader ID used by materials and RenderGraph.",
                "ShadingModel": "Fragment lighting model such as pbr or unlit.",
                "Properties": "Typed declarations such as Float amount = 0.5 Range(0.0, 1.0), Color tint = [...] HDR, or Texture2D albedo = white.",
                "Imports": "Pure GLSL function-library dependencies, for example Imports [\"lib/common\", \"lib/color\"]. Imports do not bind renderer resources.",
                "Requires": "Compiler-owned renderer resource contracts required by this stage or shading model, for example Requires [Lighting].",
                "Inputs/Outputs": "Typed stage interfaces reserved for the stage-linker migration; built-in varyings continue to work now.",
                "Surface/Queue/Cull/DepthWrite": "Material render-state defaults compiled into importer metadata.",
                "Capabilities": "ShaderInfo-only stage/pass traits such as Fullscreen, Standalone, or ParticleSprite. ShadingModelInfo must not declare this field.",
                "Unsupported": "ShadingModelInfo-only opt-out list. Every model supports Forward, Forward+, and Deferred by default; use Unsupported [Deferred] only when reconstructed deferred surface/context data is insufficient.",
            },
            "entry_points": {
                "surface": "Preferred fragment workflow: void surface(out SurfaceData s). If no main() exists, engine injects templates, varyings, outputs, and shading-model evaluate().",
                "vertex": "Optional vertex deformation hook: void vertex(inout VertexInput v).",
                "main": "Reserved for later stage-linker and engine-internal paths. User-authored ShaderInfo assets must not declare Vulkan layout; prefer surface() or vertex() in the current public path.",
            },
            "builtins": {
                "surface_data": "Use s = InitSurfaceData(); then set fields such as albedo, alpha, emission, normalWS, metallic, smoothness, occlusion.",
                "common_varyings": ["v_TexCoord", "v_Color", "v_WorldPos", "v_Normal", "v_Tangent", "v_ViewDepth"],
                "common_uniforms": ["material", "_Globals", "lighting"],
                "material_ubo": "MaterialProperties is exposed as global 'material'. Texture2D properties become sampler2D bindings managed by the compiler.",
            },
            "reload_limitations": [
                "File watcher reload primarily handles .vert and .frag.",
                "Editing .glsl, .shadingmodel, or _templates/*.glsl may require touching/reloading a dependent .vert/.frag or restarting/invalidation.",
                "Shader.reload in Python is not a complete substitute for C++ file reload paths in every build.",
            ],
            "material_binding": [
                "Use material.vert_shader_name for vertex shader id.",
                "Use material.frag_shader_name for fragment shader id.",
                "Do not bind .glsl libraries or .shadingmodel files directly as material shaders.",
                "Use material_get_properties MCP to inspect shader.vertex, shader.fragment, render_queue, and synced properties.",
            ],
            "property_types": PROPERTY_TYPES,
        },
        "symbols": ["Shader", "Material", "shader_catalog", "shader_describe", "shader_guide"],
    },
    "audio": {
        "summary": "Audio uses AudioListener for the ears, AudioSource for multi-track playback, and AudioClip for WAV/OGG assets.",
        "concepts": [
            "AudioListener should usually be attached to the main camera. Only one listener is active at a time.",
            "AudioSource is multi-track: set track_count, assign clips per track, then play(track_index).",
            "AudioClip.load(path) returns an AudioClip wrapper or None; pass clip or clip.native to AudioSource methods.",
            "Use play_one_shot(clip, volume_scale) for transient SFX rather than creating many temporary AudioSource objects.",
        ],
        "workflow": [
            "Ensure a GameObject has AudioSource and the camera has AudioListener.",
            "Load WAV, OGG, MP3, or FLAC assets with AudioClip.load('Assets/Audio/name.wav').",
            "Assign clips with source.set_track_clip(index, clip) or set_track_clip_by_guid(index, guid).",
            "Use source.volume/pitch/mute/loop/play_on_awake for source-level behavior.",
            "Use source.set_track_volume(index, value), play(index), pause(index), stop(index), stop_all().",
        ],
        "common_mistakes": [
            "Do not use source.clip = clip; this engine exposes per-track clips instead.",
            "Track indices are zero-based and must be below source.track_count.",
            "AudioClip decodes WAV, OGG/Vorbis, MP3, and FLAC assets.",
            "Attach one AudioListener to the main camera instead of adding listeners to many objects.",
        ],
        "symbols": ["AudioSource", "AudioListener", "AudioClip", "audio_guide"],
    },
    "component": {
        "summary": "Python components inherit InxComponent; built-ins expose CppProperty fields and delegate methods.",
        "concepts": [
            "Use component_list_types and component_describe_type for exact component fields.",
            "Use serialized_field for script fields that should persist and appear in the inspector.",
            "Use lifecycle methods awake/start/update/late_update/on_enable/on_disable/on_destroy.",
        ],
        "symbols": ["InxComponent", "serialized_field", "component_describe_type", "component_list_types"],
    },
    "material": {
        "summary": "Material wraps InxMaterial and stores shader selection, render state, and typed shader properties.",
        "concepts": [
            "Material.create_lit uses default_lit; Material.create_unlit uses default_unlit.",
            "Use vert_shader_name and frag_shader_name when vertex/fragment shader IDs differ.",
            "Use set_color/set_float/set_int/set_vector*/set_texture based on the imported ShaderInfo property schema.",
            "Use material_set_surface_type for opaque/transparent render state; render queue alone does not enable blending.",
        ],
        "symbols": [
            "Material",
            "shader_describe",
            "material_create",
            "material_set_property",
            "material_set_render_queue",
            "material_set_surface_type",
        ],
    },
    "ui": {
        "summary": "Screen-space UI uses UICanvas plus UIText, UIImage, UIButton, pointer events, and persistent UIEventEntry bindings.",
        "concepts": [
            "Create a UICanvas root, then child UI elements such as UIText, UIImage, and UIButton.",
            "Positions and sizes are in canvas design pixels, scaled by UICanvas.compute_scale for the Game View.",
            "Use api_get('UICanvas'), api_get('UIText'), api_get('UIButton'), and api_get('InxUIScreenComponent') before scripting UI.",
            "UIButton.on_click is a runtime UIEvent; persistent callbacks use on_click_entries.",
            "Persistent clicks reference a target GameObject, then a component attached to it, then one public method; never bind a script asset directly.",
        ],
        "symbols": ["UICanvas", "UIText", "UIImage", "UIButton", "UIEvent", "PointerEventData"],
    },
}


_API_INDEX: dict[str, Any] | None = None
_API_INDEX_LOCK = threading.Lock()
_GUIDE_ALIASES = {
    "inxcomponent": "scripting",
    "keycode": "input",
    "time": "scripting",
    "transform": "scripting",
    "vector3": "scripting",
}


def register_api_tools(mcp) -> None:
    _register_metadata()

    @mcp.tool(name="api_subsystems")
    def api_subsystems() -> dict:
        """List documented engine subsystems available to agents."""
        return ok({
            "agent_guidance": _agent_api_guidance(),
            "subsystems": [
                {"name": name, "summary": guide["summary"], "symbols": guide.get("symbols", [])}
                for name, guide in sorted(SUBSYSTEM_GUIDES.items())
            ],
            "python_api": _api_index_status(),
        })

    @mcp.tool(name="api_get")
    def api_get(name: str) -> dict:
        """Return a subsystem guide or symbol API page."""
        key = str(name or "").strip()
        guide = SUBSYSTEM_GUIDES.get(key.lower())
        if guide is not None:
            payload = {"kind": "subsystem", "name": key.lower(), **guide}
            if key.lower() in {"shader", "audio", "ui", "material"}:
                lock_scope = "shader" if key.lower() == "material" else key.lower()
                payload["knowledge_lock"] = issue_knowledge_token(lock_scope, source_tool=f"api_get:{key.lower()}")
            return ok(payload)
        symbol = _symbol_doc(key)
        if symbol:
            return ok({"kind": "symbol", **symbol})
        module_doc = _module_doc(key)
        if module_doc:
            return ok({"kind": "module", **module_doc})
        index = _api_index()
        return ok({
            "found": False,
            "available_subsystems": sorted(SUBSYSTEM_GUIDES),
            "available_symbol_count": len(index["symbols"]),
            "available_module_count": len(index["modules"]),
            "sample_symbols": sorted(index["symbols"])[:80],
            "sample_modules": sorted(index["modules"])[:40],
            "hint": "Use api_search with a focused term or api_get with a module-qualified symbol name.",
        })

    @mcp.tool(name="api_search")
    def api_search(query: str, limit: int = 20) -> dict:
        """Search subsystem guides, symbols, shader IDs, and component names."""
        query_l = str(query or "").lower()
        safe_limit = max(1, min(int(limit or 20), 100))
        guide_name = _GUIDE_ALIASES.get(query_l.strip(), query_l.strip())
        exact_guide = SUBSYSTEM_GUIDES.get(guide_name)
        if exact_guide is not None:
            return ok({
                "query": query,
                "matches": [{
                    "kind": "subsystem",
                    "name": guide_name,
                    "summary": exact_guide["summary"],
                    "score": 100,
                }],
                "search_mode": "guide_exact",
            })

        matches = []
        for name, guide in SUBSYSTEM_GUIDES.items():
            haystack = " ".join([
                name,
                guide.get("summary", ""),
                " ".join(guide.get("concepts", [])),
                " ".join(guide.get("symbols", [])),
            ]).lower()
            score = _score(query_l, haystack)
            if score:
                matches.append({"kind": "subsystem", "name": name, "summary": guide["summary"], "score": score})
        index = _api_index()
        seen_symbols: set[str] = set()
        for doc in index["symbols"].values():
            qualname = str(doc.get("qualname") or "")
            if not qualname or qualname in seen_symbols:
                continue
            seen_symbols.add(qualname)
            name = str(doc.get("name") or qualname)
            haystack = " ".join([
                name,
                doc.get("doc", ""),
                " ".join(m["name"] for m in doc.get("methods", [])),
                " ".join(p["name"] for p in doc.get("properties", [])),
                " ".join(a["name"] for a in doc.get("attributes", [])),
            ]).lower()
            score = _score(query_l, haystack)
            if score:
                matches.append({
                    "kind": "symbol",
                    "name": name,
                    "module": doc.get("module", ""),
                    "summary": doc.get("doc", "").splitlines()[0] if doc.get("doc") else "",
                    "score": score,
                })
        for name, module in index["modules"].items():
            haystack = " ".join([name, module.get("path", ""), " ".join(module.get("symbols", []))]).lower()
            score = _score(query_l, haystack)
            if score:
                matches.append({"kind": "module", "name": name, "summary": module.get("path", ""), "score": score})
        if _search_needs_shader_scan(query_l):
            for shader in _scan_shaders():
                haystack = " ".join([
                    shader["shader_id"],
                    shader["kind"],
                    shader.get("path", ""),
                    " ".join(shader.get("imports", [])),
                ]).lower()
                score = _score(query_l, haystack)
                if score:
                    matches.append({
                        "kind": "shader",
                        "name": shader["shader_id"],
                        "shader_kind": shader["kind"],
                        "summary": shader.get("path", ""),
                        "score": score,
                    })
        matches.sort(key=lambda item: (-item["score"], item["kind"], item["name"]))
        return ok({"query": query, "matches": matches[:safe_limit], "search_mode": "static_index"})

    @mcp.tool(name="shader_guide")
    def shader_guide(topic: str = "") -> dict:
        """Return shader authoring rules, property annotation syntax, and examples."""
        guide = dict(SUBSYSTEM_GUIDES["shader"])
        guide["property_types"] = PROPERTY_TYPES
        guide["examples"] = _shader_examples()
        guide["knowledge_lock"] = issue_knowledge_token("shader", source_tool="shader_guide")
        if topic:
            guide["topic"] = topic
        return ok(guide)

    @mcp.tool(name="shader_catalog")
    def shader_catalog(kind: str = "", include_hidden: bool = False) -> dict:
        """List shader IDs from project and built-in shader roots."""
        shaders = [
            item
            for item in _scan_shaders()
            if (not kind or item["kind"] == kind or item["extension"].lstrip(".") == kind)
            and (include_hidden or not item.get("hidden"))
        ]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in shaders:
            grouped.setdefault(item["kind"], []).append(item)
        return ok({"shaders": shaders, "grouped": grouped, "property_types": PROPERTY_TYPES})

    @mcp.tool(name="shader_describe")
    def shader_describe(shader_id: str, kind: str = "") -> dict:
        """Describe a shader file, annotations, material properties, and usage."""
        shader_id_l = str(shader_id or "").strip().lower()
        candidates = [
            item for item in _scan_shaders()
            if item["shader_id"].lower() == shader_id_l and (not kind or item["kind"] == kind or item["extension"].lstrip(".") == kind)
        ]
        if not candidates:
            return ok({"found": False, "shader_id": shader_id, "available": [item["shader_id"] for item in _scan_shaders()]})
        return ok({"shader_id": shader_id, "matches": candidates, "usage": _shader_usage(candidates)})

    @mcp.tool(name="audio_guide")
    def audio_guide(topic: str = "") -> dict:
        """Return AudioSource/AudioListener/AudioClip usage guidance."""
        guide = dict(SUBSYSTEM_GUIDES["audio"])
        guide["examples"] = _audio_examples()
        guide["symbols"] = [_symbol_doc(name) for name in ("AudioSource", "AudioListener", "AudioClip")]
        guide["knowledge_lock"] = issue_knowledge_token("audio", source_tool="audio_guide")
        if topic:
            guide["topic"] = topic
        return ok(guide)


def _symbol_doc(symbol: str) -> dict[str, Any]:
    key = str(symbol or "").strip()
    index = _api_index()
    entry = index["symbols"].get(key)
    candidates = _symbol_candidates(index, key)
    if "." not in key and len(candidates) > 1:
        preferred = _preferred_symbol_candidate(candidates)
        if preferred is not None:
            entry = preferred
    if entry is None and "." in key:
        module_name, _, attr = key.rpartition(".")
        entry = index["symbols"].get(attr)
        if entry is not None and entry.get("module") != module_name:
            entry = None
    if entry is None:
        lowered = key.lower()
        matches = [item for name, item in index["symbols"].items() if name.lower() == lowered or item.get("qualname", "").lower() == lowered]
        if len(matches) == 1:
            entry = matches[0]
    if entry is None:
        return {}
    doc = dict(entry)
    if "." not in key and len(candidates) > 1:
        doc["ambiguous_short_name"] = True
        doc["alternatives"] = [
            {"name": item.get("name", ""), "qualname": item.get("qualname", ""), "module": item.get("module", ""), "source": item.get("source", "")}
            for item in candidates
        ]
        doc["recommendation"] = "Use api_get with the module-qualified name from alternatives for deterministic results."
    runtime = _runtime_symbol_doc(doc.get("module", ""), doc.get("name", ""))
    if runtime:
        doc.setdefault("runtime_doc", runtime.get("doc", ""))
        doc.setdefault("runtime_module", runtime.get("module", ""))
        if not doc.get("doc"):
            doc["doc"] = runtime.get("doc", "")
        if not doc.get("methods"):
            doc["methods"] = runtime.get("methods", [])
        if not doc.get("properties"):
            doc["properties"] = runtime.get("properties", [])
    return doc


def _symbol_candidates(index: dict[str, Any], key: str) -> list[dict[str, Any]]:
    lowered = str(key or "").lower()
    seen: set[str] = set()
    candidates = []
    for item in index["symbols"].values():
        qualname = str(item.get("qualname", ""))
        if qualname in seen:
            continue
        if str(item.get("name", "")).lower() == lowered or qualname.lower() == lowered:
            candidates.append(item)
            seen.add(qualname)
    return candidates


def _preferred_symbol_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    prefixes = (
        "Infernux.components.builtin.",
        "Infernux.components.",
        "Infernux.ui.",
        "Infernux.core.",
        "Infernux.renderstack.",
    )
    for prefix in prefixes:
        for item in candidates:
            if str(item.get("module", "")).startswith(prefix):
                return item
    return candidates[0] if candidates else None


def _runtime_symbol_doc(module_name: str, attr: str) -> dict[str, Any]:
    try:
        module = __import__(module_name, fromlist=[attr])
        obj = getattr(module, attr)
    except Exception:
        return {}
    methods = []
    properties = []
    for name, member in inspect.getmembers(obj):
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            properties.append({"name": name, "doc": inspect.getdoc(member) or ""})
        elif inspect.isfunction(member) or inspect.ismethod(member):
            try:
                signature = str(inspect.signature(member))
            except Exception:
                signature = "(...)"
            methods.append({"name": name, "signature": signature, "doc": inspect.getdoc(member) or ""})
    return {
        "name": attr,
        "module": module_name,
        "doc": inspect.getdoc(obj) or "",
        "methods": methods,
        "properties": properties,
    }


def _module_doc(name: str) -> dict[str, Any]:
    index = _api_index()
    module = index["modules"].get(str(name or "").strip())
    if module is None:
        lowered = str(name or "").strip().lower()
        matches = [item for key, item in index["modules"].items() if key.lower() == lowered]
        if len(matches) == 1:
            module = matches[0]
    return dict(module) if module else {}


def _api_index_status() -> dict[str, Any]:
    index = _API_INDEX
    if index is None:
        return {
            "state": "cold",
            "scope": "The static Python API index is built lazily by focused api_search/api_get requests.",
        }
    return {
        "state": "ready",
        "module_count": len(index["modules"]),
        "symbol_count": len(index["symbols"]),
        "stub_symbol_count": sum(1 for item in index["symbols"].values() if item.get("source") == "stub"),
    }


def _api_index() -> dict[str, Any]:
    global _API_INDEX
    if _API_INDEX is not None:
        return _API_INDEX
    with _API_INDEX_LOCK:
        if _API_INDEX is not None:
            return _API_INDEX
        modules: dict[str, Any] = {}
        symbols: dict[str, Any] = {}
        roots = _python_api_roots()
        seen_stems: set[str] = set()
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = _public_api_dirnames(dirnames)
                for filename in filenames:
                    if not filename.endswith(".pyi"):
                        continue
                    path = os.path.join(dirpath, filename)
                    module_name = _module_name_for_path(path)
                    seen_stems.add(os.path.splitext(path)[0])
                    _merge_module_api(modules, symbols, module_name, path, source="stub")
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = _public_api_dirnames(dirnames)
                for filename in filenames:
                    if not filename.endswith(".py") or filename.startswith("_"):
                        continue
                    path = os.path.join(dirpath, filename)
                    if os.path.splitext(path)[0] in seen_stems:
                        continue
                    module_name = _module_name_for_path(path)
                    _merge_module_api(modules, symbols, module_name, path, source="python")
        _API_INDEX = {"modules": modules, "symbols": symbols}
        return _API_INDEX


def _public_api_dirnames(names: list[str]) -> list[str]:
    excluded = {"__pycache__", ".mypy_cache", "mcp", "test"}
    return [name for name in names if name not in excluded and not name.startswith(".")]


def _python_api_roots() -> list[str]:
    return [resolved_path(os.path.join(os.path.dirname(__file__), "..", ".."))]


def _module_name_for_path(path: str) -> str:
    root = _python_api_roots()[0]
    package_parent = os.path.dirname(root)
    rel = relative_path(path, package_parent)
    module = os.path.splitext(rel)[0].replace(os.sep, ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module


def _merge_module_api(
    modules: dict[str, Any],
    symbols: dict[str, Any],
    module_name: str,
    path: str,
    *,
    source: str,
) -> None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            tree = ast.parse(f.read(), filename=path)
    except Exception:
        return
    module_doc = ast.get_docstring(tree) or ""
    module_symbols = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            entry = _class_entry(module_name, node, path, source)
            _store_symbol(symbols, entry)
            module_symbols.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            entry = _function_entry(module_name, node, path, source)
            _store_symbol(symbols, entry)
            module_symbols.append(node.name)
    modules[module_name] = {
        "name": module_name,
        "path": _project_rel(path),
        "source": source,
        "doc": module_doc,
        "symbols": sorted(module_symbols),
    }


def _store_symbol(symbols: dict[str, Any], entry: dict[str, Any]) -> None:
    name = entry["name"]
    existing = symbols.get(name)
    if (
        existing is None
        or _symbol_priority(entry) > _symbol_priority(existing)
        or (existing.get("source") != "stub" and entry.get("source") == "stub")
    ):
        symbols[name] = entry
    symbols[entry["qualname"]] = entry


def _symbol_priority(entry: dict[str, Any]) -> int:
    module = str(entry.get("module", ""))
    if module.startswith("Infernux.components.builtin."):
        return 50
    if module.startswith("Infernux.components."):
        return 40
    if module.startswith("Infernux.ui."):
        return 35
    if module.startswith("Infernux.core."):
        return 30
    if module.startswith("Infernux.renderstack."):
        return 25
    if module.startswith("Infernux.lib."):
        return 10
    return 0


def _class_entry(module_name: str, node: ast.ClassDef, path: str, source: str) -> dict[str, Any]:
    methods = []
    properties = []
    attributes = []
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and not item.target.id.startswith("_"):
            attributes.append({
                "name": item.target.id,
                "type": _expr_to_source(item.annotation),
                "doc": _inline_attribute_doc(node.body, item),
            })
            continue
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    attributes.append({
                        "name": target.id,
                        "type": "",
                        "doc": _inline_attribute_doc(node.body, item),
                    })
            continue
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) or item.name.startswith("_"):
            continue
        decorators = [_decorator_name(dec) for dec in item.decorator_list]
        if "property" in decorators:
            properties.append({"name": item.name, "type": _return_annotation(item), "doc": ast.get_docstring(item) or ""})
        elif any(dec.endswith(".setter") for dec in decorators):
            continue
        else:
            methods.append({"name": item.name, "signature": _signature_from_ast(item), "doc": ast.get_docstring(item) or ""})
    return {
        "name": node.name,
        "qualname": f"{module_name}.{node.name}",
        "module": module_name,
        "kind": "class",
        "source": source,
        "path": _project_rel(path),
        "doc": ast.get_docstring(node) or "",
        "bases": [_expr_to_source(base) for base in node.bases],
        "attributes": attributes,
        "methods": methods,
        "properties": properties,
    }


def _function_entry(module_name: str, node: ast.FunctionDef | ast.AsyncFunctionDef, path: str, source: str) -> dict[str, Any]:
    return {
        "name": node.name,
        "qualname": f"{module_name}.{node.name}",
        "module": module_name,
        "kind": "function",
        "source": source,
        "path": _project_rel(path),
        "doc": ast.get_docstring(node) or "",
        "signature": _signature_from_ast(node),
        "attributes": [],
        "methods": [],
        "properties": [],
    }


def _inline_attribute_doc(body: list[ast.stmt], item: ast.stmt) -> str:
    try:
        index = body.index(item)
    except ValueError:
        return ""
    if index + 1 >= len(body):
        return ""
    next_item = body[index + 1]
    if isinstance(next_item, ast.Expr) and isinstance(next_item.value, ast.Constant) and isinstance(next_item.value.value, str):
        return str(next_item.value.value)
    return ""


def _signature_from_ast(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = []
    positional = list(node.args.posonlyargs) + list(node.args.args)
    defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
    for arg, default in zip(positional, defaults):
        args.append(_format_arg(arg, default))
    if node.args.vararg:
        args.append("*" + _format_arg(node.args.vararg, None))
    elif node.args.kwonlyargs:
        args.append("*")
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        args.append(_format_arg(arg, default))
    if node.args.kwarg:
        args.append("**" + _format_arg(node.args.kwarg, None))
    ret = _return_annotation(node)
    return f"({', '.join(args)})" + (f" -> {ret}" if ret else "")


def _format_arg(arg: ast.arg, default: ast.expr | None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {_expr_to_source(arg.annotation)}"
    if default is not None:
        text += f" = {_expr_to_source(default)}"
    return text


def _return_annotation(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return _expr_to_source(node.returns) if node.returns is not None else ""


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _decorator_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _expr_to_source(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def _scan_shaders() -> list[dict[str, Any]]:
    from Infernux.engine.ui import inspector_shader_utils as shader_utils

    results = []
    for root in shader_utils._get_shader_search_roots():
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, filenames in os.walk(root):
            for filename in filenames:
                ext = os.path.splitext(filename)[1].lower()
                if ext not in {".vert", ".frag", ".glsl", ".shadingmodel"}:
                    continue
                path = os.path.join(dirpath, filename)
                annotations = _parse_shader_annotations(path)
                shader_id = annotations.get("shader_id") or os.path.splitext(filename)[0]
                kind = {
                    ".vert": "vertex",
                    ".frag": "fragment",
                    ".glsl": "library",
                    ".shadingmodel": "shading_model",
                }[ext]
                results.append({
                    "shader_id": shader_id,
                    "kind": kind,
                    "extension": ext,
                    "path": _project_rel(path),
                    "hidden": annotations.get("hidden", False),
                    "properties": shader_utils.parse_shader_properties(path) if ext in {".vert", ".frag"} else [],
                    "imports": annotations.get("imports", []),
                    "requirements": annotations.get("requirements", []),
                    "unsupported": annotations.get("unsupported", []),
                    "targets": annotations.get("targets", []),
                    "shading_model": annotations.get("shading_model", ""),
                    "queue": annotations.get("queue", ""),
                })
    results.sort(key=lambda item: (item["kind"], item["shader_id"], item["path"]))
    return results


def _parse_shader_annotations(path: str) -> dict[str, Any]:
    try:
        from Infernux.core.asset_types import read_meta_file

        metadata = read_meta_file(path)
    except (ImportError, OSError, TypeError, ValueError):
        metadata = None

    if isinstance(metadata, dict) and str(metadata.get("shader_id") or "").strip():
        def _json_value(key: str, fallback):
            encoded = metadata.get(key)
            if not isinstance(encoded, str):
                return fallback
            try:
                import json

                value = json.loads(encoded)
            except (TypeError, ValueError):
                return fallback
            return value

        imports = _json_value("shader_imports", [])
        requirements = _json_value("shader_requirements", [])
        capabilities = _json_value("shader_capabilities", [])
        unsupported = _json_value("shader_unsupported", [])
        inputs = _json_value("shader_inputs", [])
        outputs = _json_value("shader_outputs", [])
        entries = _json_value("shader_entries", {})
        return {
            "shader_id": str(metadata["shader_id"]).strip(),
            "hidden": bool(metadata.get("shader_hidden", False)),
            "imports": imports if isinstance(imports, list) else [],
            "requirements": requirements if isinstance(requirements, list) else [],
            "targets": list(entries) if isinstance(entries, dict) else [],
            "entries": entries if isinstance(entries, dict) else {},
            "capabilities": capabilities if isinstance(capabilities, list) else [],
            "unsupported": unsupported if isinstance(unsupported, list) else [],
            "inputs": inputs if isinstance(inputs, list) else [],
            "outputs": outputs if isinstance(outputs, list) else [],
            "shading_model": str(metadata.get("shader_lighting_type") or ""),
            "queue": metadata.get("shader_queue", ""),
            "schema_format": str(metadata.get("shader_schema_format") or ""),
        }

    # Structured source fallback for assets that have not been imported yet.
    from Infernux.engine.ui.inspector_shader_utils import _read_source_shader_metadata

    source = _read_source_shader_metadata(path)
    entries = source.get("entries", {})
    return {
        "shader_id": source.get("shader_id", ""),
        "hidden": bool(source.get("shader_hidden", False)),
        "imports": source.get("imports", []),
        "requirements": source.get("requirements", []),
        "targets": list(entries) if isinstance(entries, dict) else [],
        "entries": entries if isinstance(entries, dict) else {},
        "capabilities": source.get("capabilities", []),
        "unsupported": source.get("unsupported", []),
        "inputs": [],
        "outputs": [],
        "shading_model": source.get("shading_model", ""),
        "queue": source.get("queue", ""),
        "schema_format": "ShaderInfo" if source else "",
    }


def _shader_usage(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = {item["kind"] for item in candidates}
    usage = {
        "material_binding": [],
        "notes": [],
        "next_tools": ["shader_catalog", "asset_create_builtin_resource", "asset_write_text", "asset_refresh", "material_create", "material_set_shader", "material_set_render_queue"],
    }
    if "vertex" in kinds:
        usage["material_binding"].append("material.vert_shader_name = '<shader_id>'")
    if "fragment" in kinds:
        usage["material_binding"].append("material.frag_shader_name = '<shader_id>'")
    if "shading_model" in kinds:
        usage["notes"].append("Reference this from a .frag ShaderInfo block with ShadingModel \"<shader_id>\"; do not bind it directly as a material fragment shader.")
    if "library" in kinds:
        usage["notes"].append("Name this library in a ShaderInfo Imports list; do not bind it directly to Material.")
    return usage


def _shader_examples() -> dict[str, str]:
    return {
        "surface_fragment": (
            "#version 450\n\n"
            "ShaderInfo {\n"
            "    Name \"my_unlit\"\n"
            "    ShadingModel \"unlit\"\n"
            "    Surface Opaque\n"
            "    Queue 2000\n"
            "    Properties {\n"
            "        Color baseColor = [1.0, 0.8, 0.4, 1.0]\n"
            "        Texture2D texSampler = white\n"
            "    }\n"
            "}\n\n"
            "void surface(out SurfaceData s) {\n"
            "    s = InitSurfaceData();\n"
            "    vec4 texColor = texture(texSampler, v_TexCoord);\n"
            "    s.albedo = texColor.rgb * material.baseColor.rgb;\n"
            "    s.alpha = texColor.a * material.baseColor.a;\n"
            "}\n"
        ),
        "material_binding": (
            "from Infernux.core.material import Material\n"
            "mat = Material.create_unlit('MyMat')\n"
            "mat.vert_shader_name = 'Standard'\n"
            "mat.frag_shader_name = 'my_unlit'\n"
            "mat.set_color('baseColor', 1.0, 0.8, 0.4, 1.0)\n"
        ),
        "shading_model": (
            "ShadingModelInfo {\n"
            "    Name \"my_lighting\"\n"
            "    Imports [\"Lighting\"]\n"
            "    Requires [Lighting]\n"
            "}\n\n"
            "void shading(in SurfaceData s, out vec4 color) {\n"
            "    color = vec4(s.albedo + s.emission, s.alpha);\n"
            "}\n"
        ),
    }


def _audio_examples() -> dict[str, str]:
    return {
        "multi_track": (
            "from Infernux.components.builtin import AudioSource, AudioListener\n"
            "from Infernux.core.audio_clip import AudioClip\n\n"
            "source = self.game_object.get_component(AudioSource)\n"
            "source.track_count = 2\n"
            "bgm = AudioClip.load('Assets/Audio/bgm.wav')\n"
            "sfx = AudioClip.load('Assets/Audio/click.wav')\n"
            "source.set_track_clip(0, bgm)\n"
            "source.set_track_clip(1, sfx)\n"
            "source.loop = True\n"
            "source.play(0)\n"
            "source.play_one_shot(sfx, 0.8)\n"
        ),
        "listener": "Attach AudioListener to the main camera GameObject; do not create multiple active listeners.",
    }


def _score(query: str, haystack: str) -> int:
    tokens = [token for token in query.split() if token]
    if not tokens:
        return 1
    return sum(1 for token in tokens if token in haystack)


def _search_needs_shader_scan(query: str) -> bool:
    shader_terms = ("shader", "glsl", "fragment", "vertex", "shading", "shadingmodel")
    return any(term in query for term in shader_terms)


def _agent_api_guidance() -> list[str]:
    return [
        "Infernux is new and changes quickly. Do not infer unknown APIs from Unity; query them first.",
        "For an ordinary behavior script, start with api_get('scripting') and api_get('input') for stable imports and input conventions.",
        "Use api_search(query) for Python/stub-backed APIs, then api_get(symbol_or_module) for signatures and docstrings.",
        "Use component_describe_type(component_type) before component_set_field/component_set_fields.",
        "Use shader_guide, shader_catalog, and shader_describe for shader authoring because shader behavior is C++/schema/compiler-backed.",
        "When a guide returns data.knowledge_lock.token, pass that token as knowledge_token to gated write tools for that subsystem.",
        "Use mcp_catalog_search or mcp_catalog_recommend before selecting MCP tools for unfamiliar tasks.",
    ]


def _project_rel(path: str) -> str:
    try:
        from Infernux.engine.project_context import get_project_root
        root = get_project_root()
        if root:
            return relative_path(path, root, allow_root=True)
    except Exception:
        pass
    return portable_path(str(path))


def _register_metadata() -> None:
    for name, summary, category, tags in [
        ("api_subsystems", "List documented engine subsystems and API entry points.", "foundation/api", ["api", "subsystem", "docs"]),
        ("api_get", "Return a subsystem guide or symbol API page.", "foundation/api", ["api", "docs", "symbol"]),
        ("api_search", "Search subsystem guides, symbols, shader IDs, and component names.", "foundation/api", ["api", "search"]),
        ("shader_guide", "Return shader authoring rules and examples.", "shader/guide", ["shader", "guide", "glsl"]),
        ("shader_catalog", "List project and built-in shader IDs.", "shader/catalog", ["shader", "catalog", "vertex", "fragment", "shadingmodel"]),
        ("shader_describe", "Describe shader annotations, properties, and material binding usage.", "shader/catalog", ["shader", "properties", "material"]),
        ("audio_guide", "Return AudioSource, AudioListener, and AudioClip usage guidance.", "audio/guide", ["audio", "guide", "script"]),
    ]:
        register_tool_metadata(
            name,
            summary=summary,
            category=category,
            tags=tags,
            level="foundation" if name.startswith("api.") else "semantic",
            aliases=["script api", "engine api", "how to use", "查询API"] + tags,
            next_suggested_tools=["api_search", "api_get", "component_describe_type"],
        )
