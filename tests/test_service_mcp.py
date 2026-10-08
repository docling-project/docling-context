"""Public service, MCP, and durable import behavior."""

from __future__ import annotations

import asyncio
import base64
import json

from doclang import DocLangXDocument
from starlette.testclient import TestClient

from docling_context.cli import main as dc_main
from docling_context.ingest_queue import IngestQueue
from docling_context.ingestion import Ingestor
from docling_context.local import LocalContextStore
from docling_context.mcp_server import authenticated_http_app, create_mcp
from docling_context.models import Principal
from docling_context.remote import RemoteContextService
from docling_context.service import (
    ContextService,
    IngestRequest,
    JobStatusRequest,
    ListRequest,
    OutlineRequest,
    SearchRequest,
    ShowRequest,
    call_service,
)

URI = "docling://resources/articles/karoo"
PRINCIPAL = Principal("tenant-a", "alice")


def _package() -> bytes:
    document = DocLangXDocument()
    assert document.read_xml(
        "<doclang><heading>Karoo</heading><text>Koonap Formation</text></doclang>"
    )
    return document.write_bytes()


def test_service_citations_survive_worker_restart(tmp_path, capsys):
    with LocalContextStore(tmp_path) as store:
        service = ContextService(store, PRINCIPAL)
        queued = service.ingest(
            IngestRequest(URI, "karoo.dclx", base64.b64encode(_package()).decode())
        ).data
        assert queued["status"] == "queued"
        assert store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    with LocalContextStore(tmp_path) as store:
        finished = IngestQueue(store).run_once()
        assert finished is not None and finished.status == "completed"
        service = ContextService(store, PRINCIPAL)
        job = service.job_status(JobStatusRequest(queued["job_id"])).data
        assert job["revision_id"] == finished.revision_id
        assert (
            dc_main(
                [
                    "--store",
                    str(tmp_path),
                    "--tenant",
                    PRINCIPAL.tenant_id,
                    "--user",
                    PRINCIPAL.user_id,
                    "jobs",
                    "status",
                    queued["job_id"],
                    "--json",
                ]
            )
            == 0
        )
        assert (
            json.loads(capsys.readouterr().out)["revision_id"] == finished.revision_id
        )
        outline = service.outline(OutlineRequest(URI)).data
        assert outline["links"] == ["/doclang[1]/heading[1]"]
        search = service.search(
            SearchRequest("Koonap", uri=URI, include_context=True)
        ).data
        assert search["hits"][0]["revision_id"] == finished.revision_id
        assert "Koonap Formation" in search["context"]
        assert (
            service.show(
                ShowRequest(URI, "@toc:/doclang[1]/toc[1]/entry[1]/description[1]")
            ).data["content"]
            == "Karoo"
        )
        assert (
            "Karoo"
            in service.show(ShowRequest(URI, "@summary:/doclang[1]/text[1]")).data[
                "content"
            ]
        )
        citation = service.show(
            ShowRequest(URI, "/doclang[1]/text[1]", finished.revision_id)
        ).data
        assert citation["content"] == "Koonap Formation"
        assert (
            dc_main(
                [
                    "--store",
                    str(tmp_path),
                    "--tenant",
                    PRINCIPAL.tenant_id,
                    "--user",
                    PRINCIPAL.user_id,
                    "show",
                    URI,
                    "/doclang[1]/text[1]",
                    "--revision",
                    finished.revision_id,
                    "--json",
                ]
            )
            == 0
        )
        cli_citation = json.loads(capsys.readouterr().out)
        assert {
            key: cli_citation[key]
            for key in ("uri", "revision_id", "xpath", "content", "truncated")
        } == {
            key: citation[key]
            for key in ("uri", "revision_id", "xpath", "content", "truncated")
        }
        assert (
            "<text>Koonap Formation</text>"
            in service.show(ShowRequest(URI, "/doclang[1]/text[1]", format="xml")).data[
                "content"
            ]
        )
        assert (
            call_service(
                service, "show", {"uri": URI, "xpath": "/doclang[1]/text[99]"}
            )["error"]["code"]
            == "stale_citation"
        )
        assert (
            call_service(service, "show", {"uri": URI, "xpath": "//text"})["error"][
                "code"
            ]
            == "invalid_xpath"
        )
        assert (
            call_service(
                service,
                "show",
                {"uri": URI, "xpath": "/doclang[1]/text[1]", "max_chars": 100_000},
            )["error"]["code"]
            == "budget_exceeded"
        )
        assert (
            call_service(
                service, "list", {"uri": "docling://users/tenant-a/bob/memories"}
            )["error"]["code"]
            == "unauthorized_scope"
        )
        assert (
            call_service(service, "search", {"query": "Karoo", "image_base64": "bad!"})[
                "error"
            ]["code"]
            == "invalid_request"
        )


def test_mcp_tools_and_http_token(tmp_path):
    with LocalContextStore(tmp_path) as store:
        record = Ingestor(store).add_resource(
            PRINCIPAL, URI, _package(), filename="karoo.dclx"
        )
        service = ContextService(store, PRINCIPAL)
        server = create_mcp(service)
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert {"search", "outline", "show", "tree", "list_resources"} <= names
        assert "ingest" not in names
        writable = create_mcp(service, allow_writes=True)
        assert "ingest" in {tool.name for tool in asyncio.run(writable.list_tools())}
        result = asyncio.run(
            server.call_tool(
                "show",
                {
                    "uri": URI,
                    "xpath": "/doclang[1]/text[1]",
                    "revision_id": record.revision_id,
                },
            )
        )
        assert isinstance(result, tuple)
        assert result[1]["data"]["content"] == "Koonap Formation"
        app = authenticated_http_app(server, "secret", service)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            assert client.post("/mcp", json={}).status_code == 401
            assert (
                client.post(
                    "/mcp", headers={"Authorization": "Bearer wrong"}, json={}
                ).status_code
                == 401
            )
            assert client.post("/v1/list", json={}).status_code == 401
            denied = client.post(
                "/v1/ingest",
                headers={"Authorization": "Bearer secret"},
                json={"schema_version": 1, "data": {}},
            )
            assert denied.status_code == 403
            mcp_headers = {
                "Authorization": "Bearer secret",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            initialized = client.post(
                "/mcp",
                headers=mcp_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200
            assert (
                initialized.json()["result"]["serverInfo"]["name"] == "docling-context"
            )
            remote = RemoteContextService(
                "https://example.test", "secret", client=client
            )
            assert remote.list(ListRequest()).data == service.list(ListRequest()).data


def test_failed_ingest_reports_stable_conversion_code(tmp_path):
    with LocalContextStore(tmp_path) as store:
        service = ContextService(store, PRINCIPAL)
        queued = service.ingest(
            IngestRequest(URI, "broken.dclx", base64.b64encode(b"broken").decode())
        ).data
        assert IngestQueue(store).run_once().status == "failed"
        status = service.job_status(JobStatusRequest(queued["job_id"])).data
        assert status["error_code"] == "conversion_failure"
