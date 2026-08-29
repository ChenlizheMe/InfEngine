<p align="center">
  <img src="docs/assets/logo.png" alt="Infernux logo" width="128" />
</p>

<h1 align="center">熔炉 · Infernux</h1>

<p align="center">
  <strong>C++ / Vulkan 跑游戏，Python 写玩法和编辑器。</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/version-0.4.0--dev-orange.svg" alt="Version 0.4.0 development" />
  <img src="https://img.shields.io/badge/status-active_development-yellow.svg" alt="持续开发" />
  <img src="https://img.shields.io/badge/current_platform-Windows-lightgrey.svg" alt="当前平台：Windows" />
  <img src="https://img.shields.io/badge/python-3.13-brightgreen.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/graphics-Vulkan-red.svg" alt="Vulkan" />
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

运行时是 C++17 和 Vulkan。玩法、组件、编辑器、资源和渲染编排用 Python 3.13 编写。

当前开发树面向 **0.4.0**。Windows x64 已稳定，Android 与 Web Player 正在用同一个项目验收；Linux Player 和 PyTorch 栈尚未完成。

## 现在能干什么

- 场景、组件、物理、音频、UI、动画、粒子、Prefab
- Vulkan Forward / Forward+ / Deferred、PBR、RenderGraph、RenderStack
- Hub、安装器、Windows 独立游戏打包
- 插件，用法接近 Unity Package Manager

MCP 不再焊在引擎里。它是官方默认插件 `infernux/mcp`。新项目会带上，不想用就关掉或卸掉。

0.3.7 允许独立动画 FBX 驱动关节一致的蒙皮模型，能够处理 Assimp 生成的 pivot 辅助节点；骨架不兼容时会明确失败，不再按几何形状猜测关节对应关系。

## 插件

插件就是一个 InxPackage。丢 `.inxpkg`、选本地目录、贴 GitHub 地址，或从官方列表里装。

```
MyPlugin/
  InxPackage.json
  README.md
  Runtime/          # 进游戏
  Editor/           # 只在编辑器里
  InxPluginPages/   # 插件窗口里的额外页
```

`Runtime` 和普通资源会进 Player。编辑器脚本留在编辑器。

[插件说明](https://infernux-engine.com/wiki/site/zh/plugin-package-content.html)

## 开始用

从 [GitHub Releases](https://github.com/ChenlizheMe/Infernux/releases/latest) 下 Windows x64 安装器，让 InfernuxHub 管理引擎版本。

全新安装的 Hub 会直接带上隔离的 Python 3.13 运行环境。每个 Infernux 版本都严格
绑定到 wheel 声明的 Python ABI；Hub 只有在对应的托管运行环境已经安装后，才允许
安装该引擎版本。旧版本所需的其它运行环境可以在 Hub 的“安装”页面中按需安装。

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

Linux 下请改为运行 `bash scripts/setup/configure_development.sh`。初始化脚本会补齐
子模块，并按照 `environment.yml` 创建项目统一使用的 Python 3.13 Conda 环境；如果已有
`infernux` 环境使用不同的 Python ABI，脚本会重建它，不会勉强沿用旧环境。

```bash
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
