#!/usr/bin/env python3
"""Create the immutable Android Platform Kit consumed by Infernux Hub.

This is a release/staging command.  It never downloads tools: every input must
already be a validated, pinned toolchain prepared by the release workflow.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import stat
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "packaging/android_support.py"


def _support_module():
    packaging_dir = str(MODULE.parent)
    if packaging_dir not in sys.path:
        sys.path.insert(0, packaging_dir)
    spec = importlib.util.spec_from_file_location("infernux_android_support", MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Android compatibility contract: {MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parser(module) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Package a pinned Hub-owned Android compatibility bundle."
    )
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--jdk", type=Path, required=True)
    parser.add_argument("--gradle", type=Path, required=True)
    parser.add_argument("--python-arm64", type=Path, required=True)
    parser.add_argument("--python-x86-64", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / module.archive_name(),
    )
    return parser


def _input_trees(arguments, module) -> tuple[tuple[str, Path], ...]:
    sdk = arguments.sdk.expanduser().resolve()
    roots = (
        (f"sdk/platforms/android-{module.ANDROID_API}", sdk / f"platforms/android-{module.ANDROID_API}"),
        (f"sdk/build-tools/{module.ANDROID_BUILD_TOOLS}", sdk / f"build-tools/{module.ANDROID_BUILD_TOOLS}"),
        (f"sdk/cmake/{module.ANDROID_CMAKE}", sdk / f"cmake/{module.ANDROID_CMAKE}"),
        (f"sdk/ndk/{module.ANDROID_NDK}", sdk / f"ndk/{module.ANDROID_NDK}"),
        ("sdk/platform-tools", sdk / "platform-tools"),
        ("sdk/licenses", sdk / "licenses"),
        ("jdk", arguments.jdk.expanduser().resolve()),
        ("gradle", arguments.gradle.expanduser().resolve()),
        ("python/arm64-v8a", arguments.python_arm64.expanduser().resolve()),
        ("python/x86_64", arguments.python_x86_64.expanduser().resolve()),
    )
    missing = [str(source) for _prefix, source in roots if not source.is_dir()]
    if missing:
        raise FileNotFoundError(
            "Android compatibility inputs are missing:\n" + "\n".join(missing)
        )
    return roots


def _files(roots: tuple[tuple[str, Path], ...]):
    seen: set[str] = set()
    for prefix, source_root in roots:
        for source in sorted(source_root.rglob("*"), key=lambda path: path.as_posix()):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root).as_posix()
            archive_path = f"{prefix}/{relative}"
            folded = archive_path.casefold()
            if folded in seen:
                raise RuntimeError(f"Duplicate Android compatibility path: {archive_path}")
            seen.add(folded)
            yield archive_path, source


def _write_file(archive: zipfile.ZipFile, archive_path: str, source: Path) -> None:
    info = zipfile.ZipInfo(archive_path, (1980, 1, 1, 0, 0, 0))
    mode = source.stat().st_mode
    info.external_attr = (stat.S_IFREG | (mode & 0o777)) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with source.open("rb") as reader, archive.open(info, "w", force_zip64=True) as writer:
        while block := reader.read(1024 * 1024):
            writer.write(block)


def main() -> int:
    module = _support_module()
    arguments = _parser(module).parse_args()
    roots = _input_trees(arguments, module)
    files = tuple(_files(roots))
    total_bytes = sum(source.stat().st_size for _path, source in files)
    manifest = module.create_android_support_manifest(total_bytes=total_bytes)
    output = arguments.output.expanduser().resolve()
    if output.name != module.archive_name():
        raise ValueError(
            f"Android compatibility release asset must be named {module.archive_name()}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            manifest_info = zipfile.ZipInfo(module.MANIFEST_NAME, (1980, 1, 1, 0, 0, 0))
            manifest_info.external_attr = (stat.S_IFREG | 0o644) << 16
            manifest_info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(
                manifest_info,
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            for archive_path, source in files:
                _write_file(archive, archive_path, source)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "archive": str(output),
                "files": len(files),
                "uncompressed_bytes": total_bytes,
                "archive_bytes": output.stat().st_size,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
