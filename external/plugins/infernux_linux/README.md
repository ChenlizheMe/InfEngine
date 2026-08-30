# Infernux Linux Platform

This official InxPackage provides the `linux-x64` Player build target. It is
installed automatically from the Linux wheel and uses the wheel's native
Player runtime, Vulkan backend, Python 3.13 runtime pack, and shared build
service.

The package owns Linux target discovery and registration. Removing or
disabling it removes the Linux build target without changing the platform
independent build service.
