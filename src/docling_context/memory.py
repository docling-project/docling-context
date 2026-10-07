"""Small in-memory backend for contract tests and SDK development."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from .local import MAX_TREE_LIMIT, MAX_WALK_DEPTH, RecordMissing, RevisionConflict
from .models import (
    ChangeEvent,
    DocumentRecord,
    NodeAddress,
    PackageRef,
    Principal,
    TreeEntry,
)
from .package import load_package, read_node
from .profiles import validate_profile
from .uri import AccessDenied, authorize_uri, parse_uri


class MemoryContextStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], DocumentRecord] = {}
        self.revisions: dict[tuple[str, str, str], DocumentRecord] = {}
        self.blobs: dict[str, bytes] = {}
        self.outbox: list[ChangeEvent] = []

    def put(
        self,
        principal: Principal,
        uri: str,
        package: bytes,
        *,
        expected_revision: str | None = None,
        source: dict | None = None,
        revision_id: str | None = None,
    ) -> DocumentRecord:
        address = parse_uri(uri)
        authorize_uri(principal, address)
        validate_profile(address.value, load_package(package))
        key = principal.tenant_id, address.value
        current = self.records.get(key)
        if (current is None and expected_revision is not None) or (
            current is not None and current.revision_id != expected_revision
        ):
            raise RevisionConflict("revision changed")
        digest = hashlib.sha256(package).hexdigest()
        now = datetime.now(UTC).isoformat()
        revision_id = revision_id or uuid.uuid4().hex
        if len(revision_id) != 32 or any(
            c not in "0123456789abcdef" for c in revision_id
        ):
            raise ValueError("revision_id must be a lowercase UUID hex string")
        record = DocumentRecord(
            address.value,
            principal.tenant_id,
            current.document_id if current else uuid.uuid4().hex,
            revision_id,
            PackageRef(digest, len(package)),
            address.parent,
            source or {},
            current.created_at if current else now,
            now,
        )
        self.blobs[digest] = package
        self.records[key] = record
        self.revisions[
            (principal.tenant_id, record.document_id, record.revision_id)
        ] = record
        self.outbox.append(
            ChangeEvent(
                len(self.outbox) + 1,
                principal.tenant_id,
                address.value,
                record.document_id,
                record.revision_id,
                "put",
                now,
            )
        )
        return record

    def get_record(self, principal: Principal, uri: str) -> DocumentRecord:
        address = parse_uri(uri)
        authorize_uri(principal, address)
        try:
            record = self.records[(principal.tenant_id, address.value)]
            self.blobs[record.package.sha256]
            return record
        except KeyError as exc:
            raise RecordMissing(address.value) from exc

    def get_revision(
        self, principal: Principal, address: NodeAddress
    ) -> DocumentRecord:
        try:
            record = self.revisions[
                (principal.tenant_id, address.document_id, address.revision_id)
            ]
        except KeyError as exc:
            raise RecordMissing(address.revision_id) from exc
        authorize_uri(principal, parse_uri(record.uri))
        return record

    def get_package(self, principal: Principal, uri: str) -> bytes:
        return self.blobs[self.get_record(principal, uri).package.sha256]

    def get_document(self, principal: Principal, uri: str):
        return load_package(self.get_package(principal, uri))

    def read_node(
        self, principal: Principal, address: NodeAddress, max_chars: int = 65_536
    ):
        record = self.get_revision(principal, address)
        return read_node(self.blobs[record.package.sha256], address, max_chars)

    def delete(self, principal: Principal, uri: str, *, expected_revision: str) -> None:
        record = self.get_record(principal, uri)
        if record.revision_id != expected_revision:
            raise RevisionConflict("revision changed")
        del self.records[(principal.tenant_id, record.uri)]
        self.outbox.append(
            ChangeEvent(
                len(self.outbox) + 1,
                principal.tenant_id,
                record.uri,
                record.document_id,
                record.revision_id,
                "delete",
                datetime.now(UTC).isoformat(),
            )
        )

    def walk(
        self, principal: Principal, uri: str, depth: int = 3, limit: int = 100
    ) -> list[TreeEntry]:
        if not 1 <= depth <= MAX_WALK_DEPTH or not 1 <= limit <= MAX_TREE_LIMIT:
            raise ValueError("tree depth or limit is out of bounds")
        address = parse_uri(uri, prefix=True)
        authorize_uri(principal, address)
        base = address.value + "/"
        entries: dict[str, TreeEntry] = {}
        for record in self.records.values():
            if record.tenant_id != principal.tenant_id or not record.uri.startswith(
                base
            ):
                continue
            segments = record.uri[len(base) :].split("/")
            for index in range(1, min(len(segments), depth) + 1):
                child_uri = base + "/".join(segments[:index])
                is_document = index == len(segments)
                entries[child_uri] = TreeEntry(
                    child_uri,
                    "document" if is_document else "directory",
                    index,
                    record.document_id if is_document else None,
                    record.revision_id if is_document else None,
                )
        return sorted(entries.values(), key=lambda entry: entry.uri)[:limit]

    def list_children(
        self, principal: Principal, uri: str, limit: int = 100
    ) -> list[TreeEntry]:
        return self.walk(principal, uri, depth=1, limit=limit)

    def events(
        self, principal: Principal, after: int = 0, limit: int = 100
    ) -> list[ChangeEvent]:
        if after < 0 or not 1 <= limit <= MAX_TREE_LIMIT:
            raise ValueError("event cursor or limit is out of bounds")
        result = []
        for event in self.outbox:
            if event.sequence <= after or event.tenant_id != principal.tenant_id:
                continue
            try:
                authorize_uri(principal, parse_uri(event.uri))
            except AccessDenied:
                continue
            result.append(event)
            if len(result) == limit:
                break
        return result
