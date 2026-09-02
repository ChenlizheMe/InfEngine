from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.renderstack.fullscreen_effect import FullScreenEffect


def test_fullscreen_effect_exposes_invalid_serialized_default(monkeypatch):
    from Infernux.components import fields

    class BrokenEffect(FullScreenEffect):
        name = "Broken"
        injection_point = "before_post_process"

        def __setattr__(self, name, value):
            if name == "broken":
                raise TypeError("invalid effect default")
            super().__setattr__(name, value)

    monkeypatch.setattr(
        fields,
        "get_serialized_fields",
        lambda _effect_type: {"broken": SimpleNamespace(default=1)},
    )

    with pytest.raises(TypeError, match="invalid effect default"):
        BrokenEffect()
