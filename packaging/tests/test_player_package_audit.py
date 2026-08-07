from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PYTHON = ROOT / "python"
if str(SOURCE_PYTHON) not in sys.path:
    sys.path.insert(0, str(SOURCE_PYTHON))

from Infernux.engine.player_package_audit import audit_player_package
from Infernux.engine.player_package_format import write_pack


def _valid_player(tmp_path: Path) -> Path:
    root = tmp_path / "Player"
    data = root / "Data"
    source = tmp_path / "source"
    data.mkdir(parents=True)
    source.mkdir()
    (root / "Balance.exe").write_bytes(b"one entry")
    (data / "BuildManifest.json").write_text("{}", encoding="utf-8")
    (source / "Player.pyc").write_bytes(b"compiled")
    (source / "runtime.bin").write_bytes(b"artifact")
    write_pack(
        (
            ("Assets/Player.pyc", source / "Player.pyc"),
            ("Library/Artifacts/runtime.bin", source / "runtime.bin"),
        ),
        data / "Content.inxpack",
    )
    (data / "Content.json").write_text(
        json.dumps({"archive": "Content.inxpack"}), encoding="utf-8"
    )
    return root


def test_audit_writes_service_and_reachability_manifest(tmp_path: Path):
    root = _valid_player(tmp_path)

    manifest = audit_player_package(root)

    assert manifest["$schema"] == "infernux.player_runtime_manifest"
    assert manifest["product"]["single_entry_point"] is True
    assert "player_bootstrap" in manifest["services"]["declared"]
    assert manifest["audit"]["passed"] is True
    assert (root / "Data" / "PlayerRuntimeManifest.json").is_file()


def test_audit_rejects_meta_and_author_source(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Data" / "Assets").mkdir()
    (root / "Data" / "Assets" / "Player.py").write_text("pass", encoding="utf-8")
    (root / "Data" / "Assets" / "Player.py.meta").write_text("meta", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Player package audit failed"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_duplicate_native_payload(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "engine.dll").write_bytes(b"same native")
    (root / "Data" / "engine-copy.dll").write_bytes(b"same native")

    with pytest.raises(RuntimeError, match="duplicate_native_payloads"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_legacy_zip(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Data" / "legacy.zip").write_bytes(b"PK\x03\x04legacy")

    with pytest.raises(RuntimeError, match="legacy_zip_files"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_multiple_player_entry_points(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "LegacyPlayer.exe").write_bytes(b"legacy entry")

    with pytest.raises(RuntimeError, match="legacy_dual_entry_point"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_duplicate_payloads_inside_containers(tmp_path: Path):
    root = _valid_player(tmp_path)
    source = tmp_path / "duplicate.bin"
    source.write_bytes(b"same payload")
    write_pack(
        (("Library/a.bin", source), ("Library/b.bin", source)),
        root / "Data" / "duplicate.inxpack",
    )

    with pytest.raises(RuntimeError, match="duplicate_payload_groups"):
        audit_player_package(root, write_manifest=False)
