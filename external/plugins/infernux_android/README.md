# Infernux Android Platform

This official InxPackage adds Android Player targets to Infernux. It owns the
Android toolchain checks, build plan, host templates, packaging, package audit,
and emulator/device smoke workflow. The resulting Player uses Vulkan; OpenGL
and OpenGL ES are not fallback backends.

The initial targets are:

- `android-x64-emulator` for fast local validation with an AOSP emulator.
- `android-arm64` for release packages and physical devices.

Install **Android compatibility** from the Infernux Hub before importing this
plugin. The Hub owns one versioned Platform Kit containing JDK 17, Gradle 8.12,
the pinned Android SDK/NDK and both Android CPython target runtimes. Every
project reuses that installation; builds never download a toolchain and the
large files are not copied into this InxPackage. The plugin keeps the smaller,
version-coupled exporter, doctor and host templates. Advanced source builds may
still set `ANDROID_AVD_HOME`; target discovery and diagnostics use the shared
Infernux build service.

The plugin's `requirements.txt` installs the pinned host-side `pybind11`
headers/CMake package when the plugin is imported. Its download cache is
shared through Hub; Android builds do not install it on demand or assume it
was already present in the engine's source-development environment.

Gradle's reusable data defaults to `Shared/Cache/Gradle` when launched through
Hub, or `<project>/Cache/Gradle` for standalone source launches. Android user
state (including the debug signing key) lives separately in `Shared/State/Android`
or `<project>/State/Android`; clearing caches must not replace signing keys.
Explicit `GRADLE_USER_HOME` and `ANDROID_USER_HOME` settings remain authoritative.
Existing system-wide tool data is not moved or deleted automatically.

The exporter is being brought up in explicit stages. A target can be visible
while its doctor or execution result reports a precise missing prerequisite;
the plugin never reports an incomplete Android package as a successful build.

## Release signing

Release builds produce an AAB by default. Without signing configuration the
exporter keeps the AAB unsigned and reports that state in the build result. For
store publication, set these environment variables before building:

- `INFERNUX_ANDROID_KEYSTORE`: path to the keystore;
- `INFERNUX_ANDROID_KEY_ALIAS`: signing key alias;
- `INFERNUX_ANDROID_KEYSTORE_PASSWORD`: keystore password;
- `INFERNUX_ANDROID_KEY_PASSWORD`: key password; optional when it matches the
  keystore password.

The build profile can provide `android_keystore`, `android_key_alias`,
`android_keystore_password_env`, and `android_key_password_env`. Passwords are
read from the named environment variables and are never written into the
generated Gradle project or build manifest.
