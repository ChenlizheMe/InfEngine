"""Compiled-script sentinel for the multiplatform Player fixture."""

import infernux as inx


class PlatformFixtureBootstrap(inx.InxComponent):
    """Exercise authored input, physics, rendering, LineRenderer, and Screen UI."""

    MOVE_FORCE = 24.0
    TRAIL_MAX_POINTS = 96
    TEXT_INPUT_VALUE = "输入测试中文🙂"

    def awake(self):
        self._probe = None
        self._body = None
        self._trail = None
        self._trail_points = []
        self._actions = inx.input.InputActionMap.standard_gameplay()
        self._multitouch_reported = False
        self._unity_touch_reported = False
        self._touch_cancel_reported = False
        self._text_input_requested = False
        self._keyboard_visible_reported = False
        self._keyboard_hidden_reported = False
        self._committed_text = ""
        self._text_input_button = None
        self._text_input_status = None
        self._last_screen_view = None
        self._back_reported = False

    def start(self):
        scene = inx.SceneManager.get_active_scene()
        if scene is None:
            raise RuntimeError("Multiplatform fixture requires an active scene")

        self._probe = inx.GameObject.find("Render Probe")
        ground = inx.GameObject.find("Shadow Receiver")
        if self._probe is None or ground is None:
            raise RuntimeError("Multiplatform fixture scene objects are incomplete")
        ground.transform.local_scale = inx.Vector3(8.0, 0.4, 64.0)
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

        text_button_owner = scene.create_game_object("Platform Fixture Text Input")
        text_button_owner.set_parent(canvas_owner, world_position_stays=False)
        self._text_input_button = text_button_owner.add_component(inx.ui.UIButton)
        self._text_input_button.x = 512.0
        self._text_input_button.y = 20.0
        self._text_input_button.width = 256.0
        self._text_input_button.height = 64.0
        self._text_input_button.label = "Open keyboard"
        self._text_input_button.background_color = [0.95, 0.28, 0.12, 1.0]
        self._text_input_button.on_click.add_listener(self._begin_text_input)

        status_owner = scene.create_game_object("Platform Fixture Text Status")
        status_owner.set_parent(canvas_owner, world_position_stays=False)
        self._text_input_status = status_owner.add_component(inx.ui.UIText)
        self._text_input_status.x = 400.0
        self._text_input_status.y = 96.0
        self._text_input_status.width = 480.0
        self._text_input_status.height = 56.0
        self._text_input_status.text = "Text input idle"
        self._text_input_status.font_size = 24.0
        self._text_input_status.color = [0.92, 0.95, 1.0, 1.0]

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
        self._validate_screen_state()
        self._validate_cancel_action()
        self._validate_touch_input()
        self._validate_text_input()
        self._record_trail_position()

    def _validate_screen_state(self):
        revision = inx.Screen.revision
        width, height = inx.Screen.size
        framebuffer_width, framebuffer_height = inx.Screen.framebuffer_size
        safe = inx.Screen.safe_area
        insets = inx.Screen.safe_insets
        pixel_ratio = inx.Screen.pixel_ratio
        screen_view = (
            width,
            height,
            framebuffer_width,
            framebuffer_height,
            safe.x,
            safe.y,
            safe.width,
            safe.height,
            pixel_ratio,
        )
        if screen_view == self._last_screen_view:
            return
        if width <= 0 or height <= 0:
            raise RuntimeError("Screen logical size must be positive")
        if framebuffer_width <= 0 or framebuffer_height <= 0:
            raise RuntimeError("Screen framebuffer size must be positive")
        if pixel_ratio <= 0.0:
            raise RuntimeError("Screen pixel ratio must be positive")
        if (
            safe.x < 0
            or safe.y < 0
            or safe.width <= 0
            or safe.height <= 0
            or safe.x + safe.width > width
            or safe.y + safe.height > height
        ):
            raise RuntimeError("Screen safe area is outside the logical viewport")

        self._last_screen_view = screen_view
        inx.Debug.log(
            "INFERNUX_PLATFORM_FIXTURE_SCREEN_STATE "
            f"revision={revision} size={width}x{height} "
            f"framebuffer={framebuffer_width}x{framebuffer_height} "
            f"safe={safe.x},{safe.y},{safe.width},{safe.height} "
            f"insets={insets.left},{insets.top},{insets.right},{insets.bottom} "
            f"pixel_ratio={pixel_ratio:.6f}"
        )

    def _validate_cancel_action(self):
        if self._back_reported:
            return
        if self._actions["Cancel"].was_pressed_this_frame:
            self._back_reported = True
            inx.Debug.log("INFERNUX_PLATFORM_FIXTURE_BACK_READY")

    def _begin_text_input(self):
        if self._text_input_requested:
            return
        if not inx.input.Input.begin_text_input():
            raise RuntimeError("Platform text input service rejected the request")
        self._text_input_requested = True
        self._text_input_button.label = "Keyboard active"
        self._text_input_status.text = "Waiting for committed text"
        inx.Debug.log("INFERNUX_PLATFORM_FIXTURE_UI_CLICK_READY")

    def _validate_text_input(self):
        if not self._text_input_requested:
            return

        keyboard_inset = inx.Screen.keyboard_inset
        if (
            keyboard_inset is not None
            and keyboard_inset > 0
            and not self._keyboard_visible_reported
        ):
            self._keyboard_visible_reported = True
            inx.Debug.log(
                "INFERNUX_PLATFORM_FIXTURE_IME_VISIBLE "
                f"inset={keyboard_inset}"
            )

        committed = inx.input.Input.input_string
        if committed:
            self._committed_text += committed
            self._text_input_status.text = self._committed_text
            if self._committed_text == self.TEXT_INPUT_VALUE:
                if not self._keyboard_visible_reported:
                    raise RuntimeError("Text committed before Android IME inset was visible")
                inx.Debug.log(
                    "INFERNUX_PLATFORM_FIXTURE_TEXT_COMMITTED "
                    f"value={self._committed_text}"
                )
                inx.input.Input.end_text_input()
            elif not self.TEXT_INPUT_VALUE.startswith(self._committed_text):
                raise RuntimeError(
                    f"Unexpected committed text: {self._committed_text!r}"
                )

        if (
            self._committed_text == self.TEXT_INPUT_VALUE
            and keyboard_inset == 0
            and not self._keyboard_hidden_reported
        ):
            self._keyboard_hidden_reported = True
            self._text_input_button.label = "Keyboard verified"
            inx.Debug.log("INFERNUX_PLATFORM_FIXTURE_IME_HIDDEN")

    def _validate_touch_input(self):
        touches = inx.input.Input.touches
        if inx.input.Input.touch_count != len(touches):
            raise RuntimeError("Input.touch_count disagrees with Input.touches")

        for index, touch in enumerate(touches):
            indexed = inx.input.Input.get_touch(index)
            if (
                indexed.touch_id != touch.touch_id
                or indexed.finger_id != touch.finger_id
                or indexed.phase is not touch.phase
            ):
                raise RuntimeError("Input.get_touch(index) disagrees with Input.touches")
            if (
                not self._unity_touch_reported
                and touch.phase is inx.input.TouchPhase.MOVED
                and touch.delta_time > 0.0
            ):
                self._unity_touch_reported = True
                inx.Debug.log(
                    "INFERNUX_PLATFORM_FIXTURE_UNITY_TOUCH_READY "
                    f"count={inx.input.Input.touch_count} finger={touch.finger_id} "
                    f"delta_time={touch.delta_time:.6f} pressure={touch.pressure:.3f}"
                )
            if (
                not self._touch_cancel_reported
                and touch.phase is inx.input.TouchPhase.CANCELED
            ):
                self._touch_cancel_reported = True
                inx.Debug.log(
                    "INFERNUX_PLATFORM_FIXTURE_TOUCH_CANCELED "
                    f"finger={touch.finger_id}"
                )

        if len(touches) >= 2 and not self._multitouch_reported:
            self._multitouch_reported = True
            inx.Debug.log(
                "INFERNUX_PLATFORM_FIXTURE_MULTITOUCH_READY "
                f"count={inx.input.Input.touch_count}"
            )

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
