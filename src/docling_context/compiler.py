"""Deterministic, retryable compilation of selected session observations."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .durable_memory import MemoryService, SourceCitation
from .local import LocalContextStore, RecordMissing
from .models import Principal
from .profiles import MAX_SESSION_EVENTS
from .sessions import SessionStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


_SECRET = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|token|password|secret)\b\s*[:=]\s*\S+|"
    r"\b(?:sk-[a-z0-9_-]{12,}|ghp_[a-z0-9]{12,})\b)"
)


def redact_secrets(text: str) -> str:
    """Remove common credential shapes before storing a proposed claim."""
    return _SECRET.sub("[REDACTED]", text)


@dataclass(frozen=True, slots=True)
class MemoryJob:
    job_id: str
    session_uri: str
    session_revision_id: str
    status: str
    attempts: int
    last_error: str | None


class MemoryCompiler:
    def __init__(
        self,
        store: LocalContextStore,
        *,
        redactor: Callable[[str], str] = redact_secrets,
        max_attempts: int = 3,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.sessions = SessionStore(store)
        self.memories = MemoryService(store)
        self.redactor = redactor
        self.max_attempts = max_attempts

    def enqueue_missing(self) -> int:
        """Recover jobs for session puts whose write completed before enqueue."""
        rows = self.store.db.execute(
            """SELECT o.tenant_id,o.uri,o.document_id,o.revision_id
               FROM outbox o JOIN documents d
                 ON d.tenant_id=o.tenant_id AND d.uri=o.uri
                AND d.revision_id=o.revision_id
               WHERE o.operation='put' AND o.uri LIKE 'docling://users/%/sessions/%'
               AND NOT EXISTS (SELECT 1 FROM memory_compile_jobs j
                 WHERE j.tenant_id=o.tenant_id AND j.session_uri=o.uri
                   AND j.session_revision_id=o.revision_id)"""
        ).fetchall()
        added = 0
        for row in rows:
            parts = row["uri"].split("/")
            if len(parts) != 7 or parts[5] != "sessions":
                continue
            principal = Principal(parts[3], parts[4])
            try:
                self.sessions.uri(principal, row["uri"])
            except (ValueError, PermissionError):
                continue
            now = _now()
            with self.store.transaction():
                self.store.db.execute(
                    """UPDATE memory_compile_jobs SET status='superseded',
                       lease_until=NULL,updated_at=? WHERE tenant_id=? AND user_id=?
                       AND session_uri=? AND session_revision_id!=? AND status='queued'""",
                    (
                        now,
                        principal.tenant_id,
                        principal.user_id,
                        row["uri"],
                        row["revision_id"],
                    ),
                )
                cursor = self.store.db.execute(
                    """INSERT OR IGNORE INTO memory_compile_jobs
                       (job_id,tenant_id,user_id,session_uri,session_document_id,
                        session_revision_id,status,retry_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,'queued',?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        principal.tenant_id,
                        principal.user_id,
                        row["uri"],
                        row["document_id"],
                        row["revision_id"],
                        now,
                        now,
                        now,
                    ),
                )
                added += cursor.rowcount
        return added

    def jobs(
        self, principal: Principal, session_id: str, *, limit: int = 100
    ) -> tuple[MemoryJob, ...]:
        """Inspect compilation outcomes without crossing the session owner scope."""
        if not 1 <= limit <= 100:
            raise ValueError("job list limit is out of bounds")
        uri = self.sessions.uri(principal, session_id)
        rows = self.store.db.execute(
            """SELECT * FROM memory_compile_jobs WHERE tenant_id=? AND user_id=?
               AND session_uri=? ORDER BY created_at DESC,job_id LIMIT ?""",
            (principal.tenant_id, principal.user_id, uri, limit),
        ).fetchall()
        return tuple(
            MemoryJob(
                row["job_id"],
                row["session_uri"],
                row["session_revision_id"],
                row["status"],
                row["attempts"],
                row["last_error"],
            )
            for row in rows
        )

    @staticmethod
    def _candidate(kind: str, text: str) -> tuple[str, str] | None:
        if kind not in {"feedback", "turn"}:
            return None
        clean = text.strip()
        if clean.casefold().startswith("remember:"):
            claim = clean[len("remember:") :].strip()
            return (
                "preference" if claim.casefold().startswith("i prefer ") else "fact"
            ), claim
        if clean.casefold().startswith("remember that "):
            claim = clean[len("remember that ") :].strip()
            return (
                "preference" if claim.casefold().startswith("i prefer ") else "fact"
            ), claim
        if clean.casefold().startswith("i prefer "):
            return "preference", clean
        return None

    def _process(self, row) -> int:
        principal = Principal(row["tenant_id"], row["user_id"])
        try:
            current = self.store.get_record(principal, row["session_uri"])
        except RecordMissing:
            return 0
        if current.revision_id != row["session_revision_id"]:
            return 0
        events = self.sessions.replay(principal, row["session_uri"])
        if len(events) > MAX_SESSION_EVENTS:
            raise ValueError("session exceeds compiler event limit")
        count = 0
        for event in events:
            candidate = self._candidate(event.kind, event.text)
            if candidate is None:
                continue
            kind, claim = candidate
            claim = self.redactor(claim).strip()
            if not claim or claim == "[REDACTED]":
                continue
            citation = SourceCitation(
                current.uri, current.document_id, current.revision_id, event.xpath
            )
            before = self.memories.create(
                principal,
                kind,
                claim,
                citations=(citation,),
                author="compiler",
                generator="deterministic-v1",
                confidence=0.5,
            )
            if before.status == "proposed" and before.citations == (citation,):
                count += 1
        return count

    def run_once(self) -> MemoryJob | None:
        self.enqueue_missing()
        now = _now()
        with self.store.transaction():
            row = self.store.db.execute(
                """SELECT * FROM memory_compile_jobs WHERE
                   (status='queued' AND retry_at<=?) OR
                   (status='running' AND lease_until<=?)
                   ORDER BY created_at,job_id LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            self.store.db.execute(
                """UPDATE memory_compile_jobs SET status='running', attempts=attempts+1,
                   lease_until=?,updated_at=? WHERE job_id=?""",
                (
                    (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    now,
                    row["job_id"],
                ),
            )
        try:
            self._process(row)
        except Exception as exc:  # noqa: BLE001 - persist worker failures for retry
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= self.max_attempts else "queued"
            with self.store.transaction():
                self.store.db.execute(
                    """UPDATE memory_compile_jobs SET status=?,retry_at=?,
                       lease_until=NULL,last_error=?,updated_at=? WHERE job_id=?""",
                    (
                        status,
                        (
                            datetime.now(UTC) + timedelta(seconds=2**attempts)
                        ).isoformat(),
                        str(exc)[:500],
                        _now(),
                        row["job_id"],
                    ),
                )
        else:
            with self.store.transaction():
                self.store.db.execute(
                    """UPDATE memory_compile_jobs SET status='completed',
                       lease_until=NULL,last_error=NULL,updated_at=? WHERE job_id=?""",
                    (_now(), row["job_id"]),
                )
        final = self.store.db.execute(
            "SELECT * FROM memory_compile_jobs WHERE job_id=?", (row["job_id"],)
        ).fetchone()
        return MemoryJob(
            final["job_id"],
            final["session_uri"],
            final["session_revision_id"],
            final["status"],
            final["attempts"],
            final["last_error"],
        )
