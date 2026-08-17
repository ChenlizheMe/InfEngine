from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _restore_scene_manager_runtime_state():
    from Infernux.scene import SceneManager

    fields = (
        "_pending_scene_load",
        "_active_scene_transaction",
        "_active_scene_load_path",
        "_active_scene_file_manager",
        "_active_scene_wait_for_ready",
        "_active_scene_hold_for_activation",
        "_scene_load_generation",
        "_active_scene_load_generation",
        "_runtime_scene_service",
    )
    snapshot = {name: getattr(SceneManager, name) for name in fields}
    yield
    for name, value in snapshot.items():
        setattr(SceneManager, name, value)


def test_player_detection_uses_native_scene_manager_without_editor_manager(monkeypatch):
    from Infernux.lib import SceneManager as native_scene_manager
    from Infernux.scene import SceneManager

    class Native:
        @staticmethod
        def instance():
            return Native

        @staticmethod
        def is_playing():
            return True

    monkeypatch.setattr("Infernux.engine.play_mode.PlayModeManager._instance", None)
    monkeypatch.setattr("Infernux.scene._NativeSceneManager", Native)

    assert SceneManager._is_in_play_mode() is True


def test_player_runtime_load_is_queued_until_pending_transaction_is_processed(monkeypatch):
    from Infernux.scene import SceneManager

    monkeypatch.setattr(SceneManager, "_runtime_scene_service", None)
    monkeypatch.setattr(SceneManager, "_is_in_play_mode", staticmethod(lambda: True))
    monkeypatch.setattr(
        SceneManager,
        "_load_build_list",
        staticmethod(lambda: ["/project/Scenes/Main.scene"]),
    )
    monkeypatch.setattr(
        "Infernux.scene.os.path.isfile",
        lambda path: path == "/project/Scenes/Main.scene",
    )
    monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", None)

    assert SceneManager.load_scene("Main") is True
    assert SceneManager._pending_scene_load == "/project/Scenes/Main.scene"


def test_wait_for_load_scene_starts_background_read_immediately(monkeypatch):
    from Infernux.scene import SceneManager

    calls = []

    class Transaction:
        status = "reading"

        def start(self):
            calls.append("start")

    transaction = Transaction()
    monkeypatch.setattr(SceneManager, "_runtime_scene_service", None)
    monkeypatch.setattr(SceneManager, "_is_in_play_mode", staticmethod(lambda: True))
    monkeypatch.setattr(
        SceneManager,
        "_load_build_list",
        staticmethod(lambda: ["/project/Scenes/Main.scene"]),
    )
    monkeypatch.setattr("Infernux.scene.os.path.isfile", lambda _path: True)
    monkeypatch.setattr(
        SceneManager,
        "_create_runtime_load_transaction",
        staticmethod(lambda _path: (transaction, None)),
    )
    monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", None)

    assert SceneManager.wait_for_load_scene("Main") is True
    assert calls == ["start"]
    assert SceneManager._pending_scene_load is None
    assert SceneManager._active_scene_transaction is transaction
    assert SceneManager._active_scene_wait_for_ready is True


def test_new_runtime_scene_request_cancels_stale_preparation(monkeypatch):
    from Infernux.scene import SceneManager

    calls = []

    class Transaction:
        is_complete = False
        status = "reading"

        def cancel(self):
            calls.append("cancel-old")
            self.is_complete = True
            return True

    old_transaction = Transaction()
    monkeypatch.setattr(SceneManager, "_runtime_scene_service", None)
    monkeypatch.setattr(SceneManager, "_is_in_play_mode", staticmethod(lambda: True))
    monkeypatch.setattr(
        SceneManager,
        "_load_build_list",
        staticmethod(
            lambda: [
                "/project/Scenes/Old.scene",
                "/project/Scenes/New.scene",
            ]
        ),
    )
    monkeypatch.setattr("Infernux.scene.os.path.isfile", lambda _path: True)
    monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", old_transaction)
    monkeypatch.setattr(SceneManager, "_scene_load_generation", 7)
    monkeypatch.setattr(SceneManager, "_active_scene_load_generation", 7)

    assert SceneManager.load_scene("New") is True
    assert calls == ["cancel-old"]
    assert SceneManager._pending_scene_load == "/project/Scenes/New.scene"
    assert SceneManager._active_scene_transaction is None
    assert SceneManager._scene_load_generation == 8


def test_stale_runtime_scene_generation_never_publishes(monkeypatch):
    from Infernux.scene import SceneManager

    calls = []

    class Transaction:
        is_complete = False
        status = "ready_to_commit"

        def cancel(self):
            calls.append("cancel-stale")
            self.is_complete = True
            return True

        def poll(self):
            calls.append("publish-stale")
            return True

    monkeypatch.setattr(SceneManager, "_runtime_scene_service", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", Transaction())
    monkeypatch.setattr(SceneManager, "_scene_load_generation", 9)
    monkeypatch.setattr(SceneManager, "_active_scene_load_generation", 8)

    SceneManager.process_pending_load()

    assert calls == ["cancel-stale"]
    assert SceneManager._active_scene_transaction is None


def test_wait_for_load_scene_delegates_to_player_preparation_service(monkeypatch):
    from Infernux.scene import SceneManager

    calls = []

    class RuntimeService:
        @staticmethod
        def request_prepared_load(path):
            calls.append(path)
            return True

    monkeypatch.setattr(SceneManager, "_runtime_scene_service", RuntimeService())
    monkeypatch.setattr(
        SceneManager,
        "_load_build_list",
        staticmethod(lambda: ["/project/Scenes/Main.scene"]),
    )
    # Packaged builds do not retain the authoring scene at this logical path;
    # the Player service resolves it through RuntimeAssetCatalog instead.
    monkeypatch.setattr("Infernux.scene.os.path.isfile", lambda _path: False)

    assert SceneManager.wait_for_load_scene("Main") is True
    assert calls == ["/project/Scenes/Main.scene"]


def test_load_scene_delegates_missing_authoring_path_to_player_catalog(monkeypatch):
    from Infernux.scene import SceneManager

    calls = []

    class RuntimeService:
        @staticmethod
        def request_load(path):
            calls.append(path)
            return True

    monkeypatch.setattr(SceneManager, "_runtime_scene_service", RuntimeService())
    monkeypatch.setattr(
        SceneManager,
        "_load_build_list",
        staticmethod(lambda: ["/packaged/Data/Assets/Scenes/Main.scene"]),
    )
    monkeypatch.setattr("Infernux.scene.os.path.isfile", lambda _path: False)

    assert SceneManager.load_scene("Main") is True
    assert calls == ["/packaged/Data/Assets/Scenes/Main.scene"]


def test_prepare_scene_holds_ready_transaction_until_explicit_activation(monkeypatch):
    from Infernux.scene import SceneManager

    calls = []

    class Transaction:
        status = "ready_to_commit"
        succeeded = True
        error = ""

        def start(self):
            calls.append("start")

        def poll(self):
            calls.append("commit")
            self.status = "completed"
            return True

    transaction = Transaction()
    monkeypatch.setattr(SceneManager, "_runtime_scene_service", None)
    monkeypatch.setattr(SceneManager, "_is_in_play_mode", staticmethod(lambda: True))
    monkeypatch.setattr(
        SceneManager,
        "_load_build_list",
        staticmethod(lambda: ["/project/Scenes/Main.scene"]),
    )
    monkeypatch.setattr("Infernux.scene.os.path.isfile", lambda _path: True)
    monkeypatch.setattr(
        SceneManager,
        "_create_runtime_load_transaction",
        staticmethod(lambda _path: (transaction, None)),
    )
    monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", None)

    assert SceneManager.prepare_scene("Main") is True
    assert SceneManager.is_scene_prepared() is True
    SceneManager.process_pending_load()
    assert calls == ["start"]

    assert SceneManager.activate_prepared_scene() is True
    SceneManager.process_pending_load()
    assert calls == ["start", "commit"]


def test_runtime_scene_transaction_prefetches_external_meshes(monkeypatch):
    from Infernux.engine.runtime_scene_transaction import SceneDocumentTransaction

    document = {
        "objects": [
            {
                "components": [
                    {
                        "type_id": "native:infernux.MeshRenderer",
                        "data": {
                            "meshAssetGuid": "mesh-a",
                            "useInlineMesh": False,
                        },
                    },
                    {
                        "type_id": "native:infernux.SkinnedMeshRenderer",
                        "data": {
                            "meshAssetGuid": "mesh-a",
                            "useInlineMesh": False,
                        },
                    },
                    {
                        "type_id": "native:infernux.MeshRenderer",
                        "data": {"useInlineMesh": True},
                    },
                ],
                "children": [
                    {
                        "components": [
                            {
                                "type_id": "native:infernux.MeshRenderer",
                                "data": {
                                    "meshAssetGuid": "mesh-b",
                                    "useInlineMesh": False,
                                },
                            }
                        ],
                        "children": [],
                    }
                ],
            }
        ]
    }

    assert SceneDocumentTransaction._scene_mesh_guids(document) == (
        "mesh-a",
        "mesh-b",
    )


def test_prepared_scene_advances_at_most_one_transaction_phase_per_tick(monkeypatch):
    import Infernux.scene as scene_api
    from Infernux.scene import SceneManager

    calls = []

    class Transaction:
        status = "reading"
        succeeded = True
        error = ""

        def __init__(self):
            self._states = iter(
                ["document_ready", "resources_ready", "ready_to_commit", "completed"]
            )

        def poll(self):
            self.status = next(self._states)
            calls.append(self.status)
            return self.status == "completed"

    class Native:
        @staticmethod
        def instance():
            return Native

        @staticmethod
        def get_active_scene():
            return object()

        @staticmethod
        def _start_active_scene_for_play():
            calls.append("start_scene")

    monkeypatch.setattr(scene_api, "_NativeSceneManager", Native)
    monkeypatch.setattr(SceneManager, "_runtime_scene_service", None)
    monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", Transaction())
    monkeypatch.setattr(
        SceneManager, "_active_scene_load_path", "/project/Scenes/Main.scene"
    )
    monkeypatch.setattr(SceneManager, "_active_scene_file_manager", None)
    monkeypatch.setattr(SceneManager, "_active_scene_wait_for_ready", True)

    SceneManager.process_pending_load()
    assert calls == ["document_ready"]
    assert SceneManager.is_scene_load_pending() is True

    SceneManager.process_pending_load()
    assert calls == ["document_ready", "resources_ready"]
    assert SceneManager.is_scene_load_pending() is True

    SceneManager.process_pending_load()
    assert calls == ["document_ready", "resources_ready", "ready_to_commit"]
    assert SceneManager.is_scene_load_pending() is True

    SceneManager.process_pending_load()
    assert calls == [
        "document_ready",
        "resources_ready",
        "ready_to_commit",
        "completed",
        "start_scene",
    ]
    assert SceneManager.is_scene_load_pending() is False


def test_player_runtime_tick_advances_pending_scene_load_before_time(monkeypatch):
    from Infernux.engine.player_runtime import PlayerRuntimeSession

    calls = []

    class RuntimeSceneService:
        active_scene_path = None

        @staticmethod
        def process_pending_load():
            calls.append("process")

        @staticmethod
        def cancel_pending_load():
            return None

    monkeypatch.setattr("Infernux.timing.Time._tick", lambda value: calls.append("time"))

    session = PlayerRuntimeSession(scene_service=RuntimeSceneService())
    session._state = "playing"
    session.tick(1.0 / 60.0)

    assert calls == ["process", "time"]


def test_pending_scene_transaction_starts_the_new_scene_once(monkeypatch):
    from Infernux.scene import SceneManager

    monkeypatch.setattr(SceneManager, "_runtime_scene_service", None)
    calls = []

    class Transaction:
        succeeded = True
        error = ""

        def poll(self):
            calls.append("poll")
            return True

    class Native:
        @staticmethod
        def instance():
            return Native

        @staticmethod
        def get_active_scene():
            return object()

        @staticmethod
        def _start_active_scene_for_play():
            calls.append("start")

    monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", Transaction())
    monkeypatch.setattr(SceneManager, "_active_scene_load_path", "/project/Scenes/Main.scene")
    monkeypatch.setattr(SceneManager, "_active_scene_file_manager", None)
    monkeypatch.setattr("Infernux.scene._NativeSceneManager", Native)

    SceneManager.process_pending_load()
    SceneManager.process_pending_load()

    assert calls == ["poll", "start"]
