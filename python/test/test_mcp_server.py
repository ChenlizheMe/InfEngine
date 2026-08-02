from __future__ import annotations


def test_embedded_mcp_server_does_not_require_a_console(monkeypatch):
    import sys

    from Infernux.mcp import server as module

    calls = []

    class FakeServer:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    module._run_http_transport(
        FakeServer(), "streamable-http", "127.0.0.1", 9713
    )

    assert calls == [
        {
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": 9713,
            "show_banner": False,
            "uvicorn_config": {"log_config": None},
        }
    ]
