<p align="center">
  <img src="docs/assets/logo.png" alt="Infernux logo" width="128" />
</p>

<h1 align="center">熔炉 · Infernux</h1>

<p align="center">
  <strong>C++ / Vulkan / WebGPU 跑游戏，Python 写玩法和编辑器。</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/version-0.4.0-orange.svg" alt="Version 0.4.0" />
  <img src="https://img.shields.io/badge/status-active_development-yellow.svg" alt="持续开发" />
  <img src="https://img.shields.io/badge/platforms-Windows_|_Linux_|_Android_|_Web-lightgrey.svg" alt="Windows、Linux、Android 和 Web" />
  <img src="https://img.shields.io/badge/python-3.13-brightgreen.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/graphics-Vulkan_|_WebGPU-red.svg" alt="Vulkan 和 WebGPU" />
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="https://infernux-engine.com/">官网</a> ·
  <a href="https://infernux-engine.com/wiki.html">文档</a> ·
  <a href="https://infernux-engine.discourse.group/">论坛</a> ·
  <a href="https://github.com/ChenlizheMe/Infernux/releases">Release</a>
</p>

<p align="center">
  <img src="docs/assets/demo.png" alt="Infernux 编辑器使用自定义 RenderStack 渲染 65,536 个物体的体素场景" width="100%" />
</p>

上图是 0.3.4 的真实编辑器截图：65536 个普通 GameObject，一份网格和材质，RenderStack 同时做光照、雾、调色、移轴和 MSAA。

## 这是什么

通用游戏引擎。不是在别人编辑器外面套一层聊天框。

运行时使用 C++17，原生平台采用 Vulkan，浏览器采用 WebGPU。玩法、组件、编辑器、资源和渲染编排用 Python 3.13 编写。

**0.4.0** 带来了 Windows 和 Linux 编辑器、Windows/Linux/Android/Web 四端游戏构建、自包含插件载荷、运行时资产封包，以及 Hub 托管的构建环境。四个 Player 目标使用同一个项目验证玩法、UI、输入与包内文件读取。

## 现在能干什么

- 场景、组件、物理、音频、UI、动画、粒子、Prefab
- Vulkan Forward / Forward+ / Deferred、PBR、RenderGraph、RenderStack
- Windows 和 Linux 的 Hub、编辑器与独立游戏构建
- 通过同一构建服务导出 Android APK/AAB 和 Web Player
- InxPackage 插件，区分运行时与编辑器内容，支持资产与脚本实时刷新
- 按路径访问资产，运行时通过 GUID 索引读取封包内容
- Hub 托管 Python 与安卓支持，安装任务在后台排队执行

| 目标 | 编辑器 | Player | 图形后端 | 0.4.0 状态 |
|---|---:|---:|---|---|
| Windows x64 | 有 | 有 | Vulkan | 构建与 CI 通过 |
| Linux x86_64 | 有 | 有 | Vulkan | 构建与 CI 通过 |
| Android arm64/x86_64 | 无 | APK/AAB | Vulkan | 构建与 CI 通过 |
| Web | 无 | HTML/JS/WASM | WebGPU | 构建与 CI 通过 |

此表遵循[可审计的支持矩阵](docs/platform-support.json)。
[证据与发布边界](SUPPORT.md#platform-support)区分 CI 验收、公开发行和设备覆盖范围。
macOS 和原生 iOS 不属于受支持目标。

平台导出器是独立于核心引擎 wheel 的官方 InxPackage。较小的可复用运行时与构建载荷随插件分发，体积较大的共享 Android SDK/NDK 由 Hub Library 管理。安装安卓插件前，需要先在 Hub 安装安卓支持。OpenGL、OpenGL ES 和 WebGL 都不是产品 fallback。

MCP 不再焊在引擎里。它是官方默认插件 `infernux/mcp`。新项目会带上，不想用就关掉或卸掉。

独立动画 FBX 可以驱动关节一致的蒙皮模型，能够处理 Assimp 生成的 pivot 辅助节点；骨架不兼容时会明确失败，不再按几何形状猜测关节对应关系。

## 插件

插件就是一个 InxPackage。丢 `.inxpkg`、选本地目录、贴 GitHub 地址，或从官方列表里装。

```text
MyPluginRepository/
  README.md          # 仅供仓库展示
  package.py         # 独立打包脚本
  package/
    inx_package.json # 可选，覆盖默认元数据
    runtime/         # 进游戏
    editor/          # 只在编辑器里
    plugin_pages/    # 插件窗口里的额外页
```

本地作者选中的文件夹本身就是包根目录，不要求再套 `package/` 或手写 manifest。
未填写元数据时，输出 `.inxpkg` 的文件名决定默认 name 和 reference。
Git 仓库只打包 `package/`，CMake、Gradle、Cargo、README 和临时构建产物都留在外层。

`runtime` 和普通资源会进 Player。编辑器脚本留在编辑器。

插件资产拥有显式 `.meta` 身份，与项目资产参与同一套刷新流程。插件不只可以携带 Python 脚本，也可以携带材质、Shader、文本、网页和原生库等运行时文件。

Player 内容保留在 `Content.inxpkg` 中，不再直接展开 `Assets/` 和 `Library/` 目录树。引擎资产 API 通过构建时生成的 GUID 索引解析原始路径；需要真实文件路径的内容可以按需导出，并保留内部相对目录关系。这是二进制资产封包，不是密码学加密承诺。

[插件说明](https://infernux-engine.com/wiki/site/zh/plugin-package-content.html)

## 开始用

从 [GitHub Releases](https://github.com/ChenlizheMe/Infernux/releases/latest) 下载已发布的 Windows x64 安装器，让 InfernuxHub 管理引擎版本。

全新安装的 Hub 会直接带上隔离的 Python 3.13 运行环境。每个 Infernux 版本都严格
绑定到 wheel 声明的 Python ABI；Hub 只有在对应的托管运行环境已经安装后，才允许
安装该引擎版本。旧版本所需的其它运行环境可以在 Hub 的“安装”页面中按需安装。

Hub 在“安装”入口内分别提供引擎版本、Python 运行环境和安卓支持页面。安装在后台执行，底部用紧凑进度条展示任务，悬停即可展开队列；Hub 也可以保留在系统托盘。使用托管安装不需要先学 Python 或 Conda。

源码构建需要 Windows 10/11 x64、Python 3.13、Vulkan SDK 1.3+、CMake 3.25+、Visual Studio 2022、MSVC v143：

```powershell
git clone --recurse-submodules https://github.com/ChenlizheMe/Infernux.git
cd Infernux
./scripts/setup/configure_development.ps1
conda activate infernux
cmake --preset windows-msvc-release
cmake --build --preset windows-msvc-wheel
python packaging/launcher.py
```

Ubuntu 或 Debian 下先安装原生依赖，再运行初始化脚本。脚本会补齐
子模块，并按照 `environment.yml` 创建项目统一使用的 Python 3.13 Conda 环境；如果已有
`infernux` 环境使用不同的 Python ABI，脚本会重建它，不会勉强沿用旧环境。

```bash
scripts/setup/install_linux_dependencies.sh
bash scripts/setup/configure_development.sh
conda activate infernux
cmake --preset linux-clang-release
cmake --build --preset linux-clang-release
```

两端都可以运行 Python 测试。下面的原生测试示例使用 Windows preset；Linux 下改用 `linux-clang-dev`。

```powershell
python -m pytest python/test/ -v
cmake --preset windows-msvc-dev
cmake --build --preset windows-msvc-dev
ctest --preset windows-msvc-dev --output-on-failure
```

## 文档

- [文档入口](https://infernux-engine.com/wiki.html)
- [API](https://infernux-engine.com/wiki/site/zh/api/index.html)
- [插件](https://infernux-engine.com/wiki/site/zh/plugin-package-content.html)
- [更新日志](UpdateLog-zh.md)
- [路线图](https://infernux-engine.com/roadmap.html)
- [论文](https://arxiv.org/pdf/2604.10263)

## 引用

```bibtex
@software{chen2026infernux,
  author  = {Chen, Lizhe},
  title   = {Infernux},
  year    = {2026},
  version = {0.4.0},
  url     = {https://github.com/ChenlizheMe/Infernux}
}
```

## 许可证

MIT，见 [LICENSE](LICENSE)。报 Bug 请带上引擎版本、系统和复现步骤。大改之前先看 [CONTRIBUTING.md](CONTRIBUTING.md)。
