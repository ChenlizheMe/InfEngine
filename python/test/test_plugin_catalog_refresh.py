from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from Infernux.plugins import PluginManager, PluginRegistry
from Infernux.plugins import official


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("INFERNUX_PACKAGE_CACHE_ROOT", str(tmp_path / "shared"))
    return {
        "$schema": official.OFFICIAL_REGISTRY_SCHEMA,
        "packages": [{
            "reference": "infernux/platform-web", "version": "0.2.0",
            "engine": ">=0.4,<0.5", "artifact": "infernux.platform-web.inxpkg",
            "source": {"type": "github", "location": "https://github.com/ChenlizheMe/infernux_web"},
        }],
    }


def serve(monkeypatch, catalog):
    requests = []

    def urlopen(request, *, timeout):
        requests.append(request.full_url)
        return io.BytesIO(json.dumps(catalog).encode("utf-8"))

    monkeypatch.setattr(official, "urlopen", urlopen)
    return requests


def pinned(project):
    registry = PluginRegistry(str(project))
    source = {"type": "github", "location": "https://github.com/ChenlizheMe/Infernux",
              "subdirectory": "external/plugins/infernux_web", "revision": "old-engine-commit"}
    registry.record_install(
        {"reference": "infernux/platform-web", "version": "0.1.0"}, files=[],
        control={"guid": "a" * 32, "owned": True, "path_hint": "Packages/infernux/platform-web/inx_package.json"},
        source=source, enabled=False,
    )
    return registry


def test_refresh_changes_discovery_not_installed_pin(tmp_path, monkeypatch, catalog):
    registry = pinned(tmp_path / "project")
    installed = registry.installed()
    lock = Path(registry.lock_path).read_bytes()
    requests = serve(monkeypatch, catalog)
    official.refresh_official_registry(str(tmp_path / "project"))
    assert requests == [official.OFFICIAL_REGISTRY_URL]
    assert registry.find("infernux/platform-web")["version"] == "0.2.0"
    assert registry.installed() == installed
    assert Path(registry.lock_path).read_bytes() == lock
    assert (tmp_path / "shared/official-registry.json").is_file()


def test_next_project_uses_shared_discovery_without_network_or_wheel(tmp_path, monkeypatch, catalog):
    serve(monkeypatch, catalog)
    official.refresh_official_registry(str(tmp_path / "first"))
    monkeypatch.setattr(official, "urlopen", lambda *args, **kwargs: pytest.fail("Startup must stay offline"))
    entries = official.sync_official_registry(str(tmp_path / "second"), resources_root=str(tmp_path / "missing-wheel"))
    assert entries[0]["source"]["location"].endswith("/infernux_web")
    assert PluginRegistry(str(tmp_path / "second")).installed() == ()


@pytest.mark.parametrize("invalid", ["duplicate", "intro", "source"])
def test_invalid_refresh_keeps_cached_catalog_and_project_unchanged(tmp_path, monkeypatch, catalog, invalid):
    serve(monkeypatch, catalog)
    registry = pinned(tmp_path / "project")
    official.refresh_official_registry(str(tmp_path / "project"))
    cache = tmp_path / "shared/official-registry.json"
    original_cache = cache.read_bytes()
    original_registry = Path(registry.path).read_bytes()
    if invalid == "duplicate":
        catalog["packages"].append(dict(catalog["packages"][0]))
    elif invalid == "intro":
        catalog["packages"][0]["intros"] = {"en": ["not text"]}
    else:
        catalog["packages"][0]["source"] = {"type": "local", "location": "unsafe"}
    with pytest.raises(official.OfficialCatalogError):
        official.refresh_official_registry(str(tmp_path / "project"))
    assert cache.read_bytes() == original_cache
    assert Path(registry.path).read_bytes() == original_registry


def test_offline_refresh_reports_error_without_damaging_installed_packages(tmp_path, monkeypatch, catalog):
    registry = pinned(tmp_path / "project")
    original = Path(registry.path).read_bytes()

    def unavailable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(official, "urlopen", unavailable)
    with pytest.raises(official.OfficialCatalogError, match="offline"):
        official.refresh_official_registry(str(tmp_path / "project"))
    assert Path(registry.path).read_bytes() == original


def test_refresh_preserves_local_author_catalog_override(tmp_path, monkeypatch, catalog):
    registry = PluginRegistry(str(tmp_path / "project"))
    source = {"type": "local", "location": "Packages/my-authored-web"}
    registry.add_package("infernux/platform-web", source=source, version="custom")
    serve(monkeypatch, catalog)
    official.refresh_official_registry(str(tmp_path / "project"))
    assert registry.find("infernux/platform-web")["source"] == source
    assert registry.find("infernux/platform-web")["version"] == "custom"


def test_legacy_remote_catalog_is_migrated_without_old_subdirectory(tmp_path, monkeypatch, catalog):
    catalog["packages"][0]["source"] = {
        "type": "github", "location": "https://github.com/ChenlizheMe/Infernux",
        "subdirectory": "external/plugins/infernux_web", "revision": "old-commit",
    }
    serve(monkeypatch, catalog)
    result = official.refresh_official_registry(str(tmp_path / "project"))
    assert result[0]["source"]["location"] == "https://github.com/ChenlizheMe/infernux_web"
    assert "revision" not in result[0]["source"]
    assert "subdirectory" not in result[0]["source"]


def test_installed_legacy_repository_resolves_without_rewriting_pin(tmp_path, catalog):
    registry = pinned(tmp_path / "project")
    original = Path(registry.lock_path).read_bytes()
    manager = PluginManager(str(tmp_path / "project"), runtime=True)
    assert manager.release_repository("infernux/platform-web") == "https://github.com/ChenlizheMe/infernux_web"
    assert Path(registry.lock_path).read_bytes() == original


def test_bundled_official_package_can_discover_its_publisher(tmp_path, catalog):
    registry = pinned(tmp_path / "project")
    document = registry.load()
    document["installed"][0]["source"] = {
        "type": "local", "location": "Library/Resources/platform.inxpkg", "official": True,
        "repository": "https://github.com/ChenlizheMe/infernux_web",
    }
    registry.save(document)
    manager = PluginManager(str(tmp_path / "project"), runtime=True)
    assert manager.release_repository("infernux/platform-web") == "https://github.com/ChenlizheMe/infernux_web"


def test_existing_install_is_not_upgraded_by_dependency_resolution(tmp_path, monkeypatch, catalog):
    pinned(tmp_path / "project")
    serve(monkeypatch, catalog)
    official.refresh_official_registry(str(tmp_path / "project"))
    manager = PluginManager(str(tmp_path / "project"), runtime=True)
    reloaded = []
    monkeypatch.setattr(manager, "reload", lambda reference: reloaded.append(reference))
    monkeypatch.setattr(manager, "install_source", lambda *args, **kwargs: pytest.fail("Do not acquire a replacement"))
    manager.install_reference("infernux/platform-web")
    assert reloaded == ["infernux/platform-web"]
    assert manager.registry.installed_record("infernux/platform-web")["version"] == "0.1.0"
