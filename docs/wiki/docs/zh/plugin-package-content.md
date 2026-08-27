# 插件

Infernux 的插件就是一个 InxPackage。代码、场景、Prefab、贴图都可以打进去。

用法接近 Unity 的 Package Manager：丢一个 `.inxpkg`、选一个本地目录、贴 GitHub 地址，或者从官方列表里点安装。

## 目录怎么放

```
MyPlugin/
  InxPackage.json
  README.md
  README.zh-CN.md
  LICENSE
  requirements.txt
  Runtime/            # 进游戏
  Editor/             # 只在编辑器里跑
  InxPluginPages/     # 插件窗口里的额外页
  ...                 # 其余都当普通资源
```

装进项目之后：

| 你放的 | 落到项目里 |
|---|---|
| `Runtime/`、`Editor/`、README、许可证 | `Packages/<插件名>/` |
| 模型、场景、Prefab、贴图 | `Assets/Plugins/<插件名>/` |

`Runtime`、`Editor`、`InxPluginPages` 这三个名字大小写要写对。如果你只是想放一个普通文件夹也叫 Runtime，外面再包一层，比如 `Content/Runtime`。

插件名可以带命名空间，例如 `studio/vfx-kit`。目录套在一起不代表有依赖关系。

## InxPackage.json

最少写这些：

```json
{
  "reference": "studio/vfx-kit",
  "name": "VFX Kit",
  "version": "1.0.0",
  "engine": ">=0.3.7,<0.4"
}
```

`reference` 装完不会变，是这个插件的身份。

## 插件窗口里的说明

不用写编辑器代码也能出说明页：

- `README.md` 是描述页，`README.zh-CN.md` 是中文版
- `LICENSE` 是许可证页
- `InxPluginPages/` 里的 markdown / 文本会变成额外页签

中文文件只认一种写法：在扩展名前加 `.zh-CN`，例如 `Guide.zh-CN.md`。编辑器是中文就显示中文，没有就回退默认文件。`.en`、`Docs/`、语言目录这些引擎都不认。

图片用普通 markdown 语法，文件要打进包里。网上的图不会下载。

想自己排页签顺序，在 `InxPackage.json` 里写 `pages`：

```json
{
  "pages": [
    {"id": "intro", "title": "描述", "path": "README.md"},
    {"id": "guide", "title": "使用", "path": "InxPluginPages/Guide.md"}
  ]
}
```

没写 `intro` 的话，列表里的短介绍会取 README 第一段。

## 编辑器启动时跑代码

继承 `InxPreload`。不用在 json 里登记，引擎会自己扫 `Assets` 和 `Packages`：

```python
from Infernux.lifecycle import InxPreload

class Bootstrap(InxPreload):
    def preload(self, context):
        pass

    def unload(self):
        pass
```

禁用的包不会加载。`unload` 失败时引擎会停下来让你重启，不会假装卸干净了。

## 依赖

`requirements.txt` 里写别的插件名，会先当插件装；对不上再交给 pip。pip 装进来的只是 Python 包，不会变成 Infernux 插件。pip 失败时会尽量卸掉这次新装的东西，本地 wheel 或 git 装的不一定能原样恢复。

## 打包游戏

`Runtime` 和普通资源会进 Player。`Editor`、README、许可证、说明页不会。没有单独的包含/排除列表。

## 装、卸、搬家

文件靠 GUID 认，你在项目里挪位置没关系。卸载只删这个包自己的、而且你没改过的文件。父包卸了不会连带卸子包。

包里面再塞一个 `.inxpkg`，默认只是个文件。你自己再导入，或写进 `requirements.txt`，才会当真插件装。
