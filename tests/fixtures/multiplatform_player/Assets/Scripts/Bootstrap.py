"""Compiled-script sentinel for the multiplatform Player fixture."""

import infernux as inx


class PlatformFixtureBootstrap(inx.InxComponent):
    """Exercise authored input, physics, rendering, LineRenderer, and Screen UI."""

    MOVE_FORCE = 24.0
    TRAIL_MAX_POINTS = 96

    def awake(self):
        self._probe = None
        self._body = None
        self._trail = None
        self._trail_points = []
        self._actions = inx.input.InputActionMap.standard_gameplay()

    def start(self):
        scene = inx.SceneManager.get_active_scene()
        if scene is None:
            raise RuntimeError("Multiplatform fixture requires an active scene")

        self._probe = inx.GameObject.find("Render Probe")
        ground = inx.GameObject.find("Shadow Receiver")
        if self._probe is None or ground is None:
            raise RuntimeError("Multiplatform fixture scene objects are incomplete")
        self._probe.add_component("BoxCollider")
        self._body = self._probe.add_component("Rigidbody")
        ground.add_component("BoxCollider")

        trail_owner = scene.create_game_object("Platform Fixture Trail")
        self._trail = trail_owner.add_component("LineRenderer")
        self._trail.use_world_space = True
        self._trail.start_width = 0.06
        self._trail.end_width = 0.18
        self._trail.start_color = (0.15, 0.95, 1.0, 0.12)
        self._trail.end_color = (1.0, 0.18, 0.65, 0.95)
        self._record_trail_position(force=True)

        canvas_owner = scene.create_game_object("Platform Fixture Canvas")
        canvas = canvas_owner.add_component(inx.ui.UICanvas)
        canvas.reference_width = 1280
        canvas.reference_height = 720

        marker_owner = scene.create_game_object("Platform Fixture UI Marker")
        marker_owner.set_parent(canvas_owner, world_position_stays=False)
        marker = marker_owner.add_component(inx.ui.UIImage)
        marker.x = 32.0
        marker.y = 32.0
        marker.width = 96.0
        marker.height = 24.0
        marker.color = [0.15, 0.95, 0.72, 1.0]
        marker.raycast_target = False

        inx.Debug.log("INFERNUX_PLATFORM_FIXTURE_GAMEPLAY_READY")

    def fixed_update(self, fixed_delta_time: float):
        del fixed_delta_time
        if self._body is None:
            raise RuntimeError("Multiplatform fixture Rigidbody is unavailable")
        horizontal, vertical = self._actions["Move"].read_value()
        if horizontal != 0.0 or vertical != 0.0:
            self._body.add_force(
                inx.Vector3(
                    float(horizontal) * self.MOVE_FORCE,
                    0.0,
                    float(vertical) * self.MOVE_FORCE,
                )
            )

    def update(self, delta_time: float):
        del delta_time
        self._record_trail_position()

    def _record_trail_position(self, *, force: bool = False):
        if self._probe is None or self._trail is None:
            return
        position = self._probe.transform.position
        point = (float(position.x), float(position.y), float(position.z))
        if not force and self._trail_points:
            previous = self._trail_points[-1]
            distance_squared = sum(
                (point[index] - previous[index]) ** 2 for index in range(3)
            )
            if distance_squared < 0.0025:
                return
        self._trail_points.append(point)
        if len(self._trail_points) > self.TRAIL_MAX_POINTS:
            self._trail_points = self._trail_points[-self.TRAIL_MAX_POINTS :]
        visible = self._trail_points
        if len(visible) == 1:
            visible = [visible[0], visible[0]]
        self._trail.set_positions(visible)
