"""Project agent sessions and owned agent skill installation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from docling_context.agent_bridge import (
    inspect_agent,
    install_agent,
    remove_agent,
    skill_path,
)
from docling_context.catalog import Catalog
from docling_context.cli import main
from docling_context.integration import IntegrationSpec
from docling_context.local import LocalContextStore
from docling_context.models import Principal
from docling_context.sessions import SessionStore


@pytest.mark.parametrize("agent", ["codex", "claude", "hermes", "pi"])
def test_agent_install_is_owned_and_idempotent(tmp_path, agent):
    spec = IntegrationSpec(
        agent,
        scope="user",
        home=tmp_path / "home",
        project_root=tmp_path / "project",
        store=tmp_path / "store",
        allow_writes=True,
    )
    assert install_agent(spec)["status"] == "installed"
    skill = skill_path(spec)
    assert skill.exists() and "project_session_start" in skill.read_text()
    assert inspect_agent(spec)["status"] == "configured"
    assert install_agent(spec)["status"] == "current"
    assert remove_agent(spec)["status"] == "removed"
    assert not skill.exists()


def test_agent_install_preserves_unmanaged_skill(tmp_path):
    spec = IntegrationSpec(
        "codex",
        scope="project",
        project_root=tmp_path,
        home=tmp_path / "home",
    )
    path = skill_path(spec)
    path.parent.mkdir(parents=True)
    path.write_text("personal skill")
    with pytest.raises(ValueError, match="not managed"):
        install_agent(spec)
    assert path.read_text() == "personal skill"


def test_agent_cli_install_status_remove(tmp_path, capsys):
    args = [
        "--store",
        str(tmp_path / "store"),
        "agent",
    ]
    options = ["codex", "--scope", "project", "--project-root", str(tmp_path)]
    assert main([*args, "install", *options, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "installed"
    assert main([*args, "status", *options, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "configured"
    assert main([*args, "remove", *options, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "removed"


def test_start_no_launch_requires_project_and_records_agent(tmp_path, capsys):
    principal = Principal("default", "local")
    with LocalContextStore(tmp_path) as store:
        Catalog(store).create_project(principal, "materials", "Materials")
    args = [
        "--store",
        str(tmp_path),
        "session",
        "start",
        "--project",
        "materials",
        "--agent",
        "codex",
        "--no-launch",
        "--id",
        "review",
        "--json",
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["uri"] == "docling://projects/materials/sessions/review"
    assert result["agent"] == "codex"
    with LocalContextStore(tmp_path) as store:
        assert SessionStore(store).get(principal, result["uri"]).status == "open"
        assert (
            Catalog(store).project_sessions(principal, "materials")[0]["agent"]
            == "codex"
        )
    assert (
        main(
            [
                "--store",
                str(tmp_path),
                "session",
                "list",
                "--project",
                "materials",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)[0]["agent"] == "codex"


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ("codex", ["codex"]),
        ("claude", ["claude", "--append-system-prompt"]),
        ("hermes", ["hermes", "chat", "-q"]),
        ("pi", ["pi", "--append-system-prompt"]),
    ],
)
def test_launcher_waits_and_closes(tmp_path, monkeypatch, capsys, agent, expected):
    from docling_context import cli

    principal = Principal("default", "local")
    with LocalContextStore(tmp_path) as store:
        Catalog(store).create_project(principal, "materials", "Materials")
    monkeypatch.setattr(cli.shutil, "which", lambda name: name)
    installs = []

    def fake_install(spec):
        installs.append(spec)
        return {"status": "installed"}

    monkeypatch.setattr(cli, "install_agent", fake_install)
    calls = []

    def fake_run(command, *, env, check):
        calls.append((command, env))
        with LocalContextStore(tmp_path) as store:
            assert (
                SessionStore(store)
                .get(principal, env["DOCLING_CONTEXT_SESSION"])
                .status
                == "open"
            )
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert (
        main(
            [
                "--store",
                str(tmp_path),
                "session",
                "start",
                "--project",
                "materials",
                "--agent",
                agent,
                "--id",
                "review",
                "--json",
            ]
        )
        == 7
    )
    capsys.readouterr()
    command, env = calls[0]
    assert command[: len(expected)] == expected
    assert installs[0].scope == "user"
    assert env["DOCLING_CONTEXT_PROJECT"] == "materials"
    assert (
        env["DOCLING_CONTEXT_SESSION"] == "docling://projects/materials/sessions/review"
    )
    with LocalContextStore(tmp_path) as store:
        assert (
            SessionStore(store).get(principal, env["DOCLING_CONTEXT_SESSION"]).status
            == "closed"
        )
        assert (
            Catalog(store).project_sessions(principal, "materials")[0]["agent"] == agent
        )


def test_launcher_closes_after_process_error(tmp_path, monkeypatch, capsys):
    from docling_context import cli

    principal = Principal("default", "local")
    with LocalContextStore(tmp_path) as store:
        Catalog(store).create_project(principal, "materials", "Materials")
    monkeypatch.setattr(cli.shutil, "which", lambda name: name)
    monkeypatch.setattr(cli, "install_agent", lambda spec: {"status": "installed"})

    def fail(*args, **kwargs):
        raise OSError("agent failed to start")

    monkeypatch.setattr(cli.subprocess, "run", fail)
    assert (
        main(
            [
                "--store",
                str(tmp_path),
                "session",
                "start",
                "--project",
                "materials",
                "--agent",
                "codex",
                "--id",
                "review",
            ]
        )
        == 1
    )
    assert "agent failed to start" in capsys.readouterr().err
    with LocalContextStore(tmp_path) as store:
        assert (
            SessionStore(store)
            .get(principal, "docling://projects/materials/sessions/review")
            .status
            == "closed"
        )
