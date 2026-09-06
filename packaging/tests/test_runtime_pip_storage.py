from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import zipfile

import pytest

PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import embed_runtime_manager as runtime
import stage_bundled_python_runtime as stage


@pytest.mark.parametrize("explicit", (False, True))
def test_builder_subprocesses_use_owned_pip_cache(tmp_path, monkeypatch, explicit):
    shared = tmp_path / "Shared"
    monkeypatch.setenv("INFERNUX_SHARED_DATA_ROOT", str(shared))
    monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
    expected = shared / "Cache/Python/Pip"
    if explicit:
        expected = tmp_path / "AuthorCache"
        monkeypatch.setenv("PIP_CACHE_DIR", str(expected))
    before = dict(os.environ)
    assert stage._child_env()["PIP_CACHE_DIR"] == str(expected)
    assert dict(os.environ) == before


def test_builder_package_install_does_not_disable_cache(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(stage, "_find_python_in_root", lambda _root: sys.executable)
    monkeypatch.setattr(stage, "_ensure_pip", lambda _python: None)
    monkeypatch.setattr(stage, "_has_modules", lambda *args: True)

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(stage, "_run", run)
    stage._ensure_builder_packages(str(tmp_path))
    assert len(calls) == 1
    assert "--no-cache-dir" not in calls[0]


def test_managed_runtime_reuses_download_with_server_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("INFERNUX_SHARED_DATA_ROOT", str(tmp_path / "Shared"))
    monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
    monkeypatch.delenv("PIP_NO_CACHE_DIR", raising=False)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_TRUSTED_HOST", "127.0.0.1")
    monkeypatch.setenv("PIP_RETRIES", "0")
    monkeypatch.setenv("PIP_TIMEOUT", "2")
    wheel_data = io.BytesIO()
    with zipfile.ZipFile(wheel_data, "w") as archive:
        archive.writestr("inx_runtime_cache_probe.py", "VALUE = 'owned download'\n")
        archive.writestr("inx_runtime_cache_probe-1.0.dist-info/METADATA",
                        "Metadata-Version: 2.1\nName: inx-runtime-cache-probe\nVersion: 1.0\n")
        archive.writestr("inx_runtime_cache_probe-1.0.dist-info/WHEEL",
                        "Wheel-Version: 1.0\nGenerator: runtime-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("inx_runtime_cache_probe-1.0.dist-info/RECORD", "")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            payload = wheel_data.getvalue()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "public, max-age=31536000")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/inx_runtime_cache_probe-1.0-py3-none-any.whl"
    monkeypatch.setattr(runtime, "_RUNTIME_PACKAGES", (url,))
    monkeypatch.setattr(runtime, "_REQUIRED_RUNTIME_MODULES", ("inx_runtime_cache_probe",))
    manager = runtime.PythonRuntimeManager(runtime_dir=str(tmp_path / "Shared/Runtimes"))

    def install(name):
        target = tmp_path / name
        # Redirect only the installation destination; real pip and real module
        # detection run in subprocesses without changing this Python environment.
        monkeypatch.setattr(runtime, "_site_packages_root", lambda *args: str(target))
        monkeypatch.setenv("PYTHONPATH", str(target))
        manager._ensure_runtime_packages(sys.executable)
        assert (target / "inx_runtime_cache_probe.py").read_text() == "VALUE = 'owned download'\n"

    try:
        install("first-runtime")
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    install("second-runtime")
    assert len(requests) == 1
    assert (tmp_path / "Shared/Cache/Python/Pip").is_dir()
