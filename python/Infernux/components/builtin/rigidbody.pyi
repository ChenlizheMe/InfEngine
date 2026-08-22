from __future__ import annotations

from enum import IntEnum, IntFlag
from typing import Any, Tuple, Union

from Infernux.components.builtin_component import BuiltinComponent

class RigidbodyConstraints(IntFlag):
    """Flags to constrain rigidbody motion."""
    None_ = 0
    FreezePositionX = 2
    FreezePositionY = 4
    FreezePositionZ = 8
    FreezePosition = 14
    FreezeRotationX = 16
    FreezeRotationY = 32
    FreezeRotationZ = 64
    FreezeRotation = 112
    FreezeAll = 126

class CollisionDetectionMode(IntEnum):
    """Collision detection algorithm used by the rigidbody."""
    Discrete = 0
    Continuous = 1
    ContinuousDynamic = 2

class RigidbodyInterpolation(IntEnum):
    """Interpolation mode for rigidbody movement smoothing."""
    None_ = 0
    Interpolate = 1

class Rigidbody(BuiltinComponent):
    """Controls physics simulation for the GameObject."""

    _cpp_type_name: str
    _component_category_: str

    # ---- CppProperty fields as properties ----

    @property
    def mass(self) -> float:
        """The mass of the rigidbody in kilograms."""
        ...
    @mass.setter
    def mass(self, value: float) -> None: ...

    @property
    def drag(self) -> float:
        """The linear drag coefficient."""
        ...
    @drag.setter
    def drag(self, value: float) -> None: ...

    @property
    def angular_drag(self) -> float:
        """The angular drag coefficient."""
        ...
    @angular_drag.setter
    def angular_drag(self, value: float) -> None: ...

    @property
    def use_gravity(self) -> bool:
        """Whether gravity affects this rigidbody."""
        ...
    @use_gravity.setter
    def use_gravity(self, value: bool) -> None: ...

    @property
    def is_kinematic(self) -> bool:
        """Whether the rigidbody is kinematic (not driven by physics)."""
        ...
    @is_kinematic.setter
    def is_kinematic(self, value: bool) -> None: ...

    @property
    def constraints(self) -> int:
        """The raw constraint flags as an integer bitmask."""
        ...
    @constraints.setter
    def constraints(self, value: int) -> None: ...

    @property
    def collision_detection_mode(self) -> CollisionDetectionMode:
        """The collision detection mode used by this rigidbody."""
        ...
    @collision_detection_mode.setter
    def collision_detection_mode(self, value: Union[CollisionDetectionMode, int]) -> None: ...

    @property
    def interpolation(self) -> RigidbodyInterpolation:
        """The interpolation mode for smoothing rigidbody movement."""
        ...
    @interpolation.setter
    def interpolation(self, value: Union[RigidbodyInterpolation, int]) -> None: ...

    @property
    def max_angular_velocity(self) -> float:
        """The maximum angular velocity in radians per second."""
        ...
    @max_angular_velocity.setter
    def max_angular_velocity(self, value: float) -> None: ...

    @property
    def max_linear_velocity(self) -> float:
        """The maximum linear velocity of the rigidbody."""
        ...
    @max_linear_velocity.setter
    def max_linear_velocity(self, value: float) -> None: ...

    # ---- Convenience properties ----

    @property
    def freeze_position_x(self) -> bool:
        """Whether X-axis position is frozen."""
        ...
    @freeze_position_x.setter
    def freeze_position_x(self, value: bool) -> None: ...

    @property
    def freeze_position_y(self) -> bool:
        """Whether Y-axis position is frozen."""
        ...
    @freeze_position_y.setter
    def freeze_position_y(self, value: bool) -> None: ...

    @property
    def freeze_position_z(self) -> bool:
        """Whether Z-axis position is frozen."""
        ...
    @freeze_position_z.setter
    def freeze_position_z(self, value: bool) -> None: ...

    @property
    def freeze_rotation_x(self) -> bool:
        """Whether X-axis rotation is frozen."""
        ...
    @freeze_rotation_x.setter
    def freeze_rotation_x(self, value: bool) -> None: ...

    @property
    def freeze_rotation_y(self) -> bool:
        """Whether Y-axis rotation is frozen."""
        ...
    @freeze_rotation_y.setter
    def freeze_rotation_y(self, value: bool) -> None: ...

    @property
    def freeze_rotation_z(self) -> bool:
        """Whether Z-axis rotation is frozen."""
        ...
    @freeze_rotation_z.setter
    def freeze_rotation_z(self, value: bool) -> None: ...

    @property
    def freeze_rotation(self) -> bool:
        """Shortcut to freeze or unfreeze all rotation axes."""
        ...
    @freeze_rotation.setter
    def freeze_rotation(self, value: bool) -> None: ...

    @property
    def constraints_flags(self) -> RigidbodyConstraints:
        """The constraint flags as a RigidbodyConstraints enum."""
        ...
    @constraints_flags.setter
    def constraints_flags(self, value: Union[RigidbodyConstraints, int]) -> None: ...

    def has_constraint(self, constraint: RigidbodyConstraints) -> bool:
        """Return whether the specified constraint flag is set."""
        ...
    def add_constraint(self, constraint: RigidbodyConstraints) -> None:
        """Add a constraint flag to the rigidbody."""
        ...
    def remove_constraint(self, constraint: RigidbodyConstraints) -> None:
        """Remove a constraint flag from the rigidbody."""
        ...

    # ---- Runtime-only properties ----

    @property
    def velocity(self) -> Any:
        """The linear velocity of the rigidbody in world space."""
        ...
    @velocity.setter
    def velocity(self, value: Any) -> None: ...

    @property
    def angular_velocity(self) -> Any:
        """The angular velocity of the rigidbody in radians per second."""
        ...
    @angular_velocity.setter
    def angular_velocity(self, value: Any) -> None: ...

    @property
    def world_center_of_mass(self) -> Any:
        """The center of mass in world space."""
        ...
    @property
    def position(self) -> Any:
        """The position of the rigidbody in world space."""
        ...
    @position.setter
    def position(self, value: Any) -> None: ...
    @property
    def rotation(self) -> Tuple[float, float, float, float]:
        """The rotation of the rigidbody as a quaternion (x, y, z, w)."""
        ...
    @rotation.setter
    def rotation(self, value: Any) -> None: ...

    # ---- Force / Torque API ----

    def add_force(self, force: Any, mode: Any = ...) -> None:
        """Apply a force to the rigidbody."""
        ...
    def add_torque(self, torque: Any, mode: Any = ...) -> None:
        """Apply a torque to the rigidbody."""
        ...
    def add_force_at_position(self, force: Any, position: Any, mode: Any = ...) -> None:
        """Apply a force at a specific world-space position."""
        ...

    # ---- Kinematic movement ----

    def move_position(self, position: Any) -> None:
        """Move the kinematic rigidbody to the specified position."""
        ...
    def move_rotation(self, rotation: Any) -> None:
        """Rotate the kinematic rigidbody to the specified rotation."""
        ...

    # ---- Sleep API ----

    def is_sleeping(self) -> bool:
        """Return whether the rigidbody is currently sleeping."""
        ...
    def wake_up(self) -> None:
        """Force the rigidbody to wake up."""
        ...
    def sleep(self) -> None:
        """Force the rigidbody to sleep."""
        ...
