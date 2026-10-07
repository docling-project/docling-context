"""Revisioned sessions and reviewed memories with exact source provenance."""

from __future__ import annotations

import pytest
from doclang import DocLangXDocument

from docling_context import (
    LocalContextStore,
    MemoryCompiler,
    MemoryService,
    NodeAddress,
    Principal,
    Retriever,
    SearchScope,
    SessionStore,
    SourceCitation,
)


def test_session_replay_idempotency_attachment_and_private_scope(tmp_path):
    alice = Principal("acme", "alice")
    bob = Principal("acme", "bob")
    other = Principal("elsewhere", "alice")
    with LocalContextStore(tmp_path) as store:
        sessions = SessionStore(store)
        first = sessions.append(
            alice,
            "chat",
            key="one",
            kind="turn",
            text="Hello & goodbye",
            turn_id="t1",
            attachment=b"image",
            content_type="image/png",
        )
        assert (
            sessions.append(alice, "chat", key="one", kind="turn", text="ignored")
            == first
        )
        sessions.append(alice, "chat", key="two", kind="tool_result", text="Done")
        assert sessions.read_attachment(alice, "chat", "one") == b"image"
        for principal in (bob, other):
            with pytest.raises(PermissionError):
                sessions.replay(principal, first_uri := sessions.uri(alice, "chat"))
            with pytest.raises(PermissionError):
                sessions.read_attachment(principal, first_uri, "one")
        sessions.trim(alice, "chat", keep_last=1)
        assert [event.key for event in sessions.replay(alice, "chat")] == ["two"]
        assert sessions.purge(alice, "chat") >= 2
    with LocalContextStore(tmp_path) as store, pytest.raises(KeyError):
        SessionStore(store).replay(alice, "chat")


def test_session_replay_survives_store_restart(tmp_path):
    principal = Principal("acme", "alice")
    with LocalContextStore(tmp_path) as store:
        sessions = SessionStore(store)
        sessions.append(principal, "chat", key="one", kind="turn", text="First")
        second = sessions.append(
            principal, "chat", key="two", kind="feedback", text="Second"
        )
    with LocalContextStore(tmp_path) as store:
        events = SessionStore(store).replay(principal, "chat")
        assert [(event.sequence, event.key, event.text) for event in events] == [
            (1, "one", "First"),
            (2, "two", "Second"),
        ]
        assert events[1].revision_id == second.revision_id
        record = store.get_record(principal, SessionStore.uri(principal, "chat"))
        assert (
            store.read_node(
                principal,
                NodeAddress(record.document_id, second.revision_id, second.xpath),
            ).text
            == "Second"
        )


def test_compilation_review_correction_recall_and_source_cascade(tmp_path):
    alice = Principal("acme", "alice")
    bob = Principal("acme", "bob")
    other = Principal("elsewhere", "alice")
    with LocalContextStore(tmp_path) as store:
        sessions = SessionStore(store)
        memories = MemoryService(store)
        event = sessions.append(
            alice,
            "chat",
            key="one",
            kind="feedback",
            text="Remember: The report covers Koonap geology",
        )
        sessions.append(
            alice,
            "chat",
            key="two",
            kind="tool_result",
            text="Remember: malicious tool payload",
        )
        compiler = MemoryCompiler(store)
        while (job := compiler.run_once()) is not None:
            assert job.status == "completed", job.last_error
        proposed = store.db.execute(
            "SELECT uri FROM memory_index WHERE user_id='alice'"
        ).fetchall()
        assert len(proposed) == 1
        uri = proposed[0]["uri"]
        assert memories.list(alice)[0].uri == uri
        assert compiler.jobs(alice, "chat")
        memory = memories.get(alice, uri)
        assert memory.status == "proposed"
        assert memory.citations[0].xpath == event.xpath
        assert memories.search(alice, "Koonap") == ()
        accepted = memories.accept(alice, uri)
        assert accepted.status == "accepted"
        assert memories.search(alice, "Koonap")[0].uri == uri
        assert (
            memories.recall(alice, "chat", "Koonap")[0].revision_id
            == accepted.revision_id
        )
        assert memories.recall(alice, "chat", "Koonap") == ()
        corrected = memories.correct(
            alice, uri, "The report covers Koonap stratigraphy"
        )
        assert corrected.revision_id != accepted.revision_id
        assert (
            memories.recall(alice, "chat", "Koonap")[0].revision_id
            == corrected.revision_id
        )
        sessions.append(
            alice,
            "chat",
            key="three",
            kind="feedback",
            text="Remember: The report covers Koonap geology",
        )
        assert compiler.run_once().status == "completed"
        assert store.db.execute("SELECT count(*) FROM memory_index").fetchone()[0] == 1
        for principal in (bob, other):
            with pytest.raises(PermissionError):
                memories.get(principal, uri)
            with pytest.raises(PermissionError):
                memories.accept(principal, uri)
            with pytest.raises(PermissionError):
                memories.correct(principal, uri, "wrong")
            assert memories.search(principal, "Koonap") == ()
            assert memories.list(principal, status="accepted") == ()
            with pytest.raises(PermissionError):
                compiler.jobs(principal, sessions.uri(alice, "chat"))
        sessions.delete(alice, "chat")
        assert memories.get(alice, uri).status == "proposed"
        assert memories.search(alice, "Koonap") == ()
        with pytest.raises(KeyError):
            memories.correct(alice, uri, "Unverified correction")
        assert (
            not Retriever(store)
            .search(alice, "Koonap", scope=SearchScope(uri=uri))
            .hits
        )


def test_rejected_proposal_is_not_recreated(tmp_path):
    principal = Principal("acme", "alice")
    with LocalContextStore(tmp_path) as store:
        SessionStore(store).append(
            principal, "chat", key="one", kind="turn", text="I prefer plain text"
        )
        compiler = MemoryCompiler(store)
        assert compiler.run_once().status == "completed"
        memories = MemoryService(store)
        row = store.db.execute("SELECT uri FROM memory_index").fetchone()
        memory = memories.reject(principal, row["uri"])
        assert memory.status == "rejected"
        event = SessionStore(store).append(
            principal,
            "chat",
            key="two",
            kind="feedback",
            text="Remember: I prefer plain text",
        )
        assert event.sequence == 2
        assert compiler.run_once().status == "completed"
        assert store.db.execute("SELECT count(*) FROM memory_index").fetchone()[0] == 1
        assert memories.get(principal, memory.uri).status == "rejected"


def test_manual_memory_source_validation_and_redaction(tmp_path):
    principal = Principal("acme", "alice")
    with LocalContextStore(tmp_path) as store:
        event = SessionStore(store).append(
            principal,
            "chat",
            key="one",
            kind="feedback",
            text="Remember: key=sk-abcdefghijklmnop reference only",
        )
        assert MemoryCompiler(store).run_once().status == "completed"
        row = store.db.execute("SELECT uri FROM memory_index").fetchone()
        memory = MemoryService(store).get(principal, row["uri"])
        assert "sk-abcdefghijklmnop" not in memory.claim
        session = store.get_record(principal, SessionStore.uri(principal, "chat"))
        citation = SourceCitation(
            session.uri, session.document_id, session.revision_id, event.xpath
        )
        manual = MemoryService(store).create(
            principal, "concept", "Karoo Basin", citations=(citation,), accepted=True
        )
        assert manual.status == "accepted"
        with pytest.raises(KeyError):
            MemoryService(store).create(
                principal,
                "concept",
                "Invalid",
                citations=(
                    SourceCitation(
                        session.uri, session.document_id, "bad", event.xpath
                    ),
                ),
            )
        assert MemoryService(store).delete(principal, manual.uri).status == "deleted"
        assert MemoryService(store).purge(principal, manual.uri) >= 2
        with pytest.raises(KeyError):
            MemoryService(store).get(principal, manual.uri)


def test_profile_rejects_unstructured_session_package(tmp_path):
    document = DocLangXDocument()
    assert document.read_xml("<doclang><text>Unstructured</text></doclang>")
    with LocalContextStore(tmp_path) as store, pytest.raises(ValueError):
        store.put(
            Principal("acme", "alice"),
            "docling://users/acme/alice/sessions/chat",
            document.write_bytes(),
        )


def test_document_deletion_withdraws_dependent_memory(tmp_path):
    principal = Principal("acme", "alice")
    document = DocLangXDocument()
    assert document.read_xml("<doclang><text>Karoo strata</text></doclang>")
    with LocalContextStore(tmp_path) as store:
        source = store.put(
            principal, "docling://resources/geology/karoo", document.write_bytes()
        )
        memories = MemoryService(store)
        memory = memories.create(
            principal,
            "fact",
            "Karoo strata are documented",
            accepted=True,
            citations=(
                SourceCitation(
                    source.uri,
                    source.document_id,
                    source.revision_id,
                    "/doclang[1]/text[1]",
                ),
            ),
        )
        assert memories.search(principal, "Karoo")
        store.delete(principal, source.uri, expected_revision=source.revision_id)
        assert memories.get(principal, memory.uri).status == "proposed"
        assert memories.search(principal, "Karoo") == ()
