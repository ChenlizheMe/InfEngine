from __future__ import annotations

import subprocess
from pathlib import Path

from Infernux.engine.build import BuildRequest
from Infernux.engine.build.source_library import GitSource, acquire_git_source
from Infernux.engine.build import source_library


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *arguments],
        text=True,
        encoding="utf-8",
    ).strip()


def test_git_sources_are_downloaded_once_into_the_hub_library(tmp_path, monkeypatch):
    origin = tmp_path / "vendor" / "source"
    origin.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(origin)], check=True)
    _git(origin, "config", "user.email", "source-library@example.invalid")
    _git(origin, "config", "user.name", "Source Library Test")
    (origin / "include").mkdir()
    (origin / "include" / "fixture.h").write_text("fixture\n", encoding="utf-8")
    _git(origin, "add", "include/fixture.h")
    _git(origin, "commit", "--quiet", "-m", "fixture")
    revision = _git(origin, "rev-parse", "HEAD")

    cache_root = tmp_path / "Hub" / "Library" / "Plugins"
    monkeypatch.setenv("INFERNUX_PACKAGE_CACHE_ROOT", str(cache_root))
    progress = []
    request = BuildRequest(
        str(tmp_path / "Project"),
        "android-arm64",
        str(tmp_path / "Output"),
        progress=progress.append,
    )
    source = GitSource(
        "fixture",
        str(origin),
        revision,
        ("include/fixture.h",),
    )

    acquired = acquire_git_source(request, source)

    assert acquired.is_relative_to(cache_root.parent / "Sources")
    assert (acquired / "include" / "fixture.h").read_text(encoding="utf-8") == "fixture\n"
    assert (acquired / ".git").is_dir()
    assert any(item.message == "Downloading fixture source" for item in progress)

    monkeypatch.setattr(
        source_library,
        "_run_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cached source must not run Git")
        ),
    )
    assert acquire_git_source(request, source) == acquired
    assert any(item.message == "Using shared fixture source" for item in progress)
