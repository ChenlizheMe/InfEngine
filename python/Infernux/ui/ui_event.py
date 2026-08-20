"""UIEvent — lightweight callback list, similar to Unity's ``UnityEvent``.

Allows UI components (e.g. ``UIButton``) to expose events that scripts
can subscribe to at runtime.

Usage::

    btn = go.get_component(UIButton)
    btn.on_click.add_listener(my_handler)

    # Inside UIButton:
    self.on_click.invoke()
"""

from __future__ import annotations

from typing import Any, Callable
from Infernux.engine.runtime_dispatch import ReloadableCallbackRegistry


class UIEvent:
    """A multicast delegate / callback list.

    Call :meth:`invoke` to notify all registered listeners.
    Listeners are plain callables (no arguments by default).  For events
    that carry data, use :class:`UIEvent1`.
    """

    __slots__ = ("_registry",)

    def __init__(self):
        self._registry = ReloadableCallbackRegistry()

    def add_listener(self, callback: Callable[[], Any]) -> None:
        """Register *callback* to be called on :meth:`invoke`."""
        self._registry.add_listener(callback)

    def remove_listener(self, callback: Callable[[], Any]) -> None:
        """Unregister *callback*."""
        self._registry.remove_listener(callback)

    def remove_all_listeners(self) -> None:
        """Clear every registered listener."""
        self._registry.remove_all_listeners()

    def invoke(self) -> None:
        """Fire all listeners (order of registration)."""
        # Keep the historic UIEvent contract: listener exceptions propagate to
        # the caller instead of becoming an ignored diagnostic result.
        self._registry.invoke(propagate_exceptions=True)

    @property
    def listener_count(self) -> int:
        return self._registry.listener_count

    def __repr__(self):
        return f"UIEvent(listeners={self.listener_count})"


class UIEvent1:
    """A one-argument variant: ``UIEvent1[T]``.

    Usage::

        on_value_changed = UIEvent1()          # carries new value
        on_value_changed.add_listener(lambda v: print(v))
        on_value_changed.invoke(42)
    """

    __slots__ = ("_registry",)

    def __init__(self):
        self._registry = ReloadableCallbackRegistry()

    def add_listener(self, callback: Callable[[Any], Any]) -> None:
        self._registry.add_listener(callback)

    def remove_listener(self, callback: Callable[[Any], Any]) -> None:
        self._registry.remove_listener(callback)

    def remove_all_listeners(self) -> None:
        self._registry.remove_all_listeners()

    def invoke(self, arg: Any) -> None:
        self._registry.invoke(arg, propagate_exceptions=True)

    @property
    def listener_count(self) -> int:
        return self._registry.listener_count

    def __repr__(self):
        return f"UIEvent1(listeners={self.listener_count})"
