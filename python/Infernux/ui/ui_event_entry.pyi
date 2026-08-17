"""Type stubs for Infernux.ui.ui_event_entry — serializable persistent event bindings."""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from Infernux.components import SerializableObject, GameObjectRef
from Infernux.components.ref_wrappers import ComponentRef


LIFECYCLE_METHODS: frozenset[str]
"""Method names excluded from the event method picker."""


class UIEventEntry(SerializableObject):
    """One persistent on-click binding: target GO -> component -> method.

    Attributes:
        target: Reference to the target GameObject.
        component_name: Component type name on the target.
        method_name: Public method to invoke.
        arguments: Persistent method arguments.
    """

    target: GameObjectRef
    component_name: str
    method_name: str
    arguments: list


class UIEventArgument(SerializableObject):
    """Persistent argument payload for one reflected button-event parameter.

    Attributes:
        kind: Argument type (``"int"``, ``"float"``, ``"bool"``, ``"string"``,
              ``"game_object"``, ``"component"``).
        name: Parameter name from the reflected method signature.
        component_type: For component-typed args, the target component type.
        int_value: Stored integer value.
        float_value: Stored float value.
        bool_value: Stored boolean value.
        string_value: Stored string value.
        game_object: Stored GameObject reference.
        component: Stored component reference.
    """

    kind: str
    name: str
    component_type: str
    int_value: int
    float_value: float
    bool_value: bool
    string_value: str
    game_object: GameObjectRef
    component: ComponentRef


class UIEventMethodParameter:
    """Reflected parameter specification for an event callback method."""

    name: str
    kind: str
    component_type: str
    default_value: Any

    def __init__(
        self,
        name: str,
        kind: str,
        component_type: str = ...,
        default_value: Any = ...,
    ) -> None: ...

    @property
    def display_name(self) -> str:
        """Human-readable label for editor display."""
        ...


def get_callable_methods(component: Any) -> List[str]:
    """Return public, non-lifecycle method names on *component*."""
    ...


def get_method_parameter_specs(component: Any, method_name: str) -> List[UIEventMethodParameter]:
    """Reflect the positional parameters of a bound callback method.

    Args:
        component: The component instance to inspect.
        method_name: Name of the method to reflect.
    """
    ...


def normalize_event_arguments(
    existing_args: List[UIEventArgument],
    specs: List[UIEventMethodParameter],
) -> List[UIEventArgument]:
    """Resize and retag stored arguments to match the current reflected signature."""
    ...


def materialize_event_arguments(
    entry: UIEventEntry,
    component: Any,
    *,
    specs: Optional[Sequence[UIEventMethodParameter]] = ...,
) -> List[Any]:
    """Return the runtime argument list for a bound event entry.

    Resolves ``GameObjectRef`` and ``ComponentRef`` to live objects.
    """
    ...
