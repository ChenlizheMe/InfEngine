from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "maintenance" / "code_slimming_audit.py"


def _module():
    spec = importlib.util.spec_from_file_location("code_slimming_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_line_metrics_have_explicit_physical_and_logical_definitions():
    module = _module()

    metrics = module._line_metrics("# comment\nvalue = 1\n\n  // note\nreturn value\n")

    assert metrics == {
        "physical_lines": 5,
        "nonblank_lines": 4,
        "logical_lines": 2,
    }


def test_python_inventory_marks_candidates_without_deciding_removal(tmp_path):
    module = _module()
    source = tmp_path / "python" / "Infernux" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def load():\n"
        "    try:\n"
        "        return primary()\n"
        "    except Exception:\n"
        "        return None  # legacy fallback sha256\n",
        encoding="utf-8",
    )

    metrics, candidates = module.audit_paths(tmp_path, [source])

    assert metrics["totals"]["files"] == 1
    assert metrics["totals"]["physical_lines"] == 5
    rules = {item["rule"] for item in candidates}
    assert {
        "fallback-token",
        "legacy-token",
        "sha256-token",
        "python-broad-except",
        "python-default-on-error",
    } <= rules
    assert {item["classification"] for item in candidates} == {"unclassified"}
    assert {item["layer"] for item in candidates} == {"runtime-python"}


def test_owned_layers_exclude_third_party_and_generated_surfaces():
    module = _module()

    assert module._layer("cpp/infernux/Infernux.cpp") == "runtime-cpp"
    assert module._layer("python/Infernux/engine/engine.py") == "runtime-python"
    assert module._layer("external/plugins/infernux_web/README.md") == (
        "official-plugins"
    )
    assert module._layer("external/assimp/code/Common.cpp") is None


def test_repository_audit_is_bound_to_source_state_and_complete_inventory():
    module = _module()

    payload = module.build_audit(ROOT)

    assert payload["$schema"] == "infernux.code_slimming_audit"
    assert payload["source_state"]["branch"]
    assert len(payload["source_state"]["commit"]) == 40
    assert payload["scope"]["owned_files"] > 1000
    assert payload["metrics"]["totals"]["physical_lines"] > 100_000
    assert payload["candidate_inventory"]["count"] == len(
        payload["candidate_inventory"]["items"]
    )
    assert payload["candidate_inventory"]["product_count"] > 0
    assert payload["candidate_inventory"]["by_layer"]["runtime-python"] > 0
    assert all(
        item["classification"] == "unclassified"
        for item in payload["candidate_inventory"]["items"]
    )


def test_artifact_size_measures_directory_when_manifest_size_is_zero(tmp_path):
    module = _module()
    artifact = tmp_path / "Player"
    artifact.mkdir()
    (artifact / "runtime.bin").write_bytes(b"runtime")
    (artifact / "content.bin").write_bytes(b"content")

    assert module._artifact_size({"path": str(artifact), "size": 0}) == 14
