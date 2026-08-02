"""Shared fixtures for Infernux integration tests.

All tests use the real C++ backend (Infernux.lib). No fake/mock objects.

Session-scoped ``engine`` fixture (autouse) initialises Vulkan + SDL once for
the entire test run — every test executes with the real C++ engine running.
Per-function ``scene`` fixture creates a fresh Scene for each test.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from Infernux.lib import (
    Infernux as NativeEngine,
    LogLevel,
    SceneManager,
    Vector3,
    Physics,
    InputManager,
    lib_dir,
)
from Infernux.resources import resources_path
from Infernux.input import Input


@pytest.fixture(autouse=True)
def _reset_editor_document_registry():
    """Prevent process-wide editor documents from leaking across tests."""
    from Infernux.engine.interaction import DocumentRegistry

    registry = DocumentRegistry()
    try:
        yield registry
    finally:
        registry.clear()
        if DocumentRegistry._instance is registry:
            DocumentRegistry._instance = None


# ── session-scoped engine (Vulkan + SDL, created once for ALL tests) ─────

@pytest.fixture(scope="session", autouse=True)
def engine():
    """Start the real C++ engine with a tiny off-screen window.

    ``autouse=True`` ensures every test in the suite runs with the engine
    initialised — Vulkan renderer, SDL window, physics world, and input
    subsystem are all live.
    """
    project = tempfile.mkdtemp(prefix="infernux_test_")
    os.makedirs(os.path.join(project, "ProjectSettings"), exist_ok=True)

    eng = NativeEngine(lib_dir)
    eng.set_log_level(LogLevel.Warn)
    eng.init_renderer(64, 64, project, resources_path)
    try:
        yield eng
    finally:
        # Full native cleanup. The historical heap corruption here was fixed by
        # (a) SceneManager::Shutdown() destroying all scenes inside Cleanup()
        #     before PhysicsWorld::Shutdown(), and
        # (b) leaking the scene/physics/asset singletons so no engine teardown
        #     ever runs during C++ static destruction.
        # Running cleanup in CI is intentional: it is the regression test for
        # that fix.
        try:
            eng.cleanup()
        finally:
            # The native engine is pointed at this disposable project. Cleaning
            # that directory is sufficient and never touches tracked resources.
            shutil.rmtree(project, ignore_errors=True)


@pytest.fixture()
def scene(engine):
    """Create a disposable Scene and make it active.  Cleaned up after each test."""
    sm = SceneManager.instance()
    sc = sm.create_scene("pytest_scene")
    sm.set_active_scene(sc)
    yield sc
    # Ensure play mode is stopped (no-op if already stopped)
    if sm.is_playing():
        sm.stop()
    # Unload the scene so Jolt physics bodies are destroyed before the next
    # test creates a new scene.  Without this, stale bodies from previous
    # tests remain in the PhysicsWorld and cause access violations when
    # DispatchContactEvents / ForceAllBodiesToCurrentTransform dereference
    # Collider pointers that belong to the old (inactive) scene.
    sm.unload_scene(sc)


# ── per-test C++ rigidbody via scene ─────────────────────────────────────

@pytest.fixture
def cpp_rigidbody(scene):
    """Create a C++ Rigidbody through a real scene GameObject."""
    go = scene.create_game_object("_rb_fixture")
    return go.add_component("Rigidbody")


@pytest.fixture(autouse=True)
def _reset_input_state():
    """Reset Input focus state between every test."""
    Input._game_focused = True
    Input._automation_game_input_depth = 0
    Input._game_viewport_origin = (0.0, 0.0)
    yield
    Input._game_focused = True
    Input._automation_game_input_depth = 0
    Input._game_viewport_origin = (0.0, 0.0)


@pytest.fixture(autouse=True)
def _reset_physics_state(engine):
    """Keep process-wide physics settings isolated between tests."""
    earth_gravity = Vector3(0.0, -9.81, 0.0)
    Physics.set_gravity(earth_gravity)
    yield
    Physics.set_gravity(earth_gravity)
