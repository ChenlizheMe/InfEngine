"""Type stubs for Infernux.components.component — InxComponent base class."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

from Infernux.lib._Infernux import (
    GameObject,
    MeshRenderer,
    Transform,
)
from Infernux.coroutine import Coroutine


class InxComponent:
    """Base class for Python-scripted game components (Unity-style lifecycle).

    Subclass this to create game logic scripts.  Use ``serialized_field()``
    class variables for Inspector-editable properties.

    Example::

        class PlayerController(InxComponent):
            speed: float = serialized_field(default=5.0, range=(0, 100))

            def start(self):
                Debug.log("PlayerController started")

            def update(self, delta_time: float):
                pos = self.transform.position
                self.transform.position = Vector3(
                    pos.x + self.speed * delta_time, pos.y, pos.z
                )
    """

    _serialized_fields_: Dict[str, Any]
    _component_category_: str
    _always_show: bool

    def __init__(self) -> None: ...

    @classmethod
    def reserve_instances(cls, count: int) -> None:
        """Preallocate numeric-field storage for bulk component creation."""
        ...

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def game_object(self) -> GameObject:
        """The GameObject this component is attached to during normal lifecycle use."""
        ...
    @property
    def transform(self) -> Transform:
        """Shortcut to ``self.game_object.transform`` during normal lifecycle use."""
        ...
    @property
    def is_valid(self) -> bool:
        """Whether the underlying GameObject reference is still alive."""
        ...
    @property
    def enabled(self) -> bool:
        """Whether the component is enabled."""
        ...
    @enabled.setter
    def enabled(self, value: bool) -> None: ...
    @property
    def type_name(self) -> str:
        """Class name of this component."""
        ...
    @property
    def execution_order(self) -> int:
        """Execution order (lower value runs earlier)."""
        ...
    @execution_order.setter
    def execution_order(self, value: int) -> None: ...
    @property
    def component_id(self) -> int:
        """Unique auto-incremented ID for this component instance."""
        ...

    # =========================================================================
    # Lifecycle methods — override in subclasses
    # =========================================================================

    def awake(self) -> None:
        """Called once when the component is first created."""
        ...
    def start(self) -> None:
        """Called before the first Update after the component is enabled."""
        ...
    def update(self, delta_time: float) -> None:
        """Called every frame."""
        ...
    def fixed_update(self, fixed_delta_time: float) -> None:
        """Called at a fixed time step (default 50 Hz).  Use for physics."""
        ...
    def late_update(self, delta_time: float) -> None:
        """Called every frame after all Update calls."""
        ...
    def destroy(self) -> None:
        """Remove this component from its owning GameObject (Unity-style).

        The component's ``on_destroy`` lifecycle hook will be called.
        """
        ...
    def on_destroy(self) -> None:
        """Called when the component or its GameObject is destroyed."""
        ...
    def on_enable(self) -> None:
        """Called when the component is enabled."""
        ...
    def on_disable(self) -> None:
        """Called when the component is disabled."""
        ...
    def on_validate(self) -> None:
        """Called when a serialized field is changed in the Inspector (editor only)."""
        ...
    def reset(self) -> None:
        """Called when the component is reset to defaults (editor only)."""
        ...
    def on_after_deserialize(self) -> None:
        """Called after deserialization (scene load / undo)."""
        ...
    def on_before_serialize(self) -> None:
        """Called before serialization (scene save)."""
        ...

    # =========================================================================
    # Physics collision / trigger callbacks
    # =========================================================================

    def on_collision_enter(self, collision: Any) -> None:
        """Called when this collider starts touching another collider."""
        ...
    def on_collision_stay(self, collision: Any) -> None:
        """Called every fixed-update while two colliders remain in contact."""
        ...
    def on_collision_exit(self, collision: Any) -> None:
        """Called when two colliders stop touching."""
        ...
    def on_trigger_enter(self, other: Any) -> None:
        """Called when another collider enters this trigger volume."""
        ...
    def on_trigger_stay(self, other: Any) -> None:
        """Called every fixed-update while another collider is inside this trigger."""
        ...
    def on_trigger_exit(self, other: Any) -> None:
        """Called when another collider exits this trigger volume."""
        ...

    # =========================================================================
    # Gizmo callbacks (editor only)
    # =========================================================================

    def on_draw_gizmos(self) -> None:
        """Called every frame in the editor to draw gizmos for this component."""
        ...
    def on_draw_gizmos_selected(self) -> None:
        """Called every frame in the editor ONLY when this object is selected."""
        ...

    # =========================================================================
    # Coroutine support
    # =========================================================================

    def start_coroutine(self, generator: Any) -> Coroutine:
        """Start a coroutine on this component.

        Args:
            generator: A generator object (call your generator function first).

        Returns:
            A Coroutine handle that can be passed to ``stop_coroutine()``
            or ``yield``-ed from another coroutine.
        """
        ...
    def stop_coroutine(self, coroutine: Coroutine) -> None:
        """Stop a specific coroutine previously started with ``start_coroutine()``."""
        ...
    def stop_all_coroutines(self) -> None:
        """Stop all coroutines running on this component."""
        ...

    # =========================================================================
    # Utility methods
    # =========================================================================

    def compare_tag(self, tag: str) -> bool:
        """Returns True if the attached GameObject's tag matches."""
        ...

    # =========================================================================
    # Tag & Layer convenience properties
    # =========================================================================

    @property
    def tag(self) -> str:
        """Tag of the attached GameObject."""
        ...
    @tag.setter
    def tag(self, value: str) -> None: ...
    @property
    def game_object_layer(self) -> int:
        """Layer index (0–31) of the attached GameObject."""
        ...
    @game_object_layer.setter
    def game_object_layer(self, value: int) -> None: ...

    # =========================================================================
    # Internal (binding layer API)
    # =========================================================================

    def _set_game_object(self, game_object: Optional[GameObject]) -> None: ...
    def _bind_native_component(self, cpp_component: Any, game_object: Optional[GameObject] = ...) -> None: ...
    def _detach_native_binding_for_replacement(self) -> None: ...
    def _finalize_play_domain_replacement(self, *, was_awake: bool) -> None: ...
    def _serialize_fields(self) -> str: ...
    def _serialize_fields_document(self) -> Dict[str, Any]: ...
    def _deserialize_fields(self, json_str: str, *, _skip_on_after_deserialize: bool = ..., repair: bool = ...) -> None: ...
    def _deserialize_fields_document(self, document: Dict[str, Any], *, _skip_on_after_deserialize: bool = ..., repair: bool = ...) -> None: ...
    @classmethod
    def _clear_all_instances(cls) -> None: ...
    def __repr__(self) -> str: ...
    @classmethod
    def _get_type_guid(cls) -> str: ...
    @classmethod
    def _get_intrinsic_script_guid(cls) -> str: ...
