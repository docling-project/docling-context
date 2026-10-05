"""Durable local jobs for collection-level DocLang summaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from xml.sax.saxutils import escape

from doclang import DocLangXDocument

from .local import LocalContextStore, RecordMissing
from .models import Principal
from .uri import authorize_uri, parse_uri


@dataclass(frozen=True, slots=True)
class CollectionStatus:
    collection_uri: str
    stale: bool
    latest_event: int
    summary_revision: str | None


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    tenant_id: str
    collection_uri: str
    input_revision: str
    input_event: int
    status: str
    attempts: int
    retry_at: str
    lease_until: str | None
    last_error: str | None


def _time(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


class CollectionWorker:
    """Claims one durable job at a time; expired leases can be reclaimed."""

    def __init__(
        self,
        store: LocalContextStore,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 3,
    ):
        if lease_seconds < 1 or max_attempts < 1:
            raise ValueError("worker limits must be positive")
        self.store = store
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def status(
        self, principal: Principal, collection_uri: str
    ) -> CollectionStatus | None:
        address = parse_uri(collection_uri, prefix=True)
        authorize_uri(principal, address)
        row = self.store.db.execute(
            "SELECT * FROM collection_status WHERE tenant_id=? AND collection_uri=?",
            (principal.tenant_id, address.value),
        ).fetchone()
        if row is None:
            return None
        return CollectionStatus(
            row["collection_uri"],
            bool(row["stale"]),
            row["latest_event"],
            row["summary_revision"],
        )

    def jobs(self, principal: Principal, collection_uri: str) -> list[Job]:
        address = parse_uri(collection_uri, prefix=True)
        authorize_uri(principal, address)
        rows = self.store.db.execute(
            "SELECT * FROM jobs WHERE tenant_id=? AND collection_uri=? ORDER BY input_event",
            (principal.tenant_id, address.value),
        ).fetchall()
        return [Job(*(row[key] for key in Job.__dataclass_fields__)) for row in rows]

    def claim(self, now: datetime | None = None) -> Job | None:
        current = _time(now)
        stamp = current.isoformat()
        lease = (current + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.store.transaction():
            row = self.store.db.execute(
                """SELECT j.* FROM jobs j WHERE
                    ((j.status='queued' AND j.retry_at<=?) OR
                     (j.status='running' AND j.lease_until<=?))
                    AND NOT EXISTS (
                      SELECT 1 FROM jobs earlier WHERE earlier.tenant_id=j.tenant_id
                      AND earlier.collection_uri=j.collection_uri
                      AND earlier.input_event<j.input_event
                      AND earlier.status IN ('queued','running'))
                    ORDER BY j.input_event LIMIT 1""",
                (stamp, stamp),
            ).fetchone()
            if row is None:
                return None
            self.store.db.execute(
                """UPDATE jobs SET status='running',attempts=attempts+1,lease_until=?,updated_at=?
                   WHERE job_id=?""",
                (lease, stamp, row["job_id"]),
            )
            claimed = self.store.db.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return Job(*(claimed[key] for key in Job.__dataclass_fields__))

    def _finish(
        self,
        job: Job,
        now: datetime,
        *,
        error: Exception | None = None,
        summary_revision: str | None = None,
    ) -> None:
        stamp = now.isoformat()
        with self.store.transaction():
            current = self.store.db.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            if (
                current is None
                or current["status"] != "running"
                or current["lease_until"] != job.lease_until
            ):
                return
            if error is None:
                self.store.db.execute(
                    "UPDATE jobs SET status='complete',lease_until=NULL,updated_at=? WHERE job_id=?",
                    (stamp, job.job_id),
                )
                if summary_revision is not None:
                    self.store.db.execute(
                        """UPDATE collection_status SET stale=0,summary_revision=?
                           WHERE tenant_id=? AND collection_uri=? AND latest_event=?""",
                        (
                            summary_revision,
                            job.tenant_id,
                            job.collection_uri,
                            job.input_event,
                        ),
                    )
            else:
                terminal = current["attempts"] >= self.max_attempts
                retry = now + timedelta(seconds=min(3600, 2 ** current["attempts"]))
                self.store.db.execute(
                    """UPDATE jobs SET status=?,retry_at=?,lease_until=NULL,last_error=?,updated_at=?
                       WHERE job_id=?""",
                    (
                        "failed" if terminal else "queued",
                        retry.isoformat(),
                        str(error)[:500],
                        stamp,
                        job.job_id,
                    ),
                )

    def process(self, job: Job, now: datetime | None = None) -> None:
        current_time = _time(now)
        row = self.store.db.execute(
            "SELECT latest_event FROM collection_status WHERE tenant_id=? AND collection_uri=?",
            (job.tenant_id, job.collection_uri),
        ).fetchone()
        if row is None or row["latest_event"] != job.input_event:
            self._finish(job, current_time)
            return
        principal = Principal(job.tenant_id, "worker")
        summary_uri = f"{job.collection_uri}/_context/summary"
        try:
            prefix = f"{job.collection_uri}/"
            rows = self.store.db.execute(
                """SELECT * FROM documents WHERE tenant_id=? AND substr(uri,1,?)=?
                   AND uri<>? AND available=1 ORDER BY uri""",
                (job.tenant_id, len(prefix), prefix, summary_uri),
            ).fetchall()
            children = [self.store._verified_record(row) for row in rows]
            try:
                existing = self.store.get_record(principal, summary_uri)
            except RecordMissing:
                existing = None
            if (
                existing is not None
                and existing.source.get("input_event") == job.input_event
            ):
                self._finish(job, current_time, summary_revision=existing.revision_id)
                return
            entries = []
            for child in children[:500]:
                title = child.uri.rsplit("/", 1)[-1]
                entries.append(
                    f"<heading>{escape(title)}</heading><text>{escape(child.uri)}</text>"
                )
            xml = f"<doclang>{''.join(entries) or '<text>Empty collection</text>'}</doclang>"
            document = DocLangXDocument()
            if not document.read_xml(xml):
                raise ValueError(document.last_error())
            if not document.set_document_summary(
                f"<doclang><text>{escape(f'{len(children)} documents in {job.collection_uri}')}</text></doclang>"
            ):
                raise ValueError(document.last_error())
            toc_entries = "".join(
                f'<entry xpath="/doclang[1]/heading[{i}]"><description>{escape(child.uri.rsplit("/", 1)[-1])}</description></entry>'
                for i, child in enumerate(children[:500], 1)
            )
            if not document.set_toc(f"<doclang><toc>{toc_entries}</toc></doclang>"):
                raise ValueError(document.last_error())
            source = {
                "kind": "collection-summary",
                "input_event": job.input_event,
                "children": {child.uri: child.revision_id for child in children},
            }
            document.set_part_text(
                "context/manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_revision": job.input_revision,
                        "child_revisions": source["children"],
                    },
                    sort_keys=True,
                ),
                "application/json",
            )
            result = self.store.put(
                principal,
                summary_uri,
                document.write_bytes(),
                expected_revision=existing.revision_id if existing else None,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001 - persist arbitrary worker failures
            self._finish(job, current_time, error=exc)
            return
        self._finish(job, current_time, summary_revision=result.revision_id)

    def run_once(self, now: datetime | None = None) -> Job | None:
        job = self.claim(now)
        if job is not None:
            self.process(job, now)
        return job

    def run_forever(self, stop: Event, *, poll_seconds: float = 1.0) -> None:
        """Run in a dedicated process or thread that owns its store connection."""
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not stop.is_set():
            if self.run_once() is None:
                stop.wait(poll_seconds)
