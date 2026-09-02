# Multiplatform Player fixture

This minimal project is the managed compile, package, launch, graphics-surface,
input-bridge, physics, LineRenderer, Screen UI, and lifecycle fixture for all
Player jobs. It keeps a camera, light, and authored material in a real
serialized scene while avoiding external assets. The bootstrap component uses
the standard gameplay ActionMap to apply force to a rendered Rigidbody and
records its world-space motion with a LineRenderer.

The interactive Balance project remains the functional acceptance project for
physics, animation, particles, LineRenderer, authored materials, and shared
Action input. This fixture does not replace those device tests.
