"""Strict TagLayerSettings document and startup-loading integration tests."""

from __future__ import annotations

import json

from Infernux.engine.engine import Engine
from Infernux.engine.ui.tag_layer_settings import PhysicsLayerMatrixPanel, TagLayerSettingsPanel
from Infernux.lib import TagLayerManager


class _LayerManager:
    def __init__(self):
        self.layers = [""] * 32
        self.layers[0] = "Default"
        self.layers[8] = "OldName"
        self.collisions = {}

    def get_all_layers(self):
        return self.layers

    @staticmethod
    def is_builtin_layer(index):
        return index == 0

    def set_layer_name(self, index, name):
        self.layers[index] = name.strip()
        return True

    def get_layers_collide(self, layer_a, layer_b):
        return self.collisions.get(tuple(sorted((layer_a, layer_b))), True)

    def set_layers_collide(self, layer_a, layer_b, value):
        self.collisions[tuple(sorted((layer_a, layer_b)))] = bool(value)
        return True


class _LayerContext:
    semantic_capture_enabled = True

    def __init__(self, replacement=None):
        self.replacement = replacement
        self.semantics = []

    @staticmethod
    def set_next_item_open(*_args):
        pass

    @staticmethod
    def collapsing_header(_label):
        return True

    @staticmethod
    def push_id_str(_value):
        pass

    @staticmethod
    def pop_id():
        pass

    @staticmethod
    def label(_value):
        pass

    @staticmethod
    def same_line(*_args):
        pass

    @staticmethod
    def push_style_color(*_args):
        pass

    @staticmethod
    def pop_style_color(*_args):
        pass

    @staticmethod
    def get_window_width():
        return 400.0

    @staticmethod
    def set_next_item_width(_width):
        pass

    @staticmethod
    def get_content_region_avail_width():
        return 300.0

    def text_input(self, _label, value, _maximum):
        if value == "OldName" and self.replacement is not None:
            return self.replacement
        return value

    @staticmethod
    def spacing():
        pass

    @staticmethod
    def separator():
        pass

    def record_semantic_item(self, kind, label, enabled, semantic_id, **values):
        self.semantics.append((kind, label, enabled, semantic_id, values))


class _MatrixContext(_LayerContext):
    def __init__(self, changed_pair=None):
        super().__init__()
        self.changed_pair = changed_pair

    @staticmethod
    def begin_child(*_args):
        return True

    @staticmethod
    def end_child():
        pass

    def checkbox(self, label, current):
        if self.changed_pair and label == f"##pm_{self.changed_pair[0]}_{self.changed_pair[1]}":
            return not current
        return current


def _semantic_by_id(ctx, semantic_id):
    return next(item for item in ctx.semantics if item[3] == semantic_id)


def test_layer_name_fields_publish_stable_authoritative_semantics(monkeypatch):
    panel = TagLayerSettingsPanel()
    manager = _LayerManager()
    ctx = _LayerContext("  DynamicTest  ")
    saves = []
    monkeypatch.setattr(panel, "_auto_save", lambda _mgr: saves.append(True))

    panel._render_layers_section(ctx, manager)

    assert manager.layers[8] == "DynamicTest"
    assert saves == [True]
    assert _semantic_by_id(ctx, "tag_layer_settings.layer.8.name") == (
        "text_input",
        "Layer 8",
        True,
        "tag_layer_settings.layer.8.name",
        {"string_value": "DynamicTest"},
    )
    assert _semantic_by_id(ctx, "tag_layer_settings.layer.0.name")[2:] == (
        False,
        "tag_layer_settings.layer.0.name",
        {"string_value": "Default"},
    )


def test_layer_semantics_are_skipped_during_normal_render():
    panel = TagLayerSettingsPanel()
    manager = _LayerManager()
    ctx = _LayerContext()
    ctx.semantic_capture_enabled = False

    panel._render_layers_section(ctx, manager)

    assert ctx.semantics == []


def test_collision_matrix_publishes_symmetric_authoritative_semantics(monkeypatch):
    panel = PhysicsLayerMatrixPanel()
    manager = _LayerManager()
    manager.layers[8] = "DynamicTest"
    manager.layers[9] = "GroundTest"
    ctx = _MatrixContext((8, 9))
    monkeypatch.setattr(panel, "_draw_vertical_text", lambda *_args: None)
    monkeypatch.setattr(
        "Infernux.engine.ui.tag_layer_settings._save_mgr_to_project", lambda *_args: None
    )

    panel._render_collision_matrix(ctx, manager)

    assert manager.get_layers_collide(9, 8) is False
    assert _semantic_by_id(ctx, "physics_layer_matrix.collision.8.9") == (
        "checkbox",
        "DynamicTest / GroundTest",
        True,
        "physics_layer_matrix.collision.8.9",
        {"bool_value": False},
    )


def test_invalid_asymmetric_collision_matrix_is_rejected_transactionally():
    manager = TagLayerManager.instance()
    original = manager.serialize()
    document = json.loads(original)
    document["layer_collision_masks"][10] &= ~(1 << 11)
    document["layer_collision_masks"][11] |= 1 << 10

    assert manager.deserialize(json.dumps(document)) is False
    assert manager.serialize() == original


def test_save_atomically_replaces_file_without_temporary_residue(tmp_path):
    manager = TagLayerManager.instance()
    path = tmp_path / "TagLayerSettings.json"
    path.write_text("old content", encoding="utf-8")

    assert manager.save_to_file(str(path)) is True
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {"custom_tags", "layers", "layer_collision_masks"}
    assert len(document["layers"]) == 32
    assert list(tmp_path.glob("TagLayerSettings.json.tmp.*")) == []


def test_engine_common_settings_path_loads_collision_matrix(tmp_path):
    manager = TagLayerManager.instance()
    original = manager.serialize()
    project_settings = tmp_path / "ProjectSettings"
    project_settings.mkdir()
    path = project_settings / "TagLayerSettings.json"

    try:
        manager.set_layers_collide(10, 11, False)
        assert manager.save_to_file(str(path)) is True
        manager.set_layers_collide(10, 11, True)

        Engine._apply_project_settings(str(tmp_path))

        assert manager.get_layers_collide(10, 11) is False
    finally:
        assert manager.deserialize(original) is True
