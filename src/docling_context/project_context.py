"""Durable, deterministic project abstract and overview generation."""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape

from .catalog import Catalog
from .ingestion import Ingestor
from .local import LocalContextStore
from .models import Principal
from .package import bounded_nodes


@dataclass(frozen=True, slots=True)
class ProjectContextJob:
    project_id: str
    kind: str
    status: str
    generation: int
    attempts: int
    last_error: str | None


class ProjectContextWorker:
    def __init__(self, store: LocalContextStore, *, max_attempts: int = 3):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.catalog = Catalog(store)
        self.max_attempts = max_attempts

    def _xml(self, principal: Principal, project_id: str, kind: str) -> bytes:
        project = self.catalog.get_project(principal, project_id)
        description = ""
        if project.description_uri:
            document = self.store.get_document(principal, project.description_uri)
            description = " ".join(
                str(node["text"])
                for node in bounded_nodes(document, limit=100, text_bytes=256)
                if node["text"]
            )[:1000]
        resources = self.catalog.project_resources(principal, project_id)
        counts: dict[str, int] = {}
        for resource in resources:
            counts[resource["resource_type"]] = (
                counts.get(resource["resource_type"], 0) + 1
            )
        count_text = ", ".join(
            f"{counts.get(name, 0)} {name}"
            for name in ("library", "memories", "skills", "concepts", "knowledge")
        )
        if kind == "abstract":
            body = f"{project.title}. {description} Linked resources: {count_text}."
            return f"<doclang><text>{escape(body)}</text></doclang>".encode()
        parts = [
            f"<heading>{escape(project.title)}</heading>",
            f"<text>{escape(description or 'Project overview')}</text>",
            f"<text>Linked resources: {escape(count_text)}.</text>",
        ]
        for resource in resources[:256]:
            label = f"{resource['resource_type']}/{resource['resource_id']}"
            parts.append(f"<heading>{escape(label)}</heading>")
            parts.append(f"<text>{escape(resource['uri'])}</text>")
        return f"<doclang>{''.join(parts)}</doclang>".encode()

    def run_once(self) -> ProjectContextJob | None:
        with self.store.transaction():
            row = self.store.db.execute(
                """SELECT j.tenant_id,j.project_id,j.kind,j.generation,j.attempts,
                          p.owner_id
                   FROM dc_project_context_jobs j JOIN dc_projects p
                     USING (tenant_id,project_id)
                   WHERE j.status='queued'
                   ORDER BY j.updated_at,j.project_id,j.kind LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            self.store.db.execute(
                """UPDATE dc_project_context_jobs SET status='running',
                   attempts=attempts+1,updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND project_id=? AND kind=?""",
                (row["tenant_id"], row["project_id"], row["kind"]),
            )
        principal = Principal(row["tenant_id"], row["owner_id"])
        try:
            uri = f"docling://projects/{row['project_id']}/.{row['kind']}.dclx"
            record = Ingestor(self.store).add_resource(
                principal,
                uri,
                self._xml(principal, row["project_id"], row["kind"]),
                filename="project-context.dclg",
            )
            self.catalog.set_project_document(
                principal, row["project_id"], row["kind"], record, generated=True
            )
        except Exception as exc:  # noqa: BLE001 - durable worker records failures
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= self.max_attempts else "queued"
            with self.store.transaction():
                self.store.db.execute(
                    """UPDATE dc_project_context_jobs
                       SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP
                       WHERE tenant_id=? AND project_id=? AND kind=?
                         AND generation=?""",
                    (
                        status,
                        str(exc)[:500],
                        row["tenant_id"],
                        row["project_id"],
                        row["kind"],
                        row["generation"],
                    ),
                )
        else:
            with self.store.transaction():
                self.store.db.execute(
                    """UPDATE dc_project_context_jobs
                       SET status='completed',last_error=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE tenant_id=? AND project_id=? AND kind=?
                         AND generation=? AND status='running'""",
                    (
                        row["tenant_id"],
                        row["project_id"],
                        row["kind"],
                        row["generation"],
                    ),
                )
        final = self.store.db.execute(
            """SELECT status,generation,attempts,last_error
               FROM dc_project_context_jobs
               WHERE tenant_id=? AND project_id=? AND kind=?""",
            (row["tenant_id"], row["project_id"], row["kind"]),
        ).fetchone()
        if final is None:
            return None
        return ProjectContextJob(
            row["project_id"],
            row["kind"],
            final["status"],
            final["generation"],
            final["attempts"],
            final["last_error"],
        )
