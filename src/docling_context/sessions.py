"""Revisioned DocLang session events and bounded archive attachments."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape

from doclang import DocLangXDocument

from .local import LocalContextStore, RecordMissing
from .models import Principal
from .package import bounded_nodes
from .profiles import (
    MAX_ATTACHMENT_BYTES,
    MAX_EVENT_CHARS,
    MAX_SESSION_ATTACHMENT_BYTES,
    MAX_SESSION_EVENTS,
    SESSION_KINDS,
    SESSION_PART,
    validate_session,
)
from .uri import authorize_uri, parse_uri


@dataclass(frozen=True, slots=True)
class SessionEvent:
    sequence: int
    key: str
    kind: str
    turn_id: str
    text: str
    created_at: str
    xpath: str
    revision_id: str
    attachment_path: str | None = None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    uri: str
    session_id: str
    revision_id: str
    status: str
    event_count: int
    created_at: str
    updated_at: str
    closed_at: str | None


class SessionStore:
    def __init__(self, store: LocalContextStore):
        self.store = store

    @staticmethod
    def uri(principal: Principal, session_id: str) -> str:
        value = (
            session_id
            if session_id.startswith("docling://")
            else f"docling://users/{principal.tenant_id}/{principal.user_id}/sessions/{session_id}"
        )
        address = parse_uri(value)
        authorize_uri(principal, address)
        is_user_session = (
            address.namespace == "users"
            and len(address.segments) == 4
            and address.segments[2] == "sessions"
        )
        is_project_session = (
            address.namespace == "projects"
            and len(address.segments) == 3
            and address.segments[1] == "sessions"
        )
        if not (is_user_session or is_project_session):
            raise ValueError("expected one session URI")
        return address.value

    @staticmethod
    def _events(
        document: DocLangXDocument, revision_id: str
    ) -> tuple[SessionEvent, ...]:
        metadata = validate_session(document)
        texts = [
            node["text"]
            for node in bounded_nodes(
                document, limit=5_000, text_bytes=MAX_EVENT_CHARS * 4
            )
            if node["name"] == "text"
        ]
        return tuple(
            SessionEvent(
                sequence=number,
                key=event["key"],
                kind=event["kind"],
                turn_id=event["turn_id"],
                text=texts[number - 1],
                created_at=event["created_at"],
                xpath=event["xpath"],
                revision_id=revision_id,
                attachment_path=(event.get("attachment") or {}).get("path"),
            )
            for number, event in enumerate(metadata["events"], 1)
        )

    @staticmethod
    def _package(
        events: tuple[SessionEvent, ...],
        attachments: dict[str, tuple[bytes, str]],
        *,
        created_at: str,
        status: str = "open",
        closed_at: str | None = None,
    ) -> bytes:
        document = DocLangXDocument()
        body = "".join(f"<text>{escape(event.text)}</text>" for event in events)
        if not document.read_xml(f"<doclang>{body}</doclang>"):
            raise ValueError(document.last_error())
        entries = []
        for number, event in enumerate(events, 1):
            entry: dict[str, Any] = {
                "key": event.key,
                "kind": event.kind,
                "turn_id": event.turn_id,
                "created_at": event.created_at,
                "xpath": f"/doclang[1]/text[{number}]",
            }
            if event.attachment_path:
                payload, content_type = attachments[event.attachment_path]
                entry["attachment"] = {
                    "path": event.attachment_path,
                    "content_type": content_type,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            entries.append(entry)
        for path, (payload, content_type) in attachments.items():
            document.set_part_bytes(path, payload, content_type)
        document.set_part_text(
            SESSION_PART,
            json.dumps(
                {
                    "profile": "session-v1",
                    "events": entries,
                    "status": status,
                    "created_at": created_at,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "closed_at": closed_at,
                },
                sort_keys=True,
            ),
            "application/json",
        )
        package = document.write_bytes()
        validate_session(document)
        return package

    def start(self, principal: Principal, session_id: str | None = None) -> SessionInfo:
        session_id = session_id or uuid.uuid4().hex
        uri = self.uri(principal, session_id)
        try:
            current = self.get(principal, uri)
            if current.status == "closed":
                raise ValueError("session ID already closed")
            return current
        except RecordMissing:
            pass
        now = datetime.now(UTC).isoformat()
        address = parse_uri(uri)
        if address.namespace == "projects":
            from .catalog import Catalog

            Catalog(self.store).get_project(principal, address.segments[0])
        with self.store.transaction():
            record = self.store.put(
                principal, uri, self._package((), {}, created_at=now)
            )
            if address.namespace == "projects":
                Catalog(self.store).register_session(
                    principal,
                    address.segments[0],
                    address.segments[2],
                    revision_id=record.revision_id,
                )
        return SessionInfo(
            uri,
            parse_uri(uri).segments[-1],
            record.revision_id,
            "open",
            0,
            now,
            now,
            None,
        )

    def get(self, principal: Principal, session_id: str) -> SessionInfo:
        uri = self.uri(principal, session_id)
        record = self.store.get_record(principal, uri)
        metadata = validate_session(self.store.get_document(principal, uri))
        return SessionInfo(
            uri,
            parse_uri(uri).segments[-1],
            record.revision_id,
            metadata.get("status", "open"),
            len(metadata["events"]),
            metadata.get("created_at") or record.created_at,
            metadata.get("updated_at") or record.updated_at,
            metadata.get("closed_at"),
        )

    def list(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[SessionInfo, ...]:
        if (
            status not in {None, "open", "closed"}
            or not 1 <= limit <= 1000
            or offset < 0
        ):
            raise ValueError("invalid session list filter or page")
        prefix = f"docling://users/{principal.tenant_id}/{principal.user_id}/sessions/"
        rows = self.store.db.execute(
            """SELECT uri FROM documents WHERE tenant_id=? AND available=1
               AND substr(uri,1,?)=? ORDER BY updated_at DESC,uri""",
            (principal.tenant_id, len(prefix), prefix),
        ).fetchall()
        items = []
        for row in rows:
            item = self.get(principal, row["uri"])
            if status and item.status != status:
                continue
            if since and item.updated_at < since:
                continue
            if until and item.updated_at > until:
                continue
            items.append(item)
        return tuple(items[offset : offset + limit])

    def close(self, principal: Principal, session_id: str) -> SessionInfo:
        current = self.get(principal, session_id)
        if current.status == "closed":
            return current
        document = self.store.get_document(principal, current.uri)
        events = self._events(document, current.revision_id)
        attachments = self._attachments(document)
        record = self.store.put(
            principal,
            current.uri,
            self._package(
                events,
                attachments,
                created_at=current.created_at,
                status="closed",
                closed_at=datetime.now(UTC).isoformat(),
            ),
            expected_revision=current.revision_id,
        )
        address = parse_uri(record.uri)
        with self.store.transaction():
            if address.namespace == "projects":
                from .catalog import Catalog

                Catalog(self.store).register_session(
                    principal,
                    address.segments[0],
                    address.segments[2],
                    status="closed",
                    revision_id=record.revision_id,
                )
            self._queue_compile(
                principal, record.uri, record.document_id, record.revision_id
            )
        return self.get(principal, current.uri)

    @staticmethod
    def _attachments(document: DocLangXDocument) -> dict[str, tuple[bytes, str]]:
        attachments = {}
        for item in validate_session(document)["events"]:
            info = item.get("attachment")
            if info:
                payload = document.get_part_bytes(info["path"])
                assert payload is not None
                attachments[info["path"]] = (payload, info["content_type"])
        return attachments

    def _queue_compile(
        self, principal: Principal, uri: str, document_id: str, revision_id: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.store.db.execute(
            """UPDATE memory_compile_jobs SET status='superseded',
               lease_until=NULL,updated_at=? WHERE tenant_id=? AND user_id=?
               AND session_uri=? AND session_revision_id!=? AND status='queued'""",
            (now, principal.tenant_id, principal.user_id, uri, revision_id),
        )
        self.store.db.execute(
            """INSERT OR IGNORE INTO memory_compile_jobs
               (job_id,tenant_id,user_id,session_uri,session_document_id,
                session_revision_id,status,retry_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'queued',?,?,?)""",
            (
                uuid.uuid4().hex,
                principal.tenant_id,
                principal.user_id,
                uri,
                document_id,
                revision_id,
                now,
                now,
                now,
            ),
        )

    def append(
        self,
        principal: Principal,
        session_id: str,
        *,
        key: str,
        kind: str,
        text: str,
        turn_id: str = "",
        attachment: bytes | None = None,
        content_type: str = "application/octet-stream",
    ) -> SessionEvent:
        uri = self.uri(principal, session_id)
        if (
            kind not in SESSION_KINDS
            or not isinstance(key, str)
            or not 1 <= len(key) <= 128
            or not isinstance(text, str)
            or len(text) > MAX_EVENT_CHARS
            or not isinstance(turn_id, str)
            or len(turn_id) > 128
        ):
            raise ValueError("invalid session event")
        if attachment is not None and (
            not isinstance(attachment, bytes)
            or len(attachment) > MAX_ATTACHMENT_BYTES
            or not isinstance(content_type, str)
            or not content_type
        ):
            raise ValueError("invalid or oversized session attachment")
        try:
            current = self.store.get_record(principal, uri)
            document = self.store.get_document(principal, uri)
            previous = self._events(document, current.revision_id)
        except RecordMissing:
            current = None
            document = None
            previous = ()
        duplicate = next((event for event in previous if event.key == key), None)
        if duplicate is not None:
            return duplicate
        metadata = validate_session(document) if document is not None else {}
        if metadata.get("status", "open") != "open":
            raise ValueError("session is closed")
        if len(previous) >= MAX_SESSION_EVENTS:
            raise ValueError("session event limit reached; trim the session")
        path = f"attachments/{uuid.uuid4().hex}.bin" if attachment is not None else None
        event = SessionEvent(
            len(previous) + 1,
            key,
            kind,
            turn_id,
            text,
            datetime.now(UTC).isoformat(),
            f"/doclang[1]/text[{len(previous) + 1}]",
            "",
            path,
        )
        attachments = self._attachments(document) if document is not None else {}
        if path and attachment is not None:
            attachments[path] = (attachment, content_type)
        if (
            sum(len(payload) for payload, _ in attachments.values())
            > MAX_SESSION_ATTACHMENT_BYTES
        ):
            raise ValueError("session attachments exceed the total limit")
        record = self.store.put(
            principal,
            uri,
            self._package(
                (*previous, event),
                attachments,
                created_at=metadata.get("created_at") or event.created_at,
            ),
            expected_revision=current.revision_id if current else None,
        )
        self._queue_compile(principal, uri, record.document_id, record.revision_id)
        return SessionEvent(
            event.sequence,
            event.key,
            event.kind,
            event.turn_id,
            event.text,
            event.created_at,
            event.xpath,
            record.revision_id,
            event.attachment_path,
        )

    def replay(self, principal: Principal, session_id: str) -> tuple[SessionEvent, ...]:
        uri = self.uri(principal, session_id)
        record = self.store.get_record(principal, uri)
        return self._events(self.store.get_document(principal, uri), record.revision_id)

    def read_attachment(self, principal: Principal, session_id: str, key: str) -> bytes:
        uri = self.uri(principal, session_id)
        document = self.store.get_document(principal, uri)
        event = next(
            (item for item in self._events(document, "") if item.key == key), None
        )
        if event is None or event.attachment_path is None:
            raise KeyError(key)
        payload = document.get_part_bytes(event.attachment_path)
        if payload is None:
            raise KeyError(key)
        return payload

    def trim(self, principal: Principal, session_id: str, *, keep_last: int) -> str:
        if not 1 <= keep_last <= MAX_SESSION_EVENTS:
            raise ValueError("keep_last is out of bounds")
        uri = self.uri(principal, session_id)
        record = self.store.get_record(principal, uri)
        document = self.store.get_document(principal, uri)
        retained = self._events(document, record.revision_id)[-keep_last:]
        metadata = validate_session(document)
        attachments = {}
        keep_paths = {
            event.attachment_path for event in retained if event.attachment_path
        }
        for item in metadata["events"]:
            info = item.get("attachment")
            if info and info["path"] in keep_paths:
                payload = document.get_part_bytes(info["path"])
                assert payload is not None
                attachments[info["path"]] = (payload, info["content_type"])
        new_record = self.store.put(
            principal,
            uri,
            self._package(
                retained,
                attachments,
                created_at=metadata.get("created_at") or record.created_at,
                status=metadata.get("status", "open"),
                closed_at=metadata.get("closed_at"),
            ),
            expected_revision=record.revision_id,
        )
        return new_record.revision_id

    def delete(self, principal: Principal, session_id: str) -> None:
        uri = self.uri(principal, session_id)
        record = self.store.get_record(principal, uri)
        self.store.delete(principal, uri, expected_revision=record.revision_id)

    def purge(self, principal: Principal, session_id: str) -> int:
        """Remove every session revision and raw attachment after a logical delete."""
        uri = self.uri(principal, session_id)
        try:
            self.delete(principal, uri)
        except RecordMissing:
            pass
        rows = self.store.db.execute(
            "SELECT package_hash FROM revisions WHERE tenant_id=? AND uri=?",
            (principal.tenant_id, uri),
        ).fetchall()
        with self.store.transaction():
            self.store.db.execute(
                "DELETE FROM revisions WHERE tenant_id=? AND uri=?",
                (principal.tenant_id, uri),
            )
            self.store.db.execute(
                "DELETE FROM memory_compile_jobs WHERE tenant_id=? AND session_uri=?",
                (principal.tenant_id, uri),
            )
            self.store.db.execute(
                "DELETE FROM recall_ledger WHERE tenant_id=? AND session_uri=?",
                (principal.tenant_id, uri),
            )
        for row in rows:
            if not self.store.db.execute(
                "SELECT 1 FROM revisions WHERE package_hash=? LIMIT 1",
                (row["package_hash"],),
            ).fetchone():
                self.store.packages.path_for(row["package_hash"]).unlink(
                    missing_ok=True
                )
        return len(rows)
