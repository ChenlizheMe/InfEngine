import json
from pathlib import Path

import pytest

from Infernux.components.fields import FieldType, get_raw_field_value, get_serialized_fields
from Infernux.core.asset_ref import RenderEffectRef
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.render_effect_asset import (
    EffectAssetReference,
    RenderEffectAsset,
    RenderEffectGroupAsset,
    RenderEffectGroupEntry,
    dump_render_effect_document,
)
from Infernux.renderstack.render_effect_compiler import (
    RenderEffectArtifactRegistry,
    RenderEffectCompileError,
    expand_render_effect_reference,
    publish_live_effect_group_document,
)
from Infernux.renderstack.render_stack import RenderStack
from Infernux.renderstack.render_pipeline import RenderPipeline


class _SingleCameraPipeline(RenderPipeline):
    name = "Single Camera Contract"

    def define_topology(self, graph):
        graph.create_texture("color", camera_target=True)
        with graph.add_pass("Opaque") as render_pass:
            render_pass.write_color("color")
            render_pass.draw_renderers()
        graph.set_output("color")


class _BufferedSingleCameraPipeline(RenderPipeline):
    name = "Buffered Single Camera Contract"

    def define_topology(self, graph):
        from Infernux.rendergraph.graph import Format

        color = graph.create_texture("color", camera_target=True)
        depth = graph.create_texture("depth", format=Format.D32_SFLOAT)
        normal = graph.create_texture("normal", format=Format.RGBA16_SFLOAT)
        motion = graph.create_texture("motion", format=Format.RG16_SFLOAT)
        with graph.add_pass("Opaque") as render_pass:
            render_pass.write_color(color)
            render_pass.write_depth(depth)
            render_pass.draw_renderers()
        result = graph.publish_pass_result(
            "opaque",
            {
                "color": color,
                "depth": depth,
                "normal": normal,
                "motion": motion,
            },
        )
        with graph.pass_result(result):
            graph.injection_point(
                "after_opaque",
                resources={"color", "depth", "normal", "motion"},
            )
        graph.set_output(color)


class _SingleCameraContext:
    def __init__(self):
        self.calls = []
        self.current_revision = 0

    def setup_camera_properties(self, camera):
        self.calls.append(("setup", camera))

    def cull(self, camera):
        self.calls.append(("cull", camera))
        return "culling"

    def is_graph_revision_current(self, revision):
        self.calls.append(("is_current", revision))
        return self.current_revision == revision

    def apply_graph(self, description):
        self.calls.append(("apply", description.source_revision))
        self.current_revision = description.source_revision

    def submit_culling(self, culling):
        self.calls.append(("submit", culling))


def test_render_pipeline_callback_owns_exactly_one_camera_per_context():
    pipeline = _SingleCameraPipeline()
    context = _SingleCameraContext()
    camera = object()

    pipeline.render(context, camera)

    assert context.calls[0:2] == [("setup", camera), ("cull", camera)]
    assert context.calls[-1] == ("submit", "culling")
    assert sum(call[0] == "setup" for call in context.calls) == 1
    assert sum(call[0] == "cull" for call in context.calls) == 1
    assert sum(call[0] == "submit" for call in context.calls) == 1


def test_render_pipeline_camera_filter_does_not_touch_rejected_context():
    class _RejectedPipeline(_SingleCameraPipeline):
        def should_render_camera(self, camera):
            return False

    context = _SingleCameraContext()
    _RejectedPipeline().render(context, object())

    assert context.calls == []


def test_render_stack_forces_after_screen_ui_for_custom_pipeline():
    stack = RenderStack()
    stack._pipeline = _BufferedSingleCameraPipeline()
    stack._pipeline._render_stack = stack
    stack.effect_slots = [
        EffectSlot(
            stage_id="after_screen_ui",
            effect=RenderEffectRef(
                RenderEffect(
                    RenderEffectAsset(
                        feature_type="infernux.post.motion_blur",
                        parameters={
                            "intensity": 0.5,
                            "max_blur_pixels": 8.0,
                            "depth_rejection": 1.0,
                        },
                    )
                )
            ),
        )
    ]
    stack.invalidate_graph()

    probe = stack._build_full_topology_probe()
    sequence = list(probe.topology_sequence)
    stage = next(
        value
        for value in probe.effect_stages
        if value.stable_id == "after_screen_ui"
    )

    assert {"color", "depth", "normal", "motion"} <= set(stage.contract.inputs)
    assert sequence.index(("pass", "_DisplayEncode")) < sequence.index(
        ("pass", "_ScreenUI_Overlay")
    )
    assert sequence.index(("pass", "_ScreenUI_Overlay")) < sequence.index(
        ("effect_stage", "after_screen_ui")
    )

    description = stack.build_graph()
    pass_names = [render_pass.name for render_pass in description.passes]
    assert "_DisplayEncode" in pass_names
    assert "_ScreenUI_Overlay" in pass_names
    assert any(name.endswith("MotionBlur_Apply") for name in pass_names)
    assert stack.effect_compile_errors == ()


def test_render_effect_has_material_like_typed_parameter_api():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5},
        )
    )

    effect.set_float("intensity", 1.25)
    effect.set_int("samples", 8)
    effect.set_color("tint", 1.0, 0.5, 0.25, 1.0)

    assert effect.get_float("intensity") == pytest.approx(1.25)
    assert effect.get_int("samples") == 8
    assert effect.get_color("tint") == pytest.approx((1.0, 0.5, 0.25, 1.0))
    assert effect.revision == 3
    assert effect.feature_type == "infernux.post.bloom"


def test_render_effect_inspector_edit_updates_shared_instance_and_queues_snapshot(
    tmp_path,
    monkeypatch,
):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.ui.render_effect_inspector import (
        apply_render_effect_parameter_edit,
    )
    from Infernux.engine.interaction import (
        DocumentKind,
        DocumentRegistry,
        ensure_editable_resource_document,
    )
    from Infernux.engine.undo import UndoManager

    path = tmp_path / "Bloom.effect"
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5, "max_iterations": 3},
        ),
        file_path=str(path),
        guid="bloom-guid",
    )
    snapshots = []
    scheduled = []
    monkeypatch.setattr(
        AssetManager,
        "set_render_effect_save_snapshot",
        classmethod(
            lambda _cls, asset_path, text, **_kwargs: snapshots.append(
                (asset_path, text)
            )
        ),
    )
    monkeypatch.setattr(
        AssetManager,
        "schedule_asset_save",
        classmethod(
            lambda _cls, category, key, resource, debounce_sec=0.0: scheduled.append(
                (category, key, resource, debounce_sec)
            )
        ),
    )

    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    DocumentRegistry()
    UndoManager()
    controller = ensure_editable_resource_document(
        category="render_effect",
        document_kind=DocumentKind.RENDER_EFFECT,
        file_path=str(path),
        resource=effect,
        guid=effect.guid,
    )
    try:
        assert apply_render_effect_parameter_edit(
            effect,
            "intensity",
            1.75,
            resource_controller=controller,
        )

        assert effect.get_float("intensity") == pytest.approx(1.75)
        assert effect.revision == 1
        assert json.loads(snapshots[-1][1])["parameters"]["intensity"] == pytest.approx(1.75)
        assert scheduled[-1][:3] == ("render_effect", str(path), effect)
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager


def test_render_effect_inspector_rejects_edits_without_a_document():
    from Infernux.engine.ui.render_effect_inspector import (
        apply_render_effect_parameter_edit,
    )

    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5, "max_iterations": 3},
        )
    )
    assert not apply_render_effect_parameter_edit(effect, "intensity", 1.75)
    assert effect.get_float("intensity") == pytest.approx(0.5)


def test_render_effect_inspector_skips_edit_path_for_unchanged_fields(monkeypatch):
    from Infernux.engine.ui import render_effect_inspector as inspector
    from Infernux.renderstack import render_effect_compiler

    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5, "max_iterations": 3},
        )
    )
    edit_calls = []
    instantiate_calls = []
    feature = render_effect_compiler.get_render_effect_feature(effect.feature_type)
    original_instantiate = feature.instantiate

    class CountingFeature:
        type_id = feature.type_id
        effect_class = feature.effect_class

        @staticmethod
        def instantiate(source):
            instantiate_calls.append(source.revision)
            return original_instantiate(source)

    monkeypatch.setattr(
        render_effect_compiler,
        "get_render_effect_feature",
        lambda _type_id: CountingFeature,
    )
    monkeypatch.setattr(inspector, "max_label_w", lambda *_args: 80.0)
    monkeypatch.setattr(
        inspector,
        "render_serialized_field",
        lambda _ctx, _widget, _label, _metadata, current, _width: current,
    )
    monkeypatch.setattr(
        inspector,
        "apply_render_effect_parameter_edit",
        lambda *_args: edit_calls.append(_args) or True,
    )
    ctx = type(
        "Context",
        (),
        {"is_item_hovered": lambda self: False, "set_tooltip": lambda self, _text: None},
    )()

    assert inspector.render_render_effect_parameters(ctx, effect) is False
    assert inspector.render_render_effect_parameters(ctx, effect) is False
    assert instantiate_calls == [0]
    assert edit_calls == []

    effect.set_float("intensity", 0.75)
    assert inspector.render_render_effect_parameters(ctx, effect) is False
    assert instantiate_calls == [0, 1]


def test_render_effect_debounced_save_uses_document_store_worker(tmp_path):
    from Infernux.core.assets import AssetManager
    from Infernux.core.document_store import DocumentStore

    path = tmp_path / "Bloom.effect"
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5, "max_iterations": 3},
        ),
        file_path=str(path),
        guid="bloom-guid",
    )
    try:
        effect.set_float("intensity", 2.25)
        ticket = AssetManager.flush_scheduled_saves(str(path), force=True)
        assert ticket is not True
        assert ticket.is_complete is False
        DocumentStore.flush(str(path))
        AssetManager.poll_pending_asset_writes()

        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["parameters"]["intensity"] == pytest.approx(2.25)
        assert effect._save_pending is False
    finally:
        AssetManager._scheduled_saves.pop(str(path), None)
        AssetManager._render_effect_save_snapshots.pop(str(path), None)
        AssetManager._pending_document_writes.pop(str(path), None)
        AssetManager._pending_document_write_callbacks.pop(str(path), None)
        RenderEffect._pending_saves.discard(effect)


def test_render_effect_failed_write_remains_scheduled_for_retry(tmp_path, monkeypatch):
    from Infernux.core.assets import AssetManager

    path = tmp_path / "Retry.effect"
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5},
        ),
        file_path=str(path),
        guid="retry-guid",
    )
    scheduled = []
    monkeypatch.setattr(
        AssetManager,
        "schedule_asset_save",
        classmethod(
            lambda _cls, category, key, resource, debounce_sec=0.0: scheduled.append(
                (category, key, resource, debounce_sec)
            )
        ),
    )

    effect._save_pending = True
    RenderEffect._pending_saves.add(effect)
    effect._on_save_completed("failed")

    assert effect._save_pending is True
    assert effect in RenderEffect._pending_saves
    assert scheduled[-1][:3] == ("render_effect", str(path), effect)
    RenderEffect._pending_saves.discard(effect)


def test_material_snapshot_save_returns_document_store_ticket(tmp_path):
    from Infernux.core.assets import AssetManager
    from Infernux.core.document_store import DocumentStore

    path = tmp_path / "Surface.mat"

    class _MaterialResource:
        file_path = str(path)

        @staticmethod
        def serialize():
            return '{"name":"Surface","properties":{}}'

    normalized = str(path).replace("\\", "/").lower()
    try:
        AssetManager.set_material_save_snapshot(
            str(path),
            '{"name":"Surface","properties":{"roughness":0.4}}',
        )
        ticket = AssetManager._save_material_resource(_MaterialResource())

        assert ticket is not False
        assert ticket.is_complete is False
        DocumentStore.flush(str(path))
        AssetManager.poll_pending_asset_writes()

        assert ticket.status == "succeeded"
        assert json.loads(path.read_text(encoding="utf-8"))["properties"][
            "roughness"
        ] == pytest.approx(0.4)
    finally:
        AssetManager._material_save_snapshots.pop(normalized, None)
        AssetManager._pending_document_writes.pop(normalized, None)
        AssetManager._pending_document_write_callbacks.pop(normalized, None)


def test_tonemapping_effect_accepts_integer_enum_source_value(tmp_path):
    RenderEffectArtifactRegistry.clear()
    path = tmp_path / "Tone.effect"
    path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.tonemapping",
                parameters={"mode": 2, "exposure": 1.0},
            )
        ),
        encoding="utf-8",
    )

    artifact, _document = RenderEffectArtifactRegistry.compile_and_publish(str(path))

    assert artifact.features[0]["feature_type"] == "infernux.post.tonemapping"
    assert artifact.features[0]["route_policy"] == "mask_and_modify"


def test_tonemapping_effect_rejects_removed_gamma_parameter(tmp_path):
    RenderEffectArtifactRegistry.clear()
    path = tmp_path / "LegacyTone.effect"
    path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.tonemapping",
                parameters={"mode": 2, "exposure": 1.0, "gamma": 2.2},
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(RenderEffectCompileError, match="unknown parameters: \\['gamma'\\]"):
        RenderEffectArtifactRegistry.compile_and_publish(str(path))


def test_render_effect_clone_is_runtime_only_and_parameter_isolated():
    shared = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5},
        ),
        file_path="Assets/Effects/Bloom.effect",
        guid="bloom-guid",
    )

    instance = shared.clone()
    instance.set_float("intensity", 2.0)

    assert shared.get_float("intensity") == pytest.approx(0.5)
    assert instance.get_float("intensity") == pytest.approx(2.0)
    assert instance.guid == ""
    assert instance.file_path == ""


def test_render_effect_save_and_load_round_trip(tmp_path):
    path = tmp_path / "Bloom.effect"
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"threshold": 1.0},
        )
    )
    assert effect.save(str(path))

    loaded = RenderEffect.load(str(path))

    assert loaded is not None
    assert loaded.feature_type == "infernux.post.bloom"
    assert loaded.get_float("threshold") == pytest.approx(1.0)


def _write_effect(path: Path, *, intensity=0.5, max_iterations=3, extra=None):
    parameters = {
        "intensity": intensity,
        "max_iterations": max_iterations,
    }
    parameters.update(extra or {})
    path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.bloom",
                parameters=parameters,
            )
        ),
        encoding="utf-8",
    )


def test_render_effect_aot_artifact_distinguishes_dynamic_and_structural_edits(
    tmp_path,
    monkeypatch,
):
    from Infernux.engine import project_context

    RenderEffectArtifactRegistry.clear()
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(tmp_path))
    path = tmp_path / "Assets" / "Bloom.effect"
    path.parent.mkdir()
    _write_effect(path, intensity=0.5, max_iterations=3)

    first, _ = RenderEffectArtifactRegistry.compile_and_publish(
        str(path), guid="bloom-guid"
    )
    first_generation = RenderEffectArtifactRegistry.topology_generation()
    assert Path(first.artifact_path).is_file()

    _write_effect(path, intensity=1.5, max_iterations=3)
    dynamic, _ = RenderEffectArtifactRegistry.compile_and_publish(
        str(path), guid="bloom-guid"
    )
    assert dynamic.revision > first.revision
    assert dynamic.structural_hash == first.structural_hash
    assert RenderEffectArtifactRegistry.topology_generation() == first_generation

    _write_effect(path, intensity=1.5, max_iterations=5)
    structural, _ = RenderEffectArtifactRegistry.compile_and_publish(
        str(path), guid="bloom-guid"
    )
    assert structural.structural_hash != dynamic.structural_hash
    assert RenderEffectArtifactRegistry.topology_generation() == first_generation + 1


def test_render_effect_failed_compile_preserves_last_known_good_artifact(
    tmp_path,
    monkeypatch,
):
    from Infernux.engine import project_context

    RenderEffectArtifactRegistry.clear()
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(tmp_path))
    path = tmp_path / "Assets" / "Bloom.effect"
    path.parent.mkdir()
    _write_effect(path)
    published, _ = RenderEffectArtifactRegistry.compile_and_publish(
        str(path), guid="bloom-guid"
    )
    artifact_text = Path(published.artifact_path).read_text(encoding="utf-8")

    _write_effect(path, extra={"not_a_bloom_parameter": 1.0})
    with pytest.raises(RenderEffectCompileError, match="unknown parameters"):
        RenderEffectArtifactRegistry.compile_and_publish(
            str(path), guid="bloom-guid"
        )

    assert RenderEffectArtifactRegistry.get(str(path), "bloom-guid") == published
    assert Path(published.artifact_path).read_text(encoding="utf-8") == artifact_text


def test_render_effect_load_reuses_matching_persisted_artifact(tmp_path, monkeypatch):
    from Infernux.engine import project_context

    RenderEffectArtifactRegistry.clear()
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(tmp_path))
    path = tmp_path / "Assets" / "Bloom.effect"
    path.parent.mkdir()
    _write_effect(path)
    published, _ = RenderEffectArtifactRegistry.compile_and_publish(
        str(path), guid="bloom-guid"
    )
    RenderEffectArtifactRegistry.clear()

    def fail_compile(_cls, _document, _source_path, _guid):
        raise AssertionError("matching AOT artifact should avoid feature recompilation")

    monkeypatch.setattr(
        RenderEffectArtifactRegistry,
        "_compile_document",
        classmethod(fail_compile),
    )
    restored, _ = RenderEffectArtifactRegistry.compile_and_publish(
        str(path), guid="bloom-guid"
    )

    assert restored.source_hash == published.source_hash
    assert restored.structural_hash == published.structural_hash
    assert restored.artifact_path == published.artifact_path


def test_render_effect_rebuilds_artifact_missing_current_route_policy(
    tmp_path,
    monkeypatch,
):
    from Infernux.engine import project_context

    RenderEffectArtifactRegistry.clear()
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(tmp_path))
    path = tmp_path / "Assets" / "Bloom.effect"
    path.parent.mkdir()
    _write_effect(path)
    published, _ = RenderEffectArtifactRegistry.compile_and_publish(
        str(path), guid="bloom-guid"
    )
    payload = json.loads(Path(published.artifact_path).read_text(encoding="utf-8"))
    payload["features"][0].pop("route_policy")
    Path(published.artifact_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    RenderEffectArtifactRegistry.clear()

    original_compile = RenderEffectArtifactRegistry._compile_document
    compile_calls = []

    def track_compile(_cls, document, source_path, guid):
        compile_calls.append(source_path)
        return original_compile(document, source_path, guid)

    monkeypatch.setattr(
        RenderEffectArtifactRegistry,
        "_compile_document",
        classmethod(track_compile),
    )
    rebuilt, _ = RenderEffectArtifactRegistry.compile_and_publish(
        str(path), guid="bloom-guid"
    )
    rebuilt_payload = json.loads(
        Path(rebuilt.artifact_path).read_text(encoding="utf-8")
    )

    assert compile_calls == [str(path)]
    assert rebuilt_payload["features"][0]["route_policy"] == "additive_extract"


def test_asset_publish_updates_loaded_render_effect_in_place(tmp_path, monkeypatch):
    from Infernux.core.assets import AssetManager
    from Infernux.engine import project_context

    RenderEffectArtifactRegistry.clear()
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(tmp_path))
    path = tmp_path / "Assets" / "Bloom.effect"
    path.parent.mkdir()
    _write_effect(path, intensity=0.5)
    loaded = RenderEffect.load(str(path))
    assert loaded is not None
    monkeypatch.setattr(
        AssetManager,
        "_get_cached",
        classmethod(lambda _cls, guid: loaded if guid == "bloom-guid" else None),
    )

    assert AssetManager._compile_render_effect_runtime(str(path), "bloom-guid") == ""
    first_revision = loaded.artifact_revision
    _write_effect(path, intensity=2.0)
    assert AssetManager._compile_render_effect_runtime(str(path), "bloom-guid") == ""

    assert loaded.get_float("intensity") == pytest.approx(2.0)
    assert loaded.artifact_revision > first_revision


def test_render_stack_effect_slots_use_structured_serialized_list():
    fields = get_serialized_fields(RenderStack)
    metadata = fields["effect_slots"]

    assert metadata.field_type is FieldType.LIST
    assert metadata.element_type is FieldType.SERIALIZABLE_OBJECT
    assert metadata.element_class is EffectSlot

    stack = RenderStack()
    slot = stack.add_effect_slot(
        "final",
        RenderEffectRef(guid="bloom-guid", path_hint="Assets/Effects/Bloom.effect"),
    )
    document = stack._serialize_fields_document()

    assert isinstance(document["effect_slots"], list)
    assert document["effect_slots"][0]["$type"] == "serializable_object"
    assert "effect_stage_bindings_json" not in document
    assert slot.effect_ref.guid == "bloom-guid"


def test_render_stack_structured_slots_round_trip_without_hidden_json():
    stack = RenderStack()
    stack.add_effect_slot(
        "after_sky",
        RenderEffectRef(guid="fog-guid", path_hint="Assets/Effects/Fog.effect"),
        enabled=False,
    )

    restored = RenderStack()
    restored._deserialize_fields_document(stack._serialize_fields_document())

    slots = restored.get_effect_stage_slots("after_sky")
    assert len(slots) == 1
    assert slots[0].enabled is False
    assert slots[0].effect_ref.guid == "fog-guid"
    assert not hasattr(restored, "effect_stage_bindings_json")


def test_render_stack_serializes_an_explicit_default_pipeline_name():
    stack = RenderStack()

    document = stack._serialize_fields_document()

    assert document["pipeline_class_name"] == "Default Forward"


def test_render_stack_normalizes_the_removed_empty_default_sentinel():
    stack = RenderStack()
    document = stack._serialize_fields_document()
    document["pipeline_class_name"] = ""

    stack._deserialize_fields_document(document)

    assert stack.pipeline_class_name == "Default Forward"


def test_render_stack_rejects_obsolete_json_binding_field():
    stack = RenderStack()
    with pytest.raises(ValueError, match="removed fields"):
        stack._deserialize_fields_document({"effect_stage_bindings_json": "{}"})


@pytest.mark.parametrize(
    "removed_field",
    [
        "effect_stage_bindings_json",
        "mounted_passes_json",
    ],
)
def test_render_stack_rejects_removed_storage_fields(removed_field):
    stack = RenderStack()
    with pytest.raises(ValueError, match=removed_field):
        stack._deserialize_fields_document({removed_field: "[]"})


def test_slot_effect_property_resolves_to_mutable_runtime_asset(tmp_path):
    path = tmp_path / "Bloom.effect"
    path.write_text(
        json.dumps(
            {
                "$schema": "infernux.render_effect",
                "feature_type": "infernux.post.bloom",
                "parameters": {"intensity": 0.5},
                "dependencies": [],
            }
        ),
        encoding="utf-8",
    )
    slot = EffectSlot(stage_id="final", effect=RenderEffectRef(path_hint=str(path)))
    raw_ref = get_raw_field_value(slot, "effect")

    effect = slot.effect
    assert effect is not None
    effect._suppress_auto_save = True
    effect.set_float("intensity", 1.5)

    assert raw_ref.resolve().get_float("intensity") == pytest.approx(1.5)


def test_default_pipeline_declares_effect_stages_in_topology_order():
    stack = RenderStack()

    assert [stage.stable_id for stage in stack.effect_stages] == [
        "after_opaque",
        "after_sky",
        "after_transparent",
        "after_camera_ui",
        "final",
        "after_screen_ui",
    ]

    topology = stack._build_full_topology_probe().topology_sequence
    assert topology.index(("pass", "_ScreenUI_Camera")) < topology.index(
        ("effect_stage", "after_camera_ui")
    )
    assert topology.index(("effect_stage", "after_camera_ui")) < topology.index(
        ("effect_stage", "final")
    )
    assert topology.index(("pass", "_ScreenUI_Overlay")) < topology.index(
        ("effect_stage", "after_screen_ui")
    )


def test_render_stack_removes_obsolete_screen_ui_parameter_on_deserialize():
    stack = RenderStack()
    stack.pipeline_params_json = json.dumps(
        {
            "__default__": {
                "shadow_resolution": 4096,
                "enable_screen_ui": False,
            },
            "Default Deferred": {"enable_screen_ui": True},
        }
    )

    stack.on_after_deserialize()

    restored = json.loads(stack.pipeline_params_json)
    assert restored == {
        "__default__": {"shadow_resolution": 4096},
        "Default Deferred": {},
    }


def test_empty_render_stack_matches_no_stack_default_graph():
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline

    fallback_graph = RenderGraph("Fallback")
    DefaultForwardPipeline().define_topology(fallback_graph)
    fallback_graph.set_output("color")
    fallback = fallback_graph.build()

    stacked = RenderStack().build_graph()

    texture_signature = lambda description: [
        (
            texture.name,
            texture.format,
            texture.is_backbuffer,
            texture.is_depth,
            texture.width,
            texture.height,
            texture.size_divisor,
            texture.samples,
        )
        for texture in description.textures
    ]
    pass_signature = lambda description: [
        (
            render_pass.name,
            render_pass.type,
            tuple(render_pass.read_textures),
            tuple(render_pass.write_colors),
            render_pass.write_depth,
            render_pass.resolve_color,
            render_pass.clear_color,
            render_pass.clear_depth,
            tuple(command.type for command in render_pass.commands),
        )
        for render_pass in description.passes
    ]

    assert stacked.output_texture == fallback.output_texture
    assert stacked.msaa_samples == fallback.msaa_samples
    assert texture_signature(stacked) == texture_signature(fallback)
    assert pass_signature(stacked) == pass_signature(fallback)


def test_render_stack_rejects_undeclared_stage_but_preserves_orphan_slots():
    stack = RenderStack()
    orphan = EffectSlot(stage_id="removed_stage")
    stack.effect_slots = [orphan]

    assert stack.orphan_effect_slots == (orphan,)
    with pytest.raises(ValueError, match="does not declare EffectStage"):
        stack.add_effect_slot("removed_stage")
    assert stack.effect_slots == [orphan]


def test_render_stack_can_explicitly_remap_preserved_orphan_slots():
    stack = RenderStack()
    first = EffectSlot(stage_id="removed_stage")
    second = EffectSlot(stage_id="removed_stage")
    stack.effect_slots = [first, second]

    assert stack.remap_orphan_effect_stage("removed_stage", "final") == 2
    assert stack.get_effect_stage_slots("final") == (first, second)
    assert stack.orphan_effect_slots == ()


def test_effect_stage_inspector_routes_list_edits_to_render_stack_service():
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui.inspector_declarative import InspectorList
    from Infernux.engine.ui.inspector_renderstack import build_renderstack_inspector_model
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_core = EditorInteractionCore.instance()
    core = EditorInteractionCore()
    manager = UndoManager(core.action_journal)
    stack = RenderStack()
    first = EffectSlot(stage_id="")
    second = EffectSlot(stage_id="")
    try:
        model = build_renderstack_inspector_model(stack)
        control = next(
            control
            for section in model.sections
            for control in section.controls
            if isinstance(control, InspectorList)
            and control.key == "stage_final"
        )
        assert control.on_change is not None

        control.on_change(stack, control.field_name, [], [first, second])
        live_slots = control.value()
        assert [slot.stage_id for slot in live_slots] == ["final", "final"]
        assert all(slot.slot_id for slot in live_slots)

        control.on_change(stack, control.field_name, live_slots, [second])
        assert len(stack.get_effect_stage_slots("final")) == 1
        assert stack.get_effect_stage_slots("final")[0].stage_id == "final"
        manager.undo()
        assert len(stack.get_effect_stage_slots("final")) == 2
        assert all(
            slot.stage_id == "final"
            for slot in stack.get_effect_stage_slots("final")
        )
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        manager.clear()
        UndoManager._instance = previous_manager


def test_render_effect_picker_accepts_effect_groups():
    from Infernux.core.asset_ref import get_asset_type_config

    config = get_asset_type_config("RenderEffect")
    assert config["extensions"] == ("*.effect", "*.effectgroup")


def test_render_stack_compiles_effect_stage_to_scoped_dynamic_blocks():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.tonemapping",
            parameters={"exposure": 1.25},
        )
    )
    stack = RenderStack()
    slot = stack.add_effect_slot("final", RenderEffectRef(effect=effect))

    description = stack.build_graph()
    effect_pass = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("ToneMap_Apply")
    )
    command = effect_pass.commands[0]

    assert f"final/{slot.slot_id}/0" in effect_pass.name
    assert command.parameter_block.startswith(f"effect/final/{slot.slot_id}/0/")
    assert dict(command.push_constants)["exposure"] == pytest.approx(1.25)
    assert any(render_pass.name.endswith("final/Commit") for render_pass in description.passes)
    assert stack.effect_compile_errors == ()


def test_render_stack_batches_only_changed_effect_parameters_without_rebuild():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.tonemapping",
            parameters={"exposure": 1.0},
        )
    )
    stack = RenderStack()
    stack.add_effect_slot("final", RenderEffectRef(effect=effect))
    stack._graph_desc = stack.build_graph()

    class Context:
        graph_instance_id = 17

    requires_rebuild, initial = stack._collect_effect_parameter_updates(Context())
    assert requires_rebuild is False
    assert initial
    assert stack._collect_effect_parameter_updates(Context()) == (False, [])

    effect.set_float("exposure", 2.0)
    requires_rebuild, updates = stack._collect_effect_parameter_updates(Context())

    assert requires_rebuild is False
    assert any(dict(update.values).get("exposure") == 2.0 for update in updates)


def test_motion_blur_consumes_color_depth_and_resolved_motion():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.motion_blur",
            parameters={
                "intensity": 1.25,
                "max_blur_pixels": 48.0,
                "depth_rejection": 2.0,
            },
        )
    )
    stack = RenderStack()
    stack.add_effect_slot("final", RenderEffectRef(effect=effect))

    description = stack.build_graph()
    motion_blur = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("MotionBlur_Apply")
    )
    command = motion_blur.commands[0]
    bindings = dict(command.input_bindings)
    constants = dict(command.push_constants)

    assert command.shader_name == "Motion Blur"
    assert bindings["_SourceTex"]
    assert bindings["_DepthTex"] == "depth"
    assert bindings["_MotionTex"] == "_result/opaque/motion"
    assert set(motion_blur.read_textures) >= {
        bindings["_SourceTex"],
        "depth",
        bindings["_MotionTex"],
    }
    assert constants == {
        "intensity": pytest.approx(1.25),
        "maxBlurPixels": pytest.approx(48.0),
        "depthRejection": pytest.approx(2.0),
        "_pad0": pytest.approx(0.0),
    }
    assert stack.effect_compile_errors == ()


def test_motion_blur_parameters_update_without_graph_rebuild():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.motion_blur",
            parameters={
                "intensity": 1.0,
                "max_blur_pixels": 32.0,
                "depth_rejection": 1.0,
            },
        )
    )
    stack = RenderStack()
    stack.add_effect_slot("final", RenderEffectRef(effect=effect))
    stack._graph_desc = stack.build_graph()

    class Context:
        graph_instance_id = 29

    assert stack._collect_effect_parameter_updates(Context())[0] is False
    effect.set_float("max_blur_pixels", 64.0)
    requires_rebuild, updates = stack._collect_effect_parameter_updates(Context())

    assert requires_rebuild is False
    assert any(
        dict(update.values).get("maxBlurPixels") == pytest.approx(64.0)
        for update in updates
    )


def test_temporal_aa_consumes_motion_and_commits_typed_history():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.temporal_aa",
            parameters={
                "feedback": 0.92,
                "motion_rejection": 0.1,
                "depth_rejection": 128.0,
            },
        )
    )
    stack = RenderStack()
    stack.add_effect_slot("final", RenderEffectRef(effect=effect))

    description = stack.build_graph()
    resolve = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("TAA_Resolve")
    )
    commit = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("TAA_CommitHistory")
    )
    command = resolve.commands[0]
    bindings = dict(command.input_bindings)
    textures = {texture.name: texture for texture in description.textures}
    history_read = textures[bindings["_HistoryTex"]]
    history_write = textures[commit.commands[0].destination_resource]

    assert command.shader_name == "Temporal Anti-Aliasing"
    assert bindings["_MotionTex"] == "_result/opaque/motion"
    assert bindings["_DepthTex"] == "depth"
    assert history_read.temporal_key == history_write.temporal_key
    assert history_read.role.name == "TEMPORAL_READ"
    assert history_write.role.name == "TEMPORAL_WRITE"
    assert commit.side_effect is True
    assert dict(command.push_constants) == {
        "feedback": pytest.approx(0.92),
        "motionRejection": pytest.approx(0.1),
        "depthRejection": pytest.approx(128.0),
        "_InfernuxHistoryValid": pytest.approx(0.0),
    }
    assert stack.effect_compile_errors == ()


def test_render_stack_rebuilds_when_effect_topology_parameter_changes():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"max_iterations": 2},
        )
    )
    stack = RenderStack()
    stack.add_effect_slot("final", RenderEffectRef(effect=effect))
    stack._graph_desc = stack.build_graph()

    class Context:
        graph_instance_id = 23

    assert stack._collect_effect_parameter_updates(Context())[0] is False
    effect.set_int("max_iterations", 3)
    assert stack._collect_effect_parameter_updates(Context()) == (True, [])


def test_effect_group_expands_in_order_with_non_destructive_overrides(tmp_path):
    bloom_path = tmp_path / "Bloom.effect"
    tone_path = tmp_path / "Tone.effect"
    bloom_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.bloom",
                parameters={"intensity": 0.5, "max_iterations": 2},
            )
        ),
        encoding="utf-8",
    )
    tone_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.tonemapping",
                parameters={"exposure": 1.0},
            )
        ),
        encoding="utf-8",
    )
    group_path = tmp_path / "Post.effectgroup"
    group_path.write_text(
        dump_render_effect_document(
            RenderEffectGroupAsset(
                entries=(
                    RenderEffectGroupEntry(
                        "bloom",
                        EffectAssetReference(path_hint=bloom_path.name),
                        overrides={"intensity": 0.9},
                    ),
                    RenderEffectGroupEntry(
                        "tone",
                        EffectAssetReference(path_hint=tone_path.name),
                    ),
                )
            )
        ),
        encoding="utf-8",
    )

    effects = expand_render_effect_reference(RenderEffectRef(path_hint=str(group_path)))

    assert [effect.feature_type for effect in effects] == [
        "infernux.post.bloom",
        "infernux.post.tonemapping",
    ]
    assert effects[0].get_float("intensity") == pytest.approx(0.9)


def test_effect_group_inline_parameter_edit_updates_group_and_live_projection(tmp_path):
    from Infernux.engine.ui.render_effect_inspector import (
        apply_render_effect_parameter_edit,
    )

    RenderEffectArtifactRegistry.clear()
    bloom_path = tmp_path / "Bloom.effect"
    bloom_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.bloom",
                parameters={"intensity": 0.5, "max_iterations": 2},
            )
        ),
        encoding="utf-8",
    )
    group_path = tmp_path / "Post.effectgroup"
    group_document = RenderEffectGroupAsset(
        entries=(
            RenderEffectGroupEntry(
                "bloom",
                EffectAssetReference(path_hint=bloom_path.name),
            ),
        )
    )
    group_path.write_text(
        dump_render_effect_document(group_document),
        encoding="utf-8",
    )
    effect = expand_render_effect_reference(
        RenderEffectRef(path_hint=str(group_path))
    )[0]
    initial_revision = effect.revision

    class GroupController:
        def __init__(self, resource):
            self.resource = resource

        def capture_document(self):
            return self.resource.serialize_document()

        def apply_document(self, document, **_kwargs):
            assert self.resource.deserialize_document(document)
            publish_live_effect_group_document(
                str(group_path), self.resource.to_asset()
            )
            return True

    effect.bind_group_document_controller(GroupController(effect.group_resource))

    assert apply_render_effect_parameter_edit(effect, "intensity", 1.75)
    assert effect.get_float("intensity") == pytest.approx(1.75)
    assert effect.revision > initial_revision
    assert effect.group_resource.entries[0].overrides["intensity"] == pytest.approx(1.75)


def test_effect_group_parameter_publication_preserves_projection_identity(tmp_path):
    RenderEffectArtifactRegistry.clear()
    bloom_path = tmp_path / "Bloom.effect"
    bloom_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.bloom",
                parameters={"intensity": 0.5, "max_iterations": 2},
            )
        ),
        encoding="utf-8",
    )
    group_path = tmp_path / "Post.effectgroup"
    original = RenderEffectGroupAsset(
        entries=(
            RenderEffectGroupEntry(
                "bloom",
                EffectAssetReference(path_hint=bloom_path.name),
                overrides={"intensity": 0.75},
            ),
        )
    )
    group_path.write_text(
        dump_render_effect_document(original),
        encoding="utf-8",
    )
    reference = RenderEffectRef(path_hint=str(group_path))
    effect = expand_render_effect_reference(reference)[0]
    old_revision = effect.revision

    edited = RenderEffectGroupAsset(
        entries=(
            RenderEffectGroupEntry(
                "bloom",
                EffectAssetReference(path_hint=bloom_path.name),
                overrides={"intensity": 1.25},
            ),
        )
    )
    publish_live_effect_group_document(str(group_path), edited)

    assert expand_render_effect_reference(reference)[0] is effect
    assert effect.get_float("intensity") == pytest.approx(1.25)
    assert effect.revision > old_revision


def test_effect_group_parameter_publication_reaches_compiled_render_stack(tmp_path):
    RenderEffectArtifactRegistry.clear()
    bloom_path = tmp_path / "Bloom.effect"
    bloom_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.bloom",
                parameters={"intensity": 0.5, "max_iterations": 2},
            )
        ),
        encoding="utf-8",
    )
    group_path = tmp_path / "World.effectgroup"
    original = RenderEffectGroupAsset(
        entries=(
            RenderEffectGroupEntry(
                "bloom",
                EffectAssetReference(path_hint=bloom_path.name),
                overrides={"intensity": 0.75},
            ),
        )
    )
    group_path.write_text(
        dump_render_effect_document(original),
        encoding="utf-8",
    )

    stack = RenderStack()
    stack.add_effect_slot(
        "final",
        RenderEffectRef(path_hint=str(group_path)),
    )
    stack._graph_desc = stack.build_graph()

    class Context:
        graph_instance_id = 41

    requires_rebuild, initial = stack._collect_effect_parameter_updates(Context())
    assert requires_rebuild is False
    assert any(
        dict(update.values).get("intensity") == pytest.approx(0.75)
        for update in initial
    )
    assert stack._collect_effect_parameter_updates(Context()) == (False, [])

    edited = RenderEffectGroupAsset(
        entries=(
            RenderEffectGroupEntry(
                "bloom",
                EffectAssetReference(path_hint=bloom_path.name),
                overrides={"intensity": 1.5},
            ),
        )
    )
    publish_live_effect_group_document(str(group_path), edited)
    requires_rebuild, updates = stack._collect_effect_parameter_updates(Context())

    assert requires_rebuild is False
    assert any(
        dict(update.values).get("intensity") == pytest.approx(1.5)
        for update in updates
    )


def test_pixelation_group_parameter_publication_reaches_compiled_render_stack(tmp_path):
    RenderEffectArtifactRegistry.clear()
    pixelation_path = tmp_path / "Pixelation.effect"
    pixelation_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.route.pixelation",
                parameters={"intensity": 0.0, "pixel_size": 4},
            )
        ),
        encoding="utf-8",
    )
    group_path = tmp_path / "World.effectgroup"
    group_path.write_text(
        dump_render_effect_document(
            RenderEffectGroupAsset(
                entries=(
                    RenderEffectGroupEntry(
                        "pixel",
                        EffectAssetReference(path_hint=pixelation_path.name),
                        overrides={"intensity": 0.0, "pixel_size": 4},
                    ),
                )
            )
        ),
        encoding="utf-8",
    )

    stack = RenderStack()
    stack.add_effect_slot("final", RenderEffectRef(path_hint=str(group_path)))
    stack._graph_desc = stack.build_graph()

    class Context:
        graph_instance_id = 42

    requires_rebuild, initial = stack._collect_effect_parameter_updates(Context())
    assert requires_rebuild is False
    assert any(
        dict(update.values).get("intensity") == pytest.approx(0.0)
        and dict(update.values).get("pixelSize") == pytest.approx(4.0)
        for update in initial
    )
    assert stack._collect_effect_parameter_updates(Context()) == (False, [])

    edited = RenderEffectGroupAsset(
        entries=(
            RenderEffectGroupEntry(
                "pixel",
                EffectAssetReference(path_hint=pixelation_path.name),
                overrides={"intensity": 0.65, "pixel_size": 24},
            ),
        )
    )
    publish_live_effect_group_document(str(group_path), edited)
    requires_rebuild, updates = stack._collect_effect_parameter_updates(Context())

    assert requires_rebuild is False
    assert any(
        dict(update.values).get("intensity") == pytest.approx(0.65)
        and dict(update.values).get("pixelSize") == pytest.approx(24.0)
        for update in updates
    )


def test_effect_group_expansion_is_published_in_memory_until_asset_reimport(
    tmp_path,
    monkeypatch,
):
    RenderEffectArtifactRegistry.clear()
    bloom_path = tmp_path / "Bloom.effect"
    bloom_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.bloom",
                parameters={"intensity": 0.5, "max_iterations": 2},
            )
        ),
        encoding="utf-8",
    )
    group_path = tmp_path / "Post.effectgroup"

    def write_group(intensity):
        group_path.write_text(
            dump_render_effect_document(
                RenderEffectGroupAsset(
                    entries=(
                        RenderEffectGroupEntry(
                            "bloom",
                            EffectAssetReference(path_hint=bloom_path.name),
                            overrides={"intensity": intensity},
                        ),
                    )
                )
            ),
            encoding="utf-8",
        )

    write_group(0.75)
    original_read_text = Path.read_text
    group_reads = []

    def count_group_reads(path, *args, **kwargs):
        if path.resolve() == group_path.resolve():
            group_reads.append(str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", count_group_reads)

    first = expand_render_effect_reference(RenderEffectRef(path_hint=str(group_path)))
    second = expand_render_effect_reference(RenderEffectRef(path_hint=str(group_path)))

    assert first[0] is second[0]
    assert first[0].get_float("intensity") == pytest.approx(0.75)
    assert len(group_reads) == 1

    write_group(1.25)
    RenderEffectArtifactRegistry.compile_and_publish(str(group_path))
    refreshed = expand_render_effect_reference(RenderEffectRef(path_hint=str(group_path)))

    assert refreshed[0].get_float("intensity") == pytest.approx(1.25)
    assert len(group_reads) == 2


def test_effect_group_compiles_without_reentering_its_own_asset_load(tmp_path, monkeypatch):
    from Infernux.engine import project_context

    RenderEffectArtifactRegistry.clear()
    monkeypatch.setattr(project_context, "_project_root", str(tmp_path))
    rendering_dir = tmp_path / "Assets" / "Rendering"
    rendering_dir.mkdir(parents=True)
    bloom_path = rendering_dir / "Bloom.effect"
    bloom_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.bloom",
                parameters={"intensity": 0.5, "max_iterations": 2},
            )
        ),
        encoding="utf-8",
    )
    group_path = rendering_dir / "Post.effectgroup"
    group_path.write_text(
        dump_render_effect_document(
            RenderEffectGroupAsset(
                entries=(
                    RenderEffectGroupEntry(
                        "bloom",
                        EffectAssetReference(path_hint="Assets/Rendering/Bloom.effect"),
                    ),
                )
            )
        ),
        encoding="utf-8",
    )

    artifact, document = RenderEffectArtifactRegistry.compile_and_publish(str(group_path))

    assert isinstance(document, RenderEffectGroupAsset)
    assert artifact.kind == "group"
    assert [feature["feature_type"] for feature in artifact.features] == [
        "infernux.post.bloom"
    ]


def test_effect_group_override_view_tracks_unoverridden_live_parameters():
    from Infernux.renderstack.render_effect_compiler import _apply_group_overrides

    source = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5, "threshold": 1.0, "max_iterations": 2},
        )
    )
    view = _apply_group_overrides([source], {"intensity": 0.9}, "bloom")[0]

    source.set_float("threshold", 2.5)
    parameters = view.to_asset().parameters

    assert parameters["intensity"] == pytest.approx(0.9)
    assert parameters["threshold"] == pytest.approx(2.5)
    assert view.revision == source.revision


def test_effect_group_cycle_is_rejected(tmp_path):
    first = tmp_path / "First.effectgroup"
    second = tmp_path / "Second.effectgroup"
    first.write_text(
        dump_render_effect_document(
            RenderEffectGroupAsset(
                entries=(
                    RenderEffectGroupEntry(
                        "second",
                        EffectAssetReference(path_hint=second.name),
                    ),
                )
            )
        ),
        encoding="utf-8",
    )
    second.write_text(
        dump_render_effect_document(
            RenderEffectGroupAsset(
                entries=(
                    RenderEffectGroupEntry(
                        "first",
                        EffectAssetReference(path_hint=first.name),
                    ),
                )
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(RenderEffectCompileError, match="cycle"):
        expand_render_effect_reference(RenderEffectRef(path_hint=str(first)))


def test_failed_effect_compile_rolls_back_partial_graph_mutation():
    from Infernux.renderstack.fullscreen_effect import FullScreenEffect
    from Infernux.renderstack.render_effect_compiler import register_render_effect_feature

    class BrokenEffect(FullScreenEffect):
        name = "Broken Test Effect"
        injection_point = "after_post_process"
        default_order = 1

        def setup_passes(self, graph, bus):
            graph.create_texture("partial")
            with graph.add_pass("Partial") as render_pass:
                render_pass.write_color("partial")
                render_pass.fullscreen_quad("fullscreen_blit")
            raise ValueError("intentional compile failure")

    register_render_effect_feature("tests.post.broken", BrokenEffect)
    stack = RenderStack()
    stack.add_effect_slot(
        "final",
        RenderEffectRef(effect=RenderEffect(RenderEffectAsset("tests.post.broken"))),
    )

    description = stack.build_graph()

    assert not any("Partial" in render_pass.name for render_pass in description.passes)
    assert any("intentional compile failure" in error for error in stack.effect_compile_errors)
