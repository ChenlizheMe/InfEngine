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

## 安装路由

| 包内路径 | 项目位置 | Player |
|---|---|---|
| `runtime/...` | `Packages/<reference>/runtime/...` | 进入 |
| `editor/...` | `Packages/<reference>/editor/...` | 不进入 |
| `plugin_pages/...` | `Packages/<reference>/plugin_pages/...` | 不进入 |
| `requirements.txt` | `Packages/<reference>/requirements.txt` | 不进入 |
| 其它所有内容 | `Assets/Plugins/...` | 进入 |

`runtime`、`editor`、`plugin_pages` 必须精确小写。文件靠 GUID 识别，卸载时只删除属于该包且用户没有修改的文件。

## 插件说明页

只有 `plugin_pages/` 下的 markdown 或文本会成为插件窗口内容。仓库根部 README 和许可证只是仓库文档，不再被读取成插件页面。中文文件在扩展名前加 `.zh-CN`，例如 `guide.zh-CN.md`；图片使用相对路径并留在包根内。

## 代码与 Player

生命周期代码继承 `InxPreload`。包内 Python 使用显式相对导入，每个已安装插件拥有隔离且确定的模块命名空间。`runtime/` 参与玩法组件加载和热刷新；`editor/` 只由 Editor 生命周期加载。

这里没有 include/exclude fallback 清单。`.pyd` 或 `.wasm` 放在 `runtime/` 就属于运行时，放在 `editor/` 就只属于编辑器。材质、Shader、HTML 和其它普通资产安装到 `Assets/Plugins`，再通过正常资产管线进入 Player。

## 运行时原始资源

需要以原始文件形式交给外部运行时或库的内容放在 `runtime/`，例如 JAR、JSON、Wasm、词表或一整棵带相对 `include` 的目录。构建 Player 时会逐字节保留这棵目录及其相对结构；不要依赖当前工作目录，也不要从生命周期脚本的 `__file__` 推断安装位置。

普通玩法脚本通过 `inx.Application.package_path("studio/server", "runtime/server.jar")` 取得当前目标上的真实只读路径。`InxPreload.preload(context)` 内可用 `context.package_path("runtime/server.jar")`，不必重复 package reference。Windows/Linux 返回 Player 数据目录中的路径，Android 返回校验后解包到应用私有内容目录的路径，Web 返回 Emscripten 虚拟文件系统中的路径；因此接收文件路径并自行解析相对导入的库可以继续使用同一目录结构。

`package_path` 只解析当前已安装且已进入 Player 的包内容，并拒绝绝对路径、盘符和 `..` 越界；资源缺失会明确失败。该路径是只读发布内容，运行时生成或修改的数据应写入 `inx.Application.persistent_data_path()`。
