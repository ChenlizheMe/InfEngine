"""
Tag & Layer Settings Panel — project-wide tag/layer management and physics settings.

Tags/Layers remain a dockable editor panel.
Physics settings are exposed through a separate standalone floating window.
"""

import json

from Infernux.engine.i18n import t
from Infernux.lib import InxGUIContext
from Infernux.physics import settings as _phys_settings
from .editor_panel import EditorPanel
from .panel_registry import editor_panel
from .theme import Theme, ImGuiCol, ImGuiWindowFlags
from .igui import IGUI


def _save_mgr_to_project(mgr, project_path: str):
    """Persist tag/layer settings if a project path is available."""
    if not project_path:
        return
    import os
    settings_dir = os.path.join(project_path, "ProjectSettings")
    os.makedirs(settings_dir, exist_ok=True)
    path = os.path.join(settings_dir, "TagLayerSettings.json")
    mgr.save_to_file(path)


@editor_panel("Tags & Layers", type_id="tag_layer_settings", title_key="panel.tags_layers")
class TagLayerSettingsPanel(EditorPanel):
    """Inspector-style panel for managing project-wide tags and layers."""

    WINDOW_TYPE_ID = "tag_layer_settings"
    WINDOW_DISPLAY_NAME = "Tags & Layers"

    def __init__(self):
        super().__init__(title="Tags & Layers", window_id="tag_layer_settings")
        self._new_tag_name = ""
        self._new_layer_idx = -1
        self._new_layer_name = ""
        self._project_path = ""
        self._show_tags = True
        self._show_layers = True
        self._mgr = None

    def set_project_path(self, path: str):
        """Set the project path for saving settings."""
        self._project_path = path

    def _get_mgr(self):
        if self._mgr is None:
            from Infernux.lib import TagLayerManager
            self._mgr = TagLayerManager.instance()
        return self._mgr

    def _initial_size(self):
        return (400, 600)

    def on_render_content(self, ctx: InxGUIContext):
        mgr = self._get_mgr()
        if mgr is None:
            ctx.label(t("tags.manager_unavailable"))
        else:
            self._render_tags_section(ctx, mgr)
            self._render_layers_section(ctx, mgr)
            self._render_footer(ctx, mgr)

    def _render_tags_section(self, ctx: InxGUIContext, mgr):
        ctx.set_next_item_open(True, Theme.COND_FIRST_USE_EVER)
        if ctx.collapsing_header(t("tags.tags_header")):
            all_tags = list(mgr.get_all_tags())

            for i, tag in enumerate(all_tags):
                is_builtin = mgr.is_builtin_tag(tag)
                ctx.push_id_str(f"tag_{i}")

                if is_builtin:
                    ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
                    ctx.label(f"  {tag}")
                    ctx.same_line(ctx.get_window_width() - 80)
                    ctx.label("(built-in)")
                    ctx.pop_style_color(1)
                else:
                    ctx.label(f"  {tag}")
                    ctx.same_line(ctx.get_window_width() - 30)
                    if IGUI._mini_icon_button(ctx, "##rm", Theme.ICON_IMG_REMOVE, Theme.ICON_REMOVE):
                        self._do_remove_tag(tag)

                ctx.pop_id()

            ctx.separator()
            ctx.label(t("tags.add_tag"))
            ctx.same_line(70)
            ctx.set_next_item_width(ctx.get_content_region_avail_width() - 60)
            self._new_tag_name = ctx.text_input("##new_tag", self._new_tag_name, 128)
            ctx.same_line()
            if IGUI._mini_icon_button(ctx, "##add_tag", Theme.ICON_IMG_PLUS, Theme.ICON_PLUS):
                self._do_add_tag()
            ctx.spacing()

    def _render_layers_section(self, ctx: InxGUIContext, mgr):
        capture_semantics = bool(getattr(ctx, "semantic_capture_enabled", True))
        ctx.set_next_item_open(True, Theme.COND_FIRST_USE_EVER)
        if ctx.collapsing_header(t("tags.layers_header")):
            all_layers = list(mgr.get_all_layers())

            for i in range(32):
                name = all_layers[i] if i < len(all_layers) else ""
                is_builtin = mgr.is_builtin_layer(i)
                ctx.push_id_str(f"layer_{i}")

                ctx.label(f"{i:2d}:")
                ctx.same_line(36)

                if is_builtin:
                    ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
                    ctx.label(name if name else "---")
                    ctx.same_line(ctx.get_window_width() - 80)
                    ctx.label(t("tags.built_in"))
                    ctx.pop_style_color(1)
                    if capture_semantics:
                        ctx.record_semantic_item(
                            "status",
                            f"Layer {i}",
                            False,
                            f"tag_layer_settings.layer.{i}.name",
                            string_value=name,
                        )
                else:
                    ctx.set_next_item_width(ctx.get_content_region_avail_width() - 10)
                    new_name = ctx.text_input("##layer_name", name, 64)
                    if new_name != name:
                        mgr.set_layer_name(i, new_name)
                        self._auto_save(mgr)
                        updated_layers = list(mgr.get_all_layers())
                        name = updated_layers[i] if i < len(updated_layers) else ""
                    if capture_semantics:
                        ctx.record_semantic_item(
                            "text_input",
                            f"Layer {i}",
                            True,
                            f"tag_layer_settings.layer.{i}.name",
                            string_value=name,
                        )

                ctx.pop_id()

            ctx.spacing()

    def _render_footer(self, ctx: InxGUIContext, mgr):
        capture_semantics = bool(getattr(ctx, "semantic_capture_enabled", True))
        ctx.separator()
        ctx.button(t("tags.save_settings"), lambda: self._save(mgr))
        if capture_semantics:
            ctx.record_semantic_item(
                "button", t("tags.save_settings"), True, "tag_layer_settings.save"
            )
        ctx.same_line()

        def _reset():
            mgr.deserialize('{"custom_tags":[], "layers":[]}')
            self._auto_save(mgr)

        ctx.button(t("tags.reset_defaults"), _reset)
        if capture_semantics:
            ctx.record_semantic_item(
                "button", t("tags.reset_defaults"), True, "tag_layer_settings.reset"
            )

    def _do_remove_tag(self, tag: str):
        mgr = self._get_mgr()
        if mgr:
            mgr.remove_tag(tag)
            self._auto_save(mgr)

    def _do_add_tag(self):
        mgr = self._get_mgr()
        name = self._new_tag_name.strip()
        if mgr and name and mgr.get_tag_index(name) < 0:
            mgr.add_tag(name)
            self._new_tag_name = ""
            self._auto_save(mgr)

    def _auto_save(self, mgr):
        _save_mgr_to_project(mgr, self._project_path)

    def _save(self, mgr):
        self._auto_save(mgr)


class PhysicsLayerMatrixPanel:
    """Standalone floating panel for project-wide physics settings and collision matrix."""

    _WIN_FLAGS = Theme.WINDOW_FLAGS_DIALOG

    def __init__(self):
        self._visible = False
        self._first_open = True
        self._request_focus = False
        self._project_path = ""
        self._mgr = None
        self._gravity = list(_phys_settings.DEFAULT_PHYSICS_SETTINGS["gravity"])
        self._fixed_delta_time = float(_phys_settings.DEFAULT_PHYSICS_SETTINGS["fixed_delta_time"])
        self._max_fixed_delta_time = float(_phys_settings.DEFAULT_PHYSICS_SETTINGS["max_fixed_delta_time"])
        self._physics_settings = dict(_phys_settings.DEFAULT_PHYSICS_SETTINGS)

    def set_project_path(self, path: str):
        self._project_path = path
        self._reload_project_settings()

    @property
    def is_open(self) -> bool:
        return self._visible

    def open(self):
        self._visible = True
        self._request_focus = False
        self._reload_project_settings()

    def close(self):
        self._visible = False

    def _get_mgr(self):
        if self._mgr is None:
            from Infernux.lib import TagLayerManager
            self._mgr = TagLayerManager.instance()
        return self._mgr

    def _reload_project_settings(self):
        settings = _phys_settings.load(self._project_path)
        self._physics_settings = settings
        self._gravity = list(settings["gravity"])
        self._fixed_delta_time = float(settings["fixed_delta_time"])
        self._max_fixed_delta_time = float(settings["max_fixed_delta_time"])
        _phys_settings.apply(settings)

    def _save_project_settings(self):
        settings = dict(self._physics_settings)
        settings.update(
            gravity=[float(self._gravity[0]), float(self._gravity[1]), float(self._gravity[2])],
            fixed_delta_time=float(self._fixed_delta_time),
            max_fixed_delta_time=float(self._max_fixed_delta_time),
        )
        self._physics_settings = settings
        _phys_settings.apply(settings)
        _phys_settings.save(self._project_path, settings)

    @staticmethod
    def _draw_vertical_text(ctx: InxGUIContext, child_id: str, text: str, width: float, height: float):
        child_visible = ctx.begin_child(child_id, width, height, False)
        if child_visible:
            min_x = ctx.get_window_pos_x()
            min_y = ctx.get_window_pos_y()
            ctx.draw_text_aligned(
                min_x,
                min_y,
                min_x + width,
                min_y + height,
                text,
                *Theme.TEXT_DIM,
                0.5,
                0.5,
                0.0,
                True,
            )
        ctx.end_child()

    def render(self, ctx: InxGUIContext):
        if not self._visible:
            return

        x0, y0, dw, dh = ctx.get_main_viewport_bounds()
        cx = x0 + (dw - 980) * 0.5
        cy = y0 + (dh - 720) * 0.5
        ctx.set_next_window_pos(cx, cy, Theme.COND_ALWAYS, 0.0, 0.0)
        ctx.set_next_window_size(980, 720, Theme.COND_ALWAYS)
        visible, still_open = ctx.begin_window_closable(
            t("physics.title") + "###physics_settings", self._visible, self._WIN_FLAGS
        )

        if not still_open:
            self._visible = False
            ctx.end_window()
            return

        if visible:
            mgr = self._get_mgr()
            if mgr is None:
                ctx.label(t("tags.manager_unavailable"))
            else:
                self._render_body(ctx, mgr)

        ctx.end_window()

    def _render_body(self, ctx: InxGUIContext, mgr):
        self._render_settings_section(ctx)
        ctx.separator()
        ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
        ctx.label(t("physics.collision_matrix_hint"))
        ctx.pop_style_color(1)
        ctx.spacing()

        self._render_collision_matrix(ctx, mgr)

    def _render_collision_matrix(self, ctx: InxGUIContext, mgr):
        capture_semantics = bool(getattr(ctx, "semantic_capture_enabled", True))

        all_layers = list(mgr.get_all_layers())
        visible_layers = []
        for i in range(32):
            name = all_layers[i] if i < len(all_layers) else ""
            if mgr.is_builtin_layer(i) or name:
                visible_layers.append((i, name if name else f"Layer {i}"))

        if not visible_layers:
            ctx.label(t("physics.no_layers"))
            return

        name_col_w = 180.0
        cell_w = 32.0
        header_h = 24.0

        if ctx.begin_child("##physics_matrix_scroll", 0, 0, True):
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
            ctx.begin_child("##physics_matrix_spacer", name_col_w, header_h, False)
            ctx.end_child()
            for col_idx, (layer_idx, _) in enumerate(visible_layers):
                ctx.same_line(name_col_w + col_idx * cell_w)
                self._draw_vertical_text(
                    ctx,
                    f"##physics_header_{layer_idx}",
                    f"{layer_idx:02d}",
                    cell_w,
                    header_h,
                )
            ctx.pop_style_color(1)
            ctx.separator()

            for row_idx, (layer_a, name_a) in enumerate(visible_layers):
                ctx.push_id_str(f"physics_matrix_row_{layer_a}")
                ctx.begin_child(f"##physics_matrix_label_{layer_a}", name_col_w, 24, False)
                ctx.label(f"{layer_a:2d} {name_a}")
                if capture_semantics:
                    ctx.record_semantic_item(
                        "status",
                        f"Layer {layer_a}",
                        False,
                        f"physics_layer_matrix.layer.{layer_a}.name",
                        string_value=name_a,
                    )
                ctx.end_child()

                for col_idx in range(len(visible_layers)):
                    layer_b, _ = visible_layers[col_idx]
                    ctx.same_line(name_col_w + col_idx * cell_w)
                    if col_idx < row_idx:
                        ctx.label(Theme.ICON_DOT)
                        continue
                    current = mgr.get_layers_collide(layer_a, layer_b)
                    new_value = ctx.checkbox(f"##pm_{layer_a}_{layer_b}", current)
                    if new_value != current:
                        mgr.set_layers_collide(layer_a, layer_b, new_value)
                        _save_mgr_to_project(mgr, self._project_path)
                        new_value = mgr.get_layers_collide(layer_a, layer_b)
                    if capture_semantics:
                        low, high = sorted((layer_a, layer_b))
                        ctx.record_semantic_item(
                            "checkbox",
                            f"{name_a} / {visible_layers[col_idx][1]}",
                            True,
                            f"physics_layer_matrix.collision.{low}.{high}",
                            bool_value=new_value,
                        )
                ctx.pop_id()
        ctx.end_child()

    def _render_settings_section(self, ctx: InxGUIContext):
        ctx.label(t("physics.simulation"))

        hz = 1.0 / max(self._fixed_delta_time, 0.001)
        new_hz = ctx.drag_float(t("physics.iteration_rate"), hz, 0.5, 1.0, 1000.0)
        if abs(new_hz - hz) > 1e-6:
            self._fixed_delta_time = max(0.001, 1.0 / max(new_hz, 1.0))
            self._max_fixed_delta_time = max(self._max_fixed_delta_time, self._fixed_delta_time)
            self._save_project_settings()

        new_fixed_dt = ctx.input_float(t("physics.fixed_time_step"), self._fixed_delta_time, 0.001, 0.01, 0)
        if abs(new_fixed_dt - self._fixed_delta_time) > 1e-6:
            self._fixed_delta_time = max(0.001, float(new_fixed_dt))
            self._max_fixed_delta_time = max(self._max_fixed_delta_time, self._fixed_delta_time)
            self._save_project_settings()

        new_max_dt = ctx.input_float(t("physics.max_catchup_delta"), self._max_fixed_delta_time, 0.01, 0.05, 0)
        if abs(new_max_dt - self._max_fixed_delta_time) > 1e-6:
            self._max_fixed_delta_time = max(self._fixed_delta_time, float(new_max_dt))
            self._save_project_settings()

        ctx.spacing()
        ctx.label(t("physics.gravity"))
        gx = ctx.input_float("Gravity X", float(self._gravity[0]), 0.1, 1.0, 0)
        gy = ctx.input_float("Gravity Y", float(self._gravity[1]), 0.1, 1.0, 0)
        gz = ctx.input_float("Gravity Z", float(self._gravity[2]), 0.1, 1.0, 0)
        if abs(gx - self._gravity[0]) > 1e-6 or abs(gy - self._gravity[1]) > 1e-6 or abs(gz - self._gravity[2]) > 1e-6:
            self._gravity = [float(gx), float(gy), float(gz)]
            self._save_project_settings()

        ctx.spacing()
        ctx.set_next_item_open(False, Theme.COND_FIRST_USE_EVER)
        if ctx.collapsing_header(t("physics.advanced")):
            ctx.push_style_color(ImGuiCol.Text, *Theme.WARNING_TEXT)
            ctx.label(t("physics.restart_required"))
            ctx.pop_style_color(1)

            self._render_int_setting(ctx, "collision_steps", "physics.collision_steps", 1, 16)
            self._render_int_setting(ctx, "velocity_steps", "physics.velocity_steps", 2, 64)
            self._render_int_setting(ctx, "position_steps", "physics.position_steps", 1, 64)

            self._render_float_setting(
                ctx, "penetration_slop", "physics.penetration_slop", 0.0, 1.0, 0.001
            )
            self._render_float_setting(
                ctx,
                "speculative_contact_distance",
                "physics.speculative_contact_distance",
                0.0,
                1.0,
                0.001,
            )
            self._render_float_setting(
                ctx,
                "linear_cast_max_penetration",
                "physics.linear_cast_max_penetration",
                0.0,
                1.0,
                0.01,
            )
            self._render_float_setting(ctx, "baumgarte", "physics.baumgarte", 0.0, 1.0, 0.01)
            self._render_float_setting(
                ctx,
                "max_penetration_distance",
                "physics.max_penetration_distance",
                0.0,
                1.0,
                0.01,
            )
            self._render_float_setting(
                ctx, "linear_cast_threshold", "physics.linear_cast_threshold", 0.0, 1.0, 0.01
            )
            self._render_float_setting(
                ctx,
                "min_velocity_for_restitution",
                "physics.min_velocity_for_restitution",
                0.001,
                1000.0,
                0.1,
            )
            self._render_float_setting(
                ctx, "time_before_sleep", "physics.time_before_sleep", 0.0, 60.0, 0.1
            )
            self._render_float_setting(
                ctx,
                "point_velocity_sleep_threshold",
                "physics.point_velocity_sleep_threshold",
                0.001,
                100.0,
                0.005,
            )

    def _render_int_setting(
        self, ctx: InxGUIContext, field: str, label_key: str, minimum: int, maximum: int
    ):
        current = int(self._physics_settings[field])
        value = ctx.input_int(f"{t(label_key)}##{field}", current, 1, 4)
        value = max(minimum, min(maximum, int(value)))
        if value != current:
            self._physics_settings[field] = value
            self._save_project_settings()

    def _render_float_setting(
        self,
        ctx: InxGUIContext,
        field: str,
        label_key: str,
        minimum: float,
        maximum: float,
        step: float,
    ):
        current = float(self._physics_settings[field])
        value = ctx.input_float(f"{t(label_key)}##{field}", current, step, step * 10.0, 0)
        value = max(minimum, min(maximum, float(value)))
        if abs(value - current) > 1e-9:
            self._physics_settings[field] = value
            self._save_project_settings()

