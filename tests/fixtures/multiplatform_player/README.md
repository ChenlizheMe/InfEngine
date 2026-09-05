# Multiplatform Player fixture

This minimal project is the managed compile, package, launch, graphics-surface,
input-bridge, physics, LineRenderer, Screen UI, and lifecycle fixture for all
Player jobs. It keeps a camera, light, and authored material in a real
serialized scene while avoiding external assets. The bootstrap component uses
the standard gameplay ActionMap to apply force to a rendered Rigidbody and
records its world-space motion with a LineRenderer. It also resolves a verbatim
TXT payload from an installed package through ``Application.package_path()``,
reads it after Player export, and presents the exact value through ``UIText``.
The package preload also reads ``Assets/Data/preload_message.txt`` through
``Application.asset_path()`` before the scene starts. Its ready marker requires
both reads to succeed, exercising the frozen path-to-GUID binding during
plugin startup on every Player target.
Its package JSON references the TXT by relative filename. The preload resolves
this JSON even though it is intentionally absent from the installation ownership
ledger: it represents a file authored in `runtime/` after package installation.
This verifies that fresh AssetIndex membership reaches every Player without
changing the author's uninstall ownership. The preload resolves
both the JSON file and its cataloged package directory, then reads that sibling
to verify that raw resource layout survives the same packaging path.

The interactive Balance project remains the functional acceptance project for
physics, animation, particles, LineRenderer, authored materials, and shared
Action input. This fixture does not replace those device tests.
