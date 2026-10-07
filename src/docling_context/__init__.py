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
from .retrieval import ContextResult, RetrievalHit, Retriever, SearchScope
from .sessions import SessionEvent, SessionInfo, SessionStore
from .uri import AccessDenied, ContextURI, InvalidURI, authorize_uri, parse_uri
from .vectors import EmbeddingProvider, TurbovecIndex, VectorIndex

__all__ = [
    "CLIP_MODEL",
    "DEFAULT_MODEL",
    "AccessDenied",
    "ChangeEvent",
    "CollectionStatus",
    "CollectionWorker",
    "ContextResult",
    "ContextStore",
    "ContextURI",
    "ConversionResult",
    "Converter",
    "DoclingServeConverter",
    "DocumentRecord",
    "EmbeddingProvider",
    "FastEmbedProvider",
    "FilePackageStore",
    "Ingestor",
    "InvalidURI",
    "Job",
    "LocalContextStore",
    "LocalDoclingConverter",
    "MemoryCompiler",
    "MemoryContextStore",
    "MemoryJob",
    "MemoryRecord",
    "MemoryService",
    "MlxEmbeddingProvider",
    "NodeAddress",
    "NodeContent",
    "PackageError",
    "PackageMissing",
    "PackageRef",
    "PdfConversionConfig",
    "Principal",
    "RecordMissing",
    "RetrievalHit",
    "Retriever",
    "RevisionConflict",
    "SearchScope",
    "SessionEvent",
    "SessionInfo",
    "SessionStore",
    "SourceCitation",
    "SummaryProvider",
    "TenantScope",
    "TreeEntry",
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
