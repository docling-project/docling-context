"""Local-first context storage backed by DocLang packages."""

from .contracts import ContextStore
from .conversion_config import PdfConversionConfig
from .converters import (
    ConversionResult,
    Converter,
    DoclingServeConverter,
    LocalDoclingConverter,
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
from .uri import AccessDenied, ContextURI, InvalidURI, authorize_uri, parse_uri

__all__ = [
    "AccessDenied",
    "ChangeEvent",
    "CollectionStatus",
    "CollectionWorker",
    "ContextStore",
    "ContextURI",
    "ConversionResult",
    "Converter",
    "DoclingServeConverter",
    "DocumentRecord",
    "FilePackageStore",
    "Ingestor",
    "InvalidURI",
    "Job",
    "LocalContextStore",
    "LocalDoclingConverter",
    "MemoryContextStore",
    "NodeAddress",
    "NodeContent",
    "PackageError",
    "PackageMissing",
    "PackageRef",
    "PdfConversionConfig",
    "Principal",
    "RecordMissing",
    "RevisionConflict",
    "SummaryProvider",
    "TenantScope",
    "TreeEntry",
    "authorize_uri",
    "parse_uri",
]
