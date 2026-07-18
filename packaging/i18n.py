"""Small runtime translation layer for the early Infernux Hub."""

from __future__ import annotations

import locale
import sys


_ZH = {
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
    "Python 3.12 is not installed yet. Go to the Installs tab to install it first.": "尚未安装 Python 3.12。请先前往“安装”页面进行安装。",
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
    "Locate": "导入本地版本",
    "Install Editor": "安装引擎",
    "Install Python 3.12": "安装 Python 3.12",
    "Reinstall Python 3.12": "修复 Python 3.12",
    "Python 3.12 runtime is ready": "Python 3.12 运行环境已就绪",
    "Python 3.12 runtime is missing": "Python 3.12 运行环境缺失",
    "Installed": "已安装",
    "Install": "安装",
    "Installing Python 3.12": "正在安装 Python 3.12",
    "Installing Python 3.12 for Infernux Hub": "正在为 Infernux Hub 安装 Python 3.12",
    "A background setup process is preparing a managed full Python 3.12 runtime under C:\\Users\\Public\\InfernuxHub. Each new project will receive its own copy of this runtime. This window will close automatically when installation finishes.": "后台正在 C:\\Users\\Public\\InfernuxHub 中准备托管的完整 Python 3.12 运行环境。每个新项目都会获得独立副本。安装完成后此窗口将自动关闭。",
    "Install Engine Version": "安装引擎版本",
    "Fetching available versions...": "正在获取可用版本……",
    "No versions found.": "没有找到可用版本。",
    "Download Failed": "下载失败",
    "No engine versions installed.\nClick 'Install Editor' or 'Locate' to add one.": "尚未安装引擎版本。\n点击“安装引擎”或“导入本地版本”添加。",
    "The managed Python runtime is not installed. Hub will download the matching Python 3.12 installer when you choose Install.": "托管 Python 运行环境尚未安装。选择安装后，Hub 将下载匹配的 Python 3.12 安装程序。",
    "Python Installed": "Python 已安装",
    "Python 3.12 is ready at:\n{path}": "Python 3.12 已就绪：\n{path}",
    "Python Installation Failed": "Python 安装失败",
    "Select Infernux Wheel": "选择 Infernux Wheel",
    "Version Installed": "版本已安装",
    "Infernux {version} has been installed from the selected wheel.": "已从所选 wheel 安装 Infernux {version}。",
    "Invalid Wheel": "无效的 Wheel",
    "Remove Version": "移除版本",
    "This deletes the cached wheel. Projects using this version will need to reinstall it.": "这会删除缓存的 wheel；使用该版本的项目之后需要重新安装它。",
    "Infernux Hub Installer": "Infernux Hub 安装程序",
    "Install Infernux Hub": "安装 Infernux Hub",
    "This installer copies Infernux Hub onto your machine. During setup, it will download and prepare a managed full Python 3.12 runtime under C:\\Users\\Public\\InfernuxHub for use by all projects.": "安装程序会将 Infernux Hub 复制到此电脑，并下载、准备位于 C:\\Users\\Public\\InfernuxHub 的托管完整 Python 3.12 运行环境，供所有项目使用。",
    "Install location": "安装位置",
    "Ready to install.": "已准备安装。",
    "Launch Hub": "启动 Hub",
    "Select installation directory": "选择安装目录",
    "Missing Directory": "未选择目录",
    "Please select an installation directory.": "请选择安装目录。",
    "Unsafe Install Location": "不安全的安装位置",
    "Directory Not Empty": "目录不为空",
    "Starting installation...": "正在开始安装……",
    "Copying Infernux Hub files...": "正在复制 Infernux Hub 文件……",
    "Installing private Python 3.12 runtime...": "正在安装专用 Python 3.12 运行环境……",
    "Registering Infernux Hub...": "正在注册 Infernux Hub……",
    "Installation completed successfully. Installed to: {path}": "安装成功。安装位置：{path}",
    "Installation failed.": "安装失败。",
    "Installation Failed": "安装失败",
    "Launch Failed": "启动失败",
    "Hub executable not found: {path}": "未找到 Hub 可执行文件：{path}",
    "Python 3.12 Setup": "配置 Python 3.12",
    "Infernux Hub needs Python 3.12 to create and launch projects.\n\nThe recommended path is to install Infernux Hub through the installer. The installer or standalone Hub will\ndownload the matching full Python 3.12 installer for this machine when needed and install it under\nC:\\Users\\Public\\InfernuxHub. Each project then receives its own full copy of the runtime.": "Infernux Hub 需要 Python 3.12 来创建和启动项目。\n\n安装程序或独立 Hub 会在需要时下载适合此电脑的完整 Python 3.12，并安装到 C:\\Users\\Public\\InfernuxHub。之后每个项目都会获得独立的运行环境副本。",
    "Python 3.12 Not Ready": "Python 3.12 尚未就绪",
    "Uninstall Infernux Hub": "卸载 Infernux Hub",
    "Registry entries and shortcuts have been removed.\n\nDo you also want to delete the installation folder?\n{path}": "注册信息和快捷方式已移除。\n\n是否同时删除安装目录？\n{path}",
    "Install Folder Preserved": "已保留安装目录",
    "The installation folder was not deleted because it is not marked as a safe Infernux Hub install directory.\n\nYour projects and downloaded engine versions are preserved. Remove application files manually only if you are sure this folder does not contain user data.": "该目录未被标记为安全的 Infernux Hub 安装目录，因此没有删除。\n\n项目和下载的引擎版本均已保留。仅在确认目录不含用户数据后手动删除应用文件。",
    "Uninstall Complete": "卸载完成",
    "Infernux Hub has been uninstalled.": "Infernux Hub 已卸载。",
    "Python 3.12 Required": "需要 Python 3.12",
    "Python 3.12 is not installed yet. Open the Installs page or restart the Hub and let it finish runtime setup first.": "尚未安装 Python 3.12。请打开“安装”页面，或重启 Hub 并等待运行环境配置完成。",
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
    "System language is detected automatically on Windows.": "Windows 上会自动检测系统显示语言。",
    "Language changes apply immediately.": "语言更改会立即应用。",
    "Hub Update": "Hub 更新",
    "Check GitHub Releases for a verified incremental Hub update.": "检查 GitHub Releases 中经过校验的 Hub 增量更新。",
    "Check for Updates": "检查更新",
    "Infernux Hub is up to date.": "Infernux Hub 已是最新版本。",
    "Hub Update Available": "Hub 有可用更新",
    "Infernux Hub {version} is available. Update now?\n\nHub will close, install the verified incremental update, and restart automatically.": "Infernux Hub {version} 已发布，是否立即更新？\n\nHub 将关闭，安装经过校验的增量更新，然后自动重启。",
    "Updating Infernux Hub": "正在更新 Infernux Hub",
    "INSTALLING HUB UPDATE {version}": "正在安装 HUB 更新 {version}",
    "Downloading and verifying update...": "正在下载并校验更新……",
    "Closing Hub and installing verified files...": "正在关闭 Hub 并安装已校验文件……",
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
