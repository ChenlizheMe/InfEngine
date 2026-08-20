"""Mutable runtime wrapper for reusable ``.effect`` assets."""

from __future__ import annotations

import copy
import json
import math
import os
import time
from typing import Any, Optional, Tuple

from Infernux.renderstack.render_effect_asset import (
    RenderEffectAsset,
    RenderEffectGroupAsset,
    dump_render_effect_document,
    parse_render_effect_document,
)


class EditableRenderEffectGroup:
    """Mutable document wrapper used by the shared asset editing service."""

    def __init__(self, source: RenderEffectGroupAsset, *, file_path: str = "", guid: str = "") -> None:
        if not isinstance(source, RenderEffectGroupAsset):
            raise TypeError("EditableRenderEffectGroup requires a group source")
        self._source = source
        self.file_path = str(file_path or "")
        self.guid = str(guid or "")

    @property
    def entries(self):
        return self._source.entries

    def to_asset(self) -> RenderEffectGroupAsset:
        return self._source

    def serialize_document(self) -> dict[str, Any]:
        return self._source.to_dict()

    def deserialize_document(self, document) -> bool:
        try:
            source = (
                document
                if isinstance(document, RenderEffectGroupAsset)
                else parse_render_effect_document(document)
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(source, RenderEffectGroupAsset):
            return False
        self._source = source
        return True


class RenderEffect:
    """Material-like shared runtime state backed by a RenderEffect asset.

    Parameter edits only advance ``revision``. They do not change the effect
    feature type and therefore do not require a RenderGraph topology rebuild.
    Like Material, loaded assets are shared; call ``clone()`` for isolated,
    runtime-only state.
    """

    _AUTOSAVE_MIN_INTERVAL = 0.5
    _pending_saves: set["RenderEffect"] = set()
    _suppress_auto_save = False

    def __init__(
        self,
        source: RenderEffectAsset,
        *,
        file_path: str = "",
        guid: str = "",
    ) -> None:
        if not isinstance(source, RenderEffectAsset):
            raise TypeError("RenderEffect requires a RenderEffectAsset source")
        self._feature_type = source.feature_type
        self._parameters = copy.deepcopy(dict(source.parameters))
        self._dependencies = tuple(source.dependencies)
        self._file_path = str(file_path or "")
        self._guid = str(guid or "")
        self._revision = 0
        self._artifact_revision = 0
        self._last_save_time = 0.0
        self._save_pending = False

    @classmethod
    def load(cls, file_path: str) -> Optional["RenderEffect"]:
        """Load one effect source. Effect-group documents are not instances."""
        try:
            with open(file_path, "r", encoding="utf-8") as stream:
                source = parse_render_effect_document(stream.read())
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(source, RenderEffectAsset):
            return None

        guid = ""
        try:
            from Infernux.core.assets import AssetManager

            guid = AssetManager._get_guid_from_path(file_path) or ""
        except (AttributeError, RuntimeError):
            pass
        return cls(source, file_path=file_path, guid=guid)

    @property
    def feature_type(self) -> str:
        return self._feature_type

    @property
    def file_path(self) -> str:
        return self._file_path

    @property
    def guid(self) -> str:
        return self._guid

    @property
    def name(self) -> str:
        if self._file_path:
            return os.path.splitext(os.path.basename(self._file_path))[0]
        return self._feature_type.rsplit(".", 1)[-1]

    @property
    def revision(self) -> int:
        """Monotonic parameter revision consumed by runtime upload caches."""
        return self._revision

    @property
    def artifact_revision(self) -> int:
        """Last successfully compiled source revision published for this asset."""
        return self._artifact_revision

    def to_asset(self) -> RenderEffectAsset:
        return RenderEffectAsset(
            feature_type=self._feature_type,
            parameters=copy.deepcopy(self._parameters),
            dependencies=self._dependencies,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_asset().to_dict()

    def serialize_document(self) -> dict[str, Any]:
        """Return the canonical editable-resource document."""
        return self.to_dict()

    def deserialize_document(self, document) -> bool:
        """Apply a strict source document to this shared live instance."""
        try:
            source = (
                document
                if isinstance(document, RenderEffectAsset)
                else parse_render_effect_document(document)
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(source, RenderEffectAsset):
            return False
        previous = self.to_asset()
        if previous == source:
            return True
        self._feature_type = source.feature_type
        self._parameters = copy.deepcopy(dict(source.parameters))
        self._dependencies = tuple(source.dependencies)
        self._revision += 1
        self._schedule_save()
        return True

    def has_parameter(self, name: str) -> bool:
        return str(name) in self._parameters

    def get_param(self, name: str, default: Any = None) -> Any:
        value = self._parameters.get(str(name), default)
        return copy.deepcopy(value)

    def set_param(self, name: str, value: Any) -> None:
        key = self._validate_name(name)
        normalized = self._finite_json_clone(value)
        if self._parameters.get(key, object()) == normalized:
            return
        self._parameters[key] = normalized
        self._mark_parameter_changed()

    def set_float(self, name: str, value: float) -> None:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("effect float parameters must be finite")
        self.set_param(name, number)

    def get_float(self, name: str, default: float = 0.0) -> float:
        value = self._parameters.get(str(name))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return float(default)
        return float(value)

    def set_int(self, name: str, value: int) -> None:
        if isinstance(value, bool):
            raise TypeError("effect int parameters require an integer")
        self.set_param(name, int(value))

    def get_int(self, name: str, default: int = 0) -> int:
        value = self._parameters.get(str(name))
        if isinstance(value, bool) or not isinstance(value, int):
            return int(default)
        return value

    def set_bool(self, name: str, value: bool) -> None:
        if not isinstance(value, bool):
            raise TypeError("effect bool parameters require a boolean")
        self.set_param(name, value)

    def get_bool(self, name: str, default: bool = False) -> bool:
        value = self._parameters.get(str(name))
        return value if isinstance(value, bool) else bool(default)

    def set_vector2(self, name: str, x: float, y: float) -> None:
        self.set_param(name, self._finite_vector((x, y), 2))

    def get_vector2(self, name: str) -> Tuple[float, float]:
        return self._get_vector(name, 2, (0.0, 0.0))

    def set_vector3(self, name: str, x: float, y: float, z: float) -> None:
        self.set_param(name, self._finite_vector((x, y, z), 3))

    def get_vector3(self, name: str) -> Tuple[float, float, float]:
        return self._get_vector(name, 3, (0.0, 0.0, 0.0))

    def set_vector4(self, name: str, x: float, y: float, z: float, w: float) -> None:
        self.set_param(name, self._finite_vector((x, y, z, w), 4))

    def get_vector4(self, name: str) -> Tuple[float, float, float, float]:
        return self._get_vector(name, 4, (0.0, 0.0, 0.0, 0.0))

    def set_color(self, name: str, r: float, g: float, b: float, a: float = 1.0) -> None:
        self.set_vector4(name, r, g, b, a)

    def get_color(self, name: str) -> Tuple[float, float, float, float]:
        return self._get_vector(name, 4, (0.0, 0.0, 0.0, 1.0))

    def clone(self) -> "RenderEffect":
        """Create an isolated runtime-only copy, like ``Material.clone()``."""
        return RenderEffect(self.to_asset())

    def save(self, file_path: str = "") -> bool:
        target = str(file_path or self._file_path)
        if not target:
            return False
        try:
            from Infernux.core.document_store import write_document_text

            write_document_text(target, dump_render_effect_document(self.to_asset()))
        except (OSError, RuntimeError, ValueError):
            return False
        self._file_path = target
        if not self._publish_saved_source(target):
            return False
        self._last_save_time = time.monotonic()
        self._save_pending = False
        RenderEffect._pending_saves.discard(self)
        return True

    def _publish_compiled_source(
        self,
        source: RenderEffectAsset,
        *,
        artifact_revision: int,
        file_path: str = "",
        guid: str = "",
    ) -> None:
        """Atomically replace live values after a successful source compile."""
        previous = self.to_asset()
        self._feature_type = source.feature_type
        self._parameters = copy.deepcopy(dict(source.parameters))
        self._dependencies = tuple(source.dependencies)
        if file_path:
            self._file_path = str(file_path)
        if guid:
            self._guid = str(guid)
        self._artifact_revision = int(artifact_revision)
        if previous != source:
            self._revision += 1

    def _publish_saved_source(self, target: str) -> bool:
        """Compile immediately through the canonical asset transaction when active."""
        try:
            from Infernux.core.assets import AssetManager

            database = AssetManager._asset_database
            if database is None:
                return True
            guid = database.get_guid_from_path(target)
            result = (
                AssetManager.reimport_asset(target)
                if guid
                else AssetManager.import_asset(target)
            )
            if not result:
                return False
            from Infernux.renderstack.render_effect_compiler import (
                RenderEffectArtifactRegistry,
            )

            artifact = RenderEffectArtifactRegistry.get(target, result.guid)
            if artifact is not None:
                self._artifact_revision = artifact.revision
                if result.guid:
                    self._guid = result.guid
            return True
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def flush(self) -> None:
        if not self._save_pending or not self._file_path:
            return
        from Infernux.core.assets import AssetManager

        AssetManager.flush_scheduled_saves(self._file_path)

    @classmethod
    def flush_all_pending(cls) -> None:
        for effect in list(cls._pending_saves):
            effect.flush()

    def _mark_parameter_changed(self) -> None:
        self._revision += 1
        self._schedule_save()

    def _schedule_save(self) -> None:
        if self._suppress_auto_save or not self._file_path:
            return
        self._save_pending = True
        self._pending_saves.add(self)
        from Infernux.core.assets import AssetManager

        AssetManager.set_render_effect_save_snapshot(
            self._file_path,
            dump_render_effect_document(self.to_asset()),
            edit_revision=self._revision,
        )
        AssetManager.schedule_asset_save(
            "render_effect",
            self._file_path,
            self,
            debounce_sec=self._AUTOSAVE_MIN_INTERVAL,
        )

    def _on_save_completed(self, status: str) -> None:
        if status == "succeeded":
            self._last_save_time = time.monotonic()
            self._save_pending = False
            self._pending_saves.discard(self)
            return

        # Keep the latest in-memory generation pending. A failed or externally
        # superseded write must never turn a live effect into a false clean
        # state or strand it without another debounced persistence attempt.
        self._save_pending = True
        self._pending_saves.add(self)
        self._schedule_save()

    @staticmethod
    def _validate_name(name: str) -> str:
        key = str(name or "").strip()
        if not key:
            raise ValueError("effect parameter name cannot be empty")
        return key

    @staticmethod
    def _finite_json_clone(value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise TypeError("effect parameters must be finite JSON values") from exc

    @staticmethod
    def _finite_vector(value, size: int) -> list[float]:
        if len(value) != size:
            raise ValueError(f"effect vector requires {size} values")
        result = [float(item) for item in value]
        if not all(math.isfinite(item) for item in result):
            raise ValueError("effect vector parameters must be finite")
        return result

    def _get_vector(self, name: str, size: int, default):
        value = self._parameters.get(str(name))
        if not isinstance(value, (list, tuple)) or len(value) != size:
            return default
        try:
            result = tuple(float(item) for item in value)
        except (TypeError, ValueError):
            return default
        return result if all(math.isfinite(item) for item in result) else default

    def __repr__(self) -> str:
        return f"RenderEffect(name={self.name!r}, feature_type={self.feature_type!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RenderEffect):
            return NotImplemented
        if self.guid and other.guid:
            return self.guid == other.guid
        return self is other

    def __hash__(self) -> int:
        return hash(self.guid) if self.guid else id(self)
