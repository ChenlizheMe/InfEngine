"""Build platform-plugin Infernux Player Runtime Packs for release engineering."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from Infernux.engine.path_utils import resolved_path

from Infernux.engine.game_builder import GameBuilder
from Infernux.engine.nuitka_builder import NuitkaBuilder
from Infernux.resources import get_package_resources_path
from Infernux.version import ENGINE_VERSION


def _player_host_path() -> Path:
    configured = os.environ.get("INFERNUX_PLAYER_HOST_PATH", "").strip()
    if not configured:
        raise RuntimeError("Publish through the CMake prebuild_player_runtime target; PlayerHost is required")
    return Path(configured)


def _clean_generated_python_package_artifacts() -> None:
    """Remove editor metadata and stale incremental wheel payloads."""
    package_root = Path(resolved_path(__file__)).parents[1]
    for metadata_path in package_root.rglob("*.meta"):
        try:
            metadata_path.unlink()
        except OSError:
            pass

    repository_root = Path(resolved_path(__file__)).parents[3]
    build_root = repository_root / "build"
    if not build_root.is_dir():
        return
    for package_copy in build_root.glob("lib*/Infernux"):
        for generated_dir in ("_runtime_packs", "_runtime_modules"):
            shutil.rmtree(package_copy / generated_dir, ignore_errors=True)
        for metadata_path in package_copy.rglob("*.meta"):
            try:
                metadata_path.unlink()
            except OSError:
                pass


def build_prebuilt_runtime(
    output_root: str,
    *,
    build_cache_root: str,
    profile: str = "release",
    force: bool = False,
    lto: bool = True,
) -> dict[str, object]:
    """Compile and export a Player pack plus its optional parallel module."""
    if profile not in {"release", "debug"}:
        raise ValueError(f"Unsupported Runtime Pack profile: {profile}")

    work_root = tempfile.mkdtemp(prefix="infernux-runtime-pack-")
    try:
        project_root = os.path.join(work_root, "project")
        build_root = os.path.join(work_root, "build")
        os.makedirs(project_root, exist_ok=True)
        game_builder = GameBuilder(
            project_root,
            build_root,
            game_name="InfernuxPlayer",
            debug_mode=profile == "debug",
            lto=lto,
            enable_jit=False,
        )
        boot_script = game_builder._generate_boot_script()
        default_icon = os.path.join(
            get_package_resources_path(), "icons", "icon.png"
        )
        builder = NuitkaBuilder(
            entry_script=boot_script,
            output_dir=build_root,
            build_cache_root=build_cache_root,
            output_filename=(
                "_InfernuxPlayer.pyd"
                if sys.platform == "win32"
                else "_InfernuxPlayer.so"
            ),
            product_name="Infernux Player",
            icon_path=default_icon if os.path.isfile(default_icon) else None,
            raw_copy_packages=["numpy", "packaging"],
            runtime_support_packages=["numba", "llvmlite"],
            console_mode="force" if profile == "debug" else "disable",
            lto=lto,
            runtime_pack_cache=True,
            packaged_runtime_lookup=False,
            player_module=True,
        )
        builder.build(force_runtime_rebuild=force)
        exported_path = builder.export_runtime_pack(output_root)
        player_host = _player_host_path()
        if not player_host.is_file():
            raise RuntimeError(
                f"Release Runtime Pack cannot be exported without {player_host.name}"
            )
        shutil.copy2(player_host, Path(exported_path) / player_host.name)
        module_root = str(Path(resolved_path(output_root)).parent / "_runtime_modules")
        exported_module_path = builder.export_runtime_module(
            module_root,
            module_name="parallel",
            packages=["numba", "llvmlite"],
        )
        manifest_path = os.path.join(exported_path, "Player.inxmanifest")
        with open(manifest_path, "r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        manifest.update({
            "distribution": "wheel-package-data",
            "profile": profile,
            "engine_version": ENGINE_VERSION,
        })
        temporary = manifest_path + f".{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")
        os.replace(temporary, manifest_path)
        module_manifest_path = os.path.join(
            exported_module_path, "Player.inxmanifest"
        )
        with open(module_manifest_path, "r", encoding="utf-8") as manifest_file:
            module_manifest = json.load(manifest_file)
        module_manifest.update({
            "distribution": "wheel-package-data",
            "profile": profile,
            "engine_version": ENGINE_VERSION,
        })
        temporary = module_manifest_path + f".{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as manifest_file:
            json.dump(module_manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")
        os.replace(temporary, module_manifest_path)
        exported_resolved = Path(resolved_path(exported_path))
        for candidate_manifest in Path(output_root).glob("*/Player.inxmanifest"):
            candidate_root = candidate_manifest.parent.resolve()
            if candidate_root == exported_resolved:
                continue
            try:
                candidate = json.loads(candidate_manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                candidate.get("distribution") == "wheel-package-data"
                and candidate.get("profile") == profile
            ):
                shutil.rmtree(candidate_root, ignore_errors=True)
        exported_module_resolved = Path(resolved_path(exported_module_path))
        for candidate_manifest in Path(module_root).glob("*/Player.inxmanifest"):
            candidate_root = candidate_manifest.parent.resolve()
            if candidate_root == exported_module_resolved:
                continue
            try:
                candidate = json.loads(candidate_manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                candidate.get("distribution") == "wheel-package-data"
                and candidate.get("profile") == profile
            ):
                shutil.rmtree(candidate_root, ignore_errors=True)
        _clean_generated_python_package_artifacts()
        return {
            "path": exported_path,
            "parallel_module_path": exported_module_path,
            "profile": profile,
            "compatibility_key": builder.last_runtime_compatibility_key,
            "fingerprint": builder.last_runtime_pack_key,
            "archive_bytes": manifest.get("archive_bytes", 0),
            "parallel_module_archive_bytes": module_manifest.get(
                "archive_bytes", 0
            ),
        }
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def export_platform_player(result: dict[str, object], destination: str) -> str:
    """Publish the existing runtime archives as one plugin-owned payload directory."""
    from .precompiled_player import inspect_desktop_runtime

    source = Path(str(result["path"]))
    inspect_desktop_runtime(str(source))
    target = Path(resolved_path(destination))
    target.mkdir(parents=True, exist_ok=True)
    host = "InfernuxPlayerHost.exe" if sys.platform == "win32" else "InfernuxPlayerHost"
    for name in ("Runtime.inxrt", host):
        shutil.copy2(source / name, target / name)
    module = Path(str(result["parallel_module_path"])) / "Parallel.inxmod"
    shutil.copy2(module, target / module.name)
    manifest = json.loads((source / "Player.inxmanifest").read_text(encoding="utf-8"))
    manifest["distribution"] = "platform-plugin"
    (target / "Player.inxmanifest").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    return str(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        default=str(Path(resolved_path(__file__)).parents[1] / "_runtime_packs"),
        help="Directory embedded into the platform wheel as Infernux package data.",
    )
    parser.add_argument(
        "--build-cache-root",
        default=str(
            Path(resolved_path(__file__)).parents[3]
            / "out"
            / "build"
            / "PrebuiltRuntime"
        ),
        help="Repository-owned Nuitka staging and incremental build cache.",
    )
    parser.add_argument("--profile", choices=("release", "debug", "all"), default="release")
    parser.add_argument("--force", action="store_true", help="Ignore the local compiled Runtime Pack cache.")
    parser.add_argument("--no-lto", action="store_true", help="Build a non-LTO compatibility variant.")
    parser.add_argument("--platform-player-output", help="Publish a flat Player payload for a platform plugin.")
    args = parser.parse_args(argv)
    if args.platform_player_output and args.profile == "all":
        parser.error("A platform plugin carries one generic Player; choose one publication profile")

    profiles = ("release", "debug") if args.profile == "all" else (args.profile,)
    results = [
        build_prebuilt_runtime(
            args.output_root,
            build_cache_root=args.build_cache_root,
            profile=profile,
            force=args.force,
            lto=not args.no_lto,
        )
        for profile in profiles
    ]
    if args.platform_player_output:
        results[0]["platform_player_path"] = export_platform_player(results[0], args.platform_player_output)
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
