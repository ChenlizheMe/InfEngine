"""Strict GitHub Release-first acquisition for InxPackage repositories."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote, urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from Infernux.version import ENGINE_VERSION

from .package import InxPackage, PACKAGE_EXTENSION, validate_reference
RELEASE_MANIFEST_NAME = "infernux-plugin-release.json"
RELEASE_MANIFEST_SCHEMA = "infernux.plugin_release"
_CHUNK_BYTES = 1024 * 1024
_Progress = Callable[[str, float, str], None]


@dataclass(frozen=True, slots=True)
class GitHubReleasePackage:
    path: str
    source: dict[str, object]


@dataclass(frozen=True, slots=True)
class GitHubSourceSnapshot:
    root: str
    commit: str


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
                    progress(
                        "download_package",
                        0.08 + 0.22 * fraction,
                        _download_size_text(received, total),
                    )
        os.replace(partial, destination)
    finally:
        try:
            os.remove(partial)
        except FileNotFoundError:
            pass


def _download_size_text(received: int, total: int) -> str:
    def size(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if amount < 1024.0 or unit == "GiB":
                return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024.0
        raise AssertionError("unreachable")

    return f"{size(received)} / {size(total)}" if total else size(received)


def _download_file(
    url: str,
    destination: str,
    *,
    progress: _Progress | None,
    start: float,
    end: float,
) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"Infernux/{ENGINE_VERSION}"},
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
                    progress(
                        "download_source",
                        start + (end - start) * fraction,
                        _download_size_text(received, total),
                    )
        os.replace(partial, destination)
    finally:
        try:
            os.remove(partial)
        except FileNotFoundError:
            pass


def download_github_source(
    location: str,
    destination_root: str,
    *,
    revision: str = "",
    subdirectory: str = "",
    progress: _Progress | None = None,
) -> GitHubSourceSnapshot:
    """Download one exact GitHub source snapshot after Release resolution fails."""

    owner, repository = _repository_coordinates(location)
    requested_revision = str(revision).strip() or "HEAD"
    commit_url = (
        f"https://api.github.com/repos/{owner}/{repository}/commits/"
        f"{quote(requested_revision, safe='')}"
    )
    commit_payload = _request_bytes(commit_url, accept="application/vnd.github+json")
    try:
        commit_document = json.loads(commit_payload.decode("utf-8"))
        commit = str(commit_document["sha"]).casefold()
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("GitHub source revision response is invalid") from exc
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise RuntimeError("GitHub source revision did not resolve to a commit SHA")

    destination = Path(destination_root)
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = str(destination / "source.tar.gz")
    _download_file(
        f"https://codeload.github.com/{owner}/{repository}/tar.gz/{commit}",
        archive_path,
        progress=progress,
        start=0.08,
        end=0.24,
    )

    checkout = destination / "checkout"
    checkout.mkdir(parents=False, exist_ok=False)
    selected = tuple(part for part in str(subdirectory).replace("\\", "/").strip("/").split("/") if part)
    extracted = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            parts = tuple(part for part in Path(member.name).parts if part not in {"", "."})
            if len(parts) < 2 or any(part == ".." for part in parts):
                continue
            relative_parts = parts[1:]
            if selected:
                if relative_parts[: len(selected)] != selected:
                    continue
                relative_parts = relative_parts[len(selected) :]
            if not relative_parts:
                continue
            target = checkout.joinpath(*relative_parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError(
                    f"GitHub source snapshot contains an unsupported entry: {member.name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError(f"GitHub source snapshot entry is unreadable: {member.name}")
            with stream, target.open("wb") as output:
                shutil.copyfileobj(stream, output, length=_CHUNK_BYTES)
            os.chmod(target, member.mode & 0o777)
            extracted += 1
    os.remove(archive_path)
    if not extracted:
        suffix = f"/{'/'.join(selected)}" if selected else ""
        raise RuntimeError(f"GitHub source snapshot contains no files at {repository}{suffix}")
    if progress is not None:
        progress("read_repository", 0.28, f"{extracted} files")
    return GitHubSourceSnapshot(str(checkout), commit)


def _github_release_candidates(
    location: str,
    *,
    expected_reference: str = "",
    release_tag: str = "",
) -> list[tuple[Version, dict[str, object], Mapping[str, object], Mapping[str, object]]] | None:
    """Read compatible release metadata without downloading plugin payloads."""

    owner, repository = _repository_coordinates(location)
    api_root = f"https://api.github.com/repos/{owner}/{repository}/releases"
    releases = []
    page = 1
    while True:
        api_url = (
            f"{api_root}/tags/{quote(release_tag, safe='')}"
            if release_tag else
            f"{api_root}?per_page=100" + (f"&page={page}" if page > 1 else "")
        )
        payload = _request_bytes(api_url, accept="application/vnd.github+json")
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("GitHub releases response is invalid JSON") from exc
        if release_tag:
            if not isinstance(document, Mapping) or document.get("tag_name") != release_tag:
                raise RuntimeError("GitHub release response does not match the selected tag")
            releases.append(document)
            break
        if not isinstance(document, list):
            raise RuntimeError("GitHub releases response is not a list")
        releases.extend(document)
        if len(document) < 100:
            break
        page += 1

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
        if release_tag:
            raise RuntimeError(f"Selected release has no Infernux plugin manifest: {release_tag}")
        return None
    if not candidates:
        detail = "; ".join(errors) or "no compatible stable release"
        raise RuntimeError(f"No compatible Infernux plugin release: {detail}")

    return sorted(candidates, key=lambda item: item[0], reverse=True)


def list_github_releases(
    location: str, *, expected_reference: str = ""
) -> tuple[dict[str, object], ...]:
    """List compatible stable versions and notes without changing an installation."""

    candidates = _github_release_candidates(location, expected_reference=expected_reference)
    return tuple(
        {
            "reference": selected["reference"],
            "version": str(version),
            "engine": selected["engine"],
            "release_tag": str(release["tag_name"]),
            "release_url": str(release.get("html_url", "")),
            "notes": str(release.get("body") or ""),
        }
        for version, selected, _asset, release in candidates or ()
    )


def resolve_github_release(
    location: str,
    destination_root: str,
    *,
    expected_reference: str = "",
    release_tag: str = "",
    progress: _Progress | None = None,
) -> GitHubReleasePackage | None:
    """Download the selected tag, or the highest compatible stable release.

    Only unpinned repositories without the Infernux protocol may return None
    for explicit source acquisition. A selected version is never substituted.
    """

    candidates = _github_release_candidates(
        location, expected_reference=expected_reference, release_tag=release_tag,
    )
    if candidates is None:
        return None
    version, selected, asset, release = candidates[0]
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
    "download_github_source",
    "GitHubReleasePackage",
    "GitHubSourceSnapshot",
    "RELEASE_MANIFEST_NAME",
    "RELEASE_MANIFEST_SCHEMA",
    "release_manifest_name",
    "list_github_releases",
    "resolve_github_release",
]
