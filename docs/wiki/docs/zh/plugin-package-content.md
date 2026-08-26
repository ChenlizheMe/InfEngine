# InxPackage 信息页规范

Infernux 插件无需编写编辑器专用 Python 代码即可提供面向用户的信息页。GitHub 源与 `.inxpkg` 使用同一套约定，因为源码仓库在安装前也会先规范化为 InxPackage。

## 约定文件

布局是死规定，不进行猜测：

- `README.md` 是默认“描述”页，`README.zh-CN.md` 是它的简体中文版本。
- `LICENSE` 是默认“许可证”页，`LICENSE.zh-CN.md` 是它的简体中文版本。
- `InxPluginPages/` 下每个 `.md`、`.markdown` 或 `.txt` 文件成为额外页签。本地化文件只能在扩展名前插入 `.zh-CN`：`InxPluginPages/Guide.md` 与 `InxPluginPages/Guide.zh-CN.md` 是同一个页面。

英文直接使用无后缀默认文件。`.en`、`.zh`、语言目录、`Documentation/`、`Docs/` 及其他 locale 写法都不是本地化约定。编辑器使用中文时精确选择 `.zh-CN` 文件；不存在时回退到默认文件。其他编辑器语言一律使用默认文件。

manifest 未填写 `intro` 时，`README.md` 的第一个有效段落成为默认注册表摘要；`README.zh-CN.md` 会自动提供中文摘要。

所有页面都必须是 UTF-8 文本，位于插件根目录之内，并包含在包的文件列表中。

## Markdown 图片

Markdown 页面支持标准的本地图片语法，例如：

```markdown
![控制面板预览](Images/control-panel.png)
```

相对路径以当前 Markdown 文件所在目录为基准；以 `/` 开头的路径以插件根目录为基准。图片文件必须包含在插件或 `.inxpkg` 内，任何逃逸插件根目录的路径都会被拒绝。GitHub 插件克隆后也使用同一规则。远程 HTTP 图片不会在编辑器 UI 线程中下载；无法解析的图片会显示替代文本。

Markdown 页面还会保留一级至六级标题、段落、分隔线、有序/无序嵌套列表、引用块和围栏代码块的结构。标题层级使用统一字号下的颜色、分隔和前缀表达，以遵循编辑器的全局字体规范。

## 显式页签顺序

`InxPackage.json` 可以通过 `pages` 指定 ID、标题、路径、格式和顺序：

```json
{
  "pages": [
    {"id": "intro", "title": "概览", "path": "README.md"},
    {"id": "guide", "title": "任务指南", "path": "InxPluginPages/Guide.md"},
    {"id": "license", "title": "许可证", "path": "LICENSE", "format": "text"}
  ]
}
```

支持 `markdown` 和 `text` 两种格式。显式页面优先显示，并覆盖相同 `(id, locale)` 的自动发现页面。唯一合法的本地化 descriptor 是 `"locale": "zh-CN"`。

## InxPackage v2 布局

`.inxpkg` 是唯一的独立插件/内容包格式。安装器只解释包根的约定目录，不接受任意目标路径映射：

| 包内路径 | 项目路径 | 角色 |
|---|---|---|
| `Runtime/**` | `Packages/<reference>/Runtime/**` | Editor、Headless 与 Player 运行代码 |
| `Editor/**` | `Packages/<reference>/Editor/**` | 仅 Editor/Headless |
| manifest、README、LICENSE、requirements 和信息页 | `Packages/<reference>/**` | 控制面与文档 |
| 其他全部内容 | `Assets/Plugins/<reference>/**` | 场景、Prefab、模型、纹理等普通资产 |

`Runtime`、`Editor` 和 `InxPluginPages` 必须使用规范大小写。`Docs`、`Documentation` 等名字没有包控制语义，只会作为普通资产导入。若开发者希望把保留名称作为普通内容，可增加一层目录，例如 `Content/Runtime`。`reference` 可以是多段命名空间，如 `aabbc/physics/jolt`；父包和子包可以共存，目录嵌套本身不表示依赖。

`Library` 只保存下载缓存、安装 staging 和引擎资源镜像，不是活动插件目录。安装器也不会把文件散落到 `Assets` 根。

## GUID、重复导入与卸载

每个载荷文件及其 `.meta` 都进入包 inventory，记录 GUID、SHA-256、角色和路径提示。GUID 是持久身份，路径只表示当前位置。因此用户在 AssetDatabase 中移动或重命名资产后，插件管理器仍可找到它；构建也从 GUID catalog 解析实际文件。

- 相同 GUID 且内容 hash 相同可以由多个包复用，但只有一个账本 owner；owner 卸载时会转移所有权。
- 相同 GUID 但内容不同，或同一路径被不同 GUID 占用，安装会在写入前报冲突。
- 卸载只删除本包实际拥有、且未被用户修改的文件；父 reference 不会递归删除子包。
- 安装和卸载均使用 staging、原子替换和回滚；项目状态写入 `ProjectSettings/InxPlugins.json`，可重现证据写入 `ProjectSettings/InxPackages.lock.json`。

包内嵌套的 `.inxpkg` 默认只是普通内容。只有用户明确导入，或 `requirements.txt` 明确引用它时才安装。

## 依赖、来源与 Python 环境

直接 `.inxpkg`、本地目录、普通 Git、GitHub、其他托管平台和 HTTP artifact 最终都先规范化为同一 InxPackage v2，再进入同一安装事务。每个安装制品会按 SHA-256 缓存在项目 `Library/InxPackageCache`，用于校验和离线重装。

`requirements.txt` 中的普通名称先匹配项目的官方 InxPackage 注册表；匹配时安装对应插件，不匹配时才交给 pip。URL、VCS、wheel、本地路径和任意合法 pip 参数保持 pip 语义。pip distribution 只是 Python 依赖，不自动获得插件生命周期或资产所有权；所有 pip 命令必须使用项目 Python，并记录到 lock。

## 通用 InxPreload 生命周期

preload 不在 manifest 中声明。只有静态分析确认继承 `Infernux.lifecycle.InxPreload` 的候选脚本才会被 import：

```python
from Infernux.lifecycle import InxPreload

class Service(InxPreload):
    def preload(self, context):
        pass

    def unload(self):
        pass
```

扫描范围包括 `Assets` 与 `Packages`，支持 alias 和间接继承。生命周期身份由脚本 GUID 和类型身份组成；包禁用时不加载，依赖决定加载顺序，卸载按相反顺序。`unload()` 失败会中止禁用/卸载并标记必须重启，不能伪装成功。

## Player 导出与默认库

Player 选择规则是结构性的：`Runtime` 和普通 Content 进入游戏，`Editor`、控制文件和信息页不进入。manifest 没有自定义 include/exclude 或 `player` 裁剪语言。Packages 中导出的 Python 脚本会编译为字节码。

引擎 wheel 将官方注册表、已校验 `.inxpkg` 和 `default-libraries.json` 放在 `Infernux/resources/official_packages`，项目中的只读镜像位于 `Library/Resources/official_packages`。默认列表只在新项目创建或用户明确要求补齐时安装；普通启动不会把用户卸载的插件重新装回。0.3.7 的第一个且当前唯一的官方默认插件是 `infernux/mcp`。
