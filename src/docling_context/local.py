"""SQLite namespace, revision history, and transactional local outbox."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from .models import (
    ChangeEvent,
    DocumentRecord,
    NodeAddress,
    PackageRef,
    Principal,
    TreeEntry,
)
from .package import (
    FilePackageStore,
    PackageError,
    PackageMissing,
    load_package,
    read_node,
)
from .uri import AccessDenied, authorize_uri, parse_uri

MAX_TREE_LIMIT = 1_000
MAX_WALK_DEPTH = 32


class RevisionConflict(RuntimeError):
    pass


class RecordMissing(KeyError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LocalContextStore:
    """Local backend whose public operations all require a principal."""

    def __init__(self, root: str | Path, *, reconcile_on_start: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.packages = FilePackageStore(self.root / "packages")
        self.db = sqlite3.connect(self.root / "context.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        if reconcile_on_start:
            self.reconcile()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise RuntimeError("database schema is newer than this library")
        if version == 0:
            self.db.executescript(
                """BEGIN IMMEDIATE;
                CREATE TABLE documents (
                  tenant_id TEXT NOT NULL, uri TEXT NOT NULL, document_id TEXT NOT NULL,
                  revision_id TEXT NOT NULL, package_hash TEXT NOT NULL,
                  package_size INTEGER NOT NULL, parent_uri TEXT,
                  source_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  available INTEGER NOT NULL DEFAULT 1,
                  PRIMARY KEY (tenant_id, uri), UNIQUE (document_id)
                );
                CREATE TABLE revisions (
                  revision_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                  tenant_id TEXT NOT NULL, uri TEXT NOT NULL,
                  package_hash TEXT NOT NULL, package_size INTEGER NOT NULL,
                  parent_uri TEXT, source_json TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  available INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX revision_lookup ON revisions (tenant_id, document_id, revision_id);
                CREATE TABLE outbox (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                  uri TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                  operation TEXT NOT NULL, occurred_at TEXT NOT NULL
                );
                CREATE INDEX outbox_tenant_sequence ON outbox (tenant_id, sequence);
                PRAGMA user_version = 1;
                COMMIT;
                """
            )
        if version < 2:
            self.db.executescript(
                """BEGIN IMMEDIATE;
                CREATE TABLE collection_status (
                  tenant_id TEXT NOT NULL, collection_uri TEXT NOT NULL,
                  stale INTEGER NOT NULL DEFAULT 1, latest_event INTEGER NOT NULL,
                  summary_revision TEXT,
                  PRIMARY KEY (tenant_id, collection_uri)
                );
                CREATE TABLE jobs (
                  job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  collection_uri TEXT NOT NULL, input_revision TEXT NOT NULL,
                  input_event INTEGER NOT NULL UNIQUE,
                  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  retry_at TEXT NOT NULL, lease_until TEXT, last_error TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX jobs_due ON jobs (status, retry_at, lease_until);
                """
            )
            try:
                if version == 1:
                    collections: dict[tuple[str, str], tuple[str, str]] = {}
                    for row in self.db.execute(
                        "SELECT tenant_id,uri,revision_id FROM documents WHERE available=1"
                    ):
                        address = parse_uri(row["uri"])
                        if address.namespace == "resources" and address.segments[
                            1:
                        ] != (
                            "_context",
                            "summary",
                        ):
                            key = (
                                row["tenant_id"],
                                f"docling://resources/{address.segments[0]}",
                            )
                            collections[key] = (row["uri"], row["revision_id"])
                    for (tenant_id, collection_uri), (
                        uri,
                        revision_id,
                    ) in collections.items():
                        prefix = collection_uri + "/"
                        event = self.db.execute(
                            """SELECT sequence FROM outbox WHERE tenant_id=?
                               AND substr(uri,1,?)=? ORDER BY sequence DESC LIMIT 1""",
                            (tenant_id, len(prefix), prefix),
                        ).fetchone()
                        if event is not None:
                            self._queue_collection(
                                Principal(tenant_id, "migration"),
                                uri,
                                revision_id,
                                event["sequence"],
                                _now(),
                            )
                self.db.execute("PRAGMA user_version = 2")
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def _queue_collection(
        self, principal: Principal, uri: str, revision_id: str, event: int, now: str
    ) -> None:
        address = parse_uri(uri)
        if address.namespace != "resources" or address.segments[1:] == (
            "_context",
            "summary",
        ):
            return
        collection_uri = f"docling://resources/{address.segments[0]}"
        self.db.execute(
            """INSERT INTO collection_status (tenant_id,collection_uri,stale,latest_event)
               VALUES (?,?,1,?) ON CONFLICT(tenant_id,collection_uri)
               DO UPDATE SET stale=1,latest_event=excluded.latest_event""",
            (principal.tenant_id, collection_uri, event),
        )
        self.db.execute(
            """INSERT INTO jobs
               (job_id,tenant_id,collection_uri,input_revision,input_event,status,retry_at,created_at,updated_at)
               VALUES (?,?,?,?,?,'queued',?,?,?)""",
            (
                uuid.uuid4().hex,
                principal.tenant_id,
                collection_uri,
                revision_id,
                event,
                now,
                now,
                now,
            ),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _record(row: sqlite3.Row) -> DocumentRecord:
        return DocumentRecord(
            uri=row["uri"],
            tenant_id=row["tenant_id"],
            document_id=row["document_id"],
            revision_id=row["revision_id"],
            package=PackageRef(row["package_hash"], row["package_size"]),
            parent_uri=row["parent_uri"],
            source=json.loads(row["source_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _verified_record(self, row: sqlite3.Row | None) -> DocumentRecord:
        if row is None or not row["available"]:
            raise RecordMissing("document is unavailable")
        record = self._record(row)
        self.packages.get(record.package.sha256)
        return record

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
        canonical = address.value
        source_json = json.dumps(source or {}, sort_keys=True, ensure_ascii=False)
        digest, size = self.packages.put(package)
        now = _now()
        with self.transaction():
            current = self.db.execute(
                "SELECT * FROM documents WHERE tenant_id=? AND uri=?",
                (principal.tenant_id, canonical),
            ).fetchone()
            if current is None:
                if expected_revision is not None:
                    raise RevisionConflict("document does not exist")
                document_id = uuid.uuid4().hex
                created_at = now
            else:
                if current["revision_id"] != expected_revision:
                    raise RevisionConflict("revision changed")
                document_id = current["document_id"]
                created_at = current["created_at"]
            revision_id = revision_id or uuid.uuid4().hex
            if len(revision_id) != 32 or any(
                c not in "0123456789abcdef" for c in revision_id
            ):
                raise ValueError("revision_id must be a lowercase UUID hex string")
            values = (
                principal.tenant_id,
                canonical,
                document_id,
                revision_id,
                digest,
                size,
                address.parent,
                source_json,
                created_at,
                now,
            )
            self.db.execute(
                """INSERT INTO revisions
                (tenant_id,uri,document_id,revision_id,package_hash,package_size,
                 parent_uri,source_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            self.db.execute(
                """INSERT INTO documents
                (tenant_id,uri,document_id,revision_id,package_hash,package_size,
                 parent_uri,source_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,uri) DO UPDATE SET
                  revision_id=excluded.revision_id, package_hash=excluded.package_hash,
                  package_size=excluded.package_size, source_json=excluded.source_json,
                  updated_at=excluded.updated_at, available=1""",
                values,
            )
            cursor = self.db.execute(
                "INSERT INTO outbox (tenant_id,uri,document_id,revision_id,operation,occurred_at) "
                "VALUES (?,?,?,?,?,?)",
                (principal.tenant_id, canonical, document_id, revision_id, "put", now),
            )
            assert cursor.lastrowid is not None
            self._queue_collection(
                principal, canonical, revision_id, cursor.lastrowid, now
            )
        return self.get_record(principal, canonical)

    def get_record(self, principal: Principal, uri: str) -> DocumentRecord:
        address = parse_uri(uri)
        authorize_uri(principal, address)
        row = self.db.execute(
            "SELECT * FROM documents WHERE tenant_id=? AND uri=?",
            (principal.tenant_id, address.value),
        ).fetchone()
        return self._verified_record(row)

    def get_revision(
        self, principal: Principal, address: NodeAddress
    ) -> DocumentRecord:
        row = self.db.execute(
            "SELECT * FROM revisions WHERE tenant_id=? AND document_id=? AND revision_id=?",
            (principal.tenant_id, address.document_id, address.revision_id),
        ).fetchone()
        if row is None:
            raise RecordMissing("revision is unavailable")
        authorize_uri(principal, parse_uri(row["uri"]))
        return self._verified_record(row)

    def get_package(self, principal: Principal, uri: str) -> bytes:
        record = self.get_record(principal, uri)
        return self.packages.get(record.package.sha256)

    def get_document(self, principal: Principal, uri: str):
        return load_package(self.get_package(principal, uri))

    def read_node(
        self, principal: Principal, address: NodeAddress, max_chars: int = 65_536
    ):
        record = self.get_revision(principal, address)
        return read_node(self.packages.get(record.package.sha256), address, max_chars)

    def delete(self, principal: Principal, uri: str, *, expected_revision: str) -> None:
        address = parse_uri(uri)
        authorize_uri(principal, address)
        with self.transaction():
            current = self.db.execute(
                "SELECT * FROM documents WHERE tenant_id=? AND uri=?",
                (principal.tenant_id, address.value),
            ).fetchone()
            self._verified_record(current)
            if current["revision_id"] != expected_revision:
                raise RevisionConflict("revision changed")
            self.db.execute(
                "DELETE FROM documents WHERE tenant_id=? AND uri=?",
                (principal.tenant_id, address.value),
            )
            cursor = self.db.execute(
                "INSERT INTO outbox (tenant_id,uri,document_id,revision_id,operation,occurred_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    principal.tenant_id,
                    address.value,
                    current["document_id"],
                    current["revision_id"],
                    "delete",
                    _now(),
                ),
            )
            assert cursor.lastrowid is not None
            self._queue_collection(
                principal,
                address.value,
                current["revision_id"],
                cursor.lastrowid,
                _now(),
            )

    def _tree(
        self, principal: Principal, uri: str, depth: int, limit: int
    ) -> list[TreeEntry]:
        if not 1 <= limit <= MAX_TREE_LIMIT or not 1 <= depth <= MAX_WALK_DEPTH:
            raise ValueError("tree depth or limit is out of bounds")
        prefix = parse_uri(uri, prefix=True)
        authorize_uri(principal, prefix)
        base = prefix.value + "/"
        rows = self.db.execute(
            "SELECT * FROM documents WHERE tenant_id=? AND available=1 AND substr(uri,1,?)=? "
            "ORDER BY uri",
            (principal.tenant_id, len(base), base),
        ).fetchall()
        entries: dict[str, TreeEntry] = {}
        for row in rows:
            try:
                record = self._verified_record(row)
            except (PackageMissing, PackageError, RecordMissing):
                continue
            suffix = record.uri[len(base) :].split("/")
            for index in range(1, min(len(suffix), depth) + 1):
                child_uri = base + "/".join(suffix[:index])
                if child_uri in entries:
                    continue
                is_document = index == len(suffix)
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
        return [entry for entry in self._tree(principal, uri, 1, limit)]

    def walk(
        self, principal: Principal, uri: str, depth: int = 3, limit: int = 100
    ) -> list[TreeEntry]:
        return self._tree(principal, uri, depth, limit)

    def events(
        self, principal: Principal, after: int = 0, limit: int = 100
    ) -> list[ChangeEvent]:
        if after < 0 or not 1 <= limit <= MAX_TREE_LIMIT:
            raise ValueError("event cursor or limit is out of bounds")
        resource_prefix = "docling://resources/"
        user_prefix = f"docling://users/{principal.tenant_id}/{principal.user_id}/"
        rows = self.db.execute(
            """SELECT * FROM outbox WHERE tenant_id=? AND sequence>?
               AND (substr(uri,1,?)=? OR substr(uri,1,?)=?)
               ORDER BY sequence LIMIT ?""",
            (
                principal.tenant_id,
                after,
                len(resource_prefix),
                resource_prefix,
                len(user_prefix),
                user_prefix,
                limit,
            ),
        ).fetchall()
        result = []
        for row in rows:
            try:
                authorize_uri(principal, parse_uri(row["uri"]))
            except AccessDenied:
                continue
            result.append(ChangeEvent(**dict(row)))
        return result

    def reconcile(self) -> dict[str, list[str]]:
        """Mark missing/corrupt revisions unavailable and remove unreferenced blobs."""
        rows = self.db.execute(
            "SELECT revision_id,package_hash FROM revisions"
        ).fetchall()
        missing = sorted(
            {
                row["package_hash"]
                for row in rows
                if not self.packages.has(row["package_hash"])
            }
        )
        referenced = {row["package_hash"] for row in rows}
        orphaned = sorted(self.packages.hashes() - referenced)
        with self.transaction():
            self.db.execute("UPDATE revisions SET available=1")
            self.db.execute("UPDATE documents SET available=1")
            for digest in missing:
                self.db.execute(
                    "UPDATE revisions SET available=0 WHERE package_hash=?", (digest,)
                )
                self.db.execute(
                    "UPDATE documents SET available=0 WHERE package_hash=?", (digest,)
                )
        for digest in orphaned:
            self.packages.path_for(digest).unlink(missing_ok=True)
        return {"missing": missing, "orphaned": orphaned}
