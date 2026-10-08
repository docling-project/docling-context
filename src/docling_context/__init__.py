"""Local-first context storage backed by DocLang packages."""

from .compiler import MemoryCompiler, MemoryJob, redact_secrets
from .contracts import ContextStore
from .conversion_config import PdfConversionConfig
from .converters import (
    ConversionResult,
    Converter,
    DoclingServeConverter,
    LocalDoclingConverter,
)
from .durable_memory import MemoryRecord, MemoryService, SourceCitation
from .embeddings import (
    CLIP_MODEL,
    DEFAULT_MODEL,
    FastEmbedProvider,
    MlxEmbeddingProvider,
    embed_image,
    embed_text,
    embed_text_image,
    local_embedding_provider,
    stored_embedding_provider,
)
from .ingest_queue import IngestJob, IngestQueue
from .ingestion import Ingestor, SummaryProvider
from .jobs import CollectionStatus, CollectionWorker, Job
from .local import LocalContextStore, RecordMissing, RevisionConflict
from .memory import MemoryContextStore
from .models import (
    ChangeEvent,
    DocumentRecord,
    NodeAddress,
    NodeContent,
    PackageRef,
    Principal,
    TenantScope,
    TreeEntry,
)
from .package import FilePackageStore, PackageError, PackageMissing
from .remote import RemoteContextService
from .retrieval import ContextResult, RetrievalHit, Retriever, SearchScope
from .service import (
    SCHEMA_VERSION,
    ContextService,
    IngestRequest,
    JobStatusRequest,
    ListRequest,
    MemoryListRequest,
    MemoryRecallRequest,
    MemoryReviewRequest,
    OutlineRequest,
    SearchRequest,
    ServiceError,
    ServiceResult,
    SessionAppendRequest,
    ShowRequest,
    TreeRequest,
)
from .sessions import SessionEvent, SessionInfo, SessionStore
from .uri import AccessDenied, ContextURI, InvalidURI, authorize_uri, parse_uri
from .vectors import EmbeddingProvider, TurbovecIndex, VectorIndex

__all__ = [
    "CLIP_MODEL",
    "DEFAULT_MODEL",
    "SCHEMA_VERSION",
    "AccessDenied",
    "ChangeEvent",
    "CollectionStatus",
    "CollectionWorker",
    "ContextResult",
    "ContextService",
    "ContextStore",
    "ContextURI",
    "ConversionResult",
    "Converter",
    "DoclingServeConverter",
    "DocumentRecord",
    "EmbeddingProvider",
    "FastEmbedProvider",
    "FilePackageStore",
    "IngestJob",
    "IngestQueue",
    "IngestRequest",
    "Ingestor",
    "InvalidURI",
    "Job",
    "JobStatusRequest",
    "ListRequest",
    "LocalContextStore",
    "LocalDoclingConverter",
    "MemoryCompiler",
    "MemoryContextStore",
    "MemoryJob",
    "MemoryListRequest",
    "MemoryRecallRequest",
    "MemoryRecord",
    "MemoryReviewRequest",
    "MemoryService",
    "MlxEmbeddingProvider",
    "NodeAddress",
    "NodeContent",
    "OutlineRequest",
    "PackageError",
    "PackageMissing",
    "PackageRef",
    "PdfConversionConfig",
    "Principal",
    "RecordMissing",
    "RemoteContextService",
    "RetrievalHit",
    "Retriever",
    "RevisionConflict",
    "SearchRequest",
    "SearchScope",
    "ServiceError",
    "ServiceResult",
    "SessionAppendRequest",
    "SessionEvent",
    "SessionInfo",
    "SessionStore",
    "ShowRequest",
    "SourceCitation",
    "SummaryProvider",
    "TenantScope",
    "TreeEntry",
    "TreeRequest",
    "TurbovecIndex",
    "VectorIndex",
    "authorize_uri",
    "embed_image",
    "embed_text",
    "embed_text_image",
    "local_embedding_provider",
    "parse_uri",
    "redact_secrets",
    "stored_embedding_provider",
]
