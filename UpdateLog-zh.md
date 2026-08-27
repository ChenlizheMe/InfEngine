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
