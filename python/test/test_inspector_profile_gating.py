from Infernux.engine.ui import inspector_support


def test_profile_metrics_are_noop_when_native_profiling_is_disabled(monkeypatch):
    monkeypatch.setattr(inspector_support, "_inspector_profile_enabled", False)
    inspector_support.consume_inspector_profile_metrics()

    inspector_support.record_inspector_profile_timing("material", 1.25)
    inspector_support.record_inspector_profile_count("material_count", 2.0)

    assert inspector_support.consume_inspector_profile_metrics() == {}


def test_profile_metrics_accumulate_when_native_profiling_is_enabled(monkeypatch):
    monkeypatch.setattr(inspector_support, "_inspector_profile_enabled", True)
    inspector_support.consume_inspector_profile_metrics()

    inspector_support.record_inspector_profile_timing("material", 1.25)
    inspector_support.record_inspector_profile_count("material_count", 2.0)

    assert inspector_support.consume_inspector_profile_metrics() == {
        "material": 1.25,
        "material_count": 2.0,
    }
