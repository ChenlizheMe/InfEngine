# Infernux Web 平台

这个官方 InxPackage 为 Infernux 提供 `web-wasm32` Player 目标。Player 通过
Emdawnwebgpu 使用 WebGPU，不提供 WebGL 或 OpenGL 回退路径。

插件负责浏览器工具链诊断、Emscripten 集成、CPython 3.13 WebAssembly
运行时、浏览器 Host、网页输入桥、打包和浏览器冒烟测试。核心引擎只维护共享
构建与 RHI 契约，浏览器 SDK 和模板仍由平台插件独立维护。

初始工具链固定为 Emscripten 6.0.8 和 `wasm32-emscripten` CPython 3.13。
不受支持的浏览器能力会在构建前明确报告；尚不完整的 Player runtime 不会被
误报成可用的游戏包。

## 项目 Web 模板

项目可以通过 `ProjectSettings/WebTemplate/shell.html` 管理导出网页。建议从
插件内的 `package/editor/infernux_web/templates/host/shell.html` 复制起步；项目副本
可以修改网页结构、CSS、元数据和外围网站集成，但必须保留 Infernux Canvas
与运行时标记。同目录中的其它文件会保持多级目录结构发布到
`web-template/`。

一旦 `ProjectSettings/WebTemplate` 存在，`shell.html` 就是必需文件；模板不
完整时构建直接停止。重新构建会替换入口 HTML 和整个 `web-template/` 目录，
因此项目模板源码才是权威来源，不应长期手改构建输出。游戏内 UI 仍由
Infernux Screen UI 绘制；HTML 只是网站 Host，不是游戏的 DOM 渲染层。
