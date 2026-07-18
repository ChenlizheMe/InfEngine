"""
Infernux RenderStack Module

Scene-level rendering configuration system.
Provides a composable, per-scene rendering stack where users can mount
arbitrary render passes to pipeline-defined injection points.

Core classes:
    - **RenderStack**: Scene-singleton component managing pipeline + passes
    - **RenderPipeline**: Topology skeleton with named injection points
    - **RenderPass**: Base class for mountable rendering steps
    - **GeometryPass**: Scene geometry drawing (outline, decal, etc.)
    - **ResourceBus**: Transient resource handle dictionary
    - **InjectionPoint**: Named slot in the pipeline topology

Architecture::

    Scene
    └── RenderStack (InxComponent)
        ├── selected_pipeline: RenderPipeline
        │   └── define_topology(graph, bus, on_injection_point)
        └── pass_entries: [PassEntry, ...]
            └── each: RenderPass.inject(graph, bus)

Quick start::

    from Infernux.renderstack import RenderStack

    # Mount to scene's RenderStack
    stack = game_object.add_component(RenderStack)

See Also:
    - ``docs/design/RenderStack_Design.md`` for the full design document
"""

from Infernux.renderstack.injection_point import InjectionPoint
from Infernux.renderstack.effect_stage import EffectResourceContract, EffectScope, EffectStage
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.effect_binding import (
    EffectBindingDocument,
    EffectSlotBinding,
    dump_effect_binding_document,
    parse_effect_binding_document,
)
from Infernux.renderstack.render_effect_asset import (
    EffectAssetReference,
    RenderEffectAsset,
    RenderEffectGroupAsset,
    RenderEffectGroupEntry,
    direct_effect_dependencies,
    dump_render_effect_document,
    parse_render_effect_document,
)
from Infernux.renderstack.render_effect_compiler import (
    RenderEffectArtifact,
    RenderEffectArtifactRegistry,
    RenderEffectCompileError,
    RenderEffectFeature,
    get_render_effect_feature,
    register_render_effect_feature,
)
from Infernux.renderstack.resource_bus import ResourceBus
from Infernux.renderstack.render_pass import RenderPass
from Infernux.renderstack.render_pipeline import RenderPipeline, RenderPipelineAsset
from Infernux.renderstack.geometry_pass import GeometryPass
from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.renderstack.bloom_effect import BloomEffect
from Infernux.renderstack.tonemapping_effect import ToneMappingEffect
from Infernux.renderstack.vignette_effect import VignetteEffect
from Infernux.renderstack.color_adjustments_effect import ColorAdjustmentsEffect
from Infernux.renderstack.chromatic_aberration_effect import ChromaticAberrationEffect
from Infernux.renderstack.film_grain_effect import FilmGrainEffect
from Infernux.renderstack.white_balance_effect import WhiteBalanceEffect
from Infernux.renderstack.sharpen_effect import SharpenEffect
from Infernux.renderstack.render_stack import RenderStack, PassEntry
from Infernux.renderstack.render_stack_pipeline import RenderStackPipeline
from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline
from Infernux.renderstack.default_deferred_pipeline import DefaultDeferredPipeline
from Infernux.renderstack.discovery import discover_pipelines, discover_passes

__all__ = [
    # Core
    "RenderStack",
    "PassEntry",
    "RenderPipeline",
    "RenderPipelineAsset",
    "RenderStackPipeline",
    "DefaultForwardPipeline",
    "DefaultDeferredPipeline",
    # Injection points
    "InjectionPoint",
    "EffectStage",
    "EffectScope",
    "EffectResourceContract",
    "EffectSlot",
    "RenderEffect",
    "EffectBindingDocument",
    "EffectSlotBinding",
    "parse_effect_binding_document",
    "dump_effect_binding_document",
    "EffectAssetReference",
    "RenderEffectAsset",
    "RenderEffectGroupAsset",
    "RenderEffectGroupEntry",
    "parse_render_effect_document",
    "dump_render_effect_document",
    "direct_effect_dependencies",
    "RenderEffectCompileError",
    "RenderEffectArtifact",
    "RenderEffectArtifactRegistry",
    "RenderEffectFeature",
    "get_render_effect_feature",
    "register_render_effect_feature",
    # Resource bus
    "ResourceBus",
    # Pass base classes
    "RenderPass",
    "GeometryPass",
    "FullScreenEffect",
    # Built-in effects
    "BloomEffect",
    "ToneMappingEffect",
    "VignetteEffect",
    "ColorAdjustmentsEffect",
    "ChromaticAberrationEffect",
    "FilmGrainEffect",
    "WhiteBalanceEffect",
    "SharpenEffect",
    # Discovery
    "discover_pipelines",
    "discover_passes",
]
