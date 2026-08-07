"""Regression tests for the cached Python lifecycle dispatch path."""

from __future__ import annotations

from Infernux.components._component_lifecycle import ComponentLifecycleMixin


class _Probe(ComponentLifecycleMixin):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def update(self, value: float) -> None:
        self.calls.append(f"update:{value}")


def test_lifecycle_dispatch_reuses_bound_method_for_one_component():
    probe = _Probe()

    assert probe._safe_lifecycle_call("update", 1.0) is True
    cached = probe.__dict__["_lifecycle_dispatch_cache"]["update"]
    assert cached[0] is _Probe
    bound_method = cached[1]

    assert probe._safe_lifecycle_call("update", 2.0) is True
    assert probe.__dict__["_lifecycle_dispatch_cache"]["update"][1] is bound_method
    assert probe.calls == ["update:1.0", "update:2.0"]


def test_lifecycle_dispatch_refreshes_after_class_replacement():
    probe = _Probe()
    assert probe._safe_lifecycle_call("update", 1.0) is True

    class Replacement(_Probe):
        def update(self, value: float) -> None:
            self.calls.append(f"replacement:{value}")

    probe.__class__ = Replacement
    assert probe._safe_lifecycle_call("update", 2.0) is True
    assert probe.calls == ["update:1.0", "replacement:2.0"]
    assert probe.__dict__["_lifecycle_dispatch_cache"]["update"][0] is Replacement
