"""
BuiltinComponent — Base class for Python wrappers around C++ built-in components.

Provides a unified InxComponent interface for C++ components like Light,
MeshRenderer, and Camera.  The actual state lives in the C++ component;
Python CppProperty descriptors delegate reads/writes transparently.

Design:
    - BuiltinComponent inherits from InxComponent (unified type system)
    - No PyComponentProxy is created (C++ component is already in m_components)
    - Properties delegate to the C++ component via CppProperty descriptors
    - Wrappers are cached per component_id for identity stability

Usage (from within an InxComponent script)::

    from Infernux.components.builtin import Light, MeshRenderer

    class MyScript(InxComponent):
        def start(self):
            light = self.game_object.get_component(Light)
            light.intensity = 2.0

        def update(self, dt):
            mr = self.game_object.get_component(MeshRenderer)
            if mr:
                mr.casts_shadows = False
"""

from __future__ import annotations

import weakref
from typing import Any, Dict, Optional, Type, TYPE_CHECKING

from .fields import FieldMetadata, FieldType
from .component import InxComponent
from Infernux.debug import Debug

if TYPE_CHECKING:
    from Infernux.lib import Component as CppComponent, GameObject


# =============================================================================
# CppProperty — descriptor that delegates to a C++ component attribute
# =============================================================================


class CppProperty:
    """Descriptor that delegates get/set to a C++ component property.

    Used by BuiltinComponent subclasses to expose C++ properties as
    serialized fields within the InxComponent system.

    The ``_is_cpp_property`` flag allows InxComponent.__init_subclass__
    to recognise this descriptor without importing this module (no
    circular dependency).

    Args:
        cpp_attr: Attribute name on the pybind11 C++ component object.
        field_type: FieldType for Inspector rendering.
        default: Inspector and serialization metadata default.
        readonly: If ``True``, the property cannot be set from Python.
        tooltip: Hover text for the Inspector panel.
        header: Group header shown above this field in the Inspector.
        range: ``(min, max)`` tuple for numeric slider widgets.
        enum_type: Enum class for ENUM fields.

    Example::

        class Light(BuiltinComponent):
            _cpp_type_name = "Light"
            intensity = CppProperty("intensity", FieldType.FLOAT, default=1.0)
    """

    _is_cpp_property: bool = True  # marker for InxComponent.__init_subclass__

    def __init__(
        self,
        cpp_attr: str,
        field_type: FieldType = FieldType.UNKNOWN,
        default: Any = None,
        *,
        readonly: bool = False,
        tooltip: str = "",
        display_name_key: str = "",
        header: str = "",
        range: Optional[tuple] = None,
        enum_type=None,
        enum_labels: Optional[list] = None,
        element_type: Optional[FieldType] = None,
        element_class=None,
        visible_when=None,
        asset_type: Optional[str] = None,
        get_converter=None,
        set_converter=None,
        native_getter=None,
        native_setter=None,
        hdr: bool = False,
        curve_non_negative: bool = False,
        slider: bool = False,
    ):
        self.cpp_attr = cpp_attr
        self.get_converter = get_converter
        self.set_converter = set_converter
        self.native_getter = native_getter
        self.native_setter = native_setter
        self.metadata = FieldMetadata(
            name="",  # filled by __set_name__ / __init_subclass__
            field_type=field_type,
            default=default,
            readonly=readonly,
            tooltip=tooltip,
            display_name_key=display_name_key,
            header=header,
            range=range,
            enum_type=enum_type,
            enum_labels=enum_labels,
            element_type=element_type,
            element_class=element_class,
            visible_when=visible_when,
            asset_type=asset_type,
            hdr=hdr,
            curve_non_negative=curve_non_negative,
            slider=slider,
        )

    # Called by Python when the class body is processed.
    def __set_name__(self, owner: type, name: str):
        if not self.metadata.name:
            self.metadata.name = name

    def __get__(self, instance: Optional[Any], owner: type) -> Any:
        if instance is None:
            return self
        cpp = instance._require_cpp_component()
        try:
            value = (
                self.native_getter(cpp)
                if self.native_getter is not None
                else getattr(cpp, self.cpp_attr)
            )
        except RuntimeError as exc:
            instance._invalidate_native_binding()
            raise ReferenceError(
                f"{type(instance).__name__}.{self.metadata.name} accessed a destroyed native component"
            ) from exc
        enum_type = getattr(self.metadata, "enum_type", None)
        if isinstance(enum_type, str):
            try:
                import Infernux.lib as _lib
                enum_type = getattr(_lib, enum_type, None)
            except (ImportError, AttributeError):
                enum_type = None
        if enum_type is not None and value is not None:
            try:
                return enum_type(value)
            except (ValueError, KeyError, TypeError):
                return value
        if self.get_converter is not None:
            return self.get_converter(value)
        return value

    def __set__(self, instance: Any, value: Any) -> None:
        if self.metadata.readonly:
            raise AttributeError(
                f"Property '{self.metadata.name}' is read-only"
            )
        enum_type = getattr(self.metadata, "enum_type", None)
        if enum_type is not None:
            # Resolve string enum_type to the actual pybind11 enum class
            if isinstance(enum_type, str):
                try:
                    import Infernux.lib as _lib
                    enum_type = getattr(_lib, enum_type, None)
                except (ImportError, AttributeError):
                    enum_type = None
            # Convert int → C++ enum so pybind11 accepts the value
            if enum_type is not None and isinstance(value, int):
                value = enum_type(value)
        if self.set_converter is not None:
            value = self.set_converter(value)
        cpp = instance._require_cpp_component()
        try:
            if self.native_setter is not None:
                self.native_setter(cpp, value)
            else:
                setattr(cpp, self.cpp_attr, value)
        except RuntimeError as exc:
            instance._invalidate_native_binding()
            raise ReferenceError(
                f"{type(instance).__name__}.{self.metadata.name} accessed a destroyed native component"
            ) from exc


# =============================================================================
# BuiltinComponent — base class
# =============================================================================


class BuiltinComponent(InxComponent):
    """Base class for Python wrappers around C++ built-in components.

    Subclasses MUST set ``_cpp_type_name`` to the C++ component's registered
    type name (e.g. ``"Light"``, ``"MeshRenderer"``, ``"Camera"``).

    The wrapper is fully compatible with the InxComponent type system:

    * ``isinstance(wrapper, InxComponent)`` → ``True``
    * ``get_component(Light)`` returns the wrapper
    * Inspector reads CppProperty metadata for field display
    * Serialisation delegates to the C++ component

    Lifecycle note:
        Because the underlying C++ component already participates in the
        C++ update loop, BuiltinComponent does **not** create a
        ``PyComponentProxy``.  Lifecycle methods (awake/start/update …)
        are inherited from InxComponent and can be overridden, but they
        are **not** called automatically by the C++ loop.  If you need
        per-frame behaviour on a built-in component, attach a separate
        InxComponent script instead — this mirrors Unity's pattern where
        ``Light``/``Camera``/``MeshRenderer`` are ``Component`` (not
        ``MonoBehaviour``).
    """

    # ---- Must be overridden in concrete subclasses ----
    _cpp_type_name: str = ""
    _is_builtin_component_wrapper = True
    _registers_active_instance = False
    _uses_component_data_store = False

    # ---- Instance state (set by _bind_cpp) ----
    _cpp_component: Optional[Any] = None  # pybind11 C++ component reference

    # ---- Class-level registries ----
    _builtin_registry: Dict[str, Type["BuiltinComponent"]] = {}
    _wrapper_cache: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

    # ------------------------------------------------------------------
    # Metaclass hook — register concrete subclasses automatically
    # ------------------------------------------------------------------

    def __init_subclass__(cls, **kwargs):
        # InxComponent.__init_subclass__ runs first via super() chain and
        # processes CppProperty descriptors (thanks to _is_cpp_property).
        super().__init_subclass__(**kwargs)

        cpp_name = getattr(cls, "_cpp_type_name", "")
        if cpp_name and cpp_name != BuiltinComponent._cpp_type_name:
            BuiltinComponent._builtin_registry[cpp_name] = cls

    # ------------------------------------------------------------------
    # Binding / wrapping
    # ------------------------------------------------------------------

    def _bind_cpp(
        self, cpp_component: "CppComponent", game_object: "GameObject"
    ) -> None:
        """Bind this Python wrapper to an existing C++ component.

        Called after ``cls()`` construction to link the wrapper to the
        underlying C++ object.  Syncs component ID, enabled state, and
        game_object reference.
        """
        self._bind_native_component(cpp_component, game_object)
        # Cache by component_id
        cache_key = cpp_component.component_id
        BuiltinComponent._wrapper_cache[cache_key] = self

    @classmethod
    def _get_or_create_wrapper(
        cls, cpp_component: "CppComponent", game_object: "GameObject"
    ) -> "BuiltinComponent":
        """Return an existing wrapper or create a new one.

        The cache is keyed by the C++ component's stable ``component_id``
        so the same Python object is returned on repeated lookups.

        Play Mode rebuilds preserve those IDs while allocating new native
        instances.  A still-valid cache entry must therefore rebind when the
        incoming C++ object is a replacement, not merely a new pybind view
        of the same handle.
        """
        comp_id = cpp_component.component_id
        existing = BuiltinComponent._wrapper_cache.get(comp_id)
        if existing is not None:
            destroyed = bool(getattr(existing, "_is_destroyed", False))
            if destroyed and getattr(existing, "_cpp_component", None) is None:
                BuiltinComponent._wrapper_cache.pop(comp_id, None)
            else:
                # Scripts cache this wrapper (SmokeRangeDirector._body).  A
                # Play rebuild or inspector refresh must rebind the same
                # object so gameplay still writes to a live native body.
                if (
                    existing._is_native_binding_stale()
                    or not cls._is_same_native_instance(existing, cpp_component)
                ):
                    existing._bind_cpp(cpp_component, game_object)
                return existing

        wrapper = cls()
        wrapper._bind_cpp(cpp_component, game_object)
        return wrapper

    @staticmethod
    def _is_same_native_instance(existing, cpp_component) -> bool:
        bound = getattr(existing, "_cpp_component", None)
        if bound is cpp_component:
            return True
        existing_handle = getattr(existing, "_native_handle", None)
        incoming_handle = getattr(cpp_component, "handle", None)
        return (
            existing_handle is not None
            and incoming_handle is not None
            and existing_handle == incoming_handle
        )

    @classmethod
    def _clear_cache(cls) -> None:
        """Clear the wrapper cache (call on scene change / play-mode stop)."""
        for wrapper in list(BuiltinComponent._wrapper_cache.values()):
            try:
                wrapper._invalidate_native_binding()
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_warning(f"[BuiltinComponent] cache clear failed: {exc}")
        BuiltinComponent._wrapper_cache.clear()
        # Inspector state is an editor-only cache. Importing it from Player
        # scene publication pulls the complete Inspector/serialization UI
        # graph into the startup path even though no editor panel exists.
        from Infernux.application import Application

        if Application.is_editor():
            try:
                from Infernux.engine.ui.inspector_components import (
                    clear_component_value_cache,
                )

                clear_component_value_cache()
            except ImportError:
                pass

    @classmethod
    def _invalidate_component_ids(cls, component_ids) -> None:
        """Invalidate wrappers whose native components were transactionally replaced."""
        for component_id in component_ids:
            wrapper = BuiltinComponent._wrapper_cache.pop(int(component_id), None)
            if wrapper is not None:
                wrapper._invalidate_native_binding()

    # ------------------------------------------------------------------
    # Property overrides (delegate to C++)
    # ------------------------------------------------------------------

    def _require_cpp_component(self):
        cpp = self._get_bound_native_component()
        if cpp is None:
            raise ReferenceError(
                f"{type(self).__name__} is not bound to a live native component"
            )
        return cpp

    # ------------------------------------------------------------------
    # Inspector rendering (override in subclasses for custom layout)
    # ------------------------------------------------------------------

    def render_inspector(self, ctx) -> None:
        """Render this component's inspector UI.

        Override in subclasses to customise the layout.  The default
        implementation renders all :class:`CppProperty` descriptors
        using the standard inspector field widgets.

        Args:
            ctx: The ImGui context (:class:`InxGUIContext`).
        """
        from Infernux.engine.ui.inspector_components import render_builtin_via_setters
        render_builtin_via_setters(ctx, self, type(self))

    # ------------------------------------------------------------------
    # Property overrides (delegate to C++)
    # ------------------------------------------------------------------

    @property  # type: ignore[override]
    def enabled(self) -> bool:
        cpp = self._get_bound_native_component()
        if cpp is not None:
            try:
                return bool(cpp.enabled)
            except RuntimeError:
                self._invalidate_native_binding()
                return self._enabled
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        cpp = self._get_bound_native_component()
        if cpp is not None:
            try:
                cpp.enabled = value
            except RuntimeError:
                self._invalidate_native_binding()

    @property
    def is_valid(self) -> bool:
        return self._get_bound_native_component() is not None and not self._is_destroyed

    def __getattr__(self, name: str):
        cpp = self._get_bound_native_component()
        if cpp is not None:
            try:
                return getattr(cpp, name)
            except RuntimeError:
                self._invalidate_native_binding()
            except AttributeError:
                pass
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")

    @property
    def component_id(self) -> int:
        cpp = self._get_bound_native_component()
        if cpp is not None:
            try:
                return cpp.component_id
            except RuntimeError:
                self._invalidate_native_binding()
                return self._component_id
        return self._component_id

    # ------------------------------------------------------------------
    # Serialization (delegate to C++ component)
    # ------------------------------------------------------------------

    def serialize(self) -> str:
        """Serialize via the C++ component's own serializer."""
        cpp = self._get_bound_native_component()
        if cpp is not None:
            try:
                return cpp.serialize()
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_warning(f"BuiltinComponent serialize failed for {self._cpp_type_name}: {exc}")
                self._invalidate_native_binding()
        return "{}"

    def _serialize_fields(self) -> str:
        """Alias kept for InxComponent compatibility."""
        return self.serialize()

    def deserialize(self, json_str: str) -> bool:
        """Deserialize via the C++ component."""
        cpp = self._get_bound_native_component()
        if cpp is not None:
            try:
                return bool(cpp.deserialize(json_str))
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_warning(f"BuiltinComponent deserialize failed for {self._cpp_type_name}: {exc}")
                self._invalidate_native_binding()
        return False

    def serialize_document(self) -> dict:
        """Return the native component document without a JSON text round-trip."""
        cpp = self._get_bound_native_component()
        if cpp is None:
            raise RuntimeError(f"{self._cpp_type_name} is not bound to a native component")
        return cpp.serialize_document()

    def deserialize_document(self, document: dict) -> bool:
        """Apply a native component document without a JSON text round-trip."""
        cpp = self._get_bound_native_component()
        if cpp is None:
            raise RuntimeError(f"{self._cpp_type_name} is not bound to a native component")
        return bool(cpp.deserialize_document(document))

    def _deserialize_fields(self, json_str: str) -> None:
        """Alias kept for InxComponent compatibility."""
        self.deserialize(json_str)

    # ------------------------------------------------------------------
    # String representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        bound = self._get_bound_native_component() is not None
        return (
            f"<{self.__class__.__name__}"
            f" cpp={self._cpp_type_name}"
            f" bound={bound}"
            f" id={self._component_id}>"
        )
