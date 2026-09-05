from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from Infernux import lib
from Infernux.engine import player_build_preflight as preflight
from Infernux.host.commands import MainThreadCommandQueue


@pytest.mark.parametrize("background", [False, True])
def test_active_catalog_publication_runs_on_database_owner(tmp_path, monkeypatch, background):
    (tmp_path / "Assets").mkdir()
    database = lib.AssetDatabase()
    database.initialize(str(tmp_path))
    monkeypatch.setattr(lib, "AssetRegistry", SimpleNamespace(
        instance=lambda: SimpleNamespace(get_asset_database=lambda: database)
    ))
    queue = MainThreadCommandQueue()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)
    queue.drain()
    owner = threading.get_ident()
    observed = []

    def publish(root, active):
        assert Path(root) == tmp_path
        assert active is database
        active.refresh()
        observed.append(threading.get_ident())
        return {"entries": ({"guid": "a" * 32},)}

    monkeypatch.setattr(preflight, "publish_player_asset_catalog", publish)
    if background:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(preflight.publish_player_asset_catalog_for_host, str(tmp_path))
            try:
                deadline = time.monotonic() + 5
                while not future.done() and time.monotonic() < deadline:
                    queue.drain()
                    time.sleep(0.001)
                result = future.result(timeout=1)
            finally:
                queue.cancel_pending()
    else:
        result = preflight.publish_player_asset_catalog_for_host(str(tmp_path))
    assert observed == [owner]
    assert result["entries"][0]["guid"] == "a" * 32


def test_database_owned_by_headless_worker_publishes_without_gui_queue(tmp_path, monkeypatch):
    (tmp_path / "Assets").mkdir()
    observed = []

    def publish(root, database):
        database.refresh()
        observed.append(threading.get_ident())
        return {"entries": ()}

    monkeypatch.setattr(preflight, "publish_player_asset_catalog", publish)
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", MainThreadCommandQueue())

    def headless():
        database = lib.AssetDatabase()
        database.initialize(str(tmp_path))
        monkeypatch.setattr(lib, "AssetRegistry", SimpleNamespace(
            instance=lambda: SimpleNamespace(get_asset_database=lambda: database)
        ))
        return preflight.publish_player_asset_catalog_for_host(str(tmp_path))

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(headless).result(timeout=5) == {"entries": ()}
    assert len(observed) == 1 and observed[0] != threading.get_ident()
