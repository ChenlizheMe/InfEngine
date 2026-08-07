"""Registry for reversible content projections caused by asset renames."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
import re
from typing import Optional


AssetRenameTransform = Callable[[str, str, str], str]


def _rename_top_level_json_name(content: str, _source: str, destination: str) -> str:
    payload = json.loads(content)
    if not isinstance(payload, dict) or "name" not in payload:
        return content
    payload["name"] = os.path.splitext(os.path.basename(destination))[0]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _rename_single_component_class(content: str, source: str, destination: str) -> str:
    old_stem = os.path.splitext(os.path.basename(source))[0]
    new_stem = os.path.splitext(os.path.basename(destination))[0]
    if not old_stem.isidentifier() or not new_stem.isidentifier() or old_stem == new_stem:
        return content

    def pascal_case(stem: str) -> str:
        return "".join(part[:1].upper() + part[1:] for part in stem.split("_") if part)

    candidates = []
    for old_class_name, new_class_name in {
        old_stem: new_stem,
        pascal_case(old_stem): pascal_case(new_stem),
    }.items():
        pattern = re.compile(rf"^class\s+{re.escape(old_class_name)}\b", re.MULTILINE)
        candidates.extend((pattern, new_class_name) for _match in pattern.finditer(content))
    if len(candidates) != 1:
        return content
    pattern, new_class_name = candidates[0]
    return pattern.sub(f"class {new_class_name}", content, count=1)


class AssetRenameContentRegistry:
    """Own extension-specific, pure rename projections.

    Adapters only transform text. Workspace writes, rollback, catalog mutation,
    document projection, and Undo identity remain owned by the relocation
    transaction.
    """

    _instance: Optional["AssetRenameContentRegistry"] = None

    def __init__(self) -> None:
        self._adapters: dict[str, AssetRenameTransform] = {}
        self.register((".mat", ".particlegraph"), _rename_top_level_json_name)
        self.register((".py",), _rename_single_component_class)
        AssetRenameContentRegistry._instance = self

    @classmethod
    def instance(cls) -> "AssetRenameContentRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, extensions, transform: AssetRenameTransform) -> None:
        if not callable(transform):
            raise TypeError("asset rename content adapter must be callable")
        for extension in extensions:
            normalized = str(extension or "").strip().lower()
            if not normalized.startswith("."):
                raise ValueError("asset rename content extension must start with '.'")
            self._adapters[normalized] = transform

    def unregister(self, extension: str) -> None:
        self._adapters.pop(str(extension or "").strip().lower(), None)

    def build_patch(self, source: str, destination: str) -> tuple[str, str] | None:
        if not os.path.isfile(source):
            return None
        transform = self._adapters.get(os.path.splitext(source)[1].lower())
        if transform is None:
            return None
        with open(source, "r", encoding="utf-8") as stream:
            original = stream.read()
        updated = transform(original, source, destination)
        if not isinstance(updated, str):
            raise TypeError("asset rename content adapter must return text")
        return (original, updated) if updated != original else None

    def shutdown(self) -> None:
        self._adapters.clear()
        if AssetRenameContentRegistry._instance is self:
            AssetRenameContentRegistry._instance = None


__all__ = ["AssetRenameContentRegistry", "AssetRenameTransform"]
