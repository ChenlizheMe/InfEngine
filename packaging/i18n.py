"""Small runtime translation layer for the early Infernux Hub."""

from __future__ import annotations

import locale
import sys


_ZH = {
    "Migrate Legacy Resources": "迁移旧版共享资源",
    "Shared resources: {path}": "共享资源目录：{path}",
    "Moving resources. Keep Hub open until this finishes.": "正在移动资源，请保持 Hub 打开直至完成。",
    "Move:": "将迁移：",
    "Keep at old location (target exists):": "保留在旧位置（目标已存在）：",
    "Move {count} complete resources from {source} to {destination}?\nClose all Editors, builds and downloads first. Existing targets ({conflicts}) will be skipped and retained at the old location. Projects, settings and unfinished downloads are not moved. See details for the exact list.": "将 {count} 项完整资源从 {source} 迁移到 {destination}？\n请先关闭所有编辑器、构建和下载。目标已存在的 {conflicts} 项将跳过并保留在旧位置。不移动项目、设置或未完成的下载。展开详情可查看完整清单。",
    "Moved {count} resources. {conflicts} existing targets were skipped; their old copies have not been deleted.": "已迁移 {count} 项资源。跳过 {conflicts} 项已存在的目标，其旧副本未被删除。",
    "Projects": "项目",
    "Installs": "安装",
    "Settings": "设置",
    "Forum": "论坛",
    "Discussion": "讨论区",
    "Community": "社区",
    "Hub": "启动器",
    "Dark Mode": "深色模式",
    "Create, open and launch your Infernux projects.": "创建、打开并启动 Infernux 项目。",
    "Search projects...": "搜索项目……",
    "No projects yet": "还没有项目",
    "Create a new project or open an existing project to get started.": "创建新项目或打开已有项目以开始使用。",
    "Project path missing": "项目路径不存在",
    "Open project folder": "在资源管理器中显示项目",
    "Project actions": "项目操作",
    "Remove": "移除",
    "Relocate": "重新定位",
    "Migrate": "升级版本",
    "+ New Project": "+ 新建项目",
    "Open Existing": "打开已有项目",
    "Open": "打开",
    "New": "新建",
    "Show in Explorer": "在资源管理器中显示",
    "Remove from Hub": "从 Hub 移除",
    "Launch": "启动",
    "Remove the selected project from Hub without deleting its files": "从 Hub 移除所选项目，但不删除文件",
    "Update the location of the selected project": "更新所选项目的位置",
    "Migrate the selected project to another installed engine version": "将所选项目升级到另一个已安装的引擎版本",
    "No Selection": "未选择项目",
    "Please select a project to launch.": "请选择要启动的项目。",
    "Please select a project to remove from Hub.": "请选择要从 Hub 移除的项目。",
    "Please select a project to relocate.": "请选择要重新定位的项目。",
    "Please select a project to migrate.": "请选择要升级的项目。",
    "Project Path Missing": "项目路径不存在",
    "Project Already Open": "项目已打开",
    "Missing Runtime": "运行环境缺失",
    "Native Runtime Check Failed": "原生运行环境检查失败",
    "Open Existing Infernux Project": "打开已有 Infernux 项目",
    "Relocate Infernux Project": "重新定位 Infernux 项目",
    "Cannot Open Project": "无法打开项目",
    "Cannot Relocate Project": "无法重新定位项目",
    "Engine Version Not Installed": "未安装引擎版本",
    "Remove Project from Hub": "从 Hub 移除项目",
    "Project files will not be deleted.": "不会删除项目文件。",
    "Cancel": "取消",
    "Close": "关闭",
    "Create": "创建",
    "Create New Project": "创建新项目",
    "Set the project name, location and Infernux version.": "设置项目名称、位置和 Infernux 版本。",
    "Project Name:": "项目名称：",
    "Enter a name for your project": "输入项目名称",
    "Project Location:": "项目位置：",
    "No path selected": "未选择路径",
    "Browse...": "浏览……",
    "Engine Version:": "引擎版本：",
    "No engine versions installed. Go to the Installs tab to download one first.": "尚未安装引擎版本。请先前往“安装”页面下载。",
    "Select an installed engine version before creating a project.": "创建项目之前请选择一个已安装的引擎版本。",
    "dev (current environment)": "开发环境（当前环境）",
    "Choose Project Location": "选择项目位置",
    "Initializing": "正在初始化",
    "Preparing project...": "正在准备项目……",
    "Setting up project structure...": "正在建立项目结构……",
    "Copying engine libraries...": "正在复制引擎库……",
    "Setting up Python runtime...": "正在配置 Python 运行环境……",
    "Preparing asset folders...": "正在准备资源目录……",
    "Almost there...": "即将完成……",
    "Creating project folders...": "正在创建项目目录……",
    "Finalizing project...": "正在完成项目创建……",
    "Writing project editor settings...": "正在写入编辑器设置……",
    "Installing Infernux into the project runtime...": "正在将 Infernux 安装到项目运行环境……",
    "Installing Infernux engine files...": "正在安装 Infernux 引擎文件……",
    "Validating project runtime...": "正在验证项目运行环境……",
    "Creating project backup...": "正在创建项目备份……",
    "Preparing target engine runtime...": "正在准备目标引擎运行环境……",
    "Installs and managed runtime": "引擎版本与托管运行环境",
    "Android compatibility": "Android 兼容",
    "SDK, NDK, JDK, Gradle and Android CPython are managed once for every project.\n{path}": "SDK、NDK、JDK、Gradle 与 Android CPython 由 Hub 安装一次并供所有项目复用。\n{path}",
    "Installed files need repair: {message}": "已安装的文件需要修复：{message}",
    "Required before the Android platform plugin can be imported. Large toolchains are installed once and shared by every project.": "导入 Android 平台插件前必须先安装。大型工具链只安装一次，并由所有项目共享。",
    "Locate Bundle": "导入离线包",
    "Repair": "修复",
    "Installing Android compatibility": "正在安装 Android 兼容",
    "Installing shared Android build support": "正在安装共享 Android 构建支持",
    "Hub is extracting the selected offline Platform Kit into shared Android build support. This window will close when installation finishes.": "Hub 正在将所选离线 Platform Kit 解压到共享 Android 构建支持目录。安装完成后此窗口将自动关闭。",
    "Hub is downloading one immutable Platform Kit containing the SDK, NDK, JDK, Gradle and Android CPython runtimes. It is not copied into projects or plugin packages.": "Hub 正在下载一个不可变的 Platform Kit，其中包含 SDK、NDK、JDK、Gradle 与 Android CPython 运行时；它不会被复制到项目或插件包中。",
    "Android compatibility installed": "Android 兼容已安装",
    "Android compatibility is ready for every Infernux project at:\n{path}": "Android 兼容已可供所有 Infernux 项目使用：\n{path}",
    "Android compatibility installation failed": "Android 兼容安装失败",
    "Select Android compatibility bundle": "选择 Android 兼容离线包",
    "Invalid Android compatibility bundle": "无效的 Android 兼容离线包",
    "Locate": "导入本地版本",
    "Install Editor": "安装引擎",
    "Python {version} (default)": "Python {version}（默认）",
    "Python {version}": "Python {version}",
    "Not installed. Install this runtime before using its engine wheels.": "尚未安装。使用对应引擎 wheel 前请先安装此运行环境。",
    "Reinstall": "修复",
    "Preparing Python {version}": "正在准备 Python {version}",
    "Preparing Python {version} for Infernux Hub": "正在为 Infernux Hub 准备 Python {version}",
    "A background setup process is extracting an isolated full Python {version} runtime under the Infernux Hub runtime directory. Projects targeting this Python version receive their own copy. Your existing Python installations are not used or changed. This window will close automatically when setup finishes.": "后台正在 Infernux Hub 运行时目录中解压隔离的完整 Python {version} 运行环境。绑定此 Python 版本的项目会获得自己的副本。电脑上已有的 Python 不会被使用或改动，准备完成后此窗口会自动关闭。",
    "Infernux {engine} requires Python {version}. Please install Python {version} first.": "Infernux {engine} 需要 Python {version}。请先安装 Python {version}。",
    "Python Runtime Required": "需要 Python 运行环境",
    "This Infernux Hub requires Python {version} for current engine releases. Install Python {version} from Installs before installing or creating a project with them.\n\nOlder engine versions continue to use their own matching Python runtime.": "当前引擎版本需要 Python {version}。请先在“安装”页面安装 Python {version}，再安装该引擎或用它创建项目。\n\n旧引擎版本会继续使用各自匹配的 Python 运行环境。",
    "Python {version} is ready at:\n{path}": "Python {version} 已就绪：\n{path}",
    "Installed": "已安装",
    "Install": "安装",
    "Install Engine Version": "安装引擎版本",
    "Fetching available versions...": "正在获取可用版本……",
    "No versions found.": "没有找到可用版本。",
    "Download Failed": "下载失败",
    "No engine versions installed.\nClick 'Install Editor' or 'Locate' to add one.": "尚未安装引擎版本。\n点击“安装引擎”或“导入本地版本”添加。",
    "Python Installed": "Python 已安装",
    "Python Installation Failed": "Python 安装失败",
    "Select Infernux Wheel": "选择 Infernux Wheel",
    "Version Installed": "版本已安装",
    "Infernux {version} has been installed from the selected wheel.": "已从所选 wheel 安装 Infernux {version}。",
    "Invalid Wheel": "无效的 Wheel",
    "Remove Version": "移除版本",
    "This deletes the cached wheel. Projects using this version will need to reinstall it.": "这会删除缓存的 wheel；使用该版本的项目之后需要重新安装它。",
    "Infernux Hub Installer": "Infernux Hub 安装程序",
    "Install Infernux Hub": "安装 Infernux Hub",
    "This installer copies Infernux Hub and its isolated Python {version} runtime onto your machine. It does not install, upgrade, remove, register, or modify any Python already on your system.": "安装程序只会复制 Infernux Hub 及其隔离的 Python {version} 运行环境，不会安装、升级、删除、注册或修改电脑上已有的任何 Python。",
    "Install location": "安装位置",
    "Ready to install.": "已准备安装。",
    "Launch Hub": "启动 Hub",
    "Select installation directory": "选择安装目录",
    "Uninstall Failed": "卸载失败",
    "Remove Hub application files after this window closes?\n{path}\n\nProjects and Shared resources (plugins, SDKs, runtimes and engines) are preserved.": "关闭此窗口后移除 Hub 程序文件？\n{path}\n\n项目及 Shared 中的插件、SDK、Python 运行时和引擎版本都会保留。",
    "Missing Directory": "未选择目录",
    "Please select an installation directory.": "请选择安装目录。",
    "Unsafe Install Location": "不安全的安装位置",
    "Directory Not Empty": "目录不为空",
    "Starting installation...": "正在开始安装……",
    "Copying Infernux Hub files...": "正在复制 Infernux Hub 文件……",
    "Deploying private Python {version} runtime...": "正在部署专用 Python {version} 运行环境……",
    "Registering Infernux Hub...": "正在注册 Infernux Hub……",
    "Installation completed successfully. Installed to: {path}": "安装成功。安装位置：{path}",
    "Installation failed.": "安装失败。",
    "Installation Failed": "安装失败",
    "Launch Failed": "启动失败",
    "Hub executable not found: {path}": "未找到 Hub 可执行文件：{path}",
    "Uninstall Infernux Hub": "卸载 Infernux Hub",
    "Registry entries and shortcuts have been removed.\n\nDo you also want to delete the installation folder?\n{path}": "注册信息和快捷方式已移除。\n\n是否同时删除安装目录？\n{path}",
    "Install Folder Preserved": "已保留安装目录",
    "The installation folder was not deleted because it is not marked as a safe Infernux Hub install directory.\n\nYour projects and downloaded engine versions are preserved. Remove application files manually only if you are sure this folder does not contain user data.": "该目录未被标记为安全的 Infernux Hub 安装目录，因此没有删除。\n\n项目和下载的引擎版本均已保留。仅在确认目录不含用户数据后手动删除应用文件。",
    "Uninstall Complete": "卸载完成",
    "Infernux Hub has been uninstalled.": "Infernux Hub 已卸载。",
    "Missing Name": "缺少项目名称",
    "Please enter a project name.": "请输入项目名称。",
    "Missing Location": "缺少项目位置",
    "Please choose a project location.": "请选择项目位置。",
    "Missing Version": "缺少引擎版本",
    "Please select an installed engine version.": "请选择一个已安装的引擎版本。",
    "Project Creation Failed": "项目创建失败",
    "Project Created": "项目已创建",
    "Language": "语言",
    "System": "跟随系统",
    "Chinese": "中文",
    "English": "English",
    "Appearance": "外观",
    "About Infernux": "关于 Infernux",
    "Hub preferences, updates and project-independent information.": "Hub 偏好、更新和与项目无关的信息。",
    "Switch between the neutral dark and light Hub themes.": "在中性暗色和浅色 Hub 主题之间切换。",
    "About Infernux": "关于 Infernux",
    "Infernux is my personal game engine project, exploring a practical C++17/Vulkan runtime with a Python authoring workflow. Infernux Hub is the early desktop entry point for managing projects, runtimes and editor launches.": "Infernux 是我的个人游戏引擎项目，探索实用的 C++17/Vulkan 运行时与 Python 创作工作流。Infernux Hub 是用于管理项目、运行环境和编辑器启动的早期桌面入口。",
    "Hub version: {version}": "Hub 版本：{version}",
    "The official Infernux community for support, ideas, and project sharing.": "Infernux 官方社区，用于获取帮助、交流想法和分享项目。",
    "INFERNUX COMMUNITY": "INFERNUX 社区",
    "Join the Infernux community.": "加入 Infernux 社区。",
    "Ask questions, report bugs, discuss engine workflows, and share projects with other Infernux users.": "在这里提问、反馈问题、讨论引擎工作流，并与其他 Infernux 用户分享项目。",
    "Open Community": "打开社区",
    "Popular this week": "本周热门",
    "Refresh": "刷新",
    "Loading community topics...": "正在加载社区帖子……",
    "No public topics this week.": "本周暂无公开帖子。",
    "{replies} replies · {views} views · {likes} likes": "{replies} 条回复 · {views} 次浏览 · {likes} 个赞",
    "Open": "打开",
    "Community topics could not be loaded: {message}": "无法加载社区帖子：{message}",
    "System language is detected automatically on Windows.": "Windows 上会自动检测系统显示语言。",
    "Language changes apply immediately.": "语言更改会立即应用。",
    "Hub Update": "Hub 更新",
    "Check the Infernux release catalog for a Hub update.": "从 Infernux 发布目录检查 Hub 更新。",
    "Check for Updates": "检查更新",
    "Plugin Library": "插件库",
    "Clean Unused Packages": "清理未使用的包",
    "Plugin library cleanup is unavailable: {message}": "插件库清理不可用：{message}",
    "{count} packages · {size} · {path}": "{count} 个包 · {size} · {path}",
    "{count} unused packages can release {size}.": "可清理 {count} 个未使用的包，释放 {size}。",
    "Every downloaded package is still referenced by a Hub project.": "每个已下载的包仍被 Hub 中的项目引用。",
    "Delete {count} unreferenced plugin packages and release {size}?": "删除 {count} 个未引用的插件包并释放 {size}？",
    "Infernux Hub is up to date.": "Infernux Hub 已是最新版本。",
    "Hub Update Available": "Hub 有可用更新",
    "Infernux Hub {version} is available. Update now?\n\nHub will close, install the update, and restart automatically.": "Infernux Hub {version} 已发布，是否立即更新？\n\nHub 将关闭、安装更新并自动重启。",
    "Update Check Unavailable": "暂时无法检查更新",
    "The Hub update catalog could not be reached.\n\n{message}": "无法连接 Hub 更新目录。\n\n{message}",
    "Update Catalog Invalid": "更新目录无效",
    "The Hub update catalog is invalid.\n\n{message}": "Hub 更新目录无效。\n\n{message}",
    "Full Hub Install Required": "需要完整安装 Hub",
    "This Hub is too old for the current in-app update path. Open the {version} installer download now?": "当前 Hub 版本过旧，无法使用应用内更新。是否打开 {version} 安装包下载？",
    "Updating Infernux Hub": "正在更新 Infernux Hub",
    "INSTALLING HUB UPDATE {version}": "正在安装 HUB 更新 {version}",
    "Downloading the Hub update...": "正在下载 Hub 更新……",
    "Closing Hub and installing the update...": "正在关闭 Hub 并安装更新……",
    "Update failed": "更新失败",
    "Hub Update Failed": "Hub 更新失败",
    "Update Check Failed": "检查更新失败",
    "Restart Required": "需要重启",
    "The language preference was saved. Restart Infernux Hub to apply it everywhere.": "语言偏好已保存。请重启 Infernux Hub 以完整应用。",
    "Current language": "当前语言",
    "Initializing engine...": "正在初始化引擎……",
    "Checking project...": "正在检查项目……",
    "Starting engine process...": "正在启动引擎进程……",
    "Waiting for the editor...": "正在等待编辑器……",
    "Ready": "已就绪",
    "Unversioned": "未绑定版本",
    "Missing": "缺失",
    "Launch failed": "启动失败",
    "Engine Launch Failed": "引擎启动失败",
    "Engine Launch Timed Out": "引擎启动超时",
    "The editor did not become ready within {seconds} seconds.": "编辑器未能在 {seconds} 秒内完成启动。",
    "Retry": "重试",
    "Open Logs": "打开日志",
    "Stop": "停止",
    "Keep Waiting": "继续等待",
    "Migrate Project": "升级项目",
    "Select target engine version:": "选择目标引擎版本：",
    "No Other Version": "没有其他版本",
    "Install another engine version before migrating this project.": "请先安装另一个引擎版本，再升级此项目。",
    "Confirm Project Migration": "确认项目升级",
    "A backup of Assets and ProjectSettings will be created before the runtime and version pin are changed.": "更改运行环境和版本锁定前，将备份 Assets 与 ProjectSettings。",
    "Project Migration Failed": "项目升级失败",
    "Project Migration Complete": "项目升级完成",
    "Backup created at:\n{path}": "备份已创建：\n{path}",
}

_mode = "system"
_language = "en"


def detect_system_locale() -> str:
    """Return the user's UI locale, preferring the Windows display language."""
    if sys.platform == "win32":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(85)
            language_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if language_id and ctypes.windll.kernel32.LCIDToLocaleName(
                language_id, buffer, len(buffer), 0,
            ):
                if buffer.value:
                    return buffer.value
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
                if buffer.value:
                    return buffer.value
        except (AttributeError, OSError):
            pass
    value = locale.getlocale()[0]
    return value or "en"


def resolve_language(mode: str = "system", *, system_locale: str | None = None) -> str:
    normalized_mode = str(mode or "system").strip().lower().replace("_", "-")
    if normalized_mode in {"zh", "zh-cn", "chinese"}:
        return "zh"
    if normalized_mode in {"en", "en-us", "english"}:
        return "en"
    detected = (system_locale or detect_system_locale()).lower().replace("_", "-")
    return "zh" if detected.startswith("zh") else "en"


def configure_language(mode: str = "system") -> str:
    global _mode, _language
    _mode = mode if mode in {"system", "zh", "en"} else "system"
    _language = resolve_language(_mode)
    return _language


def current_language() -> str:
    return _language


def language_mode() -> str:
    return _mode


def tr(text: str, **values) -> str:
    template = _ZH.get(text, text) if _language == "zh" else text
    return template.format(**values) if values else template


configure_language()


__all__ = [
    "configure_language",
    "current_language",
    "detect_system_locale",
    "language_mode",
    "resolve_language",
    "tr",
]
