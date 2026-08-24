# Infernux v0.3.6 · 统一 Hierarchy 与 Hub 更新

0.3.6 统一了编辑器 Hierarchy，并恢复打包版 Infernux Hub 的自动更新发现能力。

[English release notes](UpdateLog.md)

**版本对比：** [`v0.3.5...v0.3.6`](https://github.com/ChenlizheMe/Infernux/compare/v0.3.5...v0.3.6)

### 编辑器

- 移除独立的 Hierarchy UI 模式；场景对象和 UI 对象现在共用同一棵树、同一套选择模型与右键菜单路径。
- 在统一 Hierarchy 中保留 Canvas 只能位于根节点、UI Screen 子树不能离开 Canvas 等结构约束。

### Hub 更新

- 恢复 Nuitka 打包版 Hub 启动时的自动更新检查。
- 修复“安装引擎版本”列表错误继承桌面系统配色的问题，使其始终遵循 Hub 当前主题。
- 增加经过校验的完整包更新回退，并为受保护安装目录请求管理员权限。
- 发布流程改为提供可独立安装的完整 Hub 包，不再发布增量 patch 资产。
