"""UIEventEntry — a serializable event binding for Unity-style persistent callbacks.

Each entry stores a target GameObject reference, a component type name,
and a method name.  At runtime the button resolves the reference and
calls the method.
"""

from __future__ import annotations

import inspect
import types
import copy
from dataclasses import dataclass
from typing import Any, get_args, get_origin, get_type_hints

from Infernux.components import SerializableObject, serialized_field, GameObjectRef
from Infernux.components.ref_wrappers import ComponentRef
from Infernux.components.fields import FieldType
from Infernux.debug import Debug
from Infernux.engine.runtime_dispatch import ReloadableCallbackRegistry


# Lifecycle / internal methods that should never appear in the method picker.
LIFECYCLE_METHODS: frozenset[str] = frozenset({
    "awake", "start", "update", "late_update", "fixed_update",
    "on_destroy", "on_enable", "on_disable",
    "on_draw_gizmos", "on_draw_gizmos_selected",
    "on_pointer_click", "on_pointer_down", "on_pointer_up",
    "on_pointer_enter", "on_pointer_exit",
    "on_validate", "on_collision_enter", "on_collision_exit",
    "on_collision_stay", "on_trigger_enter", "on_trigger_exit",
    "on_trigger_stay",
})


class UIEventEntry(SerializableObject):
    """One persistent on-click binding: target GO → component → method."""

    target: GameObjectRef = serialized_field(
        default=None, field_type=FieldType.GAME_OBJECT,
        tooltip="Target GameObject",
    )
    component_name: str = serialized_field(
        default="", tooltip="Component type name on the target",
    )
    method_name: str = serialized_field(
        default="", tooltip="Public method to invoke",
    )
    arguments: list = serialized_field(
        default=[], field_type=FieldType.LIST,
        element_type=FieldType.SERIALIZABLE_OBJECT,
        element_class=None,
        tooltip="Persistent method arguments",
    )


class UIEventArgument(SerializableObject):
    """Persistent argument payload for one reflected button-event parameter."""

    kind: str = serialized_field(default="string")
    name: str = serialized_field(default="")
    component_type: str = serialized_field(default="")
    int_value: int = serialized_field(default=0)
    float_value: float = serialized_field(default=0.0)
    bool_value: bool = serialized_field(default=False)
    string_value: str = serialized_field(default="")
    game_object: GameObjectRef = serialized_field(
        default=None, field_type=FieldType.GAME_OBJECT,
    )
    component: ComponentRef = serialized_field(
        default=ComponentRef(), field_type=FieldType.COMPONENT,
        component_type="",
    )


UIEventEntry._serialized_fields_["arguments"].element_class = UIEventArgument


def _get_serializable_raw_field(obj, field_name: str, default=None):
    try:
        data = object.__getattribute__(obj, "__dict__")
    except Exception:
        return default
    if field_name in data:
        return data[field_name]
    try:
        cls = object.__getattribute__(obj, "__class__")
        meta = getattr(cls, "_serialized_fields_", {}).get(field_name)
        if meta is not None:
            return meta.default
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        pass
    return default


@dataclass(frozen=True)
class UIEventMethodParameter:
    name: str
    kind: str
    component_type: str = ""
    default_value: Any = inspect._empty

    @property
    def display_name(self) -> str:
        kind_label = self.kind.replace("_", " ")
        return f"{self.name} ({kind_label})"


def get_callable_methods(component) -> list[str]:
    """Return public, non-lifecycle method names on *component*."""
    methods: list[str] = []
    for name in sorted(dir(component)):
        if name.startswith("_"):
            continue
        if name in LIFECYCLE_METHODS:
            continue
        attr = getattr(type(component), name, None)
        if attr is None:
            attr = getattr(component, name, None)
        if callable(attr) and not isinstance(attr, property):
            methods.append(name)
    return methods


def _unwrap_annotation(annotation):
    origin = get_origin(annotation)
    if origin in (types.UnionType, getattr(__import__("typing"), "Union", None)):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _infer_argument_kind(annotation, default_value=inspect._empty) -> tuple[str, str]:
    annotation = _unwrap_annotation(annotation)
    if annotation in (bool, int, float, str):
        return annotation.__name__, ""

    type_name = getattr(annotation, "__name__", str(annotation or ""))
    if type_name in ("GameObject", "GameObjectRef"):
        return "game_object", ""
    if type_name == "ComponentRef":
        return "component", ""

    try:
        from Infernux.components.component import InxComponent
        from Infernux.components.builtin_component import BuiltinComponent

        if isinstance(annotation, type):
            if issubclass(annotation, BuiltinComponent):
                return "component", getattr(annotation, "_cpp_type_name", "") or annotation.__name__
            if issubclass(annotation, InxComponent) and annotation is not InxComponent:
                return "component", annotation.__name__
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        pass

    if default_value is not inspect._empty:
        if isinstance(default_value, bool):
            return "bool", ""
        if isinstance(default_value, int) and not isinstance(default_value, bool):
            return "int", ""
        if isinstance(default_value, float):
            return "float", ""
        if isinstance(default_value, str):
            return "string", ""
        if isinstance(default_value, GameObjectRef):
            return "game_object", ""
        if isinstance(default_value, ComponentRef):
            return "component", default_value.component_type

    return "string", ""


def get_method_parameter_specs(component, method_name: str) -> list[UIEventMethodParameter]:
    """Reflect the positional parameters of a bound callback method."""
    if component is None or not method_name:
        return []

    fn = getattr(component, method_name, None)
    if not callable(fn):
        return []

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []

    func_obj = getattr(fn, "__func__", fn)
    try:
        type_hints = get_type_hints(func_obj, getattr(func_obj, "__globals__", {}), None)
    except Exception:
        type_hints = {}

    specs: list[UIEventMethodParameter] = []
    for param in sig.parameters.values():
        if param.kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            continue
        annotation = type_hints.get(param.name, param.annotation)
        kind, component_type = _infer_argument_kind(annotation, param.default)
        specs.append(UIEventMethodParameter(
            name=param.name,
            kind=kind,
            component_type=component_type,
            default_value=param.default,
        ))
    return specs


def _build_default_argument(spec: UIEventMethodParameter) -> UIEventArgument:
    arg = UIEventArgument(kind=spec.kind, name=spec.name, component_type=spec.component_type)
    default_value = spec.default_value
    if spec.kind == "bool":
        arg.bool_value = bool(default_value) if default_value is not inspect._empty else False
    elif spec.kind == "int":
        arg.int_value = int(default_value) if default_value is not inspect._empty else 0
    elif spec.kind == "float":
        arg.float_value = float(default_value) if default_value is not inspect._empty else 0.0
    elif spec.kind == "string":
        arg.string_value = str(default_value) if default_value is not inspect._empty else ""
    elif spec.kind == "game_object":
        if isinstance(default_value, GameObjectRef):
            arg.game_object = default_value
        else:
            arg.game_object = GameObjectRef(persistent_id=0)
    elif spec.kind == "component":
        if isinstance(default_value, ComponentRef):
            arg.component = default_value
        else:
            arg.component = ComponentRef(component_type=spec.component_type or "")
    return arg


def normalize_event_arguments(existing_args: list[UIEventArgument], specs: list[UIEventMethodParameter]) -> list[UIEventArgument]:
    """Resize and retag stored arguments to match the current reflected signature."""
    normalized: list[UIEventArgument] = []
    existing_args = list(existing_args or [])
    for index, spec in enumerate(specs):
        if index < len(existing_args) and isinstance(existing_args[index], UIEventArgument):
            arg = copy.deepcopy(existing_args[index])
            arg.kind = spec.kind
            arg.name = spec.name
            arg.component_type = spec.component_type or ""
            if spec.kind == "component":
                existing_ref = arg.component if isinstance(arg.component, ComponentRef) else ComponentRef()
                if existing_ref.component_type != (spec.component_type or ""):
                    arg.component = ComponentRef(go_id=existing_ref.go_id, component_type=spec.component_type or "")
            normalized.append(arg)
            continue
        normalized.append(_build_default_argument(spec))
    return normalized


def materialize_event_arguments(
    entry: UIEventEntry,
    component,
    *,
    specs: list[UIEventMethodParameter] | tuple[UIEventMethodParameter, ...] | None = None,
) -> list[Any]:
    """Return the runtime argument list for a bound event entry."""
    if specs is None:
        specs = get_method_parameter_specs(
            component,
            getattr(entry, "method_name", "") or "",
        )
    specs = tuple(specs)
    # The binding owns the immutable method signature.  Do not normalize or
    # deep-copy the serialized list on every click; authoring code already
    # keeps the entry aligned with the selected method.  Missing arguments are
    # still filled from the cached specs so a partially-authored entry fails
    # predictably without entering the reflection path.
    existing_args = tuple(getattr(entry, "arguments", None) or [])
    values: list[Any] = []
    for index, spec in enumerate(specs):
        arg = (
            existing_args[index]
            if index < len(existing_args) and isinstance(existing_args[index], UIEventArgument)
            else _build_default_argument(spec)
        )
        if spec.kind == "bool":
            values.append(bool(arg.bool_value))
        elif spec.kind == "int":
            values.append(int(arg.int_value))
        elif spec.kind == "float":
            values.append(float(arg.float_value))
        elif spec.kind == "game_object":
            game_object_ref = _get_serializable_raw_field(arg, "game_object")
            values.append(game_object_ref.resolve() if hasattr(game_object_ref, "resolve") else game_object_ref)
        elif spec.kind == "component":
            component_ref = _get_serializable_raw_field(arg, "component")
            values.append(component_ref.resolve() if hasattr(component_ref, "resolve") else component_ref)
        else:
            values.append(str(arg.string_value or ""))
    return values


@dataclass(frozen=True)
class _UIEventRuntimeBinding:
    """Non-serialized epoch binding cached by a persistent UI event owner."""

    entry_identity: int
    component_name: str
    method_name: str
    target_identity: int
    component_identity: int
    registry: ReloadableCallbackRegistry
    reference: Any
    parameter_specs: tuple[UIEventMethodParameter, ...]

    @staticmethod
    def _target_identity(target) -> int:
        if target is None:
            return 0
        stable_id = getattr(target, "id", None)
        if stable_id is not None:
            try:
                return int(stable_id)
            except (TypeError, ValueError):
                pass
        return id(target)

    @staticmethod
    def _component_identity(component) -> int:
        stable_id = getattr(component, "component_id", None)
        if stable_id is not None:
            try:
                return int(stable_id)
            except (TypeError, ValueError):
                pass
        return id(component)

    @classmethod
    def create(
        cls,
        entry: UIEventEntry,
        component,
        target=None,
    ) -> "_UIEventRuntimeBinding":
        component_name = str(getattr(entry, "component_name", "") or "")
        method_name = str(getattr(entry, "method_name", "") or "")
        method = getattr(component, method_name, None)
        if not callable(method):
            raise ValueError(f"persistent callback method '{method_name}' is unavailable")
        registry = ReloadableCallbackRegistry()
        reference = registry.add_listener(method)
        return cls(
            entry_identity=id(entry),
            component_name=component_name,
            method_name=method_name,
            target_identity=cls._target_identity(target),
            component_identity=cls._component_identity(component),
            registry=registry,
            reference=reference,
            parameter_specs=tuple(get_method_parameter_specs(component, method_name)),
        )

    def matches(self, entry: UIEventEntry, target, component) -> bool:
        if (
            self.entry_identity != id(entry)
            or self.component_name != str(getattr(entry, "component_name", "") or "")
            or self.method_name != str(getattr(entry, "method_name", "") or "")
            or self.registry.listener_count != 1
            or self.target_identity != self._target_identity(target)
            or self.component_identity != self._component_identity(component)
        ):
            return False
        # The owner identity is already part of the cache key and the
        # registry resolves the method against the current epoch at invoke
        # time. Do not perform a second getattr() lookup on every click.
        return True

    def invoke(self, entry: UIEventEntry):
        arguments = materialize_event_arguments(
            entry,
            None,
            specs=self.parameter_specs,
        )
        results = self.registry.invoke(
            *arguments,
            propagate_exceptions=True,
        )
        if not results:
            raise RuntimeError("persistent callback registry lost its binding")
        return results[0]
