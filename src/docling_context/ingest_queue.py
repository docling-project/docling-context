"""Durable, bounded ingestion requests processed outside interactive MCP calls."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from .conversion_config import PdfConversionConfig
from .converters import LocalDoclingConverter
from .ingestion import Ingestor
from .local import LocalContextStore
from .models import Principal
from .uri import authorize_uri, parse_uri

MAX_QUEUED_BYTES = 2_500_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class IngestJob:
    job_id: str
    uri: str
    status: str
    revision_id: str | None
    error: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class IngestQueue:
    def __init__(self, store: LocalContextStore):
        self.store = store

    def submit(
        self,
        principal: Principal,
        uri: str,
        source: bytes,
        filename: str,
        *,
        force: bool = False,
    ) -> IngestJob:
        address = parse_uri(uri)
        authorize_uri(principal, address)
        if not 0 < len(source) <= MAX_QUEUED_BYTES or not filename:
            raise ValueError("queued source must be nonempty and at most 2.5 MB")
        job_id, stamp = uuid.uuid4().hex, _now()
        with self.store.transaction():
            self.store.db.execute(
                """INSERT INTO ingest_jobs
                   (job_id,tenant_id,user_id,uri,filename,source,force,status,
                    created_at,updated_at) VALUES (?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    job_id,
                    principal.tenant_id,
                    principal.user_id,
                    address.value,
                    filename,
                    source,
                    int(force),
                    stamp,
                    stamp,
                ),
            )
        return self.get(principal, job_id)

    def get(self, principal: Principal, job_id: str) -> IngestJob:
        row = self.store.db.execute(
            """SELECT * FROM ingest_jobs WHERE job_id=? AND tenant_id=? AND user_id=?""",
            (job_id, principal.tenant_id, principal.user_id),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        authorize_uri(principal, parse_uri(row["uri"]))
        return IngestJob(*(row[field] for field in IngestJob.__dataclass_fields__))

    def run_once(self) -> IngestJob | None:
        stamp = _now()
        lease = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        with self.store.transaction():
            row = self.store.db.execute(
                """SELECT * FROM ingest_jobs WHERE status='queued'
                   OR (status='running' AND lease_until<?)
                   ORDER BY created_at LIMIT 1""",
                (stamp,),
            ).fetchone()
            if row is None:
                return None
            self.store.db.execute(
                """UPDATE ingest_jobs SET status='running',lease_until=?,updated_at=?
                   WHERE job_id=?""",
                (lease, stamp, row["job_id"]),
            )
        principal = Principal(row["tenant_id"], row["user_id"])
        revision = None
        error = None
        try:
            record = Ingestor(
                self.store,
                converter=LocalDoclingConverter(
                    PdfConversionConfig(document_timeout=300)
                ),
            ).add_resource(
                principal,
                row["uri"],
                row["source"],
                filename=row["filename"],
                force=bool(row["force"]),
            )
            revision = record.revision_id
        except Exception as exc:  # noqa: BLE001 - converter failures must be recorded
            error = f"{type(exc).__name__}: {exc}"[:2_000]
        with self.store.transaction():
            self.store.db.execute(
                """UPDATE ingest_jobs SET status=?,revision_id=?,error=?,
                   source=x'',lease_until=NULL,updated_at=? WHERE job_id=?""",
                (
                    "failed" if error else "completed",
                    revision,
                    error,
                    _now(),
                    row["job_id"],
                ),
            )
        return self.get(principal, row["job_id"])
