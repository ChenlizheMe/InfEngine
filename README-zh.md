<p align="center">
  <img src="docs/assets/logo.png" alt="Infernux logo" width="128" />
</p>

<h1 align="center">熔炉 · Infernux</h1>

<p align="center">
  <strong>以 C++/GPU 为运行时、以 Python 为一等开发界面的实验性通用游戏引擎。</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/version-0.3.6-orange.svg" alt="Version 0.3.6" />
  <img src="https://img.shields.io/badge/status-active_development-yellow.svg" alt="持续开发" />
  <img src="https://img.shields.io/badge/current_platform-Windows-lightgrey.svg" alt="当前平台：Windows" />
  <img src="https://img.shields.io/badge/python-3.12+-brightgreen.svg" alt="Python 3.12+" />
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

上图是 0.3.4 技术展示的真实编辑器截图：65,536 个普通 GameObject
共享网格与材质，可编程 RenderStack 同时处理光照、雾、色彩、移轴景深与 MSAA。

## Infernux 是什么

Infernux 的目标是通用游戏引擎，而不是在既有编辑器外面套一层 AI。性能敏感的运行时工作由
C++17 与 Vulkan 承担；玩法、组件、编辑器扩展、内容工作流、自动化和渲染编排通过
pybind11 使用 Python 3.12。

项目的长期判断是：AI 时代的引擎不能只增加一个聊天框。模型与 Agent 需要明确的引擎语义、
世界状态、高吞吐数据、可复现执行和受治理工具。Infernux 当前已经有一些可用于这个方向的基础，
但大多数 AI-native 系统仍属于计划，而不是已经交付的能力。

## 0.3.6

当前公开版本是 Windows-first 开发基线，可以在 Windows x64 上创作和打包游戏，
并覆盖了一套广泛但仍年轻的传统引擎工具。0.3.6 把场景与 UI 对象统一到同一个
Hierarchy，恢复打包版 Hub 的更新发现，并改为发布可独立安装的完整 Hub 包。

| 当前可用 | 0.5.2 周期计划 |
|:---------|:---------------|
| Windows x64 编辑器与 Player 导出 | Android APK/AAB 与 Web 游戏包体 |
| C++17/Vulkan 运行时与 Python 3.12 API | Linux Player 与无窗口/离屏运行 |
| 场景、组件、资产、物理、音频、UI、动画、VFX 与编辑器工作流 | Host 侧 Torch 工作流与各平台 Player 推理 |
| Hub、安装器、wheel、运行时包与独立游戏打包 | 权威反射与 Command/Query schema |
| 编辑器侧 MCP 开发和验证 Harness | snapshot/delta、确定性 replay、批量世界与张量数据面 |
| Numba 可选加速与纯 Python fallback | 带权限、测试和生命周期的正式 Engine Tool |

Android、Web 和 Linux Player 构建在 **0.3.6 中尚不可用**。当前也没有通用 PyTorch
Vulkan backend、移动/Web 包体中的任意 Python 包兼容能力，或已经完成的 VLM/LLM
benchmark 平台。

## 架构边界

| 层级 | 负责内容 |
|:-----|:---------|
| C++17 / Vulkan | 渲染、资源所有权、场景状态、物理、音频与平台服务 |
| pybind11 | 向 Python 暴露带类型的原生句柄与 API |
| Python | 玩法、组件、编辑器逻辑、自动化、资产管线与渲染创作 |

基本原则是：性能敏感状态属于原生运行时，公开生产工作流保持 Python 可编程。
当任意 Python 对象生命周期不够安全时，引擎使用稳定身份、代际检查、文档所有权和显式事务。

当前子系统包括 Vulkan Forward/Forward+/Deferred、PBR、RenderGraph 和可编程
RenderStack；Jolt 物理；基于 GUID 的资产记录与导入制品；2D/3D 动画基础；
Canvas 运行时 UI；GPU Particle Graph；Prefab；热重载；以及共享命令、文档、保存和
撤销回退服务的多面板编辑器。

## 为什么下一阶段不同

0.3.6 到 0.5.2 由三条彼此连接的交付线组成：

1. **把游戏发布到 Windows 之外。** Android 和 Web Player 包体是第一个产品关口；
   Linux 与 Headless 随后服务部署、CI 和仿真。跨平台 Player 可以不带 Numba，并使用
   明确记录的可移植 Python 子集。
2. **接入现有机器学习生态。** Host/Editor 侧应直接使用标准 PyTorch 做开发、训练和导出；
   构建好的 Player 只携带适合目标平台的推理运行时与模型，不永久包含 CUDA Toolkit，
   也不携带完整训练栈。Android/Web provider 必须由真实集成 spike 和测量结果决定。
3. **让世界与工具真正可被机器使用。** 权威 schema、snapshot/replay、批量世界、
   Torch/DLPack/buffer 交换和正式 Engine Tool，应让编辑器 UI、Python、自动化与 Agent
   共用同一份语义，而不是继续积累平行 API。

0.5.2 是本轮建设的收束点，不是 1.0，也不代表 Infernux 已经能够替代 Unity、Unreal
Engine 或 Godot。具体版本关口见[公开路线图](https://infernux-engine.com/roadmap.html)。

## 开始使用

普通用户可以从 [GitHub Releases](https://github.com/ChenlizheMe/Infernux/releases/latest)
下载当前 Windows x64 安装器，由 InfernuxHub 安装和管理引擎版本。

从源码构建需要 Windows 10/11 x64、Python 3.12+、Vulkan SDK 1.3+、CMake 3.22+、
Visual Studio 2022 与 MSVC v143：

```bash
git clone --recurse-submodules https://github.com/ChenlizheMe/Infernux.git
cd Infernux
conda create -n infernux python=3.12 -y
conda activate infernux
pip install -r requirements.txt
cmake --preset release
cmake --build --preset release
```

从源码启动 Hub：

```bash
python packaging/launcher.py
```

在仓库根目录运行 Python 与原生测试：

```bash
python -m pytest python/test/ -v
cmake --preset native-tests
cmake --build --preset native-tests
ctest --preset native-tests --output-on-failure
```

## 文档与仓库维护

- [文档入口](https://infernux-engine.com/wiki.html)
- [API 参考](https://infernux-engine.com/wiki/site/zh/api/index.html)
- [版本记录](UpdateLog-zh.md)
- [贡献指南](CONTRIBUTING.md)
- [仓库脚本索引](scripts/README.md)
- [技术报告](https://arxiv.org/pdf/2604.10263)

仓库级命令统一放在 `scripts/`；官网专用的确定性生成器和契约测试放在 `docs/tools/`。
构建与打包中间产物进入 `out/`，最终本地发布物进入 `dist/releases/<version>/`。

需要显式更新已经提交的 API 文档时运行：

```bat
scripts\docs\update_api_docs.bat
```

## 引用

```bibtex
@software{chen2026infernux,
  author  = {Chen, Lizhe},
  title   = {Infernux},
  year    = {2026},
  version = {0.3.6},
  url     = {https://github.com/ChenlizheMe/Infernux}
}
```

## 参与贡献与许可证

欢迎提交 Bug、功能建议、可复现测试、平台适配和工作流反馈。引擎仍很年轻，Issue 中请附上
准确版本、运行环境、复现步骤和受影响层级。敏感问题或大型修改前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 和 [SUPPORT.md](SUPPORT.md)。

Infernux 使用 [MIT License](LICENSE) 发布。
