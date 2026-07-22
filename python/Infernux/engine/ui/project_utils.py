"""
Pure utility functions and constants for the Project panel.

These have no dependency on ``ProjectPanel`` instance state.
"""

import os
from Infernux.debug import Debug

from Infernux.engine.ide_preference import get_ide
from Infernux.engine.path_utils import resolved_path

# File extensions to hide
HIDDEN_EXTENSIONS = {'.meta', '.pyc', '.pyo', '.tmp'}
HIDDEN_PREFIXES = {'.', '__'}
HIDDEN_FILES = {'imgui.ini'}

# IDE support
SUPPORTED_IDES = ("vscode", "pycharm")
_AVAILABLE_IDES_CACHE: dict[str, bool] | None = None

def should_show(name: str) -> bool:
    """Check if a file or folder should be shown (filters hidden files)."""
    if name in HIDDEN_FILES:
        return False
    for prefix in HIDDEN_PREFIXES:
        if name.startswith(prefix):
            return False
    _, ext = os.path.splitext(name)
    if ext.lower() in HIDDEN_EXTENSIONS:
        return False
    return True


def detect_available_ides(force_refresh: bool = False) -> list[str]:
    """Return installed/available IDEs, cached for repeated checks."""
    global _AVAILABLE_IDES_CACHE

    if _AVAILABLE_IDES_CACHE is None or force_refresh:
        _AVAILABLE_IDES_CACHE = {
            "vscode": _find_vscode_executable() is not None,
            "pycharm": _find_pycharm_executable() is not None,
        }

    return [ide for ide in SUPPORTED_IDES if _AVAILABLE_IDES_CACHE.get(ide, False)]


def is_ide_available(ide: str, force_refresh: bool = False) -> bool:
    """Return whether a supported IDE is available."""
    if ide not in SUPPORTED_IDES:
        return False
    return ide in detect_available_ides(force_refresh=force_refresh)


def _find_vscode_executable() -> str | None:
    """Locate the VS Code CLI executable on the current platform.

    On Windows ``code.cmd`` is often *not* on the system PATH even when
    VS Code is installed. This helper checks (in order):

    1. ``shutil.which('code')`` — works if the user ticked "Add to PATH"
       during install.
    2. Common installation directories (User & System installs).
    3. The Windows Registry ``App Paths`` key that VS Code registers.

    Returns the full path to the executable/script, or *None* if VS Code
    cannot be found.
    """
    import shutil
    import platform

    # Fast path: already on PATH
    found = shutil.which('code') or shutil.which('code.cmd') or shutil.which('code.exe')
    if found:
        return found

    if platform.system() != 'Windows':
        return None  # macOS/Linux typically have `code` symlinked

    # --- Windows-specific search -------------------------------------------

    # Common install locations (User install → System install)
    candidates = []
    local = os.environ.get('LOCALAPPDATA', '')
    if local:
        candidates.append(os.path.join(local, 'Programs', 'Microsoft VS Code', 'bin', 'code.cmd'))
        candidates.append(os.path.join(local, 'Programs', 'Microsoft VS Code', 'Code.exe'))
    program_files = os.environ.get('ProgramFiles', r'C:\Program Files')
    candidates.append(os.path.join(program_files, 'Microsoft VS Code', 'bin', 'code.cmd'))
    candidates.append(os.path.join(program_files, 'Microsoft VS Code', 'Code.exe'))
    program_files_x86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    candidates.append(os.path.join(program_files_x86, 'Microsoft VS Code', 'bin', 'code.cmd'))
    candidates.append(os.path.join(program_files_x86, 'Microsoft VS Code', 'Code.exe'))

    for path in candidates:
        if os.path.isfile(path):
            return path

    # Last resort: check Windows Registry (App Paths)
    import winreg
    registry_roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    registry_paths = (
        r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\code.exe',
        r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{EA457B21-F73E-494C-ACAB-524FDE069978}_is1',
    )
    for root in registry_roots:
        for key_path in registry_paths:
            try:
                key = winreg.OpenKey(root, key_path)
            except OSError as _exc:
                # Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                continue
            try:
                exe_path, _ = winreg.QueryValueEx(key, '')
            except OSError:
                try:
                    install_location, _ = winreg.QueryValueEx(key, 'InstallLocation')
                    exe_path = os.path.join(install_location, 'Code.exe')
                except OSError:
                    exe_path = None
            finally:
                winreg.CloseKey(key)
            if exe_path and os.path.isfile(exe_path):
                return exe_path

    return None


def open_in_vscode(file_path: str, line: int = 0, project_root: str = "") -> bool:
    """Open a file in VS Code and optionally jump to a line.

    Returns ``True`` when a VS Code launch was attempted successfully.
    """
    import platform
    import subprocess

    if not file_path:
        return False

    file_path = resolved_path(file_path)
    if not os.path.isfile(file_path):
        return False

    code_exe = _find_vscode_executable()
    if not code_exe:
        return False

    target = f"{file_path}:{max(int(line), 1)}" if line and int(line) > 0 else file_path

    cmd = []
    if project_root:
        project_root = resolved_path(project_root)
        if os.path.isdir(project_root):
            cmd.append(project_root)
    cmd.extend(['--goto', target])

    try:
        if platform.system() == 'Windows' and code_exe.lower().endswith('.cmd'):
            subprocess.Popen(
                ['cmd.exe', '/c', code_exe, *cmd],
                shell=False,
                creationflags=0x08000000,
            )
        else:
            subprocess.Popen(
                [code_exe, *cmd],
                shell=False,
                creationflags=(0x08000000 if platform.system() == 'Windows' else 0),
            )
        return True
    except (OSError, subprocess.SubprocessError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return False


def _is_executable_file(path: str | None) -> bool:
    """Return True if path exists, is a file, and is executable."""
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _find_pycharm_from_launchers(launcher_names: tuple[str, ...]) -> str | None:
    """Locate PyCharm from launcher names available on PATH."""
    import shutil

    for name in launcher_names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _find_pycharm_on_macos() -> str | None:
    """Locate PyCharm on macOS from common app bundle paths."""
    mac_candidates = (
        '/Applications/PyCharm.app/Contents/MacOS/pycharm',
        '/Applications/PyCharm CE.app/Contents/MacOS/pycharm',
    )
    for path in mac_candidates:
        if _is_executable_file(path):
            return path
    return None


def _find_pycharm_linux_snap() -> str | None:
    """Locate PyCharm from common Snap launcher paths."""
    snap_candidates = (
        '/snap/bin/pycharm-professional',
        '/snap/bin/pycharm-community',
        '/snap/bin/pycharm',
    )
    for path in snap_candidates:
        if _is_executable_file(path):
            return path
    return None


def _find_pycharm_linux_flatpak() -> str | None:
    """Locate PyCharm from common Flatpak exported launcher paths."""
    home = os.path.expanduser('~')
    app_ids = (
        'com.jetbrains.PyCharm-Professional',
        'com.jetbrains.PyCharm-Community',
    )

    export_roots = (
        os.path.join(home, '.local', 'share', 'flatpak', 'exports', 'bin'),
        '/var/lib/flatpak/exports/bin',
    )

    for root in export_roots:
        for app_id in app_ids:
            path = os.path.join(root, app_id)
            if _is_executable_file(path):
                return path

    return None


def _find_pycharm_linux_toolbox() -> str | None:
    """Locate PyCharm installed by JetBrains Toolbox on Linux."""
    home = os.path.expanduser('~')

    toolbox_script_candidates = (
        os.path.join(home, '.local', 'share', 'JetBrains', 'Toolbox', 'scripts', 'pycharm'),
        os.path.join(home, '.local', 'share', 'JetBrains', 'Toolbox', 'scripts', 'pycharm.sh'),
    )
    for path in toolbox_script_candidates:
        if _is_executable_file(path):
            return path

    toolbox_roots = (
        os.path.join(home, '.local', 'share', 'JetBrains', 'Toolbox', 'apps', 'PyCharm-P'),
        os.path.join(home, '.local', 'share', 'JetBrains', 'Toolbox', 'apps', 'PyCharm-C'),
        os.path.join(home, '.local', 'share', 'JetBrains', 'Toolbox', 'apps', 'PyCharm Professional'),
        os.path.join(home, '.local', 'share', 'JetBrains', 'Toolbox', 'apps', 'PyCharm Community'),
        os.path.join(home, '.local', 'share', 'JetBrains', 'Toolbox', 'apps', 'PyCharm'),
    )

    for root in toolbox_roots:
        if not os.path.isdir(root):
            continue

        try:
            channels = os.listdir(root)
        except OSError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            continue

        for channel in channels:
            channel_dir = os.path.join(root, channel)
            if not os.path.isdir(channel_dir):
                continue

            try:
                builds = sorted(os.listdir(channel_dir), reverse=True)
            except OSError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                continue

            for build in builds:
                build_dir = os.path.join(channel_dir, build)
                if not os.path.isdir(build_dir):
                    continue

                candidates = (
                    os.path.join(build_dir, 'bin', 'pycharm.sh'),
                    os.path.join(build_dir, 'bin', 'pycharm'),
                )
                for path in candidates:
                    if _is_executable_file(path):
                        return path

    return None


def _find_pycharm_on_linux() -> str | None:
    """Locate PyCharm on Linux from PATH, Snap, Flatpak, and Toolbox."""
    launcher_names = (
        'pycharm',
        'pycharm-professional',
        'pycharm-community',
        'pycharm64',
        'charm',
    )

    found = _find_pycharm_from_launchers(launcher_names)
    if found:
        return found

    found = _find_pycharm_linux_snap()
    if found:
        return found

    found = _find_pycharm_linux_flatpak()
    if found:
        return found

    found = _find_pycharm_linux_toolbox()
    if found:
        return found

    return None


def _find_pycharm_in_jetbrains_root(root: str) -> str | None:
    """Locate PyCharm under a JetBrains installation root on Windows."""
    if not root or not os.path.isdir(root):
        return None

    try:
        entries = os.listdir(root)
    except OSError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None

    exe_names = (
        'pycharm64.exe',
        'pycharm.exe',
        'pycharm64.bat',
        'pycharm.bat',
    )

    for name in sorted(entries, reverse=True):
        if not name.lower().startswith('pycharm'):
            continue

        install_dir = os.path.join(root, name)
        if not os.path.isdir(install_dir):
            continue

        bin_dir = os.path.join(install_dir, 'bin')
        for exe_name in exe_names:
            exe_path = os.path.join(bin_dir, exe_name)
            if os.path.isfile(exe_path):
                return exe_path

    return None


def _find_pycharm_in_windows_toolbox(local: str) -> str | None:
    """Locate PyCharm from JetBrains Toolbox scripts or app installs on Windows."""
    if not local:
        return None

    toolbox_script_candidates = (
        os.path.join(local, 'JetBrains', 'Toolbox', 'scripts', 'pycharm.cmd'),
        os.path.join(local, 'JetBrains', 'Toolbox', 'scripts', 'pycharm.bat'),
        os.path.join(local, 'JetBrains', 'Toolbox', 'scripts', 'pycharm'),
    )
    for path in toolbox_script_candidates:
        if os.path.isfile(path):
            return path

    toolbox_apps = os.path.join(local, 'JetBrains', 'Toolbox', 'apps', 'PyCharm')
    if not os.path.isdir(toolbox_apps):
        return None

    exe_names = ('pycharm64.exe', 'pycharm.exe')
    try:
        for edition in os.listdir(toolbox_apps):
            edition_dir = os.path.join(toolbox_apps, edition)
            if not os.path.isdir(edition_dir):
                continue

            for channel in os.listdir(edition_dir):
                channel_dir = os.path.join(edition_dir, channel)
                if not os.path.isdir(channel_dir):
                    continue

                for build in sorted(os.listdir(channel_dir), reverse=True):
                    build_dir = os.path.join(channel_dir, build)
                    if not os.path.isdir(build_dir):
                        continue

                    bin_dir = os.path.join(build_dir, 'bin')
                    for exe_name in exe_names:
                        exe_path = os.path.join(bin_dir, exe_name)
                        if os.path.isfile(exe_path):
                            return exe_path
    except OSError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")

    return None


def _find_pycharm_from_windows_registry() -> str | None:
    """Locate PyCharm from Windows Registry uninstall entries."""
    try:
        import winreg
    except ImportError:
        return None

    registry_roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    uninstall_key = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'

    for root in registry_roots:
        try:
            base_key = winreg.OpenKey(root, uninstall_key)
        except OSError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            continue

        try:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(base_key, i)
                    i += 1
                except OSError:
                    break

                full_key_path = uninstall_key + '\\' + subkey_name
                try:
                    subkey = winreg.OpenKey(root, full_key_path)
                except OSError as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    continue

                try:
                    try:
                        display_name, _ = winreg.QueryValueEx(subkey, 'DisplayName')
                    except OSError:
                        display_name = ''

                    if 'pycharm' not in str(display_name).lower():
                        continue

                    try:
                        install_location, _ = winreg.QueryValueEx(subkey, 'InstallLocation')
                    except OSError:
                        install_location = None

                    if install_location and os.path.isdir(install_location):
                        for exe_name in ('pycharm64.exe', 'pycharm.exe'):
                            exe_path = os.path.join(install_location, 'bin', exe_name)
                            if os.path.isfile(exe_path):
                                return exe_path

                    try:
                        display_icon, _ = winreg.QueryValueEx(subkey, 'DisplayIcon')
                    except OSError:
                        display_icon = None

                    if display_icon:
                        exe_path = str(display_icon).strip('"')
                        if os.path.isfile(exe_path):
                            return exe_path
                finally:
                    winreg.CloseKey(subkey)
        finally:
            winreg.CloseKey(base_key)

    return None


def _find_pycharm_on_windows() -> str | None:
    """Locate PyCharm on Windows from PATH, install roots, Toolbox, and registry."""
    launcher_names = (
        'pycharm64.exe',
        'pycharm.exe',
        'pycharm64',
        'pycharm',
    )

    found = _find_pycharm_from_launchers(launcher_names)
    if found:
        return found

    program_files = os.environ.get('ProgramFiles', r'C:\Program Files')
    program_files_x86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    local = os.environ.get('LOCALAPPDATA', '')

    search_roots = [
        os.path.join(program_files, 'JetBrains'),
        os.path.join(program_files_x86, 'JetBrains'),
    ]
    if local:
        search_roots.append(os.path.join(local, 'Programs', 'JetBrains'))

    for root in search_roots:
        found = _find_pycharm_in_jetbrains_root(root)
        if found:
            return found

    found = _find_pycharm_in_windows_toolbox(local)
    if found:
        return found

    found = _find_pycharm_from_windows_registry()
    if found:
        return found

    return None


def _find_pycharm_executable() -> str | None:
    """Locate a PyCharm executable on the current platform."""
    import platform

    system = platform.system()

    if system == 'Darwin':
        return _find_pycharm_on_macos()

    if system == 'Linux':
        return _find_pycharm_on_linux()

    if system == 'Windows':
        return _find_pycharm_on_windows()

    return None



def _ensure_pycharm_project_files(project_root: str) -> bool:
    """Create a minimal PyCharm project structure and a bilingual setup guide.

    The generated project:

    - marks ``Assets`` as source root
    - excludes ``Library``, ``Logs``, ``.vscode``, and ``.runtime``
    - creates ``PYCHARM_SETUP.zh-CN.en.md`` in project root to guide interpreter setup

    Returns ``True`` if the files already exist or were written successfully.
    Returns ``False`` on failure.
    """
    if not project_root:
        return False

    project_root = resolved_path(project_root)

    # Accept either a project directory or a file path inside the project.
    if os.path.isfile(project_root):
        project_root = os.path.dirname(project_root)

    if not os.path.isdir(project_root):
        return False

    idea_dir = os.path.join(project_root, '.idea')
    project_name = os.path.basename(resolved_path(project_root)) or 'Project'
    module_name = 'project'
    module_rel_path = f'.idea/{module_name}.iml'
    setup_guide_path = os.path.join(project_root, 'PYCHARM_SETUP.zh-CN.en.md')
    runtime_python = os.path.join(project_root, '.runtime', 'python312', 'python.exe')

    try:
        os.makedirs(idea_dir, exist_ok=True)
    except OSError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return False

    def _write_if_changed(path: str, content: str) -> None:
        old = None
        try:
            if os.path.isfile(path):
                with open(path, 'r', encoding='utf-8') as f:
                    old = f.read()
        except OSError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")

        if old == content:
            return

        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)

    source_entries = []
    exclude_entries = []

    if os.path.isdir(os.path.join(project_root, 'Assets')):
        source_entries.append(
            '      <sourceFolder url="file://$MODULE_DIR$/Assets" isTestSource="false" />'
        )

    for name in ('Library', 'Logs', '.vscode', '.runtime'):
        if os.path.isdir(os.path.join(project_root, name)):
            exclude_entries.append(
                f'      <excludeFolder url="file://$MODULE_DIR$/{name}" />'
            )

    iml_xml = '\n'.join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<module type="PYTHON_MODULE" version="4">',
        '  <component name="NewModuleRootManager">',
        '    <content url="file://$MODULE_DIR$">',
        *source_entries,
        *exclude_entries,
        '    </content>',
        '    <orderEntry type="inheritedJdk" />',
        '    <orderEntry type="sourceFolder" forTests="false" />',
        '  </component>',
        '</module>',
        '',
    ])

    modules_xml = '\n'.join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<project version="4">',
        '  <component name="ProjectModuleManager">',
        '    <modules>',
        f'      <module fileurl="file://$PROJECT_DIR$/{module_rel_path}" filepath="$PROJECT_DIR$/{module_rel_path}" />',
        '    </modules>',
        '  </component>',
        '</project>',
        '',
    ])

    misc_xml = '\n'.join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<project version="4">',
        '  <component name="ProjectRootManager" version="2" />',
        '</project>',
        '',
    ])

    idea_gitignore = '\n'.join([
        '# PyCharm local state',
        'workspace.xml',
        'tasks.xml',
        'usage.statistics.xml',
        'shelf/',
        '',
    ])

    setup_md = '\n'.join([
    '# PyCharm 环境配置 / PyCharm Environment Setup',
    '',
    f'项目 / Project: `{project_name}`',
    '',
    '---',
    '',
    '## 中文说明',
    '',
    '本项目使用随项目提供的 Python 运行环境：',
    '',
    '```text',
    '.runtime/python312/python.exe',
    '```',
    '',
    '如果 PyCharm 打开项目后没有可用的 Python SDK，请按以下步骤手动配置：',
    '',
    f'1. 打开 **设置** → **项目: {project_name}** → **Python 解释器**',
    '2. 点击右上角的 **齿轮图标** → **添加解释器...**',
    '3. 选择 **添加本地解释器**',
    '4. 选择 **现有**',
    '5. 在解释器类型中选择 **Virtualenv 环境**',
    '6. 选择下面这个解释器文件：',
    '',
    '```text',
    runtime_python,
    '```',
    '',
    '配置完成后，PyCharm 就会使用与引擎一致的 Python 环境。',
    '',
    '补充说明：',
    '',
    '- `Assets/` 是主要代码目录',
    '- `.runtime/` 会被排除，不参与索引',
    '- `.vscode/`、`Library/` 和 `Logs/` 也会被排除',
    '',
    '---',
    '',
    '## English',
    '',
    'This project uses the bundled Python runtime located at:',
    '',
    '```text',
    '.runtime/python312/python.exe',
    '```',
    '',
    'If PyCharm opens the project without a valid Python SDK, configure it manually:',
    '',
    f'1. Open **Settings** → **Project: {project_name}** → **Python Interpreter**',
    '2. Click the **gear icon** → **Add Interpreter...**',
    '3. Choose **Add Local Interpreter**',
    '4. Choose **Existing**',
    '5. Select **Virtualenv Environment** as the interpreter type',
    '6. Select this interpreter executable:',
    '',
    '```text',
    runtime_python,
    '```',
    '',
    'After that, PyCharm should use the same Python environment as the engine.',
    '',
    'Notes:',
    '',
    '- `Assets/` is the main source root',
    '- `.runtime/` is intentionally excluded from indexing',
    '- `.vscode/`, `Library/`, and `Logs/` are also excluded',
    '',
])

    try:
        _write_if_changed(os.path.join(idea_dir, 'modules.xml'), modules_xml)
        _write_if_changed(os.path.join(idea_dir, 'misc.xml'), misc_xml)
        _write_if_changed(os.path.join(idea_dir, f'{module_name}.iml'), iml_xml)
        _write_if_changed(os.path.join(idea_dir, '.gitignore'), idea_gitignore)
        _write_if_changed(setup_guide_path, setup_md)

        if not os.path.isfile(runtime_python):
            Debug.log(f"[Suppressed] Bundled runtime not found: {runtime_python}")

        return True
    except OSError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return False


def open_in_pycharm(file_path: str, line: int = 0, project_root: str = "") -> bool:
    """Open a file in PyCharm and also surface the setup guide.

    If *project_root* is provided, a minimal PyCharm project structure is
    created first. A bilingual setup guide file is also created and opened
    so users can configure the interpreter when needed.

    Returns ``True`` when a PyCharm launch was attempted successfully.
    """
    import platform
    import subprocess

    if not file_path:
        return False

    file_path = resolved_path(file_path)
    if not os.path.isfile(file_path):
        return False

    pycharm_exe = _find_pycharm_executable()
    if not pycharm_exe:
        return False

    project_root = resolved_path(project_root) if project_root else ""
    if project_root:
        if os.path.isfile(project_root):
            project_root = os.path.dirname(project_root)
        elif not os.path.isdir(project_root):
            project_root = ""

    
    project_initialized = False
    if project_root:
        idea_dir = os.path.join(project_root, '.idea')
        setup_guide_path = os.path.join(project_root, 'PYCHARM_SETUP.zh-CN.en.md')
        module_file = os.path.join(idea_dir, 'project.iml')
        modules_file = os.path.join(idea_dir, 'modules.xml')
        misc_file = os.path.join(idea_dir, 'misc.xml')

        project_initialized = (
            os.path.isfile(setup_guide_path) and
            os.path.isfile(module_file) and
            os.path.isfile(modules_file) and
            os.path.isfile(misc_file)
        )

        if not project_initialized:
            if not _ensure_pycharm_project_files(project_root):
                Debug.log("[Suppressed] Failed to prepare PyCharm project files")

    setup_guide_path = os.path.join(project_root, 'PYCHARM_SETUP.zh-CN.en.md') if project_root else ""

    try:
        creationflags = 0x08000000 if platform.system() == 'Windows' else 0

        cmd = [pycharm_exe]

        # Open the project first so subsequent files are attached to the correct project.
        if project_root:
            cmd.append(project_root)

        # Then open the setup guide so the user sees the interpreter instructions.
        # open it first-time initialization only
        if setup_guide_path and os.path.isfile(setup_guide_path) and not project_initialized:
            cmd.append(setup_guide_path)

        # Finally open the target file.
        cmd.append(file_path)

        if line and int(line) > 0:
            cmd.extend(['--line', str(max(int(line), 1))])

        subprocess.Popen(
            cmd,
            shell=False,
            creationflags=creationflags,
        )
        return True

    except (OSError, subprocess.SubprocessError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return False



def open_file_with_system(file_path: str, project_root: str = ""):
    """
    Open *file_path* with the OS default application.

    For ``.py``, ``.vert``, ``.frag``, ``.glsl``, ``.hlsl``, ``.json``,
    ``.txt``, and ``.md`` files, open in VS Code with the *project_root*
    as the workspace folder — so that the project's Python runtime
    interpreter and type stubs are automatically picked up by Pylance.
    """
    import subprocess
    import platform

    CODE_EXTENSIONS = {
        '.py', '.vert', '.frag', '.glsl', '.hlsl',
        '.json', '.txt', '.md', '.yaml', '.yml', '.xml',
        '.lua', '.cs', '.cpp', '.c', '.h',
    }

    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    # For code files, try IDEs first.
    # If the preferred IDE is available, try it first and then the other IDE.
    # If the preferred IDE is unavailable, fall back to the current default order:
    # VS Code -> PyCharm.
    if ext in CODE_EXTENSIONS and project_root:
        preferred_ide = get_ide()
        # Refresh here instead of trusting the startup cache. IDEs can be
        # installed, updated, or selected while the editor is already running.
        available_ides = detect_available_ides(force_refresh=True)

        ide_order = ["vscode", "pycharm"]
        if preferred_ide in available_ides:
            ide_order = [preferred_ide] + [ide for ide in ide_order if ide != preferred_ide]

        for ide in ide_order:
            if ide not in available_ides:
                continue

            if ide == "vscode":
                if open_in_vscode(file_path, project_root=project_root):
                    return
                Debug.log("[ProjectPanel] VS Code launch failed, trying next IDE")

            elif ide == "pycharm":
                if open_in_pycharm(file_path, project_root=project_root):
                    return
                Debug.log("[ProjectPanel] PyCharm launch failed, trying next IDE")

    # Fallback: open with OS default application
    system = platform.system()
    if system == 'Windows':
        os.startfile(file_path)
    elif system == 'Darwin':
        subprocess.run(['open', file_path], check=True)
    else:
        subprocess.run(['xdg-open', file_path], check=True)


def get_file_type(filename: str) -> str:
    """Return a short type tag string (e.g. ``[PY]``, ``[IMG]``) based on extension."""
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    types = {
        '.png': '[IMG]', '.jpg': '[IMG]', '.jpeg': '[IMG]', '.bmp': '[IMG]',
        '.tga': '[IMG]', '.gif': '[IMG]', '.psd': '[IMG]', '.hdr': '[IMG]',
        '.pic': '[IMG]', '.pnm': '[IMG]', '.pgm': '[IMG]', '.ppm': '[IMG]',
        '.py': '[PY]', '.lua': '[LUA]', '.cs': '[CS]', '.cpp': '[CPP]',
        '.h': '[H]', '.c': '[C]',
        '.vert': '[VERT]', '.frag': '[FRAG]', '.glsl': '[GLSL]', '.hlsl': '[HLSL]',
        '.mat': '[MAT]',
        '.fbx': '[3D]', '.obj': '[3D]', '.gltf': '[3D]', '.glb': '[3D]',
        '.wav': '[SND]',
        '.json': '[JSON]', '.yaml': '[CFG]', '.yml': '[CFG]', '.xml': '[XML]',
        '.txt': '[TXT]', '.md': '[MD]',
        '.ttf': '[FNT]', '.otf': '[FNT]',
    }
    return types.get(ext, '[FILE]')


def update_material_name_in_file(mat_path: str, new_name: str):
    """Rewrite the ``"name"`` key in a ``.mat`` JSON file."""
    import json
    with open(mat_path, 'r', encoding='utf-8') as f:
        mat_data = json.load(f)
    mat_data['name'] = new_name
    from Infernux.core.document_store import write_document_text
    write_document_text(mat_path, json.dumps(mat_data, indent=2) + '\n')


def reveal_in_file_explorer(path: str):
    """Open the system file explorer and highlight *path*.

    On Windows uses ``explorer /select,<path>``.
    On macOS uses ``open -R``.
    On Linux falls back to ``xdg-open`` on the parent directory.
    """
    import platform
    import subprocess

    path = resolved_path(path)
    system = platform.system()
    if system == 'Windows':
        # /select highlights the file/folder in Explorer
        subprocess.Popen(['explorer', '/select,', path])
    elif system == 'Darwin':
        subprocess.Popen(['open', '-R', path])
    else:
        # xdg-open on parent dir
        parent = os.path.dirname(path) if os.path.isfile(path) else path
        subprocess.Popen(['xdg-open', parent])
