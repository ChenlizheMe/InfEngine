from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import zipfile

import pytest

from Infernux.plugins.manager import PluginManager


@pytest.mark.parametrize("shared", [False, True])
def test_real_pip_uses_managed_cache_without_changing_parent(tmp_path, monkeypatch, shared):
    project = tmp_path / "Project"
    project.mkdir()
    hub = tmp_path / "Hub/Shared"
    monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
    monkeypatch.delenv("PIP_NO_CACHE_DIR", raising=False)
    if shared:
        monkeypatch.setenv("INFERNUX_SHARED_DATA_ROOT", str(hub))
    else:
        monkeypatch.delenv("INFERNUX_SHARED_DATA_ROOT", raising=False)
    manager = PluginManager(str(project))
    before = dict(os.environ)

    result = manager._run_process([sys.executable, "-m", "pip", "cache", "dir"], cwd=str(project))

    assert Path(result.stdout.strip()) == (hub if shared else project) / "Cache/Python/Pip"
    assert dict(os.environ) == before
    assert list((project / "Cache/Plugins/.staging").iterdir()) == []


@pytest.mark.parametrize("failure", [False, True])
def test_pip_temporary_build_files_are_project_owned_and_removed(tmp_path, monkeypatch, failure):
    manager = PluginManager(str(tmp_path / "Project"))
    explicit = str(tmp_path / "ExplicitCache")
    monkeypatch.setenv("PIP_CACHE_DIR", explicit)
    workspaces = []

    def run(command, **kwargs):
        env = kwargs["env"]
        workspace = Path(env["TMPDIR"])
        assert workspace.is_relative_to(tmp_path / "Project/Cache/Plugins/.staging")
        assert env["TEMP"] == env["TMP"] == str(workspace)
        assert env["PIP_CACHE_DIR"] == explicit
        (workspace / "build-environment").mkdir()
        (workspace / "build-environment/output.whl").write_bytes(b"build output")
        workspaces.append(workspace)
        if failure:
            raise OSError("process launch failed")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", run)
    if failure:
        with pytest.raises(OSError, match="process launch failed"):
            manager._run_process([sys.executable, "-m", "pip", "install", "example"])
    else:
        manager._run_process([sys.executable, "-m", "pip", "install", "example"])
    assert len(workspaces) == 1 and not workspaces[0].exists()


def test_git_commands_do_not_receive_pip_environment(tmp_path, monkeypatch):
    manager = PluginManager(str(tmp_path / "Project"))

    def run(command, **kwargs):
        assert kwargs["env"] is None
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", run)
    manager._run_process(["git", "rev-parse", "HEAD"])
    assert not (tmp_path / "Project/Cache/Plugins/.staging").exists()


@pytest.mark.parametrize("failure", [False, True])
def test_requirement_files_live_in_project_transaction(tmp_path, monkeypatch, failure):
    manager = PluginManager(str(tmp_path / "Project"))
    paths = []

    def run(command, **kwargs):
        path = Path(command[command.index("-r") + 1])
        assert path.is_relative_to(tmp_path / "Project/Cache/Plugins/.staging")
        assert path.read_text(encoding="utf-8") == "example==1\nother>=2\n"
        paths.append(path)
        if failure:
            raise RuntimeError("pip failed")

    monkeypatch.setattr(manager, "_run_process", run)
    if failure:
        with pytest.raises(RuntimeError, match="pip failed"):
            manager._run_pip_requirement_file(["example==1", "other>=2\n"], executable=sys.executable)
    else:
        manager._run_pip_requirement_file(["example==1", "other>=2\n"], executable=sys.executable)
    assert len(paths) == 1 and not paths[0].parent.exists()


def test_two_projects_reuse_a_real_pip_download_offline(tmp_path, monkeypatch):
    """Install into isolated targets, never the test interpreter's site-packages."""
    monkeypatch.setenv("INFERNUX_SHARED_DATA_ROOT", str(tmp_path / "Hub/Shared"))
    monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
    monkeypatch.delenv("PIP_NO_CACHE_DIR", raising=False)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr("inx_cache_probe.py", "VALUE = 'shared download'\n")
        wheel.writestr("inx_cache_probe-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: inx-cache-probe\nVersion: 1.0\n")
        wheel.writestr("inx_cache_probe-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: storage-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr("inx_cache_probe-1.0.dist-info/RECORD", "")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            data = buffer.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=31536000")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/inx_cache_probe-1.0-py3-none-any.whl"

    def install(project):
        project.mkdir()
        manager = PluginManager(str(project))
        target = project / "Cache/ProbePython"
        manager._run_process([
            sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
            "--disable-pip-version-check", "--trusted-host", "127.0.0.1",
            "--retries", "0", "--timeout", "2", "--target", str(target), url,
        ], cwd=str(project))
        assert (target / "inx_cache_probe.py").read_text() == "VALUE = 'shared download'\n"
        assert not list((project / "Cache/Plugins/.staging").iterdir())

    try:
        install(tmp_path / "FirstProject")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    # The source server is now offline: the second project must use Hub cache.
    install(tmp_path / "SecondProject")
    assert len(requests) == 1
