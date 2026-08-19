from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from threading import Lock
from typing import Protocol

from device_agent_lab.agent_contracts import DeviceConversationContext


class ConversationStore(Protocol):
    def load(
        self,
        conversation_id: str,
    ) -> DeviceConversationContext | None: ...

    def save(
        self,
        conversation_id: str,
        context: DeviceConversationContext,
    ) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._items: dict[str, DeviceConversationContext] = {}

    def load(
        self,
        conversation_id: str,
    ) -> DeviceConversationContext | None:
        context = self._items.get(conversation_id)
        return context.model_copy(deep=True) if context is not None else None

    def save(
        self,
        conversation_id: str,
        context: DeviceConversationContext,
    ) -> None:
        self._items[conversation_id] = context.model_copy(deep=True)


class SqliteConversationStore:
    """Persist the small, credential-free conversation context."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @property
    def path(self) -> Path:
        return self._path

    def load(
        self,
        conversation_id: str,
    ) -> DeviceConversationContext | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT context_json
                FROM conversations
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return DeviceConversationContext.model_validate_json(row[0])

    def save(
        self,
        conversation_id: str,
        context: DeviceConversationContext,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id,
                    context_json,
                    updated_at
                )
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    context_json = excluded.context_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (conversation_id, context.model_dump_json()),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
