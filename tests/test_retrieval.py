"""Scoped retrieval, citation, and vector projection lifecycle."""

from __future__ import annotations

import hashlib
import json

import pytest
from doclang import DocLangXDocument

from docling_context import (
    Ingestor,
    LocalContextStore,
    MemoryService,
    Principal,
    Retriever,
    SearchScope,
    TurbovecIndex,
)
from docling_context.cli import _search_excerpt, main
from docling_context.service import ContextService, SearchRequest
from docling_context.vectors import VectorStorageUnavailable


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


def _maintenance_retriever(store, provider):
    """Exercise the explicit index maintenance path used by legacy tests."""
    return Retriever(
        store, embedder=provider, vector_index=TurbovecIndex(store, provider)
    )


def test_vector_search_requires_saved_storage(tmp_path, capsys):
    pytest.importorskip("turbovec")
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/one",
            _package("<doclang><text>ice</text></doclang>"),
        )
        with pytest.raises(VectorStorageUnavailable, match="dc index vector rebuild"):
            Retriever(store, embedder=WordVectors())
    command = ["--store", str(tmp_path), "--tenant", "north", "--user", "alice"]
    assert main([*command, "library", "search", "ice", "--mode", "vector"]) == 1
    assert "dc index vector rebuild" in capsys.readouterr().err


def test_stale_vector_search_warns_without_rebuilding(tmp_path, capsys, monkeypatch):
    pytest.importorskip("turbovec")
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal,
            "docling://resources/notes/one",
            _package("<doclang><text>ice</text></doclang>"),
        )
        TurbovecIndex(store, WordVectors(), force_rebuild=True)
        generation = store.db.execute("SELECT generation FROM vector_state").fetchone()[
            0
        ]
        store.put(
            principal,
            "docling://resources/notes/two",
            _package("<doclang><text>basalt</text></doclang>"),
        )
        result = Retriever(store, embedder=WordVectors()).search(
            principal, "ice", mode="vector"
        )
        assert result.hits
        assert result.warnings and "dc index vector rebuild" in result.warnings[0]
        monkeypatch.setattr(
            "docling_context.cli.stored_embedding_provider",
            lambda _model: WordVectors(),
        )
        command = ["--store", str(tmp_path), "--tenant", "north", "--user", "alice"]
        assert (
            main(
                [
                    *command,
                    "library",
                    "search",
                    "ice",
                    "--mode",
                    "vector",
                    "--uri",
                    "docling://resources/",
                    "--json",
                ]
            )
            == 0
        )
        output = capsys.readouterr()
        assert "dc index vector rebuild" in output.err
        assert json.loads(output.out.splitlines()[0])["uri"].endswith("/one")
        monkeypatch.setattr(
            "docling_context.embeddings.stored_embedding_provider",
            lambda _model: WordVectors(),
        )
        service_result = (
            ContextService(store, principal)
            .search(SearchRequest("ice", mode="vector"))
            .data
        )
        assert service_result["warnings"] == list(result.warnings)
        assert (
            store.db.execute("SELECT generation FROM vector_state").fetchone()[0]
            == generation
        )
        assert store.index_status(principal)["vector_snapshot_current"] is False
        store.db.execute("DELETE FROM retrieval_fts")
        combined = Retriever(store, embedder=WordVectors()).search(
            principal, "ice", mode="vector"
        )
        assert len(combined.warnings) == 2
        assert "dc index lex rebuild" in combined.warnings[0]
        assert "dc index vector rebuild" in combined.warnings[1]
        (tmp_path / f"vectors-{generation}.tvim").write_bytes(b"corrupt")
        with pytest.raises(VectorStorageUnavailable, match="dc index vector rebuild"):
            Retriever(store, embedder=WordVectors())


def test_search_warns_without_repairing_missing_lexical_records(tmp_path, capsys):
    principal = Principal("north", "alice")
    uri = "docling://resources/notes/one"
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal, uri, _package("<doclang><text>amber fossil</text></doclang>")
        )
        store.db.execute("DELETE FROM retrieval_fts")
        store.db.execute("DELETE FROM retrieval_units")
    with LocalContextStore(tmp_path) as store:
        result = Retriever(store).search(principal, "fossil", mode="lexical")
        assert result.hits == ()
        assert result.warnings and "dc index lex rebuild" in result.warnings[0]
        assert store.index_status(principal)["indexed_documents"] == 0
        assert (
            store.db.execute("SELECT COUNT(*) FROM retrieval_units").fetchone()[0] == 0
        )
        service_result = (
            ContextService(store, principal)
            .search(SearchRequest("fossil", mode="lexical"))
            .data
        )
        assert service_result["warnings"] == list(result.warnings)
    command = ["--store", str(tmp_path), "--tenant", "north", "--user", "alice"]
    assert main([*command, "library", "search", "fossil", "--mode", "lexical"]) == 0
    assert "dc index lex rebuild" in capsys.readouterr().err


def test_proposed_memory_is_not_reindexed_on_each_search(tmp_path, monkeypatch):
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        MemoryService(store).create(principal, "fact", "Unreviewed claim")
        assert store.index_status(principal)["indexed_documents"] == 1
        monkeypatch.setattr(
            "docling_context.local.index_package",
            lambda *_args: pytest.fail("already indexed proposed memory"),
        )
        assert Retriever(store).search(principal, "claim", mode="lexical").hits == ()


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


def test_cli_search_warns_until_lexical_rebuild(tmp_path, capsys):
    principal = Principal("north", "alice")
    uri = "docling://resources/notes/one"
    with LocalContextStore(tmp_path) as store:
        store.put(
            principal, uri, _package("<doclang><text>amber fossil</text></doclang>")
        )
        store.db.execute("DELETE FROM retrieval_fts")
        store.db.execute("DELETE FROM retrieval_units")
    with LocalContextStore(tmp_path) as store:
        result = Retriever(store).search(principal, "fossil", mode="lexical")
        assert result.hits == ()
        assert result.warnings
    command = ["--store", str(tmp_path), "--tenant", "north", "--user", "alice"]
    assert main([*command, "index", "lex", "rebuild", "--json"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--store",
                str(tmp_path),
                "--tenant",
                "north",
                "--user",
                "alice",
                "library",
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
                "library",
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
        "library",
        "search",
        "fossil",
        "--uri",
        "docling://resources/notes",
        "-k",
        "1",
    ]
    assert main(command) == 0
    assert "Showing 1 of 3 matching nodes." in capsys.readouterr().out
    assert main(command[:8] + ["missing"] + command[9:]) == 0
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
    assert status["indexed_documents"] == 0
    assert status["embedded_nodes"] == 0
    assert status["vector_snapshot_current"] is False
    with LocalContextStore(tmp_path) as store:
        result = Retriever(store).search(alice, "fossil", mode="lexical")
        assert not result.hits
        assert result.warnings and "dc index lex rebuild" in result.warnings[0]
    assert main([*command, "index", "lex", "status"]) == 0
    assert "Indexed documents" in capsys.readouterr().out
    assert main([*command, "index", "lex", "status", "--json"]) == 0
    assert "embedded_nodes" not in json.loads(capsys.readouterr().out)
    assert main([*command, "index", "lex", "rebuild", "--json"]) == 0
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


def test_lexical_and_vector_search_reach_nodes_beyond_ten_thousand(tmp_path):
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        first = store.put(
            principal,
            "docling://resources/notes/first",
            _package("<doclang><text>fossil</text></doclang>"),
        )
        first_id = store.db.execute(
            "SELECT MAX(vector_id) FROM retrieval_units"
        ).fetchone()[0]
        filler = [
            (
                first_id + number + 1,
                principal.tenant_id,
                first.uri,
                first.document_id,
                first.revision_id,
                f"/doclang[1]/text[{number + 2}]",
                2,
                "fossil",
                first.updated_at,
            )
            for number in range(10_001)
        ]
        with store.transaction():
            store.db.executemany(
                """INSERT INTO retrieval_units
                   (vector_id,tenant_id,uri,document_id,revision_id,xpath,tier,text,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                filler,
            )
            store.db.executemany(
                "INSERT INTO retrieval_fts(rowid,text) VALUES (?,?)",
                ((row[0], "fossil") for row in filler),
            )
            store.db.execute(
                "UPDATE lexical_state SET unit_count=(SELECT COUNT(*) FROM retrieval_units WHERE uri=?) WHERE uri=?",
                (first.uri, first.uri),
            )
        target = store.put(
            principal,
            "docling://resources/notes/second",
            _package("<doclang><text>fossil fossil fossil fossil</text></doclang>"),
        )
        target_id = store.db.execute(
            "SELECT MAX(vector_id) FROM retrieval_units WHERE uri=?", (target.uri,)
        ).fetchone()[0]

        class TargetIndex:
            def search(self, _query, allowed_ids, _k):
                assert target_id in allowed_ids
                return [(target_id, 0.9)]

        lexical = Retriever(store).search(principal, "fossil", mode="lexical", k=1)
        assert lexical.total_hits > 10_000
        assert lexical.hits[0].uri == target.uri
        vector = Retriever(
            store, embedder=WordVectors(), vector_index=TargetIndex()
        ).search(principal, "fossil", mode="vector", k=1)
        assert vector.hits[0].uri == target.uri
        hybrid = Retriever(
            store, embedder=WordVectors(), vector_index=TargetIndex()
        ).search(principal, "fossil", mode="hybrid", k=1)
        assert hybrid.hits[0].uri == target.uri


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
        retrieval = _maintenance_retriever(store, provider)
        assert isinstance(retrieval.vector_index, TurbovecIndex)
        result = retrieval.search(principal, "glacier", mode="vector")
        assert result.hits[0].revision_id == first.revision_id
        assert all("private south" not in hit.text for hit in result.hits)
        cited_text = result.hits[0].text
        generation = retrieval.vector_index.generation
        snapshot = tmp_path / f"vectors-{generation}.tvim"
        assert snapshot.exists()
    with LocalContextStore(tmp_path) as store:
        retrieval = _maintenance_retriever(store, provider)
        assert retrieval.vector_index.generation == generation
        assert retrieval.search(principal, "glacier", mode="vector").hits
    snapshot.write_bytes(b"bad snapshot")
    with LocalContextStore(tmp_path) as store:
        retrieval = _maintenance_retriever(store, provider)
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
            _maintenance_retriever(store, provider)
            .search(principal, "glacier", mode="vector")
            .hits
            == ()
        )

        class NewModel(WordVectors):
            model_id = "word-hash-2"

        changed = _maintenance_retriever(store, NewModel())
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
        retriever = _maintenance_retriever(store, provider)
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
        old_generation = _maintenance_retriever(
            store, WordVectors()
        ).vector_index.generation
        store.db.executescript("DROP TABLE vector_members; PRAGMA user_version=3;")
    with LocalContextStore(tmp_path) as store:
        retriever = _maintenance_retriever(store, WordVectors())
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
            "DROP TABLE lexical_state; "
            "ALTER TABLE retrieval_units DROP COLUMN asset_path; PRAGMA user_version=4;"
        )
    with LocalContextStore(tmp_path) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert "asset_path" in {
            row["name"]
            for row in store.db.execute("PRAGMA table_info(retrieval_units)")
        }
        stale = Retriever(store).search(principal, "glacier", mode="lexical")
        assert stale.hits
        assert stale.warnings and "dc index lex rebuild" in stale.warnings[0]
        store.rebuild_index(principal)
        assert (
            Retriever(store).search(principal, "glacier", mode="lexical").warnings == ()
        )


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


def test_hits_rank_by_total_score_across_tiers(tmp_path):
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
        assert [hit.score for hit in hits] == sorted(
            (hit.score for hit in hits), reverse=True
        )


def test_vector_hits_rank_by_displayed_score(tmp_path):
    pytest.importorskip("turbovec")
    principal = Principal("north", "alice")
    with LocalContextStore(tmp_path) as store:
        Ingestor(store).add_resource(
            principal,
            "docling://resources/notes/vector-order",
            _package(
                "<doclang><heading>Glacier overview</heading>"
                "<text>Glacier glacier glacier glacier</text></doclang>"
            ),
            filename="vector-order.dclx",
        )
        TurbovecIndex(store, WordVectors(), force_rebuild=True)
        hits = (
            Retriever(store, embedder=WordVectors())
            .search(principal, "glacier", mode="vector", k=10)
            .hits
        )
        assert len(hits) >= 2
        assert [hit.score for hit in hits] == sorted(
            (hit.score for hit in hits), reverse=True
        )


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
        retriever = _maintenance_retriever(store, provider)
        assert ("image", b"first image") in provider.calls
        assert ("mixed", "Rock diagram", b"second image") in provider.calls
        hit = retriever.search(principal, "", image=b"query", mode="vector", k=1).hits[
            0
        ]
        assert hit.xpath == "/doclang[1]/picture[1]"
        assert hit.asset_path == "assets/one.png"
        assert hit.text == ""
        text_only = _maintenance_retriever(store, WordVectors())
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
        retriever = _maintenance_retriever(store, WordVectors())
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
    assert (
        main([*command, "index", "vector", "rebuild", "--backend", "onnx", "--json"])
        == 0
    )
    assert selected_backends == ["onnx"]
    assert json.loads(capsys.readouterr().out)["embedded_nodes"] > 0
    assert main([*command, "index", "vector", "status"]) == 0
    vector_table = capsys.readouterr().out
    assert "Vector model" in vector_table and "word-hash-1" in vector_table
    assert main([*command, "index", "vector", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["vector_model"] == "word-hash-1"
    assert main([*command, "library", "search", "glacier"]) == 0
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
