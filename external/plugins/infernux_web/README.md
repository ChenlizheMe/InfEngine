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

## Project Web template

Projects can own the exported page by creating
`ProjectSettings/WebTemplate/shell.html`. Start from the package's
`Editor/infernux_web/templates/host/shell.html`; the project copy may change
the page structure, CSS, metadata, and surrounding website integration while
keeping the Infernux canvas and runtime markers intact. Additional files in
the same directory are published under `web-template/`, preserving nested
directories.

Once `ProjectSettings/WebTemplate` exists, `shell.html` is required and the
build stops on an incomplete template. Rebuilding replaces the generated
entry point and the complete `web-template/` directory, so project template
sources—not hand-edited build output—remain authoritative. Gameplay UI still
belongs to Infernux Screen UI; the HTML shell is the website host, not a DOM
rendering layer for the game.
