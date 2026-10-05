"""Storage contracts used by local and remote implementations."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from doclang import DocLangXDocument

from .models import (
    ChangeEvent,
    DocumentRecord,
    NodeAddress,
    NodeContent,
    Principal,
    TreeEntry,
)


class PackageStorage(Protocol):
    def put(self, data: bytes) -> tuple[str, int]: ...
    def get(self, sha256: str) -> bytes: ...
    def has(self, sha256: str) -> bool: ...


class MetadataLookup(Protocol):
    def get_record(self, principal: Principal, uri: str) -> DocumentRecord: ...
    def list_children(
        self, principal: Principal, uri: str, limit: int = 100
    ) -> list[TreeEntry]: ...
    def walk(
        self, principal: Principal, uri: str, depth: int = 3, limit: int = 100
    ) -> list[TreeEntry]: ...


class EventPublication(Protocol):
    def events(
        self, principal: Principal, after: int = 0, limit: int = 100
    ) -> list[ChangeEvent]: ...


class TransactionProvider(Protocol):
    def transaction(self) -> AbstractContextManager[object]: ...


class ContextStore(MetadataLookup, EventPublication, Protocol):
    def put(
        self,
        principal: Principal,
        uri: str,
        package: bytes,
        *,
        expected_revision: str | None = None,
        source: dict | None = None,
        revision_id: str | None = None,
    ) -> DocumentRecord: ...
    def get_package(self, principal: Principal, uri: str) -> bytes: ...
    def get_document(self, principal: Principal, uri: str) -> DocLangXDocument: ...
    def get_revision(
        self, principal: Principal, address: NodeAddress
    ) -> DocumentRecord: ...
    def read_node(
        self, principal: Principal, address: NodeAddress, max_chars: int = 65_536
    ) -> NodeContent: ...
    def delete(
        self, principal: Principal, uri: str, *, expected_revision: str
    ) -> None: ...
