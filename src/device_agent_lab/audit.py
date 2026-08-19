from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class AuditRecord(BaseModel):
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    thread_id: str
    conversation_id: str | None = None
    status: Literal["completed", "confirmation_required"]
    request: str
    command: dict[str, Any]
    approved: bool | None = None
    result: dict[str, Any] | None = None
    trace: list[str] = Field(default_factory=list)
    knowledge_ids: list[str] = Field(default_factory=list)


class DeviceSwitchAuditRecord(BaseModel):
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    event_type: Literal["device_switch"] = "device_switch"
    from_profile: str
    to_profile: str
    status: Literal["completed", "failed"]


AuditEntry = AuditRecord | DeviceSwitchAuditRecord


class AuditLog(Protocol):
    def append(self, record: AuditEntry) -> None: ...


class NullAuditLog:
    def append(self, record: AuditEntry) -> None:
        del record


class InMemoryAuditLog:
    def __init__(self) -> None:
        self.records: list[AuditEntry] = []

    def append(self, record: AuditEntry) -> None:
        self.records.append(record)


class JsonlAuditLog:
    """Append-only local audit log that never receives backend credentials."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: AuditEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json())
            handle.write("\n")
