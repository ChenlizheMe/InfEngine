"""Command-line package build, verification, and release metadata tools."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from Infernux.version import ENGINE_VERSION
from Infernux.engine.path_utils import resolved_path

from .github_releases import (
    RELEASE_MANIFEST_SCHEMA,
)
from .package import InxPackage


def _validated_metadata(package: Path) -> dict[str, object]:
    preview = InxPackage.inspect(str(package))
    metadata = dict(preview.metadata)
    version_text = str(metadata.get("version", "")).strip()
    engine = str(metadata.get("engine", "")).strip()
    try:
        Version(version_text)
    except InvalidVersion as exc:
        raise RuntimeError(f"Invalid InxPackage version: {version_text}") from exc
    try:
        SpecifierSet(engine)
    except InvalidSpecifier as exc:
        raise RuntimeError(f"Invalid InxPackage engine range: {engine}") from exc
    return metadata


def package_build(args: argparse.Namespace) -> int:
    source = Path(resolved_path(args.source))
    output = Path(resolved_path(args.output))
    output.parent.mkdir(parents=True, exist_ok=True)
    preview = InxPackage.export_source(
        str(source),
        str(output),
        profile=str(args.profile),
    )
    print(
        json.dumps(
            {
                "path": str(output),
                "reference": str(preview.metadata.get("reference", "")),
                "version": str(preview.metadata.get("version", "")),
                "bytes": output.stat().st_size,
            },
            ensure_ascii=False,
        )
    )
    return 0


def package_verify(args: argparse.Namespace) -> int:
    package = Path(resolved_path(args.package))
    metadata = _validated_metadata(package)
    requested_engine = str(args.engine or "").strip()
    engine = str(metadata.get("engine", "")).strip()
    if requested_engine and engine and Version(requested_engine) not in SpecifierSet(engine):
        raise RuntimeError(
            f"InxPackage requires Infernux {engine}, requested {requested_engine}"
        )
    print(
        json.dumps(
            {
                "path": str(package),
                "reference": str(metadata.get("reference", "")),
                "version": str(metadata.get("version", "")),
                "engine": engine,
                "bytes": package.stat().st_size,
                "verified": True,
            },
            ensure_ascii=False,
        )
    )
    return 0


def package_release_manifest(args: argparse.Namespace) -> int:
    package = Path(resolved_path(args.package))
    output = Path(resolved_path(args.output))
    metadata = _validated_metadata(package)
    version = str(metadata.get("version", "")).strip()
    expected_tag = f"v{version}"
    tag = str(args.tag or os.environ.get("GITHUB_REF_NAME", "")).strip()
    if args.shared_release and not tag:
        raise RuntimeError("A shared repository release requires an explicit tag")
    if tag and not args.shared_release and tag != expected_tag:
        raise RuntimeError(
            f"Release tag {tag!r} does not match InxPackage version {version!r}"
        )
    document = {
        "$schema": RELEASE_MANIFEST_SCHEMA,
        "reference": str(metadata.get("reference", "")),
        "version": version,
        "engine": str(metadata.get("engine", "")),
        "artifact": {
            "name": package.name,
        },
        "generator": {
            "name": "Infernux",
            "version": ENGINE_VERSION,
        },
    }
    if args.shared_release:
        document["release_tag"] = tag
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    print(str(output))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inx")
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package")
    package_commands = package.add_subparsers(dest="package_command", required=True)

    build = package_commands.add_parser("build")
    build.add_argument("source")
    build.add_argument("output")
    build.add_argument("--profile", choices=("development", "release"), default="release")
    build.set_defaults(handler=package_build)

    verify = package_commands.add_parser("verify")
    verify.add_argument("package")
    verify.add_argument("--engine", default=ENGINE_VERSION)
    verify.set_defaults(handler=package_verify)

    manifest = package_commands.add_parser("release-manifest")
    manifest.add_argument("package")
    manifest.add_argument("--output", required=True)
    manifest.add_argument("--tag", default="")
    manifest.add_argument("--shared-release", action="store_true")
    manifest.set_defaults(handler=package_release_manifest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"inx: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
