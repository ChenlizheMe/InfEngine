# Infernux v0.3.4 · 完整引擎更新

0.3.4 是 Infernux 至今规模最大的一次发布。渲染、GPU VFX、编辑器交互、资产、运行时、物理和 Player 分发路径都围绕明确的数据所有权与生产工作流进行了重建，同时继续保持 Python 作为公开的玩法与工具层。

[English release notes](UpdateLog.md)

**版本对比：** [`v0.2.9...v0.3.4`](https://github.com/ChenlizheMe/Infernux/compare/v0.2.9...v0.3.4)

### 版本亮点

- 可编程 RenderStack 与 RenderGraph 现在覆盖 Forward、Forward+、Deferred 路由、可复用 Effect、明确的阶段挂载点、自定义管线与逐相机渲染状态。
- GPU Particle Graph 现已覆盖类型化图编辑、Graph 到 IR 的 AOT 编译、Sprite、Mesh、Ribbon 输出、事件、延续执行、排序、裁剪、软粒子与六面光照烟雾基础能力。
- 编辑器通过统一 Interaction Core 管理 Scene、Project、Inspector、Timeline、动画、FSM 与节点编辑中的命令、快捷键、选择、焦点、文档、保存关闭、脏状态与撤销回退。
- 资产系统以 GUID 身份和 Library 制品作为持久契约，移动与重命名不再破坏引用，并明确隔离编辑源数据与 Player 内容。
- 独立游戏使用原生启动器、私有 Python 运行时、压缩的 `Content.inxpkg`、完整运行时着色器与引擎资产，不携带编辑器服务。

### 渲染与着色器

- 加入可由 Python 编排的 RenderStack 管线、按顺序执行的 Effect 与 EffectGroup 资产、明确渲染阶段、运行时参数更新和失败回退。
- 统一 Forward、Forward+ 与 Deferred 的几何提交、材质、光照、阴影和相机契约。
- RenderGraph 新增类型化资源与 Pass、结构编译缓存、瞬态资源别名、明确同步意图、Renderer List 资源与逐帧安全退休。
- 加入结构化 GLSL 阶段元数据与接口链接，材质可以明确组织顶点、片元与 Shading Model，而不必手写 Descriptor Layout。
- 加入统一几何缓冲请求，可获取深度、法线、基础颜色、运动向量和项目自定义缓冲，也可从指定 Pass 采样命名缓冲。
- 相机裁剪、阴影、Render Target 与光照资源改为逐相机隔离，Scene、Game、预览与运行时相机不再共享可变帧状态。
- 加入 Forward+ Tile Light List、统一 GPU 光源数据、Light Mask、粒子光照与链接材质表面。
- 改进阴影稳定性、级联策略、资源生命周期、MSAA 路由、Display Encode、全屏效果、描边、Picking、Gizmo、Preview 和编辑器/Player 渲染一致性。
- 加入编辑器、独立视图和游戏相机的引擎内置截图能力。

### GPU Particle Graph

- 公开粒子工作流改为 GPU 优先，并通过一套模型统一 Emitter Settings、Init、Update、事件与 Rendering 阶段。
- 加入类型化通用图核心、Particle Graph 编辑器、节点默认值、类型化属性、曲线、渐变、噪声、条件、归一化年龄、生命周期旋转与尺寸，以及运行时公开参数。
- 加入 Sprite、静态 Mesh 与 GPU Ribbon 输出，支持实时材质与纹理、对齐、UV、Flipbook 基础、排序、Indirect Draw 与逐视图裁剪。
- 加入类型化 GPU 事件载荷、事件路由、间接生成、延续执行、等待与图所有的生命周期状态。
- 加入平面、球体、表面、Vector Field、SDF、蒙皮姿态与场景深度交互基础。
- 加入软粒子、六面光照烟雾、发射器生命周期控制、Scene/Game 预览、选择、Gizmo、Bounds、诊断与调度遥测。

### 资产、文档与序列化

- Scene 与 Component 使用带修订号的类型化文档，具备稳定对象/组件身份、校验、事务发布、回滚和脚本缺失恢复。
- 加入资产索引、依赖追踪、持久 GUID 引用、异步 Refresh 仲裁、冲突感知原子写入和读取当前磁盘内容的重载语义。
- 资产移动和重命名可以保持引用，不再依赖运行时路径解析。
- 扩展模型、纹理、着色器、音频、材质、Prefab、动画、VFX 与物理材质的导入和预览路径。
- 纹理导入增加制品格式、Mip、压缩设置、与 GPU 一致的预览，并扩展常用图像、模型和音频格式支持。
- 移除旧序列化和旧 Shader 语法路径，统一到当前 Schema 与明确迁移错误。

### 编辑器创作与交互

- 加入全局命令与快捷键路由，支持面板焦点判断和 Play 状态下编辑。
- Hierarchy、Scene、Project、Inspector、UI、Timeline、Console、FSM 与节点编辑器共享唯一全局选择。
- 全局 Action Journal 以事务方式记录场景、资产、组件、文档导航、Timeline、节点图与 UI 创作操作并执行撤销回退。
- 所有创作窗口统一 Save、Save As、Discard、Cancel、关闭、退出、外部更改仲裁与脏状态所有权。
- Particle Graph 与动画 FSM 共享通用 Node Graph 的连线、动态重建、选择、参数、上下文命令与撤销基础。
- 改进 Hierarchy 展开、持久对象、组件排序、序列化资源字段、Project 搜索导航、Console 过滤、Inspector 刷新与编辑面板 Docking。

### 运行时、脚本、物理与动画

- Python 组件支持在 Play 中热重载方法与序列化字段，不会因为代码更新重新触发生命周期入口。
- 改进运行时组件代理、生命周期分发、协程等待、`WaitForEndOfFrame`、延迟场景加载与场景请求代际控制。
- 加入原生批量 Transform 与 GameObject 创建，同时保留标准场景模型和公开 `instantiate` 工作流。
- 改进 Jolt 刚体同步、物理材质、碰撞体、固定步长、插值、碰撞回调，以及阴影与可见物体 Transform 的一致提交。
- 扩展 2D Clip、Sprite 动画、Timeline、骨骼动画、蒙皮网格、FBX Take、动画状态机与共享图编辑。
- 加入 `DontDestroyOnLoad` 场景所有权和持久运行时对象的 Hierarchy 展示。

### Player 构建与分发

- 导出的游戏使用一个原生启动器和私有运行时，不会安装、替换或修改用户的系统 Python 环境。
- 加入压缩的 `Content.inxpkg` 与基于 Library 的运行时资产，并排除编辑器面板、创作元数据、缓存、测试和仅源码服务。
- 打包内置及项目 Shader 源码、Effect、自定义 Pipeline、Display Encode、全屏 Pass 等保证编辑器与 Player 一致的渲染依赖。
- 加入随 Wheel 分发的 Player Runtime Pack、便携 Hub 压缩包、安装器、增量 Hub Manifest 与 SHA-256 发布清单。
- 构建扫描、内容打包与 Finalize 等长任务移出编辑器帧循环，并通过构建界面提供进度与取消。
- Release 构建屏蔽原生 Debug/Info 噪声，同时保留 Python 日志与原生 Error。

### MCP 与验证

- 加入编辑器侧 MCP Harness，用于确定性项目创建、场景和资产创作、语义 UI 检查、有限输入、检查点、截图与阻塞反馈。
- MCP 只服务于编辑器开发和验证，不会进入导出的游戏。
- 扩展 Vulkan、Shader 链接、渲染器、物理、Headless、Python、打包、网站与安装态 Wheel 回归测试。

### 升级说明

- 使用 0.3.4 打开旧项目之前，请先备份或创建分支。本版本有意移除了多条旧数据与旧 Shader 兼容路径。
- 升级后重新导入项目资产，以生成当前版本的 Library 制品和依赖记录。
- 自定义 Shader 必须使用当前结构化 Schema 与精确 Shader ID；旧 `@` 语法和手写引擎 Descriptor Layout 不再受支持。
- 粒子创作采用 GPU 优先路径，旧 CPU Particle 专用内容不属于 0.3.4 支持工作流。
- Windows 10/11 x64 与 Python 3.12 仍是主要支持的开发和分发目标。

### 社区

- 文档：<https://infernux-engine.com/wiki.html>
- 社区论坛：<https://infernux-engine.discourse.group/>
- 问题反馈：<https://github.com/ChenlizheMe/Infernux/issues>
