from __future__ import annotations

import os
from pathlib import Path

import pytest

from Infernux.engine.path_utils import (
    is_path_within,
    lexical_path,
    lexical_path_key,
    path_key,
    portable_path,
    relative_path,
    resolved_path,
    safe_path,
    same_path,
)


def test_path_helpers_separate_storage_paths_from_identity_keys(tmp_path: Path):
    source = tmp_path / "identity.txt"
    source.write_text("identity", encoding="utf-8")

    resolved = resolved_path(str(source))
    assert os.path.isabs(resolved)
    assert safe_path(str(source)) == resolved
    assert path_key(str(source)) == os.path.normcase(resolved)
    assert lexical_path_key(str(source)) == os.path.normcase(os.path.abspath(str(source)))
    assert same_path(str(source), resolved)
    assert not same_path("", "")
    assert lexical_path(source) == os.path.abspath(str(source))


def test_containment_uses_components_and_relative_paths_are_portable(tmp_path: Path):
    root = tmp_path / "Project"
    asset = root / "Assets" / "folder" / "asset.txt"
    sibling = tmp_path / "Project-copy" / "asset.txt"
    asset.parent.mkdir(parents=True)
    asset.write_text("asset", encoding="utf-8")

    assert is_path_within(asset, root)
    assert is_path_within(root, root)
    assert not is_path_within(root, root, allow_root=False)
    assert not is_path_within(sibling, root)
    assert relative_path(asset, root) == "Assets/folder/asset.txt"
    assert relative_path(root, root, allow_root=True) == "."
    assert portable_path(r"Assets\folder\asset.txt") == "Assets/folder/asset.txt"
    with pytest.raises(ValueError):
        relative_path(sibling, root)


def test_same_path_recognizes_hard_links(tmp_path: Path):
    source = tmp_path / "source.txt"
    alias = tmp_path / "alias.txt"
    source.write_text("identity", encoding="utf-8")
    try:
        os.link(source, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    assert same_path(source, alias)


def test_containment_resolves_symlink_escape(tmp_path: Path):
    root = tmp_path / "Project"
    outside = tmp_path / "Outside"
    root.mkdir()
    outside.mkdir()
    link = root / "linked-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    assert not is_path_within(link / "asset.txt", root)


def test_lexical_identity_stays_stable_for_deleted_paths(tmp_path: Path):
    root = tmp_path / "Unicode-路径"
    root.mkdir()
    target = root / "deleted.txt"
    key_before_creation = lexical_path_key(target)
    target.write_text("temporary", encoding="utf-8")
    target.unlink()

    assert lexical_path_key(target) == key_before_creation
    assert is_path_within(target, root)
    assert relative_path(target, root, resolve=False) == "deleted.txt"


def test_relative_path_rejects_lexical_parent_escape(tmp_path: Path):
    root = tmp_path / "Project"
    root.mkdir()
    with pytest.raises(ValueError):
        relative_path(root / "Assets" / ".." / ".." / "outside.txt", root, resolve=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows short-path identity regression")
def test_existing_windows_short_path_resolves_to_long_storage_path(tmp_path: Path):
    import ctypes

    source = tmp_path / "p.txt"
    source.write_text("short path", encoding="utf-8")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(source), buffer, len(buffer))
    if not length or buffer.value == str(source):
        pytest.skip("8.3 short paths are unavailable on this volume")

    assert resolved_path(buffer.value) == resolved_path(str(source))
    assert same_path(buffer.value, str(source))
