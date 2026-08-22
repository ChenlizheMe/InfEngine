import datetime
import os
import sys
import json
import subprocess
import shutil
import glob
import zipfile
import sysconfig
import uuid

from hub_utils import is_frozen, merge_child_env_utf8
from project_paths import inspect_existing_project, new_project_target
from python_runtime import PythonRuntimeError, PythonRuntimeManager
import logging

# Suppress console windows for all child processes on Windows
_NO_WINDOW: int = 0x08000000 if sys.platform == "win32" else 0

_COMPONENT_SCRIPT_NAMESPACE = uuid.UUID("594f85cc-9c3a-4ea9-93ed-65a26f77e3a4")
_COMPONENT_TYPE_NAMESPACE = uuid.UUID("41934666-ab60-4a29-b7ae-c8e15faf83c2")

_FALLBACK_PROJECT_GITIGNORE = """# Infernux generated state
/Library/
/Temp/
/Logs/
/Cache/
/.runtime/
/.venv/
/Build/
/Builds/
/Dist/
/Export/
/Exports/
/ProjectSettings/.infernux-engine-lock.json
__pycache__/
*.py[cod]
*.meta.tmp
*.tmp
*.bak
*.log
.vs/
.vscode/
.idea/
.DS_Store
Thumbs.db
desktop.ini
imgui.ini
"""

_FALLBACK_PROJECT_GITATTRIBUTES = """# Keep project text deterministic across platforms
* text=auto
*.py text eol=lf
*.scene text eol=lf
*.prefab text eol=lf
*.mat text eol=lf
*.effect text eol=lf
*.effectgroup text eol=lf
*.particlegraph text eol=lf
*.meta text eol=lf

# Binary assets must never receive line-ending conversion
*.png binary
*.jpg binary
*.jpeg binary
*.tga binary
*.dds binary
*.ktx binary
*.ktx2 binary
*.fbx binary
*.glb binary
*.wav binary
*.mp3 binary
*.ogg binary
"""


def _engine_component_type_id(module_name: str, qualified_name: str) -> str:
    """Return the stable scene identity used by engine-owned Python components."""
    script_guid = uuid.uuid5(_COMPONENT_SCRIPT_NAMESPACE, module_name).hex
    type_guid = uuid.uuid5(
        _COMPONENT_TYPE_NAMESPACE,
        f"{module_name}:{qualified_name}",
    ).hex
    return f"python:{script_guid}:{type_guid}:{module_name}:{qualified_name}"


def _write_json_document(path: str, document: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _default_scene_document() -> dict:
    render_stack_type = _engine_component_type_id(
        "Infernux.renderstack.render_stack",
        "RenderStack",
    )
    return {
        "isPlaying": False,
        "mainCameraComponentId": 2,
        "name": "Start",
        "objects": [
            {
                "active": True,
                "children": [],
                "components": [
                    {
                        "component_id": 2,
                        "data": {
                            "aspectRatio": 1.7777777910232544,
                            "backgroundColor": [0.1, 0.1, 0.1, 1.0],
                            "clearFlags": 0,
                            "cullingMask": 4294967295,
                            "depth": 0.0,
                            "farClip": 5000.0,
                            "fov": 60.0,
                            "nearClip": 0.01,
                            "orthoSize": 5.0,
                            "projectionMode": 0,
                        },
                        "enabled": True,
                        "execution_order": 0,
                        "type_id": "native:infernux.Camera",
                    }
                ],
                "id": 1,
                "is_static": False,
                "layer": 0,
                "name": "Main Camera",
                "tag": "MainCamera",
                "transform": {
                    "component_id": 1,
                    "enabled": True,
                    "execution_order": 0,
                    "position": [0.0, 1.0, -10.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                    "type": "Transform",
                },
            },
            {
                "active": True,
                "children": [],
                "components": [
                    {
                        "component_id": 4,
                        "data": {
                            "baked": False,
                            "areaSize": [1.6, 1.0],
                            "areaTwoSided": False,
                            "color": [1.0, 0.95, 0.9],
                            "cullingMask": 4294967295,
                            "influenceDomains": 3,
                            "intensity": 1.0,
                            "lightType": 0,
                            "outerSpotAngle": 45.0,
                            "range": 10.0,
                            "renderMode": 0,
                            "shadowBias": 1.0,
                            "shadowNormalBias": 1.0,
                            "shadowSoftness": 1.5,
                            "shadowStrength": 1.0,
                            "shadows": 2,
                            "spotAngle": 30.0,
                        },
                        "enabled": True,
                        "execution_order": 0,
                        "type_id": "native:infernux.Light",
                    }
                ],
                "id": 2,
                "is_static": False,
                "layer": 0,
                "name": "Directional Light",
                "tag": "Untagged",
                "transform": {
                    "component_id": 3,
                    "enabled": True,
                    "execution_order": 0,
                    "position": [0.0, 0.0, 0.0],
                    "rotation": [50.0, 330.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                    "type": "Transform",
                },
            },
            {
                "active": True,
                "children": [],
                "components": [
                    {
                        "component_id": 6,
                        "data": {
                            "effect_slots": [
                                {
                                    "$type": "serializable_object",
                                    "fields": {
                                        "effect": {
                                            "$type": "asset_ref",
                                            "asset_type": "RenderEffect",
                                            "guid": "",
                                            "path_hint": "Assets/Rendering/Default Post Processing.effectgroup",
                                        },
                                        "enabled": True,
                                        "slot_id": "default_post_processing",
                                        "stage_id": "final",
                                    },
                                    "type_id": "Infernux.renderstack.effect_slot:EffectSlot",
                                }
                            ],
                            "pipeline_class_name": "",
                            "pipeline_params_json": "",
                        },
                        "enabled": True,
                        "execution_order": 0,
                        "type_id": render_stack_type,
                    }
                ],
                "id": 3,
                "is_static": False,
                "layer": 0,
                "name": "RenderStack",
                "tag": "Untagged",
                "transform": {
                    "component_id": 5,
                    "enabled": True,
                    "execution_order": 0,
                    "position": [0.0, 0.0, 0.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                    "type": "Transform",
                },
            },
        ],
    }


def _create_default_project_content(
    staging_dir: str,
    final_dir: str,
    project_name: str,
) -> None:
    assets_dir = os.path.join(staging_dir, "Assets")
    for folder in (
        "Scenes",
        "Rendering",
        "Materials",
        "Scripts",
        "Textures",
        "Models",
        "Audio",
    ):
        os.makedirs(os.path.join(assets_dir, folder), exist_ok=True)

    bloom = {
        "$schema": "infernux.render_effect",
        "dependencies": [],
        "feature_type": "infernux.post.bloom",
        "parameters": {
            "clamp": 65472.0,
            "intensity": 0.8,
            "max_iterations": 5,
            "scatter": 0.7,
            "threshold": 1.0,
            "tint": [1.0, 1.0, 1.0, 1.0],
        },
    }
    tone_mapping = {
        "$schema": "infernux.render_effect",
        "dependencies": [],
        "feature_type": "infernux.post.tonemapping",
        "parameters": {"exposure": 1.0, "mode": 2},
    }
    effect_group = {
        "$schema": "infernux.render_effect_group",
        "entries": [
            {
                "asset": {
                    "guid": "",
                    "path_hint": "Assets/Rendering/Bloom.effect",
                },
                "enabled": True,
                "entry_id": "bloom",
                "overrides": {},
            },
            {
                "asset": {
                    "guid": "",
                    "path_hint": "Assets/Rendering/ACES Tone Mapping.effect",
                },
                "enabled": True,
                "entry_id": "tonemapping",
                "overrides": {},
            },
        ],
    }
    rendering_dir = os.path.join(assets_dir, "Rendering")
    _write_json_document(os.path.join(rendering_dir, "Bloom.effect"), bloom)
    _write_json_document(
        os.path.join(rendering_dir, "ACES Tone Mapping.effect"),
        tone_mapping,
    )
    _write_json_document(
        os.path.join(rendering_dir, "Default Post Processing.effectgroup"),
        effect_group,
    )

    scene_path = os.path.join(assets_dir, "Scenes", "Start.scene")
    _write_json_document(scene_path, _default_scene_document())
    final_scene_path = os.path.join(final_dir, "Assets", "Scenes", "Start.scene")
    _write_json_document(
        os.path.join(staging_dir, "ProjectSettings", "BuildSettings.json"),
        {
            "debug_mode": False,
            "display_mode": "windowed",
            "enable_jit": False,
            "game_name": project_name,
            "icon_path": "",
            "lto": True,
            "output_dir": "",
            "scenes": [final_scene_path],
            "splash_items": [],
            "window_height": 720,
            "window_resizable": True,
            "window_width": 1280,
        },
    )
    _write_json_document(
        os.path.join(staging_dir, "ProjectSettings", "EditorSettings.json"),
        {"lastOpenedScene": final_scene_path},
    )


def _popen_kwargs(*, capture_output: bool = False) -> dict:
    """Common subprocess kwargs: suppress console window for child processes.

    When capture_output is True we collect stdout/stderr so the UI can show a
    meaningful failure message instead of hanging indefinitely.
    """
    kw: dict = {
        "stdin": subprocess.DEVNULL,
        "env": merge_child_env_utf8({"PYTHONDONTWRITEBYTECODE": "1"}),
    }
    if capture_output:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
        kw["text"] = True
        kw["encoding"] = "utf-8"
        kw["errors"] = "replace"
    else:
        kw["stdout"] = subprocess.DEVNULL
        kw["stderr"] = subprocess.DEVNULL
    if sys.platform == "win32":
        kw["creationflags"] = _NO_WINDOW
    return kw


def _run_hidden(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            check=True,
            timeout=timeout,
            **_popen_kwargs(capture_output=True),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Command timed out after {timeout} s.\n{' '.join(args)}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = _summarize_output(exc.stderr or exc.stdout)
        raise RuntimeError(
            f"Command failed (exit code {exc.returncode}).\n{' '.join(args)}\n{details}"
        ) from exc


def _summarize_output(output: str) -> str:
    text = (output or "").strip()
    if not text:
        return "No diagnostic output was produced."
    lines = text.splitlines()
    return "\n".join(lines[-20:])


_NATIVE_IMPORT_SMOKE_TEST = (
    "import Infernux.lib\n"
    "print('INFERNUX_NATIVE_IMPORT_OK')\n"
)


def _python_cp_tag(python_exe: str = "") -> str:
    if python_exe:
        try:
            completed = subprocess.run(
                [python_exe, "-c", "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')"],
                timeout=30,
                **_popen_kwargs(capture_output=True),
            )
            if completed.returncode == 0:
                tag = (completed.stdout or "").strip()
                if tag.startswith("cp"):
                    return tag
        except (OSError, subprocess.SubprocessError) as _exc:
            logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def _engine_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _iter_cmake_python_cache_entries() -> list[str]:
    cache_path = os.path.join(_engine_root(), "out", "build", "CMakeCache.txt")
    if not os.path.isfile(cache_path):
        return []

    keys = (
        "Python3_EXECUTABLE",
        "_Python3_EXECUTABLE",
        "PYBIND11_PYTHON_EXECUTABLE_LAST",
    )
    out: list[str] = []
    try:
        with open(cache_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" not in line:
                    continue
                key_part, value = line.rstrip("\n").split("=", 1)
                key = key_part.split(":", 1)[0]
                if key in keys and value:
                    out.append(os.path.normpath(value.strip()))
    except OSError as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
    return out


def _iter_dev_project_python_candidates() -> list[str]:
    candidates: list[str] = []

    override = os.environ.get("INFERNUX_PROJECT_PYTHON", "").strip()
    if override:
        candidates.append(os.path.normpath(override))

    candidates.extend(_iter_cmake_python_cache_entries())
    candidates.append(sys.executable)

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        norm = os.path.normcase(os.path.abspath(candidate))
        if norm in seen:
            continue
        seen.add(norm)
        unique.append(candidate)
    return unique


def _select_dev_project_python() -> str:
    """Choose the interpreter used to create project .venv in dev mode.

    Prefer the Python that CMake used to build the native wheel.  Launching the
    Hub from a different Python should not silently create an incompatible
    project virtual environment.
    """
    fallback = sys.executable
    for candidate in _iter_dev_project_python_candidates():
        if not os.path.isfile(candidate):
            continue
        if _find_dev_wheel(candidate):
            return candidate
        if os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(os.path.abspath(sys.executable)):
            fallback = candidate
    return fallback


def _wheel_tag_parts(wheel_path: str) -> tuple[set[str], set[str], set[str]]:
    name = os.path.basename(wheel_path or "")
    if not name.lower().endswith(".whl"):
        return set(), set(), set()
    parts = name[:-4].split("-")
    if len(parts) < 5:
        return set(), set(), set()
    return set(parts[-3].split(".")), set(parts[-2].split(".")), set(parts[-1].split("."))


def _wheel_matches_python(wheel_path: str, python_tag: str) -> bool:
    python_tags, abi_tags, platform_tags = _wheel_tag_parts(wheel_path)
    if not python_tags or not abi_tags or not platform_tags:
        return False

    major_tag = f"py{python_tag[2]}" if python_tag.startswith("cp") and len(python_tag) >= 3 else "py3"
    platform_tag = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    return (
        (python_tag in python_tags or major_tag in python_tags or "py3" in python_tags)
        and (python_tag in abi_tags or "abi3" in abi_tags or "none" in abi_tags)
        and (platform_tag in platform_tags or "any" in platform_tags)
    )


def _find_dev_wheel(python_exe: str = "", *, strict: bool = False) -> str:
    """Find the newest compatible wheel produced by the local source tree.

    Only used in dev mode (non-frozen).
    """
    engine_root = _engine_root()
    wheel_dirs = (
        os.path.join(engine_root, "out", "build", "python_wheel"),
        os.path.join(engine_root, "dist"),
    )
    wheels = [
        wheel
        for wheel_dir in wheel_dirs
        for wheel in glob.glob(os.path.join(wheel_dir, "infernux-*.whl"))
    ]
    if wheels:
        python_tag = _python_cp_tag(python_exe)
        compatible = [wheel for wheel in wheels if _wheel_matches_python(wheel, python_tag)]
        if compatible:
            compatible.sort(key=os.path.getmtime, reverse=True)
            return compatible[0]
        if strict:
            available = ", ".join(os.path.basename(wheel) for wheel in sorted(wheels))
            raise RuntimeError(
                f"No prebuilt Infernux wheel compatible with Python {python_tag} was found in dist/.\n"
                f"Available wheels: {available}"
            )
    return ""


def _wheel_install_fingerprint(wheel_path: str) -> str:
    try:
        stat = os.stat(wheel_path)
    except OSError:
        return ""
    return f"{os.path.abspath(wheel_path)}\n{stat.st_size}\n{stat.st_mtime_ns}\n"


def _project_wheel_marker(project_dir: str) -> str:
    runtime_name = ".runtime" if is_frozen() else ".venv"
    return os.path.join(project_dir, runtime_name, ".infernux-wheel")


def _wheel_version_from_path(wheel_path: str) -> str:
    name = os.path.basename(wheel_path or "")
    if not name.lower().endswith(".whl"):
        return ""
    parts = name[:-4].split("-")
    if len(parts) < 2:
        return ""
    distribution = parts[0].replace("_", "-").lower()
    if distribution != "infernux":
        return ""
    return parts[1]


def _installed_distribution_version(python_exe: str, distribution_name: str) -> str:
    script = (
        "import importlib.metadata as metadata, sys; "
        "name = sys.argv[1]; "
        "\ntry:\n"
        "    print(metadata.version(name))\n"
        "except metadata.PackageNotFoundError:\n"
        "    raise SystemExit(1)\n"
    )
    try:
        completed = subprocess.run(
            [python_exe, "-c", script, distribution_name],
            timeout=30,
            **_popen_kwargs(capture_output=True),
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _distribution_files_present(site_packages: str, distribution_name: str) -> bool:
    if not os.path.isdir(site_packages):
        return False
    normalized = distribution_name.replace("-", "_").lower()
    dist_info_prefix = distribution_name.replace("_", "-").lower() + "-"
    try:
        names = os.listdir(site_packages)
    except OSError as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
        return False
    for name in names:
        lower_name = name.lower()
        if lower_name == normalized:
            return True
        if lower_name.startswith(dist_info_prefix) and lower_name.endswith(".dist-info"):
            return True
    return False


def _remove_tree(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/c", "rd", "/s", "/q", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
            env=merge_child_env_utf8(),
        )
        if completed.returncode == 0 and not os.path.exists(path):
            return
    shutil.rmtree(path, ignore_errors=True)


def _safe_wheel_member_path(name: str) -> str:
    normalized = name.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return ""
    return os.path.join(*parts)


def _wheel_target_relative_path(member_name: str) -> str:
    safe_name = _safe_wheel_member_path(member_name)
    if not safe_name:
        return ""
    parts = safe_name.split(os.sep)
    if len(parts) >= 3 and parts[0].endswith(".data") and parts[1] in {"purelib", "platlib"}:
        return os.path.join(*parts[2:])
    if len(parts) >= 2 and parts[0].endswith(".data"):
        return ""
    return safe_name


def _remove_installed_distribution(site_packages: str, distribution_name: str) -> None:
    normalized_package = distribution_name.replace("-", "_").lower()
    dist_info_prefix = distribution_name.replace("_", "-").lower() + "-"
    try:
        names = os.listdir(site_packages)
    except OSError as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
        return

    for name in names:
        lower_name = name.lower()
        if lower_name == normalized_package or (
            lower_name.startswith(dist_info_prefix) and lower_name.endswith(".dist-info")
        ):
            path = os.path.join(site_packages, name)
            if os.path.isdir(path) and not os.path.islink(path):
                _remove_tree(path)
            else:
                try:
                    os.remove(path)
                except OSError as _exc:
                    logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)


def _install_wheel_direct(wheel_path: str, site_packages: str, distribution_name: str) -> None:
    os.makedirs(site_packages, exist_ok=True)
    _remove_installed_distribution(site_packages, distribution_name)

    try:
        with zipfile.ZipFile(wheel_path) as wheel:
            for member in wheel.infolist():
                target_relative = _wheel_target_relative_path(member.filename)
                if not target_relative:
                    continue
                target_path = os.path.join(site_packages, target_relative)
                if member.is_dir():
                    os.makedirs(target_path, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with wheel.open(member) as src, open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(
            f"Failed to install the Infernux wheel into the project runtime.\n{wheel_path}\n{exc}"
        ) from exc


class ProjectModel:
    def __init__(self, db, version_manager=None, runtime_manager=None):
        self.db = db
        self.version_manager = version_manager
        self.runtime_manager = runtime_manager or PythonRuntimeManager()

    def add_project(self, name: str, project_dir: str):
        """Register a fully initialized project directory in the Hub."""
        if self.db is None:
            return None
        return self.db.add_project(name, project_dir)

    def register_existing_project(self, project_dir: str):
        """Validate and register an existing project without modifying it."""
        info = inspect_existing_project(project_dir)
        if self.db is None:
            raise RuntimeError("Project registry is not available.")
        if self.db.find_project_by_path(info.path) is not None:
            raise RuntimeError(f"This project is already in Infernux Hub:\n{info.path}")
        record = self.db.add_project(info.name, info.path)
        if record is None:
            raise RuntimeError(f"Failed to add the project to Infernux Hub:\n{info.path}")
        return record, info

    def remove_project(self, project_id: str) -> bool:
        """Remove only the Hub registry entry; project files are untouched."""
        return bool(self.db is not None and self.db.remove_project(project_id))

    def relocate_project(self, project_id: str, project_dir: str):
        """Point an existing registry entry at a validated project directory."""
        if self.db is None or self.db.get_project(project_id) is None:
            raise RuntimeError("The selected project is no longer registered in Hub.")

        info = inspect_existing_project(project_dir)
        existing = self.db.find_project_by_path(info.path)
        if existing is not None and existing.project_id != project_id:
            raise RuntimeError(f"This project is already in Infernux Hub:\n{info.path}")

        record = self.db.relocate_project(project_id, info.name, info.path)
        if record is None:
            raise RuntimeError(f"Failed to relocate the project in Infernux Hub:\n{info.path}")
        return record, info

    def delete_project(self, project_id: str) -> bool:
        """Compatibility alias for old callers; never deletes project files."""
        return self.remove_project(project_id)

    def init_project_folder(self, project_name: str, project_path: str,
                            engine_version: str = "", on_status=None) -> str:
        """Create a project transactionally and return its final directory."""
        parent_dir, final_dir = new_project_target(project_path, project_name)
        if os.path.exists(final_dir):
            raise RuntimeError(f"Project directory already exists:\n{final_dir}")

        staging_dir = os.path.join(parent_dir, f".infernux-create-{uuid.uuid4().hex}")
        if on_status:
            on_status("Creating project folders...")
        os.makedirs(staging_dir)
        committed = False

        try:
            for subdir in ("ProjectSettings", "Logs", "Library", "Assets"):
                os.makedirs(os.path.join(staging_dir, subdir))

            _create_default_project_content(
                staging_dir,
                final_dir,
                project_name,
            )

            self._copy_bundled_project_gitignore(
                os.path.join(staging_dir, ".gitignore"),
                engine_version,
            )
            self._copy_bundled_project_gitattributes(
                os.path.join(staging_dir, ".gitattributes"),
                engine_version,
            )

            req_path = os.path.join(staging_dir, "ProjectSettings", "requirements.txt")
            self._copy_bundled_requirements(req_path, engine_version)

            ini_path = os.path.join(staging_dir, f"{project_name}.ini")
            now = datetime.datetime.now()
            with open(ini_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("[Project]\n")
                f.write(f"name = {project_name}\n")
                f.write(f"path = {final_dir}\n")
                f.write(f"created_at = {now}\n")
                f.write(f"changed_at = {now}\n")

            if engine_version:
                from version_manager import VersionManager
                VersionManager.write_project_version(staging_dir, engine_version)

            if on_status:
                on_status("Finalizing project...")
            os.replace(staging_dir, final_dir)
            committed = True

            # Virtual environments can contain absolute paths and must be
            # created at their final location instead of being moved there.
            self._create_project_runtime(final_dir, on_status=on_status)
            self._install_infernux_in_runtime(final_dir, engine_version, on_status=on_status)

            if on_status:
                on_status("Writing project editor settings...")
            self._create_vscode_workspace(final_dir)
            return final_dir
        except Exception:
            _remove_tree(final_dir if committed else staging_dir)
            raise

    # -----------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------

    def _copy_bundled_support_file(
        self,
        source_name: str,
        dest_path: str,
        engine_version: str,
    ) -> bool:
        """Copy one support template from the source tree or selected wheel."""
        import zipfile

        engine_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        source_path = os.path.join(
            engine_root,
            "python",
            "Infernux",
            "resources",
            "supports",
            source_name,
        )
        if os.path.isfile(source_path):
            shutil.copy2(source_path, dest_path)
            return True

        wheel = ""
        if engine_version and self.version_manager is not None:
            wheel = self.version_manager.get_wheel_path(engine_version) or ""
        if not wheel and not is_frozen():
            wheel = _find_dev_wheel()
        if wheel and os.path.isfile(wheel):
            try:
                with zipfile.ZipFile(wheel) as zf:
                    archive_suffix = f"resources/supports/{source_name}"
                    for name in zf.namelist():
                        if name.endswith(archive_suffix):
                            with zf.open(name) as src, open(dest_path, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            return True
            except zipfile.BadZipFile as exc:
                logging.getLogger(__name__).debug(
                    "[Suppressed] %s: %s", type(exc).__name__, exc
                )
        return False

    def _copy_bundled_project_gitignore(self, dest_path: str, engine_version: str) -> None:
        if self._copy_bundled_support_file(
            "project.gitignore.txt", dest_path, engine_version
        ):
            return
        with open(dest_path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_FALLBACK_PROJECT_GITIGNORE)

    def _copy_bundled_project_gitattributes(
        self, dest_path: str, engine_version: str
    ) -> None:
        if self._copy_bundled_support_file(
            "project.gitattributes.txt", dest_path, engine_version
        ):
            return
        with open(dest_path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_FALLBACK_PROJECT_GITATTRIBUTES)

    def _copy_bundled_requirements(self, dest_path: str, engine_version: str) -> None:
        """Copy the default requirements.txt to *dest_path*.

        Resolves the file from the source tree (dev mode) or extracts it
        from the engine wheel, avoiding any ``import Infernux`` in the Hub
        process (which doesn't have the engine package installed).
        """
        self._copy_bundled_support_file("requirements.txt", dest_path, engine_version)

    @staticmethod
    def _get_project_python(project_dir: str) -> str:
        """Return the Python executable for the project.

        In frozen (packaged Hub) mode, each project owns a full Python copy
        under .runtime/python312/.  In dev mode, we use a classic .venv.
        """
        if is_frozen():
            runtime_dir = os.path.join(project_dir, ".runtime", "python312")
            if sys.platform == "win32":
                return os.path.join(runtime_dir, "python.exe")
            return os.path.join(runtime_dir, "bin", "python")
        # Dev mode: classic .venv
        venv_dir = os.path.join(project_dir, ".venv")
        if sys.platform == "win32":
            return os.path.join(venv_dir, "Scripts", "python.exe")
        return os.path.join(venv_dir, "bin", "python")

    def _create_project_runtime(self, project_dir: str, *, on_status=None) -> None:
        if is_frozen():
            runtime_path = os.path.join(project_dir, ".runtime", "python312")
            try:
                self.runtime_manager.create_project_runtime(runtime_path, on_status=on_status)
            except PythonRuntimeError as exc:
                raise RuntimeError(str(exc)) from exc
            return

        # Dev mode: create a classic .venv
        venv_path = os.path.join(project_dir, ".venv")
        if os.path.exists(venv_path):
            _remove_tree(venv_path)
        source_python = _select_dev_project_python()
        if on_status:
            on_status(f"Creating project virtual environment with {os.path.basename(source_python)}...")
        _run_hidden([source_python, "-m", "venv", "--copies", "--system-site-packages", venv_path], timeout=600)

    def _install_infernux_in_runtime(
        self,
        project_dir: str,
        engine_version: str = "",
        *,
        on_status=None,
        validate_current: bool = True,
    ):
        """Install the Infernux wheel into the project's Python environment.

        In frozen (packaged Hub) mode, the wheel is installed into the project's
        full Python copy at .runtime/python312/.
        In dev mode, the wheel is installed into the classic .venv.

        Source builds are intentionally blocked here so project creation never
        falls back to a local C++ compile.
        """
        project_python = ProjectModel._get_project_python(project_dir)
        if not os.path.isfile(project_python):
            raise RuntimeError(
                f"Project Python not found at {project_python}.\n"
                "The project runtime may not have been created correctly."
            )

        wheel = ""

        if not is_frozen():
            wheel = _find_dev_wheel(project_python, strict=True)
        elif engine_version and self.version_manager is not None:
            wheel = self.version_manager.get_wheel_path(engine_version) or ""

        if not wheel:
            if is_frozen():
                raise RuntimeError(
                    f"No downloaded Infernux wheel was found for version {engine_version or '(unknown)'}.\n"
                    "Open the Installs page and install that engine version first."
                )
            raise RuntimeError(
                "No prebuilt Infernux wheel was found in dist/.\n"
                "Build a wheel first; project creation will not fall back to a source build."
            )

        target_version = _wheel_version_from_path(wheel)
        if on_status:
            on_status("Checking the project runtime...")
        site_packages = ProjectModel._get_site_packages(project_dir)
        distribution_present = _distribution_files_present(site_packages, "Infernux")
        marker_path = _project_wheel_marker(project_dir)
        expected_fingerprint = _wheel_install_fingerprint(wheel)
        installed_fingerprint = ""
        try:
            with open(marker_path, "r", encoding="utf-8") as marker:
                installed_fingerprint = marker.read()
        except OSError:
            pass
        wheel_is_current = bool(
            expected_fingerprint and installed_fingerprint == expected_fingerprint
        )
        if distribution_present and wheel_is_current:
            if not validate_current:
                return
            try:
                ProjectModel.validate_python_runtime(project_python)
                return
            except RuntimeError as _exc:
                logging.getLogger(__name__).debug(
                    "[Suppressed] %s: %s", type(_exc).__name__, _exc
                )

        installed_version = ""
        if distribution_present:
            installed_version = _installed_distribution_version(project_python, "Infernux")
        version_is_current = bool(target_version and installed_version == target_version)
        if version_is_current and (is_frozen() or wheel_is_current):
            if not validate_current:
                return
            try:
                ProjectModel.validate_python_runtime(project_python)
                return
            except RuntimeError as _exc:
                logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)

        if is_frozen():
            if on_status:
                on_status("Installing Infernux engine files...")
            _install_wheel_direct(wheel, site_packages, "Infernux")
            if on_status:
                on_status("Validating project runtime...")
            ProjectModel.validate_python_runtime(project_python)
            os.makedirs(os.path.dirname(marker_path), exist_ok=True)
            with open(marker_path, "w", encoding="utf-8", newline="\n") as marker:
                marker.write(expected_fingerprint)
            return

        _PIP_FLAGS = [
            "--no-input",
            "--disable-pip-version-check",
            "--prefer-binary",
            "--only-binary=:all:",
            "--no-cache-dir",
            "--no-deps",
        ]

        pip_args = [project_python, "-m", "pip", "install", *_PIP_FLAGS]
        pip_args.append("--force-reinstall")
        pip_args.append(wheel)

        if on_status:
            on_status("Installing Infernux into the project runtime...")
        _run_hidden(pip_args, timeout=600)
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, "w", encoding="utf-8", newline="\n") as marker:
            marker.write(expected_fingerprint)
        if on_status:
            on_status("Validating project runtime...")
        ProjectModel.validate_python_runtime(project_python)

    @staticmethod
    def validate_python_runtime(project_python: str) -> None:
        if not os.path.isfile(project_python):
            raise RuntimeError(
                f"Project Python not found at {project_python}.\n"
                "The project runtime may not have been created correctly."
            )

        _run_hidden([project_python, "-c", _NATIVE_IMPORT_SMOKE_TEST], timeout=120)

    @staticmethod
    def validate_project_runtime(project_dir: str) -> None:
        ProjectModel.validate_python_runtime(ProjectModel._get_project_python(project_dir))

    @staticmethod
    def _get_site_packages(project_dir: str) -> str:
        """Return the site-packages directory for the project's Python runtime."""
        if is_frozen():
            runtime_dir = os.path.join(project_dir, ".runtime", "python312")
            if sys.platform == "win32":
                return os.path.join(runtime_dir, "Lib", "site-packages")
            return os.path.join(runtime_dir, "lib", "python3.12", "site-packages")
        venv_dir = os.path.join(project_dir, ".venv")
        if sys.platform == "win32":
            return os.path.join(venv_dir, "Lib", "site-packages")
        return os.path.join(venv_dir, "lib", "python3.12", "site-packages")

    @staticmethod
    def _create_vscode_workspace(project_dir: str):
        """
        Create .vscode/ config so that opening any file inside the project
        uses the correct Python interpreter and gets full Infernux autocompletion.
        """
        vscode_dir = os.path.join(project_dir, ".vscode")
        os.makedirs(vscode_dir, exist_ok=True)

        # ── settings.json ───────────────────────────────────────────────
        site_packages = ProjectModel._get_site_packages(project_dir)
        vscode_python = ProjectModel._vscode_python_path(project_dir)
        settings = {
            "python.defaultInterpreterPath": vscode_python,
            "python.terminal.activateEnvironment": True,
            "python.analysis.typeCheckingMode": "basic",
            "python.analysis.autoImportCompletions": True,
            "python.analysis.extraPaths": [site_packages],
            "python.analysis.diagnosticSeverityOverrides": {
                "reportMissingModuleSource": "none",
            },
            "editor.formatOnSave": True,
            "files.exclude": {
                "**/__pycache__": True,
                "**/*.pyc": True,
                "**/*.meta": True,
                ".venv": True,
                ".runtime": True,
                "Library": True,
                "Logs": True,
                "ProjectSettings": True,
            },
        }
        settings_path = os.path.join(vscode_dir, "settings.json")
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)

        # ── extensions.json ─────────────────────────────────────────────
        extensions = {
            "recommendations": [
                "ms-python.python",
                "ms-python.vscode-pylance",
            ]
        }
        extensions_path = os.path.join(vscode_dir, "extensions.json")
        with open(extensions_path, "w", encoding="utf-8") as f:
            json.dump(extensions, f, indent=4, ensure_ascii=False)

        # ── pyrightconfig.json (at project root) ────────────────────────
        # In frozen mode, point Pyright directly at the project runtime Python;
        # in dev mode, use the classic venvPath/venv convention.
        if is_frozen():
            pyright_config = {
                "pythonVersion": "3.12",
                "typeCheckingMode": "basic",
                "reportMissingModuleSource": False,
                "reportWildcardImportFromLibrary": False,
                "extraPaths": [site_packages],
                "include": ["Assets"],
            }
        else:
            pyright_config = {
                "venvPath": ".",
                "venv": ".venv",
                "pythonVersion": "3.12",
                "typeCheckingMode": "basic",
                "reportMissingModuleSource": False,
                "reportWildcardImportFromLibrary": False,
                "extraPaths": [site_packages],
                "include": ["Assets"],
            }
        pyright_path = os.path.join(project_dir, "pyrightconfig.json")
        with open(pyright_path, "w", encoding="utf-8") as f:
            json.dump(pyright_config, f, indent=4, ensure_ascii=False)

    @staticmethod
    def _vscode_python_path(project_dir: str) -> str:
        """Return the interpreter path VSCode should store in settings.json."""
        if is_frozen():
            return ProjectModel._get_project_python(project_dir).replace("\\", "/")
        if sys.platform == "win32":
            return "${workspaceFolder}/.venv/Scripts/python.exe"
        return "${workspaceFolder}/.venv/bin/python"
