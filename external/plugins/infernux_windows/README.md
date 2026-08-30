# Infernux Windows Platform

This official InxPackage provides the `windows-x64` Player build target. It is
installed automatically from the Windows wheel and uses the wheel's native
Player runtime, Vulkan backend, Python 3.13 runtime pack, and shared build
service.

The package owns Windows target discovery and registration. Removing or
disabling it removes the Windows build target without changing the platform
independent build service.
