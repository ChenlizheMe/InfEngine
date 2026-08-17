"""Tests for Infernux.renderstack — InjectionPoint, RenderPass, ResourceBus, FullScreenEffect (real C++ backend)."""

from __future__ import annotations

import sys
import types

import pytest

from Infernux.renderstack.injection_point import InjectionPoint
from Infernux.renderstack.resource_bus import ResourceBus
from Infernux.renderstack.render_pass import RenderPass
from Infernux.renderstack.fullscreen_effect import FullScreenEffect


def test_pipeline_file_callback_adopts_transactionally_published_module(monkeypatch):
    from Infernux.renderstack._render_pipeline_reload import PipelineReloadMixin
    import Infernux.renderstack.discovery as discovery

    module_name = "_infernux_test_pipeline_reload"
    previous_module = types.ModuleType(module_name)
    published_module = types.ModuleType(module_name)
    monkeypatch.setitem(sys.modules, module_name, published_module)
    invalidations = []
    monkeypatch.setattr(
        discovery,
        "invalidate_discovery_cache",
        lambda: invalidations.append("discovery"),
    )

    class Subject(PipelineReloadMixin):
        def __init__(self):
            self._pipeline_module = previous_module
            self._pipeline = object()
            self.saved = 0
            self.invalidated = 0

        def _save_current_pipeline_params(self):
            self.saved += 1

        def invalidate_graph(self):
            self.invalidated += 1

    subject = Subject()
    subject._on_pipeline_file_changed("pipeline.py")

    assert subject._pipeline_module is published_module
    assert subject._pipeline is None
    assert subject.saved == 1
    assert subject.invalidated == 1
    assert invalidations == ["discovery"]


def test_pipeline_catalog_change_filter_ignores_ordinary_component(tmp_path):
    from Infernux.renderstack.discovery import script_may_affect_pipeline_catalog

    component = tmp_path / "player.py"
    component.write_text(
        "from Infernux.components import InxComponent\n"
        "class Player(InxComponent):\n"
        "    pass\n",
        encoding="utf-8",
    )

    assert not script_may_affect_pipeline_catalog(str(component), "modified")


def test_pipeline_catalog_change_filter_accepts_pipeline_source(tmp_path):
    from Infernux.renderstack.discovery import script_may_affect_pipeline_catalog

    pipeline = tmp_path / "pipeline.py"
    pipeline.write_text(
        "from Infernux.renderstack import RenderPipeline\n"
        "class CustomPipeline(RenderPipeline):\n"
        "    pass\n",
        encoding="utf-8",
    )

    assert script_may_affect_pipeline_catalog(str(pipeline), "created")


def test_pipeline_discovery_finds_indirect_project_subclass(tmp_path):
    from Infernux.engine.project_context import get_project_root, set_project_root
    from Infernux.renderstack.discovery import discover_pipelines, invalidate_discovery_cache

    project = tmp_path / "IndirectPipelineProject"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    (assets / "pipeline_base.py").write_text(
        "from Infernux.renderstack import RenderPipeline\n"
        "class ProjectPipelineBase(RenderPipeline):\n"
        "    name = '_project_pipeline_base'\n",
        encoding="utf-8",
    )
    child = assets / "showcase_pipeline.py"
    child.write_text(
        "from pipeline_base import ProjectPipelineBase\n"
        "class IndirectShowcasePipeline(ProjectPipelineBase):\n"
        "    name = 'Indirect Showcase Regression'\n",
        encoding="utf-8",
    )

    previous = get_project_root()
    set_project_root(str(project))
    invalidate_discovery_cache()
    try:
        assert "RenderPipeline" not in child.read_text(encoding="utf-8")
        assert discover_pipelines()["Indirect Showcase Regression"].__name__ == (
            "IndirectShowcasePipeline"
        )
    finally:
        set_project_root(previous)
        invalidate_discovery_cache()


def test_pipeline_discovery_finds_project_subclass_of_builtin_pipeline(tmp_path):
    from Infernux.engine.project_context import get_project_root, set_project_root
    from Infernux.renderstack.discovery import discover_pipelines, invalidate_discovery_cache

    project = tmp_path / "BuiltinPipelineBaseProject"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    source = assets / "showcase_pipeline.py"
    source.write_text(
        "from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline\n"
        "class BuiltinDerivedShowcasePipeline(DefaultForwardPipeline):\n"
        "    name = 'Builtin Derived Showcase Regression'\n",
        encoding="utf-8",
    )

    previous = get_project_root()
    set_project_root(str(project))
    invalidate_discovery_cache()
    try:
        assert "class BuiltinDerivedShowcasePipeline(RenderPipeline)" not in source.read_text(
            encoding="utf-8"
        )
        assert discover_pipelines()["Builtin Derived Showcase Regression"].__name__ == (
            "BuiltinDerivedShowcasePipeline"
        )
    finally:
        set_project_root(previous)
        invalidate_discovery_cache()


def test_player_missing_custom_pipeline_is_not_silently_replaced(monkeypatch):
    from Infernux.renderstack.render_stack import RenderStack

    stack = RenderStack()
    stack.pipeline_class_name = "Missing Packaged Pipeline"
    monkeypatch.setenv("_INFERNUX_PLAYER_MODE", "1")
    monkeypatch.setattr(stack, "discover_pipelines", lambda: {})

    with pytest.raises(RuntimeError, match="Missing Packaged Pipeline"):
        stack._create_pipeline()

    assert stack.pipeline_class_name == "Missing Packaged Pipeline"


def test_effect_feature_lookup_discovers_project_registration_module(tmp_path):
    from Infernux.engine.project_context import get_project_root, set_project_root
    from Infernux.renderstack.discovery import invalidate_discovery_cache
    from Infernux.renderstack.render_effect_compiler import get_render_effect_feature

    project = tmp_path / "EffectFeatureDiscoveryProject"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    (assets / "project_effect.py").write_text(
        "from Infernux.renderstack import FullScreenEffect, render_effect_feature\n"
        "@render_effect_feature('tests.post.lazy_project_feature')\n"
        "class ProjectEffect(FullScreenEffect):\n"
        "    name = 'Project Effect Discovery Regression'\n",
        encoding="utf-8",
    )

    previous = get_project_root()
    set_project_root(str(project))
    invalidate_discovery_cache()
    try:
        feature = get_render_effect_feature("tests.post.lazy_project_feature")
        assert feature.effect_class.__name__ == "ProjectEffect"
    finally:
        set_project_root(previous)
        invalidate_discovery_cache()


def test_effect_feature_decorator_replaces_same_source_candidate_class(tmp_path):
    import importlib.util

    from Infernux.renderstack.render_effect_compiler import (
        get_render_effect_feature,
        render_effect_feature,
    )

    source = tmp_path / "replaceable_effect.py"
    source.write_text("class Effect:\n    pass\n", encoding="utf-8")

    def load(module_name):
        spec = importlib.util.spec_from_file_location(module_name, source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Effect

    first = load("effect_discovery_revision")
    second = load("effect_candidate_revision")
    first.__module__ = "detached_discovery_module"
    second.__module__ = "detached_candidate_module"
    render_effect_feature("tests.post.same_source_replacement")(first)
    render_effect_feature("tests.post.same_source_replacement")(second)

    assert get_render_effect_feature("tests.post.same_source_replacement").effect_class is second


# ══════════════════════════════════════════════════════════════════════
# InjectionPoint
# ══════════════════════════════════════════════════════════════════════

class TestInjectionPoint:
    def test_auto_display_name(self):
        ip = InjectionPoint(name="after_opaque")
        assert ip.display_name == "After Opaque"

    def test_explicit_display_name(self):
        ip = InjectionPoint(name="x", display_name="Custom")
        assert ip.display_name == "Custom"

    def test_default_resource_state(self):
        ip = InjectionPoint(name="test")
        assert ip.resource_state == {"color", "depth"}

    def test_custom_resource_state(self):
        ip = InjectionPoint(name="test", resource_state={"color"})
        assert ip.resource_state == {"color"}


# ══════════════════════════════════════════════════════════════════════
# ResourceBus
# ══════════════════════════════════════════════════════════════════════

class TestResourceBus:
    def test_empty_bus(self):
        bus = ResourceBus()
        assert bus.available_resources == set()
        assert bus.get("color") is None
        assert not bus.has("color")

    def test_set_and_get(self):
        bus = ResourceBus()
        bus.set("color", "handle_c")
        bus.set("depth", "handle_d")
        assert bus.get("color") == "handle_c"
        assert bus.has("depth")
        assert bus.available_resources == {"color", "depth"}

    def test_initial_resources(self):
        bus = ResourceBus(initial={"a": 1, "b": 2})
        assert bus.has("a")
        assert bus.get("b") == 2

    def test_snapshot(self):
        bus = ResourceBus(initial={"x": 10})
        snap = bus.snapshot()
        assert snap == {"x": 10}
        snap["x"] = 999
        assert bus.get("x") == 10

    def test_repr(self):
        bus = ResourceBus(initial={"a": 1, "b": 2})
        r = repr(bus)
        assert "a" in r
        assert "b" in r


# ══════════════════════════════════════════════════════════════════════
# RenderPass (base class)
# ══════════════════════════════════════════════════════════════════════

class TestRenderPass:
    def test_requires_name(self):
        with pytest.raises(ValueError, match="name"):
            RenderPass()

    def test_requires_injection_point(self):
        class NoIP(RenderPass):
            name = "test"
        with pytest.raises(ValueError, match="injection_point"):
            NoIP()

    def test_valid_subclass_constructs(self):
        class Good(RenderPass):
            name = "good"
            injection_point = "after_opaque"
        p = Good()
        assert p.enabled is True

    def test_validate_missing_resources(self):
        class NeedsColor(RenderPass):
            name = "need"
            injection_point = "x"
            requires = {"color", "depth"}
        p = NeedsColor()
        errors = p.validate({"color"})
        assert len(errors) > 0

    def test_validate_all_satisfied(self):
        class NeedsColor(RenderPass):
            name = "need"
            injection_point = "x"
            requires = {"color"}
        p = NeedsColor()
        errors = p.validate({"color", "depth"})
        assert errors == []


# ══════════════════════════════════════════════════════════════════════
# FullScreenEffect (base class)
# ══════════════════════════════════════════════════════════════════════

class TestFullScreenEffect:
    def test_default_resource_declarations(self):
        class TestFX(FullScreenEffect):
            name = "test_fx"
            injection_point = "before_post_process"
        fx = TestFX()
        assert "color" in fx.requires
        assert "color" in fx.modifies

    def test_enabled_default(self):
        class TestFX(FullScreenEffect):
            name = "test_fx"
            injection_point = "before_post_process"
        fx = TestFX()
        assert fx.enabled is True

    def test_disabled_construction(self):
        class TestFX(FullScreenEffect):
            name = "test_fx"
            injection_point = "before_post_process"
        fx = TestFX(enabled=False)
        assert fx.enabled is False

    def test_bind_buffers_uses_instance_resource_contract(self):
        from Infernux.renderstack.resource_bus import ResourceBus

        class TestFX(FullScreenEffect):
            name = "test_fx"
            injection_point = "before_post_process"
            requires = {"color", "depth", "normal"}
            modifies = {"color"}

        class RecordingPass:
            def set_textures(self, bindings):
                self.bindings = bindings

        render_pass = RecordingPass()
        bus = ResourceBus(
            {"color": object(), "depth": object(), "normal": object()}
        )

        TestFX().bind_buffers(render_pass, bus)

        assert set(render_pass.bindings) == {
            "_InxPassColor",
            "_InxPassDepth",
            "_InxPassNormal",
        }
