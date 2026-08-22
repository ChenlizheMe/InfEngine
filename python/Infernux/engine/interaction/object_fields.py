"""Shared interaction models for object and asset reference fields."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag, auto
import os
from typing import Any, Callable, Optional, Sequence

from .search import SearchQueryModel
from .serialized_properties import (
    PropertyTransaction,
    PropertyTransactionStatus,
    property_drawer_registry,
)


class ObjectFieldGesture(IntFlag):
    """Gestures reported by the native ObjectField chrome."""

    NONE = 0
    LOCATE = auto()
    OPEN_PICKER = auto()
    OPEN = auto()
    KEYBOARD_OPEN = auto()
    CLEAR = auto()
    CONTEXT_MENU = auto()


PickerProvider = Callable[[str], Sequence[tuple[str, Any]]]

ASSET_REFERENCE_OPEN_COMMAND = "asset_reference.open"
ASSET_REFERENCE_REVEAL_COMMAND = "asset_reference.reveal"
ASSET_REFERENCE_COPY_COMMAND = "asset_reference.copy"
ASSET_REFERENCE_PASTE_COMMAND = "asset_reference.paste"
ASSET_REFERENCE_CLEAR_COMMAND = "asset_reference.clear"


def _merged_picker_provider(
    base: PickerProvider, additional: PickerProvider
) -> PickerProvider:
    def _items(query: str) -> tuple[tuple[str, Any], ...]:
        result = []
        seen = set()
        for provider in (additional, base):
            for label, value in provider(query):
                key = (str(label).casefold(), repr(value))
                if key in seen:
                    continue
                seen.add(key)
                result.append((label, value))
        return tuple(result)

    return _items


@dataclass
class AssetReferenceCatalog:
    """Generation-cached project catalog used by every asset picker."""

    _database_identity: int = 0
    _generation: int = -1
    _paths: tuple[str, ...] = ()
    _typed_items: dict[str, tuple[tuple[str, str], ...]] = field(
        default_factory=dict
    )

    def invalidate(self) -> None:
        self._database_identity = 0
        self._generation = -1
        self._paths = ()
        self._typed_items.clear()

    def items(self, asset_type: str, query: str) -> tuple[tuple[str, str], ...]:
        query_token = str(query or "").strip().casefold()
        candidates = self._items_for_type(asset_type)
        if not query_token:
            return candidates
        return tuple(
            (name, path)
            for name, path in candidates
            if query_token in name.casefold()
            or query_token in path.replace("\\", "/").casefold()
        )

    def _items_for_type(self, asset_type: str) -> tuple[tuple[str, str], ...]:
        from Infernux.core.asset_reference_types import asset_type_registry
        from Infernux.engine.path_utils import lexical_path, lexical_path_key
        from Infernux.engine.project_context import get_project_root

        descriptor = asset_type_registry.require(asset_type)
        paths = self._snapshot()
        cache_key = descriptor.type_id.casefold()
        cached = self._typed_items.get(cache_key)
        if cached is not None:
            return cached

        shader_type = cache_key.startswith("shader")
        project_root = get_project_root()
        assets_root = (
            lexical_path_key(os.path.join(project_root, "Assets"))
            if project_root
            else ""
        )
        assets_prefix = assets_root.rstrip("\\/") + os.sep if assets_root else ""
        matches: list[tuple[str, str]] = []
        for path in paths:
            portable = path.replace("\\", "/")
            folded = portable.casefold()
            candidate_path = (
                os.path.join(project_root, path)
                if project_root and not os.path.isabs(path)
                else path
            )
            if not shader_type:
                candidate_key = lexical_path_key(candidate_path)
                inside_assets = bool(assets_root) and (
                    candidate_key == assets_root
                    or candidate_key.startswith(assets_prefix)
                )
                if not inside_assets:
                    continue
            if not any(folded.endswith(extension) for extension in descriptor.extensions):
                continue
            if shader_type:
                from Infernux.engine.ui.inspector_shader_utils import is_shader_hidden

                if is_shader_hidden(lexical_path(candidate_path)):
                    continue
            name = os.path.basename(portable)
            matches.append((name, path))
        matches.sort(key=lambda item: (item[0].casefold(), item[1].casefold()))
        result = tuple(matches)
        self._typed_items[cache_key] = result
        return result

    def provider(self, asset_type: str) -> PickerProvider:
        type_id = str(asset_type or "").strip()
        return lambda query: self.items(type_id, query)

    def _snapshot(self) -> tuple[str, ...]:
        from Infernux.core.assets import AssetManager

        database = getattr(AssetManager, "_asset_database", None)
        if database is None:
            self.invalidate()
            return ()
        identity = id(database)
        generation = int(getattr(database, "query_generation", 0) or 0)
        if identity == self._database_identity and generation == self._generation:
            return self._paths

        paths = []
        seen = set()
        for asset_path in database.get_all_asset_paths():
            path = str(asset_path or "").strip()
            key = path.replace("\\", "/").casefold()
            if not path or key in seen:
                continue
            seen.add(key)
            paths.append(path)
        paths.sort(key=lambda value: value.replace("\\", "/").casefold())
        self._database_identity = identity
        self._generation = generation
        self._paths = tuple(paths)
        self._typed_items.clear()
        return self._paths


@dataclass
class ObjectPickerModel:
    """Persistent, renderer-independent state for all ObjectField pickers."""

    _queries: dict[str, SearchQueryModel] = field(default_factory=dict)
    _focus_requests: set[str] = field(default_factory=set)
    _open_requests: set[str] = field(default_factory=set)

    def request_open(self, field_id: str) -> None:
        key = self._key(field_id)
        self.query_model(key).clear()
        self._focus_requests.add(key)
        self._open_requests.add(key)

    def open_requested(self, field_id: str) -> bool:
        return self._key(field_id) in self._open_requests

    def confirm_open(self, field_id: str) -> None:
        self._open_requests.discard(self._key(field_id))

    def consume_focus_request(self, field_id: str) -> bool:
        key = self._key(field_id)
        if key not in self._focus_requests:
            return False
        self._focus_requests.remove(key)
        return True

    def query(self, field_id: str) -> str:
        model = self._queries.get(self._key(field_id))
        return model.query if model is not None else ""

    def set_query(self, field_id: str, value: str) -> bool:
        return self.query_model(field_id).set_query(value)

    def query_model(self, field_id: str) -> SearchQueryModel:
        key = self._key(field_id)
        model = self._queries.get(key)
        if model is None:
            model = SearchQueryModel()
            self._queries[key] = model
        return model

    def close(self, field_id: str) -> None:
        key = self._key(field_id)
        self._focus_requests.discard(key)
        self._open_requests.discard(key)

    @staticmethod
    def _key(field_id: str) -> str:
        key = str(field_id or "").strip()
        if not key:
            raise ValueError("object picker field id must not be empty")
        return key


@dataclass
class ObjectReferenceFieldModel:
    """One authoritative behavior contract for an ObjectField instance."""

    field_id: str
    display_text: str
    type_hint: str
    selected: bool = False
    clickable: bool = True
    accept: Optional[str | Sequence[str]] = None
    scene_items: Optional[PickerProvider] = None
    asset_items: Optional[PickerProvider] = None
    on_drop: Optional[Callable[[Any], None]] = None
    on_pick: Optional[Callable[[Any], None]] = None
    on_clear: Optional[Callable[[], None]] = None
    on_locate: Optional[Callable[[], None]] = None
    on_open: Optional[Callable[[], None]] = None
    ping_path: Optional[str] = None
    semantic_id: str = ""
    has_value: Optional[bool] = None

    def __post_init__(self) -> None:
        self.field_id = str(self.field_id or "").strip()
        if not self.field_id:
            raise ValueError("object reference field id must not be empty")
        self.semantic_id = str(self.semantic_id or "").strip() or (
            f"object_field.{self.field_id}"
        )
        if self.has_value is None:
            path = str(self.ping_path or "").strip()
            self.has_value = bool(path and path.casefold() not in {"none", "null"}) or (
                self.on_locate is not None or self.on_open is not None
            )

    @property
    def has_picker(self) -> bool:
        return self.scene_items is not None or self.asset_items is not None

    @property
    def can_accept_drop(self) -> bool:
        return self.on_drop is not None

    def dispatch_chrome(self, flags: int) -> ObjectFieldGesture:
        """Dispatch one chrome result using the global ObjectField semantics."""

        gesture = ObjectFieldGesture(int(flags))
        if gesture & (ObjectFieldGesture.OPEN | ObjectFieldGesture.KEYBOARD_OPEN):
            if not self.has_value and self.has_picker:
                gesture |= ObjectFieldGesture.OPEN_PICKER
            elif self.on_open is not None:
                self.on_open()
            else:
                self.locate()
        elif gesture & ObjectFieldGesture.LOCATE:
            if not self.has_value and self.has_picker:
                gesture |= ObjectFieldGesture.OPEN_PICKER
            else:
                self.locate()
        if gesture & ObjectFieldGesture.CLEAR and self.has_value:
            if self.on_clear is not None:
                self.on_clear()
        return gesture

    def dispatch_picker(self, intent: Optional[tuple[str, Any]]) -> None:
        if intent is None:
            return
        kind, value = intent
        if kind == "clear":
            if self.on_clear is not None:
                self.on_clear()
            return
        if kind == "pick" and self.on_pick is not None:
            self.on_pick(value)

    def dispatch_drop(self, value: Any) -> None:
        if self.on_drop is not None:
            self.on_drop(value)

    def locate(self) -> None:
        if self.on_locate is not None:
            self.on_locate()
            return
        path = str(self.ping_path or "").strip()
        if not path or path.casefold() in {"none", "null"}:
            return
        from Infernux.engine.ui._inspector_references import ping_asset_in_project

        ping_asset_in_project(path)


@dataclass
class AssetReferenceFieldModel(ObjectReferenceFieldModel):
    """ObjectField model with shared asset-type compatibility validation."""

    asset_type: str = ""
    on_assign: Optional[Callable[[Any], None]] = None
    on_rejected: Optional[Callable[[str], None]] = None
    additional_asset_items: Optional[PickerProvider] = None
    reference_value: Any = None
    transaction: Optional[PropertyTransaction] = None
    alternate_compatibility: Optional[Callable[[Any], str]] = None
    field_read_only: bool = False

    def __post_init__(self) -> None:
        from Infernux.core.asset_reference_types import asset_type_registry

        if self.on_pick is not None or self.on_drop is not None:
            raise ValueError(
                "asset reference fields use one on_assign callback for picker, "
                "drop and paste; on_pick/on_drop are scene-reference APIs"
            )
        descriptor = asset_type_registry.require(self.asset_type or self.type_hint)
        if self.asset_items is not None:
            raise ValueError(
                "asset reference fields must use the shared AssetReferenceCatalog; "
                "register virtual candidates through additional_asset_items"
            )
        self.asset_type = descriptor.type_id
        if self.transaction is not None:
            transaction_type = str(
                getattr(self.transaction, "value_type", "") or ""
            ).strip()
            if transaction_type.casefold() not in {
                descriptor.type_id.casefold(),
                "asset_reference",
            }:
                raise ValueError(
                    f"asset field '{self.field_id}' transaction type "
                    f"'{transaction_type}' does not match '{descriptor.type_id}'"
                )
            if self.reference_value is None:
                self.reference_value = self.transaction.value
        if self.reference_value is None and self.ping_path:
            self.reference_value = {
                "asset_type": descriptor.type_id,
                "path_hint": str(self.ping_path),
            }
        if self.accept is None:
            self.accept = descriptor.drag_types
        provider = asset_reference_catalog.provider(descriptor.type_id)
        if self.additional_asset_items is not None:
            provider = _merged_picker_provider(provider, self.additional_asset_items)
        self.asset_items = provider
        super().__post_init__()

    @property
    def read_only(self) -> bool:
        return bool(
            self.field_read_only
            or (
                self.transaction is not None
                and self.transaction.read_only
            )
        )

    @property
    def mixed(self) -> bool:
        return bool(
            self.transaction is not None and self.transaction.mixed
        )

    @property
    def can_clear(self) -> bool:
        return bool(
            not self.read_only
            and (self.transaction is not None or self.on_clear is not None)
        )

    @property
    def can_accept_drop(self) -> bool:
        return bool(
            not self.read_only
            and (self.transaction is not None or self.on_assign is not None)
        )

    @property
    def has_picker(self) -> bool:
        return bool(not self.read_only and super().has_picker)

    def dispatch_chrome(self, flags: int) -> ObjectFieldGesture:
        gesture = ObjectFieldGesture(int(flags))
        if gesture & (ObjectFieldGesture.OPEN | ObjectFieldGesture.KEYBOARD_OPEN):
            if not self.has_value and self.has_picker:
                gesture |= ObjectFieldGesture.OPEN_PICKER
            else:
                # Resource ObjectFields use double-click/Enter to reveal the
                # referenced file.  Editing/opening the resource remains an
                # explicit context-menu command so every field has identical
                # body semantics.
                self.locate()
        # A resource field body single-click only focuses/selects the field.
        # The dedicated picker button owns OPEN_PICKER, while double-click and
        # Enter own resource location.  Keeping these gestures disjoint avoids
        # a first click navigating away before the second click can complete.
        if gesture & ObjectFieldGesture.CLEAR and self.has_value:
            self._clear()
        return gesture

    def dispatch_picker(self, intent: Optional[tuple[str, Any]]) -> None:
        if intent is None or self.read_only:
            return
        kind, value = intent
        if kind == "clear":
            self._clear()
            return
        if kind != "pick":
            return
        if not self._accepts(value) or self._is_no_op(value):
            return
        self._assign(value)

    def dispatch_drop(self, value: Any) -> None:
        if self.read_only or not self._accepts(value) or self._is_no_op(value):
            return
        self._assign(value)

    def copy_reference_text(self) -> str:
        if not self.has_value:
            return ""
        from Infernux.core.asset_reference_types import AssetReferenceCodec

        return AssetReferenceCodec.encode(self.asset_type, self.reference_value)

    def can_paste_reference(self, text: str) -> bool:
        from Infernux.core.asset_reference_types import AssetReferenceCodec

        try:
            value = AssetReferenceCodec.decode(text)
        except (KeyError, TypeError, ValueError):
            return False
        return not self.read_only and self._accepts(value, report=False)

    def dispatch_paste_reference(self, text: str) -> bool:
        from Infernux.core.asset_reference_types import AssetReferenceCodec

        try:
            value = AssetReferenceCodec.decode(text)
        except (KeyError, TypeError, ValueError) as exc:
            self._reject(str(exc))
            return False
        if not self._accepts(value) or self._is_no_op(value):
            return False
        return self._assign(value)

    def clear_reference(self) -> bool:
        """Clear the field through the same transaction used by every input path."""

        return self._clear()

    def _assign(self, value: Any) -> bool:
        if self.transaction is not None:
            return self.transaction.commit(value) is PropertyTransactionStatus.APPLIED
        if self.on_assign is None:
            return False
        self.on_assign(value)
        return True

    def _clear(self) -> bool:
        if self.read_only:
            return False
        if self.transaction is not None:
            return self.transaction.clear() is PropertyTransactionStatus.APPLIED
        if self.on_clear is None:
            return False
        self.on_clear()
        return True

    def _is_no_op(self, value: Any) -> bool:
        if self.reference_value is None:
            return False
        from Infernux.core.asset_reference_types import AssetReferenceCodec

        current = AssetReferenceCodec.normalize(self.asset_type, self.reference_value)
        candidate = AssetReferenceCodec.normalize(self.asset_type, value)
        current_guid = current["guid"].casefold()
        candidate_guid = candidate["guid"].casefold()
        if current_guid and candidate_guid:
            return current_guid == candidate_guid
        current_builtin = current["builtin"].casefold()
        candidate_builtin = candidate["builtin"].casefold()
        if current_builtin and candidate_builtin:
            return current_builtin == candidate_builtin
        current_path = current["path_hint"].replace("\\", "/").casefold()
        candidate_path = candidate["path_hint"].replace("\\", "/").casefold()
        return bool(current_path and candidate_path and current_path == candidate_path)

    def _accepts(self, value: Any, *, report: bool = True) -> bool:
        from Infernux.core.asset_reference_types import asset_type_registry

        descriptor = asset_type_registry.get(self.asset_type or self.type_hint)
        if descriptor is None:
            if report:
                self._reject(
                    f"Asset reference field '{self.field_id}' uses unknown type "
                    f"'{self.asset_type or self.type_hint}'"
                )
            return False
        error = descriptor.incompatibility(value)
        if error and self.alternate_compatibility is not None:
            alternate_error = str(self.alternate_compatibility(value) or "")
            if not alternate_error:
                return True
            error = alternate_error
        if error:
            if report:
                self._reject(error)
            return False
        return True

    def _reject(self, message: str) -> None:
        if self.on_rejected is not None:
            self.on_rejected(message)
            return
        from Infernux.debug import Debug

        Debug.log_error(message)


@dataclass(frozen=True, slots=True)
class AssetReferenceCommandTarget:
    """Frozen ObjectField target carried by one command-backed popup."""

    model: AssetReferenceFieldModel
    clipboard_text: str = ""
    clipboard_writer: Optional[Callable[[str], None]] = None


def asset_reference_command_payload(
    model: AssetReferenceFieldModel,
    *,
    clipboard_text: str = "",
    clipboard_writer: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    if not isinstance(model, AssetReferenceFieldModel):
        raise TypeError("asset reference command payload requires an asset field model")
    return {
        "asset_reference_target": AssetReferenceCommandTarget(
            model,
            str(clipboard_text or ""),
            clipboard_writer,
        )
    }


def register_asset_reference_commands(registry=None) -> None:
    """Register the global commands used by every asset ObjectField menu."""

    from .commands import EditorCommand, EditorCommandRegistry

    target_registry = registry or EditorCommandRegistry.instance()

    def _target(context) -> Optional[AssetReferenceCommandTarget]:
        value = context.payload.get("asset_reference_target")
        return value if isinstance(value, AssetReferenceCommandTarget) else None

    def _has_value(context) -> bool:
        target = _target(context)
        return bool(target is not None and target.model.has_value)

    def _can_copy(context) -> bool:
        target = _target(context)
        return bool(target is not None and target.model.copy_reference_text())

    def _can_paste(context) -> bool:
        target = _target(context)
        return bool(
            target is not None
            and target.model.can_paste_reference(target.clipboard_text)
        )

    def _can_clear(context) -> bool:
        target = _target(context)
        return bool(
            target is not None
            and target.model.has_value
            and target.model.can_clear
        )

    def _open(context) -> bool:
        target = _target(context)
        if target is None:
            return False
        if target.model.on_open is not None:
            target.model.on_open()
            return True
        path = str(target.model.ping_path or "").strip()
        if not path:
            return False
        from Infernux.engine.ui._inspector_references import open_asset_reference

        return bool(open_asset_reference(path))

    def _reveal(context) -> bool:
        target = _target(context)
        if target is None:
            return False
        target.model.dispatch_chrome(int(ObjectFieldGesture.LOCATE))
        return True

    def _copy(context) -> bool:
        target = _target(context)
        if target is None:
            return False
        text = target.model.copy_reference_text()
        if not text:
            return False
        if target.clipboard_writer is not None:
            target.clipboard_writer(text)
        from .clipboard import ClipboardDomain, ClipboardItem, ClipboardService

        ClipboardService.instance().write(
            ClipboardDomain.ASSET,
            (
                ClipboardItem(
                    target.model.field_id,
                    sub_kind=target.model.asset_type,
                    data=text,
                ),
            ),
            source_owner_id=target.model.field_id,
            reason="copy_asset_reference",
        )
        return True

    def _paste(context) -> bool:
        target = _target(context)
        return bool(
            target is not None
            and target.model.dispatch_paste_reference(target.clipboard_text)
        )

    def _clear(context) -> bool:
        target = _target(context)
        return bool(target is not None and target.model.clear_reference())

    for command in (
        EditorCommand(
            ASSET_REFERENCE_OPEN_COMMAND,
            _open,
            display_name="Open Asset Reference",
            category="Object Field",
            can_execute=_has_value,
        ),
        EditorCommand(
            ASSET_REFERENCE_REVEAL_COMMAND,
            _reveal,
            display_name="Reveal Asset Reference",
            category="Object Field",
            can_execute=_has_value,
        ),
        EditorCommand(
            ASSET_REFERENCE_COPY_COMMAND,
            _copy,
            display_name="Copy Asset Reference",
            category="Object Field",
            can_execute=_can_copy,
        ),
        EditorCommand(
            ASSET_REFERENCE_PASTE_COMMAND,
            _paste,
            display_name="Paste Asset Reference",
            category="Object Field",
            can_execute=_can_paste,
        ),
        EditorCommand(
            ASSET_REFERENCE_CLEAR_COMMAND,
            _clear,
            display_name="Clear Asset Reference",
            category="Object Field",
            can_execute=_can_clear,
        ),
    ):
        target_registry.register(command, replace=True)


asset_reference_catalog = AssetReferenceCatalog()
object_picker_model = ObjectPickerModel()


property_drawer_registry.register(
    "asset_reference",
    AssetReferenceFieldModel,
    replace=True,
)
