from __future__ import annotations

import json
import sys
import threading

import pytest

from Infernux.core.document_store import DocumentStore
from Infernux.plugins.registry import PluginRegistry


def test_registry_and_lock_use_the_shared_document_writer(tmp_path):
    registry = PluginRegistry(str(tmp_path))
    document = registry.load()
    registry.save(document)
    store = DocumentStore.instance()
    for path in (registry.path, registry.lock_path):
        metrics = store.get_metrics(path)
        assert metrics.latest_succeeded_generation > 0
        with open(path, encoding="utf-8") as stream:
            assert isinstance(json.load(stream), dict)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows delete-sharing contract")
@pytest.mark.parametrize("attribute", ("path", "lock_path"))
def test_registry_publication_survives_a_brief_windows_reader(tmp_path, attribute):
    registry = PluginRegistry(str(tmp_path))
    document = registry.load()
    registry.save(document)
    # Python's ordinary Windows file reader does not share delete access.
    # Exercise the actual OS sharing violation, not a mocked PermissionError.
    stream = open(getattr(registry, attribute), encoding="utf-8")
    release = threading.Timer(0.1, stream.close)
    release.start()
    try:
        registry.save(document)
    finally:
        release.join()
        stream.close()
    assert registry.load() == document
