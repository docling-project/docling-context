"""All direct interaction with the native DocLang package API lives here."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from doclang import ArchiveLimits, DocLangXDocument
from doclang.types import DoclangNodeRecord

from .models import NodeAddress, NodeContent

MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_NODE_TEXT_CHARS = 65_536
MAX_XPATH_CHARS = 2_048
MAX_XML_DEPTH = 256


class PackageError(ValueError):
    pass


class PackageMissing(FileNotFoundError):
    pass


def _limits() -> ArchiveLimits:
    limits = ArchiveLimits()
    limits.max_archive_bytes = MAX_PACKAGE_BYTES
    limits.max_entries = 10_000
    limits.max_entry_bytes = MAX_PACKAGE_BYTES
    limits.max_total_bytes = 1024 * 1024 * 1024
    limits.max_compression_ratio = 200
    return limits


def _safe_nodes(
    document: DocLangXDocument, xpath: str | None, limit: int, text_bytes: int
) -> list[DoclangNodeRecord]:
    for extra in range(4):
        try:
            return document.iter_nodes(
                xpath, limit=limit, max_text_chars=text_bytes + extra
            )
        except UnicodeDecodeError:
            continue
    raise PackageError("cannot decode bounded node text")


def load_package(data: bytes) -> DocLangXDocument:
    if not isinstance(data, bytes) or not data or len(data) > MAX_PACKAGE_BYTES:
        raise PackageError("package is empty, invalid, or too large")
    document = DocLangXDocument()
    if not document.read_bytes(data, _limits()):
        raise PackageError(document.last_error() or "cannot read DCLX package")
    report = document.validate_package()
    if not report["ok"]:
        raise PackageError(f"invalid DCLX package: {report['errors']}")
    nodes = _safe_nodes(document, None, 100_000, 1)
    if len(nodes) >= 100_000:
        raise PackageError("XML node limit exceeded")
    for node in nodes:
        if node["xpath"].count("/") > MAX_XML_DEPTH:
            raise PackageError("XML depth limit exceeded")
    return document


def read_node(
    data: bytes, address: NodeAddress, max_chars: int = MAX_NODE_TEXT_CHARS
) -> NodeContent:
    if not 1 <= max_chars <= MAX_NODE_TEXT_CHARS:
        raise ValueError("max_chars is out of bounds")
    xpath = address.xpath
    if not xpath.startswith("/doclang[1]") or len(xpath) > MAX_XPATH_CHARS:
        raise ValueError("invalid or oversized XPath")
    document = load_package(data)
    try:
        nodes = _safe_nodes(document, xpath, 1, max_chars * 4)
    except PackageError:
        raise
    except ValueError as exc:
        raise KeyError(xpath) from exc
    if not nodes or nodes[0]["xpath"] != xpath:
        raise KeyError(xpath)
    node = nodes[0]
    text = node["text"]
    return NodeContent(
        address,
        text[:max_chars],
        node["truncated"] or len(text) > max_chars,
        node["page"],
        node["bbox"],
    )


class FilePackageStore:
    """Content-addressed immutable DCLX blobs with verified reads."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("invalid package hash")
        return self.root / sha256[:2] / f"{sha256}.dclx"

    def put(self, data: bytes) -> tuple[str, int]:
        load_package(data)
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self.get(digest)
            return digest, len(data)
        descriptor, name = tempfile.mkstemp(prefix=".incoming-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return digest, len(data)

    def get(self, sha256: str) -> bytes:
        path = self.path_for(sha256)
        try:
            if path.stat().st_size > MAX_PACKAGE_BYTES:
                raise PackageError("package is too large")
            data = path.read_bytes()
        except OSError as exc:
            raise PackageMissing(sha256) from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise PackageError("package hash mismatch")
        return data

    def has(self, sha256: str) -> bool:
        try:
            self.get(sha256)
        except (PackageMissing, PackageError):
            return False
        return True

    def hashes(self) -> set[str]:
        return {p.stem for p in self.root.glob("??/*.dclx") if len(p.stem) == 64}
