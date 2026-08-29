# Multiplatform Player fixture

This minimal project is the managed compile, package, launch, graphics-surface,
input-bridge, and lifecycle fixture for Android and Web Player jobs. It keeps a
camera and a light in a real serialized scene while avoiding external assets.
The bootstrap component deliberately enters the normal Python compile/catalog
path even though the scene does not need to attach it.

The interactive Balance project remains the functional acceptance project for
physics, animation, particles, LineRenderer, authored materials, and shared
Action input. This fixture does not replace those device tests.
