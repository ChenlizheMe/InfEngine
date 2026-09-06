# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Infernux Hub.

Build with:
    cd packaging
    pyinstaller infernux_hub.spec --clean
or via CMake:
    cmake --build --preset windows-hub
"""

import os
import sys

block_cipher = None

_PACKAGING_DIR = os.path.dirname(os.path.abspath(SPEC))


def collect_tree(src_root, dest_root):
    datas = []
    for root, _dirs, files in os.walk(src_root):
        rel_dir = os.path.relpath(root, src_root)
        target_dir = dest_root if rel_dir == "." else os.path.join(dest_root, rel_dir)
        for filename in files:
            datas.append((os.path.join(root, filename), target_dir))
    return datas


_RUNTIME_PAYLOAD = os.path.join(_PACKAGING_DIR, "runtime")
_RUNTIME_BUNDLE = os.path.join(_RUNTIME_PAYLOAD, "runtime_bundle.zip")
# Locate OpenSSL DLLs required by _ssl.pyd — PyInstaller often misses these in conda envs.
import sys as _sys
_env_lib_bin = os.path.join(os.path.dirname(_sys.executable), "Library", "bin")
_ssl_binaries = []
for _dll_name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll", "libssl-3.dll", "libcrypto-3.dll"):
    _p = os.path.join(_env_lib_bin, _dll_name)
    if os.path.isfile(_p):
        _ssl_binaries.append((_p, "."))
a = Analysis(
    [os.path.join(_PACKAGING_DIR, "launcher.py")],
    pathex=[_PACKAGING_DIR],
    binaries=[*_ssl_binaries],
    datas=[
        (os.path.join(_PACKAGING_DIR, "resources", "icon.png"), "resources"),
        (os.path.join(_PACKAGING_DIR, "resources", "PingFangSC-Regular.ttf"), "resources"),
        (_RUNTIME_BUNDLE, "InfernuxHubData/runtime"),
    ],
    hiddenimports=[
        "hub_resources",
        "hub_utils",
        "android_support",
        "installer_safety",
        "python_runtime",
        "version_manager",
        "ssl",
        "_ssl",
        "_hashlib",
        "database",
        "splash_screen",
        "style",
        "ui_project_list",
        "model",
        "model.project_model",
        "model.new_project_model",
        "view",
        "view.control_pane_view",
        "view.new_project_view",
        "view.sidebar_view",
        "view.installs_view",
        "viewmodel",
        "viewmodel.control_pane_viewmodel",
        "viewmodel.new_project_viewmodel",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(_PACKAGING_DIR, "pyi_rth_infernux_utf8.py")],
    excludes=[
        # Don't bundle the engine itself — it's installed per-project
        "Infernux",
        # Heavy dev packages that aren't needed
        "numpy",
        "watchdog",
        "PIL",
        "cv2",
        "matplotlib",
        "scipy",
        "pandas",
        "tkinter",
        "unittest",
        "test",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Infernux Hub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(_PACKAGING_DIR, "resources", "icon.png"),
    version=os.path.join(_PACKAGING_DIR, "windows_version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Infernux Hub",
)
