<p align="center">
  <img src="docs/assets/logo.png" alt="Infernux logo" width="128" />
</p>

<h1 align="center">熔炉 · Infernux</h1>

<p align="center">
  <strong>以 C++17 / Vulkan 为原生运行时、以 Python 为生产层的开源游戏引擎。</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/version-0.3.4-orange.svg" alt="Version 0.3.4" />
  <img src="https://img.shields.io/badge/status-active_development-yellow.svg" alt="持续开发" />
  <img src="https://img.shields.io/badge/platform-Windows-lightgrey.svg" alt="Platform" />
  <img src="https://img.shields.io/badge/python-3.12+-brightgreen.svg" alt="Python" />
  <img src="https://img.shields.io/badge/C%2B%2B-17-blue.svg" alt="C++ 17" />
  <img src="https://img.shields.io/badge/graphics-Vulkan-red.svg" alt="Vulkan" />
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="https://infernux-engine.com/">官网</a> ·
  <a href="https://infernux-engine.com/wiki.html">文档</a> ·
  <a href="https://github.com/ChenlizheMe/Infernux/releases">下载</a> ·
  <a href="https://arxiv.org/pdf/2604.10263">技术报告</a>
</p>

<p align="center">
  <img src="docs/assets/demo.png" alt="Infernux 编辑器渲染 10000 个物体的场景" width="100%" />
</p>

## Infernux 是什么

Infernux 是一个从零构建的开源游戏引擎。它使用 C++ 维护渲染、资源、物理、场景状态和平台服务等原生热路径，再通过 pybind11 把稳定的能力暴露给 Python；玩法、编辑器扩展、内容工作流、渲染编排和外部工具都可以直接在 Python 层开发。

这里的 Python 不是附加在封闭编辑器上的脚本语言，而是引擎的一等生产界面。这让常规游戏逻辑更容易编写和检查，也让项目可以原生接入 Python 生态中的 AI、视觉、仿真和数据工具。

Infernux 当前仍以 Windows 为主要开发平台，并处于快速迭代阶段。`0.3.4` 是一次完整的引擎版本更新；不过项目仍很年轻，现阶段会主动接受必要的破坏性数据格式与 API 更新。

## 0.3.4

`0.3.4` 是一次覆盖面很广的引擎更新，并不只是 MCP 或 VFX 更新：场景文档、序列化、资产、渲染、粒子、物理、编辑器交互、运行时执行、自动化和游戏分发都围绕更严格的数据所有权与运行时边界进行了重构。

这一版的主要变化包括：

- 引入带稳定对象/组件身份的 Scene 与 Component 类型化文档，并增加严格校验、脚本缺失恢复和事务式发布/回滚。
- 重构资产索引、依赖追踪与导入产物，改善材质、网格、纹理和物理材质引用的稳定性。
- 加入可由 Python 编排的 RenderStack、可复用 Effect 资产、显式挂载点以及 Forward/Forward+/Deferred 路由，并整理多相机下的 RenderGraph/RHI 所有权。
- 建立 GPU ParticleGraph 管线：类型化节点、发射阶段、Graph 到 IR 的 AOT 编译、Sprite/Mesh 输出、编辑器预览与运行时参数控制。
- 引入结构化 GLSL 写法，明确区分顶点、片元与 Shading Model，同时不要求用户手写 Descriptor Layout。
- 批处理原生 Transform 与 Jolt 物理路径，改善大场景、Play/Stop 恢复和场景重载。
- 建立共享 Interaction Core，统一选择、命令、快捷键、撤销回退、文档所有权、脏状态、保存、另存为、关闭与退出行为。
- 重组游戏导出：原生启动器、私有运行时、压缩的 `Content.inxpkg`，以及随 wheel 分发的 Player Runtime Pack。
- 增加引擎内置画面捕获，可截取整个编辑器、子视图或游戏相机，不依赖桌面截图。

完整变更、兼容性提示和验证记录见 [UpdateLog.md](UpdateLog.md)。

## 架构

| 层级 | 负责内容 |
|:-----|:---------|
| C++17 / Vulkan | 渲染、资源所有权、场景状态、物理、音频、平台服务 |
| pybind11 | 向 Python 暴露带类型的原生句柄与 API |
| Python | 玩法、组件、编辑器逻辑、自动化、内容管线、渲染编排 |

基本原则很直接：性能敏感状态归 C++，日常生产接口保留在 Python。原生句柄使用稳定身份和代际检查；文档与资产变更通过明确的事务边界提交，而不是依赖任意 Python 对象的生命周期。

## 当前系统

| 领域 | 当前范围 |
|:-----|:---------|
| 渲染 | Vulkan Forward/Forward+/Deferred、PBR、逐相机光照与阴影、MSAA、可复用 Effect、RenderGraph、可编程 RenderStack |
| 物理 | Jolt 刚体、基础/网格碰撞体、物理材质、查询、回调、层过滤 |
| 资产 | GUID 身份、依赖索引、导入产物、材质、Prefab、场景、动画与 VFX 资产 |
| 编辑器 | Hierarchy、Inspector、Scene/Game、Project、Console、UI、动画、Timeline、Particle Graph、统一命令/文档、构建设置 |
| 动画 | 2D 动画片段、骨骼动画、蒙皮网格、FBX Take、状态机、Timeline |
| 运行时 UI | Canvas、Text、Image、Button、指针输入、持久化的组件方法事件绑定 |
| VFX | GPU 模拟、Emitter/Init/Update/Rendering 阶段、类型化图 IR、Sprite/Mesh 粒子、Scene/Game 预览、实时参数 |
| Python | 组件生命周期、序列化字段、协程、Play 态热重载，以及公开渲染、VFX、物理和编辑器 API |
| 分发 | Hub、Windows 安装器、wheel、压缩运行时包、独立游戏导出 |

公开 Shader 工作流不再提供 Compute Shader 编排。引擎内部仍可在合适时通过 RHI 使用 Vulkan Compute；项目侧通过 Particle Graph 或对应的 Python 资产 API 描述粒子行为，不需要直接绑定引擎内部计算 Pass。

## MCP Harness

仓库内包含一套仅用于编辑器开发侧的 MCP Harness，用来做可重复、可控的引擎开发和验证。它最现实的出发点，是缓解一个严格意义上仍只有一人维护的项目所面临的测试瓶颈。

目前的工作流依然非常克制：AI 可以承担小规模编写、巡检和复现，人类负责审核改动并做工程决策。Harness 最初用于让开发者 Agent 通过公开 API 操作一个项目；一次偶然实验后，我发现同样的反馈循环也可以用于定位和迭代引擎自身的问题。

它区分两种模式：

- **开发辅助模式：** 读取语义化编辑器状态，通过公开 API 修改资产和场景，并协助搭建项目。
- **全局验证与卡点反馈模式：** 按帧或时间注入输入、确定性暂停、检查状态、建立检查点，并报告正常开发流程卡在哪里。

截图和录制是可选的人类审核材料，不是 Agent 的主要控制回路。MCP 只存在于编辑器开发环境，不会被塞进导出的游戏。

## 快速开始

### 环境要求

| 依赖 | Windows |
|:-----|:--------|
| 系统 | Windows 10/11，64 位 |
| Python | 3.12+ |
| Vulkan SDK | 1.3+ |
| CMake | 3.22+ |
| 编译器 | Visual Studio 2022，MSVC v143 |

仓库包含 macOS 和 Linux 的持续开发预设，但当前预览版主要支持 Windows 开发与分发。

### 克隆并准备环境

```bash
git clone --recurse-submodules https://github.com/ChenlizheMe/Infernux.git
cd Infernux
conda create -n infernux python=3.12 -y
conda activate infernux
pip install -r requirements.txt
```

如果克隆时没有获取子模块：

```bash
git submodule update --init --recursive
```

### 配置与构建

请使用仓库内的 CMake Preset，不要自行拼接另一套构建目录：

```bash
conda activate infernux
cmake --preset release
cmake --build --preset release
```

开发构建使用 `debug` 配置和构建预设。平台预设还包括 `release-macos`、`debug-macos`、`release-linux` 和 `debug-linux`。

### 启动 Hub

```bash
conda activate infernux
python packaging/launcher.py
```

### 运行测试

```bash
conda activate infernux
cd python
python -m pytest test/ -v
```

## 文档

- 官网：<https://infernux-engine.com/>
- 文档入口：<https://infernux-engine.com/wiki.html>
- 技术报告：[Infernux: A Python-Native Game Engine with JIT-Accelerated Scripting](https://arxiv.org/pdf/2604.10263)
- API 参考：发布于 `docs/wiki/site/`

网站发布工作流使用已经提交的 API Markdown，再生成静态文档、索引、多语言分包、站点地图和 Service Worker。需要更新 API 源 Markdown 时，应显式运行 `update_api_docs.bat`；网站发布不会悄悄重写公开 API 基线。

本地构建静态文档：

```bash
conda activate infernux
python -m mkdocs build --clean -f docs/wiki/mkdocs.yml
```

对应的 CMake 目标包括 `generate_api_docs` 与 `build_wiki_html`。

## 打包与分发

Release 构建会准备原生 wheel 和压缩的 Player Runtime Pack。可选的并行运行时会在 Release 阶段准备好，但只有项目构建设置确实需要时才会进入游戏包体。

```bash
conda activate infernux
cmake --build --preset packaging
cmake --build --preset packaging-installer
```

第一条命令构建便携 Hub，第二条构建 Windows 安装器。导出的游戏使用原生启动器和私有运行时，不会直接暴露代码仓库里的 Python 包组织。

## 引用

```bibtex
@software{chen2026infernux,
  author  = {Chen, Lizhe},
  title   = {Infernux},
  year    = {2026},
  version = {0.3.4},
  url     = {https://github.com/ChenlizheMe/Infernux},
  note    = {Open-source game engine with a C++17/Vulkan runtime and a Python production layer}
}
```

## 参与贡献

欢迎提交 Bug、功能建议和工作流反馈。请尽量附上引擎版本、环境、复现步骤，以及问题更可能位于原生运行时、Python 层、编辑器还是打包链路。

参见 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 和 [SUPPORT.md](SUPPORT.md)。

## 许可证

Infernux 使用 [MIT License](LICENSE) 发布。
