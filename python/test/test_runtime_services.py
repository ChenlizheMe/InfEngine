from __future__ import annotations

import pytest

from Infernux.runtime_services import (
    get_runtime_service,
    install_runtime_service,
    remove_runtime_service,
)


def test_runtime_service_installation_is_identity_guarded() -> None:
    first = object()
    second = object()
    remove_runtime_service("test-service")
    try:
        install_runtime_service(" Test-Service ", first)
        install_runtime_service("test-service", first)
        assert get_runtime_service("TEST-SERVICE") is first
        with pytest.raises(RuntimeError, match="already installed"):
            install_runtime_service("test-service", second)
        assert not remove_runtime_service("test-service", second)
        assert remove_runtime_service("test-service", first)
        assert get_runtime_service("test-service") is None
    finally:
        remove_runtime_service("test-service")
