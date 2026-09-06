from __future__ import annotations

import pytest

from Infernux.ui import ui_event_entry


def test_event_parameter_reflection_failure_is_not_an_empty_signature(monkeypatch):
    class Target:
        def invoke(self, value: int) -> None:
            del value

    def reject_signature(_callback):
        raise ValueError("signature unavailable")

    monkeypatch.setattr(ui_event_entry.inspect, "signature", reject_signature)

    with pytest.raises(ValueError, match="signature unavailable"):
        ui_event_entry.get_method_parameter_specs(Target(), "invoke")


def test_event_parameter_type_hint_failure_is_not_treated_as_untyped(monkeypatch):
    class Target:
        def invoke(self, value: "MissingType") -> None:
            del value

    def reject_hints(*_args, **_kwargs):
        raise NameError("MissingType")

    monkeypatch.setattr(ui_event_entry, "get_type_hints", reject_hints)

    with pytest.raises(NameError, match="MissingType"):
        ui_event_entry.get_method_parameter_specs(Target(), "invoke")
