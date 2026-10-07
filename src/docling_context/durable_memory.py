"""Reviewed, provenance-carrying long-term memory packages."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from xml.sax.saxutils import escape

from doclang import DocLangXDocument

from .local import LocalContextStore, RecordMissing
from .models import NodeAddress, Principal
from .package import PackageError, PackageMissing
from .profiles import MEMORY_KINDS, MEMORY_PART, MEMORY_STATUSES, validate_memory
from .retrieval import Retriever, SearchScope
from .uri import authorize_uri, parse_uri


@dataclass(frozen=True, slots=True)
class SourceCitation:
    uri: str
    document_id: str
    revision_id: str
    xpath: str


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    uri: str
    memory_id: str
    revision_id: str
    kind: str
    claim: str
    explanation: str
    status: str
    confidence: float
    author: str
    generator: str | None
    subject: str | None
    valid_from: str | None
    valid_to: str | None
    citations: tuple[SourceCitation, ...]
    conflicts: tuple[str, ...]
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _claim_key(claim: str) -> str:
    normalized = " ".join(re.findall(r"\w+", claim.casefold()))
    return hashlib.sha256(normalized.encode()).hexdigest()


class MemoryService:
    def __init__(self, store: LocalContextStore):
        self.store = store

    @staticmethod
    def _prefix(principal: Principal) -> str:
        return f"docling://users/{principal.tenant_id}/{principal.user_id}/memories"

    @classmethod
    def _uri(cls, principal: Principal, uri: str) -> str:
        address = parse_uri(uri)
        authorize_uri(principal, address)
        if (
            address.namespace != "users"
            or len(address.segments) != 5
            or address.segments[2] != "memories"
            or address.segments[3] not in MEMORY_KINDS
        ):
            raise ValueError("expected one memory URI")
        return address.value

    def _record(self, principal: Principal, uri: str) -> MemoryRecord:
        uri = self._uri(principal, uri)
        stored = self.store.get_record(principal, uri)
        metadata = validate_memory(self.store.get_document(principal, uri))
        if (
            metadata["kind"] != parse_uri(uri).segments[3]
            or metadata["memory_id"] != parse_uri(uri).segments[4]
        ):
            raise ValueError("memory metadata differs from URI")
        return MemoryRecord(
            uri=uri,
            memory_id=metadata["memory_id"],
            revision_id=stored.revision_id,
            kind=metadata["kind"],
            claim=metadata["claim"],
            explanation=metadata["explanation"],
            status=metadata["status"],
            confidence=float(metadata["confidence"]),
            author=metadata["author"],
            generator=metadata.get("generator"),
            subject=metadata.get("subject"),
            valid_from=metadata.get("valid_from"),
            valid_to=metadata.get("valid_to"),
            citations=tuple(SourceCitation(**item) for item in metadata["citations"]),
            conflicts=tuple(metadata.get("conflicts", [])),
            updated_at=stored.updated_at,
        )

    def _sync(self, principal: Principal) -> None:
        prefix = self._prefix(principal) + "/"
        documents = self.store.db.execute(
            """SELECT uri,revision_id,updated_at FROM documents
               WHERE tenant_id=? AND available=1 AND substr(uri,1,?)=?""",
            (principal.tenant_id, len(prefix), prefix),
        ).fetchall()
        active = set()
        for row in documents:
            try:
                uri = self._uri(principal, row["uri"])
            except (ValueError, PermissionError):
                continue
            active.add(uri)
            indexed = self.store.db.execute(
                "SELECT revision_id FROM memory_index WHERE tenant_id=? AND uri=?",
                (principal.tenant_id, uri),
            ).fetchone()
            if indexed is not None and indexed["revision_id"] == row["revision_id"]:
                continue
            record = self._record(principal, uri)
            with self.store.transaction():
                self.store.db.execute(
                    """INSERT INTO memory_index
                       (tenant_id,user_id,uri,memory_id,kind,subject,claim,claim_key,
                        status,confidence,revision_id,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(tenant_id,uri) DO UPDATE SET
                       subject=excluded.subject,claim=excluded.claim,
                       claim_key=excluded.claim_key,status=excluded.status,
                       confidence=excluded.confidence,revision_id=excluded.revision_id,
                       updated_at=excluded.updated_at""",
                    (
                        principal.tenant_id,
                        principal.user_id,
                        uri,
                        record.memory_id,
                        record.kind,
                        record.subject,
                        record.claim,
                        _claim_key(record.claim),
                        record.status,
                        record.confidence,
                        record.revision_id,
                        row["updated_at"],
                    ),
                )
                self.store.db.execute(
                    "DELETE FROM memory_edges WHERE tenant_id=? AND memory_uri=?",
                    (principal.tenant_id, uri),
                )
                self.store.db.executemany(
                    """INSERT INTO memory_edges
                       (tenant_id,user_id,memory_uri,source_uri,source_document_id,
                        source_revision_id,source_xpath) VALUES (?,?,?,?,?,?,?)""",
                    (
                        (
                            principal.tenant_id,
                            principal.user_id,
                            uri,
                            citation.uri,
                            citation.document_id,
                            citation.revision_id,
                            citation.xpath,
                        )
                        for citation in record.citations
                    ),
                )
        stale = self.store.db.execute(
            "SELECT uri FROM memory_index WHERE tenant_id=? AND user_id=?",
            (principal.tenant_id, principal.user_id),
        ).fetchall()
        with self.store.transaction():
            for row in stale:
                if row["uri"] not in active:
                    self.store.db.execute(
                        "DELETE FROM memory_index WHERE tenant_id=? AND uri=?",
                        (principal.tenant_id, row["uri"]),
                    )
                    self.store.db.execute(
                        "DELETE FROM memory_edges WHERE tenant_id=? AND memory_uri=?",
                        (principal.tenant_id, row["uri"]),
                    )

    def get(self, principal: Principal, uri: str) -> MemoryRecord:
        self._sync(principal)
        return self._record(principal, uri)

    def _validate_citations(
        self, principal: Principal, citations: tuple[SourceCitation, ...]
    ) -> None:
        if len(citations) > 100:
            raise ValueError("too many memory citations")
        for citation in citations:
            current = self.store.get_record(principal, citation.uri)
            stored = self.store.get_revision(
                principal,
                NodeAddress(citation.document_id, citation.revision_id, citation.xpath),
            )
            if (
                stored.uri != citation.uri
                or current.document_id != citation.document_id
            ):
                raise ValueError("citation URI differs from its revision")
            self.store.read_node(
                principal,
                NodeAddress(citation.document_id, citation.revision_id, citation.xpath),
            )

    def _write(
        self,
        principal: Principal,
        uri: str,
        metadata: dict,
        *,
        expected_revision: str | None,
    ) -> MemoryRecord:
        document = DocLangXDocument()
        xml = (
            f"<doclang><heading>{escape(metadata['kind'])}</heading>"
            f"<text>{escape(metadata['claim'])}</text>"
            f"<text>{escape(metadata['explanation'])}</text></doclang>"
        )
        if not document.read_xml(xml):
            raise ValueError(document.last_error())
        document.set_part_text(
            MEMORY_PART, json.dumps(metadata, sort_keys=True), "application/json"
        )
        validate_memory(document)
        self.store.put(
            principal,
            uri,
            document.write_bytes(),
            expected_revision=expected_revision,
        )
        self._sync(principal)
        return self._record(principal, uri)

    def create(
        self,
        principal: Principal,
        kind: str,
        claim: str,
        *,
        explanation: str = "",
        citations: tuple[SourceCitation, ...] = (),
        confidence: float = 1.0,
        subject: str | None = None,
        author: str = "user",
        generator: str | None = None,
        accepted: bool = False,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> MemoryRecord:
        if kind not in MEMORY_KINDS or not isinstance(claim, str) or not claim.strip():
            raise ValueError("invalid memory kind or claim")
        if accepted and author != "user":
            raise ValueError("generated memories require explicit review")
        self._validate_citations(principal, citations)
        self._sync(principal)
        key = _claim_key(claim)
        existing = self.store.db.execute(
            """SELECT uri FROM memory_index WHERE tenant_id=? AND user_id=?
               AND kind=? AND subject IS ? AND claim_key=?
               ORDER BY updated_at DESC LIMIT 1""",
            (principal.tenant_id, principal.user_id, kind, subject, key),
        ).fetchone()
        if existing is not None:
            return self._record(principal, existing["uri"])
        # Corrections retain their old claim keys to prevent replaying an older
        # session revision from reviving a claim the user already corrected.
        prior = self.store.db.execute(
            """SELECT uri FROM memory_index WHERE tenant_id=? AND user_id=?
               AND kind=? AND subject IS ? AND status!='deleted'""",
            (principal.tenant_id, principal.user_id, kind, subject),
        ).fetchall()
        for row in prior:
            metadata = validate_memory(self.store.get_document(principal, row["uri"]))
            if key in metadata.get("claim_aliases", []):
                return self._record(principal, row["uri"])
        conflicts = []
        if subject:
            conflicts = [
                row["uri"]
                for row in self.store.db.execute(
                    """SELECT uri FROM memory_index WHERE tenant_id=? AND user_id=?
                       AND kind=? AND subject=? AND status='accepted'""",
                    (principal.tenant_id, principal.user_id, kind, subject),
                )
            ]
        memory_id = uuid.uuid4().hex
        uri = f"{self._prefix(principal)}/{kind}/{memory_id}"
        status = "accepted" if accepted else "proposed"
        metadata = {
            "profile": "memory-v1",
            "memory_id": memory_id,
            "kind": kind,
            "claim": claim.strip(),
            "explanation": explanation,
            "status": status,
            "confidence": confidence,
            "subject": subject,
            "author": author,
            "generator": generator,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "citations": [asdict(citation) for citation in citations],
            "conflicts": conflicts,
            "history": [{"status": status, "by": author, "at": _now()}],
        }
        return self._write(principal, uri, metadata, expected_revision=None)

    def transition(
        self, principal: Principal, uri: str, status: str, *, reason: str = ""
    ) -> MemoryRecord:
        if status not in MEMORY_STATUSES:
            raise ValueError("invalid memory status")
        current = self.get(principal, uri)
        if current.status == status:
            return current
        allowed = {
            "proposed": {"accepted", "rejected", "deleted"},
            "accepted": {"superseded", "proposed", "deleted"},
            "rejected": {"deleted"},
            "superseded": {"deleted"},
            "deleted": set(),
        }
        if status not in allowed[current.status]:
            raise ValueError("invalid memory transition")
        metadata = validate_memory(self.store.get_document(principal, current.uri))
        metadata["status"] = status
        metadata["history"].append(
            {"status": status, "by": principal.user_id, "at": _now(), "reason": reason}
        )
        return self._write(
            principal, current.uri, metadata, expected_revision=current.revision_id
        )

    def accept(self, principal: Principal, uri: str) -> MemoryRecord:
        return self.transition(principal, uri, "accepted")

    def reject(
        self, principal: Principal, uri: str, *, reason: str = ""
    ) -> MemoryRecord:
        return self.transition(principal, uri, "rejected", reason=reason)

    def delete(
        self, principal: Principal, uri: str, *, reason: str = ""
    ) -> MemoryRecord:
        return self.transition(principal, uri, "deleted", reason=reason)

    def purge(self, principal: Principal, uri: str) -> int:
        """Remove a deleted claim, its revision history, and its recall records."""
        memory = self.get(principal, uri)
        if memory.status != "deleted":
            raise ValueError("delete the memory before purging it")
        rows = self.store.db.execute(
            "SELECT package_hash FROM revisions WHERE tenant_id=? AND uri=?",
            (principal.tenant_id, memory.uri),
        ).fetchall()
        self.store.delete(principal, memory.uri, expected_revision=memory.revision_id)
        with self.store.transaction():
            self.store.db.execute(
                "DELETE FROM revisions WHERE tenant_id=? AND uri=?",
                (principal.tenant_id, memory.uri),
            )
            self.store.db.execute(
                "DELETE FROM memory_index WHERE tenant_id=? AND uri=?",
                (principal.tenant_id, memory.uri),
            )
            self.store.db.execute(
                "DELETE FROM memory_edges WHERE tenant_id=? AND memory_uri=?",
                (principal.tenant_id, memory.uri),
            )
            self.store.db.execute(
                """DELETE FROM recall_ledger WHERE tenant_id=? AND user_id=?
                   AND memory_id=?""",
                (principal.tenant_id, principal.user_id, memory.memory_id),
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

    def correct(
        self,
        principal: Principal,
        uri: str,
        claim: str,
        *,
        explanation: str | None = None,
        citations: tuple[SourceCitation, ...] | None = None,
    ) -> MemoryRecord:
        current = self.get(principal, uri)
        if current.status not in {"accepted", "proposed"} or not claim.strip():
            raise ValueError("only active memories can be corrected")
        sources = citations if citations is not None else current.citations
        self._validate_citations(principal, sources)
        metadata = validate_memory(self.store.get_document(principal, current.uri))
        aliases = metadata.setdefault("claim_aliases", [])
        aliases.append(_claim_key(current.claim))
        metadata.update(
            claim=claim.strip(),
            explanation=current.explanation if explanation is None else explanation,
            status="accepted",
            author="user",
            generator=None,
            citations=[asdict(citation) for citation in sources],
            supersedes_revision=current.revision_id,
        )
        metadata["history"].append(
            {
                "status": "accepted",
                "by": principal.user_id,
                "at": _now(),
                "reason": "correction",
                "supersedes_revision": current.revision_id,
            }
        )
        return self._write(
            principal, current.uri, metadata, expected_revision=current.revision_id
        )

    def reconcile_sources(self, principal: Principal) -> int:
        """Move accepted claims with no surviving cited source back to review."""
        self._sync(principal)
        rows = self.store.db.execute(
            """SELECT uri FROM memory_index WHERE tenant_id=? AND user_id=?
               AND status='accepted'""",
            (principal.tenant_id, principal.user_id),
        ).fetchall()
        changed = 0
        for row in rows:
            memory = self._record(principal, row["uri"])
            if not memory.citations:
                continue
            supported = False
            for citation in memory.citations:
                try:
                    current = self.store.get_record(principal, citation.uri)
                    if current.document_id != citation.document_id:
                        continue
                    self.store.read_node(
                        principal,
                        NodeAddress(
                            citation.document_id, citation.revision_id, citation.xpath
                        ),
                    )
                except (RecordMissing, PackageError, PackageMissing, PermissionError):
                    continue
                supported = True
                break
            if not supported:
                self.transition(
                    principal,
                    memory.uri,
                    "proposed",
                    reason="cited sources unavailable",
                )
                changed += 1
        return changed

    def search(
        self, principal: Principal, query: str = "", *, limit: int = 10
    ) -> tuple[MemoryRecord, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("memory search limit is out of bounds")
        self.reconcile_sources(principal)
        if not query.strip():
            rows = self.store.db.execute(
                """SELECT uri FROM memory_index WHERE tenant_id=? AND user_id=?
                   AND status='accepted' ORDER BY updated_at DESC LIMIT ?""",
                (principal.tenant_id, principal.user_id, limit),
            ).fetchall()
            return tuple(self._record(principal, row["uri"]) for row in rows)
        result = Retriever(self.store).search(
            principal,
            query,
            scope=SearchScope(uri=self._prefix(principal)),
            mode="lexical",
            k=100,
        )
        seen = set()
        records = []
        for hit in result.hits:
            if hit.uri in seen:
                continue
            seen.add(hit.uri)
            memory = self._record(principal, hit.uri)
            if memory.status == "accepted":
                records.append(memory)
            if len(records) >= limit:
                break
        return tuple(records)

    def list(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[MemoryRecord, ...]:
        """List current claims, including review history, for the current user."""
        if (
            status not in MEMORY_STATUSES | {None}
            or kind not in MEMORY_KINDS | {None}
            or not 1 <= limit <= 1000
            or offset < 0
        ):
            raise ValueError("invalid memory list filter or page")
        self.reconcile_sources(principal)
        conditions = ["tenant_id=?", "user_id=?"]
        params: list[str | int] = [principal.tenant_id, principal.user_id]
        for column, value, operator in (
            ("status", status, "="),
            ("kind", kind, "="),
            ("updated_at", since, ">="),
            ("updated_at", until, "<="),
        ):
            if value is not None:
                conditions.append(f"{column}{operator}?")
                params.append(value)
        rows = self.store.db.execute(
            f"SELECT uri FROM memory_index WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC,uri LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return tuple(self._record(principal, row["uri"]) for row in rows)

    def recall(
        self,
        principal: Principal,
        session_id: str,
        query: str = "",
        *,
        limit: int = 10,
    ) -> tuple[MemoryRecord, ...]:
        from .sessions import SessionStore

        session_uri = SessionStore.uri(principal, session_id)
        self.store.get_record(principal, session_uri)
        candidates = self.search(principal, query, limit=100)
        delivered = []
        for memory in candidates:
            with self.store.transaction():
                cursor = self.store.db.execute(
                    """INSERT OR IGNORE INTO recall_ledger
                       (tenant_id,user_id,session_uri,memory_id,revision_id,delivered_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        principal.tenant_id,
                        principal.user_id,
                        session_uri,
                        memory.memory_id,
                        memory.revision_id,
                        _now(),
                    ),
                )
            if cursor.rowcount:
                delivered.append(memory)
            if len(delivered) >= limit:
                break
        return tuple(delivered)
