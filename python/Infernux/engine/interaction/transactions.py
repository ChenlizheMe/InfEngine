"""Atomic editor transaction builder."""

from __future__ import annotations

import uuid
from typing import Optional


class EditorTransaction:
    """Execute related commands atomically and publish one journal action."""

    def __init__(self, description: str, transaction_id: str = "") -> None:
        self.description = str(description or "Transaction")
        self.transaction_id = str(transaction_id or uuid.uuid4().hex)
        self._commands: list = []
        self._closed = False

    @property
    def command_count(self) -> int:
        return len(self._commands)

    @property
    def is_closed(self) -> bool:
        return self._closed

    def execute(self, command) -> None:
        self._require_open()
        try:
            command.execute()
        except Exception:
            self.rollback()
            raise
        self._commands.append(command)

    def record_applied(self, command) -> None:
        self._require_open()
        self._commands.append(command)

    def rollback(self) -> None:
        if self._closed:
            return
        try:
            for command in reversed(self._commands):
                command.undo()
        finally:
            self._commands.clear()
            self._closed = True

    def commit(self):
        self._require_open()
        self._closed = True
        if not self._commands:
            return None
        from Infernux.engine.undo._base import CompoundCommand

        return CompoundCommand(self._commands, self.description)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("editor transaction is already closed")
