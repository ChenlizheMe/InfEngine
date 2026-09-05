# 插件

Infernux 的插件就是 InxPackage。Python、原生库、Wasm、Java 资源、材质、Shader、网页或任意文件都可以携带。

## 本地开发

本地导出时，用户选中的目录本身就是包根：

```text
abc/
  runtime/          # Editor 和 Player 都可用
  editor/           # 只供 Editor 使用
  plugin_pages/     # 插件窗口中的独立页面
  materials/
  shaders/
  web/
```

`inx_package.json` 可以不写。缺失时由 export 生成包内 metadata，默认 `name` 和 `reference` 取用户命名的 `.inxpkg` 文件名。例如把 `abc/` 导出为 `physics_tools.inxpkg`，默认身份就是 `physics_tools`。只有需要明确 metadata 时才写：

```json
{
  "reference": "studio/vfx-kit",
  "name": "VFX Kit",
  "version": "1.0.0",
  "engine": ">=0.4,<0.5",
  "intro": "Reusable visual effects."
}
```

manifest 目前不声明 `requirements` 或 `dependencies`。可选的 `requirements.txt` 只按固定文件名识别。

在 File Manager 中多选文件或目录后导出，会保留选中项相对于共同父目录的路径。普通内容导入时直接在 `Assets/Plugins` 下展开，不会按包名额外套一层目录。例如同时选择 `materials/` 和 `web/`，导入结果就是 `Assets/Plugins/materials/` 和 `Assets/Plugins/web/`。

## Git 仓库结构

Git 仓库只比本地包根多一层：

```text
vfx-kit/
  README.md          # 只给 GitHub 看，不进入包
  package.py         # 只依赖 Python 标准库的打包器
  CMakeLists.txt     # 可选作者构建，不进入包
  package/           # 唯一会进入 .inxpkg 的目录
    inx_package.json
    runtime/
    editor/
    plugin_pages/
    native/backend.pyd
    web/module.wasm
```

外层仓库不限制语言和构建工具。CMake、Cargo、Gradle、npm 或其它构建只需把最终产物放进 `package/`。打包器不会按扩展名猜用途，已知和未知文件都只是字节；目录位置决定所有权与 Player 导出规则。

可在任意工作目录运行 `python package.py [目标.inxpkg]`。脚本只依赖 Python 标准库并直接写出原生 InxPack 格式，因此作者的打包机器无需安装 Infernux 或 C++ 工具链。省略目标时，包会以仓库目录名写到脚本旁边；输入字节与元数据相同时，两次构建的归档字节完全一致。

## 安装路由

| 包内路径 | 项目位置 | Player |
|---|---|---|
| `runtime/...` | `Packages/<reference>/runtime/...` | 进入 |
| `editor/...` | `Packages/<reference>/editor/...` | 不进入 |
| `plugin_pages/...` | `Packages/<reference>/plugin_pages/...` | 不进入 |
| `requirements.txt` | `Packages/<reference>/requirements.txt` | 不进入 |
| 其它所有内容 | `Assets/Plugins/...` | 进入 |

`runtime`、`editor`、`plugin_pages` 必须精确小写。卸载按 GUID 追踪包拥有的文件，即使用户移动或修改了文件，只要所有权仍属于该包，卸载仍会删除它；已转移给其它包的共享文件会保留。

## 插件说明页

只有 `plugin_pages/` 下的 markdown 或文本会成为插件窗口内容。仓库根部 README 和许可证只是仓库文档，不再被读取成插件页面。中文文件在扩展名前加 `.zh-CN`，例如 `guide.zh-CN.md`；图片使用相对路径并留在包根内。

## Hub 共享存储

Hub 管理的插件库、Android Kit、Python 运行时、引擎版本和下载内容默认位于安装目录的 `InfernuxHubData/Shared/`。源码运行 `packaging/launcher.py` 时，位置为仓库内的 `packaging/InfernuxHubData/Shared/`，不取决于当前工作目录。`INFERNUX_SHARED_DATA_ROOT` 可显式指定共享位置，Hub 会将其传给启动的 Editor；`INFERNUX_PACKAGE_CACHE_ROOT` 仍可单独指定插件缓存。

项目记录和编辑器偏好仍属于小体积用户状态，使用 `INFERNUX_DATA_ROOT`；项目缓存留在项目内。安装器升级和失败回滚保留 Shared，更新包不会拥有其中的内容。Windows 安装器只为安装账户授予 Shared 的可继承修改权限，不放宽程序目录权限。

旧用户数据目录不会自动搬移或删除。已有部署在完成迁移前，可显式将 `INFERNUX_SHARED_DATA_ROOT` 指向原先存放 `Library`、`PlatformKits`、`Runtimes`、`Engines` 的根目录；这不是运行失败后的自动回退。直接启动、没有 Hub 环境的独立 Editor 仍可使用原有用户数据根。

Hub 设置页提供“迁移旧版共享资源”：先显示来源、目标和逐项清单，确认后在后台移动完整的 Python 运行时、Android Kit、引擎版本、插件包与已完成的 Python 下载归档。请先关闭编辑器、构建和下载；仍打开的已登记项目会阻止迁移。已有同名目标会整项跳过，旧副本保留，不合并或覆盖。跨盘复制完成后才移除原项，复制失败保留原项并明确报告；已经完成的项留在新位置。项目、用户状态、未完成下载和更新暂存区不参与迁移；单独配置插件缓存路径时不迁移该缓存。插件使用原有相对缓存引用，无需改写项目注册表。

卸载只移除 Hub 程序文件，保留 Shared 与项目记录。Windows 由独立系统 PowerShell 进程等待 Hub 退出后执行；清理出错会明确报告，不会提前宣称删除成功。保留安装标记使重装可以继续使用原有共享资源。

## 代码与 Player

生命周期代码继承 `InxPreload`。包内 Python 使用显式相对导入，每个已安装插件拥有隔离且确定的模块命名空间。`runtime/` 参与玩法组件加载和热刷新；`editor/` 只由 Editor 生命周期加载。

直接在 `Packages/<name>/runtime/` 编写本地包，不需要先安装自己才能构建 Player。构建会包含当前已索引的 runtime 文件及编译后的预载脚本，不会改写项目的安装所有权记录。简单包可以不写 manifest；`Packages/studio/tool/` 这样的命名空间目录需用 `inx_package.json` 明确包边界。即使 manifest 为未来分发 `.inxpkg` 指定了不同 reference，Player 仍保持当前项目目录对应的模块身份。

这里没有 include/exclude fallback 清单。`.pyd` 或 `.wasm` 放在 `runtime/` 就属于运行时，放在 `editor/` 就只属于编辑器。材质、Shader、HTML 和其它普通资产安装到 `Assets/Plugins`，再通过正常资产管线进入 Player。

## 按作者路径读取资产

`inx.Application.asset_path("Assets/Data/message.txt")` 和 `inx.Application.asset_path("Packages/studio/server/runtime/config.json")` 使用同一个通用资产读取入口，不限于某种语言或 `Resources` 目录。Editor 解析 `Assets` 或 `Packages` 中的作者文件；Player 通过构建时冻结的作者路径、GUID 和 Cook 产物绑定解析，找不到绑定会明确失败，不会扫描松散文件来补齐。

这个入口返回实际文件路径，可交给 `Path(...).read_text(encoding="utf-8")` 等读取函数。插件的 `preload()` 执行前，Player 已准备好这份资产目录，因此预载代码也可以读取 Cook 后的资产。路径只是查找键，作者目录不必原样存在于发行包旁边。

## 运行时原始资源

需要以原始文件形式交给外部运行时或库的内容放在 `runtime/`，例如 JAR、JSON、Wasm、词表或一整棵带相对 `include` 的目录。构建 Player 时会逐字节保留这棵目录及其相对结构；不要依赖当前工作目录，也不要从生命周期脚本的 `__file__` 推断安装位置。

普通玩法脚本通过 `inx.Application.package_path("studio/server", "runtime/server.jar")` 取得当前目标上的真实只读路径。`InxPreload.preload(context)` 内可用 `context.package_path("runtime/server.jar")`，不必重复 package reference。Windows/Linux/Android 从密封内容包准备产品私有的运行内容目录，Web 使用 Emscripten 虚拟文件系统；返回路径不承诺位于发行包旁边。接收文件路径并自行解析相对导入的库可以使用保留的包内目录结构。取得路径不代表目标平台能执行该文件：例如 Web 不能通过文件路径启动 Java 或原生 exe。

最终 Player 以 `Content.inxpkg` 等二进制包交付项目内容，不直接在发行目录中展开 `Assets`、`Library` 或 `Packages`。这是内容封装，并不承诺不可逆加密；需要文件系统的运行时会在产品私有位置准备内容。

`package_path` 只解析当前已安装且已进入 Player 的包内容，并拒绝绝对路径、盘符和 `..` 越界；资源缺失会明确失败。该路径是只读发布内容，运行时生成或修改的数据应写入 `inx.Application.persistent_data_path()`。

Player 中的 `package_path` 与 `asset_path` 共用冻结资产目录，不能通过往私有目录中放入同名松散文件来补齐缺失的 binding。`package_path("studio/server", "runtime/data")` 也可返回已发布原始资源的目录：目录成员关系来自构建时登记的资产，目录内的 JSON 可以继续用相对文件名读取旁边的 TXT 等文件。未发布的目录不能解析；普通 `asset_path` 仍只返回文件。
