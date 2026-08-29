# Infernux Web Platform

This official InxPackage adds the `web-wasm32` Player target to Infernux. The
Player uses WebGPU through Emdawnwebgpu. WebGL and OpenGL are not fallback
rendering paths.

The package owns browser toolchain diagnostics, Emscripten integration,
CPython 3.13 WebAssembly staging, browser host files, web input bridges,
packaging, and browser smoke checks. The core engine keeps the shared build and
RHI contracts; browser SDKs and templates remain in this platform package.

The initial toolchain is Emscripten 6.0.8 with CPython 3.13 built for
`wasm32-emscripten`. Unsupported browser capabilities are reported before a
build starts, and an incomplete Player runtime is never published as a
successful game build.

