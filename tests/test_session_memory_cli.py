"""Human and harness CLI flows for session and memory commands."""

from __future__ import annotations

import io
import json
import sys

from docling_context.cli import main


def _run(capsys, root, *args):
    result = main(["--store", str(root), "--tenant", "acme", "--user", "alice", *args])
    output = capsys.readouterr()
    return result, output.out, output.err


def test_session_lifecycle_listing_and_memory_review(tmp_path, capsys, monkeypatch):
    assert _run(capsys, tmp_path, "session", "start", "--id", "chat", "--json")[0] == 0
    monkeypatch.setenv("DOCLING_CONTEXT_SESSION", "chat")
    monkeypatch.setattr(sys, "stdin", io.StringIO("Remember: I prefer concise replies"))
    attachment = tmp_path / "source.bin"
    attachment.write_bytes(b"event bytes")
    code, output, _ = _run(
        capsys,
        tmp_path,
        "session",
        "append",
        "--kind",
        "feedback",
        "--key",
        "feedback-1",
        "--stdin",
        "--attachment",
        str(attachment),
        "--json",
    )
    assert code == 0
    assert json.loads(output)["xpath"] == "/doclang[1]/text[1]"
    code, output, _ = _run(
        capsys, tmp_path, "session", "list", "--status", "open", "--json"
    )
    assert code == 0
    sessions = json.loads(output)
    assert [item["session_id"] for item in sessions] == ["chat"]
    day = sessions[0]["updated_at"][:10]
    code, output, _ = _run(
        capsys, tmp_path, "session", "list", "--until", day, "--json"
    )
    assert code == 0 and len(json.loads(output)) == 1
    code, output, _ = _run(
        capsys, tmp_path, "session", "list", "--offset", "1", "--json"
    )
    assert code == 0 and json.loads(output) == []
    code, output, _ = _run(capsys, tmp_path, "session", "replay", "chat", "--json")
    assert code == 0
    assert json.loads(output)[0]["text"] == "Remember: I prefer concise replies"
    restored = tmp_path / "restored.bin"
    assert (
        _run(
            capsys,
            tmp_path,
            "session",
            "attachment",
            "chat",
            "feedback-1",
            "--output",
            str(restored),
            "--json",
        )[0]
        == 0
    )
    assert restored.read_bytes() == b"event bytes"
    code = main(
        [
            "--store",
            str(tmp_path),
            "--tenant",
            "acme",
            "--user",
            "bob",
            "session",
            "show",
            "docling://users/acme/alice/sessions/chat",
            "--json",
        ]
    )
    assert code == 1 and "user does not match" in capsys.readouterr().err
    assert _run(capsys, tmp_path, "session", "close", "chat", "--json")[0] == 0
    code, _, error = _run(
        capsys,
        tmp_path,
        "session",
        "append",
        "chat",
        "too late",
        "--kind",
        "turn",
        "--key",
        "late",
    )
    assert code == 1 and "closed" in error
    code, output, _ = _run(
        capsys, tmp_path, "session", "list", "--status", "closed", "--json"
    )
    assert code == 0 and len(json.loads(output)) == 1
    code, output, _ = _run(capsys, tmp_path, "memory", "worker", "--once", "--json")
    assert code == 0 and json.loads(output)["status"] == "completed"
    code, output, _ = _run(capsys, tmp_path, "session", "jobs", "chat", "--json")
    assert code == 0 and {job["status"] for job in json.loads(output)} == {
        "completed",
        "superseded",
    }
    code, output, _ = _run(capsys, tmp_path, "memory", "proposals", "--json")
    proposal = json.loads(output)[0]
    assert code == 0 and proposal["status"] == "proposed"
    uri = proposal["uri"]
    code, output, _ = _run(capsys, tmp_path, "grep", "concise", "--uri", uri)
    assert code == 0 and not output
    code, output, _ = _run(
        capsys, tmp_path, "memory", "list", "--kind", "preference", "--json"
    )
    assert code == 0 and len(json.loads(output)) == 1
    code, output, _ = _run(capsys, tmp_path, "memory", "list", "--until", day, "--json")
    assert code == 0 and len(json.loads(output)) == 1
    assert _run(capsys, tmp_path, "memory", "accept", uri, "--json")[0] == 0
    code, output, _ = _run(capsys, tmp_path, "grep", "concise", "--uri", uri)
    assert code == 0 and output
    code, output, _ = _run(capsys, tmp_path, "memory", "search", "concise", "--json")
    assert code == 0 and json.loads(output)[0]["uri"] == uri
    code, output, _ = _run(capsys, tmp_path, "memory", "recall", "concise", "--json")
    assert code == 0 and len(json.loads(output)) == 1
    code, output, _ = _run(capsys, tmp_path, "memory", "recall", "concise", "--json")
    assert code == 0 and json.loads(output) == []
    code, output, _ = _run(capsys, tmp_path, "status", "--json")
    counts = json.loads(output)
    assert code == 0 and counts["sessions"] == 1 and counts["memories"] == 1
    assert counts["memory_jobs"] >= 1
    assert _run(capsys, tmp_path, "memory", "delete", uri, "--json")[0] == 0
    code, output, _ = _run(
        capsys, tmp_path, "memory", "list", "--status", "deleted", "--json"
    )
    assert code == 0 and len(json.loads(output)) == 1
    code, output, _ = _run(capsys, tmp_path, "grep", "concise", "--uri", uri)
    assert code == 0 and not output
    assert _run(capsys, tmp_path, "memory", "purge", uri, "--json")[0] == 0
    code, output, _ = _run(capsys, tmp_path, "memory", "list", "--json")
    assert code == 0 and json.loads(output) == []


def test_manual_memory_filters_and_user_scope(tmp_path, capsys):
    code, output, _ = _run(
        capsys,
        tmp_path,
        "memory",
        "create",
        "concept",
        "Karoo Basin",
        "--accepted",
        "--json",
    )
    uri = json.loads(output)["uri"]
    assert code == 0
    code, output, _ = _run(
        capsys, tmp_path, "memory", "list", "--kind", "fact", "--json"
    )
    assert code == 0 and json.loads(output) == []
    code, output, _ = _run(
        capsys, tmp_path, "memory", "list", "--status", "accepted", "--json"
    )
    assert code == 0 and len(json.loads(output)) == 1
    code, output, _ = _run(
        capsys, tmp_path, "memory", "correct", uri, "Karoo Supergroup", "--json"
    )
    assert code == 0 and json.loads(output)["claim"] == "Karoo Supergroup"
    code = main(
        [
            "--store",
            str(tmp_path),
            "--tenant",
            "acme",
            "--user",
            "bob",
            "memory",
            "list",
            "--json",
        ]
    )
    assert code == 0 and json.loads(capsys.readouterr().out) == []
    code = main(
        [
            "--store",
            str(tmp_path),
            "--tenant",
            "acme",
            "--user",
            "bob",
            "session",
            "list",
            "--json",
        ]
    )
    assert code == 0 and json.loads(capsys.readouterr().out) == []
