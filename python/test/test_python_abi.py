from __future__ import annotations

import pytest

from Infernux.engine.python_abi import (
    BOOTSTRAP_NATIVE_MANIFEST_SCHEMA,
    player_native_library_filenames,
)


def test_player_bootstrap_manifest_uses_the_current_schema() -> None:
    assert BOOTSTRAP_NATIVE_MANIFEST_SCHEMA == "infernux.player-bootstrap-native"


def test_windows_player_native_library_contract_is_complete() -> None:
    assert player_native_library_filenames("win32") == frozenset(
        {
            "InfernuxFoundation.dll",
            "InfernuxParticleRuntime.dll",
            "InfernuxRenderCore.dll",
            "InfernuxRendererRuntime.dll",
            "InfernuxShaderCompiler.dll",
            "InfernuxVulkanBackend.dll",
            "assimp-vc143-mt.dll",
            "Jolt.dll",
            "SDL3.dll",
        }
    )


def test_linux_player_native_library_contract_is_complete() -> None:
    assert player_native_library_filenames("linux") == frozenset(
        {
            "libInfernuxFoundation.so",
            "libInfernuxParticleRuntime.so",
            "libInfernuxRenderCore.so",
            "libInfernuxRendererRuntime.so",
            "libInfernuxShaderCompiler.so",
            "libInfernuxVulkanBackend.so",
            "libassimp.so",
            "libJolt.so",
            "libSDL3.so",
        }
    )


def test_player_native_library_contract_rejects_unsupported_platforms() -> None:
    with pytest.raises(RuntimeError, match="unsupported"):
        player_native_library_filenames("darwin")
