"""Ingestion, context tiers, and durable collection work."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from doclang import DocLangXDocument

from docling_context import (
    CollectionWorker,
    ConversionResult,
    DoclingServeConverter,
    Ingestor,
    LocalContextStore,
    MemoryContextStore,
    NodeAddress,
    PdfConversionConfig,
    Principal,
    RecordMissing,
)

PRINCIPAL = Principal("tenant-a", "alice")
COLLECTION = "docling://resources/articles"
URI = f"{COLLECTION}/one"


def dclx() -> bytes:
    doc = DocLangXDocument()
    assert doc.read_xml("<doclang><heading>First</heading><text>Body</text></doclang>")
    doc.set_part_bytes("assets/source.bin", b"asset", "application/octet-stream")
    assert doc.set_concepts(
        "<doclang><concepts><concept><header>Topic</header></concept></concepts></doclang>"
    )
    return doc.write_bytes()


def test_native_import_tiers_assets_and_idempotence(tmp_path):
    with LocalContextStore(tmp_path) as store:
        ingestor = Ingestor(store)
        original = dclx()
        first = ingestor.add_resource(PRINCIPAL, URI, original, filename="one.dclx")
        assert first.source["origin_uri"].startswith("sha256:")
        package = store.get_document(PRINCIPAL, URI)
        assert package.at(xpath="/doclang[1]/text[1]") == "Body"
        assert "First" in package.summary().xml()
        assert (
            package.toc().at(xpath="/doclang[1]/toc[1]/entry[1]/description[1]")
            == "First"
        )
        assert package.get_part_bytes("assets/source.bin") == b"asset"
        assert "Topic" in package.concepts().xml()
        manifest = json.loads(package.get_part_text("context/manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["source_revision"] == first.revision_id
        address = NodeAddress(
            first.document_id, first.revision_id, "/doclang[1]/text[1]"
        )
        assert store.read_node(PRINCIPAL, address).text == "Body"
        again = ingestor.add_resource(PRINCIPAL, URI, original, filename="one.dclx")
        assert again.revision_id == first.revision_id
        assert len(store.events(PRINCIPAL)) == 1
        worker = CollectionWorker(store)
        assert len(worker.jobs(PRINCIPAL, COLLECTION)) == 1
        forced = ingestor.add_resource(
            PRINCIPAL, URI, original, filename="one.dclx", force=True
        )
        assert forced.revision_id != first.revision_id


def test_ingestor_uses_store_contract_with_in_memory_backend():
    store = MemoryContextStore()
    record = Ingestor(store).add_resource(
        PRINCIPAL, URI, b"<doclang><text>Portable</text></doclang>", filename="one.dclg"
    )
    manifest = json.loads(
        store.get_document(PRINCIPAL, URI).get_part_text("context/manifest.json")
    )
    assert manifest["source_revision"] == record.revision_id


def test_dclg_path_rules_change_and_delete_invalidation(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    source = source_dir / "one.dclg.xml"
    source.write_text("<doclang><heading>First</heading><text>Body</text></doclang>")
    with LocalContextStore(tmp_path / "store") as store:
        with pytest.raises(PermissionError):
            Ingestor(store).add_resource(PRINCIPAL, URI, source)
        ingestor = Ingestor(store, allowed_roots=(source_dir,))
        first = ingestor.add_resource(PRINCIPAL, URI, source)
        worker = CollectionWorker(store)
        assert worker.status(PRINCIPAL, COLLECTION).stale
        worker.run_once()
        status = worker.status(PRINCIPAL, COLLECTION)
        assert not status.stale
        summary = store.get_document(PRINCIPAL, f"{COLLECTION}/_context/summary")
        assert URI in summary.xml()
        assert (
            store.get_record(PRINCIPAL, f"{COLLECTION}/_context/summary").source[
                "children"
            ][URI]
            == first.revision_id
        )
        source.write_text(
            "<doclang><heading>Second</heading><text>Changed</text></doclang>"
        )
        changed = ingestor.add_resource(PRINCIPAL, URI, source)
        assert changed.revision_id != first.revision_id
        assert worker.status(PRINCIPAL, COLLECTION).stale
        worker.run_once()
        assert not worker.status(PRINCIPAL, COLLECTION).stale
        store.delete(PRINCIPAL, URI, expected_revision=changed.revision_id)
        assert worker.status(PRINCIPAL, COLLECTION).stale
        worker.run_once()
        assert not worker.status(PRINCIPAL, COLLECTION).stale
        with pytest.raises(RecordMissing):
            store.get_record(PRINCIPAL, URI)


class FakeConverter:
    max_pages = 5
    max_file_bytes = 1000

    def convert(self, source: bytes, filename: str) -> ConversionResult:
        assert source.startswith(b"%PDF") and filename.endswith(".pdf")
        return ConversionResult(dclx(), "test-pdf", "1", {"max_pages": 5})


class FailingProvider:
    def summarize(self, _document, _source_revision: str) -> str:
        raise RuntimeError("model unavailable")


class SuccessfulProvider:
    seen_revision: str | None = None

    def summarize(self, _document, source_revision: str) -> str:
        self.seen_revision = source_revision
        return "<doclang><text>Model summary</text></doclang>"


def test_pdf_converter_contract_and_model_fallback(tmp_path):
    with LocalContextStore(tmp_path) as store:
        ingestor = Ingestor(
            store, converter=FakeConverter(), summary_provider=FailingProvider()
        )
        record = ingestor.add_resource(
            PRINCIPAL, URI, b"%PDF-sample", filename="one.pdf"
        )
        assert record.source["converter"] == "test-pdf"
        assert record.source["summary_method"] == "extractive-fallback"
        assert (
            store.get_document(PRINCIPAL, URI).summary().at(xpath="/doclang[1]/text[1]")
        )


def test_model_summary_is_tied_to_committed_revision(tmp_path):
    provider = SuccessfulProvider()
    with LocalContextStore(tmp_path) as store:
        record = Ingestor(store, summary_provider=provider).add_resource(
            PRINCIPAL, URI, dclx(), filename="one.dclx"
        )
        package = store.get_document(PRINCIPAL, URI)
        assert provider.seen_revision == record.revision_id
        assert package.summary().at(xpath="/doclang[1]/text[1]") == "Model summary"


def test_nested_heading_outline(tmp_path):
    source = (
        b'<doclang><heading level="1">Chapter</heading>'
        b'<heading level="2">Section</heading><text>Body</text></doclang>'
    )
    with LocalContextStore(tmp_path) as store:
        Ingestor(store).add_resource(PRINCIPAL, URI, source, filename="one.dclg")
        toc = store.get_document(PRINCIPAL, URI).toc()
        assert (
            toc.at(xpath="/doclang[1]/toc[1]/entry[1]/entry[1]/description[1]")
            == "Section"
        )


def test_remote_adapter_uses_zip_target_and_bounded_package(monkeypatch):
    with pytest.raises(ValueError):
        DoclingServeConverter("http://public.example.test")
    assert DoclingServeConverter("http://127.0.0.1:5001").endpoint.startswith("http://")
    outer = io.BytesIO()
    with ZipFile(outer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("one.dclx", dclx())

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return outer.getvalue()

    class Opener:
        def open(self, request, *, timeout):
            assert timeout == 120
            assert b'name="target_type"\r\n\r\nzip' in request.data
            assert b'name="to_formats"\r\n\r\ndclx' in request.data
            assert b'name="do_ocr"\r\n\r\nfalse' in request.data
            assert b'name="do_chart_extraction"\r\n\r\ntrue' in request.data
            return Response()

    monkeypatch.setattr(
        "docling_context.converters.build_opener", lambda *_args: Opener()
    )
    result = DoclingServeConverter(
        "https://convert.example.test",
        config=PdfConversionConfig(do_ocr=False, do_chart_extraction=True),
    ).convert(b"%PDF", "one.pdf")
    assert DocLangXDocument().read_bytes(result.package)


def test_worker_reclaims_expired_lease_after_restart(tmp_path):
    start = datetime.now(UTC) + timedelta(seconds=1)
    with LocalContextStore(tmp_path) as store:
        Ingestor(store).add_resource(PRINCIPAL, URI, dclx(), filename="one.dclx")
        claimed = CollectionWorker(store, lease_seconds=2).claim(start)
        assert claimed is not None and claimed.status == "running"
    with LocalContextStore(tmp_path) as store:
        worker = CollectionWorker(store, lease_seconds=2)
        assert worker.claim(start + timedelta(seconds=1)) is None
        recovered = worker.run_once(start + timedelta(seconds=3))
        assert recovered.job_id == claimed.job_id
        assert worker.status(PRINCIPAL, COLLECTION).stale is False
        assert worker.jobs(PRINCIPAL, COLLECTION)[0].attempts == 2


def test_worker_crash_after_summary_commit_does_not_duplicate_revision(
    tmp_path, monkeypatch
):
    start = datetime.now(UTC) + timedelta(seconds=1)
    with LocalContextStore(tmp_path) as store:
        Ingestor(store).add_resource(PRINCIPAL, URI, dclx(), filename="one.dclx")
        worker = CollectionWorker(store, lease_seconds=2)
        claimed = worker.claim(start)

        def crash(*_args, **_kwargs):
            raise KeyboardInterrupt("simulated crash")

        monkeypatch.setattr(worker, "_finish", crash)
        with pytest.raises(KeyboardInterrupt, match="simulated crash"):
            worker.process(claimed, start)
        summary_revision = store.get_record(
            PRINCIPAL, f"{COLLECTION}/_context/summary"
        ).revision_id
        assert len(store.events(PRINCIPAL)) == 2
    with LocalContextStore(tmp_path) as store:
        worker = CollectionWorker(store, lease_seconds=2)
        worker.run_once(start + timedelta(seconds=3))
        assert (
            store.get_record(PRINCIPAL, f"{COLLECTION}/_context/summary").revision_id
            == summary_revision
        )
        assert len(store.events(PRINCIPAL)) == 2
        assert not worker.status(PRINCIPAL, COLLECTION).stale


def test_worker_retry_and_terminal_failure(tmp_path, monkeypatch):
    start = datetime.now(UTC) + timedelta(seconds=1)
    with LocalContextStore(tmp_path) as store:
        Ingestor(store).add_resource(PRINCIPAL, URI, dclx(), filename="one.dclx")
        worker = CollectionWorker(store, max_attempts=2)

        def fail(*_args, **_kwargs):
            raise RuntimeError("write failed")

        monkeypatch.setattr(store, "put", fail)
        worker.run_once(start)
        assert worker.jobs(PRINCIPAL, COLLECTION)[0].status == "queued"
        worker.run_once(start + timedelta(seconds=3))
        assert worker.jobs(PRINCIPAL, COLLECTION)[0].status == "failed"
        assert worker.status(PRINCIPAL, COLLECTION).stale


def test_existing_database_upgrades_without_losing_documents(tmp_path):
    with LocalContextStore(tmp_path) as store:
        original = store.put(PRINCIPAL, URI, dclx())
        store.db.executescript(
            "DROP TABLE jobs; DROP TABLE collection_status; "
            "DROP TABLE retrieval_fts; DROP TABLE retrieval_units; "
            "DROP TABLE vector_state; PRAGMA user_version=1;"
        )
    with LocalContextStore(tmp_path) as store:
        assert store.get_record(PRINCIPAL, URI).revision_id == original.revision_id
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert len(CollectionWorker(store).jobs(PRINCIPAL, COLLECTION)) == 1
