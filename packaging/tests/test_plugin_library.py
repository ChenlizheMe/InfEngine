import json
from pathlib import Path

import pytest

import plugin_library


def _registry(project: Path, *locations: str) -> None:
    settings = project / "ProjectSettings"
    settings.mkdir(parents=True)
    records = [
        {
            "reference": f"fixture/{index}",
            "source": {
                "cache_scope": "hub",
                "cache_location": location,
            },
        }
        for index, location in enumerate(locations)
    ]
    (settings / "InxPlugins.json").write_text(
        json.dumps(
            {
                "$schema": "infernux.plugin_registry",
                "packages": records,
                "installed": [],
            }
        ),
        encoding="utf-8",
    )


def _package(root: Path, relative: str, payload: bytes) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_library_inspection_and_cleanup_preserve_every_project_reference(
    tmp_path, monkeypatch
):
    root = tmp_path / "Hub" / "Library" / "Plugins"
    monkeypatch.setenv("INFERNUX_PACKAGE_CACHE_ROOT", str(root))
    first = _package(root, "packages/vendor/first/1.0.0.inxpkg", b"first")
    second = _package(root, "packages/vendor/second/2.0.0.inxpkg", b"second")
    unused = _package(root, "packages/vendor/unused/3.0.0.inxpkg", b"unused")
    project_a = tmp_path / "ProjectA"
    project_b = tmp_path / "ProjectB"
    _registry(project_a, "packages/vendor/first/1.0.0.inxpkg")
    _registry(project_b, "packages/vendor/second/2.0.0.inxpkg")

    before = plugin_library.inspect_plugin_library((project_a, project_b))

    assert before.root == root.resolve()
    assert before.package_count == 3
    assert before.total_bytes == len(b"firstsecondunused")
    assert before.removable == (unused,)
    assert before.removable_bytes == len(b"unused")

    after = plugin_library.prune_unreferenced_packages((project_a, project_b))

    assert first.is_file()
    assert second.is_file()
    assert not unused.exists()
    assert after.package_count == 2
    assert after.removable == ()


def test_cleanup_refuses_an_unavailable_registered_project(tmp_path, monkeypatch):
    root = tmp_path / "Hub" / "Library" / "Plugins"
    monkeypatch.setenv("INFERNUX_PACKAGE_CACHE_ROOT", str(root))
    package = _package(root, "packages/vendor/plugin/1.0.0.inxpkg", b"payload")

    with pytest.raises(FileNotFoundError, match="registered project is unavailable"):
        plugin_library.prune_unreferenced_packages((tmp_path / "MissingProject",))

    assert package.is_file()


@pytest.mark.parametrize(
    "document",
    (
        {"$schema": "old", "packages": [], "installed": []},
        {"$schema": "infernux.plugin_registry", "packages": {}, "installed": []},
        {
            "$schema": "infernux.plugin_registry",
            "packages": [
                {
                    "source": {
                        "cache_scope": "hub",
                        "cache_location": "../escape.inxpkg",
                    }
                }
            ],
            "installed": [],
        },
    ),
)
def test_cleanup_refuses_invalid_project_registry(tmp_path, monkeypatch, document):
    root = tmp_path / "Hub" / "Library" / "Plugins"
    monkeypatch.setenv("INFERNUX_PACKAGE_CACHE_ROOT", str(root))
    package = _package(root, "packages/vendor/plugin/1.0.0.inxpkg", b"payload")
    project = tmp_path / "Project"
    settings = project / "ProjectSettings"
    settings.mkdir(parents=True)
    (settings / "InxPlugins.json").write_text(
        json.dumps(document), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="project (registry|reference)"):
        plugin_library.prune_unreferenced_packages((project,))

    assert package.is_file()


def test_default_library_path_is_owned_by_hub_data(tmp_path, monkeypatch):
    monkeypatch.delenv("INFERNUX_PACKAGE_CACHE_ROOT", raising=False)
    monkeypatch.setattr(
        plugin_library,
        "get_hub_user_data_dir",
        lambda: str(tmp_path / "HubData"),
    )

    assert plugin_library.plugin_library_root() == (
        tmp_path / "HubData" / "Library" / "Plugins"
    ).resolve()
