"""Versioned records shared by local and future remote backends."""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    user_id: str
    version: int = 1

    def __post_init__(self) -> None:
        for name in ("tenant_id", "user_id"):
            value = unicodedata.normalize("NFC", getattr(self, name))
            if (
                not value
                or value in {".", ".."}
                or any(c in value for c in "/\\%?#\x00")
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise ValueError("principal needs safe tenant and user identifiers")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TenantScope:
    tenant_id: str
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PackageRef:
    sha256: str
    size_bytes: int
    version: int = 1


@dataclass(frozen=True, slots=True)
class NodeAddress:
    document_id: str
    revision_id: str
    xpath: str
    version: int = 1


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    uri: str
    tenant_id: str
    document_id: str
    revision_id: str
    package: PackageRef
    parent_uri: str | None
    source: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TreeEntry:
    uri: str
    kind: Literal["directory", "document"]
    depth: int
    document_id: str | None = None
    revision_id: str | None = None
    version: int = 1


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    sequence: int
    tenant_id: str
    uri: str
    document_id: str
    revision_id: str
    operation: Literal["put", "delete"]
    occurred_at: str
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NodeContent:
    address: NodeAddress
    text: str
    truncated: bool
    page: int | None
    bbox: tuple[tuple[float, float], tuple[float, float]] | None
    version: int = 1
