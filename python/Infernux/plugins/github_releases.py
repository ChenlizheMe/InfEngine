"""Strict GitHub Release-first acquisition for InxPackage repositories."""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from Infernux.version import ENGINE_VERSION

from .package import InxPackage, PACKAGE_EXTENSION, validate_reference
RELEASE_MANIFEST_NAME = "infernux-plugin-release.json"
RELEASE_MANIFEST_SCHEMA = "infernux.plugin_release"
_CHUNK_BYTES = 1024 * 1024
_Progress = Callable[[str, float], None]


@dataclass(frozen=True, slots=True)
class GitHubReleasePackage:
    path: str
    source: dict[str, object]


def release_manifest_name(reference: str = "") -> str:
    """Return the dedicated-repository or multi-package release filename."""

    value = str(reference).strip()
    if not value:
        return RELEASE_MANIFEST_NAME
    return f"{validate_reference(value).replace('/', '.')}.release.json"


def _request_bytes(url: str, *, accept: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": f"Infernux/{ENGINE_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request) as response:
        return response.read()


def _repository_coordinates(location: str) -> tuple[str, str]:
    parsed = urlsplit(str(location).strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() not in {
        "github.com",
        "www.github.com",
    }:
        raise ValueError("GitHub plugin source must be an https://github.com URL")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("GitHub plugin source must identify one repository")
    owner, repository = parts
    if repository.casefold().endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise ValueError("GitHub plugin source must identify one repository")
    return owner, repository


def _release_manifest(asset: Mapping[str, object]) -> dict[str, object]:
    payload = _request_bytes(
        str(asset.get("browser_download_url", "")),
        accept="application/octet-stream",
    )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("GitHub plugin release manifest is invalid JSON") from exc
    required = {"$schema", "reference", "version", "engine", "artifact", "generator"}
    if not isinstance(document, dict) or document.get("$schema") != RELEASE_MANIFEST_SCHEMA:
        raise RuntimeError("Unsupported GitHub plugin release manifest")
    if frozenset(document) not in {frozenset(required), frozenset(required | {"release_tag"})}:
        raise RuntimeError("Unsupported GitHub plugin release manifest")
    return document


def _validated_candidate(
    release: Mapping[str, object],
    manifest: Mapping[str, object],
    assets: Mapping[str, Mapping[str, object]],
    *,
    expected_reference: str,
) -> tuple[Version, dict[str, object], Mapping[str, object]]:
    reference = validate_reference(str(manifest.get("reference", "")))
    if expected_reference and reference.casefold() != expected_reference.casefold():
        raise RuntimeError(
            f"GitHub release reference mismatch: expected {expected_reference}, found {reference}"
        )
    raw_version = str(manifest.get("version", "")).strip()
    try:
        version = Version(raw_version)
    except InvalidVersion as exc:
        raise RuntimeError(f"Invalid GitHub plugin release version: {raw_version}") from exc
    tag = str(release.get("tag_name", "")).strip()
    expected_tag = str(manifest.get("release_tag", "")).strip() or f"v{raw_version}"
    if tag != expected_tag:
        raise RuntimeError(
            f"GitHub release tag {tag!r} does not match manifest tag {expected_tag!r}"
        )
    engine = str(manifest.get("engine", "")).strip()
    try:
        compatible = not engine or Version(ENGINE_VERSION) in SpecifierSet(engine)
    except InvalidSpecifier as exc:
        raise RuntimeError(f"Invalid GitHub plugin engine range: {engine}") from exc
    artifact = manifest.get("artifact")
    if not isinstance(artifact, Mapping):
        raise RuntimeError("GitHub plugin release manifest has no artifact")
    name = str(artifact.get("name", "")).strip()
    if (
        not name.casefold().endswith(PACKAGE_EXTENSION)
        or name not in assets
    ):
        raise RuntimeError("GitHub plugin release artifact metadata is invalid")
    normalized = {
        "reference": reference,
        "version": raw_version,
        "engine": engine,
        "compatible": compatible,
        "artifact_name": name,
    }
    return version, normalized, assets[name]


def _download_asset(
    asset: Mapping[str, object],
    destination: str,
    *,
    progress: _Progress | None,
) -> None:
    request = urllib.request.Request(
        str(asset.get("browser_download_url", "")),
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": f"Infernux/{ENGINE_VERSION}",
        },
    )
    partial = destination + f".{uuid.uuid4().hex}.part"
    try:
        with urllib.request.urlopen(request) as response, open(partial, "wb") as stream:
            total = int(response.headers.get("Content-Length", "0") or 0)
            received = 0
            while True:
                chunk = response.read(_CHUNK_BYTES)
                if not chunk:
                    break
                stream.write(chunk)
                received += len(chunk)
                fraction = min(1.0, received / total) if total else 0.0
                if progress is not None:
                    progress("download_package", 0.08 + 0.22 * fraction)
        os.replace(partial, destination)
    finally:
        try:
            os.remove(partial)
        except FileNotFoundError:
            pass


def resolve_github_release(
    location: str,
    destination_root: str,
    *,
    expected_reference: str = "",
    progress: _Progress | None = None,
) -> GitHubReleasePackage | None:
    """Return the highest compatible stable protocol release, or ``None``.

    ``None`` means the repository has no Infernux release protocol at all and
    may use the explicit source-snapshot fallback. An invalid or incompatible
    protocol release raises and never falls through to repository HEAD.
    """

    owner, repository = _repository_coordinates(location)
    api_url = f"https://api.github.com/repos/{owner}/{repository}/releases?per_page=100"
    payload = _request_bytes(api_url, accept="application/vnd.github+json")
    try:
        releases = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("GitHub releases response is invalid JSON") from exc
    if not isinstance(releases, list):
        raise RuntimeError("GitHub releases response is not a list")

    protocol_seen = False
    candidates: list[
        tuple[Version, dict[str, object], Mapping[str, object], Mapping[str, object]]
    ] = []
    errors: list[str] = []
    for release in releases:
        if not isinstance(release, Mapping) or bool(release.get("draft", False)):
            continue
        assets = {
            str(asset.get("name", "")): asset
            for asset in release.get("assets", [])
            if isinstance(asset, Mapping) and str(asset.get("name", ""))
        }
        scoped_manifest_name = release_manifest_name(expected_reference)
        manifest_name = (
            scoped_manifest_name
            if scoped_manifest_name in assets
            else RELEASE_MANIFEST_NAME
        )
        manifest_asset = assets.get(manifest_name)
        package_assets = [
            name for name in assets if name.casefold().endswith(PACKAGE_EXTENSION)
        ]
        if manifest_asset is None and not package_assets:
            continue
        protocol_seen = True
        if manifest_asset is None:
            errors.append(
                f"{release.get('tag_name', '(untagged)')}: missing {scoped_manifest_name} "
                f"or {RELEASE_MANIFEST_NAME}"
            )
            continue
        try:
            manifest = _release_manifest(manifest_asset)
            version, normalized, artifact = _validated_candidate(
                release,
                manifest,
                assets,
                expected_reference=expected_reference,
            )
            if not bool(normalized["compatible"]):
                errors.append(
                    f"{release.get('tag_name', '(untagged)')}: requires Infernux "
                    f"{normalized['engine']}"
                )
                continue
            if version.is_prerelease or bool(release.get("prerelease", False)):
                continue
            candidates.append((version, normalized, artifact, release))
        except Exception as exc:
            errors.append(
                f"{release.get('tag_name', '(untagged)')}: {type(exc).__name__}: {exc}"
            )

    if not protocol_seen:
        return None
    if not candidates:
        detail = "; ".join(errors) or "no compatible stable release"
        raise RuntimeError(f"No compatible Infernux plugin release: {detail}")

    version, selected, asset, release = max(candidates, key=lambda item: item[0])
    Path(destination_root).mkdir(parents=True, exist_ok=True)
    destination = str(Path(destination_root) / str(selected["artifact_name"]))
    _download_asset(
        asset,
        destination,
        progress=progress,
    )
    preview = InxPackage.inspect(destination)
    metadata = preview.metadata
    if (
        str(metadata.get("reference", "")).casefold()
        != str(selected["reference"]).casefold()
        or str(metadata.get("version", "")) != str(version)
        or str(metadata.get("engine", "")) != str(selected["engine"])
    ):
        raise RuntimeError("GitHub release artifact does not match its manifest")
    return GitHubReleasePackage(
        destination,
        {
            "type": "github-release",
            "location": str(location),
            "release_tag": str(release.get("tag_name", "")),
            "release_url": str(release.get("html_url", "")),
            "version": str(version),
        },
    )


__all__ = [
    "GitHubReleasePackage",
    "RELEASE_MANIFEST_NAME",
    "RELEASE_MANIFEST_SCHEMA",
    "release_manifest_name",
    "resolve_github_release",
]
