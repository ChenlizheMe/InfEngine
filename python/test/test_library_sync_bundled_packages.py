from __future__ import annotations

import json
from pathlib import Path

from Infernux.engine.library_sync import sync_resources


def test_library_sync_does_not_mirror_wheel_mandatory_packages(tmp_path, monkeypatch):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "builtin.inxpkg").write_bytes(b"mandatory")
    (resources / "shader.bin").write_bytes(b"resource")
    nested = resources / "official_packages"
    nested.mkdir()
    (nested / "catalog-artifact.inxpkg").write_bytes(b"catalog")
    project = tmp_path / "project"
    monkeypatch.setattr(
        "Infernux.resources.get_package_resources_path", lambda: str(resources)
    )

    destination = Path(sync_resources(str(project)))

    assert not (destination / "builtin.inxpkg").exists()
    assert (destination / "shader.bin").read_bytes() == b"resource"
    assert (
        destination / "official_packages" / "catalog-artifact.inxpkg"
    ).read_bytes() == b"catalog"
    manifest = json.loads(
        (project / "Library" / ".InfernuxResources.json").read_text(encoding="utf-8")
    )
    assert set(manifest) == {"entries"}
    assert manifest["entries"]["shader.bin"]["size"] == len(b"resource")
