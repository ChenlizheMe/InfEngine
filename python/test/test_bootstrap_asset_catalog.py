from types import SimpleNamespace

import pytest

from Infernux.engine.bootstrap import EditorBootstrap


class _Database:
    def __init__(self, pending: bool):
        self.refresh_pending = pending
        self.completions = 0

    def complete_pending_refresh(self):
        self.completions += 1


def _bootstrap(database):
    bootstrap = EditorBootstrap.__new__(EditorBootstrap)
    bootstrap.engine = SimpleNamespace(get_asset_database=lambda: database)
    return bootstrap


@pytest.mark.parametrize("pending,expected", [(True, 1), (False, 0)])
def test_initial_asset_catalog_barrier_completes_only_pending_refresh(
    pending, expected
):
    database = _Database(pending)

    _bootstrap(database)._complete_initial_asset_catalog()

    assert database.completions == expected


def test_initial_asset_catalog_barrier_precedes_scene_and_icon_consumers():
    import inspect

    source = inspect.getsource(EditorBootstrap.run)
    barrier = source.index("self._complete_initial_asset_catalog()")
    managers = source.index("self._create_managers()")
    scene = source.index("self._load_initial_scene()")
    assert barrier < managers < scene


def test_initial_asset_catalog_barrier_propagates_publication_failure():
    class _RejectingDatabase(_Database):
        def complete_pending_refresh(self):
            raise RuntimeError("catalog rejected")

    with pytest.raises(RuntimeError, match="catalog rejected"):
        _bootstrap(_RejectingDatabase(True))._complete_initial_asset_catalog()
