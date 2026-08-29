# Infernux Web 平台

这个官方 InxPackage 为 Infernux 提供 `web-wasm32` Player 目标。Player 通过
Emdawnwebgpu 使用 WebGPU，不提供 WebGL 或 OpenGL 回退路径。

插件负责浏览器工具链诊断、Emscripten 集成、CPython 3.13 WebAssembly
运行时、浏览器 Host、网页输入桥、打包和浏览器冒烟测试。核心引擎只维护共享
构建与 RHI 契约，浏览器 SDK 和模板仍由平台插件独立维护。

初始工具链固定为 Emscripten 6.0.8 和 `wasm32-emscripten` CPython 3.13。
不受支持的浏览器能力会在构建前明确报告；尚不完整的 Player runtime 不会被
误报成可用的游戏包。

