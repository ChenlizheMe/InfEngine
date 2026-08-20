"""
FullScreenEffect — Multi-pass fullscreen post-processing effect base class.

A FullScreenEffect is a higher-level abstraction above ``RenderPass`` that
represents a **complete, parameterised, multi-pass fullscreen effect** such
as Bloom, Vignette, or SSAO.

Subclass hierarchy::

    RenderPass
    └── FullScreenEffect          (this module)
        ├── BloomEffect           (built-in)
        └── ...user-defined...

Subclass contract:
    1. Define ``name``, ``injection_point``, ``default_order``
    2. Declare tuneable parameters via ``serialized_field``
    3. Implement ``setup_passes(graph, bus)`` — inject all passes into the graph
    4. Optionally implement ``get_shader_list()`` for validation / precompilation

Integration with RenderStack:
    FullScreenEffect inherits RenderPass, so it is transparently discovered,
    mounted, validated, and serialised by the existing RenderStack machinery.
    ``inject()`` delegates to ``setup_passes()`` — subclasses override
    ``setup_passes`` instead of ``inject``.

Parameter serialization:
    Uses the same ``serialized_field`` / ``__init_subclass__`` mechanism as
    ``RenderPipeline``.  Parameters are persisted in the scene JSON via
    ``RenderStack.on_before_serialize()`` and restored by
    ``on_after_deserialize()``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Dict, List, Mapping, Set, TYPE_CHECKING

from Infernux.renderstack._pipeline_common import (
    COLOR_TEXTURE,
    DEPTH_TEXTURE,
    MOTION_TEXTURE,
    NORMAL_TEXTURE,
)
from Infernux.renderstack.render_pass import RenderPass
from Infernux.renderstack._serialized_field_mixin import SerializedFieldCollectorMixin
from Infernux.debug import Debug

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.rendergraph.graph import Format
    from Infernux.renderstack.resource_bus import ResourceBus


class EffectColorComposition(str, Enum):
    """How an effect contributes its output to the current image set."""

    REPLACE = "replace"
    ALPHA_OVER = "alpha_over"


class FullScreenEffect(SerializedFieldCollectorMixin, RenderPass):
    """Base class for multi-pass fullscreen post-processing effects.

    Subclasses must define:
        - ``name``: globally unique effect name
        - ``injection_point``: target injection point such as
          ``"before_post_process"``
        - ``default_order``: ordering value within the injection point

    Subclasses may optionally define:
        - ``menu_path``: editor menu category path such as
          ``"Post-processing/Bloom"``

    Subclasses declare tunable parameters with ``serialized_field``::

        class BloomEffect(FullScreenEffect):
            menu_path = "Post-processing/Bloom"
            threshold: float = serialized_field(default=1.0, range=(0, 10))
            intensity: float = serialized_field(default=0.5, range=(0, 3))

    Subclasses implement ``setup_passes(graph, bus)`` to inject all render
    passes needed for the effect.
    """

    # ---- Default resource declarations ----
    requires: ClassVar[Set[str]] = {"color"}
    modifies: ClassVar[Set[str]] = {"color"}

    # ---- Optional editor menu path ----
    menu_path: ClassVar[str] = ""

    # Route isolation and composition are separate RenderStack concerns. A
    # fullscreen effect replaces the color flowing through its own stage.
    color_composition: ClassVar[EffectColorComposition] = EffectColorComposition.REPLACE

    # ---- Reserved attrs for the mixin ----
    _reserved_attrs_ = frozenset({
        "name", "injection_point", "default_order", "menu_path",
        "requires", "modifies", "creates", "enabled", "color_composition",
    })

    # ---- Class-level serialized field metadata ----
    _serialized_fields_: ClassVar[Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Instance init
    # ------------------------------------------------------------------

    def __init__(self, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        # Prime instance storage for serialized fields
        from Infernux.components.fields import get_serialized_fields
        for field_name, meta in get_serialized_fields(self.__class__).items():
            if not hasattr(self, f"_sf_{field_name}"):
                try:
                    setattr(self, field_name, meta.default)
                except (AttributeError, TypeError) as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    pass

    # ==================================================================
    # Texture helper — eliminates per-effect _tex() boilerplate
    # ==================================================================

    @staticmethod
    def get_or_create_texture(
        graph: "RenderGraph",
        name: str,
        *,
        format: "Format" = None,
        camera_target: bool = False,
        size=None,
        size_divisor: int = 0,
    ):
        """Return an existing texture from *graph*, or create a new one.

        The ``*`` makes the configuration arguments keyword-only, so call
        sites stay readable::

            _tex(graph, "_bloom_mip0", format=Format.RGBA16_SFLOAT, size_divisor=2)

        Args:
            graph: Render graph that owns the texture resource.
            name: Graph-local texture alias.
            format: Texture format. ``None`` uses ``RenderGraph.create_texture``
                default format.
            camera_target: Whether this texture is the camera's main color
                target.
            size: Explicit texture size ``(width, height)``.
            size_divisor: Resolution scale divisor relative to the scene size.
        """
        existing = graph.get_texture(name)
        if existing is not None:
            return existing

        create_kwargs = {
            "camera_target": camera_target,
            "size": size,
            "size_divisor": size_divisor,
        }
        if format is not None:
            create_kwargs["format"] = format

        return graph.create_texture(name, **create_kwargs)

    def apply_single_source_effect(
        self,
        graph: "RenderGraph",
        bus: "ResourceBus",
        *,
        output_name: str,
        pass_name: str,
        shader_name: str,
        format: "Format",
        params: Mapping[str, object] | None = None,
    ) -> bool:
        """Build a one-pass fullscreen effect that reads and rewrites scene color.

        This is the common pattern used by most post-processing passes:
        read the current scene color from the bus, render into a temporary
        target, and publish the new color handle back into the bus.
        """
        color_in = bus.get(COLOR_TEXTURE)
        if color_in is None:
            return False

        color_out = self.get_or_create_texture(graph, output_name, format=format)

        with graph.add_pass(pass_name) as p:
            p.set_texture("_SourceTex", color_in)
            p.write_color(color_out)
            for param_name, param_value in (params or {}).items():
                p.set_param(param_name, param_value)
            p.fullscreen_quad(shader_name)

        bus.set(COLOR_TEXTURE, color_out)
        return True

    def bind_buffers(
        self,
        render_pass,
        bus: "ResourceBus",
        *,
        extra_bindings: Mapping[str, object] | None = None,
    ) -> None:
        """Bind named buffers supplied by the current explicit PassResult.

        Shader binding names remain stable, but their resources are selected
        from the current source-scoped result rather than global camera state.
        """
        semantic_bindings = (
            ("_InxPassColor", COLOR_TEXTURE),
            ("_InxPassDepth", DEPTH_TEXTURE),
            ("_InxPassNormal", NORMAL_TEXTURE),
            ("_InxPassMotion", MOTION_TEXTURE),
        )
        required = set(getattr(self, "requires", ())) | set(getattr(self, "modifies", ()))
        selected = tuple(
            (shader_name, semantic)
            for shader_name, semantic in semantic_bindings
            if semantic in required
        )
        missing = [semantic for _, semantic in selected if bus.get(semantic) is None]
        if missing:
            raise RuntimeError(
                "effect requires unavailable buffers from its PassResult: "
                + ", ".join(missing)
            )
        render_pass.set_textures(
            {shader_name: bus.get(semantic) for shader_name, semantic in selected}
        )
        if extra_bindings:
            render_pass.set_textures(extra_bindings)

    # ==================================================================
    # Core interface — subclasses implement these
    # ==================================================================

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        """Inject all render passes for this effect into the graph.

        Subclasses should:
        1. Read input resources from ``bus``
        2. Create intermediate textures as needed
        3. Add passes in execution order
        4. Write modified resources back to ``bus``

        Args:
            graph: RenderGraph currently being built.
            bus: Resource bus for the effect.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement setup_passes()"
        )

    def get_shader_list(self) -> List[str]:
        """Return the shader ids used by this effect.

        This is used for editor-time validation and future shader
        precompilation or caching.

        Returns:
            Shader ids such as
            ``["Bloom Prefilter", "Bloom Downsample", ...]``.
        """
        return []

    # ==================================================================
    # inject() — bridge to RenderStack
    # ==================================================================

    def inject(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        """Entry point used by RenderStack.

        This delegates to ``setup_passes()``. Subclasses should override
        ``setup_passes()`` instead of this method.
        """
        if not self.enabled:
            return
        self.setup_passes(graph, bus)

    # ==================================================================
    # Serialization helpers
    # ==================================================================

    def get_params_dict(self) -> Dict[str, Any]:
        """Export the current parameters as a JSON-serializable dict."""
        from Infernux.components.fields import get_serialized_fields
        from enum import Enum

        params: Dict[str, Any] = {}
        for field_name in get_serialized_fields(self.__class__):
            value = getattr(self, field_name, None)
            if isinstance(value, Enum):
                params[field_name] = {"__enum_name__": value.name}
            else:
                params[field_name] = value
        return params

    def set_params_dict(self, params: Dict[str, Any]) -> None:
        """Restore parameters from a dict."""
        from Infernux.components.fields import get_serialized_fields, FieldType

        fields = get_serialized_fields(self.__class__)
        if type(params) is not dict:
            raise TypeError(f"{type(self).__name__} parameters must be an object")
        self._inf_deserializing = True
        try:
            for field_name, value in params.items():
                meta = fields.get(field_name)
                if meta is None:
                    raise ValueError(f"{type(self).__name__} has no parameter '{field_name}'")
                if meta.field_type == FieldType.ENUM:
                    enum_cls = meta.enum_type
                    if enum_cls is None:
                        raise ValueError(f"{type(self).__name__}.{field_name} has no enum type")
                    if type(value) is dict and set(value) == {"__enum_name__"}:
                        enum_name = value["__enum_name__"]
                        if type(enum_name) is not str or enum_name not in enum_cls.__members__:
                            raise ValueError(f"{type(self).__name__}.{field_name} has an unknown enum value")
                        setattr(self, field_name, enum_cls[enum_name])
                        continue
                    try:
                        enum_value = enum_cls(value)
                    except (TypeError, ValueError):
                        raise ValueError(f"{type(self).__name__}.{field_name} has an unknown enum value")
                    setattr(self, field_name, enum_value)
                    continue
                setattr(self, field_name, value)
        finally:
            self._inf_deserializing = False

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} name='{self.name}' "
            f"point='{self.injection_point}' "
            f"enabled={self.enabled}>"
        )
