"""
InxComponent - Base class for all Python-defined components.

Provides Unity-style lifecycle methods and property injection.
Users inherit from this class to create custom game logic.

Example:
    from Infernux.components import InxComponent, serialized_field
    
    class PlayerController(InxComponent):
        speed: float = serialized_field(default=5.0)
        
        def start(self):
            print("Player started!")
        
        def update(self, delta_time: float):
            pos = self.transform.position
            self.transform.position = Vector3(pos.x + self.speed * delta_time, pos.y, pos.z)
"""

from typing import Optional, Dict, Any, Type, TYPE_CHECKING, List
import threading
import weakref

from Infernux.lib import GameObject

if TYPE_CHECKING:
    from Infernux.lib import GameObject, Transform
    from Infernux.coroutine import Coroutine

from ._component_native import ComponentNativeMixin
from ._component_lifecycle import ComponentLifecycleMixin
from ._component_physics import ComponentPhysicsMixin
from ._component_coroutine import ComponentCoroutineMixin
from ._component_serialization import ComponentSerializationMixin


class InxComponent(ComponentNativeMixin, ComponentLifecycleMixin, ComponentPhysicsMixin, ComponentCoroutineMixin, ComponentSerializationMixin):
    """
    Base class for Python-defined components.
    
    Lifecycle methods (override as needed):
        - awake(): Called when the component first becomes active in the hierarchy
        - start(): Called before first update, after all awake()
        - update(delta_time): Called every frame
        - late_update(delta_time): Called after all update()
        - on_destroy(): Called when component is removed/destroyed
        - on_enable(): Called when component becomes enabled
        - on_disable(): Called when component becomes disabled
    
    Serialized fields:
        - Use class-level ``serialized_field()`` declarations.

    Injected properties (read-only):
        - game_object: The GameObject this component is attached to
        - transform: Shortcut to game_object.get_transform()
        - enabled: Whether this component is active
        - component_id: Stable unique ID for this component instance
    """
    
    # Class-level storage for serialized field metadata
    _serialized_fields_: Dict[str, Any] = {}

    # Active instance registry: go_id (int) → list of live InxComponent instances.
    # Used by GizmosCollector to skip the expensive get_all_objects() + get_py_components()
    # scene walk — instead we iterate only objects that actually have Python components.
    # Populated by _set_game_object(), removed by _call_on_destroy() or
    # _invalidate_native_binding() during scene rebuild/destruction.
    # Use a plain dict (not WeakValueDictionary) so we hold strong refs; instances
    # remove themselves from the registry in _call_on_destroy().
    _active_instances: Dict[int, List['InxComponent']] = {}

    # Python script components need a strong lifecycle registry and numeric-field
    # storage. Native component facades override both flags because C++ owns their
    # lifetime and property data.
    _registers_active_instance: bool = True
    _uses_component_data_store: bool = True

    # Component category for the Add Component menu.
    # Override in subclasses to group related components together.
    # Examples: "Physics", "Rendering", "Audio", "UI", etc.
    # When empty, script components default to the "Scripts" group.
    _component_category_: str = ""

    _intrinsic_script_guid_: str = ""
    _type_guid_: str = ""
    _asset_script_guid_: str = ""

    # Gizmo visibility: when True, on_draw_gizmos() is called every frame
    # for this component.  When False, on_draw_gizmos() is only called when
    # the owning GameObject (or one of its ancestors) is selected.
    # Subclasses can override: ``_always_show = False``
    _always_show: bool = True
    
    # Thread-safe component ID generator
    _next_component_id: int = 1
    _id_lock: threading.Lock = threading.Lock()

    @classmethod
    def reserve_instances(cls, count: int) -> None:
        """Preallocate numeric-field storage for bulk creation of this component type."""
        from ._cds_bridge import reserve_class

        reserve_class(cls, count)
    
    def __init_subclass__(cls, **kwargs):
        """
        Called when a subclass is created. Collect class-level fields as serialized fields.
        
        This enables Unity-style field declaration:
            class Kobe(InxComponent):
                speed = 5.0
                count = int_field(10)
        """
        super().__init_subclass__(**kwargs)

        from .component_identity import component_type_guid, intrinsic_script_guid
        cls._intrinsic_script_guid_ = intrinsic_script_guid(cls.__module__)
        cls._type_guid_ = component_type_guid(cls.__module__, cls.__qualname__)
        cls._asset_script_guid_ = ""

        # ---- Enforce lifecycle: forbid __init__ override ----
        if '__init__' in cls.__dict__:
            raise TypeError(
                f"{cls.__qualname__} overrides __init__, which is forbidden. "
                f"InxComponent manages its own initialization internally. "
                f"Use awake() for one-time setup or start() for deferred init."
            )
        
        # Always create a fresh dict for this class (don't inherit from parent)
        cls._serialized_fields_ = {}

        from .serialized_field import (
            FieldMetadata, HiddenField, SerializedFieldDescriptor,
            build_field_from_annotation, get_annotation_default,
            infer_field_type_from_value, _UNSET, _unwrap_annotation,
            _apply_markers, NON_SERIALIZED_FIELD,
        )

        # ── Resolve own-class annotations once ──────────────────────────
        # String annotations (incl. files using ``from __future__ import
        # annotations``) are evaluated against the defining module's globals
        # so Annotated[...]/Optional[...] survive. Resolution is per-name —
        # a single unresolvable forward ref must not poison the others
        # (deliberately NOT typing.get_type_hints, which walks the whole MRO
        # and fails wholesale on any base-class forward reference).
        own_annotations = dict(cls.__dict__.get('__annotations__', {}))
        resolved_hints: dict = {}
        if own_annotations:
            import sys as _sys
            _module = _sys.modules.get(cls.__module__)
            _globalns = getattr(_module, '__dict__', {})
            for _k, _v in own_annotations.items():
                if isinstance(_v, str):
                    try:
                        resolved_hints[_k] = eval(_v, _globalns, dict(vars(cls)))  # noqa: S307
                    except Exception:
                        pass  # Keep raw strings for deferred annotation resolution.
                else:
                    resolved_hints[_k] = _v

        def _annotation_for(name):
            return resolved_hints.get(name, own_annotations.get(name))

        # ── Pass 1: attributes with a class-level value ──────────────────
        for attr_name in list(cls.__dict__):
            if attr_name.startswith('_'):
                continue

            # Raw attribute from class __dict__ (avoids descriptor protocol)
            attr = cls.__dict__[attr_name]

            if callable(attr) or isinstance(attr, (property, classmethod, staticmethod)):
                continue
            if isinstance(attr, HiddenField):
                continue

            ann = _annotation_for(attr_name)

            # CppProperty — delegates to a C++ component attribute.
            if getattr(attr, '_is_cpp_property', False):
                if hasattr(attr, 'metadata'):
                    attr.metadata.name = attr_name
                    cls._serialized_fields_[attr_name] = attr.metadata
                continue

            # serialized_field() descriptor — keep it, but fold in any
            # Annotated[] markers from a coexisting type annotation so
            # ``speed: Annotated[float, Range(0, 1)] = serialized_field(0.5)``
            # composes naturally.
            if isinstance(attr, SerializedFieldDescriptor):
                if ann is not None:
                    _base, markers = _unwrap_annotation(ann)
                    if markers and _apply_markers(attr.metadata, markers) is None:
                        # NonSerialized marker wins: drop the field entirely.
                        delattr(cls, attr_name)
                        continue
                cls._serialized_fields_[attr_name] = attr.metadata
                continue

            if isinstance(attr, FieldMetadata):
                cls._serialized_fields_[attr_name] = attr
                continue

            # Annotation present → annotation drives the field type; the
            # class value becomes the default (Unity-style declaration).
            metadata = None
            if ann is not None:
                metadata = build_field_from_annotation(ann, default=attr)
                if metadata is NON_SERIALIZED_FIELD:
                    # Explicitly excluded: keep the plain class attribute as-is
                    # (regular Python attr, not serialized, not in Inspector).
                    continue

            # No (usable) annotation → infer from the plain value.
            if metadata is None:
                if attr is None:
                    continue  # bare ``x = None`` with no usable annotation
                from enum import Enum as _Enum
                field_type = infer_field_type_from_value(attr)
                metadata = FieldMetadata(
                    name=attr_name,
                    field_type=field_type,
                    default=attr,
                    enum_type=type(attr) if isinstance(attr, _Enum) else None,
                )

            metadata.name = attr_name
            descriptor = SerializedFieldDescriptor(metadata)
            descriptor.__set_name__(cls, attr_name)
            setattr(cls, attr_name, descriptor)
            cls._serialized_fields_[attr_name] = metadata

        # ── Pass 2: annotation-only fields (no ``= value``) ─────────────
        for attr_name in own_annotations:
            if attr_name in cls.__dict__ or attr_name in cls._serialized_fields_:
                continue
            ann = _annotation_for(attr_name)

            if attr_name.startswith('_'):
                default_value = get_annotation_default(ann)
                if default_value is not None:
                    hidden = HiddenField(default=default_value)
                    hidden.__set_name__(cls, attr_name)
                    setattr(cls, attr_name, hidden)
                continue

            metadata = build_field_from_annotation(ann, default=_UNSET)
            if metadata is NON_SERIALIZED_FIELD:
                continue
            if metadata is not None:
                metadata.name = attr_name
                descriptor = SerializedFieldDescriptor(metadata)
                descriptor.__set_name__(cls, attr_name)
                setattr(cls, attr_name, descriptor)
                cls._serialized_fields_[attr_name] = metadata

        # ── Register numeric fields with C++ ComponentDataStore ──
        from ._cds_bridge import register_class as _cds_register
        _cds_register(cls)

        from .registry import register_component_type
        register_component_type(cls)
    
    def __init__(self):
        """Internal framework initialization — **do not override**.

        Subclasses must use lifecycle methods instead:
        - ``awake()`` — called once when the component is first created
        - ``start()`` — called before the first ``update()``, after all ``awake()``
        - ``on_destroy()`` — called when the component is removed / scene unloaded

        Overriding ``__init__`` is enforced as a ``TypeError`` at class
        creation time (see ``__init_subclass__``).
        """
        # Generate stable component ID immediately (thread-safe)
        with InxComponent._id_lock:
            self._component_id = InxComponent._next_component_id
            InxComponent._next_component_id += 1
        
        self._game_object: Optional['GameObject'] = None  # Reference to the owning GameObject
        self._game_object_ref: Optional[weakref.ref] = None  # Weak reference for safety
        self._cpp_component = None  # Native lifecycle authority (PyComponentProxy or built-in C++ component)
        self._native_handle = None
        self._native_scene = None
        self._native_game_object_handle = None
        self._enabled = True
        self._execution_order = 0
        self._has_started = False
        self._awake_called = False
        self._is_destroyed = False  # Track destruction state
        self._component_name = self.__class__.__name__
        self._script_guid: str = self.__class__._intrinsic_script_guid_
        self._registered_go_id: Optional[int] = None  # go_id this comp is registered under
        self._native_generation: int = 0
        
        # Coroutine scheduler (lazy-created on first start_coroutine call)
        self._coroutine_scheduler = None

        self._cds_slot: Optional[tuple[int, int]] = None
        self._cds_class_id: Optional[int] = None
        if self._uses_component_data_store:
            from ._cds_bridge import allocate_slot as _cds_alloc, get_class_id as _cds_class_id
            self._cds_slot = _cds_alloc(self.__class__)
            self._cds_class_id = _cds_class_id(self.__class__)

        # Initialize serialized fields with defaults (from class-level declarations)
        self._init_serialized_fields()

    @classmethod
    def _get_type_guid(cls) -> str:
        return cls._type_guid_

    @classmethod
    def _get_intrinsic_script_guid(cls) -> str:
        return cls._intrinsic_script_guid_

    def _init_serialized_fields(self):
        """Initialize all serialized fields with their default values."""
        from .serialized_field import (
            SerializedFieldDescriptor,
            copy_serialized_field_default,
            get_serialized_fields,
        )
        fields = get_serialized_fields(self.__class__)
        for name, metadata in fields.items():
            descriptor = next(
                (base.__dict__[name] for base in self.__class__.__mro__ if name in base.__dict__),
                None,
            )
            default_value = copy_serialized_field_default(metadata)

            if isinstance(descriptor, SerializedFieldDescriptor):
                # CDS-backed fields: write default to C++ store.
                if (descriptor._cds_class_id is not None
                        and self._cds_slot is not None
                        and descriptor._cds_class_id == self._cds_class_id):
                    from ._cds_bridge import cds_set
                    cds_set(descriptor._cds_class_id, descriptor._cds_field_id,
                            descriptor._cds_type_code, self._cds_slot, default_value)
                else:
                    # Non-CDS field: Python dict.
                    inst_id = id(self)
                    with descriptor._lock:
                        descriptor._values[inst_id] = default_value
                        descriptor._weak_refs[inst_id] = weakref.ref(self, descriptor._make_ref_callback(inst_id))
            elif hasattr(descriptor, '_is_cpp_property'):
                # Skip ALL CppProperty descriptors — their values come from C++.
                continue
            elif metadata.default is not None:
                setattr(self, name, default_value)

    # ========================================================================
    # Property Injection (set by the engine)
    # ========================================================================

    @property
    def game_object(self) -> 'GameObject':
        """Get the owning GameObject during normal component lifetime."""
        go = self._try_get_game_object()
        if go is None:
            raise RuntimeError(
                f"{self.__class__.__name__}.game_object is unavailable because the component is not bound to a live GameObject"
            )
        return go

    @property
    def transform(self) -> 'Transform':
        """Get the attached Transform during normal component lifetime."""
        transform = self._try_get_transform()
        if transform is None:
            raise RuntimeError(
                f"{self.__class__.__name__}.transform is unavailable because the component is not bound to a live GameObject"
            )
        return transform
    
    @property
    def is_valid(self) -> bool:
        """Check if this component is still valid (not destroyed, has game_object)."""
        return not self._is_destroyed and self._try_get_game_object() is not None
    
    @property
    def enabled(self) -> bool:
        """Check if this component is enabled."""
        cpp_component = self._get_bound_native_component()
        if cpp_component is not None:
            try:
                self._enabled = bool(cpp_component.enabled)
            except RuntimeError:
                self._invalidate_native_binding()
        return self._enabled
    
    @enabled.setter
    def enabled(self, value: bool):
        """Enable or disable this component.

        Native lifecycle is always authoritative once the component is bound.
        Before binding, the value is simply staged on the Python instance and
        will be consumed by the native proxy when attached.
        """
        if self._is_destroyed:
            return
        value = bool(value)
        if self._enabled == value and getattr(self, '_cpp_component', None) is None:
            return

        cpp_component = getattr(self, '_cpp_component', None)
        if cpp_component is not None:
            cpp_component.enabled = value
            return

        self._enabled = value
    
    @property
    def type_name(self) -> str:
        """Get the component type name."""
        return self._component_name

    @property
    def execution_order(self) -> int:
        """Execution order (lower value runs earlier)."""
        cpp_component = self._get_bound_native_component()
        if cpp_component is not None:
            try:
                return int(cpp_component.execution_order)
            except RuntimeError:
                self._invalidate_native_binding()
                return int(getattr(self, '_execution_order', 0))
        return int(getattr(self, '_execution_order', 0))

    @execution_order.setter
    def execution_order(self, value: int):
        cpp_component = getattr(self, '_cpp_component', None)
        if cpp_component is not None:
            cpp_component.execution_order = int(value)
            return
        self._execution_order = int(value)

    @property
    def component_id(self) -> int:
        """Get the stable component ID (assigned at construction)."""
        return self._component_id
    
    # ========================================================================
    # Internal methods (called by the engine)
    # ========================================================================
    
    # ------------------------------------------------------------------
    # Internal helper: safely call a user-overridden lifecycle method,
    # routing any exception to the engine Console so it is visible in
    # the editor (not just stderr).
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Physics collision / trigger callbacks (Unity-style)
    # ------------------------------------------------------------------

    # ========================================================================
    # Lifecycle methods (override in subclasses)
    # ========================================================================
    
    def awake(self):
        """
        Called when the component first becomes active in the hierarchy.
        Use for initialization that doesn't depend on other components.
        """
        pass
    
    def start(self):
        """
        Called before the first update, after all awake() calls.
        Use for initialization that depends on other components being ready.
        """
        pass
    
    def update(self, delta_time: float):
        """
        Called every frame.
        
        Args:
            delta_time: Time in seconds since last frame
        """
        pass

    def fixed_update(self, fixed_delta_time: float):
        """
        Called at a fixed time step (default 50 Hz).
        Use for physics and deterministic logic.
        
        Args:
            fixed_delta_time: Fixed time step in seconds
        """
        pass

    def late_update(self, delta_time: float):
        """
        Called every frame after all update() calls.
        Useful for camera follow, physics cleanup, etc.
        
        Args:
            delta_time: Time in seconds since last frame
        """
        pass
    
    def destroy(self):
        """Remove this component from its owning GameObject (Unity-style).

        The component's ``on_destroy`` lifecycle hook will be called.
        After this call the component is considered destroyed and should
        not be used further.
        """
        if self._is_destroyed:
            return
        go = self._try_get_game_object()
        if go is None:
            return
        # Use the C++ binding appropriate for the component type
        cpp = self._get_bound_native_component()
        if cpp is not None:
            go.remove_component(cpp)
        else:
            go.remove_py_component(self)

    def on_destroy(self):
        """
        Called when the component is being destroyed.
        Use for cleanup (unsubscribe events, release resources).
        """
        pass

    def on_enable(self):
        """
        Called when the component becomes enabled.
        Use for subscribing to events, starting coroutines, etc.
        """
        pass

    def on_disable(self):
        """
        Called when the component becomes disabled.
        Use for unsubscribing from events and releasing active subscriptions.
        Disabling the script itself does not stop Unity-style coroutines; deactivating
        the owning GameObject does.
        """
        pass

    def on_inspector_gui(self, ctx) -> None:
        """
        Override to draw a fully custom inspector for this component.

        When this method is overridden in a subclass, the engine will call
        it instead of auto-generating the inspector from serialized fields.
        ``ctx`` is an :class:`InxGUIContext` providing the full ImGui API.

        Return *None*.  The default implementation returns
        :data:`NotImplemented` to signal "use the auto-generated inspector".

        Example::

            class MyComponent(InxComponent):
                health: float = serialized_field(default=100.0)

                def on_inspector_gui(self, ctx):
                    ctx.label("Custom Inspector")
                    new_val = ctx.float_slider("##health", self.health, 0, 200)
                    if abs(new_val - self.health) > 1e-5:
                        self.health = new_val

        NOTE: This is editor-only; it is never called during play mode in
        standalone builds.
        """
        return NotImplemented

    def on_validate(self):
        """
        Called in the editor when the component is loaded or a value changes.
        Use for validation and clamping of serialized values.
        NOTE: This is editor-only; do not use for gameplay logic.
        """
        pass

    def reset(self):
        """
        Called in the editor when the user selects "Reset" on the component.
        Use to restore default values.
        NOTE: This is editor-only; do not use for gameplay logic.
        """
        pass

    def on_after_deserialize(self):
        """
        Called after component fields have been deserialized.
        Use to rebuild runtime references that weren't serialized.
        
        Example:
            def on_after_deserialize(self):
                # Rebuild cached references
                self._cached_renderer = self.game_object.get_component(MeshRenderer)
        """
        pass

    def on_before_serialize(self):
        """
        Called before component fields are serialized.
        Use to prepare data for serialization.
        """
        pass

    # ========================================================================
    # Physics collision / trigger callbacks (Unity-style)
    # ========================================================================

    def on_collision_enter(self, collision):
        """
        Called when this collider starts touching another collider.

        Args:
            collision: CollisionInfo with contact details (collider,
                       game_object, contact_point, contact_normal,
                       relative_velocity).
        """
        pass

    def on_collision_stay(self, collision):
        """
        Called every fixed-update while two colliders remain in contact.

        Args:
            collision: CollisionInfo with contact details.
        """
        pass

    def on_collision_exit(self, collision):
        """
        Called when two colliders stop touching.

        Args:
            collision: CollisionInfo with contact details.
        """
        pass

    def on_trigger_enter(self, other):
        """
        Called when another collider enters this trigger volume.

        Args:
            other: The other Collider that entered.
        """
        pass

    def on_trigger_stay(self, other):
        """
        Called every fixed-update while another collider is inside this trigger.

        Args:
            other: The other Collider that is inside.
        """
        pass

    def on_trigger_exit(self, other):
        """
        Called when another collider exits this trigger volume.

        Args:
            other: The other Collider that exited.
        """
        pass

    def on_draw_gizmos(self):
        """
        Called every frame in the editor to draw gizmos for this component.

        Override this to draw custom visual aids (wireframes, lines, etc.)
        using the ``Gizmos`` API.  When ``always_show`` is False on this
        component, this callback is only invoked when the owning
        GameObject (or one of its ancestors) is selected.

        Example::

            from Infernux.gizmos import Gizmos

            def on_draw_gizmos(self):
                Gizmos.color = (0, 1, 0)
                Gizmos.draw_wire_sphere(self.transform.position, 2.0)
        """
        pass

    def on_draw_gizmos_selected(self):
        """
        Called every frame in the editor ONLY when this object is selected.

        Use this for gizmos that should only appear when the user is
        inspecting this specific object.

        Example::

            from Infernux.gizmos import Gizmos

            def on_draw_gizmos_selected(self):
                Gizmos.color = (1, 1, 0)
                Gizmos.draw_wire_cube(self.transform.position, (1, 1, 1))
        """
        pass

    # ========================================================================
    # Coroutine support (Unity-style)
    # ========================================================================

    # ========================================================================
    # Serialization (used by Play Mode snapshot)
    # ========================================================================
    
    # ========================================================================
    # Utility methods
    # ========================================================================
    
    # ========================================================================
    # Tag & Layer convenience properties
    # ========================================================================

    @property
    def tag(self) -> str:
        """Get the tag of the attached GameObject."""
        go = self._try_get_game_object()
        if go is not None and hasattr(go, 'tag'):
            return go.tag
        return "Untagged"

    @tag.setter
    def tag(self, value: str):
        """Set the tag of the attached GameObject."""
        go = self._try_get_game_object()
        if go is not None and hasattr(go, 'tag'):
            go.tag = value

    @property
    def game_object_layer(self) -> int:
        """Get the layer of the attached GameObject."""
        go = self._try_get_game_object()
        if go is not None and hasattr(go, 'layer'):
            return go.layer
        return 0

    @game_object_layer.setter
    def game_object_layer(self, value: int):
        """Set the layer of the attached GameObject."""
        go = self._try_get_game_object()
        if go is not None and hasattr(go, 'layer'):
            go.layer = value

    def compare_tag(self, tag: str) -> bool:
        """Returns True if the attached GameObject's tag matches the given tag."""
        go = self._try_get_game_object()
        if go is not None and hasattr(go, 'compare_tag'):
            return go.compare_tag(tag)
        return False

    def __repr__(self) -> str:
        return f"<{self._component_name} id={self._component_id} enabled={self.enabled}>"
