from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "acceptance" / "build_evidence_manifest.py"


def _module():
    spec = importlib.util.spec_from_file_location("build_evidence_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _repository(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@infernux.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Infernux Test"], cwd=path, check=True)
    (path / "source.txt").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.txt"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"],
        cwd=path,
        check=True,
        env={**__import__("os").environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"},
    )


def test_manifest_hashes_files_directories_and_results(tmp_path: Path):
    module = _module()
    repository = tmp_path / "repo"
    evidence = tmp_path / "evidence"
    repository.mkdir()
    evidence.mkdir()
    _repository(repository)
    artifact = evidence / "player.bin"
    artifact.write_bytes(b"player")
    web = evidence / "web"
    web.mkdir()
    (web / "index.html").write_text("web", encoding="utf-8")
    result = evidence / "smoke.json"
    result.write_text(json.dumps({"status": "passed", "success": True}), encoding="utf-8")

    manifest = module.build_manifest(
        release="0.4.0",
        root=evidence,
        repository=repository,
        artifacts=(("android", artifact), ("web", web)),
        results=(("browser-smoke", result),),
        require_clean=True,
    )

    assert manifest["$schema"] == module.SCHEMA
    assert manifest["source"]["dirty"] is False
    assert manifest["source"]["generated_at"] == "2026-01-01T00:00:00+00:00"
    assert [item["id"] for item in manifest["artifacts"]] == ["android", "web"]
    assert manifest["artifacts"][1]["file_count"] == 1
    assert manifest["results"][0]["status"] == "passed"
    assert manifest["results"][0]["success"] is True


def test_manifest_rejects_paths_outside_root(tmp_path: Path):
    module = _module()
    root = tmp_path / "evidence"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    with pytest.raises(ValueError, match="outside the evidence root"):
        module.artifact_record("outside", outside, root)


def test_named_paths_reject_duplicate_ids():
    module = _module()

    with pytest.raises(ValueError, match="Duplicate"):
        module.parse_named_paths(("player=a", "player=b"))


def test_require_clean_rejects_dirty_repository(tmp_path: Path):
    module = _module()
    _repository(tmp_path)
    (tmp_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(RuntimeError, match="clean Git worktree"):
        module.repository_record(tmp_path, require_clean=True)
