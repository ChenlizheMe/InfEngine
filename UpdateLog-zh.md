# Infernux v0.4.0 · 多平台构建与分发

0.4.0 加入 Windows、Linux、Android 和 Web Player 构建，完善 InxPackage 创作与运行时资产分发，并将共享构建环境交给 Hub 管理。

[English release notes](UpdateLog.md)

**版本对比：** [`v0.3.7...v0.4.0`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.7...v0.4.0)

### 多平台

- 通过统一构建服务生成 Windows/Linux 编辑器与 Player、Android APK/AAB，以及 Web Player。
- 四个平台插件携带预编译 Player、运行时和目标工具；普通导出不需要引擎源码、子模块、CMake 或原生引擎编译。
- 原生目标使用 Vulkan，浏览器使用 WebGPU，覆盖 Python 玩法、渲染、输入、UI、音频和粒子。
- 使用同一个 MultiPlatform040 项目进行跨平台验证，包括点击按钮读取包内文本资产。
- 加入 Windows/Linux 桌面端与各平台 Player 的 CI 构建，以及 Web 浏览器验收。

### 插件与资产

- Git 仓库只打包 `package/`，使用独立 `package.py`；README 和各语言的构建配置留在包外。
- 保留直接选中本地文件夹创作的方式；未提供 manifest 时，根据输出文件名生成默认包元数据。
- 插件支持携带脚本、材质、Shader 和任意运行时文件；`Packages/` 内资产具有显式元数据并参与实时刷新。
- Player 资产保留在 `Content.inxpkg` 中，不再展开项目创作目录树；引擎按路径通过构建后的 GUID 身份读取资产，并为需要真实路径的载荷提供文件系统访问。
- 分离编辑器内容与 Player 载荷，平台构建支持以可选插件分发。
- 检查兼容的 GitHub Release 并显式选择插件更新，保留 GUID、启用状态和用户新增文件；覆盖本地修改前要求确认。
- 独立刷新官方发现目录，不升级项目固定的插件版本，并将旧平台源解析到对应独立仓库。

### Hub 与构建环境

- 提供 Windows/Linux Hub 分发与托管的 Python 3.13 环境。
- Windows/Linux Hub 从渠道安装安卓支持，共享 SDK、NDK、JDK、Gradle 和目标 Python 依赖，自动提供工具链路径；安装支持后才允许导入安卓插件。
- Hub Library 复用下载内容，项目构建缓存保留在项目内部。
- 在“安装”入口下分别管理引擎、Python 和安卓支持；支持后台安装、紧凑进度条、可展开队列和系统托盘。
- 中断的安卓套件下载留在 Hub 共享缓存，用户重新发起安装时续传，安装成功后清理下载缓存。

---

# Infernux v0.3.7 · 插件系统与骨骼动画

0.3.7 加入 InxPackage 插件系统，把 MCP 从引擎核心中拆出，并修复从独立 FBX 文件导入的骨骼动画。

[English release notes](UpdateLog.md)

**版本对比：** [`v0.3.6...v0.3.7`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.6...v0.3.7)

### 插件

- 可以装 `.inxpkg`、本地目录、Git，或从官方列表安装。
- `Runtime/` 进游戏，`Editor/` 留在编辑器。
- 插件窗口里的说明来自 `README.md`、`LICENSE` 和 `InxPluginPages/`。
- MCP 变成官方默认插件 `infernux/mcp`。新项目会带上，可以禁用或卸载。

### 骨骼动画

- 独立动画 FBX 通过精确关节名称映射，同时允许 Assimp 在对应关节之间插入 pivot 和辅助节点。
- 对关节改名或层级不兼容的骨架明确拒绝自动重定向，不再按几何形状猜测并错误驱动肢体。

### 编辑器与创作流程

- 插件窗口支持安装、启用、禁用、重载和卸载，并从下载开始持续显示进度直到激活完成。
- Headless 和 MCP 创作走与编辑器相同的场景、命令、权限和撤销路径。

---

# Infernux v0.3.6 · 统一 Hierarchy 与 Hub 更新

0.3.6 统一了编辑器 Hierarchy，并恢复打包版 Infernux Hub 的自动更新发现能力。

**版本对比：** [`v0.3.5...v0.3.6`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.5...v0.3.6)

### 编辑器

- 移除独立的 Hierarchy UI 模式；场景对象和 UI 对象现在共用同一棵树、同一套选择模型与右键菜单路径。
- 在统一 Hierarchy 中保留 Canvas 只能位于根节点、UI Screen 子树不能离开 Canvas 等结构约束。

### Hub 更新

- 恢复 Nuitka 打包版 Hub 启动时的自动更新检查。
- 修复“安装引擎版本”列表错误继承桌面系统配色的问题，使其始终遵循 Hub 当前主题。
- 增加经过校验的完整包更新回退，并为受保护安装目录请求管理员权限。
- 发布流程改为提供可独立安装的完整 Hub 包，不再发布增量 patch 资产。
