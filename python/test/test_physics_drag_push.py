"""Gizmo-dragged colliders must push dynamic bodies with real momentum.

Regression tests for two historical bugs:
1. A collider-only (static) body dragged into a sphere only produced
   positional depenetration — the sphere was squeezed out with zero exit
   velocity and stopped dead (fixed by PhysicsWorld::MoveStaticBodyWithVelocity,
   which temporarily drives dragged statics kinematically).
2. Jolt's MoveKinematic velocity persists after the body reaches its target,
   so a kinematic body glided away from its Transform forever once a drag
   ended (fixed by PhysicsWorld::SettleKinematicMoves).
"""
from __future__ import annotations

import pytest

from Infernux.lib import Physics, SceneManager, Vector3

DT = 1.0 / 60.0


def _step(n: int = 1):
    sm = SceneManager.instance()
    for _ in range(n):
        sm.step(DT)


def _make_ground(scene):
    ground = scene.create_game_object("Ground")
    ground.transform.position = Vector3(0, -0.5, 0)
    ground.transform.local_scale = Vector3(100, 1, 100)
    ground.add_component("BoxCollider")
    return ground


def _make_sphere(scene, pos):
    sphere = scene.create_game_object("Sphere")
    sphere.transform.position = pos
    rb = sphere.add_component("Rigidbody")
    col = sphere.add_component("SphereCollider")
    col.radius = 0.5
    return sphere, rb


def _make_cube(scene, pos, *, kinematic: bool):
    cube = scene.create_game_object("Cube")
    cube.transform.position = pos
    cube.add_component("BoxCollider")
    rb = None
    if kinematic:
        rb = cube.add_component("Rigidbody")
        rb.is_kinematic = True
    return cube, rb


def _drag_cube(cube, from_x: float, to_x: float, frames: int):
    """Simulate a gizmo drag: write transform.position once per frame."""
    for i in range(frames):
        t = (i + 1) / frames
        x = from_x + (to_x - from_x) * t
        cube.transform.position = Vector3(x, 0.5, 0)
        _step(1)


def test_static_collider_drag_pushes_sphere(scene):
    Physics.set_gravity(Vector3(0, -9.81, 0))
    _make_ground(scene)
    sphere, srb = _make_sphere(scene, Vector3(0, 0.5, 0))
    cube, _ = _make_cube(scene, Vector3(-3, 0.5, 0), kinematic=False)

    sm = SceneManager.instance()
    sm.play()
    sm.pause()

    # Settle: sphere should come to rest and sleep.
    _step(120)
    assert srb.is_sleeping(), "sphere should be asleep after settling"

    # Drag the cube into the sphere at 3 m/s (0.05 m per frame).
    _drag_cube(cube, -3.0, 0.0, frames=60)

    x_after_drag = sphere.transform.position.x
    vel = srb.velocity
    print(f"\n[static] sphere x after drag = {x_after_drag:.3f}, vel = ({vel.x:.3f}, {vel.y:.3f}, {vel.z:.3f})")

    # Let it keep moving.
    _step(120)
    x_final = sphere.transform.position.x
    print(f"[static] sphere x after 2s free run = {x_final:.3f}")

    # Sphere must be displaced past the cube surface...
    assert x_after_drag > 0.9, f"sphere was not pushed out (x={x_after_drag})"
    # ...and should have been knocked away with real velocity (Unity-like).
    assert x_final > x_after_drag + 0.5, (
        f"sphere stopped dead after push: {x_after_drag:.3f} -> {x_final:.3f}"
    )


def test_kinematic_collider_drag_pushes_sphere(scene):
    Physics.set_gravity(Vector3(0, -9.81, 0))
    _make_ground(scene)
    sphere, srb = _make_sphere(scene, Vector3(0, 0.5, 0))
    cube, crb = _make_cube(scene, Vector3(-3, 0.5, 0), kinematic=True)

    sm = SceneManager.instance()
    sm.play()
    sm.pause()

    _step(120)
    assert srb.is_sleeping(), "sphere should be asleep after settling"

    _drag_cube(cube, -3.0, 0.0, frames=60)

    x_after_drag = sphere.transform.position.x
    vel = srb.velocity
    print(f"\n[kinematic] sphere x after drag = {x_after_drag:.3f}, vel = ({vel.x:.3f}, {vel.y:.3f}, {vel.z:.3f})")

    _step(120)
    x_final = sphere.transform.position.x
    cube_pos = cube.transform.position
    body_pos = crb.position
    print(f"[kinematic] sphere x after 2s = {x_final:.3f}")
    print(f"[kinematic] cube transform = ({cube_pos.x:.3f}, {cube_pos.y:.3f}, {cube_pos.z:.3f})")
    print(f"[kinematic] cube BODY pos  = ({body_pos.x:.3f}, {body_pos.y:.3f}, {body_pos.z:.3f})")

    assert x_after_drag > 0.9, f"sphere was not pushed by kinematic cube (x={x_after_drag})"
    assert x_final > x_after_drag + 0.5, (
        f"sphere stopped dead after kinematic push: {x_after_drag:.3f} -> {x_final:.3f}"
    )


def test_gizmo_style_drag_imparts_cross_velocity(scene):
    """Mimic the editor gizmo exactly: dragging a dynamic-Rigidbody cube.

    The gizmo temporarily flips the dragged Rigidbody to kinematic and calls
    Physics.sync_transforms() after every transform write. Historically
    SyncTransforms used dt = 0, degrading every move into a zero-velocity
    teleport: a sphere crossing the cube's path was pushed aside positionally
    but gained no velocity along the impact axis.
    """
    from Infernux.lib import Physics

    Physics.set_gravity(Vector3(0, -9.81, 0))
    _make_ground(scene)
    sphere = scene.create_game_object("Sphere")
    sphere.transform.position = Vector3(0, 0.5, 0)
    srb = sphere.add_component("Rigidbody")
    scol = sphere.add_component("SphereCollider")
    scol.radius = 0.5

    cube = scene.create_game_object("Cube")
    cube.transform.position = Vector3(1.5, 0.5, -2.0)
    cube.add_component("BoxCollider")
    crb = cube.add_component("Rigidbody")

    sm = SceneManager.instance()
    sm.play()
    sm.pause()
    _step(30)

    # Sphere is rolling along +X; cube is dragged along +Z across its path.
    srb.velocity = Vector3(2.0, 0.0, 0.0)

    # Gizmo drag start: flip the dynamic rigidbody to kinematic.
    crb.is_kinematic = True
    crb.wake_up()

    steps = 70
    z0, z1 = -2.0, 2.2
    for i in range(steps):
        t = (i + 1) / steps
        cube.transform.position = Vector3(1.5, 0.5, z0 + (z1 - z0) * t)
        Physics.sync_transforms()  # the gizmo does this every drag frame
        _step(1)

    # Gizmo drag end: restore dynamics on the dragged body.
    crb.is_kinematic = False
    crb.velocity = Vector3(0, 0, 0)
    crb.angular_velocity = Vector3(0, 0, 0)
    crb.wake_up()

    v = srb.velocity
    print(f"\n[gizmo-style] sphere vel after impact = ({v.x:.2f}, {v.y:.2f}, {v.z:.2f})")
    assert v.z > 1.0, f"cross impact imparted no Z velocity (vz={v.z:.3f})"

    z_impact = sphere.transform.position.z
    _step(90)
    z_final = sphere.transform.position.z
    print(f"[gizmo-style] sphere z {z_impact:.2f} -> {z_final:.2f} after 1.5s")
    assert z_final > z_impact + 1.0, (
        f"sphere did not keep moving along Z: {z_impact:.2f} -> {z_final:.2f}"
    )


def test_kinematic_body_does_not_drift_after_drag(scene):
    """After a gizmo drag ends, the kinematic body must stop at the target."""
    Physics.set_gravity(Vector3(0, -9.81, 0))
    _make_ground(scene)
    cube, crb = _make_cube(scene, Vector3(-3, 0.5, 0), kinematic=True)

    sm = SceneManager.instance()
    sm.play()
    sm.pause()
    _step(30)

    _drag_cube(cube, -3.0, 0.0, frames=60)

    # Stop dragging; run 2 seconds.
    _step(120)
    tf = cube.transform.position
    body = crb.position
    print(f"\n[drift] cube transform = ({tf.x:.3f}, {tf.y:.3f}, {tf.z:.3f})")
    print(f"[drift] cube body pos  = ({body.x:.3f}, {body.y:.3f}, {body.z:.3f})")

    assert abs(body.x - 0.0) < 0.1, f"kinematic body drifted after drag ended: x={body.x:.3f}"
    assert abs(tf.x - 0.0) < 0.1, f"cube transform drifted after drag ended: x={tf.x:.3f}"
