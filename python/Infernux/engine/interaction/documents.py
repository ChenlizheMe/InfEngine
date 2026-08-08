"""Stable editor documents, revisions, views, and authoring operations."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum, IntFlag, auto
import hashlib
import json
import os
from typing import Any, Optional, Protocol
import uuid

from Infernux.engine.path_utils import path_key


def _capture_durable_file_state(resource_path: str):
    path = str(resource_path or "").strip()
    # Composite documents such as Project Settings own a directory containing
    # several durable files. Their controller performs per-file persistence;
    # the single-target CAS token only applies to regular-file documents.
    if not path or os.path.isdir(path):
        return None
    from Infernux.core.document_store import capture_document_file_state

    return capture_document_file_state(path)


def _durable_file_identity(state: Any):
    """Return the content identity used for external-change decisions.

    Watchers report filesystem activity, not content changes.  ``modified_ns``
    is intentionally excluded here: a read-only inspection, timestamp repair,
    or an atomic replace of identical bytes must not create an editor conflict.
    The native CAS hash remains the authority for the actual file contents.
    """
    if state is None:
        return None
    if isinstance(state, dict):
        exists = state.get("exists")
        size = state.get("size", 0)
        content_hash = state.get("content_hash", state.get("contentHash", 0))
    else:
        exists = getattr(state, "exists", None)
        size = getattr(state, "size", 0)
        content_hash = getattr(state, "content_hash", 0)
    if exists is None:
        return None
    try:
        return bool(exists), int(size or 0), int(content_hash or 0)
    except (TypeError, ValueError):
        return None


def document_content_token(value: Any) -> str:
    """Return the canonical token used to close a document save transaction."""
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class DocumentKind(str, Enum):
    SCENE = "scene"
    PREFAB = "prefab"
    MATERIAL = "material"
    PHYSIC_MATERIAL = "physic_material"
    RENDER_EFFECT = "render_effect"
    ANIMATION_CLIP = "animation_clip"
    ANIMATION_FSM = "animation_fsm"
    TIMELINE = "timeline"
    PARTICLE_GRAPH = "particle_graph"
    UI_DOCUMENT = "ui_document"
    IMPORT_SETTINGS = "import_settings"
    PROJECT_SETTINGS = "project_settings"
    GENERIC = "generic"


class DocumentIdentityKind(str, Enum):
    ASSET_GUID = "asset_guid"
    RESOURCE_PATH = "resource_path"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class DocumentKey:
    kind: DocumentKind
    identity_kind: DocumentIdentityKind
    identity: str

    def __post_init__(self) -> None:
        kind = DocumentKind(self.kind)
        identity_kind = DocumentIdentityKind(self.identity_kind)
        identity = str(self.identity or "").strip()
        if identity_kind is DocumentIdentityKind.RESOURCE_PATH:
            identity = path_key(identity)
        elif identity_kind is DocumentIdentityKind.ASSET_GUID:
            identity = identity.casefold()
        if not identity:
            raise ValueError("document identity must not be empty")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "identity_kind", identity_kind)
        object.__setattr__(self, "identity", identity)

    @classmethod
    def asset(cls, kind: DocumentKind, guid: str) -> "DocumentKey":
        return cls(kind, DocumentIdentityKind.ASSET_GUID, guid)

    @classmethod
    def resource(cls, kind: DocumentKind, path: str) -> "DocumentKey":
        return cls(kind, DocumentIdentityKind.RESOURCE_PATH, path)

    @classmethod
    def session(
        cls,
        kind: DocumentKind,
        session_id: str = "",
    ) -> "DocumentKey":
        return cls(kind, DocumentIdentityKind.SESSION, session_id or uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class DocumentLocator:
    """Stable address used by history after a document view is closed.

    ``document_id`` is intentionally absent from the address. The registry
    preserves the original live identity when possible, while ``stable_id``
    and the asset/resource key can still resolve a replacement entry.
    """

    stable_id: str
    key_hint: DocumentKey
    resource_path: str = ""
    title: str = ""

    def __post_init__(self) -> None:
        stable_id = str(self.stable_id or "").strip()
        if not stable_id:
            raise ValueError("document locator stable_id must not be empty")
        if not isinstance(self.key_hint, DocumentKey):
            raise TypeError("document locator key must be a DocumentKey")
        object.__setattr__(self, "stable_id", stable_id)
        object.__setattr__(self, "resource_path", str(self.resource_path or ""))
        object.__setattr__(self, "title", str(self.title or ""))

    @property
    def key(self) -> DocumentKey:
        """Compatibility/readability alias for the current key hint."""
        return self.key_hint


class DocumentState(str, Enum):
    READY = "ready"
    SAVING = "saving"
    CONFLICT = "conflict"
    CLOSED = "closed"


class DocumentCapability(IntFlag):
    NONE = 0
    SAVE = auto()
    SAVE_AS = auto()
    DISCARD = auto()


class DocumentActionStatus(str, Enum):
    APPLIED = "applied"
    PENDING = "pending"
    NO_OP = "no_op"
    REJECTED = "rejected"
    FAILED = "failed"


class SaveTicketStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DocumentActionResult:
    status: DocumentActionStatus
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in {
            DocumentActionStatus.APPLIED,
            DocumentActionStatus.PENDING,
            DocumentActionStatus.NO_OP,
        }


@dataclass(slots=True)
class SaveTicket:
    ticket_id: str
    document_id: str
    captured_revision: int
    captured_external_revision: int = 0
    expected_file_state: Any = None
    resource_path: str = ""
    content_token: str = ""
    save_as: bool = False
    operation_id: str = ""
    commit_token: str = ""
    publication_bookkeeping_revision: Optional[int] = None
    status: SaveTicketStatus = SaveTicketStatus.PENDING
    message: str = ""

    @property
    def is_pending(self) -> bool:
        return self.status is SaveTicketStatus.PENDING


class DocumentController(Protocol):
    def save(self, *, ticket: SaveTicket, save_as: bool = False) -> Any: ...

    def discard(self, *, document_id: str) -> Any: ...

    def reload_from_resource(self, *, document_id: str, resource_path: str) -> Any: ...

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ) -> Any: ...


class ExplicitResourceSaveController(Protocol):
    """Optional controller capability used by explicit-path save requests."""

    def save_to_resource(
        self,
        *,
        ticket: SaveTicket,
        resource_path: str,
    ) -> Any: ...


@dataclass(slots=True)
class EditorDocument:
    document_id: str
    stable_id: str
    key: DocumentKey
    kind: DocumentKind
    title: str
    resource_path: str = ""
    revision: int = 0
    saved_revision: int = 0
    external_revision: int = 0
    # Monotonic persistence and dependency milestones shared by all views.
    edit_revision: int = 0
    requested_write_revision: int = 0
    persisted_revision: int = 0
    imported_disk_revision: int = 0
    preview_dependency_revision: int = 0
    durable_file_state: Any = field(default=None, repr=False)
    state: DocumentState = DocumentState.READY
    capabilities: DocumentCapability = DocumentCapability.NONE
    view_ids: set[str] = field(default_factory=set)
    dirty_view_ids: set[str] = field(default_factory=set)
    controller: Optional[DocumentController] = field(default=None, repr=False)
    _revision_high_watermark: int = field(default=0, repr=False)
    _dirty_view_ids_by_revision: dict[int, frozenset[str]] = field(
        default_factory=dict,
        repr=False,
    )

    @property
    def is_dirty(self) -> bool:
        return self.revision != self.saved_revision

    def is_dirty_for_view(self, view_id: str) -> bool:
        """Return whether one View contributed to the current unsaved state."""
        if not self.is_dirty:
            return False
        return str(view_id or "") in self.dirty_owner_view_ids()

    def dirty_owner_view_ids(self) -> frozenset[str]:
        """Return the Views that visibly own the current unsaved revision.

        Legacy or newly-created documents may become dirty before a View is
        attached.  That state must still resolve to one authoring View instead
        of broadcasting a dirty marker to every projection of the document.
        """
        if not self.is_dirty:
            return frozenset()
        owners = frozenset(
            self._dirty_view_ids_by_revision.get(
                self.revision,
                frozenset(self.dirty_view_ids),
            )
        )
        if owners:
            return owners
        if self.kind is DocumentKind.SCENE:
            return frozenset(("scene_view",))
        if self.view_ids:
            return frozenset((sorted(self.view_ids)[0],))
        return frozenset()


@dataclass(slots=True)
class _DormantDocumentRecord:
    document: EditorDocument
    restore_state: Any = None


class DocumentRegistry:
    """Single authority for editor document identity and dirty revisions."""

    SESSION_STATE_VERSION = 1
    _instance: Optional["DocumentRegistry"] = None

    def __init__(self) -> None:
        self._documents: dict[str, EditorDocument] = {}
        self._document_ids_by_key: dict[DocumentKey, str] = {}
        self._document_ids_by_stable_id: dict[str, str] = {}
        self._dormant_locators_by_key: dict[DocumentKey, DocumentLocator] = {}
        self._dormant_locators_by_stable_id: dict[str, DocumentLocator] = {}
        self._dormant_documents_by_stable_id: dict[str, _DormantDocumentRecord] = {}
        self._view_documents: dict[str, str] = {}
        self._save_tickets: dict[str, SaveTicket] = {}
        self._active_save_by_document: dict[str, str] = {}
        self._deferred_save_requests: dict[str, bool] = {}
        self._external_change_preflights: dict[str, tuple[str, ...]] = {}
        self._pending_external_reloads: set[str] = set()
        self._external_reload_results: dict[str, DocumentActionResult] = {}
        self._pending_session_records: dict[str, dict[str, Any]] = {}
        self._pending_session_document_by_view: dict[str, str] = {}
        self._session_restore_suppressed_stable_ids: set[str] = set()
        self._session_restore_suppressed_view_ids: set[str] = set()
        self._revision = 0
        DocumentRegistry._instance = self

    @classmethod
    def instance(cls) -> "DocumentRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def documents(self) -> tuple[EditorDocument, ...]:
        return tuple(self._documents.values())

    def has_pending_session_document(self, view_id: str) -> bool:
        """Return whether startup has a durable document snapshot for a View."""
        return str(view_id or "").strip() in self._pending_session_document_by_view

    def pending_session_view_ids(self) -> tuple[str, ...]:
        """Return the Views that still own an unclaimed startup document."""
        return tuple(sorted(self._pending_session_document_by_view))

    def prune_pending_session_views(
        self,
        restorable_view_ids,
    ) -> tuple[str, ...]:
        """Discard startup drafts whose authoring Views will not be restored.

        Window topology and document drafts are persisted independently.  A
        document-backed View that is explicitly closed must not leave behind
        an invisible draft that is captured again on every shutdown.  Shared
        documents retain the subset of Views that are actually being restored;
        a dirty revision whose former owner was removed is reassigned to one
        remaining authoring View so its confirmation stays visible.
        """
        restorable = {
            str(value or "").strip()
            for value in restorable_view_ids
            if str(value or "").strip()
        }
        removed_views: list[str] = []
        retained_records: dict[str, dict[str, Any]] = {}
        retained_by_view: dict[str, str] = {}
        changed = False

        for stable_id, source in self._pending_session_records.items():
            original_views = tuple(source["view_ids"])
            retained_views = tuple(
                view_id for view_id in original_views if view_id in restorable
            )
            removed = tuple(
                view_id for view_id in original_views if view_id not in restorable
            )
            if removed:
                changed = True
                removed_views.extend(removed)
                self._session_restore_suppressed_view_ids.update(removed)
            if not retained_views:
                self._session_restore_suppressed_stable_ids.add(stable_id)
                continue

            record = copy.deepcopy(source) if removed else source
            if removed:
                dirty_views = tuple(
                    view_id
                    for view_id in record.get("dirty_view_ids", ())
                    if view_id in retained_views
                )
                if (
                    record["revision"] != record["saved_revision"]
                    and not dirty_views
                ):
                    owner_view = (
                        "scene_view"
                        if record["kind"] is DocumentKind.SCENE
                        and "scene_view" in retained_views
                        else retained_views[0]
                    )
                    dirty_views = (owner_view,)
                record["view_ids"] = retained_views
                record["dirty_view_ids"] = dirty_views
            retained_records[stable_id] = record
            for view_id in retained_views:
                retained_by_view[view_id] = stable_id

        if changed:
            self._pending_session_records = retained_records
            self._pending_session_document_by_view = retained_by_view
            self._touch()
        return tuple(sorted(set(removed_views)))

    def is_session_restore_suppressed(self, view_id: str) -> bool:
        """Return whether terminal discard retired a View for this session."""
        return str(view_id or "").strip() in self._session_restore_suppressed_view_ids

    def capture_session_state(self) -> dict[str, Any]:
        """Capture restorable authoring documents independently of Panel state."""
        records: list[dict[str, Any]] = []
        for stable_id, pending in self._pending_session_records.items():
            if stable_id in self._session_restore_suppressed_stable_ids:
                continue
            record = copy.deepcopy(pending)
            record["kind"] = DocumentKind(record["kind"]).value
            key = record["key"]
            record["key"] = {
                "identity_kind": key.identity_kind.value,
                "identity": key.identity,
            }
            record["capabilities"] = int(record["capabilities"])
            record["view_ids"] = list(record["view_ids"])
            record["dirty_view_ids"] = list(record.get("dirty_view_ids", ()))
            records.append(
                json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False))
            )
        pending_ids = {str(record["stable_id"]) for record in records}
        for document in self._documents.values():
            if (
                not document.view_ids
                or document.stable_id in pending_ids
                or document.stable_id in self._session_restore_suppressed_stable_ids
            ):
                continue
            capture = getattr(
                document.controller,
                "capture_document_restore_state",
                None,
            )
            if not callable(capture):
                continue
            restore_state = capture(document.document_id)
            if restore_state is None:
                continue
            record = {
                "document_id": document.document_id,
                "stable_id": document.stable_id,
                "kind": document.kind.value,
                "key": {
                    "identity_kind": document.key.identity_kind.value,
                    "identity": document.key.identity,
                },
                "title": document.title,
                "resource_path": document.resource_path,
                "revision": document.revision,
                "saved_revision": document.saved_revision,
                "external_revision": document.external_revision,
                "state": (
                    DocumentState.CONFLICT.value
                    if document.state is DocumentState.CONFLICT
                    else DocumentState.READY.value
                ),
                "capabilities": int(document.capabilities),
                "view_ids": sorted(document.view_ids),
                "dirty_view_ids": sorted(document.dirty_view_ids),
                "restore_state": copy.deepcopy(restore_state),
            }
            # Validate and detach the snapshot from mutable authoring models.
            record = json.loads(
                json.dumps(record, ensure_ascii=False, allow_nan=False)
            )
            records.append(record)
        records.sort(key=lambda item: (item["kind"], item["stable_id"]))
        return {"version": self.SESSION_STATE_VERSION, "documents": records}

    def queue_session_restore(self, data: dict[str, Any] | None) -> int:
        """Validate persistent authoring snapshots for lazy View claims."""
        self._pending_session_records.clear()
        self._pending_session_document_by_view.clear()
        self._session_restore_suppressed_stable_ids.clear()
        self._session_restore_suppressed_view_ids.clear()
        if not data:
            return 0
        if not isinstance(data, dict) or set(data) != {"version", "documents"}:
            raise ValueError("document session state has an invalid envelope")
        if data["version"] != self.SESSION_STATE_VERSION:
            raise ValueError(
                f"document session state requires version {self.SESSION_STATE_VERSION}"
            )
        records = data["documents"]
        if not isinstance(records, list):
            raise TypeError("document session records must be an array")
        required = {
            "document_id",
            "stable_id",
            "kind",
            "key",
            "title",
            "resource_path",
            "revision",
            "saved_revision",
            "external_revision",
            "state",
            "capabilities",
            "view_ids",
            "dirty_view_ids",
            "restore_state",
        }
        pending_records: dict[str, dict[str, Any]] = {}
        pending_by_view: dict[str, str] = {}
        for raw_record in records:
            if not isinstance(raw_record, dict):
                raise ValueError("document session record has an invalid field set")
            # Dirty ownership was added to the current session snapshot without
            # changing document identity. Older local session state simply
            # restores its dirty Scene state to the Scene View.
            if "dirty_view_ids" not in raw_record:
                raw_record = dict(raw_record)
                raw_record["dirty_view_ids"] = []
            if set(raw_record) != required:
                raise ValueError("document session record has an invalid field set")
            stable_id = str(raw_record["stable_id"] or "").strip()
            document_id = str(raw_record["document_id"] or "").strip()
            if not stable_id or not document_id:
                raise ValueError("document session record requires stable identities")
            if stable_id in pending_records:
                raise ValueError(f"duplicate document session stable id: {stable_id}")
            kind = DocumentKind(raw_record["kind"])
            key_data = raw_record["key"]
            if not isinstance(key_data, dict) or set(key_data) != {
                "identity_kind",
                "identity",
            }:
                raise ValueError("document session key has an invalid field set")
            key = DocumentKey(
                kind,
                DocumentIdentityKind(key_data["identity_kind"]),
                key_data["identity"],
            )
            revision = int(raw_record["revision"])
            saved_revision = int(raw_record["saved_revision"])
            external_revision = int(raw_record["external_revision"])
            if revision < 0 or saved_revision < 0 or saved_revision > revision:
                raise ValueError("document session revisions are invalid")
            if external_revision < 0:
                raise ValueError("document external revision is invalid")
            state = DocumentState(raw_record["state"])
            if state not in {DocumentState.READY, DocumentState.CONFLICT}:
                raise ValueError("document session state is not restorable")
            view_ids = raw_record["view_ids"]
            if not isinstance(view_ids, list) or not view_ids:
                raise ValueError("document session record requires at least one View")
            normalized_views: list[str] = []
            for value in view_ids:
                view_id = str(value or "").strip()
                if not view_id or view_id in pending_by_view:
                    raise ValueError(f"duplicate or empty document session View: {view_id}")
                normalized_views.append(view_id)
                pending_by_view[view_id] = stable_id
            dirty_view_ids = raw_record["dirty_view_ids"]
            if not isinstance(dirty_view_ids, list):
                raise ValueError("document session dirty View ids must be an array")
            normalized_dirty_views = tuple(
                dict.fromkeys(
                    str(value or "").strip()
                    for value in dirty_view_ids
                    if str(value or "").strip()
                )
            )
            if revision != saved_revision and not normalized_dirty_views and kind is DocumentKind.SCENE:
                normalized_dirty_views = ("scene_view",)
            record = copy.deepcopy(raw_record)
            record["kind"] = kind
            record["key"] = key
            record["view_ids"] = tuple(normalized_views)
            record["dirty_view_ids"] = normalized_dirty_views
            record["revision"] = revision
            record["saved_revision"] = saved_revision
            record["external_revision"] = external_revision
            record["state"] = state
            record["capabilities"] = DocumentCapability(raw_record["capabilities"])
            pending_records[stable_id] = record
        self._pending_session_records = pending_records
        self._pending_session_document_by_view = pending_by_view
        return len(self._pending_session_records)

    def claim_session_document(
        self,
        view_id: str,
        *,
        controller: DocumentController,
    ) -> tuple[EditorDocument, Any] | None:
        """Lazily restore the document previously owned by ``view_id``."""
        view = str(view_id or "").strip()
        stable_id = self._pending_session_document_by_view.pop(view, None)
        if stable_id is None:
            return None
        record = self._pending_session_records.get(stable_id)
        if record is None:
            raise RuntimeError("document session View points to a missing record")

        previous = self.document_for_view(view)
        if previous is not None:
            self.detach_view(view)
            if not previous.view_ids:
                self.unregister(previous.document_id, preserve_dormant=False)

        document = self.resolve_locator(
            DocumentLocator(
                stable_id,
                record["key"],
                resource_path=record["resource_path"],
                title=record["title"],
            )
        )
        first_claim = document is None
        if document is None:
            document = self.create(
                record["kind"],
                record["title"],
                document_id=record["document_id"],
                stable_id=stable_id,
                key=record["key"],
                resource_path=record["resource_path"],
                revision=record["revision"],
                saved_revision=record["saved_revision"],
                external_revision=record["external_revision"],
                state=record["state"],
                dirty_view_ids=record["dirty_view_ids"],
                capabilities=record["capabilities"],
                controller=controller,
            )
        elif document.controller is not controller:
            raise RuntimeError("multiple Panel controllers claimed one document session")
        self.attach_view(document.document_id, view)
        if not any(
            value == stable_id
            for value in self._pending_session_document_by_view.values()
        ):
            self._pending_session_records.pop(stable_id, None)
        return document, copy.deepcopy(record["restore_state"]) if first_claim else None

    def create(
        self,
        kind: DocumentKind,
        title: str,
        *,
        document_id: str = "",
        stable_id: str = "",
        key: Optional[DocumentKey] = None,
        resource_path: str = "",
        revision: int = 0,
        saved_revision: Optional[int] = None,
        external_revision: int = 0,
        state: DocumentState = DocumentState.READY,
        dirty_view_ids: tuple[str, ...] | list[str] | set[str] = (),
        capabilities: DocumentCapability = DocumentCapability.NONE,
        controller: Optional[DocumentController] = None,
    ) -> EditorDocument:
        identifier = str(document_id or uuid.uuid4().hex).strip()
        if not identifier:
            raise ValueError("document_id must not be empty")
        if identifier in self._documents:
            raise ValueError(f"document already registered: {identifier}")
        document_kind = DocumentKind(kind)
        document_key = key
        if document_key is None:
            document_key = (
                DocumentKey.resource(document_kind, resource_path)
                if resource_path
                else DocumentKey.session(document_kind, identifier)
            )
        elif document_key.kind is not document_kind:
            raise ValueError("document key kind must match the document kind")
        existing_id = self._document_ids_by_key.get(document_key)
        if existing_id is not None:
            raise ValueError(
                f"document key already registered by {existing_id}: {document_key}"
            )
        dormant = self._dormant_locators_by_key.get(document_key)
        if dormant is None and resource_path:
            resource_identity = path_key(resource_path)
            dormant = next(
                (
                    locator
                    for locator in self._dormant_locators_by_stable_id.values()
                    if locator.key_hint.kind is document_kind
                    and locator.resource_path
                    and path_key(locator.resource_path) == resource_identity
                ),
                None,
            )
        logical_id = str(stable_id or (dormant.stable_id if dormant else "") or uuid.uuid4().hex).strip()
        existing_stable_id = self._document_ids_by_stable_id.get(logical_id)
        if existing_stable_id is not None:
            raise ValueError(
                f"document stable identity already registered by {existing_stable_id}: {logical_id}"
            )
        current_revision = max(0, int(revision))
        clean_revision = (
            current_revision if saved_revision is None else max(0, int(saved_revision))
        )
        if clean_revision > current_revision:
            raise ValueError("saved_revision cannot exceed revision")
        current_external_revision = max(0, int(external_revision))
        initial_state = DocumentState(state)
        if initial_state in {DocumentState.SAVING, DocumentState.CLOSED}:
            raise ValueError("new documents cannot start in a transient or closed state")
        initial_dirty_views = {
            str(value or "").strip()
            for value in dirty_view_ids
            if str(value or "").strip()
        }
        if current_revision != clean_revision and not initial_dirty_views and document_kind is DocumentKind.SCENE:
            initial_dirty_views.add("scene_view")
        document = EditorDocument(
            document_id=identifier,
            stable_id=logical_id,
            key=document_key,
            kind=document_kind,
            title=str(title or identifier),
            resource_path=str(resource_path or ""),
            revision=current_revision,
            saved_revision=clean_revision,
            external_revision=current_external_revision,
            edit_revision=current_revision,
            persisted_revision=clean_revision,
            imported_disk_revision=current_external_revision,
            durable_file_state=_capture_durable_file_state(resource_path),
            state=initial_state,
            capabilities=DocumentCapability(capabilities),
            dirty_view_ids=initial_dirty_views,
            controller=controller,
            _revision_high_watermark=max(current_revision, clean_revision),
            _dirty_view_ids_by_revision={
                current_revision: frozenset(initial_dirty_views)
            },
        )
        self._documents[identifier] = document
        self._document_ids_by_key[document_key] = identifier
        self._document_ids_by_stable_id[logical_id] = identifier
        previous_dormant = self._dormant_locators_by_stable_id.pop(logical_id, None)
        if previous_dormant is not None:
            self._dormant_locators_by_key.pop(previous_dormant.key_hint, None)
        self._dormant_documents_by_stable_id.pop(logical_id, None)
        self._dormant_locators_by_key.pop(document_key, None)
        self._touch()
        return document

    def get(self, document_id: str) -> Optional[EditorDocument]:
        return self._documents.get(str(document_id or ""))

    def require(self, document_id: str) -> EditorDocument:
        document = self.get(document_id)
        if document is None:
            raise KeyError(f"unknown editor document: {document_id}")
        return document

    def get_by_key(self, key: DocumentKey) -> Optional[EditorDocument]:
        document_id = self._document_ids_by_key.get(key)
        return self.get(document_id or "")

    def locate(self, document_id: str) -> Optional[DocumentLocator]:
        """Capture a stable locator for one currently registered document."""
        document = self.get(document_id)
        if document is None:
            return None
        return DocumentLocator(
            document.stable_id,
            document.key,
            resource_path=document.resource_path,
            title=document.title,
        )

    def canonical_locator(self, locator: DocumentLocator) -> DocumentLocator:
        """Return the registry's current address for a document identity.

        History entries can outlive a Save As or a path-to-GUID promotion.  In
        that interval their key hint is intentionally stale, while the stable
        identity remains the same.  Restore callers must use the latest live
        or dormant locator before opening a document; otherwise the old key
        can create a second document with a different stable identity.
        """
        if not isinstance(locator, DocumentLocator):
            raise TypeError("document locator must be a DocumentLocator")

        document = self.resolve_locator(locator)
        # The stable identity is authoritative.  A stale history key may now
        # belong to another live document (for example after Save As followed
        # by a new document at the old path); never canonicalize that locator
        # to the other document merely because its key happens to match.
        if document is not None and (
            document.stable_id == locator.stable_id
            or self._is_unopened_resource_locator(locator)
        ):
            current = self.locate(document.document_id)
            if current is not None:
                return current

        dormant = self._dormant_locators_by_stable_id.get(locator.stable_id)
        if dormant is not None:
            return dormant
        return locator

    def locate_resource(
        self,
        kind: DocumentKind,
        resource_path: str,
        *,
        guid: str = "",
        title: str = "",
    ) -> DocumentLocator:
        """Build a stable locator for a live, dormant, or unopened resource."""
        document_kind = DocumentKind(kind)
        path = str(resource_path or "").strip()
        asset_guid = str(guid or "").strip()
        if not path:
            raise ValueError("document resource path must not be empty")
        key = (
            DocumentKey.asset(document_kind, asset_guid)
            if asset_guid
            else DocumentKey.resource(document_kind, path)
        )
        resource_identity = path_key(path)
        document = self.get_by_key(key)
        if document is None:
            document = next(
                (
                    candidate
                    for candidate in self._documents.values()
                    if candidate.kind is document_kind
                    and candidate.resource_path
                    and path_key(candidate.resource_path) == resource_identity
                ),
                None,
            )
        if document is not None:
            locator = self.locate(document.document_id)
            if locator is not None:
                return locator
        dormant = self._dormant_locators_by_key.get(key)
        if dormant is None:
            dormant = next(
                (
                    locator
                    for locator in self._dormant_locators_by_stable_id.values()
                    if locator.key_hint.kind is document_kind
                    and locator.resource_path
                    and path_key(locator.resource_path) == resource_identity
                ),
                None,
            )
        if dormant is not None:
            return dormant
        # Session documents are restored lazily by their owning Views.  A
        # resource may be opened through history or navigation before that
        # View claims its snapshot, so the pending record still owns the
        # durable identity for this key.
        pending = next(
            (
                record
                for record in self._pending_session_records.values()
                if record["key"] == key
                or (
                    record["kind"] is document_kind
                    and record["resource_path"]
                    and path_key(record["resource_path"]) == resource_identity
                )
            ),
            None,
        )
        if pending is not None:
            return DocumentLocator(
                str(pending["stable_id"]),
                key,
                resource_path=str(pending["resource_path"] or path),
                title=str(pending["title"] or title),
            )
        stable_seed = (
            f"infernux-editor-document:{document_kind.value}:"
            f"{key.identity_kind.value}:{key.identity}"
        )
        return DocumentLocator(
            uuid.uuid5(uuid.NAMESPACE_URL, stable_seed).hex,
            key,
            resource_path=path,
            title=str(title or ""),
        )

    def _is_unopened_resource_locator(self, locator: DocumentLocator) -> bool:
        """Identify a deterministic locator created before first open.

        ``locate_resource`` must return an address before an adapter creates a
        document, so it derives a UUID from the resource key.  The adapter may
        choose the real stable identity on that first open.  Once the derived
        identity is known to the registry (live, dormant, or pending session),
        key fallback is no longer allowed for it.
        """
        if not locator.resource_path:
            return False
        if locator.stable_id in self._document_ids_by_stable_id:
            return False
        if locator.stable_id in self._dormant_locators_by_stable_id:
            return False
        if locator.stable_id in self._pending_session_records:
            return False
        if locator.key_hint.identity_kind not in {
            DocumentIdentityKind.ASSET_GUID,
            DocumentIdentityKind.RESOURCE_PATH,
        }:
            return False
        stable_seed = (
            f"infernux-editor-document:{locator.key_hint.kind.value}:"
            f"{locator.key_hint.identity_kind.value}:{locator.key_hint.identity}"
        )
        return locator.stable_id == uuid.uuid5(uuid.NAMESPACE_URL, stable_seed).hex

    def resolve_locator(
        self,
        locator: Optional[DocumentLocator],
    ) -> Optional[EditorDocument]:
        """Resolve a locator without opening resources or mutating workspace state."""
        if locator is None:
            return None
        if not isinstance(locator, DocumentLocator):
            raise TypeError("document locator must be a DocumentLocator")
        document_id = self._document_ids_by_stable_id.get(locator.stable_id)
        document = self.get(document_id or "")
        if document is not None:
            return document
        document = self.get_by_key(locator.key_hint)
        if document is not None and (
            document.stable_id == locator.stable_id
            or self._is_unopened_resource_locator(locator)
        ):
            return document
        return None

    def open_or_create(
        self,
        key: DocumentKey,
        title: str,
        *,
        stable_id: str = "",
        resource_path: str = "",
        revision: int = 0,
        saved_revision: Optional[int] = None,
        capabilities: DocumentCapability = DocumentCapability.NONE,
        controller: Optional[DocumentController] = None,
    ) -> tuple[EditorDocument, bool]:
        requested_stable_id = str(stable_id or "").strip()

        # History restoration may have a canonical key while the document is
        # dormant.  Revive by stable identity before consulting the key map so
        # a matching path can never silently replace the history document.
        if requested_stable_id:
            key_owner = self.get_by_key(key)
            if key_owner is not None and key_owner.stable_id != requested_stable_id:
                raise RuntimeError(
                    "document key is owned by a different stable identity: "
                    f"{key_owner.stable_id} != {requested_stable_id}"
                )
            live = self.get(
                self._document_ids_by_stable_id.get(requested_stable_id, "")
            )
            if live is not None:
                if live.key != key and (key_owner is None or key_owner is live):
                    self.rekey(live.document_id, key, resource_path=resource_path)
                self.update_metadata(
                    live.document_id,
                    title=title,
                    resource_path=resource_path,
                    capabilities=capabilities,
                    controller=controller,
                )
                return live, False

            dormant = self._dormant_locators_by_stable_id.get(requested_stable_id)
            if dormant is not None:
                document = self.restore_dormant(dormant, controller=controller)
                if document is None:
                    raise RuntimeError("failed to restore stable editor document")
                if document.key != key and (key_owner is None or key_owner is document):
                    self.rekey(document.document_id, key, resource_path=resource_path)
                self.update_metadata(
                    document.document_id,
                    title=title,
                    resource_path=resource_path,
                    capabilities=capabilities,
                    controller=controller,
                )
                return document, False

        existing = self.get_by_key(key)
        if existing is None and resource_path:
            resource_identity = path_key(resource_path)
            existing = next(
                (
                    document
                    for document in self._documents.values()
                    if document.kind is key.kind
                    and document.resource_path
                    and path_key(document.resource_path) == resource_identity
                ),
                None,
            )
            if existing is not None and existing.key != key:
                self.rekey(
                    existing.document_id,
                    key,
                    resource_path=resource_path,
                )
        if existing is not None:
            self.update_metadata(
                existing.document_id,
                title=title,
                resource_path=resource_path,
                capabilities=capabilities,
                controller=controller,
            )
            return existing, False
        dormant_locator = self._dormant_locators_by_key.get(key)
        if dormant_locator is None and resource_path:
            resource_identity = path_key(resource_path)
            dormant_locator = next(
                (
                    locator
                    for locator in self._dormant_locators_by_stable_id.values()
                    if locator.key_hint.kind is key.kind
                    and locator.resource_path
                    and path_key(locator.resource_path) == resource_identity
                ),
                None,
            )
        if (
            dormant_locator is not None
            and dormant_locator.stable_id in self._dormant_documents_by_stable_id
        ):
            document = self.restore_dormant(
                dormant_locator,
                controller=controller,
            )
            if document is None:
                raise RuntimeError("failed to restore dormant editor document")
            if document.key != key:
                self.rekey(document.document_id, key, resource_path=resource_path)
            self.update_metadata(
                document.document_id,
                title=title,
                resource_path=resource_path,
                capabilities=capabilities,
                controller=controller,
            )
            return document, False
        return (
            self.create(
                key.kind,
                title,
                key=key,
                stable_id=requested_stable_id,
                resource_path=resource_path,
                revision=revision,
                saved_revision=saved_revision,
                capabilities=capabilities,
                controller=controller,
            ),
            True,
        )

    def rekey(
        self,
        document_id: str,
        key: DocumentKey,
        *,
        resource_path: Optional[str] = None,
    ) -> bool:
        document = self.require(document_id)
        if key.kind is not document.kind:
            raise ValueError("document key kind must match the document kind")
        if key == document.key:
            if resource_path is not None:
                return self.update_metadata(document_id, resource_path=resource_path)
            return False
        existing_id = self._document_ids_by_key.get(key)
        if existing_id is not None and existing_id != document.document_id:
            raise ValueError(f"document key already registered by {existing_id}: {key}")
        del self._document_ids_by_key[document.key]
        self._document_ids_by_key[key] = document.document_id
        document.key = key
        if resource_path is not None:
            document.resource_path = str(resource_path)
            document.durable_file_state = _capture_durable_file_state(
                document.resource_path
            )
        self._touch()
        return True

    def document_for_view(self, view_id: str) -> Optional[EditorDocument]:
        document_id = self._view_documents.get(str(view_id or ""))
        return self.get(document_id or "")

    def attach_view(self, document_id: str, view_id: str) -> bool:
        document = self.require(document_id)
        view_id = str(view_id or "").strip()
        if not view_id:
            raise ValueError("document view_id must not be empty")
        previous_id = self._view_documents.get(view_id)
        if previous_id == document.document_id:
            return False
        if previous_id:
            previous = self.get(previous_id)
            if previous is not None:
                previous.view_ids.discard(view_id)
        self._view_documents[view_id] = document.document_id
        document.view_ids.add(view_id)
        self._touch()
        return True

    def replace_view_document(self, document_id: str, view_id: str) -> bool:
        """Destructively replace the document owned by one authoring view.

        Explicit New/Open/Load operations have already decided to abandon the
        previous in-memory document. Capturing that previous document through
        the same panel controller after the replacement model is staged can
        overwrite the new model. This operation therefore retires an orphaned
        previous document without creating dormant restore state.
        """
        document = self.require(document_id)
        view_id = str(view_id or "").strip()
        if not view_id:
            raise ValueError("document view_id must not be empty")
        previous_id = self._view_documents.get(view_id)
        if previous_id == document.document_id:
            return False
        if previous_id:
            previous = self.get(previous_id)
            self.detach_view(view_id)
            if previous is not None and not previous.view_ids:
                self.unregister(previous_id, preserve_dormant=False)
        self.attach_view(document.document_id, view_id)
        return True

    def detach_view(self, view_id: str) -> Optional[str]:
        view_id = str(view_id or "").strip()
        document_id = self._view_documents.pop(view_id, None)
        if document_id is None:
            return None
        document = self.get(document_id)
        if document is not None:
            document.view_ids.discard(view_id)
        self._touch()
        return document_id

    def close_view(
        self,
        view_id: str,
        *,
        preserve_dormant: bool = True,
    ) -> Optional[str]:
        """Detach one view and retire its document when no views remain."""
        document_id = self.detach_view(view_id)
        if document_id is None:
            return None
        document = self.get(document_id)
        if document is not None and not document.view_ids:
            self.unregister(document_id, preserve_dormant=preserve_dormant)
        return document_id

    def unregister(self, document_id: str, *, preserve_dormant: bool = True) -> bool:
        identifier = str(document_id or "")
        document = self._documents.get(identifier)
        if document is None:
            return False
        restore_state = None
        if preserve_dormant:
            capture = getattr(
                document.controller,
                "capture_document_restore_state",
                None,
            )
            if callable(capture):
                try:
                    # Capture while the document is still fully registered. A
                    # controller may need its resource path, revision, or view
                    # ownership to produce an exact dormant session snapshot.
                    restore_state = copy.deepcopy(capture(document.document_id))
                except Exception as exc:
                    from Infernux.debug import Debug

                    Debug.log_suppressed(
                        "DocumentRegistry.unregister.capture_restore_state",
                        exc,
                    )
        self._documents.pop(identifier)
        self._deferred_save_requests.pop(document.document_id, None)
        self._pending_external_reloads.discard(document.document_id)
        self._external_reload_results.pop(document.document_id, None)
        self._document_ids_by_key.pop(document.key, None)
        self._document_ids_by_stable_id.pop(document.stable_id, None)
        locator = DocumentLocator(
            document.stable_id,
            document.key,
            resource_path=document.resource_path,
            title=document.title,
        )
        if not preserve_dormant:
            previous = self._dormant_locators_by_stable_id.pop(
                document.stable_id, None
            )
            if previous is not None:
                self._dormant_locators_by_key.pop(previous.key_hint, None)
            self._dormant_locators_by_key.pop(document.key, None)
            self._dormant_documents_by_stable_id.pop(document.stable_id, None)
            self._touch()
            return True
        previous = self._dormant_locators_by_stable_id.get(document.stable_id)
        if previous is not None:
            self._dormant_locators_by_key.pop(previous.key_hint, None)
        self._dormant_locators_by_stable_id[document.stable_id] = locator
        self._dormant_locators_by_key[document.key] = locator
        self._dormant_documents_by_stable_id[document.stable_id] = (
            _DormantDocumentRecord(document, restore_state)
        )
        ticket_id = self._active_save_by_document.pop(document.document_id, None)
        if ticket_id:
            ticket = self._save_tickets.get(ticket_id)
            if ticket is not None and ticket.is_pending:
                ticket.status = SaveTicketStatus.CANCELLED
                ticket.message = "document was closed"
        for view_id in tuple(document.view_ids):
            self._view_documents.pop(view_id, None)
        document.view_ids.clear()
        document.state = DocumentState.CLOSED
        self._touch()
        return True

    def restore_dormant(
        self,
        locator: DocumentLocator,
        *,
        controller: Optional[DocumentController] = None,
    ) -> Optional[EditorDocument]:
        """Revive a closed document without changing its history identity."""
        if not isinstance(locator, DocumentLocator):
            raise TypeError("document locator must be a DocumentLocator")
        live = self.resolve_locator(locator)
        if live is not None:
            if controller is not None:
                live.controller = controller
            return live
        record = self._dormant_documents_by_stable_id.pop(locator.stable_id, None)
        if record is None:
            return None
        document = record.document
        if document.document_id in self._documents:
            raise RuntimeError(f"document id is already live: {document.document_id}")
        if document.key in self._document_ids_by_key:
            raise RuntimeError(f"document key is already live: {document.key}")
        document.state = DocumentState.READY
        document.controller = controller
        document.view_ids.clear()
        self._documents[document.document_id] = document
        self._document_ids_by_key[document.key] = document.document_id
        self._document_ids_by_stable_id[document.stable_id] = document.document_id
        dormant_locator = self._dormant_locators_by_stable_id.pop(
            document.stable_id, None
        )
        if dormant_locator is not None:
            self._dormant_locators_by_key.pop(dormant_locator.key_hint, None)
        self._touch()
        return document

    def dormant_restore_state(self, locator: DocumentLocator) -> Any:
        """Return an isolated authoring snapshot for a dormant document."""
        record = self._dormant_documents_by_stable_id.get(locator.stable_id)
        return copy.deepcopy(record.restore_state) if record is not None else None

    def update_metadata(
        self,
        document_id: str,
        *,
        title: Optional[str] = None,
        resource_path: Optional[str] = None,
        capabilities: Optional[DocumentCapability] = None,
        controller: Optional[DocumentController] = None,
    ) -> bool:
        document = self.require(document_id)
        changed = False
        if title is not None and document.title != str(title):
            document.title = str(title)
            changed = True
        if resource_path is not None and document.resource_path != str(resource_path):
            document.resource_path = str(resource_path)
            document.durable_file_state = _capture_durable_file_state(
                document.resource_path
            )
            changed = True
        if capabilities is not None and document.capabilities != capabilities:
            document.capabilities = DocumentCapability(capabilities)
            changed = True
        if controller is not None and document.controller is not controller:
            document.controller = controller
            changed = True
        if changed:
            self._touch()
        return changed

    def remap_resource_path(
        self,
        source_path: str,
        destination_path: str,
        *,
        guid: str = "",
        title: str = "",
    ) -> tuple[str, ...]:
        """Move every open projection of one resource as one registry edit."""
        source_key = path_key(source_path)
        destination = str(destination_path or "")
        if not source_key or not destination:
            raise ValueError("document resource remap requires source and destination")

        affected = tuple(
            document
            for document in self._documents.values()
            if document.resource_path and path_key(document.resource_path) == source_key
        )
        for document in affected:
            if self.active_save_ticket(document.document_id) is not None:
                raise RuntimeError(
                    f"cannot move resource while document is saving: {document.title}"
                )
            if document.key.identity_kind is DocumentIdentityKind.RESOURCE_PATH:
                next_key = DocumentKey.resource(document.kind, destination)
                existing_id = self._document_ids_by_key.get(next_key)
                if existing_id is not None and existing_id != document.document_id:
                    raise ValueError(
                        f"resource destination already has an open document: {destination}"
                    )

        remapped_ids: list[str] = []
        for document in affected:
            if document.key.identity_kind is DocumentIdentityKind.RESOURCE_PATH:
                self.rekey(
                    document.document_id,
                    DocumentKey.resource(document.kind, destination),
                    resource_path=destination,
                )
            else:
                self.update_metadata(document.document_id, resource_path=destination)
            if title:
                self.update_metadata(document.document_id, title=title)
            remapped_ids.append(document.document_id)

        for document in affected:
            callback = getattr(document.controller, "resource_moved", None)
            if not callable(callback):
                continue
            try:
                callback(
                    document_id=document.document_id,
                    source_path=str(source_path),
                    destination_path=destination,
                    guid=str(guid or ""),
                )
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("DocumentRegistry.resource_moved", exc)
        return tuple(remapped_ids)

    def preflight_resource_remaps(self, remaps) -> None:
        """Validate a complete relocation batch without changing document state."""
        pairs = tuple((path_key(source), str(destination or "")) for source, destination in remaps)
        if any(not source or not destination for source, destination in pairs):
            raise ValueError("document resource remap requires source and destination")
        destinations: dict[DocumentKey, str] = {}
        for document in self._documents.values():
            source_key = path_key(document.resource_path) if document.resource_path else ""
            destination = next((value for source, value in pairs if source == source_key), "")
            if not destination:
                continue
            if self.active_save_ticket(document.document_id) is not None:
                raise RuntimeError(
                    f"cannot move resource while document is saving: {document.title}"
                )
            if document.key.identity_kind is not DocumentIdentityKind.RESOURCE_PATH:
                continue
            next_key = DocumentKey.resource(document.kind, destination)
            existing_id = self._document_ids_by_key.get(next_key)
            if existing_id is not None and existing_id != document.document_id:
                raise ValueError(
                    f"resource destination already has an open document: {destination}"
                )
            previous = destinations.setdefault(next_key, document.document_id)
            if previous != document.document_id:
                raise ValueError(f"multiple documents relocate to the same key: {destination}")

    @staticmethod
    def _resolve_dirty_view_id(document: EditorDocument, view_id: str) -> str:
        candidate = str(view_id or "").strip()
        if not candidate:
            try:
                from .contexts import FocusService

                candidate = str(
                    FocusService.instance().snapshot.active_view_id or ""
                ).strip()
            except (AttributeError, ImportError, RuntimeError):
                candidate = ""

        if document.kind is DocumentKind.SCENE:
            if candidate == "ui_editor":
                return "ui_editor"
            if candidate == "inspector":
                try:
                    from .selection import SelectionService

                    if SelectionService.instance().snapshot.owner_id == "ui_editor":
                        return "ui_editor"
                except (AttributeError, ImportError, RuntimeError):
                    pass
            # Hierarchy, Inspector and scene-level commands author the Scene
            # document. Game View is a read-only projection and never owns a
            # dirty marker.
            return "scene_view"

        if candidate in document.view_ids:
            return candidate
        if len(document.view_ids) == 1:
            return next(iter(document.view_ids))
        return candidate

    def mark_changed(self, document_id: str, *, view_id: str = "") -> int:
        document = self.require(document_id)
        document.revision = self.reserve_changed_revision(
            document_id,
            view_id=view_id,
        )
        document.dirty_view_ids = set(
            document._dirty_view_ids_by_revision[document.revision]
        )
        if (
            document.state is not DocumentState.CONFLICT
            and self.active_save_ticket(document_id) is None
        ):
            document.state = DocumentState.READY
        self._touch()
        return document.revision

    def reserve_content_revision(self, document_id: str) -> int:
        """Allocate a future content token without changing the visible document state."""
        document = self.require(document_id)
        document._revision_high_watermark += 1
        document.edit_revision = max(document.edit_revision, document._revision_high_watermark)
        return document._revision_high_watermark

    def reserve_changed_revision(self, document_id: str, *, view_id: str = "") -> int:
        """Allocate one authored revision and freeze its dirty-view ownership.

        Commands reserve their destination revision before execution so they
        can replay an exact revision during Undo/Redo.  Recording ownership at
        reservation time is equally important: command execution may move
        focus, and a shared document may have several views.  A bare reserved
        revision would otherwise become dirty with no owner and later choose a
        view by incidental sort order.
        """
        document = self.require(document_id)
        owners = set(document.dirty_owner_view_ids()) if document.is_dirty else set()
        owner = self._resolve_dirty_view_id(document, view_id)
        if owner:
            owners.add(owner)
        revision = self.reserve_content_revision(document_id)
        document._dirty_view_ids_by_revision[revision] = frozenset(owners)
        return revision

    def mark_saved(self, document_id: str, revision: Optional[int] = None) -> int:
        document = self.require(document_id)
        target = document.revision if revision is None else int(revision)
        if target < 0 or target > document._revision_high_watermark:
            raise ValueError("saved revision must reference an existing document revision")
        document.saved_revision = target
        document.persisted_revision = target
        document._dirty_view_ids_by_revision[target] = frozenset()
        if document.revision == target:
            document.dirty_view_ids.clear()
        document.state = DocumentState.READY
        self._touch()
        return target

    def establish_loaded_baseline(self, document_id: str) -> int:
        """Publish content loaded from an authoritative persisted source.

        Loading/reloading is not a save operation.  It replaces the in-memory
        authoring model with a new clean baseline and therefore receives a new
        content revision whose current and saved positions are identical.
        Panels must not use this to acknowledge a write; successful writes are
        committed exclusively through :meth:`complete_save`.
        """
        document = self.require(document_id)
        if self.active_save_ticket(document.document_id) is not None:
            raise RuntimeError(
                f"cannot replace loaded content while document is saving: {document.title}"
            )
        revision = self.reserve_content_revision(document.document_id)
        document.revision = revision
        document.saved_revision = revision
        document.persisted_revision = revision
        document.imported_disk_revision += 1
        document.preview_dependency_revision += 1
        document.dirty_view_ids.clear()
        document._dirty_view_ids_by_revision[revision] = frozenset()
        document.state = DocumentState.READY
        self._touch()
        return revision

    def restore_saved_revision(self, document_id: str) -> int:
        """Move authored content back to its existing durable save point.

        This never advances ``saved_revision``.  It is the revision-side commit
        for a successful discard or for Undo returning to the exact persisted
        state.
        """
        document = self.require(document_id)
        if self.active_save_ticket(document.document_id) is not None:
            raise RuntimeError(
                f"cannot discard content while document is saving: {document.title}"
            )
        return self.restore_content_revision(
            document.document_id,
            document.saved_revision,
        )

    def abandon_session_changes(self, document_id: str) -> int:
        """Forget unsaved content without rebuilding its in-memory model.

        This is reserved for terminal lifecycle operations such as exiting the
        Editor.  The process is about to discard the live model, so reloading a
        graph, scene, or asset solely to make it clean wastes work.  Suppressing
        its session snapshot is essential: otherwise the unchanged live model
        would be serialized as a draft immediately before shutdown.
        """
        document = self.require(document_id)
        if self.active_save_ticket(document.document_id) is not None:
            raise RuntimeError(
                f"cannot abandon content while document is saving: {document.title}"
            )
        document.revision = document.saved_revision
        document.dirty_view_ids.clear()
        document._dirty_view_ids_by_revision[document.revision] = frozenset()
        document.state = DocumentState.READY
        stable_id = document.stable_id
        self._session_restore_suppressed_stable_ids.add(stable_id)
        self._session_restore_suppressed_view_ids.update(document.view_ids)
        self._pending_session_records.pop(stable_id, None)
        self._pending_session_document_by_view = {
            view_id: pending_stable_id
            for view_id, pending_stable_id in self._pending_session_document_by_view.items()
            if pending_stable_id != stable_id
        }
        self._touch()
        return document.revision

    def retire_deleted_resource_document(self, document_id: str) -> tuple[str, ...]:
        """Prepare a non-scene document for terminal closure after deletion.

        A deleted asset has no durable source to reload or preserve as a
        dormant editor draft.  Pending saves are cancelled, future autosaves
        are disarmed, and every owning view is returned to the window layer so
        it can close the corresponding authoring surface without prompting.
        """
        document = self.require(document_id)
        if document.kind is DocumentKind.SCENE:
            raise ValueError("scene deletion requires scene conflict arbitration")

        ticket_id = self._active_save_by_document.pop(document.document_id, None)
        if ticket_id:
            ticket = self._save_tickets.get(ticket_id)
            if ticket is not None and ticket.is_pending:
                ticket.status = SaveTicketStatus.CANCELLED
                ticket.message = "document resource was deleted"

        try:
            from Infernux.core.assets import AssetManager

            AssetManager.cancel_scheduled_save(document.resource_path)
        except (ImportError, RuntimeError):
            pass

        callback = getattr(document.controller, "resource_deleted", None)
        if callable(callback):
            try:
                callback(
                    document_id=document.document_id,
                    resource_path=document.resource_path,
                )
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("DocumentRegistry.resource_deleted", exc)

        views = tuple(document.view_ids)
        self.abandon_session_changes(document.document_id)
        document.durable_file_state = _capture_durable_file_state(
            document.resource_path
        )
        return views

    def restore_content_revision(self, document_id: str, revision: int) -> int:
        """Move a document through existing content history without moving its save point."""
        document = self.require(document_id)
        target = int(revision)
        if target < 0 or target > document._revision_high_watermark:
            raise ValueError("content revision must reference an existing document revision")
        if document.revision == target:
            return target
        document.revision = target
        document.edit_revision = max(document.edit_revision, target)
        document.dirty_view_ids = set(
            document._dirty_view_ids_by_revision.get(target, ())
        )
        if target == document.saved_revision:
            document.dirty_view_ids.clear()
            document._dirty_view_ids_by_revision[target] = frozenset()
        if (
            document.state is not DocumentState.CONFLICT
            and self.active_save_ticket(document_id) is None
        ):
            document.state = DocumentState.READY
        self._touch()
        return target

    def mark_conflict(self, document_id: str) -> None:
        document = self.require(document_id)
        if document.state is DocumentState.CONFLICT:
            return
        document.state = DocumentState.CONFLICT
        self._touch()

    def documents_for_resource(self, resource_path: str) -> tuple[EditorDocument, ...]:
        """Return every live document whose durable source is ``resource_path``."""
        identity = path_key(resource_path)
        if not identity:
            return ()
        return tuple(
            document
            for document in self._documents.values()
            if document.resource_path and path_key(document.resource_path) == identity
        )

    @staticmethod
    def _reload_callback(document: EditorDocument):
        """Resolve the durable reload hook without weakening document identity.

        Scene documents can briefly outlive their view/controller during scene
        navigation and session restoration.  The active SceneFileManager is
        the canonical controller for that one document, so recover the binding
        here instead of making every watcher and conflict presenter special
        case scenes independently.
        """
        if document.kind is DocumentKind.SCENE:
            try:
                from Infernux.engine.scene_manager import SceneFileManager

                manager = SceneFileManager.instance()
            except (ImportError, RuntimeError):
                manager = None
            if manager is not None and manager.owns_document(document.document_id):
                document.controller = manager
                return getattr(manager, "reload_from_resource", None)
            if manager is not None and isinstance(document.controller, SceneFileManager):
                # Never send a scene reload through a retired manager binding.
                return None
        callback = getattr(document.controller, "reload_from_resource", None)
        return callback if callable(callback) else None

    @staticmethod
    def _external_reload_callback(document: EditorDocument):
        """Resolve the explicit user-confirmed durable reload entry point."""
        if document.kind is DocumentKind.SCENE:
            try:
                from Infernux.engine.scene_manager import SceneFileManager

                manager = SceneFileManager.instance()
            except (ImportError, RuntimeError):
                manager = None
            if manager is not None and manager.owns_document(document.document_id):
                document.controller = manager
                callback = getattr(manager, "request_external_reload", None)
                if callable(callback):
                    return callback
            if manager is not None and isinstance(document.controller, SceneFileManager):
                return None
        callback = getattr(document.controller, "request_external_reload", None)
        if callable(callback):
            return callback
        return DocumentRegistry._reload_callback(document)

    def durable_resource_content_changed(
        self,
        resource_path: str,
        *,
        deleted: bool = False,
    ) -> Optional[bool]:
        """Compare the current disk identity with all live document baselines.

        ``False`` means the watcher event is stale or a duplicate. ``True``
        means the bytes/existence really changed. ``None`` means the registry
        lacks a trustworthy baseline and the caller should defer until the
        filesystem identity can be captured; it must not manufacture a
        conflict from that uncertainty.
        """
        affected = self.documents_for_resource(resource_path)
        if not affected:
            return True
        current = _capture_durable_file_state(resource_path)
        current_identity = _durable_file_identity(current)
        if current_identity is None:
            return None
        baselines = tuple(
            _durable_file_identity(document.durable_file_state)
            for document in affected
        )
        if any(identity is None for identity in baselines):
            return None
        changed = any(identity != current_identity for identity in baselines)
        if deleted and current_identity[0]:
            # A delete notification followed by an atomic replacement is a
            # stale delete, not a deletion of the durable resource.
            return changed
        return changed

    def preflight_external_resource_change(
        self,
        resource_path: str,
        *,
        deleted: bool = False,
    ) -> bool:
        """Reserve one real content change before AssetManager mutates resources.

        Scene documents preserve conflict arbitration because a scene owns a
        graph of live references. Other asset documents follow the durable
        file automatically: local drafts/pending saves are cancelled before
        reimport and the current disk content becomes the new clean baseline.
        A watcher event whose durable identity still matches every live
        baseline is a no-op.
        """
        identity = path_key(resource_path)
        affected = self.documents_for_resource(resource_path)
        if not affected:
            return True
        if identity in self._external_change_preflights:
            return True

        content_changed = self.durable_resource_content_changed(
            resource_path,
            deleted=deleted,
        )
        if content_changed is False:
            return False
        if content_changed is None:
            return False

        for document in affected:
            document.external_revision += 1

        scene_documents = tuple(
            document
            for document in affected
            if document.kind is DocumentKind.SCENE
        )
        blocked_scene = any(
            bool(deleted)
            or document.is_dirty
            or self.active_save_ticket(document.document_id) is not None
            or not callable(self._reload_callback(document))
            for document in scene_documents
        )
        if blocked_scene:
            for document in scene_documents:
                document.state = DocumentState.CONFLICT
            self._touch()
            return False

        for document in affected:
            if document.kind is DocumentKind.SCENE:
                continue
            ticket_id = self._active_save_by_document.pop(
                document.document_id,
                None,
            )
            if ticket_id:
                ticket = self._save_tickets.get(ticket_id)
                if ticket is not None and ticket.is_pending:
                    ticket.status = SaveTicketStatus.CANCELLED
                    ticket.message = "superseded by an external asset change"
            try:
                from Infernux.core.assets import AssetManager

                AssetManager.cancel_scheduled_save(document.resource_path)
            except (ImportError, RuntimeError):
                pass

        self._external_change_preflights[identity] = tuple(
            document.document_id for document in affected
        )
        self._touch()
        return True

    def has_pending_external_change_preflight(self, resource_path: str) -> bool:
        """Return whether a watcher change still awaits successful publish."""
        identity = path_key(resource_path)
        return bool(identity and identity in self._external_change_preflights)

    def fail_external_resource_change(
        self,
        resource_path: str,
        *,
        message: str = "",
    ) -> tuple[str, ...]:
        """Abort an approved reimport and preserve every live document."""
        del message
        identity = path_key(resource_path)
        document_ids = self._external_change_preflights.pop(identity, ())
        for document_id in document_ids:
            document = self.get(document_id)
            if document is not None:
                document.state = DocumentState.CONFLICT
        if document_ids:
            self._touch()
        return tuple(document_ids)

    def publish_external_resource_change(
        self,
        resource_path: str,
        *,
        deleted: bool = False,
    ) -> tuple[str, ...]:
        """Apply one watcher-confirmed external content revision.

        Scene conflicts retain their in-memory graph for explicit arbitration.
        Every other asset document follows the durable source automatically;
        deletion is finalized by the window lifecycle subscriber. External
        changes are consequences, never user actions, so this method does not
        touch the global action journal.
        """
        identity = path_key(resource_path)
        document_ids = self._external_change_preflights.pop(identity, ())
        if not document_ids:
            if not self.preflight_external_resource_change(
                resource_path,
                deleted=deleted,
            ):
                return tuple(
                    document.document_id
                    for document in self.documents_for_resource(resource_path)
                )
            document_ids = self._external_change_preflights.pop(identity, ())

        affected = tuple(
            document
            for document_id in document_ids
            if (document := self.get(document_id)) is not None
        )
        for document in affected:
            if deleted:
                if document.kind is DocumentKind.SCENE:
                    document.state = DocumentState.CONFLICT
                else:
                    document.state = DocumentState.READY
                    document.durable_file_state = _capture_durable_file_state(
                        resource_path
                    )
                continue
            reload_from_disk = self._reload_callback(document)
            if not callable(reload_from_disk):
                document.state = DocumentState.CONFLICT
                continue
            try:
                result = reload_from_disk(
                    document_id=document.document_id,
                    resource_path=resource_path,
                )
            except Exception:
                document.state = DocumentState.CONFLICT
                continue
            if isinstance(result, DocumentActionResult):
                reloaded = result.status in {
                    DocumentActionStatus.APPLIED,
                    DocumentActionStatus.NO_OP,
                }
            else:
                reloaded = result is not False
            current = self.get(document.document_id)
            if not reloaded or current is None:
                if current is not None:
                    current.state = DocumentState.CONFLICT
                continue
            self.establish_loaded_baseline(current.document_id)
            current.durable_file_state = _capture_durable_file_state(resource_path)
            current.state = DocumentState.READY
        if affected:
            self._touch()
        return tuple(document.document_id for document in affected)

    def request_reload_external(self, document_id: str) -> DocumentActionResult:
        """Replace one conflicted draft with the current durable resource."""
        document = self.require(document_id)
        if document.state is not DocumentState.CONFLICT:
            return DocumentActionResult(DocumentActionStatus.NO_OP)
        if self.active_save_ticket(document.document_id) is not None:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the external conflict cannot reload while a save is pending",
            )
        if document.document_id in self._pending_external_reloads:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        self._external_reload_results.pop(document.document_id, None)
        reload_from_disk = self._external_reload_callback(document)
        if not callable(reload_from_disk):
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the document controller cannot reload its durable resource",
            )
        try:
            result = reload_from_disk(
                document_id=document.document_id,
                resource_path=document.resource_path,
            )
        except Exception as exc:
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        if isinstance(result, DocumentActionResult):
            if result.status is DocumentActionStatus.PENDING:
                self._pending_external_reloads.add(document.document_id)
                return result
            if result.status not in {
                DocumentActionStatus.APPLIED,
                DocumentActionStatus.NO_OP,
            }:
                return result
        elif result is False:
            return DocumentActionResult(
                DocumentActionStatus.FAILED,
                "the document controller rejected the durable reload",
            )
        current = self.require(document.document_id)
        self.establish_loaded_baseline(current.document_id)
        current.durable_file_state = _capture_durable_file_state(
            current.resource_path
        )
        return DocumentActionResult(DocumentActionStatus.APPLIED)

    def complete_external_reload(
        self,
        document_id: str,
        *,
        success: bool,
        message: str = "",
    ) -> DocumentActionResult:
        """Complete a controller-owned asynchronous durable reload."""
        identifier = str(document_id or "")
        self._pending_external_reloads.discard(identifier)
        document = self.get(identifier)
        if document is None:
            result = DocumentActionResult(
                DocumentActionStatus.FAILED,
                message or "the document closed before durable reload completed",
            )
        elif success:
            self.establish_loaded_baseline(identifier)
            document = self.require(identifier)
            document.durable_file_state = _capture_durable_file_state(
                document.resource_path
            )
            document.state = DocumentState.READY
            result = DocumentActionResult(DocumentActionStatus.APPLIED)
        else:
            document.state = DocumentState.CONFLICT
            result = DocumentActionResult(
                DocumentActionStatus.FAILED,
                message or "the document controller rejected the durable reload",
            )
            self._touch()
        self._external_reload_results[identifier] = result
        return result

    def take_external_reload_result(
        self,
        document_id: str,
    ) -> Optional[DocumentActionResult]:
        """Consume the completion result of an asynchronous durable reload."""
        return self._external_reload_results.pop(str(document_id or ""), None)

    def resolve_conflict_keep_local(self, document_id: str) -> DocumentActionResult:
        """Acknowledge the external revision while retaining the local draft."""
        document = self.require(document_id)
        if document.state is not DocumentState.CONFLICT:
            return DocumentActionResult(DocumentActionStatus.NO_OP)
        if self.active_save_ticket(document.document_id) is not None:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the external conflict cannot be resolved while a save is pending",
            )
        document.state = DocumentState.READY
        self._touch()
        return DocumentActionResult(DocumentActionStatus.APPLIED)

    def restore_revision_state(
        self,
        document_id: str,
        *,
        revision: int,
        saved_revision: int,
        state: DocumentState = DocumentState.READY,
    ) -> None:
        """Restore editor-only revision state after a transient runtime session."""
        document = self.require(document_id)
        current_revision = max(0, int(revision))
        clean_revision = max(0, int(saved_revision))
        restored_state = DocumentState(state)
        if restored_state is DocumentState.CLOSED:
            raise ValueError("an open document cannot restore CLOSED state")
        if (
            document.revision == current_revision
            and document.saved_revision == clean_revision
            and document.state is restored_state
        ):
            return
        document.revision = current_revision
        document.saved_revision = clean_revision
        document.dirty_view_ids = set(
            document._dirty_view_ids_by_revision.get(current_revision, ())
        )
        if current_revision == clean_revision:
            document.dirty_view_ids.clear()
            document._dirty_view_ids_by_revision[current_revision] = frozenset()
        document._revision_high_watermark = max(
            document._revision_high_watermark,
            current_revision,
            clean_revision,
        )
        document.state = restored_state
        self._touch()

    def dirty_documents(self) -> tuple[EditorDocument, ...]:
        return tuple(document for document in self._documents.values() if document.is_dirty)

    def request_save(
        self,
        document_id: str,
        *,
        save_as: bool = False,
    ) -> DocumentActionResult:
        document = self.require(document_id)
        capability = DocumentCapability.SAVE_AS if save_as else DocumentCapability.SAVE
        if not document.capabilities & capability:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "save is not supported")
        if document.state is DocumentState.CONFLICT and not save_as:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the document changed outside the Editor; reload it, keep the local draft, or save a copy",
            )
        if not save_as and not document.is_dirty:
            return DocumentActionResult(DocumentActionStatus.NO_OP)
        active = self.active_save_ticket(document_id)
        if active is not None:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        controller = document.controller
        if controller is None:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "document has no controller")
        ticket = self.begin_save(document_id, save_as=save_as)
        return self._invoke_save_controller(
            ticket,
            lambda: controller.save(ticket=ticket, save_as=save_as),
        )

    def request_save_to_resource(
        self,
        document_id: str,
        resource_path: str,
    ) -> DocumentActionResult:
        """Save As to an explicit path without bypassing the SaveTicket contract."""
        document = self.require(document_id)
        target = str(resource_path or "").strip()
        if not target:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "save resource path is missing",
            )
        if not document.capabilities & DocumentCapability.SAVE_AS:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "Save As is not supported",
            )
        if self.active_save_ticket(document_id) is not None:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        controller = document.controller
        save_to_resource = getattr(controller, "save_to_resource", None)
        if controller is None or not callable(save_to_resource):
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "document controller does not support explicit resource saves",
            )
        ticket = self.begin_save(document_id, save_as=True)
        return self._invoke_save_controller(
            ticket,
            lambda: save_to_resource(ticket=ticket, resource_path=target),
        )

    def _invoke_save_controller(
        self,
        ticket: SaveTicket,
        invoke,
    ) -> DocumentActionResult:
        try:
            result = invoke()
        except Exception as exc:
            self.complete_save(ticket.ticket_id, success=False, message=str(exc))
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        if isinstance(result, DocumentActionResult):
            if result.status is DocumentActionStatus.PENDING:
                return result
            if result.status in {
                DocumentActionStatus.APPLIED,
                DocumentActionStatus.NO_OP,
            }:
                if ticket.is_pending:
                    message = (
                        "document controller reported success without completing "
                        "its SaveTicket"
                    )
                    self.complete_save(
                        ticket.ticket_id,
                        success=False,
                        message=message,
                    )
                    return DocumentActionResult(
                        DocumentActionStatus.FAILED,
                        message,
                    )
                return self._result_for_ticket(ticket)
            if ticket.is_pending:
                self.complete_save(
                    ticket.ticket_id,
                    success=False,
                    cancelled=result.status is DocumentActionStatus.REJECTED,
                    message=result.message,
                )
            return result
        if not ticket.is_pending:
            return self._result_for_ticket(ticket)
        if result is False:
            self.complete_save(
                ticket.ticket_id,
                success=False,
                message="document controller rejected the save",
            )
            return DocumentActionResult(
                DocumentActionStatus.FAILED,
                "document controller rejected the save",
            )
        message = "document controller returned without completing its SaveTicket"
        self.complete_save(ticket.ticket_id, success=False, message=message)
        return DocumentActionResult(DocumentActionStatus.FAILED, message)

    def defer_save(
        self,
        document_id: str,
        *,
        save_as: bool = False,
    ) -> DocumentActionResult:
        """Queue a UI save until every control in the current frame has committed.

        ImGui toolbars are commonly rendered before the fields they edit.  An
        immediate save from a toolbar or shortcut can therefore serialize the
        previous value and then receive the field's deactivation commit later
        in the same frame.  Deferring only the request (not the save ticket)
        makes the ticket capture the exact revision rendered by that frame.
        """
        document = self.require(document_id)
        capability = DocumentCapability.SAVE_AS if save_as else DocumentCapability.SAVE
        if not document.capabilities & capability:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "save is not supported")
        if document.controller is None:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "document has no controller")
        if self.active_save_ticket(document.document_id) is not None:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        previous_save_as = self._deferred_save_requests.get(document.document_id, False)
        self._deferred_save_requests[document.document_id] = bool(
            previous_save_as or save_as
        )
        return DocumentActionResult(DocumentActionStatus.PENDING)

    def process_deferred_saves(self) -> tuple[DocumentActionResult, ...]:
        """Run queued UI saves after the frame that requested them."""
        requests = tuple(self._deferred_save_requests.items())
        self._deferred_save_requests.clear()
        results = []
        for document_id, save_as in requests:
            if self.get(document_id) is None:
                continue
            results.append(self.request_save(document_id, save_as=save_as))
        return tuple(results)

    def process_pending_saves(self) -> int:
        """Poll unique document controllers that own asynchronous persistence."""
        completed = 0
        visited: set[int] = set()
        for document in tuple(self._documents.values()):
            controller = document.controller
            controller_id = id(controller)
            if controller is None or controller_id in visited:
                continue
            visited.add(controller_id)
            poll = getattr(controller, "poll_pending_writes", None)
            if callable(poll):
                completed += int(poll() or 0)
        return completed

    def begin_save(self, document_id: str, *, save_as: bool = False) -> SaveTicket:
        document = self.require(document_id)
        active = self.active_save_ticket(document_id)
        if active is not None:
            raise RuntimeError(f"document already has a pending save: {document_id}")
        document.requested_write_revision += 1
        document.edit_revision = max(document.edit_revision, document.revision)
        ticket = SaveTicket(
            ticket_id=uuid.uuid4().hex,
            document_id=document.document_id,
            captured_revision=document.revision,
            captured_external_revision=document.external_revision,
            expected_file_state=document.durable_file_state,
            resource_path=document.resource_path,
            save_as=bool(save_as),
            operation_id=uuid.uuid4().hex,
            commit_token=uuid.uuid4().hex,
        )
        self._save_tickets[ticket.ticket_id] = ticket
        self._active_save_by_document[document.document_id] = ticket.ticket_id
        document.state = DocumentState.SAVING
        self._touch()
        return ticket

    def active_save_ticket(self, document_id: str) -> Optional[SaveTicket]:
        ticket_id = self._active_save_by_document.get(str(document_id or ""))
        ticket = self._save_tickets.get(ticket_id or "")
        return ticket if ticket is not None and ticket.is_pending else None

    def get_save_ticket(self, ticket_id: str) -> Optional[SaveTicket]:
        return self._save_tickets.get(str(ticket_id or ""))

    def capture_save_revision(
        self,
        ticket_id: str,
        *,
        content_token: str = "",
    ) -> int:
        """Bind a pending save ticket to the content snapshot being serialized.

        Save As dialogs may remain pending across multiple UI frames. The
        revision captured when the dialog opened is therefore not necessarily
        the revision written when the user finally chooses a path. Controllers
        call this immediately before capturing their serializable state so a
        successful completion advances the save point to the exact content
        that reached disk, without clearing edits made after that snapshot.
        """
        ticket = self._save_tickets.get(str(ticket_id or ""))
        if ticket is None:
            raise KeyError(f"unknown document save ticket: {ticket_id}")
        if not ticket.is_pending:
            raise RuntimeError(f"document save ticket is no longer pending: {ticket_id}")
        document = self.require(ticket.document_id)
        ticket.captured_revision = document.revision
        ticket.content_token = str(content_token or "")
        return ticket.captured_revision

    def capture_save_target(
        self,
        ticket_id: str,
        resource_path: str,
    ):
        """Freeze the target state used by a conditional atomic write."""
        ticket = self._save_tickets.get(str(ticket_id or ""))
        if ticket is None:
            raise KeyError(f"unknown document save ticket: {ticket_id}")
        if not ticket.is_pending:
            raise RuntimeError(
                f"document save ticket is no longer pending: {ticket_id}"
            )
        target = str(resource_path or "").strip()
        if not target:
            raise ValueError("document save target is missing")
        if ticket.save_as or not ticket.resource_path:
            ticket.save_as = True
            ticket.expected_file_state = _capture_durable_file_state(target)
        elif path_key(target) != path_key(ticket.resource_path):
            raise RuntimeError("ordinary save cannot change the document resource path")
        return ticket.expected_file_state

    def mark_save_publication_bookkeeping(self, ticket_id: str) -> int:
        """Mark a synchronous publication-only revision for this save.

        This contract is intentionally separate from ``mark_changed``. A
        matching content token alone never proves that a later revision came
        from reimport bookkeeping; the publishing controller must explicitly
        identify the revision while the save ticket is still active.
        """
        ticket = self._save_tickets.get(str(ticket_id or ""))
        if ticket is None:
            raise KeyError(f"unknown document save ticket: {ticket_id}")
        if not ticket.is_pending:
            raise RuntimeError(
                f"document save ticket is no longer pending: {ticket_id}"
            )
        document = self.require(ticket.document_id)
        if document.revision != ticket.captured_revision:
            ticket.publication_bookkeeping_revision = document.revision
        return document.revision

    def complete_save(
        self,
        ticket_id: str,
        *,
        success: bool,
        cancelled: bool = False,
        key: Optional[DocumentKey] = None,
        resource_path: Optional[str] = None,
        title: Optional[str] = None,
        content_token: Optional[str] = None,
        committed_file_state: Any = None,
        conflict: bool = False,
        message: str = "",
    ) -> SaveTicket:
        ticket = self._save_tickets.get(str(ticket_id or ""))
        if ticket is None:
            raise KeyError(f"unknown document save ticket: {ticket_id}")
        if not ticket.is_pending:
            return ticket
        document = self.get(ticket.document_id)
        if document is None:
            ticket.status = SaveTicketStatus.CANCELLED
            ticket.message = message or "document was closed"
            return ticket
        if conflict:
            success = False
            document.state = DocumentState.CONFLICT
        if success:
            external_revision_changed = (
                not ticket.save_as
                and document.external_revision != ticket.captured_external_revision
            )
            if external_revision_changed:
                success = False
                message = (
                    "the resource changed outside the Editor while the save was pending"
                )
                document.state = DocumentState.CONFLICT
        if success:
            try:
                if key is not None:
                    self.rekey(
                        document.document_id,
                        key,
                        resource_path=resource_path,
                    )
                    resource_path = None
                self.update_metadata(
                    document.document_id,
                    title=title,
                    resource_path=resource_path,
                )
            except Exception as exc:
                success = False
                message = str(exc)
        if success:
            # Content revisions are positions in the Undo graph, not a
            # monotonic persistence sequence. A valid active ticket may save
            # revision 0 after revision 1 was previously durable. Ticket and
            # operation identity reject stale completions. A synchronous
            # reimport may append a bookkeeping revision while publishing the
            # exact bytes captured by this ticket. When the content token is
            # unchanged, that revision is part of the same durable commit and
            # must be absorbed instead of leaving a false dirty document.
            content_unchanged = bool(
                ticket.content_token
                and content_token
                and ticket.content_token == content_token
            )
            publication_revision = ticket.publication_bookkeeping_revision
            absorb_publication = bool(
                content_unchanged
                and publication_revision is not None
                and document.revision == publication_revision
            )
            saved_revision = (
                document.revision if absorb_publication else ticket.captured_revision
            )
            document.saved_revision = saved_revision
            document.persisted_revision = saved_revision
            document._dirty_view_ids_by_revision[saved_revision] = frozenset()
            if document.revision == saved_revision:
                document.dirty_view_ids.clear()
            document.durable_file_state = (
                committed_file_state
                if committed_file_state is not None
                else _capture_durable_file_state(document.resource_path)
            )
            document.edit_revision = max(document.edit_revision, document.revision)
            document.state = DocumentState.READY
            if ticket.save_as:
                document.external_revision = 0
            ticket.status = SaveTicketStatus.SUCCEEDED
        else:
            if not conflict and document.state is not DocumentState.CONFLICT:
                document.state = DocumentState.READY
            ticket.status = (
                SaveTicketStatus.CANCELLED if cancelled else SaveTicketStatus.FAILED
            )
        ticket.message = str(message or "")
        if self._active_save_by_document.get(document.document_id) == ticket.ticket_id:
            del self._active_save_by_document[document.document_id]
        self._touch()
        return ticket

    @staticmethod
    def _result_for_ticket(ticket: SaveTicket) -> DocumentActionResult:
        if ticket.status is SaveTicketStatus.PENDING:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        if ticket.status is SaveTicketStatus.SUCCEEDED:
            return DocumentActionResult(DocumentActionStatus.APPLIED, ticket.message)
        if ticket.status is SaveTicketStatus.CANCELLED:
            return DocumentActionResult(DocumentActionStatus.REJECTED, ticket.message)
        return DocumentActionResult(DocumentActionStatus.FAILED, ticket.message)

    def request_discard(self, document_id: str) -> DocumentActionResult:
        document = self.require(document_id)
        was_conflicted = document.state is DocumentState.CONFLICT
        if not document.capabilities & DocumentCapability.DISCARD:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "discard is not supported")
        if self.active_save_ticket(document.document_id) is not None:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "discard is unavailable while the document is saving",
            )
        controller = document.controller
        if controller is None:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "document has no controller")
        try:
            result = controller.discard(document_id=document.document_id)
        except Exception as exc:
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        if isinstance(result, DocumentActionResult):
            if result.status not in {
                DocumentActionStatus.APPLIED,
                DocumentActionStatus.NO_OP,
            }:
                return result
        if result is False:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "discard was cancelled")
        current = self.get(document.document_id)
        if current is None:
            return DocumentActionResult(
                DocumentActionStatus.FAILED,
                "document controller removed the document during discard",
            )
        try:
            if was_conflicted:
                self.establish_loaded_baseline(current.document_id)
            else:
                self.restore_saved_revision(current.document_id)
        except Exception as exc:
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        return DocumentActionResult(DocumentActionStatus.APPLIED)

    def is_save_pending(self, document_id: str) -> bool:
        document = self.require(document_id)
        ticket = self.active_save_ticket(document_id)
        if ticket is None:
            return False
        poll = getattr(document.controller, "poll_save", None)
        if not callable(poll):
            return True
        try:
            outcome = poll(ticket)
        except Exception as exc:
            self.complete_save(ticket.ticket_id, success=False, message=str(exc))
            return False
        if outcome is None:
            return True
        if ticket.is_pending:
            self.complete_save(
                ticket.ticket_id,
                success=False,
                message=(
                    "document controller poll returned completion without "
                    "completing its SaveTicket"
                ),
            )
        return False

    def clear(self) -> None:
        if (
            not self._documents
            and not self._view_documents
            and not self._dormant_documents_by_stable_id
            and not self._pending_session_records
            and not self._session_restore_suppressed_stable_ids
            and not self._session_restore_suppressed_view_ids
        ):
            return
        for document in self._documents.values():
            document.state = DocumentState.CLOSED
            document.view_ids.clear()
        self._documents.clear()
        self._document_ids_by_key.clear()
        self._document_ids_by_stable_id.clear()
        self._dormant_locators_by_key.clear()
        self._dormant_locators_by_stable_id.clear()
        self._dormant_documents_by_stable_id.clear()
        self._view_documents.clear()
        for ticket in self._save_tickets.values():
            if ticket.is_pending:
                ticket.status = SaveTicketStatus.CANCELLED
                ticket.message = "document registry was cleared"
        self._active_save_by_document.clear()
        self._deferred_save_requests.clear()
        self._external_change_preflights.clear()
        self._pending_external_reloads.clear()
        self._external_reload_results.clear()
        self._pending_session_records.clear()
        self._pending_session_document_by_view.clear()
        self._session_restore_suppressed_stable_ids.clear()
        self._session_restore_suppressed_view_ids.clear()
        self._touch()

    def _touch(self) -> None:
        self._revision += 1
