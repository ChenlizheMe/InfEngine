"""Generate website release metadata from the actual versioned distributions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def build_catalog(release_dir: Path, published_at: str | None, linux_inventory: Path | None = None) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    base = f"https://github.com/ChenlizheMe/Infernux/releases/download/v{version}"
    release_url = f"https://github.com/ChenlizheMe/Infernux/releases/tag/v{version}"
    platforms = {}
    assets = []
    ci = json.loads(linux_inventory.read_text(encoding="utf-8")) if linux_inventory else None
    for platform, suffix, wheel_suffix in (
        ("windows-x64", ".exe", "win_amd64.whl"),
        ("linux-x64", "", "linux_x86_64.whl"),
    ):
        manifest_name = f"InfernuxHub-{platform}-manifest.json"
        from_ci = platform == "linux-x64" and ci is not None
        manifest = ci["manifest"] if from_ci else json.loads((release_dir / manifest_name).read_text(encoding="utf-8"))
        if manifest["version"] != version or manifest["platform"] != platform:
            raise ValueError(f"{manifest_name} does not describe {version}/{platform}")
        def asset_size(name):
            return ci["files"][f"{version}/{name}"] if from_ci else (release_dir / name).stat().st_size
        installer_name = f"InfernuxHubInstaller-{version}-{platform}{suffix}"
        update_name = f"InfernuxHub-{version}-{platform}-full.zip"
        wheel_name = f"infernux-{version}-cp313-cp313-{wheel_suffix}"
        platforms[platform] = {
            "installer": {"name": installer_name, "url": f"{base}/{installer_name}"},
            "update": {"name": update_name, "url": f"{base}/{update_name}", "size": asset_size(update_name)},
            "manifest": {"name": manifest_name, "url": f"{base}/{manifest_name}"},
        }
        for kind, name in (("hub-installer", installer_name), ("python-wheel", wheel_name)):
            assets.append({"kind": kind, "name": name, "size_bytes": asset_size(name), "url": f"{base}/{name}"})
    release = {
        "schema_version": 2, "version": version, "tag": f"v{version}",
        "name": f"Infernux v{version}", "channel": "stable", "published_at": published_at,
        "platforms": ["Windows 10/11 x64", "Linux x86_64"], "python_abi": "CPython 3.13 x64",
        "release_url": release_url, "assets": assets,
    }
    catalog_path = ROOT / "docs/hub-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["stable"] = version
    catalog["releases"] = [{
        "version": version, "channel": "stable", "published_at": published_at,
        "release_url": release_url, "minimum_updatable_version": version, "platforms": platforms,
    }] + [item for item in catalog["releases"] if item["version"] != version]
    for path, document in ((ROOT / "docs/release.json", release), (catalog_path, catalog)):
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {version} release catalogs from {release_dir}; published_at={published_at!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--published-at", help="Actual GitHub publication timestamp; omit while preparing the release")
    parser.add_argument("--linux-inventory", type=Path, help="Verified Linux CI archive inventory instead of local Linux files")
    args = parser.parse_args()
    build_catalog(args.release_dir, args.published_at, args.linux_inventory)
