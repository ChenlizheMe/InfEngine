from pathlib import Path
import json

from Infernux.engine.bootstrap import EditorBootstrap
from Infernux.particle.artifact import ParticleArtifactRegistry
from Infernux.particle.asset import ParticleGraphAsset


def _write_graph(path: Path, *, guid: str, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ParticleGraphAsset(stable_id=name, name=name).canonical_json(),
        encoding="utf-8",
    )
    Path(str(path) + ".meta").write_text(
        json.dumps(
            {
                "metadata": {
                    "guid": {"type": "string", "value": guid},
                }
            }
        ),
        encoding="utf-8",
    )


def test_editor_bootstrap_compiles_missing_particle_artifacts(tmp_path):
    ParticleArtifactRegistry.clear()
    path = tmp_path / "Assets" / "VFX" / "Portal.particlegraph"
    guid = "b" * 32
    _write_graph(path, guid=guid, name="portal-boot")
    artifact = tmp_path / "Library" / "Artifacts" / "Particle" / f"{guid}.inxparticle"
    assert not artifact.exists()

    bootstrap = EditorBootstrap.__new__(EditorBootstrap)
    bootstrap.project_path = str(tmp_path)
    bootstrap._ensure_particle_artifacts()

    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["$schema"] == "infernux.particle_artifact"
    assert payload["source_hash"]


def test_editor_bootstrap_continues_when_particle_compile_fails(tmp_path):
    ParticleArtifactRegistry.clear()
    path = tmp_path / "Assets" / "VFX" / "Broken.particlegraph"
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    bootstrap = EditorBootstrap.__new__(EditorBootstrap)
    bootstrap.project_path = str(tmp_path)
    bootstrap._ensure_particle_artifacts()
