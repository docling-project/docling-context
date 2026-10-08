"""Harness installer preserves unrelated settings and owns its entry."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from doclang import DocLangXDocument

import docling_context.integration as integration_module
from docling_context.app import main as app_main
from docling_context.ingestion import Ingestor
from docling_context.integration import IntegrationSpec, config_path, inspect, integrate
from docling_context.local import LocalContextStore
from docling_context.models import Principal


@pytest.mark.parametrize("harness", ["codex", "claude", "hermes", "pi"])
def test_user_install_upgrade_and_removal(tmp_path, harness):
    spec = IntegrationSpec(harness, home=tmp_path, store=tmp_path / "store")
    path = config_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    if harness == "codex":
        original = '[mcp_servers.other]\ncommand = "other"\n'
    elif harness == "hermes":
        original = "mcp_servers:\n  other:\n    command: other\n"
    else:
        original = json.dumps({"mcpServers": {"other": {"command": "other"}}})
    path.write_text(original)
    assert integrate(spec)["status"] == "installed"
    assert inspect(spec)["status"] == "configured"
    assert integrate(spec)["status"] == "current"
    upgraded = replace(spec, tenant="new-tenant")
    assert integrate(upgraded)["status"] == "updated"
    assert integrate(upgraded, remove=True)["status"] == "removed"
    assert "other" in path.read_text()
    assert "docling-context" not in path.read_text()


@pytest.mark.parametrize("harness", ["codex", "claude", "pi"])
def test_project_remote_uses_environment_token(tmp_path, harness):
    spec = IntegrationSpec(
        harness,
        scope="project",
        mode="remote",
        project_root=tmp_path,
        url="https://example.test/mcp",
        token_env="PRIVATE_TOKEN",
    )
    assert integrate(spec)["status"] == "installed"
    text = config_path(spec).read_text()
    assert "PRIVATE_TOKEN" in text
    assert "Bearer secret" not in text


def test_unmanaged_entry_and_unsupported_scope(tmp_path):
    spec = IntegrationSpec("pi", home=tmp_path)
    path = config_path(spec)
    path.parent.mkdir(parents=True)
    path.write_text('{"mcpServers":{"docling-context":{"command":"other"}}}')
    with pytest.raises(ValueError, match="not managed"):
        integrate(spec)
    assert "other" in path.read_text()
    with pytest.raises(ValueError, match="Hermes supports user scope"):
        integrate(IntegrationSpec("hermes", scope="project", project_root=tmp_path))


def test_failed_upgrade_restores_previous_config(tmp_path, monkeypatch):
    spec = IntegrationSpec("codex", home=tmp_path)
    integrate(spec)
    path = config_path(spec)
    before = path.read_text()
    original_write = integration_module._atomic_write

    def fail_marker(target, content):
        if target.name.endswith(".docling-context-managed.json"):
            raise OSError("simulated marker write failure")
        original_write(target, content)

    monkeypatch.setattr(integration_module, "_atomic_write", fail_marker)
    with pytest.raises(OSError, match="marker write failure"):
        integrate(replace(spec, tenant="changed"))
    assert path.read_text() == before
    assert inspect(spec)["status"] == "configured"


@pytest.mark.parametrize("harness", ["codex", "claude", "hermes", "pi"])
def test_cli_install_checks_cited_retrieval(tmp_path, harness, capsys):
    document = DocLangXDocument()
    assert document.read_xml("<doclang><text>Karoo citation</text></doclang>")
    store_path = tmp_path / "store"
    with LocalContextStore(store_path) as store:
        Ingestor(store).add_resource(
            Principal("default", "local"),
            "docling://resources/articles/karoo",
            document.write_bytes(),
            filename="karoo.dclx",
        )
    assert (
        app_main(
            [
                "integrate",
                harness,
                "--home",
                str(tmp_path / "home"),
                "--store",
                str(store_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "installed"
    assert result["verification"]["citation"]["xpath"] == "/doclang[1]/text[1]"
