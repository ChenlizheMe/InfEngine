"""Scene-view authoring tools for the built-in LineRenderer."""

from __future__ import annotations

import math


def _tuple3(value):
    return (float(value[0]), float(value[1]), float(value[2]))


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(value, scalar):
    return (value[0] * scalar, value[1] * scalar, value[2] * scalar)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(value):
    return math.sqrt(_dot(value, value))


def _normalized(value):
    length = _length(value)
    return _mul(value, 1.0 / length) if length > 1.0e-8 else (0.0, 0.0, 1.0)


class SceneViewLineToolsMixin:
    """Unity-style edit/create/simplify tools for a selected LineRenderer."""

    @staticmethod
    def _line_ray_plane_intersection(origin, direction, point, normal):
        denominator = _dot(direction, normal)
        if abs(denominator) <= 1.0e-7:
            return None
        distance = _dot(_sub(point, origin), normal) / denominator
        if distance < 0.0:
            return None
        return _add(origin, _mul(direction, distance))

    @staticmethod
    def _line_simplify_positions(positions, tolerance):
        points = [_tuple3(point) for point in positions]
        if len(points) <= 2 or tolerance <= 0.0:
            return points

        tolerance_squared = float(tolerance) * float(tolerance)
        keep = [False] * len(points)
        keep[0] = keep[-1] = True

        def distance_squared(point, start, end):
            segment = _sub(end, start)
            length_squared = _dot(segment, segment)
            if length_squared <= 1.0e-12:
                delta = _sub(point, start)
                return _dot(delta, delta)
            amount = max(0.0, min(1.0, _dot(_sub(point, start), segment) / length_squared))
            delta = _sub(point, _add(start, _mul(segment, amount)))
            return _dot(delta, delta)

        def simplify(first, last):
            if last <= first + 1:
                return
            furthest = first
            maximum = -1.0
            for index in range(first + 1, last):
                candidate = distance_squared(points[index], points[first], points[last])
                if candidate > maximum:
                    maximum = candidate
                    furthest = index
            if maximum <= tolerance_squared:
                return
            keep[furthest] = True
            simplify(first, furthest)
            simplify(furthest, last)

        simplify(0, len(points) - 1)
        return [point for index, point in enumerate(points) if keep[index]]

    @staticmethod
    def _line_subdivide_positions(positions, selected, loop=False):
        points = [_tuple3(point) for point in positions]
        selected = {int(index) for index in selected}
        if len(points) < 2 or len(selected) < 2:
            return points
        result = []
        for index, point in enumerate(points):
            result.append(point)
            next_index = index + 1
            if next_index < len(points) and index in selected and next_index in selected:
                result.append(_mul(_add(point, points[next_index]), 0.5))
        if loop and 0 in selected and len(points) - 1 in selected:
            result.append(_mul(_add(points[-1], points[0]), 0.5))
        return result

    def _selected_line_renderer(self):
        try:
            from Infernux.components.builtin import LineRenderer
            from Infernux.engine.interaction import SelectionService
            from Infernux.lib import SceneManager

            object_id = int(SelectionService.instance().primary_scene_object_id() or 0)
            scene = SceneManager.instance().get_active_scene()
            game_object = scene.find_by_id(object_id) if scene and object_id else None
            line = game_object.get_component(LineRenderer) if game_object else None
            return game_object, line
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            return None, None

    @staticmethod
    def _line_world_position(game_object, line, position):
        if bool(line.use_world_space):
            return _tuple3(position)
        return _tuple3(game_object.get_transform().transform_point(position))

    @staticmethod
    def _line_authored_position(game_object, line, world_position):
        from Infernux.lib import Vector3

        value = Vector3(*world_position)
        if bool(line.use_world_space):
            return world_position
        return _tuple3(game_object.get_transform().inverse_transform_point(value))

    def _line_camera_plane_normal(self):
        camera = self._engine.editor_camera if self._engine else None
        if camera is None:
            return (0.0, 0.0, 1.0)
        return _normalized(_sub(_tuple3(camera.focus_point), _tuple3(camera.position)))

    def _line_viewport_ray(self, ctx, vp):
        if not self._engine:
            return None
        local_x, local_y = vp.mouse_local(ctx)
        ray = self._engine.screen_to_world_ray(
            local_x,
            local_y,
            max(1.0, vp.image_max_x - vp.image_min_x),
            max(1.0, vp.image_max_y - vp.image_min_y),
        )
        return _tuple3(ray[:3]), _tuple3(ray[3:])

    def _line_create_world_point(self, ctx, vp, anchor):
        ray = self._line_viewport_ray(ctx, vp)
        if ray is None:
            return None
        origin, direction = ray
        if int(self._line_create_input) == 1:
            try:
                from Infernux.physics import Physics

                hit = Physics.raycast(
                    origin,
                    direction,
                    100000.0,
                    int(self._line_create_layer_mask),
                )
                if hit is None:
                    return None
                return _add(
                    _tuple3(hit.point),
                    _mul(_tuple3(hit.normal), float(self._line_create_offset)),
                )
            except (ReferenceError, RuntimeError, TypeError, ValueError):
                return None
        point = self._line_ray_plane_intersection(
            origin, direction, anchor, self._line_camera_plane_normal()
        )
        if point is None:
            return None
        return _add(point, _mul(self._line_camera_plane_normal(), float(self._line_create_offset)))

    def _draw_line_renderer_scene_tools(self, ctx, vp, cursor_x, cursor_y):
        from Infernux.engine.i18n import t
        from Infernux.engine.ui._inspector_undo import (
            _record_generic_component,
            _record_property,
        )
        from Infernux.engine.ui.theme import Theme

        game_object, line = self._selected_line_renderer()
        if line is None:
            self._line_edit_mode = 0
            self._line_active_component_id = 0
            self._line_selected_points.clear()
            self._line_point_dragging = False
            self._line_create_dragging = False
            self._line_create_before_document = None
            return False

        component_id = int(getattr(line, "component_id", 0) or 0)
        if component_id != self._line_active_component_id:
            self._line_active_component_id = component_id
            self._line_selected_points.clear()
            self._line_point_dragging = False
            self._line_create_dragging = False
            self._line_create_before_document = None

        if self._line_edit_mode == 0:
            panel_height = 82.0
        elif self._line_edit_mode == 1:
            panel_height = 116.0
        else:
            panel_height = 152.0 if int(self._line_create_input) == 1 else 132.0
        ctx.set_cursor_pos_x(cursor_x + 8.0)
        ctx.set_cursor_pos_y(cursor_y + 48.0)
        visible = ctx.begin_child("##line_renderer_scene_tools", 420.0, panel_height, True)
        hovered = bool(ctx.is_window_hovered())
        if visible:
            edit_active = self._line_edit_mode == 1
            create_active = self._line_edit_mode == 2
            if ctx.button(t("line_renderer.edit_points")):
                self._line_edit_mode = 0 if edit_active else 1
            ctx.same_line()
            if ctx.button(t("line_renderer.create_points")):
                self._line_edit_mode = 0 if create_active else 2
            ctx.same_line()
            self._line_show_wireframe = ctx.checkbox(
                t("line_renderer.show_wireframe"), self._line_show_wireframe
            )

            if self._line_edit_mode == 0:
                self._line_simplify_preview = ctx.checkbox(
                    t("line_renderer.simplify_preview"),
                    self._line_simplify_preview,
                )
                ctx.same_line()
                self._line_simplify_tolerance = float(
                    ctx.drag_float(
                        t("line_renderer.tolerance"),
                        self._line_simplify_tolerance,
                        0.01,
                        0.0,
                        100000.0,
                    )
                )
                ctx.same_line()
                if ctx.button(t("line_renderer.simplify")):
                    before = line.serialize_document()
                    line.simplify(self._line_simplify_tolerance)
                    after = line.serialize_document()
                    _record_generic_component(line, before, after)
            elif self._line_edit_mode == 1:
                if ctx.button(t("line_renderer.subdivide_selected")):
                    positions = self._line_subdivide_positions(
                        line.positions,
                        self._line_selected_points,
                        bool(line.loop),
                    )
                    if len(positions) != len(line.positions):
                        _record_property(
                            line,
                            "positions",
                            line.positions,
                            positions,
                            "Subdivide Line Points",
                        )
                        self._line_selected_points.clear()
            else:
                self._line_create_input = ctx.combo(
                    t("line_renderer.input"),
                    self._line_create_input,
                    [
                        t("line_renderer.mouse_position"),
                        t("line_renderer.physics_raycast"),
                    ],
                    -1,
                )
                if int(self._line_create_input) == 1:
                    self._line_create_layer_mask = int(
                        ctx.drag_int(
                            t("line_renderer.layer_mask"),
                            self._line_create_layer_mask,
                            1.0,
                            0,
                            0x7FFFFFFF,
                        )
                    )
                self._line_create_min_distance = float(
                    ctx.drag_float(
                        t("line_renderer.min_vertex_distance"),
                        self._line_create_min_distance,
                        0.01,
                        0.0,
                        100000.0,
                    )
                )
                ctx.same_line()
                self._line_create_offset = float(
                    ctx.drag_float(
                        t("line_renderer.offset"),
                        self._line_create_offset,
                        0.01,
                        -100000.0,
                        100000.0,
                    )
                )
        ctx.end_child()

        positions = list(line.positions)
        draw_positions = positions
        if self._line_edit_mode == 0 and self._line_simplify_preview:
            draw_positions = self._line_simplify_positions(
                positions, self._line_simplify_tolerance
            )
        world_positions = [
            self._line_world_position(game_object, line, position)
            for position in draw_positions
        ]
        camera = self._engine.editor_camera if self._engine else None
        if camera is None:
            return hovered
        screen_positions = []
        for point in world_positions:
            screen = camera.world_to_screen_point(*point)
            screen_positions.append(
                (vp.image_min_x + float(screen[0]), vp.image_min_y + float(screen[1]))
            )
        if self._line_show_wireframe or self._line_edit_mode != 0:
            for first, second in zip(screen_positions, screen_positions[1:]):
                ctx.draw_line(*first, *second, *Theme.ACCENT, 2.0)
            if bool(line.loop) and len(screen_positions) > 2:
                ctx.draw_line(
                    *screen_positions[-1], *screen_positions[0], *Theme.ACCENT, 2.0
                )

        if self._line_edit_mode == 0 or not positions:
            return hovered

        authored_world = [
            self._line_world_position(game_object, line, position)
            for position in positions
        ]
        authored_screen = []
        for point in authored_world:
            screen = camera.world_to_screen_point(*point)
            authored_screen.append(
                (vp.image_min_x + float(screen[0]), vp.image_min_y + float(screen[1]))
            )
        mouse = (float(ctx.get_mouse_pos_x()), float(ctx.get_mouse_pos_y()))
        hit_index = -1
        hit_distance = 9.0 * 9.0
        for index, point in enumerate(authored_screen):
            distance = (mouse[0] - point[0]) ** 2 + (mouse[1] - point[1]) ** 2
            if distance <= hit_distance:
                hit_distance = distance
                hit_index = index
            selected = index in self._line_selected_points
            color = Theme.ACCENT if selected else Theme.TEXT
            ctx.draw_filled_circle(point[0], point[1], 5.0, *color, 16)
            ctx.draw_circle(point[0], point[1], 6.0, *Theme.PANEL_BORDER, 1.0, 16)

        ctrl = self._is_ctrl_down(ctx)
        if self._line_edit_mode == 1 and hit_index >= 0 and ctx.is_mouse_button_clicked(0):
            if ctrl:
                if hit_index in self._line_selected_points:
                    self._line_selected_points.remove(hit_index)
                else:
                    self._line_selected_points.add(hit_index)
            else:
                if hit_index not in self._line_selected_points:
                    self._line_selected_points = {hit_index}
                ray = self._line_viewport_ray(ctx, vp)
                if ray is not None:
                    plane_point = authored_world[hit_index]
                    intersection = self._line_ray_plane_intersection(
                        ray[0], ray[1], plane_point, self._line_camera_plane_normal()
                    )
                    if intersection is not None:
                        self._line_point_dragging = True
                        self._line_drag_plane_point = plane_point
                        self._line_drag_plane_normal = self._line_camera_plane_normal()
                        self._line_drag_start_world = intersection
                        self._line_drag_start_positions = list(positions)
                        self._line_drag_before_document = line.serialize_document()
            hovered = True

        if self._line_point_dragging:
            hovered = True
            if ctx.is_mouse_button_down(0):
                ray = self._line_viewport_ray(ctx, vp)
                intersection = (
                    self._line_ray_plane_intersection(
                        ray[0],
                        ray[1],
                        self._line_drag_plane_point,
                        self._line_drag_plane_normal,
                    )
                    if ray is not None
                    else None
                )
                if intersection is not None:
                    delta = _sub(intersection, self._line_drag_start_world)
                    updated = list(self._line_drag_start_positions)
                    for index in self._line_selected_points:
                        start_world = self._line_world_position(
                            game_object, line, self._line_drag_start_positions[index]
                        )
                        updated[index] = self._line_authored_position(
                            game_object, line, _add(start_world, delta)
                        )
                    line.positions = updated
            else:
                after = line.serialize_document()
                _record_generic_component(
                    line, self._line_drag_before_document, after
                )
                self._line_point_dragging = False

        if (
            self._line_edit_mode == 2
            and not self._line_create_dragging
            and not hovered
            and hit_index < 0
            and ctx.is_mouse_button_clicked(0)
        ):
            anchor = authored_world[-1] if authored_world else _tuple3(camera.focus_point)
            world = self._line_create_world_point(ctx, vp, anchor)
            if world is not None and (
                not authored_world
                or _length(_sub(world, authored_world[-1]))
                >= self._line_create_min_distance
            ):
                self._line_create_before_document = line.serialize_document()
                line.positions = list(positions) + [
                    self._line_authored_position(game_object, line, world)
                ]
                self._line_create_dragging = True
                hovered = True

        if self._line_create_dragging:
            hovered = True
            if ctx.is_mouse_button_down(0):
                current_positions = list(line.positions)
                current_world = [
                    self._line_world_position(game_object, line, position)
                    for position in current_positions
                ]
                anchor = (
                    current_world[-1]
                    if current_world
                    else _tuple3(camera.focus_point)
                )
                world = self._line_create_world_point(ctx, vp, anchor)
                if world is not None and (
                    not current_world
                    or _length(_sub(world, current_world[-1]))
                    >= self._line_create_min_distance
                ):
                    line.positions = current_positions + [
                        self._line_authored_position(game_object, line, world)
                    ]
            else:
                after = line.serialize_document()
                _record_generic_component(
                    line, self._line_create_before_document, after
                )
                self._line_create_dragging = False
                self._line_create_before_document = None

        return hovered or hit_index >= 0


__all__ = ["SceneViewLineToolsMixin"]
