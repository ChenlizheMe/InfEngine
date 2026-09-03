#!/usr/bin/env python3
"""Stamp or verify an Infernux Android CPython prefix."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    ROOT
    / "external/plugins/infernux_android/package/editor/infernux_android/runtime_manifest.py"
)


def _runtime_manifest_module():
    spec = importlib.util.spec_from_file_location(
        "infernux_android_runtime_manifest", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Android runtime manifest support: {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify the provenance manifest of an Android CPython prefix."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    stamp = subparsers.add_parser("stamp", help="Create a manifest after building a prefix")
    stamp.add_argument("prefix", type=Path)
    stamp.add_argument("--abi", required=True, choices=("arm64-v8a", "x86_64"))
    stamp.add_argument("--python-version", required=True)
    stamp.add_argument("--cpython-api", required=True, type=int)
    stamp.add_argument("--minimum-api", required=True, type=int)
    stamp.add_argument("--ndk-version", required=True)
    stamp.add_argument("--source-url", required=True)

    verify = subparsers.add_parser("verify", help="Verify an existing prefix")
    verify.add_argument("prefix", type=Path)
    verify.add_argument("--abi", required=True, choices=("arm64-v8a", "x86_64"))
    verify.add_argument("--python-series", default="3.13")
    verify.add_argument("--application-minimum-api", type=int, default=26)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    module = _runtime_manifest_module()
    if arguments.command == "stamp":
        manifest = module.create_runtime_manifest(
            arguments.prefix,
            abi=arguments.abi,
            python_version=arguments.python_version,
            cpython_android_api=arguments.cpython_api,
            minimum_android_api=arguments.minimum_api,
            ndk_version=arguments.ndk_version,
            source_url=arguments.source_url,
        )
    else:
        manifest = module.validate_runtime_manifest(
            arguments.prefix,
            expected_abi=arguments.abi,
            expected_python_series=arguments.python_series,
            application_minimum_android_api=arguments.application_minimum_api,
        )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
