"""SQLite namespace, revision history, and transactional local outbox."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
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
from .profiles import validate_profile
from .retrieval import index_package, remove_index_records
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
        self.db = sqlite3.connect(
            self.root / "context.sqlite3",
            isolation_level=None,
            check_same_thread=False,
        )
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
        if version > 8:
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
        if version < 3:
            try:
                self.db.execute("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(text)")
            except sqlite3.OperationalError:
                fts_module = "fts3"
            else:
                fts_module = "fts5"
                self.db.execute("DROP TABLE temp.fts_probe")
            self.db.executescript(
                f"""BEGIN IMMEDIATE;
                CREATE TABLE retrieval_units (
                  vector_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id TEXT NOT NULL, uri TEXT NOT NULL,
                  document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                  xpath TEXT NOT NULL, parent_xpath TEXT,
                  tier INTEGER NOT NULL, page INTEGER, bbox_json TEXT,
                  text TEXT NOT NULL, updated_at TEXT NOT NULL,
                  model_id TEXT, dimensions INTEGER, normalization TEXT,
                  input_hash TEXT, index_generation TEXT, embedding BLOB,
                  UNIQUE (tenant_id, uri, revision_id, xpath)
                );
                CREATE INDEX retrieval_scope ON retrieval_units
                  (tenant_id, uri, revision_id, tier, xpath);
                CREATE VIRTUAL TABLE retrieval_fts USING {fts_module}(text);
                CREATE TABLE vector_state (
                  singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                  generation TEXT NOT NULL, model_id TEXT NOT NULL,
                  dimensions INTEGER NOT NULL, normalization TEXT NOT NULL,
                  outbox_offset INTEGER NOT NULL, snapshot_hash TEXT NOT NULL
                );
                PRAGMA user_version = 3;
                COMMIT;
                """
            )
        if version < 4:
            self.db.executescript(
                """BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS vector_members (
                  generation TEXT NOT NULL, tenant_id TEXT NOT NULL,
                  uri TEXT NOT NULL, vector_id INTEGER NOT NULL,
                  PRIMARY KEY (generation, vector_id)
                );
                CREATE INDEX IF NOT EXISTS vector_members_uri ON vector_members
                  (generation, tenant_id, uri);
                PRAGMA user_version = 4;
                COMMIT;
                """
            )
        if version < 5:
            columns = {
                row["name"]
                for row in self.db.execute("PRAGMA table_info(retrieval_units)")
            }
            if "asset_path" not in columns:
                self.db.execute(
                    "ALTER TABLE retrieval_units ADD COLUMN asset_path TEXT"
                )
            self.db.execute("PRAGMA user_version = 5")
        if version < 6:
            columns = {
                row["name"]
                for row in self.db.execute("PRAGMA table_info(retrieval_units)")
            }
            if "section_xpath" not in columns:
                self.db.execute(
                    "ALTER TABLE retrieval_units ADD COLUMN section_xpath TEXT"
                )
            self.db.execute("PRAGMA user_version = 6")
        if version < 7:
            self.db.executescript(
                """BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS memory_index (
                  tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, uri TEXT NOT NULL,
                  memory_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT,
                  claim TEXT NOT NULL, claim_key TEXT NOT NULL,
                  status TEXT NOT NULL, confidence REAL NOT NULL,
                  revision_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY (tenant_id, uri)
                );
                CREATE INDEX IF NOT EXISTS memory_owner_status ON memory_index
                  (tenant_id, user_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS memory_claim_key ON memory_index
                  (tenant_id, user_id, kind, claim_key);
                CREATE TABLE IF NOT EXISTS memory_edges (
                  tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                  memory_uri TEXT NOT NULL, source_uri TEXT NOT NULL,
                  source_document_id TEXT NOT NULL,
                  source_revision_id TEXT NOT NULL, source_xpath TEXT NOT NULL,
                  PRIMARY KEY (tenant_id, memory_uri, source_uri,
                               source_revision_id, source_xpath)
                );
                CREATE INDEX IF NOT EXISTS memory_source_edges ON memory_edges
                  (tenant_id, source_uri, source_revision_id);
                CREATE TABLE IF NOT EXISTS recall_ledger (
                  tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                  session_uri TEXT NOT NULL, memory_id TEXT NOT NULL,
                  revision_id TEXT NOT NULL, delivered_at TEXT NOT NULL,
                  PRIMARY KEY (tenant_id, user_id, session_uri, memory_id, revision_id)
                );
                CREATE TABLE IF NOT EXISTS memory_compile_jobs (
                  job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  user_id TEXT NOT NULL, session_uri TEXT NOT NULL,
                  session_document_id TEXT NOT NULL,
                  session_revision_id TEXT NOT NULL,
                  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  retry_at TEXT NOT NULL, lease_until TEXT, last_error TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE (tenant_id, session_uri, session_revision_id)
                );
                CREATE INDEX IF NOT EXISTS memory_compile_due ON memory_compile_jobs
                  (status, retry_at, lease_until);
                PRAGMA user_version = 7;
                COMMIT;
                """
            )
        if version < 8:
            self.db.executescript(
                """BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS ingest_jobs (
                  job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  user_id TEXT NOT NULL, uri TEXT NOT NULL, filename TEXT NOT NULL,
                  source BLOB NOT NULL, force INTEGER NOT NULL,
                  status TEXT NOT NULL, revision_id TEXT, error TEXT,
                  lease_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ingest_jobs_due ON ingest_jobs (status, lease_until);
                PRAGMA user_version = 8;
                COMMIT;
                """
            )
        self._backfill_retrieval()

    def _backfill_retrieval(self) -> None:
        rows = self.db.execute(
            """SELECT d.* FROM documents d WHERE d.available=1 AND NOT EXISTS (
                 SELECT 1 FROM retrieval_units r WHERE r.tenant_id=d.tenant_id
                 AND r.uri=d.uri AND r.revision_id=d.revision_id)"""
        ).fetchall()
        for row in rows:
            try:
                package = self.packages.get(row["package_hash"])
                with self.transaction():
                    index_package(self.db, self._record(row), package)
                    self.db.execute("UPDATE vector_state SET outbox_offset=-1")
            except (PackageMissing, PackageError):
                continue

    def index_status(self, principal: Principal) -> dict[str, int | str | None]:
        """Report the current, authorized retrieval projection."""
        rows = self.db.execute(
            "SELECT uri,revision_id FROM documents WHERE tenant_id=? AND available=1",
            (principal.tenant_id,),
        ).fetchall()
        visible = [row for row in rows if self._can_access(principal, row["uri"])]
        indexed = units = embedded = 0
        for row in visible:
            count, with_embeddings = self.db.execute(
                """SELECT COUNT(*),COUNT(embedding) FROM retrieval_units
                   WHERE tenant_id=? AND uri=? AND revision_id=?""",
                (principal.tenant_id, row["uri"], row["revision_id"]),
            ).fetchone()
            indexed += count > 0
            units += count
            embedded += with_embeddings
        state = self.db.execute(
            "SELECT model_id,outbox_offset FROM vector_state WHERE singleton=1"
        ).fetchone()
        offset = self.db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM outbox"
        ).fetchone()[0]
        return {
            "documents": len(visible),
            "indexed_documents": indexed,
            "indexed_nodes": units,
            "embedded_nodes": embedded,
            "vector_model": state["model_id"] if state else None,
            "vector_snapshot_current": bool(state and state["outbox_offset"] == offset),
        }

    def rebuild_index(
        self,
        principal: Principal,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, int | str | None]:
        """Recreate authorized retrieval rows from stored DCLX packages."""
        rows = self.db.execute(
            "SELECT * FROM documents WHERE tenant_id=? AND available=1 ORDER BY uri",
            (principal.tenant_id,),
        ).fetchall()
        visible = [row for row in rows if self._can_access(principal, row["uri"])]
        rebuilt = 0
        if progress is not None:
            progress(0, len(visible))
        for row in visible:
            package = self.packages.get(row["package_hash"])
            document = load_package(package)
            with self.transaction():
                index_package(self.db, self._record(row), package, document=document)
                self.db.execute("UPDATE vector_state SET outbox_offset=-1")
            rebuilt += 1
            if progress is not None:
                progress(rebuilt, len(visible))
        return {"rebuilt_documents": rebuilt, **self.index_status(principal)}

    def require_exclusive_vector_scope(self, principal: Principal) -> None:
        """Prevent a store-wide vector build from reading another principal's data."""
        rows = self.db.execute(
            "SELECT tenant_id,uri FROM documents WHERE available=1"
        ).fetchall()
        if any(
            row["tenant_id"] != principal.tenant_id
            or not self._can_access(principal, row["uri"])
            for row in rows
        ):
            raise PermissionError(
                "vector indexing needs a store containing only documents "
                "accessible to the selected tenant and user"
            )

    @staticmethod
    def _can_access(principal: Principal, uri: str) -> bool:
        try:
            authorize_uri(principal, parse_uri(uri))
        except AccessDenied:
            return False
        return True

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
        indexed = load_package(package)
        validate_profile(canonical, indexed)
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
            index_package(
                self.db,
                DocumentRecord(
                    canonical,
                    principal.tenant_id,
                    document_id,
                    revision_id,
                    PackageRef(digest, size),
                    address.parent,
                    source or {},
                    created_at,
                    now,
                ),
                package,
                document=indexed,
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
        dependents = self.db.execute(
            """SELECT DISTINCT user_id FROM memory_edges
               WHERE tenant_id=? AND source_uri=?""",
            (principal.tenant_id, address.value),
        ).fetchall()
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
            remove_index_records(self.db, principal.tenant_id, address.value)
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
        if dependents:
            from .durable_memory import MemoryService

            memories = MemoryService(self)
            for dependent in dependents:
                memories.reconcile_sources(
                    Principal(principal.tenant_id, dependent["user_id"])
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
