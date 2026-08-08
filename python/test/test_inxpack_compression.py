from __future__ import annotations

import importlib

import pytest


def _native_inxpack():
    for name in ("Infernux.lib._Infernux", "_Infernux"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "_inxpack_write"):
            return module
    pytest.skip("the native InxPack binding is not installed")


def test_inxpack_compression_profile_roundtrip_and_determinism(tmp_path):
    native = _native_inxpack()
    source = tmp_path / "payload.bin"
    source.write_bytes((b"infernux-release-payload\0" * 4096) + bytes(range(256)))
    files = [("Runtime/payload.bin", str(source))]

    development_path = tmp_path / "development.inxrt"
    development_again_path = tmp_path / "development-again.inxrt"
    release_path = tmp_path / "release.inxrt"
    development_manifest = native._inxpack_write(files, str(development_path))
    development_again_manifest = native._inxpack_write(files, str(development_again_path))
    release_manifest = native._inxpack_write(files, str(release_path), profile="release")
    explicit_manifest = native._inxpack_write(files, str(tmp_path / "explicit.inxrt"), compression_level=12)

    assert development_path.read_bytes() == development_again_path.read_bytes()
    assert development_manifest["archive_sha256"] == development_again_manifest["archive_sha256"]
    assert release_manifest["archive_sha256"] == explicit_manifest["archive_sha256"]
    assert native._inxpack_read_entry(str(release_path), "Runtime/payload.bin") == source.read_bytes()


@pytest.mark.parametrize("compression_level", [-1, 0, 23])
def test_inxpack_rejects_invalid_explicit_compression_level(tmp_path, compression_level):
    native = _native_inxpack()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")
    with pytest.raises((ValueError, RuntimeError)):
        native._inxpack_write(
            [("Runtime/payload.bin", str(source))],
            str(tmp_path / "invalid.inxrt"),
            compression_level=compression_level,
        )
