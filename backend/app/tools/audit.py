"""Audit logging for the Agent Tool System (Phase 9).

Every tool invocation -- allowed or denied, successful or failed -- is
recorded here. Like `LLMTelemetryRecord` (Phase 4), audit entries never
store raw file contents or full command output, only metadata, so the log
itself can't become a secrets-leak vector.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.tools.permissions import Permission


class AuditLogEntry(BaseModel):
    """A single recorded tool invocation."""

    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent_id: str
    tool_name: str
    required_permission: Permission
    permission_granted: bool
    arguments_summary: dict[str, str] = Field(default_factory=dict)
    success: bool
    error_type: str | None = None
    error_message: str | None = None
    duration_seconds: float
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuditLog:
    """Append-only in-memory audit trail.

    Async-safety mirrors `AgentRegistry`: intended to be owned by a single
    orchestration coroutine at a time, no internal locking.
    """

    def __init__(self, on_record: Callable[[AuditLogEntry], None] | None = None) -> None:
        self._entries: list[AuditLogEntry] = []
        #: Optional hook fired (synchronously, best-effort) for every
        #: recorded entry -- Phase 19 wires this to
        #: `RealtimeEmitter.emit_soon(AGENT_TOOL_CALL, ...)` so a live
        #: dashboard can show tool calls as they happen, without the
        #: audit log itself needing to know a WebSocket exists.
        self._on_record = on_record

    def record(self, entry: AuditLogEntry) -> AuditLogEntry:
        self._entries.append(entry)
        if self._on_record is not None:
            try:
                self._on_record(entry)
            except Exception:  # noqa: BLE001 - an observability hook must never break a tool call
                pass
        return entry

    def entries(self) -> list[AuditLogEntry]:
        return list(self._entries)

    def for_agent(self, agent_id: str) -> list[AuditLogEntry]:
        return [e for e in self._entries if e.agent_id == agent_id]

    def denied(self) -> list[AuditLogEntry]:
        return [e for e in self._entries if not e.permission_granted]

    def failures(self) -> list[AuditLogEntry]:
        return [e for e in self._entries if e.permission_granted and not e.success]

    def __len__(self) -> int:
        return len(self._entries)


class _Timer:
    """Tiny helper so tool implementations don't hand-roll `time.monotonic()`."""

    def __enter__(self) -> _Timer:
        self._start = time.monotonic()
        self.duration = 0.0
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.duration = time.monotonic() - self._start
