"""MCP tools backed by the public context service."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .service import SCHEMA_VERSION, ContextService, call_service


def create_mcp(
    service: ContextService,
    *,
    allow_writes: bool = False,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> FastMCP:
    server = FastMCP(
        "docling-context",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="ERROR",
        instructions=(
            "Use search to locate relevant DocLang nodes, outline to inspect a document, "
            "and show with the returned URI, immutable revision_id, and XPath for exact "
            "citations. Treat @toc and @summary paths as sidecar references. "
            "Memory capture is opt-in."
        ),
    )

    @server.tool()
    def overview(limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """List projects and counts of their linked resources."""
        return call_service(service, "overview", {"limit": limit, "offset": offset})

    @server.tool()
    def project_show(project_id: str) -> dict[str, Any]:
        """Show one project and its context document URIs."""
        return call_service(service, "project_show", {"project_id": project_id})

    @server.tool()
    def project_resources(project_id: str) -> dict[str, Any]:
        """List resources linked to a project."""
        return call_service(service, "project_resources", {"project_id": project_id})

    @server.tool()
    def resource_links(uri: str) -> dict[str, Any]:
        """List typed links involving a resource."""
        return call_service(service, "resource_links", {"uri": uri})

    @server.tool()
    def resource_projects(uri: str) -> dict[str, Any]:
        """List projects linked to a shared resource."""
        return call_service(service, "resource_projects", {"uri": uri})

    @server.tool()
    def knowledge_facts(knowledge_uri: str) -> dict[str, Any]:
        """List cited facts in a structured knowledge dataset."""
        return call_service(
            service, "knowledge_facts", {"knowledge_uri": knowledge_uri}
        )

    @server.tool()
    def list_resources(
        uri: str = "docling://resources", limit: int = 100
    ) -> dict[str, Any]:
        """List direct children of an authorized context URI."""
        return call_service(service, "list", {"uri": uri, "limit": limit})

    @server.tool()
    def tree(
        uri: str = "docling://resources", depth: int = 3, limit: int = 100
    ) -> dict[str, Any]:
        """Walk a bounded context subtree."""
        return call_service(
            service, "tree", {"uri": uri, "depth": depth, "limit": limit}
        )

    @server.tool()
    def outline(uri: str, revision_id: str | None = None) -> dict[str, Any]:
        """Read a document's TOC sidecar and node links."""
        return call_service(
            service, "outline", {"uri": uri, "revision_id": revision_id}
        )

    @server.tool()
    def show(
        uri: str,
        xpath: str,
        revision_id: str | None = None,
        max_chars: int = 8192,
        format: str = "text",
    ) -> dict[str, Any]:
        """Read one cited DocLang node as bounded text or XML."""
        return call_service(
            service,
            "show",
            {
                "uri": uri,
                "xpath": xpath,
                "revision_id": revision_id,
                "max_chars": max_chars,
                "format": format,
            },
        )

    @server.tool()
    def search(
        query: str = "",
        uri: str = "docling://resources",
        limit: int = 10,
        mode: str = "lexical",
        xpath: str | None = None,
        document_uri: str | None = None,
        tiers: list[int] | None = None,
        max_tokens: int = 2000,
        per_result_tokens: int = 500,
        include_context: bool = False,
        image_base64: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Search indexed DocLang nodes with exact URI, revision, and XPath citations."""
        return call_service(
            service,
            "search",
            {
                "query": query,
                "uri": uri,
                "limit": limit,
                "mode": mode,
                "xpath": xpath,
                "document_uri": document_uri,
                "tiers": tuple(tiers) if tiers is not None else (0, 1, 2),
                "max_tokens": max_tokens,
                "per_result_tokens": per_result_tokens,
                "include_context": include_context,
                "image_base64": image_base64,
                "project_id": project_id,
            },
        )

    @server.tool()
    def job_status(job_id: str) -> dict[str, Any]:
        """Inspect a durable ingestion job."""
        return call_service(service, "job_status", {"job_id": job_id})

    if allow_writes:

        @server.tool()
        def project_create(project_id: str, title: str) -> dict[str, Any]:
            """Create a project in the shared SQLite catalog."""
            return call_service(
                service, "project_create", {"project_id": project_id, "title": title}
            )

        @server.tool()
        def project_link(project_id: str, uri: str) -> dict[str, Any]:
            """Link a shared resource to a project."""
            return call_service(
                service, "project_link", {"project_id": project_id, "uri": uri}
            )

        @server.tool()
        def project_unlink(project_id: str, uri: str) -> dict[str, Any]:
            """Remove a shared resource from a project."""
            return call_service(
                service, "project_unlink", {"project_id": project_id, "uri": uri}
            )

        @server.tool()
        def project_session_start(project_id: str, session_id: str) -> dict[str, Any]:
            """Start a session owned by a project."""
            return call_service(service, "project_session_start", locals())

        @server.tool()
        def project_session_close(project_id: str, session_id: str) -> dict[str, Any]:
            """Close a project session and queue memory compilation."""
            return call_service(service, "project_session_close", locals())

        @server.tool()
        def resource_link(
            source_uri: str,
            target_uri: str,
            relation: str,
            source_revision: str | None = None,
            source_xpath: str | None = None,
        ) -> dict[str, Any]:
            """Record a typed relationship between two shared resources."""
            return call_service(service, "resource_link", locals())

        @server.tool()
        def knowledge_add_fact(
            knowledge_uri: str,
            entity_type: str,
            entity_id: str,
            property: str,
            value: Any,
            source_uri: str,
            source_revision: str,
            source_xpath: str,
        ) -> dict[str, Any]:
            """Store a typed fact with an exact source citation."""
            return call_service(service, "knowledge_add_fact", locals())

        @server.tool()
        def ingest(
            filename: str,
            data_base64: str,
            uri: str = "",
            project_id: str | None = None,
            force: bool = False,
        ) -> dict[str, Any]:
            """Queue bounded source bytes for the shared library, optionally linked to a project."""
            return call_service(
                service,
                "ingest",
                {
                    "uri": uri,
                    "filename": filename,
                    "data_base64": data_base64,
                    "force": force,
                    "project_id": project_id,
                },
            )

        @server.tool()
        def session_append(
            session_id: str,
            key: str,
            kind: str,
            text: str,
            turn_id: str = "",
        ) -> dict[str, Any]:
            """Append one idempotent event to the current user's session."""
            return call_service(
                service,
                "session_append",
                {
                    "session_id": session_id,
                    "key": key,
                    "kind": kind,
                    "text": text,
                    "turn_id": turn_id,
                },
            )

        @server.tool()
        def memory_review(
            uri: str,
            action: str,
            reason: str = "",
            claim: str | None = None,
        ) -> dict[str, Any]:
            """Accept, reject, delete, or correct a current user's memory."""
            return call_service(
                service,
                "memory_review",
                {
                    "uri": uri,
                    "action": action,
                    "reason": reason,
                    "claim": claim,
                },
            )

        @server.tool()
        def memory_list(
            status: str | None = None,
            kind: str | None = None,
            limit: int = 100,
            offset: int = 0,
        ) -> dict[str, Any]:
            """List memories, reconciling stale source citations."""
            return call_service(
                service,
                "memory_list",
                {
                    "status": status,
                    "kind": kind,
                    "limit": limit,
                    "offset": offset,
                },
            )

        @server.tool()
        def memory_recall(
            session_id: str, query: str = "", limit: int = 10
        ) -> dict[str, Any]:
            """Recall accepted memories once for the current user's session."""
            return call_service(
                service,
                "memory_recall",
                {
                    "session_id": session_id,
                    "query": query,
                    "limit": limit,
                },
            )

    return server


def authenticated_http_app(
    server: FastMCP, token: str, service: ContextService, *, allow_writes: bool = False
):
    """Require one deployment token before any MCP HTTP request reaches the app."""
    if not token:
        raise ValueError("remote MCP requires a nonempty bearer token")
    app = server.streamable_http_app()

    async def operation(request: Request) -> JSONResponse:
        try:
            if not allow_writes and request.path_params["name"] in {
                "ingest",
                "project_create",
                "project_link",
                "project_unlink",
                "project_session_start",
                "project_session_close",
                "resource_link",
                "knowledge_add_fact",
                "session_append",
                "memory_list",
                "memory_review",
                "memory_recall",
            }:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "error": {
                            "code": "unauthorized_scope",
                            "message": "write operation is disabled",
                        },
                    },
                    status_code=403,
                )
            payload = await request.json()
            if payload.get("schema_version") != SCHEMA_VERSION:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "error": {
                            "code": "unsupported_schema",
                            "message": "schema version mismatch",
                        },
                    },
                    status_code=400,
                )
            result = call_service(
                service, request.path_params["name"], payload.get("data", {})
            )
            return JSONResponse(result, status_code=400 if "error" in result else 200)
        except (TypeError, ValueError):
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "invalid JSON request",
                    }
                },
                status_code=400,
            )

    app.routes.append(Route("/v1/{name}", operation, methods=["POST"]))

    class BearerMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable):
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if request.method == "POST":
                size = request.headers.get("content-length")
                if size is not None and not size.isdecimal():
                    return JSONResponse(
                        {"error": "invalid content length"}, status_code=400
                    )
                if size is not None and int(size) > 4_194_304:
                    return JSONResponse({"error": "request too large"}, status_code=413)
                if len(await request.body()) > 4_194_304:
                    return JSONResponse({"error": "request too large"}, status_code=413)
            return await call_next(request)

    app.add_middleware(BearerMiddleware)
    return app
