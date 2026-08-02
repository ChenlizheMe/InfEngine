from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "cmake" / "clean_installed_infernux.py"


def _run(mode: str, site_packages: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), mode, "--site-packages", str(site_packages)],
        check=check,
        capture_output=True,
        text=True,
    )


def test_residue_cleanup_is_scoped_to_infernux_pip_leftovers(tmp_path: Path):
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    canonical = site_packages / "Infernux"
    canonical.mkdir()
    unrelated = site_packages / "~umpy"
    unrelated.mkdir()
    for name in ("~nfernux", "~~nfernux", "~nfernux-0.2.9.dist-info", "~~lib01"):
        (site_packages / name).mkdir()

    _run("residues", site_packages)

    assert canonical.is_dir()
    assert unrelated.is_dir()
    assert {path.name for path in site_packages.iterdir()} == {"Infernux", "~umpy"}


def test_purge_removes_canonical_package_and_distribution(tmp_path: Path):
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    for name in ("Infernux", "infernux-0.2.9.dist-info", "~nfernux"):
        (site_packages / name).mkdir()

    _run("purge", site_packages)

    assert list(site_packages.iterdir()) == []


def test_verify_rejects_metadata_inside_installed_package(tmp_path: Path):
    site_packages = tmp_path / "site-packages"
    package = site_packages / "Infernux"
    package.mkdir(parents=True)

    _run("verify", site_packages)
    (package / "stale.meta").write_text("{}", encoding="utf-8")

    result = _run("verify", site_packages, check=False)
    assert result.returncode != 0
    assert "derived metadata" in result.stderr
