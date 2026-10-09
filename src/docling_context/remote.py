"""Typed HTTP client for a deployed context service."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Self
from urllib.parse import urlparse

import httpx

from .service import (
    SCHEMA_VERSION,
    IngestRequest,
    JobStatusRequest,
    KnowledgeFactRequest,
    KnowledgeFactsRequest,
    ListRequest,
    MemoryListRequest,
    MemoryRecallRequest,
    MemoryReviewRequest,
    OutlineRequest,
    OverviewRequest,
    ProjectLinkRequest,
    ProjectRequest,
    ProjectSessionRequest,
    ResourceLinkRequest,
    ResourceLinksRequest,
    SearchRequest,
    ServiceError,
    ServiceResult,
    SessionAppendRequest,
    ShowRequest,
    TreeRequest,
)


class RemoteContextService:
    """Call the same versioned operations through authenticated HTTP."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30,
        client: httpx.Client | None = None,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        ):
            raise ValueError("remote service URL must use HTTPS or loopback HTTP")
        if not token:
            raise ValueError("remote service needs a bearer token")
        self.base_url = base_url.rstrip("/").removesuffix("/mcp")
        self.token = token
        self.timeout = timeout
        self.client = client or httpx.Client()
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _call(self, operation: str, request: Any) -> ServiceResult[Any]:
        response = self.client.post(
            f"{self.base_url}/v1/{operation}",
            json={"schema_version": SCHEMA_VERSION, "data": asdict(request)},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        payload = response.json()
        if "error" in payload:
            error = payload["error"]
            raise ServiceError(error["code"], error["message"])
        response.raise_for_status()
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ServiceError("unsupported_schema", "server schema version mismatch")
        return ServiceResult(
            payload["schema_version"], payload["operation_id"], payload["data"]
        )

    def list(self, request: ListRequest) -> ServiceResult[Any]:
        return self._call("list", request)

    def overview(self, request: OverviewRequest) -> ServiceResult[Any]:
        return self._call("overview", request)

    def project_create(self, request: ProjectRequest) -> ServiceResult[Any]:
        return self._call("project_create", request)

    def project_show(self, request: ProjectRequest) -> ServiceResult[Any]:
        return self._call("project_show", request)

    def project_resources(self, request: ProjectRequest) -> ServiceResult[Any]:
        return self._call("project_resources", request)

    def project_link(self, request: ProjectLinkRequest) -> ServiceResult[Any]:
        return self._call("project_link", request)

    def project_unlink(self, request: ProjectLinkRequest) -> ServiceResult[Any]:
        return self._call("project_unlink", request)

    def resource_links(self, request: ResourceLinksRequest) -> ServiceResult[Any]:
        return self._call("resource_links", request)

    def resource_projects(self, request: ResourceLinksRequest) -> ServiceResult[Any]:
        return self._call("resource_projects", request)

    def resource_link(self, request: ResourceLinkRequest) -> ServiceResult[Any]:
        return self._call("resource_link", request)

    def knowledge_facts(self, request: KnowledgeFactsRequest) -> ServiceResult[Any]:
        return self._call("knowledge_facts", request)

    def knowledge_add_fact(self, request: KnowledgeFactRequest) -> ServiceResult[Any]:
        return self._call("knowledge_add_fact", request)

    def tree(self, request: TreeRequest) -> ServiceResult[Any]:
        return self._call("tree", request)

    def outline(self, request: OutlineRequest) -> ServiceResult[Any]:
        return self._call("outline", request)

    def show(self, request: ShowRequest) -> ServiceResult[Any]:
        return self._call("show", request)

    def search(self, request: SearchRequest) -> ServiceResult[Any]:
        return self._call("search", request)

    def ingest(self, request: IngestRequest) -> ServiceResult[Any]:
        return self._call("ingest", request)

    def job_status(self, request: JobStatusRequest) -> ServiceResult[Any]:
        return self._call("job_status", request)

    def session_append(self, request: SessionAppendRequest) -> ServiceResult[Any]:
        return self._call("session_append", request)

    def project_session_start(
        self, request: ProjectSessionRequest
    ) -> ServiceResult[Any]:
        return self._call("project_session_start", request)

    def project_session_close(
        self, request: ProjectSessionRequest
    ) -> ServiceResult[Any]:
        return self._call("project_session_close", request)

    def memory_list(self, request: MemoryListRequest) -> ServiceResult[Any]:
        return self._call("memory_list", request)

    def memory_review(self, request: MemoryReviewRequest) -> ServiceResult[Any]:
        return self._call("memory_review", request)

    def memory_recall(self, request: MemoryRecallRequest) -> ServiceResult[Any]:
        return self._call("memory_recall", request)
