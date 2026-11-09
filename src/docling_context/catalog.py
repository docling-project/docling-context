"""Shared resource and project catalog in the local SQLite store."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self
from xml.sax.saxutils import escape

from .doclang_authoring import (
    DEFAULT_SKILL_NAME,
    DEFAULT_SKILL_SUMMARY,
    DEFAULT_SKILL_TEXT,
    DEFAULT_SKILL_URI,
)
from .models import DocumentRecord, NodeAddress, Principal
from .package import load_package
from .profiles import validate_descriptor
from .uri import parse_uri

if TYPE_CHECKING:
    from .local import LocalContextStore

RESOURCE_TYPES = ("library", "memories", "skills", "concepts", "knowledge")
CATALOG_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    title: str
    description_uri: str | None
    abstract_uri: str | None
    overview_uri: str | None


class Catalog:
    def __init__(self, store: LocalContextStore):
        self.store = store
        self.db = store.db

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        pass  # The caller owns the LocalContextStore connection.

    def setup(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS dc_schema_version (
                 version INTEGER PRIMARY KEY CHECK (version = 1))""",
            """CREATE TABLE IF NOT EXISTS dc_projects (
                 tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                 title TEXT NOT NULL, description_uri TEXT, abstract_uri TEXT,
                 overview_uri TEXT, owner_id TEXT NOT NULL,
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (tenant_id, project_id))""",
            """CREATE TABLE IF NOT EXISTS dc_resources (
                 tenant_id TEXT NOT NULL, resource_type TEXT NOT NULL
                   CHECK (resource_type IN
                     ('library','memories','skills','concepts','knowledge')),
                 resource_id TEXT NOT NULL, uri TEXT NOT NULL,
                 source_sha256 TEXT, current_revision TEXT NOT NULL,
                 package_hash TEXT NOT NULL, owner_id TEXT NOT NULL,
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (tenant_id, resource_type, resource_id),
                 UNIQUE (tenant_id, uri),
                 FOREIGN KEY (tenant_id, uri) REFERENCES documents (tenant_id, uri))""",
            """CREATE UNIQUE INDEX IF NOT EXISTS dc_library_source_hash
                 ON dc_resources (tenant_id, source_sha256)
                 WHERE resource_type='library' AND source_sha256 IS NOT NULL""",
            """CREATE TABLE IF NOT EXISTS dc_resource_revisions (
                 tenant_id TEXT NOT NULL, resource_type TEXT NOT NULL,
                 resource_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                 package_hash TEXT NOT NULL, source_sha256 TEXT,
                 provenance TEXT NOT NULL, created_at TEXT NOT NULL
                   DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (tenant_id, revision_id),
                 FOREIGN KEY (tenant_id, resource_type, resource_id)
                   REFERENCES dc_resources (tenant_id, resource_type, resource_id)
                   ON DELETE CASCADE,
                 FOREIGN KEY (revision_id) REFERENCES revisions (revision_id))""",
            """CREATE TABLE IF NOT EXISTS dc_project_resource_links (
                 tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                 resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
                 relation TEXT NOT NULL DEFAULT 'member',
                 created_by TEXT NOT NULL,
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (tenant_id, project_id, resource_type, resource_id),
                 FOREIGN KEY (tenant_id, project_id)
                   REFERENCES dc_projects (tenant_id, project_id) ON DELETE CASCADE,
                 FOREIGN KEY (tenant_id, resource_type, resource_id)
                   REFERENCES dc_resources (tenant_id, resource_type, resource_id)
                   ON DELETE CASCADE)""",
            """CREATE INDEX IF NOT EXISTS dc_links_by_resource
                 ON dc_project_resource_links
                 (tenant_id, resource_type, resource_id, project_id)""",
            """CREATE TABLE IF NOT EXISTS dc_project_sessions (
                 tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                 session_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                 agent TEXT, status TEXT NOT NULL DEFAULT 'open', revision_id TEXT,
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (tenant_id, project_id, session_id),
                 FOREIGN KEY (tenant_id, project_id)
                   REFERENCES dc_projects (tenant_id, project_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS dc_project_documents (
                 tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                 document_kind TEXT NOT NULL CHECK
                   (document_kind IN ('description','abstract','overview')),
                 uri TEXT NOT NULL, revision_id TEXT NOT NULL,
                 package_hash TEXT NOT NULL,
                 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (tenant_id, project_id, document_kind),
                 FOREIGN KEY (tenant_id, project_id)
                   REFERENCES dc_projects (tenant_id, project_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS dc_resource_links (
                 tenant_id TEXT NOT NULL, source_type TEXT NOT NULL,
                 source_id TEXT NOT NULL, target_type TEXT NOT NULL,
                 target_id TEXT NOT NULL, relation TEXT NOT NULL,
                 source_revision TEXT, source_xpath TEXT,
                 PRIMARY KEY (tenant_id, source_type, source_id,
                              target_type, target_id, relation),
                 FOREIGN KEY (tenant_id, source_type, source_id)
                   REFERENCES dc_resources (tenant_id, resource_type, resource_id)
                   ON DELETE CASCADE,
                 FOREIGN KEY (tenant_id, target_type, target_id)
                   REFERENCES dc_resources (tenant_id, resource_type, resource_id)
                   ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS dc_knowledge_datasets (
                 tenant_id TEXT NOT NULL, knowledge_id TEXT NOT NULL,
                 schema_json TEXT NOT NULL, current_version INTEGER NOT NULL,
                 PRIMARY KEY (tenant_id, knowledge_id))""",
            """CREATE TABLE IF NOT EXISTS dc_knowledge_facts (
                 tenant_id TEXT NOT NULL, knowledge_id TEXT NOT NULL,
                 fact_id TEXT NOT NULL, entity_type TEXT NOT NULL,
                 entity_id TEXT NOT NULL, property TEXT NOT NULL,
                 value_json TEXT NOT NULL, source_uri TEXT NOT NULL,
                 source_revision TEXT NOT NULL, source_xpath TEXT NOT NULL,
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (tenant_id, knowledge_id, fact_id),
                 FOREIGN KEY (tenant_id, knowledge_id)
                   REFERENCES dc_knowledge_datasets (tenant_id, knowledge_id)
                   ON DELETE CASCADE)""",
            """CREATE INDEX IF NOT EXISTS dc_facts_by_entity
                 ON dc_knowledge_facts
                 (tenant_id, knowledge_id, entity_type, entity_id, property)""",
            """CREATE TABLE IF NOT EXISTS dc_project_context_jobs (
                 tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                 kind TEXT NOT NULL CHECK (kind IN ('abstract','overview')),
                 status TEXT NOT NULL DEFAULT 'queued',
                 generation INTEGER NOT NULL DEFAULT 1,
                 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (tenant_id, project_id, kind),
                 FOREIGN KEY (tenant_id, project_id)
                   REFERENCES dc_projects (tenant_id, project_id) ON DELETE CASCADE)""",
        )
        with self.store.transaction():
            for statement in statements:
                self.db.execute(statement)
            columns = {
                row["name"]
                for row in self.db.execute("PRAGMA table_info(dc_project_sessions)")
            }
            if "agent" not in columns:
                self.db.execute("ALTER TABLE dc_project_sessions ADD COLUMN agent TEXT")
            self.db.execute(
                "INSERT OR IGNORE INTO dc_schema_version (version) VALUES (?)",
                (CATALOG_SCHEMA_VERSION,),
            )
        self._ensure_existing_project_skills()

    def _ensure_existing_project_skills(self) -> None:
        rows = self.db.execute(
            """SELECT p.tenant_id,p.project_id,p.owner_id FROM dc_projects p
               LEFT JOIN dc_project_resource_links l
                 ON l.tenant_id=p.tenant_id AND l.project_id=p.project_id
                AND l.resource_type='skills' AND l.resource_id=?
               WHERE l.project_id IS NULL""",
            (DEFAULT_SKILL_URI.rsplit("/", 1)[-1],),
        ).fetchall()
        for row in rows:
            principal = Principal(row["tenant_id"], row["owner_id"])
            with self.store.transaction():
                self._ensure_default_skill(principal)
                self.link(principal, row["project_id"], DEFAULT_SKILL_URI)

    @staticmethod
    def _project_id(value: str) -> str:
        address = parse_uri(f"docling://projects/{value}")
        if len(address.segments) != 1:
            raise ValueError("project ID must be one URI segment")
        return address.segments[0]

    @staticmethod
    def _resource_parts(uri: str) -> tuple[str, str]:
        address = parse_uri(uri)
        if (
            address.namespace != "resources"
            or len(address.segments) != 2
            or address.segments[0] not in RESOURCE_TYPES
        ):
            raise ValueError("expected one flat resource URI")
        return address.segments[0], address.segments[1]

    def _queue_context(self, principal: Principal, project_id: str) -> None:
        for kind in ("abstract", "overview"):
            self.db.execute(
                """INSERT INTO dc_project_context_jobs
                   (tenant_id,project_id,kind,status) VALUES (?,?,?,'queued')
                   ON CONFLICT (tenant_id,project_id,kind)
                   DO UPDATE SET status='queued',generation=generation+1,
                     updated_at=CURRENT_TIMESTAMP""",
                (principal.tenant_id, project_id, kind),
            )

    def create_project(
        self, principal: Principal, project_id: str, title: str
    ) -> Project:
        project_id = self._project_id(project_id)
        if not title.strip():
            raise ValueError("project title is required")
        with self.store.transaction():
            self._ensure_default_skill(principal)
            self.db.execute(
                """INSERT INTO dc_projects
                   (tenant_id,project_id,title,owner_id) VALUES (?,?,?,?)""",
                (principal.tenant_id, project_id, title.strip(), principal.user_id),
            )
            self.link(principal, project_id, DEFAULT_SKILL_URI)
        return Project(project_id, title.strip(), None, None, None)

    def _ensure_default_skill(self, principal: Principal) -> None:
        row = self.db.execute(
            """SELECT 1 FROM dc_resources WHERE tenant_id=? AND uri=?""",
            (principal.tenant_id, DEFAULT_SKILL_URI),
        ).fetchone()
        if row is not None:
            return
        from .ingestion import Ingestor

        xml = f"<doclang><text>{escape(DEFAULT_SKILL_TEXT)}</text></doclang>"
        record = Ingestor(self.store).add_resource(
            principal,
            DEFAULT_SKILL_URI,
            xml.encode("utf-8"),
            filename="doclang-authoring.dclg",
            profile_metadata={
                "profile": "skill-v1",
                "skill_id": DEFAULT_SKILL_URI.rsplit("/", 1)[-1],
                "name": DEFAULT_SKILL_NAME,
                "summary": DEFAULT_SKILL_SUMMARY,
            },
        )
        self.register_resource(principal, record, "skills")

    def get_project(self, principal: Principal, project_id: str) -> Project:
        project_id = self._project_id(project_id)
        row = self.db.execute(
            """SELECT project_id,title,description_uri,abstract_uri,overview_uri
               FROM dc_projects WHERE tenant_id=? AND project_id=?""",
            (principal.tenant_id, project_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"project does not exist: {project_id}")
        return Project(**dict(row))

    def set_project_document(
        self,
        principal: Principal,
        project_id: str,
        kind: str,
        record: DocumentRecord,
        *,
        generated: bool = False,
    ) -> None:
        if kind not in {"description", "abstract", "overview"}:
            raise ValueError("invalid project document kind")
        project_id = self._project_id(project_id)
        if record.uri != f"docling://projects/{project_id}/.{kind}.dclx":
            raise ValueError("project document URI does not match project and kind")
        self.get_project(principal, project_id)
        column = {
            "description": "description_uri",
            "abstract": "abstract_uri",
            "overview": "overview_uri",
        }[kind]
        with self.store.transaction():
            self.db.execute(
                """INSERT INTO dc_project_documents
                   (tenant_id,project_id,document_kind,uri,revision_id,package_hash)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT (tenant_id,project_id,document_kind)
                   DO UPDATE SET revision_id=excluded.revision_id,
                     package_hash=excluded.package_hash,updated_at=CURRENT_TIMESTAMP""",
                (
                    principal.tenant_id,
                    project_id,
                    kind,
                    record.uri,
                    record.revision_id,
                    record.package.sha256,
                ),
            )
            self.db.execute(
                f"UPDATE dc_projects SET {column}=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE tenant_id=? AND project_id=?",
                (record.uri, principal.tenant_id, project_id),
            )
            if kind == "description":
                self._queue_context(principal, project_id)
            elif not generated:
                self.db.execute(
                    """UPDATE dc_project_context_jobs SET status='completed',
                       last_error=NULL,updated_at=CURRENT_TIMESTAMP
                       WHERE tenant_id=? AND project_id=? AND kind=?""",
                    (principal.tenant_id, project_id, kind),
                )

    def refresh_project(self, principal: Principal, project_id: str) -> None:
        project_id = self._project_id(project_id)
        self.get_project(principal, project_id)
        with self.store.transaction():
            self._queue_context(principal, project_id)

    def context_jobs(
        self, principal: Principal, project_id: str
    ) -> list[dict[str, Any]]:
        project_id = self._project_id(project_id)
        self.get_project(principal, project_id)
        rows = self.db.execute(
            """SELECT kind,status,generation,attempts,last_error,updated_at
               FROM dc_project_context_jobs WHERE tenant_id=? AND project_id=?
               ORDER BY kind""",
            (principal.tenant_id, project_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def register_session(
        self,
        principal: Principal,
        project_id: str,
        session_id: str,
        *,
        status: str = "open",
        revision_id: str | None = None,
        agent: str | None = None,
    ) -> None:
        project_id = self._project_id(project_id)
        if status not in {"open", "closed"}:
            raise ValueError("invalid session status")
        parse_uri(f"docling://projects/{project_id}/sessions/{session_id}")
        self.get_project(principal, project_id)
        with self.store.transaction():
            self.db.execute(
                """INSERT INTO dc_project_sessions
                   (tenant_id,project_id,session_id,owner_id,agent,status,revision_id)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (tenant_id,project_id,session_id)
                   DO UPDATE SET status=excluded.status,
                     agent=COALESCE(dc_project_sessions.agent,excluded.agent),
                     revision_id=COALESCE(excluded.revision_id,dc_project_sessions.revision_id)""",
                (
                    principal.tenant_id,
                    project_id,
                    session_id,
                    principal.user_id,
                    agent,
                    status,
                    revision_id,
                ),
            )

    def project_sessions(
        self, principal: Principal, project_id: str
    ) -> list[dict[str, str]]:
        self.get_project(principal, project_id)
        rows = self.db.execute(
            """SELECT session_id,agent,status,owner_id,created_at FROM dc_project_sessions
               WHERE tenant_id=? AND project_id=? ORDER BY session_id""",
            (principal.tenant_id, project_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_source(self, principal: Principal, source_sha256: str) -> str | None:
        row = self.db.execute(
            """SELECT uri FROM dc_resources WHERE tenant_id=?
               AND resource_type='library' AND source_sha256=?""",
            (principal.tenant_id, source_sha256),
        ).fetchone()
        return row["uri"] if row else None

    def register_resource(
        self,
        principal: Principal,
        record: DocumentRecord,
        resource_type: str = "library",
    ) -> None:
        actual_type, resource_id = self._resource_parts(record.uri)
        if actual_type != resource_type:
            raise ValueError("resource URI must be flat and match its type")
        source_sha256 = record.source.get("source_sha256")
        with self.store.transaction():
            self.db.execute(
                """INSERT INTO dc_resources
                   (tenant_id,resource_type,resource_id,uri,source_sha256,
                    current_revision,package_hash,owner_id)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT (tenant_id,resource_type,resource_id)
                   DO UPDATE SET current_revision=excluded.current_revision,
                     package_hash=excluded.package_hash,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    principal.tenant_id,
                    resource_type,
                    resource_id,
                    record.uri,
                    source_sha256,
                    record.revision_id,
                    record.package.sha256,
                    principal.user_id,
                ),
            )
            self.db.execute(
                """INSERT OR IGNORE INTO dc_resource_revisions
                   (tenant_id,resource_type,resource_id,revision_id,package_hash,
                    source_sha256,provenance) VALUES (?,?,?,?,?,?,?)""",
                (
                    principal.tenant_id,
                    resource_type,
                    resource_id,
                    record.revision_id,
                    record.package.sha256,
                    source_sha256,
                    json.dumps(record.source, sort_keys=True),
                ),
            )

    def link(self, principal: Principal, project_id: str, uri: str) -> None:
        project_id = self._project_id(project_id)
        resource_type, resource_id = self._resource_parts(uri)
        self.get_project(principal, project_id)
        with self.store.transaction():
            self.db.execute(
                """INSERT OR IGNORE INTO dc_project_resource_links
                   (tenant_id,project_id,resource_type,resource_id,created_by)
                   VALUES (?,?,?,?,?)""",
                (
                    principal.tenant_id,
                    project_id,
                    resource_type,
                    resource_id,
                    principal.user_id,
                ),
            )
            self._queue_context(principal, project_id)

    def unlink(self, principal: Principal, project_id: str, uri: str) -> None:
        project_id = self._project_id(project_id)
        resource_type, resource_id = self._resource_parts(uri)
        if uri == DEFAULT_SKILL_URI:
            raise ValueError(
                "the default DocLang authoring skill is required by every project"
            )
        self.get_project(principal, project_id)
        with self.store.transaction():
            self.db.execute(
                """DELETE FROM dc_project_resource_links WHERE tenant_id=?
                   AND project_id=? AND resource_type=? AND resource_id=?""",
                (principal.tenant_id, project_id, resource_type, resource_id),
            )
            self._queue_context(principal, project_id)

    def project_resources(
        self, principal: Principal, project_id: str
    ) -> list[dict[str, str]]:
        self.get_project(principal, project_id)
        rows = self.db.execute(
            """SELECT r.resource_type,r.resource_id,r.uri
               FROM dc_project_resource_links l JOIN dc_resources r
                 USING (tenant_id,resource_type,resource_id)
               WHERE l.tenant_id=? AND l.project_id=?
               ORDER BY r.resource_type,r.resource_id""",
            (principal.tenant_id, project_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def resource_projects(self, principal: Principal, uri: str) -> list[dict[str, str]]:
        resource_type, resource_id = self._resource_parts(uri)
        rows = self.db.execute(
            """SELECT p.project_id,p.title FROM dc_project_resource_links l
               JOIN dc_projects p ON p.tenant_id=l.tenant_id
                 AND p.project_id=l.project_id
               WHERE l.tenant_id=? AND l.resource_type=? AND l.resource_id=?
               ORDER BY lower(p.title),p.project_id""",
            (principal.tenant_id, resource_type, resource_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def project_resource_uris(
        self, principal: Principal, project_id: str, *, max_items: int = 10_000
    ) -> tuple[str, ...]:
        self.get_project(principal, project_id)
        rows = self.db.execute(
            """SELECT r.uri FROM dc_project_resource_links l
               JOIN dc_resources r USING (tenant_id,resource_type,resource_id)
               WHERE l.tenant_id=? AND l.project_id=?
               ORDER BY r.uri LIMIT ?""",
            (principal.tenant_id, project_id, max_items + 1),
        ).fetchall()
        if len(rows) > max_items:
            raise ValueError("project has too many linked resources for one search")
        return tuple(row["uri"] for row in rows)

    def link_resources(
        self,
        principal: Principal,
        source_uri: str,
        target_uri: str,
        relation: str,
        *,
        source_revision: str | None = None,
        source_xpath: str | None = None,
    ) -> None:
        if not relation or len(relation) > 80:
            raise ValueError("relation must be 1 to 80 characters")
        if source_xpath is not None and (
            not source_xpath.startswith("/doclang[1]") or len(source_xpath) > 2048
        ):
            raise ValueError("invalid source XPath")
        source_type, source_id = self._resource_parts(source_uri)
        target_type, target_id = self._resource_parts(target_uri)
        with self.store.transaction():
            self.db.execute(
                """INSERT INTO dc_resource_links
                   (tenant_id,source_type,source_id,target_type,target_id,
                    relation,source_revision,source_xpath)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT (tenant_id,source_type,source_id,
                                target_type,target_id,relation)
                   DO UPDATE SET source_revision=excluded.source_revision,
                     source_xpath=excluded.source_xpath""",
                (
                    principal.tenant_id,
                    source_type,
                    source_id,
                    target_type,
                    target_id,
                    relation,
                    source_revision,
                    source_xpath,
                ),
            )

    def resource_links(
        self, principal: Principal, uri: str
    ) -> list[dict[str, str | None]]:
        resource_type, resource_id = self._resource_parts(uri)
        rows = self.db.execute(
            """SELECT s.uri AS source_uri,t.uri AS target_uri,l.relation,
                      l.source_revision,l.source_xpath
               FROM dc_resource_links l
               JOIN dc_resources s ON s.tenant_id=l.tenant_id
                 AND s.resource_type=l.source_type AND s.resource_id=l.source_id
               JOIN dc_resources t ON t.tenant_id=l.tenant_id
                 AND t.resource_type=l.target_type AND t.resource_id=l.target_id
               WHERE l.tenant_id=? AND
                 ((l.source_type=? AND l.source_id=?) OR
                  (l.target_type=? AND l.target_id=?))
               ORDER BY s.uri,t.uri,l.relation""",
            (
                principal.tenant_id,
                resource_type,
                resource_id,
                resource_type,
                resource_id,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def overview(
        self, principal: Principal, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("overview limit or offset is out of bounds")
        rows = self.db.execute(
            """SELECT p.project_id,p.title,
                 count(CASE WHEN l.resource_type='library' THEN 1 END) AS documents,
                 count(CASE WHEN l.resource_type='memories' THEN 1 END) AS memories,
                 count(CASE WHEN l.resource_type='skills' THEN 1 END) AS skills,
                 count(CASE WHEN l.resource_type='concepts' THEN 1 END) AS concepts,
                 count(CASE WHEN l.resource_type='knowledge' THEN 1 END) AS knowledge,
                 (SELECT count(*) FROM dc_project_sessions s
                  WHERE s.tenant_id=p.tenant_id AND s.project_id=p.project_id) AS sessions
               FROM (SELECT * FROM dc_projects WHERE tenant_id=?
                     ORDER BY lower(title),project_id LIMIT ? OFFSET ?) p
               LEFT JOIN dc_project_resource_links l
                 ON l.tenant_id=p.tenant_id AND l.project_id=p.project_id
               GROUP BY p.tenant_id,p.project_id,p.title
               ORDER BY lower(p.title),p.project_id""",
            (principal.tenant_id, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def resource_uri(kind: str, value: str) -> str:
        uri = (
            value
            if value.startswith("docling://")
            else f"docling://resources/{kind}/{value}"
        )
        address = parse_uri(uri)
        if (
            address.namespace != "resources"
            or address.segments[0] != kind
            or len(address.segments) != 2
        ):
            raise ValueError(f"expected a {kind} ID or URI")
        return uri

    def resources(
        self, principal: Principal, kind: str, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        if project_id:
            return [
                row
                for row in self.project_resources(principal, project_id)
                if row["resource_type"] == kind
            ]
        rows = self.db.execute(
            "SELECT resource_type,resource_id,uri,current_revision AS revision_id FROM dc_resources WHERE tenant_id=? AND resource_type=? ORDER BY resource_id",
            (principal.tenant_id, kind),
        ).fetchall()
        return [dict(row) for row in rows]

    def knowledge_dataset(self, principal: Principal, uri: str) -> dict[str, Any]:
        uri = self.resource_uri("knowledge", uri)
        self.store.get_record(principal, uri)
        row = self.db.execute(
            "SELECT schema_json FROM dc_knowledge_datasets WHERE tenant_id=? AND knowledge_id=?",
            (principal.tenant_id, uri.rsplit("/", 1)[-1]),
        ).fetchone()
        if row is None:
            raise ValueError("knowledge dataset is not registered")
        data = json.loads(row["schema_json"])
        if "concept_uri" not in data:
            raise ValueError(
                "knowledge dataset has no pinned concept; recreate it with knowledge create --concept"
            )
        return {"uri": uri, **data}

    def register_knowledge_dataset(
        self,
        principal: Principal,
        uri: str,
        concept_uri: str,
        concept_revision: str | None = None,
    ) -> None:
        uri = self.resource_uri("knowledge", uri)
        concept_uri = self.resource_uri("concepts", concept_uri)
        concept = self.store.get_record(principal, concept_uri)
        if concept_revision:
            concept = self.store.get_revision(
                principal,
                NodeAddress(concept.document_id, concept_revision, "/doclang[1]"),
            )
        metadata = validate_descriptor(
            concept_uri, load_package(self.store.packages.get(concept.package.sha256))
        )
        data = {
            "concept_uri": concept_uri,
            "concept_revision": concept.revision_id,
            "schema": metadata["schema"],
        }
        encoded = json.dumps(data, sort_keys=True)
        previous = self.db.execute(
            "SELECT schema_json FROM dc_knowledge_datasets WHERE tenant_id=? AND knowledge_id=?",
            (principal.tenant_id, uri.rsplit("/", 1)[-1]),
        ).fetchone()
        if previous and previous[0] != encoded:
            raise ValueError("a knowledge dataset's pinned concept cannot be changed")
        with self.store.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO dc_knowledge_datasets (tenant_id,knowledge_id,schema_json,current_version) VALUES (?,?,?,1)",
                (principal.tenant_id, uri.rsplit("/", 1)[-1], encoded),
            )
            self.link_resources(principal, uri, concept_uri, "defined-by")

    def add_fact(
        self,
        principal: Principal,
        knowledge_uri: str,
        *,
        entity_type: str,
        entity_id: str,
        property: str,
        value: Any,
        source_uri: str,
        source_revision: str,
        source_xpath: str,
        unit: str | None = None,
    ) -> str:
        resource_type, knowledge_id = self._resource_parts(knowledge_uri)
        if resource_type != "knowledge":
            raise ValueError("expected a knowledge resource")
        schema = self.knowledge_dataset(principal, knowledge_uri)["schema"]
        definition = schema["properties"].get(property, {})
        kind = definition.get("type")
        if entity_type not in schema["entity_types"]:
            raise ValueError("entity type does not match the pinned concept schema")
        if unit != definition.get("unit"):
            raise ValueError("fact unit does not match the pinned concept schema")
        valid = {
            "text": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "json": True,
        }.get(kind, False)
        if not valid or not entity_type.strip() or not entity_id.strip():
            raise ValueError("fact does not match the knowledge schema")
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) > 16_384:
            raise ValueError("fact value is too large")
        source = self.store.get_record(principal, source_uri)
        revision = self.store.get_revision(
            principal, NodeAddress(source.document_id, source_revision, source_xpath)
        )
        if revision.uri != source_uri:
            raise ValueError("fact citation URI differs from its revision")
        self.store.read_node(
            principal, NodeAddress(source.document_id, source_revision, source_xpath)
        )
        fact_id = uuid.uuid4().hex
        with self.store.transaction():
            self.db.execute(
                """INSERT INTO dc_knowledge_facts
                   (tenant_id,knowledge_id,fact_id,entity_type,entity_id,property,
                    value_json,source_uri,source_revision,source_xpath)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    principal.tenant_id,
                    knowledge_id,
                    fact_id,
                    entity_type,
                    entity_id,
                    property,
                    encoded,
                    source_uri,
                    source_revision,
                    source_xpath,
                ),
            )
        return fact_id

    def knowledge_facts(
        self, principal: Principal, knowledge_uri: str
    ) -> list[dict[str, Any]]:
        resource_type, knowledge_id = self._resource_parts(knowledge_uri)
        if resource_type != "knowledge":
            raise ValueError("expected a knowledge resource")
        rows = self.db.execute(
            """SELECT fact_id,entity_type,entity_id,property,value_json,
                      source_uri,source_revision,source_xpath
               FROM dc_knowledge_facts WHERE tenant_id=? AND knowledge_id=?
               ORDER BY entity_type,entity_id,property,fact_id""",
            (principal.tenant_id, knowledge_id),
        ).fetchall()
        return [{**dict(row), "value": json.loads(row["value_json"])} for row in rows]


def new_document_uri() -> str:
    return f"docling://resources/library/{uuid.uuid4().hex}"
