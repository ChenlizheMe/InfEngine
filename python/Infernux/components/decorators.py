"""
Component decorators for Infernux.

Provides Unity-style component attributes:
    - @require_component: Declare dependency on another component type
    - @disallow_multiple: Prevent multiple instances of this component on a GameObject
    - @execute_in_edit_mode: Allow update() to run in edit mode

Example:
    from Infernux.components import InxComponent
    from Infernux.components.decorators import require_component, disallow_multiple
    
    @require_component(Rigidbody)
    @disallow_multiple
    class CharacterController(InxComponent):
        def start(self):
            self.rb = self.game_object.get_component(Rigidbody)
"""

from typing import Type, Union, Callable


def _refresh_component_registration(component_type: Type) -> None:
    from .registry import register_component_type

    register_component_type(component_type)


def require_component(*component_types: Type) -> Callable:
    """
    Decorator to declare that a component requires other component types.
    
    When this component is added to a GameObject, the engine will automatically
    add the required components if they don't already exist.
    
    Args:
        *component_types: One or more component types that are required
        
    Example:
        @require_component(Rigidbody, Collider)
        class PhysicsController(InxComponent):
            pass
    """
    def decorator(cls):
        # Copy inherited requirements before extending them. Mutating the
        # inherited list would silently contaminate the base class and sibling
        # component registrations.
        cls._require_components_ = list(
            getattr(cls, '_require_components_', ()) or ()
        )

        # Add all specified types (avoid duplicates)
        for comp_type in component_types:
            if comp_type not in cls._require_components_:
                cls._require_components_.append(comp_type)
        _refresh_component_registration(cls)
        return cls
    return decorator


def disallow_multiple(cls: Type = None) -> Union[Type, Callable]:
    """
    Decorator to prevent multiple instances of this component on a GameObject.
    
    When attempting to add a second instance of this component type, the
    engine will reject it and return None (with a warning).
    
    Can be used with or without parentheses:
        @disallow_multiple
        class MySingleton(InxComponent): pass
        
        @disallow_multiple()
        class MySingleton(InxComponent): pass
    """
    def apply(cls):
        cls._disallow_multiple_ = True
        _refresh_component_registration(cls)
        return cls
    
    # Support both @disallow_multiple and @disallow_multiple()
    if cls is not None:
        return apply(cls)
    return apply


def execute_in_edit_mode(cls: Type = None) -> Union[Type, Callable]:
    """
    Decorator to allow a component's update() to run in edit mode.
    
    By default, update() only runs during play mode. This decorator
    enables update() to also run in the editor for preview/gizmo purposes.
    
    Example:
        @execute_in_edit_mode
        class PreviewComponent(InxComponent):
            def update(self, delta_time):
                # This runs even in edit mode
                pass
    """
    def apply(cls):
        cls._execute_in_edit_mode_ = True
        return cls
    
    if cls is not None:
        return apply(cls)
    return apply


def add_component_menu(path: str) -> Callable:
    """
    Decorator to specify where this component appears in the Add Component menu.
    
    Args:
        path: Menu path like "Physics/Character Controller"
        
    Example:
        @add_component_menu("Custom/My Components/Special Controller")
        class SpecialController(InxComponent):
            pass
    """
    def decorator(cls):
        cls._component_menu_path_ = path
        from .registry import register_component_type
        register_component_type(cls)
        return cls
    return decorator


def icon(icon_path: str) -> Callable:
    """
    Decorator to specify a custom icon for this component in the inspector.
    
    Args:
        icon_path: Path to the icon image (relative to project assets)
        
    Example:
        @icon("icons/custom_component.png")
        class CustomComponent(InxComponent):
            pass
    """
    def decorator(cls):
        cls._component_icon_ = icon_path
        return cls
    return decorator


def help_url(url: str) -> Callable:
    """
    Decorator to specify a help URL for this component.
    
    Args:
        url: URL to documentation
        
    Example:
        @help_url("https://docs.myengine.com/components/player")
        class PlayerController(InxComponent):
            pass
    """
    def decorator(cls):
        cls._help_url_ = url
        return cls
    return decorator


# Convenience aliases for Unity-style naming
RequireComponent = require_component
DisallowMultipleComponent = disallow_multiple
ExecuteInEditMode = execute_in_edit_mode
AddComponentMenu = add_component_menu
HelpURL = help_url
Icon = icon
