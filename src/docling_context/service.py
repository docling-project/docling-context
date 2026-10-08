"""Versioned, principal-bound operations shared by the SDK and MCP surfaces."""

from __future__ import annotations

import base64
import binascii
import builtins
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any

from .durable_memory import MemoryService
from .ingest_queue import IngestQueue
from .jobs import CollectionWorker
from .local import LocalContextStore, RecordMissing
from .models import NodeAddress, Principal
from .package import MAX_NODE_TEXT_CHARS, PackageError, PackageMissing
from .retrieval import Retriever, SearchScope
from .sessions import SessionStore
from .uri import AccessDenied, InvalidURI, authorize_uri, parse_uri

SCHEMA_VERSION = 1
_LOG = logging.getLogger(__name__)
_XPATH_PART = re.compile(r"([A-Za-z][A-Za-z0-9_.-]*)\[([1-9][0-9]*)\]")


@dataclass(frozen=True, slots=True)
class ServiceResult[T]:
    schema_version: int
    operation_id: str
    data: T

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True, slots=True)
class ListRequest:
    uri: str = "docling://resources"
    limit: int = 100


@dataclass(frozen=True, slots=True)
class TreeRequest:
    uri: str = "docling://resources"
    depth: int = 3
    limit: int = 100


@dataclass(frozen=True, slots=True)
class OutlineRequest:
    uri: str
    revision_id: str | None = None
    limit: int = 256


@dataclass(frozen=True, slots=True)
class ShowRequest:
    uri: str
    xpath: str
    revision_id: str | None = None
    max_chars: int = 8_192
    format: str = "text"


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str = ""
    uri: str = "docling://resources"
    xpath: str | None = None
    document_uri: str | None = None
    tiers: tuple[int, ...] = (0, 1, 2)
    limit: int = 10
    mode: str = "lexical"
    max_tokens: int = 2_000
    per_result_tokens: int = 500
    include_context: bool = False
    image_base64: str | None = None


@dataclass(frozen=True, slots=True)
class IngestRequest:
    uri: str
    filename: str
    data_base64: str
    force: bool = False


@dataclass(frozen=True, slots=True)
class JobStatusRequest:
    job_id: str


@dataclass(frozen=True, slots=True)
class SessionAppendRequest:
    session_id: str
    key: str
    kind: str
    text: str
    turn_id: str = ""


@dataclass(frozen=True, slots=True)
class MemoryListRequest:
    status: str | None = None
    kind: str | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True, slots=True)
class MemoryReviewRequest:
    uri: str
    action: str
    reason: str = ""
    claim: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRecallRequest:
    session_id: str
    query: str = ""
    limit: int = 10


def _xml_node(xml: str, xpath: str) -> str:
    parts = xpath.strip("/").split("/")
    if not parts or parts[0] != "doclang[1]" or len(parts) > 256:
        raise ServiceError("invalid_xpath", "XPath must begin with /doclang[1]")
    root = ET.fromstring(xml)
    current = root
    for part in parts[1:]:
        match = _XPATH_PART.fullmatch(part)
        if match is None:
            raise ServiceError("invalid_xpath", "XPath has an invalid segment")
        name, ordinal = match.groups()
        children = [child for child in current if child.tag.rsplit("}", 1)[-1] == name]
        index = int(ordinal) - 1
        if index >= len(children):
            raise ServiceError("stale_citation", "XPath is absent from this revision")
        current = children[index]
    return ET.tostring(current, encoding="unicode")


def _validate_xpath(xpath: str) -> None:
    parts = xpath.strip("/").split("/")
    if (
        not xpath.startswith("/doclang[1]")
        or len(xpath) > 2_048
        or len(parts) > 256
        or parts[0] != "doclang[1]"
        or any(_XPATH_PART.fullmatch(part) is None for part in parts[1:])
    ):
        raise ServiceError("invalid_xpath", "invalid or oversized XPath")


class ContextService:
    """Bind an authenticated principal before accepting operation requests."""

    def __init__(self, store: LocalContextStore, principal: Principal):
        self.store = store
        self.principal = principal
        self._lock = RLock()

    @staticmethod
    def _result[T](data: T) -> ServiceResult[T]:
        return ServiceResult(SCHEMA_VERSION, uuid.uuid4().hex, data)

    def _record(self, uri: str, revision_id: str | None = None):
        address = parse_uri(uri)
        authorize_uri(self.principal, address)
        current = self.store.get_record(self.principal, address.value)
        if revision_id is None or revision_id == current.revision_id:
            return current
        return self.store.get_revision(
            self.principal,
            NodeAddress(current.document_id, revision_id, "/doclang[1]"),
        )

    def list(
        self, request: ListRequest
    ) -> ServiceResult[builtins.list[dict[str, Any]]]:
        if not 1 <= request.limit <= 1_000:
            raise ServiceError("budget_exceeded", "list limit must be 1 to 1000")
        return self._result(
            [
                asdict(item)
                for item in self.store.list_children(
                    self.principal, request.uri, request.limit
                )
            ]
        )

    def tree(
        self, request: TreeRequest
    ) -> ServiceResult[builtins.list[dict[str, Any]]]:
        if not 0 <= request.depth <= 10 or not 1 <= request.limit <= 1_000:
            raise ServiceError("budget_exceeded", "tree depth or limit is too large")
        return self._result(
            [
                asdict(item)
                for item in self.store.walk(
                    self.principal, request.uri, request.depth, request.limit
                )
            ]
        )

    def outline(self, request: OutlineRequest) -> ServiceResult[dict[str, Any]]:
        if not 1 <= request.limit <= 256:
            raise ServiceError("budget_exceeded", "outline limit must be 1 to 256")
        record = self._record(request.uri, request.revision_id)
        from .package import load_package

        document = load_package(self.store.packages.get(record.package.sha256))
        toc = document.toc()
        links = []
        if toc is not None:
            for entry in ET.fromstring(toc.xml()).iter("entry"):
                target = entry.get("xpath")
                if target:
                    links.append(target)
        xml = toc.xml() if toc is not None else ""
        return self._result(
            {
                "uri": record.uri,
                "document_id": record.document_id,
                "revision_id": record.revision_id,
                "toc_xml": xml[:65_536],
                "links": links[: request.limit],
                "truncated": len(xml) > 65_536 or len(links) > request.limit,
            }
        )

    def show(self, request: ShowRequest) -> ServiceResult[dict[str, Any]]:
        if not 1 <= request.max_chars <= MAX_NODE_TEXT_CHARS:
            raise ServiceError("budget_exceeded", "show max_chars is out of bounds")
        if request.format not in {"text", "xml"}:
            raise ServiceError("invalid_request", "format must be text or xml")
        sidecar = None
        xpath = request.xpath
        for name in ("summary", "toc"):
            if xpath.startswith(f"@{name}:"):
                sidecar = name
                xpath = xpath[len(name) + 2 :]
                break
        _validate_xpath(xpath)
        record = self._record(request.uri, request.revision_id)
        address = NodeAddress(record.document_id, record.revision_id, xpath)
        if sidecar is not None:
            from .package import load_package

            document = load_package(self.store.packages.get(record.package.sha256))
            part = document.summary() if sidecar == "summary" else document.toc()
            if part is None:
                raise ServiceError("stale_citation", "sidecar is unavailable")
            selected_xml = _xml_node(part.xml(), xpath)
            value = (
                selected_xml
                if request.format == "xml"
                else "".join(ET.fromstring(selected_xml).itertext())
            )
            truncated = len(value) > request.max_chars
            value = value[: request.max_chars]
            page, bbox = None, None
        elif request.format == "text":
            node = self.store.read_node(self.principal, address, request.max_chars)
            value, truncated = node.text, node.truncated
            page, bbox = node.page, node.bbox
        else:
            from .package import load_package

            document = load_package(self.store.packages.get(record.package.sha256))
            value = _xml_node(document.xml(), xpath)
            truncated = len(value) > request.max_chars
            value = value[: request.max_chars]
            page, bbox = None, None
        return self._result(
            {
                "uri": record.uri,
                "document_id": record.document_id,
                "revision_id": record.revision_id,
                "xpath": request.xpath,
                "format": request.format,
                "content": value,
                "truncated": truncated,
                "page": page,
                "bbox": bbox,
            }
        )

    def search(self, request: SearchRequest) -> ServiceResult[dict[str, Any]]:
        if (
            not 1 <= request.limit <= 100
            or not 1 <= request.per_result_tokens <= request.max_tokens <= 20_000
        ):
            raise ServiceError("budget_exceeded", "search limits are out of bounds")
        image = None
        if request.image_base64 is not None:
            try:
                image = base64.b64decode(request.image_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ServiceError(
                    "invalid_request", "image_base64 is invalid"
                ) from exc
            if not image or len(image) > 2_500_000:
                raise ServiceError("budget_exceeded", "image query exceeds 2.5 MB")
        retriever = Retriever(self.store)
        if request.mode in {"vector", "hybrid"}:
            from .embeddings import stored_embedding_provider

            state = self.store.db.execute(
                "SELECT model_id FROM vector_state WHERE singleton=1"
            ).fetchone()
            if state is None:
                raise ServiceError(
                    "unsupported_provider",
                    "vector search needs a configured embedding provider",
                )
            self.store.require_exclusive_vector_scope(self.principal)
            retriever = Retriever(
                self.store, embedder=stored_embedding_provider(state["model_id"])
            )
        started = time.monotonic()
        scope = SearchScope(
            request.uri, request.xpath, request.document_uri, request.tiers
        )
        result = retriever.search(
            self.principal,
            request.query,
            image=image,
            scope=scope,
            k=request.limit,
            mode=request.mode,
            max_tokens=request.max_tokens,
            per_result_tokens=request.per_result_tokens,
        )
        context = None
        if request.include_context:
            context = "\n\n".join(
                f"[{hit.uri} @ {hit.revision_id} {hit.xpath}]\n{hit.text}"
                for hit in result.hits
            )
        return self._result(
            {
                "hits": [asdict(hit) for hit in result.hits],
                "context": context,
                "total_hits": result.total_hits,
                "tokens_used": result.tokens_used,
                "truncated": result.truncated,
                "elapsed_ms": round((time.monotonic() - started) * 1_000),
            }
        )

    def ingest(self, request: IngestRequest) -> ServiceResult[dict[str, Any]]:
        try:
            data = base64.b64decode(request.data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ServiceError("invalid_request", "data_base64 is invalid") from exc
        job = IngestQueue(self.store).submit(
            self.principal,
            request.uri,
            data,
            request.filename,
            force=request.force,
        )
        return self._result(asdict(job))

    def job_status(self, request: JobStatusRequest) -> ServiceResult[dict[str, Any]]:
        try:
            job = IngestQueue(self.store).get(self.principal, request.job_id)
            data = asdict(job)
            if job.status == "failed":
                data["error_code"] = "conversion_failure"
            return self._result(data)
        except KeyError:
            return self._result(
                asdict(
                    CollectionWorker(self.store).get_job(self.principal, request.job_id)
                )
            )

    def session_append(
        self, request: SessionAppendRequest
    ) -> ServiceResult[dict[str, Any]]:
        return self._result(
            asdict(
                SessionStore(self.store).append(
                    self.principal,
                    request.session_id,
                    key=request.key,
                    kind=request.kind,
                    text=request.text,
                    turn_id=request.turn_id,
                )
            )
        )

    def memory_list(
        self, request: MemoryListRequest
    ) -> ServiceResult[builtins.list[dict[str, Any]]]:
        return self._result(
            [
                asdict(item)
                for item in MemoryService(self.store).list(
                    self.principal,
                    status=request.status,
                    kind=request.kind,
                    limit=request.limit,
                    offset=request.offset,
                )
            ]
        )

    def memory_review(
        self, request: MemoryReviewRequest
    ) -> ServiceResult[dict[str, Any]]:
        memory = MemoryService(self.store)
        if request.action == "correct" and request.claim is not None:
            record = memory.correct(self.principal, request.uri, request.claim)
        elif request.action in {"accept", "reject", "delete"}:
            record = memory.transition(
                self.principal,
                request.uri,
                {"accept": "accepted", "reject": "rejected", "delete": "deleted"}[
                    request.action
                ],
                reason=request.reason,
            )
        else:
            raise ServiceError("invalid_request", "unsupported memory review action")
        return self._result(asdict(record))

    def memory_recall(
        self, request: MemoryRecallRequest
    ) -> ServiceResult[builtins.list[dict[str, Any]]]:
        return self._result(
            [
                asdict(item)
                for item in MemoryService(self.store).recall(
                    self.principal,
                    request.session_id,
                    request.query,
                    limit=request.limit,
                )
            ]
        )


def call_service(
    service: ContextService, operation: str, data: dict[str, Any]
) -> dict[str, Any]:
    started = time.monotonic()
    with service._lock:
        result = _dispatch(service, operation, data)
    payload = result.get("data")
    if isinstance(payload, builtins.list):
        count = len(payload)
    elif isinstance(payload, dict):
        count = len(payload.get("hits", ())) if "hits" in payload else 1
    else:
        count = 0
    generation = service.store.db.execute(
        "SELECT generation FROM vector_state WHERE singleton=1"
    ).fetchone()
    _LOG.info(
        "operation_id=%s operation=%s uri_scope=%s latency_ms=%d index_generation=%s result_count=%d",
        result["operation_id"],
        operation,
        data.get("uri", ""),
        round((time.monotonic() - started) * 1_000),
        generation["generation"] if generation else "none",
        count,
    )
    return result


def _dispatch(
    service: ContextService, operation: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch one versioned request and return a stable error envelope."""
    requests = {
        "list": ListRequest,
        "tree": TreeRequest,
        "outline": OutlineRequest,
        "show": ShowRequest,
        "search": SearchRequest,
        "ingest": IngestRequest,
        "job_status": JobStatusRequest,
        "session_append": SessionAppendRequest,
        "memory_list": MemoryListRequest,
        "memory_review": MemoryReviewRequest,
        "memory_recall": MemoryRecallRequest,
    }
    if operation not in requests:
        return {
            "schema_version": SCHEMA_VERSION,
            "operation_id": uuid.uuid4().hex,
            "error": {"code": "invalid_request", "message": "unknown operation"},
        }
    try:
        request = requests[operation](**data)
        return getattr(service, operation)(request).to_dict()
    except ServiceError as exc:
        error = exc.to_dict()
    except InvalidURI as exc:
        error = {"code": "invalid_uri", "message": str(exc)}
    except AccessDenied as exc:
        error = {"code": "unauthorized_scope", "message": str(exc)}
    except RecordMissing as exc:
        error = {"code": "missing_revision", "message": str(exc)}
    except PackageError as exc:
        error = {"code": "package_failure", "message": str(exc)}
    except PackageMissing as exc:
        error = {"code": "package_failure", "message": str(exc)}
    except (KeyError, FileNotFoundError) as exc:
        error = {"code": "stale_citation", "message": str(exc)}
    except (TypeError, ValueError) as exc:
        error = {"code": "invalid_request", "message": str(exc)}
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": uuid.uuid4().hex,
        "error": error,
    }
