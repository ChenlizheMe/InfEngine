"""Authoritative Vulkan-to-WebGPU product capability inventory."""

from __future__ import annotations

import copy
from collections.abc import Mapping


WEBGPU_CAPABILITY_SCHEMA = "infernux.webgpu_capability_inventory"
WEBGPU_CAPABILITY_FILENAME = "infernux-webgpu-capabilities.json"

_STATUSES = frozenset({"equivalent", "different-but-validated", "unsupported"})
_PARITY_GATES = frozenset({"closed", "open", "not-required"})

_FEATURES: tuple[dict[str, object], ...] = (
    {
        "id": "render.pbr-material",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebSceneRenderer",
        "difference": "The Web host consumes the shared scene publication through a fixed WGSL PBR pipeline; the native 1280x720 fixed-frame full-scene and character-region comparisons pass.",
        "validation": [
            "web.contract.scene-publication",
            "web.smoke.scene-frame",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "animation.skeletal",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebSceneRenderer",
        "difference": "WebGPU consumes published skinned vertices; the native-resolution deterministic character-region comparison passes.",
        "validation": [
            "web.contract.skinned-scene",
            "web.smoke.animation-clock",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "sky.procedural",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebSceneRenderer",
        "difference": "The WebGPU sky shader is backend-native but uses the same camera and scene publication semantics.",
        "validation": ["web.smoke.sky-pixel-difference"],
    },
    {
        "id": "shadow.directional",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebSceneRenderer",
        "difference": "The WebGPU host implements the directional shadow pass independently; the native-resolution fixed-frame cross-backend tolerance passes.",
        "validation": [
            "web.smoke.shadow-pixel-difference",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "render.transparent",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebSceneRenderer",
        "difference": "Transparent draw ordering and blend state are translated by the Web host rather than the Vulkan pipeline compiler; the 040 line and particle regions pass the fixed-frame gate.",
        "validation": [
            "web.contract.transparent-pass",
            "web.smoke.scene-frame",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "render.line-renderer",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebSceneRenderer",
        "difference": "Published line geometry is rendered by the WebGPU transparent path; dynamic geometry, depth and native-resolution pixel parity pass for the 040 trajectory.",
        "validation": [
            "web.smoke.line-runtime-state",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "particles.gpu-sprite",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebParticleRuntime",
        "difference": "Portable particle kernels are lowered to WGSL and rendered by a WebGPU sprite pipeline; fixed-frame testing proves HDR Bloom participation and the 040 particle shape/color region passes tolerance.",
        "validation": [
            "web.contract.particle-catalog",
            "web.smoke.particle-clock",
            "web.smoke.particle-hdr-bloom-fixed-frame",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "ui.screen",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebScreenUIRenderer",
        "difference": "The shared engine draw list is rasterized directly into the WebGPU backbuffer without a DOM visual layer.",
        "validation": ["web.smoke.screen-ui-pixels", "web.smoke.pointer-hit"],
    },
    {
        "id": "post.bloom-hdr",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebPostProcessRenderer",
        "difference": "The WebGPU post chain owns HDR intermediates and bloom kernels; native-resolution fixed-exposure particle and emissive-region comparisons pass.",
        "validation": [
            "web.contract.hdr-format",
            "web.smoke.post-process",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "post.aces",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebPostProcessRenderer",
        "difference": "ACES is implemented in backend-native WGSL; fixed-exposure native-resolution cross-backend color tolerance passes.",
        "validation": [
            "web.contract.aces-pass",
            "web.smoke.post-process",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "present.orientation-srgb",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebPostProcessRenderer",
        "difference": "The Web host applies the explicit Vulkan-to-Web clip-space transform and browser-surface display encoding.",
        "validation": ["web.smoke.frame-orientation", "web.smoke.non-black-present"],
    },
    {
        "id": "material.custom-surface",
        "status": "unsupported",
        "required_for_040": False,
        "parity_gate": "not-required",
        "owner": "WebSceneRenderer",
        "reason": "The Web scene renderer does not yet consume cooked custom surface shader variants.",
        "validation": ["web.contract.capability-inventory"],
    },
    {
        "id": "material.toon",
        "status": "unsupported",
        "required_for_040": False,
        "parity_gate": "not-required",
        "owner": "WebSceneRenderer",
        "reason": "The Web scene renderer currently exposes only its PBR material pipeline.",
        "validation": ["web.contract.capability-inventory"],
    },
    {
        "id": "material.alpha-clip",
        "status": "unsupported",
        "required_for_040": False,
        "parity_gate": "not-required",
        "owner": "WebSceneRenderer",
        "reason": "The current WebGPU material ABI has no authored alpha-clip contract.",
        "validation": ["web.contract.capability-inventory"],
    },
    {
        "id": "lighting.point-lights",
        "status": "different-but-validated",
        "required_for_040": True,
        "parity_gate": "closed",
        "owner": "WebSceneRenderer",
        "difference": "Point-light evaluation is implemented in the backend-native WGSL scene path; both authored 040 point lights are active in the passing native-resolution fixed-frame comparison.",
        "validation": [
            "web.contract.point-light-publication",
            "web.smoke.vulkan-webgpu-fixed-frame",
        ],
    },
    {
        "id": "lighting.spot-lights",
        "status": "unsupported",
        "required_for_040": False,
        "parity_gate": "not-required",
        "owner": "WebSceneRenderer",
        "reason": "The WGSL path contains spot-light evaluation, but no dedicated visibility and cross-backend product gate validates it yet.",
        "validation": ["web.contract.capability-inventory"],
    },
    {
        "id": "shadow.local-lights",
        "status": "unsupported",
        "required_for_040": False,
        "parity_gate": "not-required",
        "owner": "WebSceneRenderer",
        "reason": "Point and spot shadow passes are not implemented by the WebGPU host.",
        "validation": ["web.contract.capability-inventory"],
    },
    {
        "id": "renderstack.full-effect-catalog",
        "status": "unsupported",
        "required_for_040": False,
        "parity_gate": "not-required",
        "owner": "WebPostProcessRenderer",
        "reason": "Only the 040 Bloom and ACES chain is implemented in the WebGPU host.",
        "validation": ["web.contract.capability-inventory"],
    },
)


def webgpu_capability_inventory() -> dict[str, object]:
    """Return an isolated copy of the product capability contract."""

    features = copy.deepcopy(list(_FEATURES))
    return {
        "$schema": WEBGPU_CAPABILITY_SCHEMA,
        "backend": "webgpu",
        "reference_backend": "vulkan",
        "policy": "fire-forced",
        "required_feature_ids": [
            str(feature["id"])
            for feature in features
            if feature["required_for_040"]
        ],
        "features": features,
    }


def validate_webgpu_capability_inventory(document: Mapping[str, object]) -> None:
    """Reject an incomplete or misleading WebGPU product declaration."""

    if set(document) != {
        "$schema",
        "backend",
        "reference_backend",
        "policy",
        "required_feature_ids",
        "features",
    } or document.get("$schema") != WEBGPU_CAPABILITY_SCHEMA:
        raise ValueError("WebGPU capability inventory does not match the current contract")
    if document.get("backend") != "webgpu" or document.get("reference_backend") != "vulkan":
        raise ValueError("WebGPU capability inventory has an invalid backend contract")
    if document.get("policy") != "fire-forced":
        raise ValueError("WebGPU capability inventory must declare fire-forced policy")

    raw_features = document.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise ValueError("WebGPU capability inventory has no feature records")
    features: dict[str, Mapping[str, object]] = {}
    for raw_feature in raw_features:
        if not isinstance(raw_feature, Mapping):
            raise ValueError("WebGPU capability feature records must be objects")
        feature_id = str(raw_feature.get("id", "")).strip()
        if not feature_id or feature_id in features:
            raise ValueError("WebGPU capability feature IDs must be unique and non-empty")
        status = str(raw_feature.get("status", ""))
        if status not in _STATUSES:
            raise ValueError(f"WebGPU capability {feature_id} has an invalid status")
        parity_gate = str(raw_feature.get("parity_gate", ""))
        if parity_gate not in _PARITY_GATES:
            raise ValueError(f"WebGPU capability {feature_id} has an invalid parity gate")
        if not str(raw_feature.get("owner", "")).strip():
            raise ValueError(f"WebGPU capability {feature_id} has no owner")
        validation = raw_feature.get("validation")
        if not isinstance(validation, list) or not validation or any(
            not isinstance(item, str) or not item.strip() for item in validation
        ):
            raise ValueError(f"WebGPU capability {feature_id} has no validation evidence")
        if status == "different-but-validated" and not str(
            raw_feature.get("difference", "")
        ).strip():
            raise ValueError(f"WebGPU capability {feature_id} has no documented difference")
        if status == "unsupported" and not str(raw_feature.get("reason", "")).strip():
            raise ValueError(f"Unsupported WebGPU capability {feature_id} has no reason")
        features[feature_id] = raw_feature

    required_ids = document.get("required_feature_ids")
    if not isinstance(required_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in required_ids
    ):
        raise ValueError("WebGPU capability inventory has invalid required feature IDs")
    if len(required_ids) != len(set(required_ids)):
        raise ValueError("WebGPU capability inventory repeats a required feature ID")
    for feature_id in required_ids:
        feature = features.get(feature_id)
        if feature is None or feature.get("required_for_040") is not True:
            raise ValueError(f"Required WebGPU capability {feature_id} has no required record")
        if feature.get("status") == "unsupported":
            raise ValueError(f"Required WebGPU capability {feature_id} is unsupported")
        if feature.get("parity_gate") != "closed":
            raise ValueError(
                f"Required WebGPU capability {feature_id} has an open parity gate"
            )

    declared_required = {
        feature_id
        for feature_id, feature in features.items()
        if feature.get("required_for_040") is True
    }
    if declared_required != set(required_ids):
        raise ValueError("WebGPU required feature list disagrees with feature records")
