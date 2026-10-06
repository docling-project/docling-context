"""Step 1 storage and native API contract checks."""

from __future__ import annotations

import hashlib
import io
from zipfile import ZipFile

import pytest
from doclang import DoclangDocument, DocLangXDocument

from docling_context import (
    AccessDenied,
    Ingestor,
    InvalidURI,
    LocalContextStore,
    MemoryContextStore,
    NodeAddress,
    PackageError,
    Principal,
    RecordMissing,
    RevisionConflict,
    parse_uri,
)
from docling_context.package import bounded_nodes

ALICE = Principal("tenant-a", "alice")
BOB = Principal("tenant-a", "bob")
OTHER = Principal("tenant-b", "alice")
URI = "docling://resources/manuals/guide"
USER_URI = "docling://users/tenant-a/alice/memories/first"
XPATH = "/doclang[1]/text[1]"


def package(text: str = "Body") -> bytes:
    document = DocLangXDocument()
    assert document.read_xml(f'<doclang version="0.7"><text>{text}</text></doclang>')
    document.set_part_bytes("assets/keep.bin", b"opaque", "application/octet-stream")
    document.set_part_text("context/source.json", '{"id":1}', "application/json")
    assert document.set_document_summary(
        "<doclang><text>Short summary</text></doclang>"
    )
    return document.write_bytes()


def test_doclang_native_smoke_and_package_round_trip():
    data = package()
    document = DocLangXDocument()
    assert document.read_bytes(data)
    assert isinstance(document, DoclangDocument)
    assert document.validate_package()["ok"]
    assert document.at(xpath=XPATH) == "Body"
    assert document.get_part_bytes("assets/keep.bin") == b"opaque"
    rewritten = document.write_bytes()
    restored = DocLangXDocument()
    assert restored.read_bytes(rewritten)
    assert restored.at(xpath=XPATH) == "Body"
    assert restored.summary().at(xpath=XPATH) == "Short summary"
    with ZipFile(io.BytesIO(rewritten)) as archive:
        assert archive.read("document.xml").startswith(b"<?xml")
        assert archive.read("assets/keep.bin") == b"opaque"
        assert archive.read("context/source.json") == b'{"id":1}'


@pytest.mark.parametrize(
    "bad",
    [
        "docling://resources/manuals/../guide",
        "docling://resources/manuals//guide",
        "docling://resources/manuals/a%2fb",
        "docling://resources/manuals/a%5Cb",
        "docling://Resources/manuals/guide",
        "docling://users/tenant-a/alice/other/a",
        "docling://resources/manuals/guide?x=1",
    ],
)
def test_uri_rejects_ambiguous_paths(bad):
    with pytest.raises(InvalidURI):
        parse_uri(bad)


def test_uri_unicode_normalization_and_case_policy():
    assert (
        parse_uri("docling://resources/manuals/cafe\u0301").value
        == "docling://resources/manuals/caf\u00e9"
    )
    assert parse_uri("docling://resources/Manuals/Guide").value != URI


@pytest.mark.parametrize("store_kind", ["local", "memory"])
def test_backend_contract_tenants_revisions_and_tree(tmp_path, store_kind):
    store = (
        LocalContextStore(tmp_path) if store_kind == "local" else MemoryContextStore()
    )
    try:
        first = store.put(ALICE, URI, package(), source={"kind": "manual"})
        assert (
            first.package.sha256
            == hashlib.sha256(store.get_package(ALICE, URI)).hexdigest()
        )
        assert store.get_record(ALICE, URI).source == {"kind": "manual"}
        assert store.get_document(ALICE, URI).at(xpath=XPATH) == "Body"
        assert (
            store.list_children(ALICE, "docling://resources/manuals")[0].kind
            == "document"
        )
        assert store.walk(ALICE, "docling://resources", depth=2)[-1].uri == URI

        with pytest.raises(RecordMissing):
            store.get_record(OTHER, URI)
        assert store.list_children(OTHER, "docling://resources/manuals") == []
        assert store.events(OTHER) == []
        other_record = store.put(OTHER, URI, package("Other tenant"))
        assert other_record.document_id != first.document_id
        assert store.get_document(OTHER, URI).at(xpath=XPATH) == "Other tenant"
        assert store.get_document(ALICE, URI).at(xpath=XPATH) == "Body"

        user_record = store.put(ALICE, USER_URI, package("Private"))
        assert user_record.tenant_id == ALICE.tenant_id
        with pytest.raises(AccessDenied):
            store.get_record(BOB, USER_URI)
        with pytest.raises(AccessDenied):
            store.list_children(BOB, "docling://users/tenant-a/alice/memories")
        with pytest.raises((AccessDenied, InvalidURI)):
            store.walk(BOB, "docling://users/tenant-a")

        address = NodeAddress(first.document_id, first.revision_id, XPATH)
        assert store.read_node(ALICE, address, max_chars=2).text == "Bo"
        assert store.read_node(ALICE, address, max_chars=2).truncated
        with pytest.raises(RecordMissing):
            store.get_revision(OTHER, address)
        with pytest.raises(RevisionConflict):
            store.put(ALICE, URI, package("Second"))
        second = store.put(
            ALICE, URI, package("Second"), expected_revision=first.revision_id
        )
        assert second.document_id == first.document_id
        assert second.revision_id != first.revision_id
        assert store.read_node(ALICE, address).text == "Body"
        assert (
            store.read_node(
                ALICE, NodeAddress(second.document_id, second.revision_id, XPATH)
            ).text
            == "Second"
        )
        with pytest.raises(RevisionConflict):
            store.delete(ALICE, URI, expected_revision=first.revision_id)
        store.delete(ALICE, URI, expected_revision=second.revision_id)
        with pytest.raises(RecordMissing):
            store.get_record(ALICE, URI)
        assert store.read_node(ALICE, address).text == "Body"
        assert [event.operation for event in store.events(ALICE)] == [
            "put",
            "put",
            "put",
            "delete",
        ]
        assert [event.operation for event in store.events(BOB)] == [
            "put",
            "put",
            "delete",
        ]
        assert store.events(BOB, after=1, limit=1)[0].revision_id == second.revision_id
    finally:
        if store_kind == "local":
            store.close()


def test_local_restart_recovery_and_fail_closed(tmp_path):
    data = package()
    with LocalContextStore(tmp_path) as store:
        record = store.put(ALICE, URI, data)
        blob = store.packages.path_for(record.package.sha256)
    with LocalContextStore(tmp_path) as store:
        assert store.get_record(ALICE, URI).package.sha256 == record.package.sha256
        assert store.get_package(ALICE, URI) == data
    blob.write_bytes(b"corrupt")
    with LocalContextStore(tmp_path) as store:
        assert record.package.sha256 in store.reconcile()["missing"]
        with pytest.raises(RecordMissing):
            store.get_record(ALICE, URI)
        assert store.list_children(ALICE, "docling://resources/manuals") == []


def test_failed_blob_write_and_sql_commit_expose_no_revision(tmp_path, monkeypatch):
    with LocalContextStore(tmp_path) as store:

        def fail_put(_data):
            raise OSError("simulated package failure")

        original_put = store.packages.put
        monkeypatch.setattr(store.packages, "put", fail_put)
        with pytest.raises(OSError):
            store.put(ALICE, URI, package())
        monkeypatch.setattr(store.packages, "put", original_put)
        assert store.events(ALICE) == []
        with pytest.raises(RecordMissing):
            store.get_record(ALICE, URI)

        store.db.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON outbox
                           BEGIN SELECT RAISE(ABORT, 'simulated commit failure'); END""")
        with pytest.raises(Exception, match="simulated commit failure"):
            store.put(ALICE, URI, package())
        assert store.events(ALICE) == []
        with pytest.raises(RecordMissing):
            store.get_record(ALICE, URI)
    with LocalContextStore(tmp_path) as store:
        assert store.packages.hashes() == set()  # orphan removed on startup


def test_invalid_package_is_not_stored(tmp_path):
    with LocalContextStore(tmp_path) as store:
        with pytest.raises(PackageError):
            store.put(ALICE, URI, b"not a package")
        assert store.packages.hashes() == set()


def test_unicode_node_read_respects_character_limit(tmp_path):
    with LocalContextStore(tmp_path) as store:
        record = store.put(ALICE, URI, package("ééééé"))
        address = NodeAddress(record.document_id, record.revision_id, XPATH)
        content = store.read_node(ALICE, address, max_chars=1)
        assert content.text == "é"
        assert content.truncated


def test_multibyte_nodes_survive_native_byte_boundaries(tmp_path):
    xml = (
        "<doclang><text>éaaaa</text><text>aéaaa</text>"
        "<text>aaéaa</text><text>aaaéa</text></doclang>"
    )
    document = DocLangXDocument()
    assert document.read_xml(xml)
    for text_bytes in (1, 2, 3, 4):
        with pytest.raises(UnicodeDecodeError):
            document.iter_nodes(limit=100_000, max_text_chars=text_bytes)
    nodes = bounded_nodes(document, limit=100_000, text_bytes=1)
    assert len(nodes) == 5
    assert all(node["truncated"] for node in nodes[1:])
    with LocalContextStore(tmp_path) as store:
        record = Ingestor(store).add_resource(
            ALICE, URI, xml.encode(), filename="multibyte.dclg"
        )
        assert store.get_document(ALICE, record.uri).summary() is not None
