<!-- language:en -->

<span class="mini-tag">Gameplay Foundations · Chapter 5</span>

# Physics and collision callbacks

Physics becomes predictable when shape, motion, and reaction each have one clear owner. A `Collider` supplies the shape, a `Rigidbody` supplies simulated motion, `fixed_update` supplies fixed-step gameplay input, and collision or Trigger callbacks report changes in contact state.

In this chapter you will build a rolling probe that lands on a floor, moves through a Trigger volume, and reports every callback phase in the Console.

<div class="learn-article-toc"><strong>In this chapter</strong><a href="#physics-parts">Collider and Rigidbody roles</a><a href="#build-physics-scene">Build the test scene</a><a href="#physics-probe-script">Write the component</a><a href="#collision-trigger-phases">Read the callbacks</a><a href="#verify-physics">Verify the result</a><a href="#physics-errors">Common errors</a></div>

<figure class="learn-figure">
  <img src="../assets/learn/gameplay-physics-events.webp" alt="Infernux physics test scene with a moving body, floor collider, and trigger volume" loading="lazy" decoding="async">
  <figcaption>A dynamic body first makes solid contact with the floor, then crosses a non-solid Trigger volume.</figcaption>
</figure>

## Collider and Rigidbody roles {#physics-parts}

A GameObject can participate in this exercise in three ways:

| Object | Components | Result |
| --- | --- | --- |
| Floor | `BoxCollider` | A stationary solid shape |
| Probe | `SphereCollider`, `Rigidbody`, `PhysicsProbe` | A gravity-driven body that receives forces and callbacks |
| Sensor | `BoxCollider` with **Is Trigger** enabled | An overlap volume with no solid contact response |

`Collider` is the shared base for concrete shapes such as `BoxCollider` and `SphereCollider`. Its public `center` property offsets the shape in local space, and `is_trigger` selects solid contact or Trigger behavior. Shape-specific properties live on each concrete collider; for example, `BoxCollider.size` controls its full local-space size.

`Rigidbody` adds mass, gravity, drag, constraints, velocity, forces, and kinematic movement. A dynamic body has `is_kinematic` disabled. A kinematic body is script-driven and can use `move_position(...)` or `move_rotation(...)` during fixed steps.

<div class="learn-note"><strong>Use the physics clock.</strong><p><code>fixed_update(self, fixed_delta_time)</code> runs on the fixed physics step, whose default is 50 Hz. Apply continuous forces and kinematic movement there. Frame-rate presentation and ordinary per-frame input polling can remain in <code>update</code>.</p></div>

## Build the test scene {#build-physics-scene}

Start from a scene with a Camera that can see the origin, then create these objects:

1. Create a **Cube** named `Ground`. Set Position to `(0, -0.5, 0)` and Scale to `(12, 1, 4)`. The primitive already has a `BoxCollider`; leave its **Is Trigger** disabled and do not add a Rigidbody.
2. Create a **Sphere** named `Probe`. Set Position to `(-4, 2, 0)`. The primitive already has a `SphereCollider`; use **Add Component** to add only `Rigidbody`. Keep **Use Gravity** enabled and **Is Kinematic** disabled. Enable the Rigidbody's **Freeze Position Z** and all three **Freeze Rotation** controls so the example stays on one line.
3. Create another **Cube** named `Sensor`. Set Position to `(2, 0.75, 0)` and Scale to `(1, 1.5, 4)`. On its existing `BoxCollider`, enable **Is Trigger**. You may disable its MeshRenderer after placing it so it behaves as an invisible gameplay volume.
4. Save the scene before entering Play mode.

The floor and sensor do not need a Rigidbody for this setup. The moving `Probe` supplies the active Rigidbody for both interactions.

## Write the component {#physics-probe-script}

Create `Assets/Scripts/physics_probe.py` with the following complete component:

```python
from Infernux.components import InxComponent
from Infernux.components.builtin import Rigidbody
from Infernux.debug import Debug
from Infernux.math import Vector3


class PhysicsProbe(InxComponent):
    def awake(self):
        self._body = None
        self._collision_stay_steps = 0
        self._trigger_stay_steps = 0

    def start(self):
        self._body = self.game_object.get_component(Rigidbody)
        if self._body is None:
            Debug.log_error("PhysicsProbe requires a Rigidbody.", self.game_object)

    def fixed_update(self, fixed_delta_time):
        if self._body is None:
            return

        # add_force defaults to ForceMode.Force.
        self._body.add_force(Vector3(3.0, 0.0, 0.0))

    def on_collision_enter(self, collision):
        self._collision_stay_steps = 0
        Debug.log(
            f"collision enter: {collision.game_object.name} "
            f"at {collision.contact_point}"
        )

    def on_collision_stay(self, collision):
        self._collision_stay_steps += 1
        if self._collision_stay_steps == 1:
            Debug.log(
                f"collision stay: {collision.game_object.name}; "
                f"normal={collision.contact_normal}; "
                f"relative velocity={collision.relative_velocity}"
            )

    def on_collision_exit(self, collision):
        Debug.log(f"collision exit: {collision.game_object.name}")

    def on_trigger_enter(self, other):
        self._trigger_stay_steps = 0
        Debug.log(f"trigger enter: {other.game_object.name}")

    def on_trigger_stay(self, other):
        self._trigger_stay_steps += 1
        if self._trigger_stay_steps == 1:
            Debug.log(f"trigger stay: {other.game_object.name}")

    def on_trigger_exit(self, other):
        Debug.log(f"trigger exit: {other.game_object.name}")
```

Return to the editor, select `Probe`, choose **Add Component**, and attach `PhysicsProbe`. The component stores its Rigidbody reference in `start`; Infernux components manage their own initialization, so gameplay scripts use `awake` or `start` instead of defining `__init__`.

The `fixed_delta_time` parameter is available when a calculation needs the duration of one fixed step. `add_force` already applies a force through the physics solver, so this example does not multiply the force vector by `fixed_delta_time`.

## Read the callbacks {#collision-trigger-phases}

Solid contacts receive a `CollisionInfo` value:

| Callback | When it runs | Available data used above |
| --- | --- | --- |
| `on_collision_enter(collision)` | Contact begins | `game_object`, `collider`, `contact_point`, `contact_normal`, `relative_velocity` |
| `on_collision_stay(collision)` | Each fixed step while contact remains | The same `CollisionInfo` fields |
| `on_collision_exit(collision)` | Contact ends | The other collider and GameObject remain identifiable |

Trigger overlaps receive the other `Collider` directly:

| Callback | When it runs |
| --- | --- |
| `on_trigger_enter(other)` | The other collider enters the Trigger |
| `on_trigger_stay(other)` | Each fixed step while the overlap remains |
| `on_trigger_exit(other)` | The other collider leaves the Trigger |

The two parameter types are intentionally different. Contact points and normals belong to `CollisionInfo`; Trigger callbacks use `other.game_object` when they need the overlapping object. The current public `CollisionInfo` has no `impulse` property.

`Stay` callbacks can run many times. The example counts them and logs only the first stay step for each interaction, keeping the Console readable while still proving that the phase occurred.

## Verify the result {#verify-physics}

1. Clear the Console and enter Play mode.
2. Watch `Probe` fall onto `Ground`, then accelerate along positive X.
3. Confirm the Console reports `collision enter: Ground`, one `collision stay: Ground`, then `trigger enter: Sensor`, one `trigger stay: Sensor`, and `trigger exit: Sensor`.
4. Stop Play mode and confirm the authored Transform values return.

The exact timing depends on the starting position and Rigidbody settings. The phase order is the important result: Enter precedes Stay, and Exit appears only after separation. The Probe can remain in contact with the long floor while its Trigger overlap begins and ends.

For a second check, disable **Is Trigger** on `Sensor` and play again. The Probe should receive collision callbacks for `Sensor` and be physically blocked by it. Restore **Is Trigger** when the comparison is complete.

## Common errors {#physics-errors}

**The Probe falls through the floor.** Confirm that both objects have concrete colliders, the collider shapes cover the visible meshes, and the involved layers are allowed to interact in the Physics Layer Matrix.

**No callback appears.** Confirm that `PhysicsProbe` is enabled on `Probe`, Play mode is running, and `Probe` has an enabled Rigidbody. Also check that the objects actually overlap; selecting a collider shows its shape gizmo.

**Only Trigger callbacks appear.** Inspect **Is Trigger** on every collider. A Trigger overlap follows the `on_trigger_*` path and does not create solid contact.

**The Console floods with Stay messages.** `on_collision_stay` and `on_trigger_stay` run on fixed steps while the relationship remains active. Count, throttle, or use the phase to maintain state instead of logging every call.

**Motion changes with rendering frame rate.** Apply continuous force in `fixed_update`. For a kinematic Rigidbody, call `move_position` or `move_rotation` from the same fixed-step callback.

**`collision.impulse` raises an attribute error.** The current `CollisionInfo` API exposes `collider`, `game_object`, `contact_point`, `contact_normal`, and `relative_velocity`. Use only those documented fields.

## Next chapter

The Probe now turns low-level physics transitions into clean gameplay events. In [UI actions and scene flow](gameplay-ui-scenes.html), you will expose a public component method to a Button and use it to request a safe runtime scene change.

<!-- language:zh -->

<span class="mini-tag">玩法基础 · 第 5 章</span>

# 物理与碰撞回调

把形状、运动和响应分开管理后，物理逻辑会更容易检查。`Collider` 提供形状，`Rigidbody` 提供模拟运动，`fixed_update` 接收固定步玩法输入，碰撞与 Trigger 回调负责报告接触状态变化。

本章会搭建一个物理探针：它先落到地面，再穿过 Trigger 区域，并把每个回调阶段写入 Console。

<div class="learn-article-toc"><strong>本章内容</strong><a href="#physics-parts_1">Collider 与 Rigidbody</a><a href="#build-physics-scene_1">搭建测试场景</a><a href="#physics-probe-script_1">编写组件</a><a href="#collision-trigger-phases_1">理解回调阶段</a><a href="#verify-physics_1">验证结果</a><a href="#physics-errors_1">常见错误</a></div>

<figure class="learn-figure">
  <img src="../assets/learn/gameplay-physics-events.webp" alt="Infernux 物理测试场景，包含运动刚体、地面 Collider 与 Trigger 区域" loading="lazy" decoding="async">
  <figcaption>动态刚体先与地面发生实体接触，随后穿过不产生阻挡的 Trigger 区域。</figcaption>
</figure>

## Collider 与 Rigidbody 各自负责什么 {#physics-parts_1}

本练习包含三类物体：

| 物体 | 组件 | 结果 |
| --- | --- | --- |
| Floor | `BoxCollider` | 固定的实体形状 |
| Probe | `SphereCollider`、`Rigidbody`、`PhysicsProbe` | 受重力与力影响，并接收回调 |
| Sensor | 启用 **Is Trigger** 的 `BoxCollider` | 可穿过的重叠区域 |

`Collider` 是 `BoxCollider`、`SphereCollider` 等具体形状的公共基类。`center` 会在局部空间偏移碰撞形状，`is_trigger` 用于选择实体接触或 Trigger 行为。具体形状还有自己的属性，例如 `BoxCollider.size` 表示局部空间中的完整尺寸。

`Rigidbody` 提供质量、重力、阻力、约束、速度、力和运动接口。关闭 `is_kinematic` 后，刚体由物理模拟驱动。启用运动学模式后，可以在固定步中调用 `move_position(...)` 或 `move_rotation(...)`。

<div class="learn-note"><strong>使用物理时钟。</strong><p><code>fixed_update(self, fixed_delta_time)</code> 按固定物理步运行，默认频率为 50 Hz。连续施力与运动学移动适合放在这里。逐帧显示逻辑和普通输入轮询可以留在 <code>update</code>。</p></div>

## 搭建测试场景 {#build-physics-scene_1}

先准备一个能看见原点的 Camera，再创建以下物体：

1. 创建 **Cube**，命名为 `Ground`。把 Position 设为 `(0, -0.5, 0)`，Scale 设为 `(12, 1, 4)`。该基础几何体已经带有 `BoxCollider`；保持 **Is Trigger** 关闭，不添加 Rigidbody。
2. 创建 **Sphere**，命名为 `Probe`。把 Position 设为 `(-4, 2, 0)`。该基础几何体已经带有 `SphereCollider`；通过 **Add Component** 只添加 `Rigidbody`。保持 **Use Gravity** 开启、**Is Kinematic** 关闭。开启 Rigidbody 的 **Freeze Position Z** 和三个 **Freeze Rotation**，让运动稳定在一条直线上。
3. 再创建一个 **Cube**，命名为 `Sensor`。把 Position 设为 `(2, 0.75, 0)`，Scale 设为 `(1, 1.5, 4)`。在已有的 `BoxCollider` 上开启 **Is Trigger**。定位完成后可以关闭它的 MeshRenderer，把它作为不可见的玩法区域。
4. 进入 Play 模式前保存场景。

这个场景中的地面与 Sensor 无需 Rigidbody。移动的 `Probe` 为两次交互提供活动刚体。

## 编写物理组件 {#physics-probe-script_1}

创建 `Assets/Scripts/physics_probe.py`，写入以下完整组件：

```python
from Infernux.components import InxComponent
from Infernux.components.builtin import Rigidbody
from Infernux.debug import Debug
from Infernux.math import Vector3


class PhysicsProbe(InxComponent):
    def awake(self):
        self._body = None
        self._collision_stay_steps = 0
        self._trigger_stay_steps = 0

    def start(self):
        self._body = self.game_object.get_component(Rigidbody)
        if self._body is None:
            Debug.log_error("PhysicsProbe requires a Rigidbody.", self.game_object)

    def fixed_update(self, fixed_delta_time):
        if self._body is None:
            return

        # add_force 默认使用 ForceMode.Force。
        self._body.add_force(Vector3(3.0, 0.0, 0.0))

    def on_collision_enter(self, collision):
        self._collision_stay_steps = 0
        Debug.log(
            f"collision enter: {collision.game_object.name} "
            f"at {collision.contact_point}"
        )

    def on_collision_stay(self, collision):
        self._collision_stay_steps += 1
        if self._collision_stay_steps == 1:
            Debug.log(
                f"collision stay: {collision.game_object.name}; "
                f"normal={collision.contact_normal}; "
                f"relative velocity={collision.relative_velocity}"
            )

    def on_collision_exit(self, collision):
        Debug.log(f"collision exit: {collision.game_object.name}")

    def on_trigger_enter(self, other):
        self._trigger_stay_steps = 0
        Debug.log(f"trigger enter: {other.game_object.name}")

    def on_trigger_stay(self, other):
        self._trigger_stay_steps += 1
        if self._trigger_stay_steps == 1:
            Debug.log(f"trigger stay: {other.game_object.name}")

    def on_trigger_exit(self, other):
        Debug.log(f"trigger exit: {other.game_object.name}")
```

回到编辑器，选中 `Probe`，通过 **Add Component** 挂载 `PhysicsProbe`。组件在 `start` 中保存 Rigidbody 引用。Infernux 会管理组件初始化，因此玩法组件应使用 `awake` 或 `start`，不要定义 `__init__`。

需要固定步时长时，可以读取 `fixed_delta_time`。`add_force` 会把力交给物理解算器，本例无需再用 `fixed_delta_time` 乘以力向量。

## 理解六个回调阶段 {#collision-trigger-phases_1}

实体接触会收到 `CollisionInfo`：

| 回调 | 调用时机 | 本例使用的数据 |
| --- | --- | --- |
| `on_collision_enter(collision)` | 接触开始 | `game_object`、`collider`、`contact_point`、`contact_normal`、`relative_velocity` |
| `on_collision_stay(collision)` | 保持接触期间的每个固定步 | 同一组 `CollisionInfo` 字段 |
| `on_collision_exit(collision)` | 接触结束 | 仍可识别另一 Collider 与 GameObject |

Trigger 重叠会直接收到另一个 `Collider`：

| 回调 | 调用时机 |
| --- | --- |
| `on_trigger_enter(other)` | 另一个 Collider 进入 Trigger |
| `on_trigger_stay(other)` | 保持重叠期间的每个固定步 |
| `on_trigger_exit(other)` | 另一个 Collider 离开 Trigger |

这两组参数类型不同。接触点与法线位于 `CollisionInfo`；Trigger 回调可以通过 `other.game_object` 取得重叠物体。当前公开的 `CollisionInfo` 没有 `impulse` 属性。

`Stay` 回调可能连续运行很多次。本例只记录每次交互的第一个 Stay 固定步，既能验证阶段，又能保持 Console 清晰。

## 验证结果 {#verify-physics_1}

1. 清空 Console，进入 Play 模式。
2. 观察 `Probe` 落到 `Ground`，随后沿 X 正方向加速。
3. 确认 Console 依次出现 `collision enter: Ground`、一条 `collision stay: Ground`、`trigger enter: Sensor`、一条 `trigger stay: Sensor` 和 `trigger exit: Sensor`。
4. 停止 Play 模式，确认编辑状态下的 Transform 数值恢复。

具体时间会随初始位置和 Rigidbody 设置变化。验证重点是阶段顺序：Enter 先出现，保持关系后出现 Stay，分离后才出现 Exit。Probe 可以持续接触长地面，同时完成一次 Trigger 进入与离开。

还可以关闭 `Sensor` 的 **Is Trigger** 再运行一次。此时 Probe 会收到针对 `Sensor` 的碰撞回调，并被它挡住。完成对照后恢复 **Is Trigger**。

## 常见错误 {#physics-errors_1}

**Probe 穿过地面。** 确认两个物体都有具体 Collider，碰撞形状覆盖可见 Mesh，并检查 Physics Layer Matrix 是否允许相关 Layer 交互。

**没有任何回调。** 确认 `PhysicsProbe` 已启用并挂在 `Probe` 上，当前处于 Play 模式，`Probe` 具有启用的 Rigidbody。还要检查物体是否真的相交；选中 Collider 可以查看它的形状 Gizmo。

**只出现 Trigger 回调。** 检查每个 Collider 的 **Is Trigger**。Trigger 重叠只走 `on_trigger_*` 路径，也不会产生实体阻挡。

**Console 被 Stay 消息占满。** `on_collision_stay` 与 `on_trigger_stay` 会在关系保持期间按固定步运行。可以计数、限频，或用它维护状态。

**运动随渲染帧率变化。** 把连续施力放进 `fixed_update`。运动学 Rigidbody 可在同一回调中调用 `move_position` 或 `move_rotation`。

**访问 `collision.impulse` 报错。** 当前 `CollisionInfo` 提供 `collider`、`game_object`、`contact_point`、`contact_normal` 与 `relative_velocity`，请使用这些公开字段。

## 下一章

现在，Probe 已经把底层物理变化整理成清晰的玩法事件。下一章 [UI 操作与场景流程](gameplay-ui-scenes.html) 会把组件的公开方法交给 Button，并通过它发起安全的运行时场景切换。
