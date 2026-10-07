"""Scoped retrieval, citation, and vector projection lifecycle."""

from __future__ import annotations

import hashlib
import json

import pytest
from doclang import DocLangXDocument

from docling_context import (
    Ingestor,
    LocalContextStore,
    Principal,
    Retriever,
    SearchScope,
    TurbovecIndex,
)
from docling_context.cli import _search_excerpt, main


def _package(body: str) -> bytes:
    document = DocLangXDocument()
    assert document.read_xml(body)
    return document.write_bytes()


def _image_package() -> bytes:
    document = DocLangXDocument()
    assert document.read_xml(
        '<doclang><picture><src uri="assets/one.png"/></picture>'
        '<picture><src uri="assets/two.png"/><text>Rock diagram</text>'
        "</picture></doclang>"
    )
    document.set_part_bytes("assets/one.png", b"first image", "image/png")
    document.set_part_bytes("assets/two.png", b"second image", "image/png")
    return document.write_bytes()


class WordVectors:
    model_id = "word-hash-1"
    dimensions = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        output = []
        for text in texts:
            values = [0.0] * 8
            for word in text.casefold().split():
                index = hashlib.sha256(word.encode()).digest()[0] % 8
                values[index] += 1.0
            output.append(values)
        return output


def test_lexical_scope_exact_citations_and_budget(tmp_path):
    alice = Principal("north", "alice")
    bob = Principal("south", "bob")
    with LocalContextStore(tmp_path) as store:
        record = Ingestor(store).add_resource(
            alice,
            "docling://resources/geology/one",
            _package(
                "<doclang><heading>Glacier report</heading>"
                "<text>Blue glacier advances in winter</text>"
                "<heading>Volcano</heading><text>Red lava flows</text></doclang>"
            ),
            filename="one.dclx",
        )
        store.put(
            alice,
            "docling://resources/geology-sibling/two",
            _package("<doclang><text>glacier sibling</text></doclang>"),
        )
        store.put(
            bob,
            "docling://resources/geology/three",
            _package("<doclang><text>glacier other tenant</text></doclang>"),
        )
        retrieval = Retriever(store)
        result = retrieval.search(
            alice,
            "glacier",
            scope=SearchScope(
                uri="docling://resources/geology",
                xpath="/doclang[1]/text[1]",
            ),
            mode="lexical",
            max_tokens=3,
            per_result_tokens=3,
        )
        assert len(result.hits) == 1
        hit = result.hits[0]
        assert hit.uri == record.uri
        assert hit.revision_id == record.revision_id
        assert hit.xpath == "/doclang[1]/text[1]"
        assert hit.text == "Blue glacier advances"
        assert hit.truncated and result.truncated
        assert result.tokens_used == 3
        assert hit.lexical_score > 0
        assert hit.vector_score == 0
        sidecars = retrieval.search(
            alice,
            "Glacier report",
            scope=SearchScope(uri="docling://resources/geology"),
            mode="lexical",
            k=10,
        ).hits
        assert any(hit.xpath.startswith("@summary:") for hit in sidecars)
        assert any(hit.xpath.startswith("@toc:") for hit in sidecars)
        assert (
            retrieval.search(
                alice,
                "sibling",
                scope=SearchScope(uri="docling://resources/geology"),
                mode="lexical",
            ).hits
            == ()
        )
        with pytest.raises(PermissionError):
            retrieval.search(
                alice,
                "glacier",
                scope=SearchScope(uri="docling://users/south/bob/memories"),
            )


def test_replacement_delete_and_stale_row_are_never_cited(tmp_path):
    principal = Principal("north", "alice")
    uri = "docling://resources/geology/one"
    with LocalContextStore(tmp_path) as store:
        old = store.put(
            principal,
            uri,
            _package("<doclang><text>old glacier</text></doclang>"),
        )
        new = store.put(
            principal,
            uri,
            _package("<doclang><text>new basalt</text></doclang>"),
            expected_revision=old.revision_id,
        )
        cursor = store.db.execute(
            """INSERT INTO retrieval_units
               (tenant_id,uri,document_id,revision_id,xpath,tier,text,updated_at)
               VALUES (?,?,?,?,?,2,?,?)""",
            (
                principal.tenant_id,
                uri,
                old.document_id,
                old.revision_id,
                "/doclang[1]/text[1]",
                "old glacier",
                old.updated_at,
            ),
        )
        store.db.execute(
            "INSERT INTO retrieval_fts(rowid,text) VALUES (?,?)",
            (cursor.lastrowid, "old glacier"),
        )
        retrieval = Retriever(store)
        assert retrieval.search(principal, "old", mode="lexical").hits == ()
        assert (
            retrieval.search(principal, "basalt", mode="lexical").hits[0].revision_id
            == new.revision_id
        )
        store.delete(principal, uri, expected_revision=new.revision_id)
        assert retrieval.search(principal, "basalt", mode="lexical").hits == ()


def test_cli_search_and_schema_backfill(tmp_path, capsys):
    principal = Principal("north", "alice")
    uri = "docling://resources/notes/one"
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal, uri, _package("<doclang><text>amber fossil</text></doclang>")
        )
        store.db.execute("DELETE FROM retrieval_fts")
        store.db.execute("DELETE FROM retrieval_units")
    with LocalContextStore(tmp_path) as store:
        assert Retriever(store).search(principal, "fossil", mode="lexical").hits
    assert (
        main(
            [
                "--store",
                str(tmp_path),
                "--tenant",
                "north",
                "--user",
                "alice",
                "search",
                "fossil",
                "--uri",
                "docling://resources/notes",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["xpath"] == "/doclang[1]/text[1]"
    assert (
        main(
            [
                "--store",
                str(tmp_path),
                "--tenant",
                "north",
                "--user",
                "alice",
                "search",
                "fossil",
                "--uri",
                "docling://resources/notes",
            ]
        )
        == 0
    )
    table = capsys.readouterr().out
    assert "Score" in table and "Document ID" in table and "Excerpt" in table
    assert "amber fossil" in table and "/doclang[1]/text[1]" in table
    assert "revision_id" not in table


def test_search_excerpt_does_not_overlap_or_wrap():
    first = "A" * 64
    last = "B" * 64
    assert _search_excerpt(first + last) == first + last
    assert _search_excerpt(first + "X" + last) == first + " ... " + last
    assert _search_excerpt("first\n  second") == "first second"


def test_search_reports_matching_nodes_before_result_limit(tmp_path, capsys):
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        for number in range(3):
            store.put(
                principal,
                f"docling://resources/notes/{number}",
                _package(f"<doclang><text>fossil {number}</text></doclang>"),
            )
        store.put(
            principal,
            "docling://resources/other/four",
            _package("<doclang><text>fossil elsewhere</text></doclang>"),
        )
        result = Retriever(store).search(
            principal,
            "fossil",
            scope=SearchScope(uri="docling://resources/notes"),
            mode="lexical",
            k=1,
        )
        assert len(result.hits) == 1
        assert result.total_hits == 3
    command = [
        "--store",
        str(tmp_path),
        "--tenant",
        "north",
        "--user",
        "alice",
        "search",
        "fossil",
        "--uri",
        "docling://resources/notes",
        "-k",
        "1",
    ]
    assert main(command) == 0
    assert "Showing 1 of 3 matching nodes." in capsys.readouterr().out
    assert main(command[:7] + ["missing"] + command[8:]) == 0
    assert "Showing 0 of 0 matching nodes." in capsys.readouterr().out


def test_index_status_and_rebuild_restore_search_records(tmp_path, capsys):
    alice = Principal("north", "alice")
    bob = Principal("north", "bob")
    uri = "docling://resources/notes/one"
    with LocalContextStore(tmp_path) as store:
        store.put(alice, uri, _package("<doclang><text>amber fossil</text></doclang>"))
        store.put(
            bob,
            "docling://users/north/bob/memories/secret",
            _package("<doclang><text>private fossil</text></doclang>"),
        )
        identifier = store.db.execute(
            "SELECT vector_id FROM retrieval_units WHERE tenant_id=? AND uri=? AND text=?",
            (alice.tenant_id, uri, "amber fossil"),
        ).fetchone()[0]
        store.db.execute("DELETE FROM retrieval_fts WHERE rowid=?", (identifier,))
        store.db.execute(
            """INSERT INTO vector_state
               (singleton,generation,model_id,dimensions,normalization,outbox_offset,snapshot_hash)
               VALUES (1,'old','fixture',8,'l2',0,'old')"""
        )
    command = ["--store", str(tmp_path), "--tenant", "north", "--user", "alice"]
    assert main([*command, "index", "status"]) == 0
    assert "Indexed nodes" in capsys.readouterr().out
    assert main([*command, "index", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["documents"] == 1
    assert status["indexed_documents"] == 1
    assert status["embedded_nodes"] == 0
    assert status["vector_snapshot_current"] is False
    with LocalContextStore(tmp_path) as store:
        assert not Retriever(store).search(alice, "fossil", mode="lexical").hits
    assert main([*command, "index", "lex", "status"]) == 0
    assert "Indexed documents" in capsys.readouterr().out
    assert main([*command, "index", "lex", "status", "--json"]) == 0
    assert "embedded_nodes" not in json.loads(capsys.readouterr().out)
    assert main([*command, "index", "lex", "rebuild"]) == 0
    rebuilt = json.loads(capsys.readouterr().out)
    assert rebuilt["rebuilt_documents"] == 1
    assert rebuilt["indexed_nodes"] >= 1
    with LocalContextStore(tmp_path) as store:
        assert Retriever(store).search(alice, "fossil", mode="lexical").hits
        assert (
            store.db.execute(
                "SELECT outbox_offset FROM vector_state WHERE singleton=1"
            ).fetchone()[0]
            == -1
        )


def test_lexical_search_filters_before_candidate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("docling_context.retrieval._MAX_CANDIDATES", 1)
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/first",
            _package("<doclang><text>quartz</text></doclang>"),
        )
        store.put(
            principal,
            "docling://resources/notes/second",
            _package("<doclang><text>fossil</text></doclang>"),
        )
        result = Retriever(store).search(principal, "fossil", mode="lexical")
        assert result.total_hits == 1
        assert len(result.hits) == 1
        assert result.hits[0].uri.endswith("/second")


def test_vector_snapshot_restart_corruption_and_generation(tmp_path):
    pytest.importorskip("turbovec")
    principal = Principal("north", "alice")
    uri = "docling://resources/geology/one"
    provider = WordVectors()
    with LocalContextStore(tmp_path) as store:
        first = store.put(
            principal,
            uri,
            _package("<doclang><text>glacier ice</text></doclang>"),
        )
        store.put(
            Principal("south", "bob"),
            uri,
            _package("<doclang><text>glacier private south</text></doclang>"),
        )
        retrieval = Retriever(store, embedder=provider)
        assert isinstance(retrieval.vector_index, TurbovecIndex)
        result = retrieval.search(principal, "glacier", mode="vector")
        assert result.hits[0].revision_id == first.revision_id
        assert all("private south" not in hit.text for hit in result.hits)
        cited_text = result.hits[0].text
        generation = retrieval.vector_index.generation
        snapshot = tmp_path / f"vectors-{generation}.tvim"
        assert snapshot.exists()
    with LocalContextStore(tmp_path) as store:
        retrieval = Retriever(store, embedder=provider)
        assert retrieval.vector_index.generation == generation
        assert retrieval.search(principal, "glacier", mode="vector").hits
    snapshot.write_bytes(b"bad snapshot")
    with LocalContextStore(tmp_path) as store:
        retrieval = Retriever(store, embedder=provider)
        assert retrieval.vector_index.generation != generation
        assert (
            retrieval.search(principal, "glacier", mode="vector").hits[0].text
            == cited_text
        )
        new = store.put(
            principal,
            uri,
            _package("<doclang><text>basalt lava</text></doclang>"),
            expected_revision=first.revision_id,
        )
        assert (
            retrieval.search(principal, "glacier", mode="vector").hits[0].revision_id
            == new.revision_id
        )
        store.delete(principal, uri, expected_revision=new.revision_id)
        assert retrieval.search(principal, "glacier", mode="vector").hits == ()
    with LocalContextStore(tmp_path) as store:
        assert (
            Retriever(store, embedder=provider)
            .search(principal, "glacier", mode="vector")
            .hits
            == ()
        )

        class NewModel(WordVectors):
            model_id = "word-hash-2"

        changed = Retriever(store, embedder=NewModel())
        state = store.db.execute(
            "SELECT model_id FROM vector_state WHERE singleton=1"
        ).fetchone()
        assert state["model_id"] == "word-hash-2"
        assert changed.search(principal, "glacier", mode="vector").hits == ()


def test_vector_outbox_replay_only_embeds_changed_document(tmp_path, monkeypatch):
    pytest.importorskip("turbovec")

    class CountingVectors(WordVectors):
        def __init__(self):
            self.texts: list[str] = []

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.texts.extend(texts)
            return super().embed(texts)

    principal = Principal("north", "alice")
    provider = CountingVectors()
    with LocalContextStore(tmp_path) as store:
        first = store.put(
            principal,
            "docling://resources/notes/one",
            _package("<doclang><text>glacier ice</text></doclang>"),
        )
        store.put(
            principal,
            "docling://resources/notes/two",
            _package("<doclang><text>basalt lava</text></doclang>"),
        )
        retriever = Retriever(store, embedder=provider)
        initial = retriever.vector_index.generation
        monkeypatch.setattr(
            retriever.vector_index,
            "rebuild",
            lambda: pytest.fail("compatible outbox changes must replay"),
        )
        provider.texts.clear()
        store.put(
            principal,
            first.uri,
            _package("<doclang><text>quartz crystal</text></doclang>"),
            expected_revision=first.revision_id,
        )
        assert retriever.search(principal, "quartz", mode="vector").hits
        assert provider.texts == ["quartz crystal", "quartz"]
        assert retriever.vector_index.generation != initial
        assert all(
            hit.uri != first.uri
            for hit in retriever.search(principal, "ice", mode="lexical").hits
        )
        provider.texts.clear()
        current = store.get_record(principal, first.uri)
        store.delete(principal, first.uri, expected_revision=current.revision_id)
        assert retriever.search(principal, "quartz", mode="vector").hits
        assert provider.texts == ["quartz"]
        assert not store.db.execute(
            "SELECT 1 FROM vector_members WHERE uri=?", (first.uri,)
        ).fetchone()


def test_legacy_vector_snapshot_rebuilds_membership_on_upgrade(tmp_path):
    pytest.importorskip("turbovec")
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/one",
            _package("<doclang><text>glacier ice</text></doclang>"),
        )
        old_generation = Retriever(
            store, embedder=WordVectors()
        ).vector_index.generation
        store.db.executescript("DROP TABLE vector_members; PRAGMA user_version=3;")
    with LocalContextStore(tmp_path) as store:
        retriever = Retriever(store, embedder=WordVectors())
        assert retriever.vector_index.generation != old_generation
        assert retriever.search(principal, "glacier", mode="vector").hits
        assert store.db.execute("SELECT COUNT(*) FROM vector_members").fetchone()[0] > 0


def test_existing_retrieval_index_adds_picture_column_on_upgrade(tmp_path):
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/one",
            _package("<doclang><text>glacier ice</text></doclang>"),
        )
        store.db.executescript(
            "ALTER TABLE retrieval_units DROP COLUMN asset_path; PRAGMA user_version=4;"
        )
    with LocalContextStore(tmp_path) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "asset_path" in {
            row["name"]
            for row in store.db.execute("PRAGMA table_info(retrieval_units)")
        }
        assert Retriever(store).search(principal, "glacier", mode="lexical").hits


def test_section_xpath_tracks_heading_without_changing_xml_parent(tmp_path):
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/one",
            _package(
                "<doclang><heading>Ice</heading><text>Glacier snow</text>"
                "<heading>Rock</heading><text>Glacier stone</text></doclang>"
            ),
        )
        rows = store.db.execute(
            """SELECT xpath,parent_xpath,section_xpath FROM retrieval_units
               WHERE xpath IN ('/doclang[1]/text[1]','/doclang[1]/text[2]')
               ORDER BY xpath"""
        ).fetchall()
        assert [row["parent_xpath"] for row in rows] == [
            "/doclang[1]",
            "/doclang[1]",
        ]
        assert [row["section_xpath"] for row in rows] == [
            "/doclang[1]/heading[1]",
            "/doclang[1]/heading[2]",
        ]


def test_matching_outline_precedes_body_when_budget_allows(tmp_path):
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        Ingestor(store).add_resource(
            principal,
            "docling://resources/notes/outline",
            _package(
                "<doclang><heading>Glacier overview</heading>"
                "<text>Glacier details with glacier glacier glacier examples</text>"
                "</doclang>"
            ),
            filename="outline.dclx",
        )
        hits = Retriever(store).search(principal, "glacier", mode="lexical", k=4).hits
        assert hits
        assert [hit.tier for hit in hits] == sorted(hit.tier for hit in hits)


def test_search_decodes_each_cited_package_once(tmp_path, monkeypatch):
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        Ingestor(store).add_resource(
            principal,
            "docling://resources/notes/one",
            _package(
                "<doclang><heading>Glacier overview</heading>"
                "<text>Glacier ice</text><text>Glacier snow</text></doclang>"
            ),
            filename="one.dclx",
        )
        original = store.get_document
        calls = []

        def counted(*args, **kwargs):
            calls.append(args[1])
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "get_document", counted)
        hits = Retriever(store).search(principal, "glacier", mode="lexical", k=10).hits
        assert len(hits) >= 3
        assert len(calls) == 1


def test_multiword_lexical_search_uses_all_words_then_falls_back(tmp_path):
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/one",
            _package("<doclang><text>glacier ice</text></doclang>"),
        )
        store.put(
            principal,
            "docling://resources/notes/two",
            _package("<doclang><text>glacier rock</text></doclang>"),
        )
        retriever = Retriever(store)
        focused = retriever.search(principal, "glacier ice", mode="lexical")
        assert focused.total_hits == 1
        assert [hit.uri for hit in focused.hits] == ["docling://resources/notes/one"]
        fallback = retriever.search(principal, "glacier snow", mode="lexical")
        assert fallback.total_hits == 2


def test_picture_assets_are_vector_units_when_provider_supports_images(tmp_path):
    pytest.importorskip("turbovec")

    class ImageVectors(WordVectors):
        model_id = "image-test-1"
        supports_images = True

        def __init__(self):
            self.calls = []

        def embed_image(self, image: bytes):
            self.calls.append(("image", image))
            return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        def embed_text_image(self, text: str, image: bytes):
            self.calls.append(("mixed", text, image))
            return [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(principal, "docling://resources/images/one", _image_package())
        provider = ImageVectors()
        retriever = Retriever(store, embedder=provider)
        assert ("image", b"first image") in provider.calls
        assert ("mixed", "Rock diagram", b"second image") in provider.calls
        hit = retriever.search(principal, "", image=b"query", mode="vector", k=1).hits[
            0
        ]
        assert hit.xpath == "/doclang[1]/picture[1]"
        assert hit.asset_path == "assets/one.png"
        assert hit.text == ""
        text_only = Retriever(store, embedder=WordVectors())
        assert text_only.search(principal, "", image=b"query", mode="vector").hits == ()
        assert (
            store.db.execute(
                "SELECT embedding FROM retrieval_units WHERE xpath=?",
                ("/doclang[1]/picture[1]",),
            ).fetchone()[0]
            is None
        )


def test_rebuild_index_refreshes_existing_vector_retriever(tmp_path):
    pytest.importorskip("turbovec")
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/one",
            _package("<doclang><text>glacier ice</text></doclang>"),
        )
        retriever = Retriever(store, embedder=WordVectors())
        first = retriever.search(principal, "glacier", mode="vector")
        assert first.hits and first.hits[0].vector_score > 0
        old_generation = retriever.vector_index.generation
        lex_progress: list[tuple[int, int]] = []
        status = store.rebuild_index(
            principal,
            progress=lambda done, total: lex_progress.append((done, total)),
        )
        assert lex_progress[0] == (0, 1)
        assert lex_progress[-1] == (1, 1)
        assert status["embedded_nodes"] == 0
        assert status["vector_snapshot_current"] is False
        second = retriever.search(principal, "glacier", mode="vector")
        assert second.hits and second.hits[0].vector_score > 0
        assert retriever.vector_index.generation != old_generation
        assert store.index_status(principal)["vector_snapshot_current"] is True
        vector_progress: list[tuple[int, int]] = []
        TurbovecIndex(
            store,
            WordVectors(),
            force_rebuild=True,
            progress=lambda done, total: vector_progress.append((done, total)),
        )
        assert vector_progress[0][0] == 0
        assert vector_progress[-1][0] == vector_progress[-1][1]
        assert vector_progress[-1][1] > 0


def test_cli_vector_rebuild_search_and_scope_guard(tmp_path, capsys, monkeypatch):
    pytest.importorskip("turbovec")
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/one",
            _package("<doclang><text>glacier ice</text></doclang>"),
        )
    selected_backends: list[str] = []

    def provider_for_cli(_model, *, backend, offline):
        assert offline is False
        selected_backends.append(backend)
        return WordVectors()

    monkeypatch.setattr(
        "docling_context.cli.local_embedding_provider", provider_for_cli
    )
    monkeypatch.setattr(
        "docling_context.cli.stored_embedding_provider",
        lambda *_args, **_kwargs: WordVectors(),
    )
    command = ["--store", str(tmp_path), "--tenant", "north", "--user", "alice"]
    assert main([*command, "index", "vector", "rebuild", "--backend", "onnx"]) == 0
    assert selected_backends == ["onnx"]
    assert json.loads(capsys.readouterr().out)["embedded_nodes"] > 0
    assert main([*command, "index", "vector", "status"]) == 0
    vector_table = capsys.readouterr().out
    assert "Vector model" in vector_table and "word-hash-1" in vector_table
    assert main([*command, "index", "vector", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["vector_model"] == "word-hash-1"
    assert main([*command, "search", "glacier"]) == 0
    output = capsys.readouterr().out
    assert "Showing" in output and "ranked nodes" in output
    with LocalContextStore(tmp_path) as store:
        result = Retriever(store, embedder=WordVectors()).search(
            principal, "", image=b"unsupported", mode="vector"
        )
        assert result.hits == ()
        store.put(
            Principal("north", "bob"),
            "docling://users/north/bob/memories/private",
            _package("<doclang><text>private ice</text></doclang>"),
        )
    assert main([*command, "index", "vector", "rebuild"]) == 1
    assert "accessible" in capsys.readouterr().err
