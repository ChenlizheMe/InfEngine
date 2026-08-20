"""Runtime-safe owner-thread transactions for complete Scene documents."""

from __future__ import annotations

import copy
import os
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional

from Infernux.lib import (
    _SceneDocumentReadTicket,
    _collect_scene_resource_dependencies,
    _schedule_scene_document_read,
)
from Infernux.engine.path_utils import resolved_path


class SceneDocumentTransactionError(RuntimeError):
    """Raised when a Scene document transaction cannot complete."""


class SceneDocumentTransactionState(Enum):
    CREATED = "created"
    READING = "reading"
    DOCUMENT_READY = "document_ready"
    RESOURCE_PREFLIGHTING = "resource_preflighting"
    RESOURCES_READY = "resources_ready"
    PREFLIGHTING = "preflighting"
    READY_TO_COMMIT = "ready_to_commit"
    COMMITTING = "committing"
    ROLLING_BACK = "rolling_back"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset(
    {
        SceneDocumentTransactionState.COMPLETED,
        SceneDocumentTransactionState.FAILED,
        SceneDocumentTransactionState.CANCELLED,
    }
)


class SceneDocumentTransaction:
    """Read, preflight, commit, and publish one complete Scene document.

    File IO and native structural validation run on the native JobSystem. Every
    Python operation and live-scene mutation is restricted to the thread that
    constructs the transaction.
    """

    def __init__(
        self,
        scene: Any,
        *,
        path: Optional[os.PathLike[str] | str] = None,
        document: Any = None,
        asset_database: Any = None,
        native_engine: Any = None,
        clear_registries: bool = True,
        borrow_document: bool = False,
        prefer_loaded_types: bool = False,
        before_commit: Optional[Callable[[], None]] = None,
        after_publish: Optional[Callable[[], None]] = None,
    ) -> None:
        if scene is None:
            raise ValueError("scene is required")
        if (path is None) == (document is None):
            raise ValueError("exactly one of path or document is required")
        is_native_snapshot = document is not None and callable(
            getattr(document, "_python_component_records", None)
        ) and callable(getattr(document, "_preflight_resource_dependencies", None))
        if document is not None and not isinstance(document, dict) and not is_native_snapshot:
            raise TypeError("scene document must be a dict or native Play Mode snapshot")

        self._scene = scene
        self._path = os.fspath(path) if path is not None else None
        self._document = (
            document
            if document is not None and (borrow_document or is_native_snapshot)
            else copy.deepcopy(document) if document is not None else None
        )
        self._asset_database = asset_database
        self._native_engine = native_engine
        self._clear_registries = bool(clear_registries)
        self._prefer_loaded_types = bool(prefer_loaded_types)
        self._before_commit = before_commit
        self._after_publish = after_publish
        self._owner_thread_id = threading.get_ident()
        self._state = SceneDocumentTransactionState.CREATED
        self._ticket: Optional[_SceneDocumentReadTicket] = None
        self._asset_load_tickets: list[Any] = []
        self._linked_shader_ticket: Any = None
        self._linked_shader_preload_started = False
        self._resource_dependencies: tuple[tuple[str, str], ...] = ()
        self._resource_preflight_started: Optional[float] = None
        self._prepared_graph = None
        self._commit_token = None
        self._native_committed = False
        self._rolled_back = False
        self._rollback_error = ""
        self._error = ""
        self._failure_exception: Optional[BaseException] = None
        self._phase_timings_ms: dict[str, float] = {}

    @property
    def state(self) -> SceneDocumentTransactionState:
        return self._state

    @property
    def status(self) -> str:
        return self._state.value

    @property
    def error(self) -> str:
        return self._error

    @property
    def failure_exception(self) -> Optional[BaseException]:
        return self._failure_exception

    @property
    def is_complete(self) -> bool:
        return self._state in _TERMINAL_STATES

    @property
    def succeeded(self) -> bool:
        return self._state is SceneDocumentTransactionState.COMPLETED

    @property
    def ran_on_worker(self) -> bool:
        return bool(self._ticket is not None and self._ticket.ran_on_worker)

    @property
    def rolled_back(self) -> bool:
        return self._rolled_back

    @property
    def rollback_error(self) -> str:
        return self._rollback_error

    @property
    def phase_timings_ms(self) -> dict[str, float]:
        return dict(self._phase_timings_ms)

    @property
    def document(self) -> Any:
        if callable(getattr(self._document, "_python_component_records", None)):
            return self._document
        return copy.deepcopy(self._document)

    def _require_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("SceneDocumentTransaction must run on its owner thread")

    def _fail(self, error: str | BaseException) -> None:
        if isinstance(error, BaseException):
            self._failure_exception = error
            self._error = str(error) or type(error).__name__
        else:
            self._error = error
        if self._prepared_graph is not None:
            self._prepared_graph.discard()
        self._prepared_graph = None
        self._state = SceneDocumentTransactionState.FAILED

    def _rebuild_python_registries(self) -> None:
        if not self._clear_registries:
            return
        from Infernux.components.component import InxComponent
        from Infernux.components.builtin_component import BuiltinComponent

        InxComponent._clear_all_instances()
        BuiltinComponent._clear_cache()
        scenes = [self._scene]
        try:
            from Infernux.lib import SceneManager

            persistent_scene = SceneManager.instance().get_runtime_persistent_scene()
            if persistent_scene is not None and persistent_scene is not self._scene:
                scenes.append(persistent_scene)
        except (AttributeError, RuntimeError):
            # Source-only tests can run against an older installed native
            # module until the consolidated preset build publishes this API.
            pass
        for scene in scenes:
            for game_object in scene.get_all_objects():
                for component in game_object.get_py_components() or []:
                    component._set_game_object(game_object)
                    component._refresh_native_handle()

    def _reconcile_persistent_python_registries(self) -> None:
        """Restore only persistent components cleared by scene publication."""
        if not self._clear_registries:
            return
        try:
            from Infernux.lib import SceneManager

            persistent_scene = SceneManager.instance().get_runtime_persistent_scene()
        except (AttributeError, RuntimeError):
            # Source-only tests can run against an older installed native
            # module until the consolidated preset build publishes this API.
            return
        if persistent_scene is None or persistent_scene is self._scene:
            return
        for game_object in persistent_scene.get_all_objects():
            for component in game_object.get_py_components() or []:
                component._set_game_object(game_object)
                component._refresh_native_handle()

    @staticmethod
    def _scene_mesh_guids(document: dict[str, Any]) -> tuple[str, ...]:
        """Collect external meshes that would otherwise load during commit."""
        result: list[str] = []
        seen: set[str] = set()

        def visit(game_object: Any) -> None:
            if not isinstance(game_object, dict):
                return
            for component in game_object.get("components", ()):
                if not isinstance(component, dict):
                    continue
                type_id = str(component.get("type_id", ""))
                if not (
                    type_id.endswith(".MeshRenderer")
                    or type_id.endswith(".SkinnedMeshRenderer")
                ):
                    continue
                data = component.get("data")
                if not isinstance(data, dict) or bool(data.get("useInlineMesh", False)):
                    continue
                guid = str(data.get("meshAssetGuid", "") or "").strip()
                if guid and guid not in seen:
                    seen.add(guid)
                    result.append(guid)
            for child in game_object.get("children", ()):
                visit(child)

        for root in document.get("objects", ()):
            visit(root)
        return tuple(result)

    def _start_asset_preloads(self) -> None:
        from Infernux.lib import AssetRegistry

        registry = AssetRegistry.instance()
        asset_database = registry.get_asset_database()
        begin_load = {
            "Material": getattr(registry, "begin_load_material_by_guid", None),
            "PhysicMaterial": getattr(
                registry, "begin_load_physic_material_by_guid", None
            ),
            "Audio": getattr(registry, "begin_load_audio_by_guid", None),
            "Mesh": getattr(registry, "begin_load_mesh_by_guid", None),
            "Texture": getattr(registry, "begin_load_texture_by_guid", None),
        }
        tickets: list[Any] = []
        for guid, resource_type in self._resource_dependencies:
            schedule = begin_load.get(resource_type)
            if schedule is not None and asset_database.get_path_from_guid(guid):
                tickets.append(schedule(guid))
        self._asset_load_tickets = tickets

    def _poll_asset_preloads(self) -> bool:
        """Publish completed worker payloads; return true once all are resident."""
        if self._asset_load_tickets:
            from Infernux.lib import AssetRegistry

            registry = AssetRegistry.instance()
            if any(not ticket.complete for ticket in self._asset_load_tickets):
                return False
            for ticket in self._asset_load_tickets:
                if not ticket.committed and not registry.try_commit_asset_load(ticket):
                    return False
            self._asset_load_tickets.clear()

        if not self._linked_shader_preload_started:
            self._linked_shader_preload_started = True
            begin = getattr(
                self._native_engine, "begin_prepare_linked_shader_programs", None
            )
            if callable(begin):
                material_guids = [
                    guid
                    for guid, resource_type in self._resource_dependencies
                    if resource_type == "Material"
                ]
                self._linked_shader_ticket = begin(material_guids)

        ticket = self._linked_shader_ticket
        if ticket is not None:
            if not ticket.complete:
                return False
            if not ticket.committed:
                commit = getattr(
                    self._native_engine, "try_commit_linked_shader_programs", None
                )
                if not callable(commit) or not commit(ticket):
                    return False
            self._linked_shader_ticket = None
        return True

    def _rollback_after_commit(self, cause: BaseException) -> None:
        self._state = SceneDocumentTransactionState.ROLLING_BACK
        if self._prepared_graph is not None:
            self._prepared_graph.discard()
            self._prepared_graph = None
        try:
            if self._commit_token is None or not self._commit_token.is_active:
                raise SceneDocumentTransactionError("retained native world is unavailable")
            if not self._commit_token.rollback():
                raise SceneDocumentTransactionError("retained native world rollback was rejected")
            self._commit_token = None
            self._rebuild_python_registries()
            self._native_committed = False
            self._rolled_back = True
        except Exception as rollback_exc:
            self._rollback_error = str(rollback_exc) or type(rollback_exc).__name__
            failure = SceneDocumentTransactionError(
                f"scene publish failed ({cause}); rollback also failed ({self._rollback_error})"
            )
            failure.__cause__ = rollback_exc
            self._fail(failure)
            return
        self._fail(cause)

    def start(self) -> "SceneDocumentTransaction":
        self._require_owner_thread()
        if self._state is not SceneDocumentTransactionState.CREATED:
            raise RuntimeError(f"cannot start transaction in state {self.status}")
        if self._path is None:
            self._state = SceneDocumentTransactionState.DOCUMENT_READY
        else:
            self._ticket = _schedule_scene_document_read(resolved_path(self._path))
            self._state = SceneDocumentTransactionState.READING
        return self

    def poll(self) -> bool:
        """Advance at most one transaction phase; return whether it is terminal."""
        self._require_owner_thread()
        if self._state is SceneDocumentTransactionState.CREATED:
            raise RuntimeError("transaction must be started before polling")
        if self.is_complete:
            return True

        try:
            if self._state is SceneDocumentTransactionState.READING:
                assert self._ticket is not None
                if not self._ticket.is_complete:
                    return False
                if self._ticket.status == "cancelled":
                    self._state = SceneDocumentTransactionState.CANCELLED
                    return True
                if not self._ticket.is_ready:
                    self._fail(self._ticket.error or "scene document read failed")
                    return True
                document = self._ticket._take_document()
                if not isinstance(document, dict):
                    self._fail("native scene reader returned a non-object document")
                    return True
                self._document = document
                self._state = SceneDocumentTransactionState.DOCUMENT_READY
                return False

            if self._state is SceneDocumentTransactionState.DOCUMENT_READY:
                assert self._document is not None
                self._state = SceneDocumentTransactionState.RESOURCE_PREFLIGHTING
                self._resource_preflight_started = time.perf_counter()
                native_preflight = getattr(self._document, "_preflight_resource_dependencies", None)
                native_dependencies = getattr(self._document, "_resource_dependencies", None)
                if callable(native_dependencies):
                    self._resource_dependencies = tuple(
                        (str(guid), str(resource_type))
                        for guid, resource_type in native_dependencies()
                    )
                else:
                    self._resource_dependencies = tuple(
                        (str(guid), str(resource_type))
                        for guid, resource_type in _collect_scene_resource_dependencies(self._document)
                    )
                self._start_asset_preloads()
                if not self._asset_load_tickets:
                    self._phase_timings_ms["resources"] = (
                        time.perf_counter() - self._resource_preflight_started
                    ) * 1000.0
                    self._resource_preflight_started = None
                    self._state = SceneDocumentTransactionState.RESOURCES_READY
                return False

            if self._state is SceneDocumentTransactionState.RESOURCE_PREFLIGHTING:
                if not self._poll_asset_preloads():
                    return False
                phase_started = self._resource_preflight_started or time.perf_counter()
                self._phase_timings_ms["resources"] = (time.perf_counter() - phase_started) * 1000.0
                self._resource_preflight_started = None
                self._state = SceneDocumentTransactionState.RESOURCES_READY
                return False

            if self._state is SceneDocumentTransactionState.RESOURCES_READY:
                from Infernux.engine.component_restore import preflight_scene_python_components

                assert self._document is not None
                self._state = SceneDocumentTransactionState.PREFLIGHTING
                phase_started = time.perf_counter()
                self._prepared_graph = preflight_scene_python_components(
                    self._document,
                    asset_database=self._asset_database,
                    prefer_loaded_types=self._prefer_loaded_types,
                )
                self._phase_timings_ms["python_preflight"] = (
                    time.perf_counter() - phase_started
                ) * 1000.0
                self._state = SceneDocumentTransactionState.READY_TO_COMMIT
                return False

            if self._state is SceneDocumentTransactionState.READY_TO_COMMIT:
                from Infernux.engine.component_restore import publish_prepared_scene_python_components

                assert self._document is not None
                assert self._prepared_graph is not None
                self._state = SceneDocumentTransactionState.COMMITTING
                if self._before_commit is not None:
                    self._before_commit()
                native_commit = getattr(
                    self._scene, "_commit_play_mode_snapshot_retaining_world", None
                )
                if callable(getattr(self._document, "_python_component_records", None)) and callable(
                    native_commit
                ):
                    phase_started = time.perf_counter()
                    self._commit_token = native_commit(self._document)
                else:
                    phase_started = time.perf_counter()
                    self._commit_token = self._scene._commit_document_retaining_world(self._document)
                self._phase_timings_ms["native_commit"] = (
                    time.perf_counter() - phase_started
                ) * 1000.0
                if self._commit_token is None:
                    self._fail("native scene document commit was rejected")
                    return True
                self._native_committed = True
                phase_started = time.perf_counter()
                publish_prepared_scene_python_components(
                    self._scene,
                    self._prepared_graph,
                    clear_registries=self._clear_registries,
                )
                # The active scene was registered while its prepared graph was
                # attached. Only DontDestroyOnLoad objects were removed by the
                # shared registry clear, so restore those without clearing and
                # binding the new scene a second time.
                self._reconcile_persistent_python_registries()
                self._phase_timings_ms["python_publish"] = (
                    time.perf_counter() - phase_started
                ) * 1000.0
                self._prepared_graph = None
                if self._after_publish is not None:
                    phase_started = time.perf_counter()
                    self._after_publish()
                    self._phase_timings_ms["after_publish"] = (
                        time.perf_counter() - phase_started
                    ) * 1000.0
                phase_started = time.perf_counter()
                from Infernux.components.particle_system import ParticleSystem

                ParticleSystem._begin_native_publication_batch()
                try:
                    self._commit_token.finalize()
                    ParticleSystem._end_native_publication_batch(commit=True)
                except Exception:
                    ParticleSystem._end_native_publication_batch(commit=False)
                    raise
                self._phase_timings_ms["finalize"] = (
                    time.perf_counter() - phase_started
                ) * 1000.0
                self._commit_token = None
                self._native_committed = False
                self._state = SceneDocumentTransactionState.COMPLETED
                return True

            raise RuntimeError(f"invalid transaction state {self.status}")
        except Exception as exc:
            if self._native_committed:
                self._rollback_after_commit(exc)
            else:
                self._fail(exc)
            return True

    def cancel(self) -> bool:
        """Cancel before commit begins; live scene state is left untouched."""
        self._require_owner_thread()
        if self.is_complete or self._state is SceneDocumentTransactionState.COMMITTING:
            return False
        if self._ticket is not None:
            self._ticket.cancel()
        if self._linked_shader_ticket is not None:
            cancel = getattr(self._linked_shader_ticket, "cancel", None)
            if callable(cancel):
                cancel()
        if self._prepared_graph is not None:
            self._prepared_graph.discard()
        self._prepared_graph = None
        self._state = SceneDocumentTransactionState.CANCELLED
        return True

    def run_to_completion(
        self, *, raise_on_failure: bool = True, on_tick: Optional[Callable[[], None]] = None
    ) -> bool:
        """Run all phases synchronously on the owner thread."""
        self._require_owner_thread()
        if self._state is SceneDocumentTransactionState.CREATED:
            self.start()
        while not self.poll():
            if on_tick is not None:
                on_tick()
            if self._state is SceneDocumentTransactionState.READING:
                time.sleep(0.001)
        if raise_on_failure and self._state is SceneDocumentTransactionState.FAILED:
            if self._failure_exception is not None:
                raise self._failure_exception
            raise SceneDocumentTransactionError(self._error)
        return self.succeeded
