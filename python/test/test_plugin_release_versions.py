from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from Infernux.plugins import github_releases as releases
from Infernux.plugins import PluginManager


@pytest.mark.parametrize("kind", ["metadata", "package", "source"])
def test_release_network_stall_preserves_destination(tmp_path, monkeypatch, kind):
    destination = tmp_path / "download.inxpkg"
    destination.write_bytes(b"previous complete download")

    def stalled(request, *, timeout):
        assert timeout == 30
        raise TimeoutError("network stalled")

    monkeypatch.setattr(releases.urllib.request, "urlopen", stalled)
    with pytest.raises(TimeoutError, match="network stalled"):
        if kind == "metadata":
            releases._request_bytes("https://example.invalid/metadata", accept="application/json")
        elif kind == "package":
            releases._download_asset(
                {"browser_download_url": "https://example.invalid/package"},
                str(destination), progress=None,
            )
        else:
            releases._download_file("https://example.invalid/source", str(destination),
                                    progress=None, start=0, end=1)
    assert destination.read_bytes() == b"previous complete download"
    assert not list(tmp_path.glob("*.part"))


def _release(version, *, engine=">=0.4,<0.5", prerelease=False):
    tag = f"v{version}"
    artifact = {"name": "vendor.plugin.inxpkg", "browser_download_url": f"https://example.invalid/{tag}/package"}
    manifest = {"name": releases.RELEASE_MANIFEST_NAME, "browser_download_url": f"https://example.invalid/{tag}/manifest"}
    document = {
        "tag_name": tag, "prerelease": prerelease,
        "html_url": f"https://github.com/vendor/plugin/releases/tag/{tag}",
        "body": f"Changes in {version}", "assets": [artifact, manifest],
    }
    metadata = {
        "$schema": releases.RELEASE_MANIFEST_SCHEMA,
        "reference": "vendor/plugin", "version": version, "engine": engine,
        "artifact": {"name": artifact["name"]}, "generator": "test",
    }
    return document, metadata


def _serve(monkeypatch, entries):
    calls = []
    documents = {entry[0]["tag_name"]: entry for entry in entries}

    def request(url, *, accept):
        calls.append(url)
        if "/releases/tags/" in url:
            response = documents[url.rsplit("/", 1)[1]][0]
        elif url.startswith("https://api.github.com/"):
            response = [entry[0] for entry in entries]
        elif url.endswith("/manifest"):
            response = documents[url.split("/")[-2]][1]
        else:
            raise AssertionError("Version discovery must not download package payloads")
        return json.dumps(response).encode()

    monkeypatch.setattr(releases, "_request_bytes", request)
    return calls


def test_available_versions_are_sorted_compatible_and_metadata_only(monkeypatch):
    entries = [_release("0.1.0"), _release("0.3.0", engine=">=99"),
               _release("0.2.0"), _release("0.4.0rc1", prerelease=True)]
    _serve(monkeypatch, entries)
    versions = releases.list_github_releases(
        "https://github.com/vendor/plugin", expected_reference="vendor/plugin",
    )
    assert [item["version"] for item in versions] == ["0.2.0", "0.1.0"]
    assert versions[0]["notes"] == "Changes in 0.2.0"
    assert versions[0]["release_tag"] == "v0.2.0"


def test_selected_old_release_is_not_replaced_by_latest(tmp_path, monkeypatch):
    calls = _serve(monkeypatch, [_release("0.1.0"), _release("0.2.0")])
    downloaded = []

    def download(asset, destination, **kwargs):
        downloaded.append(asset["browser_download_url"])
        Path(destination).write_bytes(b"package")

    monkeypatch.setattr(releases, "_download_asset", download)
    monkeypatch.setattr(releases.InxPackage, "inspect", lambda path: SimpleNamespace(
        metadata={"reference": "vendor/plugin", "version": "0.1.0", "engine": ">=0.4,<0.5"},
    ))
    selected = releases.resolve_github_release(
        "https://github.com/vendor/plugin", str(tmp_path),
        expected_reference="vendor/plugin", release_tag="v0.1.0",
    )
    assert selected.source["version"] == "0.1.0"
    assert calls[0].endswith("/releases/tags/v0.1.0")
    assert downloaded == ["https://example.invalid/v0.1.0/package"]


def test_selected_incompatible_release_fails_without_substitution(tmp_path, monkeypatch):
    _serve(monkeypatch, [_release("0.1.0"), _release("0.2.0", engine=">=99")])
    with pytest.raises(RuntimeError, match="No compatible Infernux plugin release"):
        releases.resolve_github_release(
            "https://github.com/vendor/plugin", str(tmp_path),
            expected_reference="vendor/plugin", release_tag="v0.2.0",
        )
    assert list(tmp_path.iterdir()) == []


def test_selected_non_protocol_release_does_not_fall_back_to_source(tmp_path, monkeypatch):
    entry, metadata = _release("0.1.0")
    entry["assets"] = []
    _serve(monkeypatch, [(entry, metadata)])
    with pytest.raises(RuntimeError, match="Selected release has no Infernux plugin manifest"):
        releases.resolve_github_release(
            "https://github.com/vendor/plugin", str(tmp_path), release_tag="v0.1.0",
        )


def test_version_discovery_reads_remaining_release_pages(monkeypatch):
    entry, metadata = _release("0.1.0")
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        if url.endswith("/manifest"):
            value = metadata
        elif "&page=2" in url:
            value = [entry]
        else:
            value = [{"tag_name": f"old-{index}", "assets": []} for index in range(100)]
        return json.dumps(value).encode()

    monkeypatch.setattr(releases, "_request_bytes", request)
    versions = releases.list_github_releases("https://github.com/vendor/plugin")
    assert [item["version"] for item in versions] == ["0.1.0"]
    assert "&page=2" in calls[1]


def test_cached_import_preserves_the_remote_source(tmp_path, monkeypatch):
    manager = PluginManager(str(tmp_path), runtime=True)
    source = {"type": "github", "location": "https://github.com/vendor/plugin"}
    manager.registry.add_package("vendor/plugin", source=source, version="0.1.0")
    monkeypatch.setattr(manager, "cached_reference_path", lambda ref: "cached.inxpkg")
    installed = []
    monkeypatch.setattr(manager, "install_package", lambda path, **kwargs: installed.append((path, kwargs)))
    manager.install_reference("vendor/plugin", install_dependencies=False)
    assert installed[0][0] == "cached.inxpkg"
    assert installed[0][1]["source"] == source


def test_checking_installed_versions_does_not_change_the_project_pin(tmp_path, monkeypatch):
    manager = PluginManager(str(tmp_path), runtime=True)
    source = {"type": "github", "location": "https://github.com/vendor/plugin"}
    manager.registry.add_package("vendor/plugin", source=source, version="0.1.0")
    monkeypatch.setattr(manager.registry, "installed_record", lambda ref: {
        "reference": ref, "version": "0.1.0", "source": source,
    })
    _serve(monkeypatch, [_release("0.1.0"), _release("0.2.0")])
    before = Path(manager.registry.path).read_bytes()
    versions = manager.available_releases("vendor/plugin")
    assert versions[0]["version"] == "0.2.0"
    assert Path(manager.registry.path).read_bytes() == before


def test_local_author_package_does_not_query_an_official_remote(tmp_path, monkeypatch):
    manager = PluginManager(str(tmp_path), runtime=True)
    manager.registry.add_package("vendor/plugin", source={
        "type": "github", "location": "https://github.com/vendor/plugin",
    })
    monkeypatch.setattr(manager.registry, "installed_record", lambda ref: {
        "reference": ref, "source": {"type": "local", "location": "author/package"},
    })
    monkeypatch.setattr(releases, "_request_bytes", lambda *args, **kwargs: pytest.fail("Local author source must not query the network"))
    assert manager.available_releases("vendor/plugin") == ()
